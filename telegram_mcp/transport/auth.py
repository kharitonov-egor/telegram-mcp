"""Optional bearer-token auth for the HTTP and SSE transports.

Off unless MCP_AUTH_TOKEN is set. When set, every HTTP request except CORS
preflight must carry ``Authorization: Bearer <token>`` and websocket upgrades
are refused. Repeated failures from
one client address lock that address out for a while (MCP_AUTH_MAX_FAILURES
within MCP_AUTH_LOCKOUT_SECONDS; 0 disables the lockout). Behind a reverse
proxy every client shares the proxy's address, so the lockout then applies to
all of them at once.
"""

from __future__ import annotations

import hmac
import json
import os
import time
from collections import OrderedDict, deque
from typing import Callable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_MAX_FAILURES = 10
DEFAULT_LOCKOUT_SECONDS = 60.0
MAX_TRACKED_CLIENTS = 1024


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"Invalid {name} '{raw}'. Expected a number.")
    if value < 0:
        raise SystemExit(f"Invalid {name} '{raw}'. Expected a number >= 0.")
    return value


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: str,
        max_failures: int = DEFAULT_MAX_FAILURES,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.app = app
        self._token = token.encode()
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._failures: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._locked_until: dict[str, float] = {}

    def _client(self, scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    def _presented_token(self, scope: Scope) -> Optional[bytes]:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, credential = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    return credential.strip()
        return None

    def _is_locked(self, client: str, now: float) -> float:
        until = self._locked_until.get(client, 0.0)
        if until <= now:
            self._locked_until.pop(client, None)
            return 0.0
        return until - now

    def _record_failure(self, client: str, now: float) -> None:
        if self.max_failures <= 0:
            return
        window = self._failures.setdefault(client, deque())
        self._failures.move_to_end(client)
        window.append(now)
        while window and window[0] <= now - self.lockout_seconds:
            window.popleft()
        if len(window) >= self.max_failures:
            self._locked_until[client] = now + self.lockout_seconds
            window.clear()
        while len(self._failures) > MAX_TRACKED_CLIENTS:
            stale, _ = self._failures.popitem(last=False)
            self._locked_until.pop(stale, None)

    async def _reject(
        self, send: Send, status: int, message: str, headers: list[tuple[bytes, bytes]]
    ) -> None:
        body = json.dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *headers,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (
            scope["type"] == "http" and scope.get("method") == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        client = self._client(scope)
        now = self._clock()
        remaining = self._is_locked(client, now)
        if remaining:
            retry = str(int(remaining) + 1).encode()
            await self._reject(
                send, 429, "too many failed auth attempts", [(b"retry-after", retry)]
            )
            return
        presented = self._presented_token(scope)
        if presented is not None and hmac.compare_digest(presented, self._token):
            await self.app(scope, receive, send)
            return
        self._record_failure(client, now)
        await self._reject(
            send, 401, "missing or invalid bearer token", [(b"www-authenticate", b"Bearer")]
        )


def auth_token() -> str:
    return os.getenv("MCP_AUTH_TOKEN", "").strip()


def wrap_with_auth(app: ASGIApp) -> ASGIApp:
    """Wrap app in BearerAuthMiddleware when MCP_AUTH_TOKEN is set; else return it."""
    if isinstance(app, BearerAuthMiddleware):
        return app
    token = auth_token()
    if not token:
        return app
    return BearerAuthMiddleware(
        app,
        token,
        max_failures=int(_env_number("MCP_AUTH_MAX_FAILURES", DEFAULT_MAX_FAILURES)),
        lockout_seconds=_env_number("MCP_AUTH_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS),
    )
