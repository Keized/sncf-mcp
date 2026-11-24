import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from sncf_mcp import tools
from sncf_mcp.api import SncfApi
from sncf_mcp.gtfsrt import service_alerts, trip_updates
from sncf_mcp.models import Departure, Journey, Station
from sncf_mcp.tools import Services

Handler = Callable[[httpx.Request], httpx.Response]
PAST, FUTURE = 1_700_000_000, 4_102_444_800


def varint(n: int) -> bytes:
    out = b""
    while n > 0x7F:
        out += bytes([n & 0x7F | 0x80])
        n >>= 7
    return out + bytes([n])


def fld(num: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return varint(num << 3) + varint(value if value >= 0 else value + (1 << 64))
    data = value.encode() if isinstance(value, str) else value
    return varint(num << 3 | 2) + varint(len(data)) + data


def stop_update(
    stop: str, delay: int | None = None, time: int | None = None, skipped: bool = False
) -> bytes:
    event = (fld(1, delay) if delay is not None else b"") + (fld(2, time) if time else b"")
    return fld(2, fld(3, event) + fld(4, stop) + (fld(5, 1) if skipped else b""))


def trip_entity(trip_id: str, date: str, stops: list[bytes], canceled: bool = False) -> bytes:
    trip = fld(1, trip_id) + fld(3, date) + (fld(4, 3) if canceled else b"")
    return fld(2, fld(1, trip_id) + fld(3, fld(1, trip) + b"".join(stops)))


def alert_entity(
    title: str, stop: str | None = None, trip_id: str | None = None, end: int = FUTURE
) -> bytes:
    def text(value: str) -> bytes:
        return fld(1, fld(1, value) + fld(2, "fr"))

    selector = (fld(5, stop) if stop else b"") + (fld(4, fld(1, trip_id)) if trip_id else b"")
    alert = fld(1, fld(1, PAST) + fld(2, end)) + fld(5, selector) + fld(7, 3) + fld(10, text(title))
    return fld(2, fld(1, "a") + fld(5, alert + fld(11, text("<p>Details  here</p>"))))


def reply(body: dict[str, Any], status: int = 200) -> Handler:
    return lambda _: httpx.Response(status, content=json.dumps(body))


def routes(table: dict[str, dict[str, Any]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, body in table.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, content=json.dumps(body))
        return httpx.Response(404, content=json.dumps({"error": {"message": "none"}}))

    return handler


@dataclass
class Env:
    services: Services
    api_seen: list[httpx.Request]
    feed_seen: list[str]
    feeds: dict[str, bytes | None] = field(default_factory=dict)

    async def find_station(self, query: str) -> list[Station]:
        return await tools.list_stations(self.services, query)

    async def departures(
        self, station_id: str, from_datetime: str | None = None, count: int = 10
    ) -> list[Departure]:
        return await tools.next_departures(self.services, station_id, from_datetime, count)

    async def journeys(
        self, from_station: str, to_station: str, from_datetime: str | None = None, count: int = 3
    ) -> list[Journey]:
        return await tools.plan_journeys(
            self.services, from_station, to_station, from_datetime, count
        )

    async def disruptions(
        self, station_id: str | None = None, line_id: str | None = None
    ) -> list[Any]:
        return await tools.current_disruptions(self.services, station_id, line_id)


def env(
    api: Handler | dict[str, dict[str, Any]] | None = None,
    trips: bytes | None = b"",
    alerts: bytes | None = b"",
) -> Env:
    api_seen: list[httpx.Request] = []
    feed_seen: list[str] = []
    bodies: dict[str, bytes | None] = {"trip-updates": trips, "service-alerts": alerts}

    def feed_handler(request: httpx.Request) -> httpx.Response:
        feed_seen.append(request.url.path)
        body = bodies[request.url.path.rsplit("-gtfs-rt-", 1)[-1]]
        return httpx.Response(200 if body is not None else 503, content=body or b"")

    def record(request: httpx.Request) -> httpx.Response:
        api_seen.append(request)
        return handler(request)

    handler = routes(api) if isinstance(api, dict) else api or routes({})
    client = (
        None
        if api is None
        else httpx.AsyncClient(
            base_url="https://sncf-api.test/v1", transport=httpx.MockTransport(record)
        )
    )
    feeds = httpx.AsyncClient(transport=httpx.MockTransport(feed_handler))
    services = Services(SncfApi(client), trip_updates(feeds), service_alerts(feeds))
    return Env(services, api_seen, feed_seen, bodies)
