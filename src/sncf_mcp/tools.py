import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from sncf_mcp import api, gtfsrt, realtime
from sncf_mcp.api import SncfApi, SncfApiError
from sncf_mcp.gtfsrt import PARIS
from sncf_mcp.models import Departure, Disruption, FeedWarning, Journey, Station

MAX_COUNT = 50
MAX_ALERTS = 20
MIN_TERM = 3
MAX_MESSAGE = 280
FEED_DOWN = "SNCF realtime feed unavailable: planned times only"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@dataclass(frozen=True)
class Services:
    api: SncfApi
    trips: gtfsrt.CachedFeed[gtfsrt.TripFeed]
    alerts: gtfsrt.CachedFeed[list[gtfsrt.Alert]]

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.trips.client.aclose()


@dataclass(frozen=True)
class Target:
    terms: list[str]
    station: str | None = None
    trains: frozenset[str] = frozenset()


def _count(value: int) -> int:
    return max(1, min(value, MAX_COUNT))


def _id(value: str) -> str:
    return quote(value, safe=":")


async def list_stations(s: Services, query: str) -> list[Station]:
    data = await s.api.get("/places", {"q": query, "type[]": "stop_area", "count": 8})
    return [{"id": p["id"], "name": p["name"]} for p in data.get("places", [])]


async def next_departures(
    s: Services, station_id: str, from_datetime: str | None, count: int
) -> list[Departure]:
    params: dict[str, Any] = {"count": _count(count), "data_freshness": "realtime"}
    if from_datetime:
        params["from_datetime"] = api.to_api(from_datetime)
    data, feed = await asyncio.gather(
        s.api.get(f"/stop_areas/{_id(station_id)}/departures", params), s.trips.get()
    )
    index = realtime.disruption_index(data)
    return [realtime.departure(d, feed, index) for d in data.get("departures", [])]


async def plan_journeys(
    s: Services, from_station: str, to_station: str, from_datetime: str | None, count: int
) -> list[Journey]:
    params: dict[str, Any] = {
        "from": from_station,
        "to": to_station,
        "count": _count(count),
        "data_freshness": "realtime",
    }
    if from_datetime:
        params["datetime"] = api.to_api(from_datetime)
    data, feed = await asyncio.gather(s.api.get("/journeys", params), s.trips.get())
    index = realtime.disruption_index(data)
    out = []
    for j in data.get("journeys", []):
        legs = [
            realtime.leg(sec, feed, index)
            for sec in j.get("sections", [])
            if sec.get("type") == "public_transport"
        ]
        item: Journey = {
            "departure": legs[0]["departure"] if legs else api.local(j.get("departure_date_time")),
            "arrival": legs[-1]["arrival"] if legs else api.local(j.get("arrival_date_time")),
            "duration_min": j.get("duration", 0) // 60,
            "transfers": j.get("nb_transfers", 0),
            "legs": legs,
        }
        if feed is None:
            item["realtime_unavailable"] = True
        out.append(item)
    return out


async def _resolve(s: Services, station_id: str | None, line_id: str | None) -> Target | None:
    if station_id:
        data = await s.api.get_or_none(f"/stop_areas/{_id(station_id)}/stop_points", {})
        if data is None:
            return None
        points = data.get("stop_points", [])
        terms = [p["stop_area"]["name"].casefold() for p in points[:1] if p.get("stop_area")]
        station = next((u for u in (realtime.uic(p) for p in points) if u), None)
        feed = await s.trips.get()
        trains = gtfsrt.station_trains(feed, station) if feed and station else set()
        return Target(terms, station, frozenset(trains))
    data = await s.api.get_or_none(f"/lines/{_id(str(line_id))}", {})
    if data is None:
        return None
    line = data.get("lines", [{}])[0]
    return Target([str(line[k]).casefold() for k in ("code", "name") if line.get(k)])


def _concerns(alert: gtfsrt.Alert, target: Target) -> bool:
    text = f"{alert.title} {alert.message}".casefold()
    return (
        bool(target.station and target.station in alert.stops)
        or any(t in target.trains for t in alert.trips)
        or any(re.search(rf"\b{re.escape(t)}\b", text) for t in target.terms if len(t) >= MIN_TERM)
    )


def _paris(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, PARIS).strftime(api.FMT) if epoch else None


