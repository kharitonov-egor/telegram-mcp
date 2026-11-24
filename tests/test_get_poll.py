"""get_poll and inline poll rendering read polls without ever voting."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, types
from telethon.tl.functions.messages import GetPollVotesRequest

from telegram_mcp import runtime
from telegram_mcp.tools import media as media_tools
from telegram_mcp.tools import messages

OPTIONS = [("Salad", b"\x00"), ("Pie", b"\x01"), ("Drinks", b"\x02")]


def make_poll(**flags):
    return types.Poll(
        id=77,
        hash=0,
        question=types.TextWithEntities("Who brings what?", []),
        answers=[types.PollAnswer(types.TextWithEntities(t, []), o) for t, o in OPTIONS],
        **flags,
    )


def make_results(counts=None, chosen=(), correct=(), total=None, **extra):
    results = None
    if counts is not None:
        results = [
            types.PollAnswerVoters(
                option=o,
                voters=n,
                chosen=o in chosen or None,
                correct=o in correct or None,
            )
            for (_, o), n in zip(OPTIONS, counts)
        ]
    return types.PollResults(results=results, total_voters=total, **extra)


def make_message(poll, results):
    return types.Message(
        id=42,
        peer_id=types.PeerChat(1234567890),
        date=datetime(2026, 11, 20, tzinfo=timezone.utc),
        message="",
        media=types.MessageMediaPoll(poll, results),
    )


def user(uid, first, last=None, username=None):
    return types.User(id=uid, first_name=first, last_name=last, username=username)


class FakeClient:
    def __init__(self, message, vote_pages=None, vote_error=None):
        self.message = message
        self.vote_pages = list(vote_pages or [])
        self.vote_error = vote_error
        self.requests = []

    async def get_messages(self, entity, ids):
        assert ids == 42
        return self.message

    async def __call__(self, request):
        self.requests.append(request)
        assert isinstance(request, GetPollVotesRequest), f"unexpected {type(request).__name__}"
        if self.vote_error is not None:
            raise self.vote_error
        return self.vote_pages.pop(0)


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    entity = types.Chat(
        id=1234567890,
        title="Test Group",
        photo=None,
        participants_count=5,
        date=None,
        version=1,
    )
    monkeypatch.setattr(messages, "resolve_entity", AsyncMock(return_value=entity))

    def _install(client):
        monkeypatch.setattr(messages, "get_client", Mock(return_value=client))
        return client

    return _install


async def read_poll(install, message, **client_kwargs):
    client = install(FakeClient(message, **client_kwargs))
    raw = await messages.get_poll(chat_id=-1234567890, message_id=42)
    return json.loads(raw), client


@pytest.mark.asyncio
async def test_voted_anonymous_poll_reports_counts_and_my_choice(install):
    message = make_message(make_poll(), make_results([3, 1, 0], chosen={b"\x01"}, total=4))

    result, client = await read_poll(install, message)

    assert result["question"] == "Who brings what?"
    assert [(o["text"], o["votes"]) for o in result["options"]] == [
        ("Salad", 3),
        ("Pie", 1),
        ("Drinks", 0),
    ]
    assert result["total_voters"] == 4
    assert result["voted"] is True
    assert result["chosen_by_me"] == ["Pie"]
    assert result["anonymous"] is True and result["public_voters"] is False
    assert result["results_visible"] is True
    assert "results_note" not in result
    assert "voters" not in result["options"][0]
    assert client.requests == []


@pytest.mark.asyncio
async def test_not_voted_with_hidden_results_says_so_and_never_votes(install):
    message = make_message(make_poll(public_voters=True), make_results(total=6))

    result, client = await read_poll(
        install, message, vote_error=errors.RPCError(None, "POLL_VOTE_REQUIRED", 400)
    )

    assert result["voted"] is False
    assert result["chosen_by_me"] == []
    assert result["results_visible"] is False
    assert result["total_voters"] == 6
    assert "has not voted" in result["results_note"]
    assert all("votes" not in o and "voters" not in o for o in result["options"])
    assert "voters_note" in result
    assert all(isinstance(r, GetPollVotesRequest) for r in client.requests)


@pytest.mark.asyncio
async def test_min_results_are_flagged_as_partial(install):
    message = make_message(make_poll(), make_results([2, 2, 0], total=4, min=True))

    result, _ = await read_poll(install, message)

    assert result["results_visible"] is False
    assert "partial (min)" in result["results_note"]


@pytest.mark.asyncio
async def test_quiz_reports_correct_answer_and_solution(install):
    poll = make_poll(quiz=True, closed=True)
    results = make_results(
        [1, 4, 0], chosen={b"\x00"}, correct={b"\x01"}, total=5, solution="Pie wins every year"
    )

    result, _ = await read_poll(install, make_message(poll, results))

    assert result["quiz"] is True
    assert result["closed"] is True
    assert result["correct_answers"] == ["Pie"]
    assert result["solution"] == "Pie wins every year"
    assert result["chosen_by_me"] == ["Salad"]
    assert result["options"][1]["correct"] is True


@pytest.mark.asyncio
async def test_multiple_choice_lists_every_chosen_option(install):
    poll = make_poll(multiple_choice=True)
    results = make_results([2, 1, 3], chosen={b"\x00", b"\x02"}, total=4)

    result, _ = await read_poll(install, make_message(poll, results))

    assert result["multiple_choice"] is True
    assert result["chosen_by_me"] == ["Salad", "Drinks"]


@pytest.mark.asyncio
async def test_closed_poll_without_my_vote_has_no_hidden_note(install):
    message = make_message(make_poll(closed=True), make_results([1, 0, 2], total=3))

    result, _ = await read_poll(install, message)

    assert result["closed"] is True
    assert result["voted"] is False
    assert result["results_visible"] is True
    assert "results_note" not in result


@pytest.mark.asyncio
async def test_public_voters_are_paged_and_named(install):
    poll = make_poll(public_voters=True, multiple_choice=True)
    message = make_message(poll, make_results([2, 1, 1], chosen={b"\x00"}, total=3))
    pages = [
        types.messages.VotesList(
            count=3,
            votes=[
                types.MessagePeerVote(types.PeerUser(1), b"\x00", None),
                types.MessagePeerVoteMultiple(types.PeerUser(2), [b"\x00", b"\x02"], None),
            ],
            chats=[],
            users=[user(1, "Alice", "Smith", "alice"), user(2, "Bob", "Jones")],
            next_offset="next",
        ),
        types.messages.VotesList(
            count=3,
            votes=[types.MessagePeerVote(types.PeerUser(3), b"\x01", None)],
            chats=[],
            users=[user(3, "Sam")],
            next_offset=None,
        ),
    ]

    result, client = await read_poll(install, message, vote_pages=pages)

    assert [r.offset for r in client.requests] == [None, "next"]
    assert all(r.limit == messages.POLL_VOTES_PAGE_SIZE for r in client.requests)
    salad, pie, drinks = result["options"]
    assert salad["voters"] == [
        {"id": 1, "name": "Alice Smith", "username": "alice"},
        {"id": 2, "name": "Bob Jones"},
    ]
    assert pie["voters"] == [{"id": 3, "name": "Sam"}]
    assert drinks["voters"] == [{"id": 2, "name": "Bob Jones"}]
    assert result["voters_listed"] == 3
    assert result["voters_total"] == 3
    assert result["voters_truncated"] is False


@pytest.mark.asyncio
async def test_public_voter_paging_stops_at_cap(install, monkeypatch):
    monkeypatch.setattr(messages, "POLL_VOTERS_CAP", 2)
    poll = make_poll(public_voters=True)
    message = make_message(poll, make_results([5, 0, 0], chosen={b"\x00"}, total=5))
    page = types.messages.VotesList(
        count=5,
        votes=[types.MessagePeerVote(types.PeerUser(i), b"\x00", None) for i in (1, 2)],
        chats=[],
        users=[user(1, "A"), user(2, "B")],
        next_offset="more",
    )

    result, client = await read_poll(install, message, vote_pages=[page])

    assert len(client.requests) == 1
    assert client.requests[0].limit == 2
    assert result["voters_listed"] == 2
    assert result["voters_truncated"] is True


@pytest.mark.asyncio
async def test_non_poll_message_is_reported(install):
    client = install(FakeClient(SimpleNamespace(media=None)))

    result = await messages.get_poll(chat_id=-1234567890, message_id=42)

    assert "not a poll" in result
    assert client.requests == []


def test_get_poll_is_exposed_by_read_only_preset():
    registered = {tool.name: tool for tool in runtime.mcp._tool_manager.list_tools()}
    assert registered["get_poll"].annotations.readOnlyHint is True


def test_message_line_renders_poll_instead_of_empty():
    message = make_message(make_poll(), make_results([3, 1, 0], chosen={b"\x01"}, total=4))

    line = messages.format_message_line(message)

    assert "[empty]" not in line
    assert "📎 poll" in line
    assert "Poll: Who brings what?; options: [0] Salad (3), [1] Pie (1) ✓me" in line
    assert "4 voters, anonymous" in line


def test_message_line_flags_hidden_results():
    message = make_message(make_poll(), make_results(total=2))

    line = messages.format_message_line(message)

    assert "[0] Salad, [1] Pie, [2] Drinks" in line
    assert "results hidden until you vote" in line


@pytest.mark.asyncio
async def test_media_info_renders_poll(monkeypatch):
    message = make_message(make_poll(quiz=True), make_results([0, 1, 0], chosen={b"\x01"}))
    client = AsyncMock()
    client.get_messages.return_value = message
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: False)
    monkeypatch.setattr(media_tools, "get_client", Mock(return_value=client))
    monkeypatch.setattr(media_tools, "resolve_entity", AsyncMock(return_value=object()))

    result = await media_tools.get_media_info(chat_id=-1234567890, message_id=42)

    assert result.startswith("Poll: Who brings what?")
    assert "quiz" in result
    assert "get_poll" in result
