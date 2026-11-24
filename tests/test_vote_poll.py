"""Exercise poll voting through the registered tool and real Telethon TL objects."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon import errors, types
from telethon.tl.functions.messages import SendVoteRequest

from telegram_mcp import runtime
from telegram_mcp.tools import messages


@pytest.fixture
def voting(monkeypatch):
    poll = types.Poll(
        id=77,
        hash=0,
        question=types.TextWithEntities("Pick a drink", []),
        answers=[
            types.PollAnswer(types.TextWithEntities(text, []), option)
            for text, option in [
                ("Tea", b"tea-id"),
                ("Water", b"\xff\x10"),
                ("Juice", b"juice-id"),
            ]
        ],
        closed=False,
        multiple_choice=False,
    )
    message = types.Message(
        id=42,
        peer_id=types.PeerChat(123),
        date=datetime.now(timezone.utc),
        message="",
        media=types.MessageMediaPoll(poll, types.PollResults()),
    )
    client = AsyncMock()
    client.get_messages.return_value = message
    entity = types.InputPeerChat(123)
    get_client = Mock(return_value=client)
    monkeypatch.setattr(messages, "get_client", get_client)
    monkeypatch.setattr(messages, "resolve_entity", AsyncMock(return_value=entity))
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    return SimpleNamespace(
        poll=poll, message=message, client=client, entity=entity, get_client=get_client
    )


async def call_vote(**kwargs):
    args = {"chat_id": -123, "message_id": 42, "option_indices": [1], **kwargs}
    return await messages.vote_poll(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize("indices", [[1], [2, 0]])
async def test_vote_sends_real_option_bytes(voting, indices):
    voting.poll.multiple_choice = len(indices) > 1
    result = json.loads(await call_vote(option_indices=indices, account="personal"))
    assert result["action"] == "voted"
    assert result["submitted"] == (["Water"] if indices == [1] else ["Juice", "Tea"])
    assert "quiz_correct" not in result
    voting.get_client.assert_called_once_with("personal")
    assert voting.client.get_messages.await_args_list[0].args == (voting.entity,)
    assert voting.client.get_messages.await_args_list[0].kwargs == {"ids": 42}
    request = voting.client.call_args.args[0]
    assert isinstance(request, SendVoteRequest)
    assert request.peer == voting.entity
    assert request.msg_id == 42
    assert request.options == ([b"\xff\x10"] if indices == [1] else [b"juice-id", b"tea-id"])
    assert bytes(request)  # Request is serializable by Telethon, not just a mock.
    voting.client.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args, expected",
    [
        ({"message_id": 0}, "positive integer"),
        ({"message_id": -1}, "positive integer"),
        ({"option_indices": [True]}, "list of integers"),
        ({"option_indices": [1.0]}, "list of integers"),
        ({"option_indices": [1, 1]}, "duplicates"),
        ({"option_indices": [-1]}, "out of range"),
        ({"option_indices": [3]}, "out of range"),
        ({"option_indices": [0, 1]}, "only allows one answer"),
    ],
)
async def test_invalid_votes_never_send(voting, args, expected):
    assert expected in await call_vote(**args)
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "plain", "closed"])
async def test_non_poll_and_closed_poll_never_send(voting, kind):
    if kind == "missing":
        voting.client.get_messages.return_value = None
    elif kind == "plain":
        voting.message.media = None
    else:
        voting.poll.closed = True
    result = await call_vote()
    assert ("Poll is closed" if kind == "closed" else "does not contain a poll") in result
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
async def test_rpc_error_is_not_reported_as_success(voting):
    voting.client.side_effect = errors.MessagePollClosedError(request=None)
    result = await call_vote()
    assert "Vote submitted" not in result
    assert "Error" in result or "error" in result


@pytest.mark.asyncio
async def test_multi_account_requires_explicit_account(voting, monkeypatch):
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: True)
    monkeypatch.setattr(runtime, "clients", {"personal": voting.client, "work": AsyncMock()})
    assert "'account' is required" in await call_vote()
    voting.client.get_messages.assert_not_awaited()
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_allowlist_blocks_vote(voting, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-456")
    assert "restricted by privacy policy" in await call_vote()
    voting.client.get_messages.assert_not_awaited()
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_chat_is_checked_before_reading(voting, monkeypatch):
    monkeypatch.setattr(messages, "is_chat_allowlist_enabled", lambda: True)
    monkeypatch.setattr(messages, "is_chat_allowed", lambda *args: False)
    monkeypatch.setattr(
        messages, "check_chat_access", lambda *args: "restricted by privacy policy"
    )
    result = await call_vote()
    assert "Vote submitted" not in result
    voting.client.get_messages.assert_not_awaited()
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_tool_accepts_username_and_rejects_coercion(voting):
    tool = next(t for t in messages.mcp._tool_manager.list_tools() if t.name == "vote_poll")
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    result = await tool.run({"chat_id": "@example_chat", "message_id": 42, "option_indices": [1]})
    assert json.loads(result)["action"] == "voted"
    for invalid in [True, 1.0, "1"]:
        voting.client.reset_mock()
        with pytest.raises(ToolError):
            await tool.run({"chat_id": -123, "message_id": 42, "option_indices": [invalid]})
        voting.client.assert_not_awaited()


def test_vote_exported_for_compatibility_entrypoint():
    from telegram_mcp.tools import vote_poll

    assert vote_poll is messages.vote_poll


def refreshed(voting, counts, chosen, correct=(), **results):
    """Make the post-vote re-read return a poll with Telegram's updated results."""
    options = [answer.option for answer in voting.poll.answers]
    after = types.Message(
        id=42,
        peer_id=types.PeerChat(123),
        date=datetime.now(timezone.utc),
        message="",
        media=types.MessageMediaPoll(
            voting.poll,
            types.PollResults(
                results=[
                    types.PollAnswerVoters(
                        option=o,
                        voters=n,
                        chosen=o in chosen or None,
                        correct=o in correct or None,
                    )
                    for o, n in zip(options, counts)
                ],
                total_voters=sum(counts),
                **results,
            ),
        ),
    )
    voting.client.get_messages.side_effect = [voting.message, after]


