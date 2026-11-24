"""Tests for the optional MCP_AUTH_TOKEN bearer check on HTTP/SSE."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from telegram_mcp import runner
from telegram_mcp.transport.auth import BearerAuthMiddleware, wrap_with_auth

TOKEN = "test-token-0123456789abcdef0123456789"


def _inner_app():
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST", "OPTIONS"])])


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _client(**kwargs):
    return TestClient(BearerAuthMiddleware(_inner_app(), TOKEN, **kwargs))


def test_wrap_with_auth_is_a_no_op_without_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    app = _inner_app()

    assert wrap_with_auth(app) is app


def test_wrap_with_auth_ignores_blank_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")
    app = _inner_app()

    assert wrap_with_auth(app) is app


def test_wrap_with_auth_reads_lockout_settings(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_AUTH_MAX_FAILURES", "3")
    monkeypatch.setenv("MCP_AUTH_LOCKOUT_SECONDS", "5")

    wrapped = wrap_with_auth(_inner_app())

    assert isinstance(wrapped, BearerAuthMiddleware)
    assert wrapped.max_failures == 3
    assert wrapped.lockout_seconds == 5
    assert wrap_with_auth(wrapped) is wrapped


def test_wrap_with_auth_rejects_bad_lockout_setting(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_AUTH_MAX_FAILURES", "lots")

    with pytest.raises(SystemExit, match="MCP_AUTH_MAX_FAILURES"):
        wrap_with_auth(_inner_app())


def test_request_with_valid_token_passes():
    res = _client().get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})

    assert res.status_code == 200
    assert res.text == "ok"


def test_scheme_is_case_insensitive():
    res = _client().get("/mcp", headers={"Authorization": f"bearer {TOKEN}"})

    assert res.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
)
def test_missing_or_wrong_token_is_rejected(headers):
    res = _client().post("/mcp", headers=headers)

    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in res.text


def test_cors_preflight_is_not_authenticated():
    res = _client().options("/mcp")

    assert res.status_code != 401


def test_repeated_failures_lock_the_client_out():
    clock = _Clock()
    client = _client(max_failures=3, lockout_seconds=30, clock=clock)

    for _ in range(3):
        assert client.get("/mcp").status_code == 401
    locked = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})

    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 30

    clock.now += 31
    res = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200


def test_failures_outside_the_window_do_not_add_up():
    clock = _Clock()
    client = _client(max_failures=3, lockout_seconds=10, clock=clock)

    for _ in range(5):
        assert client.get("/mcp").status_code == 401
        clock.now += 6

    assert client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_zero_max_failures_disables_lockout():
    client = _client(max_failures=0)

    for _ in range(20):
        assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_tracked_clients_are_bounded(monkeypatch):
    from telegram_mcp.transport import auth

    monkeypatch.setattr(auth, "MAX_TRACKED_CLIENTS", 4)
    middleware = BearerAuthMiddleware(_inner_app(), TOKEN, max_failures=5)
    for i in range(10):
        middleware._record_failure(f"10.0.0.{i}", 1.0)

    assert len(middleware._failures) == 4


def test_websocket_is_refused_when_auth_is_on():
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    middleware = BearerAuthMiddleware(_inner_app(), TOKEN)
    asyncio.run(middleware({"type": "websocket", "headers": []}, receive, send))

    assert sent == [{"type": "websocket.close", "code": 1008}]


class _FakeSettings:
    def __init__(self):
        self.host = None
        self.port = None
        self.transport_security = None
        self.log_level = "INFO"


class _FakeMcp:
    def __init__(self):
        self.settings = _FakeSettings()
        self.ran = None

    async def run_streamable_http_async(self):
        self.ran = "http"

    async def run_sse_async(self):
        self.ran = "sse"

    def streamable_http_app(self):
        return _inner_app()

    def sse_app(self):
        return _inner_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_serve_wraps_app_when_token_is_set(monkeypatch, transport):
    fake = _FakeMcp()
    served = {}

    class FakeServer:
        def __init__(self, config):
            served["app"] = config.app
            served["host"] = config.host

        async def serve(self):
            served["ran"] = True

    import uvicorn

    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MCP_HOST", raising=False)

    await runner._serve(transport)

    assert served["ran"] is True
    assert served["host"] == "127.0.0.1"
    assert isinstance(served["app"], BearerAuthMiddleware)
    assert fake.ran is None


@pytest.mark.asyncio
async def test_serve_without_token_keeps_default_runner(monkeypatch):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

    await runner._serve("http")

    assert fake.ran == "http"


@pytest.mark.asyncio
async def test_serve_warns_on_public_bind_without_token(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")

    await runner._serve("http")

    assert "MCP_AUTH_TOKEN is unset" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_serve_is_quiet_on_loopback(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)

    await runner._serve("http")

    assert "MCP_AUTH_TOKEN" not in capsys.readouterr().err
