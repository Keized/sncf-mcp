import httpx
import pytest
from helpers import env

from sncf_mcp import app
from sncf_mcp.app import Settings

pytestmark = pytest.mark.anyio

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOST", "PORT", "SNCF_API_TOKEN", "SNCF_API_URL", "MCP_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env() == Settings()
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("SNCF_API_TOKEN", "t")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    settings = Settings.from_env()
    assert (settings.host, settings.port, settings.sncf_token) == ("0.0.0.0", 9000, "t")
    assert settings.auth_token is None


def test_host_setting_applies_at_construction() -> None:
    services = env().services
    assert app.create_server(Settings(host="0.0.0.0"), services).settings.transport_security is None
    assert app.create_server(Settings(), services).settings.transport_security is not None


async def post_init(settings: Settings, headers: dict[str, str]) -> httpx.Response:
    asgi = app.build_app(settings, env().services)
    async with (
        asgi.router.lifespan_context(asgi),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url="http://127.0.0.1:8830"
        ) as client,
    ):
        return await client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **headers})


async def test_auth_open_when_unset() -> None:
    assert (await post_init(Settings(), {})).status_code == 200


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "x"}])
async def test_auth_rejects(header: dict[str, str]) -> None:
    response = await post_init(Settings(auth_token="secret"), header)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "secret" not in response.text


async def test_auth_accepts_token() -> None:
    response = await post_init(Settings(auth_token="secret"), {"Authorization": "Bearer secret"})
    assert response.status_code == 200


async def test_lifespan_closes_clients() -> None:
    e = env({})
    asgi = app.build_app(Settings(), e.services)
    async with asgi.router.lifespan_context(asgi):
        assert not e.services.trips.client.is_closed
    assert e.services.trips.client.is_closed
    assert e.services.api.client is not None
    assert e.services.api.client.is_closed