@pytest.mark.asyncio
async def test_vote_returns_now_visible_results(voting):
    refreshed(voting, [2, 1, 0], chosen={b"\xff\x10"})

    result = json.loads(await call_vote(option_indices=[1]))

    poll = result["poll"]
    assert poll["voted"] is True
    assert poll["chosen_by_me"] == ["Water"]
    assert [o["votes"] for o in poll["options"]] == [2, 1, 0]
    assert poll["results_visible"] is True
    assert "results_note" not in poll


@pytest.mark.asyncio
async def test_empty_selection_retracts_vote(voting):
    refreshed(voting, [2, 0, 0], chosen=set())

    result = json.loads(await call_vote(option_indices=[]))

    request = voting.client.call_args.args[0]
    assert isinstance(request, SendVoteRequest)
    assert request.options == []
    assert bytes(request)
    assert result["action"] == "retracted"
    assert result["submitted"] == []
    assert result["poll"]["voted"] is False


@pytest.mark.asyncio
async def test_retract_on_closed_poll_never_sends(voting):
    voting.poll.closed = True
    assert "Poll is closed" in await call_vote(option_indices=[])
    voting.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("picked, expected", [(1, True), (0, False)])
async def test_quiz_vote_reports_correctness(voting, picked, expected):
    voting.poll.quiz = True
    option = voting.poll.answers[picked].option
    refreshed(voting, [1, 1, 0], chosen={option}, correct={b"\xff\x10"}, solution="Water hydrates")

    result = json.loads(await call_vote(option_indices=[picked]))

    assert result["quiz_correct"] is expected
    assert result["poll"]["correct_answers"] == ["Water"]
    assert result["poll"]["solution"] == "Water hydrates"


@pytest.mark.asyncio
async def test_quiz_correctness_unknown_when_telegram_hides_it(voting):
    voting.poll.quiz = True
    refreshed(voting, [1, 0, 0], chosen={b"tea-id"})

    result = json.loads(await call_vote(option_indices=[0]))

    assert result["quiz_correct"] is None
