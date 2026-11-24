import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from sncf_mcp import gtfsrt, tools
from sncf_mcp.api import DEFAULT_URL, SncfApi
from sncf_mcp.tools import Services

log = logging.getLogger("sncf_mcp")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8830
    sncf_token: str | None = None
    sncf_url: str = DEFAULT_URL
    auth_token: str | None = None

    @classmethod
    def from_env(cls) -> Self:
        env = os.environ
        return cls(
            host=env.get("HOST", cls.host),
            port=int(env.get("PORT", cls.port)),
            sncf_token=env.get("SNCF_API_TOKEN") or None,
            sncf_url=env.get("SNCF_API_URL") or DEFAULT_URL,
            auth_token=env.get("MCP_AUTH_TOKEN") or None,
        )


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                response = Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def open_services(settings: Settings) -> Services:
    feeds = httpx.AsyncClient(timeout=httpx.Timeout(gtfsrt.TIMEOUT))
    return Services(
        SncfApi.create(settings.sncf_url, settings.sncf_token),
        gtfsrt.trip_updates(feeds),
        gtfsrt.service_alerts(feeds),
    )


def create_server(settings: Settings, services: Services) -> FastMCP:
    mcp = FastMCP("sncf-mcp", host=settings.host, port=settings.port)
    tools.register(mcp, services)
    return mcp


def build_app(settings: Settings, services: Services) -> Starlette:
    app: Starlette = create_server(settings, services).streamable_http_app()
    serve = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with serve(app):
                yield
        finally:
            await services.aclose()

    app.router.lifespan_context = lifespan
    if settings.auth_token:
        app.add_middleware(BearerAuth, token=settings.auth_token)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.sncf_token:
        log.warning("SNCF_API_TOKEN is not set: tools will return an error")
    config = uvicorn.Config(
        build_app(settings, open_services(settings)),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    uvicorn.Server(config).run()
