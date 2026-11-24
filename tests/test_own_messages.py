"""edit_message and delete_message only touch messages this account sent."""

from types import SimpleNamespace

import pytest
from telethon import types

from telegram_mcp import runtime
from telegram_mcp.tools import messages

ME = 7
CHAT = -1001234567890


class OwnClient:
    def __init__(self, message):
        self.message = message
        self.edited = []
        self.deleted = []

    async def get_messages(self, entity, ids):
        if isinstance(ids, list):
            return [self.message for _ in ids]
        return self.message

    async def get_me(self, input_peer=False):
        assert input_peer is True
        return types.InputPeerUser(user_id=ME, access_hash=0)

    async def edit_message(self, entity, message_id, text, **kwargs):
        self.edited.append((message_id, text))

    async def delete_messages(self, entity, message_ids):
        self.deleted.append(message_ids)


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)

    async def resolve(chat_id, cl=None):
        return types.User(id=1234567890)

    monkeypatch.setattr(messages, "resolve_entity", resolve)

    def _install(message):
        client = OwnClient(message)
        monkeypatch.setattr(messages, "get_client", lambda account=None: client)
        return client

    return _install


def message(out=False, sender_id=None):
    return SimpleNamespace(id=42, out=out, sender_id=sender_id)


@pytest.mark.asyncio
async def test_edit_own_outgoing_message(install):
    client = install(message(out=True))

    result = await messages.edit_message(chat_id=CHAT, message_id=42, new_text="fixed")

    assert result == "Message 42 edited."
    assert client.edited == [(42, "fixed")]


@pytest.mark.asyncio
async def test_edit_own_message_matched_by_sender_id(install):
    client = install(message(out=False, sender_id=ME))

    result = await messages.edit_message(chat_id=CHAT, message_id=42, new_text="fixed")

    assert result == "Message 42 edited."
    assert client.edited == [(42, "fixed")]


@pytest.mark.asyncio
async def test_edit_refuses_someone_elses_message(install):
    client = install(message(out=False, sender_id=99))

    result = await messages.edit_message(chat_id=CHAT, message_id=42, new_text="hijack")

    assert "was not sent by this account" in result
    assert "Nothing was edited" in result
    assert client.edited == []


@pytest.mark.asyncio
async def test_edit_refuses_message_outside_chat(install):
    client = install(None)

    result = await messages.edit_message(chat_id=CHAT, message_id=42, new_text="x")

    assert result == f"Message 42 is not in chat {CHAT}; nothing was edited."
    assert client.edited == []


@pytest.mark.asyncio
async def test_delete_refuses_someone_elses_message(install):
    client = install(message(out=False, sender_id=99))

    result = await messages.delete_message(chat_id=CHAT, message_id=42)

    assert "was not sent by this account" in result
    assert "Nothing was deleted" in result
    assert client.deleted == []


@pytest.mark.asyncio
async def test_delete_own_message(install):
    client = install(message(out=True))

    result = await messages.delete_message(chat_id=CHAT, message_id=42)

    assert result == "Message 42 deleted."
    assert client.deleted == [42]


@pytest.mark.asyncio
async def test_delete_refuses_message_outside_chat(install):
    client = install(None)

    result = await messages.delete_message(chat_id=CHAT, message_id=42)

    assert result == f"Message 42 is not in chat {CHAT}; nothing was deleted."
    assert client.deleted == []


def test_write_annotations():
    tools = {t.name: t for t in runtime.mcp._tool_manager.list_tools()}
    assert tools["delete_message"].annotations.destructiveHint is True
    assert not tools["delete_message"].annotations.readOnlyHint
    assert not tools["edit_message"].annotations.readOnlyHint
