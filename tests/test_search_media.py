"""search_media finds text in voice notes and images within its download caps."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from telegram_mcp import media_search, runtime, transcription
from telegram_mcp.tools import media as media_tools

CHAT = -1001234567890
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
MB = 1048576


def voice(message_id, size=50_000):
    return SimpleNamespace(
        id=message_id,
        date=NOW,
        sender=None,
        voice=object(),
        file=SimpleNamespace(size=size, duration=7),
    )


def photo(message_id, size=200_000):
    return SimpleNamespace(
        id=message_id,
        date=NOW,
        sender=None,
        photo=object(),
        file=SimpleNamespace(size=size, mime_type="image/jpeg"),
    )


class FakeClient:
    def __init__(self, history):
        self.history = history
        self.limits = []
        self.downloads = []

    async def get_messages(self, entity, limit):
        self.limits.append(limit)
        return self.history[:limit]

    async def download_media(self, msg, file):
        assert file is bytes
        self.downloads.append(msg.id)
        return b"image-bytes-" + str(msg.id).encode()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(runtime, "is_multi_mode", lambda: False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setattr(
        media_tools, "resolve_entity", AsyncMock(return_value=types.User(id=1234567890))
    )
    monkeypatch.setattr(transcription, "transcribe_mode", lambda: "on-demand")
    monkeypatch.setattr(transcription, "default_engine", lambda: "groq")
    monkeypatch.setattr(transcription, "engine_config_error", lambda engine: None)
    monkeypatch.setattr(media_search, "tesseract_path", lambda: "/usr/bin/tesseract")
    media_search._ocr_cache.clear()

    state = SimpleNamespace(cached={}, transcribed=[], ocr_texts={})

    def cached(chat_id, message_id, source=None):
        text = state.cached.get(message_id)
        return {"text": text, "source": "groq"} if text is not None else None

    async def transcribe_cached(cl, entity, msg, engine, chat_id, duration=None):
        state.transcribed.append(msg.id)
        return {"status": "ok", "text": f"fresh transcript {msg.id}", "source": engine}

    async def run_ocr(data):
        message_id = int(data.decode().rsplit("-", 1)[1])
        return state.ocr_texts.get(message_id, "")

    monkeypatch.setattr(transcription, "get_cached_transcript", cached)
    monkeypatch.setattr(transcription, "transcribe_cached", transcribe_cached)
    monkeypatch.setattr(media_search, "run_ocr", run_ocr)

    def install(history):
        client = FakeClient(history)
        monkeypatch.setattr(media_tools, "get_client", lambda account=None: client)
        state.client = client
        return client

    state.install = install
    return state


async def search(**kwargs):
    args = {"chat_id": CHAT, "query": "drinks", **kwargs}
    return json.loads(await media_tools.search_media(**args))


@pytest.mark.asyncio
async def test_cached_voice_transcript_matches_without_new_transcription(setup):
    setup.install([voice(1), voice(2)])
    setup.cached = {1: "Alice will bring Drinks and cake", 2: "nothing here"}

    result = await search()

    assert [m["message_id"] for m in result["matches"]] == [1]
    match = result["matches"][0]
    assert match["kind"] == "voice"
    assert match["source"] == "transcript:groq"
    assert "Drinks" in match["snippet"]
    assert setup.transcribed == []
    assert result["new_files_used"] == 0


@pytest.mark.asyncio
async def test_uncached_voice_is_transcribed_on_demand(setup):
    setup.install([voice(3)])

    result = await search(query="fresh transcript")

    assert setup.transcribed == [3]
    assert [m["message_id"] for m in result["matches"]] == [3]
    assert result["new_files_used"] == 1


@pytest.mark.asyncio
async def test_image_text_is_found_by_ocr(setup):
    setup.install([photo(10), photo(11)])
    setup.ocr_texts = {10: "Menu: soup, bread", 11: "Bring DRINKS by 6pm"}

    result = await search()

    assert [m["message_id"] for m in result["matches"]] == [11]
    assert result["matches"][0]["source"] == "ocr"
    assert setup.client.downloads == [10, 11]
    assert result["media_checked"] == {"image": 2, "voice": 0}


@pytest.mark.asyncio
async def test_ocr_results_are_reused_without_downloading_again(setup):
    setup.install([photo(12)])
    setup.ocr_texts = {12: "drinks list"}

    await search()
    result = await search()

    assert setup.client.downloads == [12]
    assert result["new_files_used"] == 0
    assert len(result["matches"]) == 1


@pytest.mark.asyncio
async def test_new_file_budget_caps_downloads(setup):
    setup.install([photo(20), photo(21), photo(22)])

    result = await search(max_new_files=1)

    assert setup.client.downloads == [20]
    assert result["new_files_used"] == 1
    reasons = {s["message_id"]: s["reason"] for s in result["skipped"]}
    assert reasons == {21: "max_new_files reached", 22: "max_new_files reached"}


@pytest.mark.asyncio
async def test_large_or_unknown_size_media_is_never_downloaded(setup):
    setup.install([photo(30, size=15 * MB), photo(31, size=None), voice(32, size=12 * MB)])

    result = await search(max_file_mb=10)

    assert setup.client.downloads == []
    assert setup.transcribed == []
    reasons = {s["message_id"]: s["reason"] for s in result["skipped"]}
    assert "above max_file_mb" in reasons[30]
    assert reasons[31] == "size unknown, not downloaded"
    assert "above max_file_mb" in reasons[32]


@pytest.mark.asyncio
async def test_caps_are_clamped(setup):
    setup.install([])

    result = await search(limit=10_000, max_new_files=999, max_file_mb=500)

    assert setup.client.limits == [media_tools.SEARCH_MEDIA_MAX_SCAN]
    assert result["scanned_messages"] == 0


@pytest.mark.asyncio
async def test_without_tesseract_images_are_skipped_and_voice_still_works(setup, monkeypatch):
    monkeypatch.setattr(media_search, "tesseract_path", lambda: None)
    setup.install([photo(40), voice(41)])
    setup.cached = {41: "who brings drinks?"}

    result = await search()

    assert [m["message_id"] for m in result["matches"]] == [41]
    assert result["skipped"] == [
        {"message_id": 40, "kind": "image", "reason": "OCR unavailable (tesseract not installed)"}
    ]
    assert result["ocr_status"].startswith("unavailable")
    assert setup.client.downloads == []


@pytest.mark.asyncio
async def test_kinds_filter_and_transcription_off(setup, monkeypatch):
    monkeypatch.setattr(transcription, "transcribe_mode", lambda: "off")
    setup.install([photo(50), voice(51)])

    result = await search(kinds=["voice"])

    assert result["media_checked"] == {"voice": 1}
    assert result["skipped"][0]["reason"].startswith("transcription disabled")
    assert setup.client.downloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, expected",
    [({"query": "  "}, "query must not be empty"), ({"kinds": ["video"]}, "unknown kinds")],
)
async def test_invalid_arguments(setup, kwargs, expected):
    setup.install([])
    result = await media_tools.search_media(chat_id=CHAT, **{"query": "x", **kwargs})
    assert expected in result


def test_search_media_is_read_only():
    tool = runtime.mcp._tool_manager.get_tool("search_media")
    assert tool.annotations.readOnlyHint is True


def test_snippet_windows_around_the_match():
    text = "a" * 200 + " Drinks here " + "b" * 200
    snippet = media_search.find_snippet(text, "drinks")
    assert snippet.startswith("…") and snippet.endswith("…")
    assert "Drinks here" in snippet
    assert media_search.find_snippet("nothing", "drinks") is None
