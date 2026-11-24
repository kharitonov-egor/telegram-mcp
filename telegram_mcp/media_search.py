"""Text extraction for search_media: OCR through the tesseract binary, and matching.

OCR runs the system ``tesseract`` command on image bytes held in memory. It is
optional: without the binary, search_media still searches voice and video-note
transcripts and reports images as skipped.
"""

import asyncio
import os
import shutil
from collections import OrderedDict
from typing import Any, Optional

OCR_TIMEOUT_SECONDS = 30.0
OCR_DEFAULT_LANGS = "eng+rus"
OCR_CACHE_SIZE = 512
SNIPPET_RADIUS = 80

_ocr_cache: "OrderedDict[tuple, str]" = OrderedDict()
_installed_langs: Optional[set] = None


def tesseract_path() -> Optional[str]:
    return shutil.which(os.getenv("TELEGRAM_OCR_TESSERACT", "tesseract"))


async def _installed_languages(binary: str) -> set:
    global _installed_langs
    if _installed_langs is None:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--list-langs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), OCR_TIMEOUT_SECONDS)
        lines = out.decode(errors="replace").splitlines()[1:]
        _installed_langs = {line.strip() for line in lines if line.strip()}
    return _installed_langs


async def ocr_languages(binary: str) -> str:
    """TELEGRAM_OCR_LANGS (default eng+rus), limited to installed language packs."""
    wanted = os.getenv("TELEGRAM_OCR_LANGS", OCR_DEFAULT_LANGS).split("+")
    installed = await _installed_languages(binary)
    usable = [lang for lang in wanted if lang in installed]
    return "+".join(usable) or "eng"


def ocr_status() -> str:
    binary = tesseract_path()
    if binary is None:
        return "unavailable: tesseract binary not found; images are skipped"
    return f"available: {binary}"


async def run_ocr(data: bytes) -> str:
    """Text tesseract reads from an image. Raises RuntimeError on failure."""
    binary = tesseract_path()
    if binary is None:
        raise RuntimeError("tesseract binary not found")
    langs = await ocr_languages(binary)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "stdin",
        "stdout",
        "-l",
        langs,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), OCR_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"tesseract timed out after {OCR_TIMEOUT_SECONDS:.0f}s")
    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip().splitlines()[-1:] or ["no detail"]
        raise RuntimeError(f"tesseract failed: {detail[0]}")
    return " ".join(out.decode(errors="replace").split())


def cached_ocr(chat_id: int, message_id: int) -> Optional[str]:
    key = (chat_id, message_id)
    if key in _ocr_cache:
        _ocr_cache.move_to_end(key)
        return _ocr_cache[key]
    return None


def remember_ocr(chat_id: int, message_id: int, text: str) -> None:
    _ocr_cache[(chat_id, message_id)] = text
    _ocr_cache.move_to_end((chat_id, message_id))
    while len(_ocr_cache) > OCR_CACHE_SIZE:
        _ocr_cache.popitem(last=False)


def is_image(msg: Any) -> bool:
    if getattr(msg, "photo", None) is not None and getattr(msg, "web_preview", None) is None:
        return True
    if getattr(msg, "sticker", None) is not None:
        return False
    mime = getattr(getattr(msg, "file", None), "mime_type", None) or ""
    return getattr(msg, "document", None) is not None and mime.startswith("image/")


def declared_size(msg: Any) -> Optional[int]:
    size = getattr(getattr(msg, "file", None), "size", None)
    return size if isinstance(size, int) and size >= 0 else None


def find_snippet(text: str, query: str) -> Optional[str]:
    """A window of text around the first case-insensitive match, or None."""
    position = text.lower().find(query.lower())
    if position < 0:
        return None
    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(text), position + len(query) + SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ")
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")