def _format(alerts: list[gtfsrt.Alert]) -> list[Disruption]:
    seen: set[tuple[str, str]] = set()
    out: list[Disruption] = []
    for a in sorted(alerts, key=lambda a: a.begin or 0):
        if (a.title, a.message) in seen:
            continue
        seen.add((a.title, a.message))
        out.append(
            {
                "effect": a.effect,
                "title": a.title,
                "message": a.message[:MAX_MESSAGE] or None,
                "from": _paris(a.begin),
                "until": _paris(a.end),
            }
        )
    return out[:MAX_ALERTS]


async def _from_feed(
    s: Services, alerts: list[gtfsrt.Alert], station_id: str | None, line_id: str | None
) -> list[Disruption]:
    now = int(datetime.now(UTC).timestamp())
    live = [a for a in alerts if not a.end or a.end >= now]
    if station_id or line_id:
        target = await _resolve(s, station_id, line_id)
        if target is None:
            return []
        live = [a for a in live if _concerns(a, target)]
    return _format(live)


def _compact(d: dict[str, Any]) -> Disruption:
    periods = d.get("application_periods") or []
    return {
        "status": d.get("status"),
        "effect": d.get("severity", {}).get("effect"),
        "title": d.get("severity", {}).get("name"),
        "message": next((m["text"] for m in d.get("messages", []) if m.get("text")), None),
        "from": api.utc_to_paris(periods[0].get("begin")) if periods else None,
        "until": api.utc_to_paris(periods[-1].get("end")) if periods else None,
    }


async def _from_api(s: Services, station_id: str | None, line_id: str | None) -> list[Disruption]:
    if station_id:
        path = f"/stop_areas/{_id(station_id)}/disruptions"
    elif line_id:
        path = f"/lines/{_id(line_id)}/disruptions"
    else:
        path = "/disruptions"
    data = await s.api.get_or_none(path, {"count": MAX_ALERTS})
    return [_compact(d) for d in data.get("disruptions", [])] if data else []


async def current_disruptions(
    s: Services, station_id: str | None, line_id: str | None
) -> list[Disruption] | list[FeedWarning]:
    alerts = await s.alerts.get()
    try:
        found = await _from_feed(s, alerts, station_id, line_id) if alerts else []
        if found:
            return found
        fallback = await _from_api(s, station_id, line_id)
    except SncfApiError:
        if alerts is None:
            raise
        return []
    if fallback or alerts is not None:
        return fallback
    warning: list[FeedWarning] = [{"warning": FEED_DOWN}]
    return warning


def register(mcp: FastMCP, s: Services) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def find_station(query: str) -> list[Station]:
        """Find train stations by name. Returns station ids to use in the other tools."""
        return await list_stations(s, query)

    @mcp.tool(annotations=READ_ONLY)
    async def departures(
        station_id: str, from_datetime: str | None = None, count: int = 10
    ) -> list[Departure]:
        """Next departures from a station (id from find_station). Delay, cancellation and
        skipped stop come from the SNCF GTFS-RT realtime feed (transport.data.gouv.fr, open
        licence); a train absent from it runs as planned (realtime false). Platform only when
        the SNCF API provides it. realtime_unavailable is set when the feed is down (planned
        times only). from_datetime is ISO 8601, Paris time if no offset; defaults to now."""
        return await next_departures(s, station_id, from_datetime, count)

    @mcp.tool(annotations=READ_ONLY)
    async def journeys(
        from_station: str, to_station: str, from_datetime: str | None = None, count: int = 3
    ) -> list[Journey]:
        """Journeys between two stations (ids from find_station), departing after
        `from_datetime` (ISO 8601, Paris time if no offset; defaults to now). Leg delays,
        cancellations and skipped stops come from the SNCF GTFS-RT realtime feed
        (transport.data.gouv.fr, open licence); realtime_unavailable is set when it is down."""
        return await plan_journeys(s, from_station, to_station, from_datetime, count)

    @mcp.tool(annotations=READ_ONLY)
    async def disruptions(
        station_id: str | None = None, line_id: str | None = None
    ) -> list[Disruption] | list[FeedWarning]:
        """Current and upcoming disruptions for a station (id from find_station) or a
        line id (station_id wins when both are given); without arguments, network-wide. Built
        from the SNCF GTFS-RT service alerts feed (transport.data.gouv.fr, open licence), with
        the SNCF API as fallback when the feed has nothing. Empty list when there are none; if
        the feed is down and the API has nothing, a single item with a warning."""
        return await current_disruptions(s, station_id, line_id)
