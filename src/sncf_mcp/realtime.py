from dataclasses import dataclass
from typing import Any

from sncf_mcp import api, gtfsrt
from sncf_mcp.models import Departure, Leg

Index = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Live:
    delay_min: int | None
    skipped: bool
    cancelled: bool


def disruption_index(data: dict[str, Any]) -> Index:
    return {d["id"]: d for d in data.get("disruptions", [])}


def uic(point: dict[str, Any] | None) -> str | None:
    codes = (point or {}).get("codes", [])
    return next((c["value"] for c in codes if c.get("type") == "UIC"), None)


def train(info: dict[str, Any]) -> str:
    number = info.get("trip_short_name") or info.get("headsign") or info.get("code") or ""
    return f"{info.get('commercial_mode', '')} {number}".strip()


def _effects(item: dict[str, Any], index: Index) -> list[str]:
    ids = [ln.get("id") for ln in item.get("links", []) if ln.get("type") == "disruption"]
    found = [index.get(i) for i in ids if i]
    return [d["severity"]["effect"] for d in found if d and "effect" in d.get("severity", {})]


def _live(
    feed: gtfsrt.TripFeed | None,
    info: dict[str, Any],
    point: dict[str, Any] | None,
    planned: str | None,
    kind: str = "dep",
) -> Live | None:
    start, number = api.parse(planned), str(info.get("trip_short_name") or "")
    if feed is None or start is None or not number.isdigit():
        return None
    found = gtfsrt.locate(feed, number, start, uic(point), kind)
    if found is None:
        return None
    trip, stop = found
    delay = gtfsrt.stop_delay(stop, gtfsrt.epoch(start), kind) if stop else None
    return Live(
        None if delay is None else api.minutes(delay), bool(stop and stop.skipped), trip.cancelled
    )


def departure(dep: dict[str, Any], feed: gtfsrt.TripFeed | None, index: Index) -> Departure:
    info, sdt = dep["display_informations"], dep["stop_date_time"]
    planned = sdt.get("base_departure_date_time") or sdt.get("departure_date_time")
    live = _live(feed, info, dep.get("stop_point"), planned)
    item: Departure = {
        "time": api.local(sdt.get("departure_date_time")),
        "scheduled": api.local(planned),
        "delay_min": api.delay_min(planned, sdt.get("departure_date_time")),
        "cancelled": "NO_SERVICE" in _effects(dep, index),
        "skipped": False,
        "platform": dep.get("stop_point", {}).get("platform_code"),
        "train": train(info),
        "direction": info.get("direction"),
        "realtime": live is not None or sdt.get("data_freshness") == "realtime",
    }
    if live:
        if live.delay_min is not None:
            item["time"] = api.shift(planned, live.delay_min)
            item["delay_min"] = live.delay_min
        item["cancelled"] = item["cancelled"] or live.cancelled
        item["skipped"] = live.skipped
    if feed is None:
        item["realtime_unavailable"] = True
    return item


def leg(s: dict[str, Any], feed: gtfsrt.TripFeed | None, index: Index) -> Leg:
    info = s["display_informations"]
    base_dep = s.get("base_departure_date_time") or s.get("departure_date_time")
    base_arr = s.get("base_arrival_date_time") or s.get("arrival_date_time")
    start = _live(feed, info, s["from"].get("stop_point"), base_dep)
    end = _live(feed, info, s["to"].get("stop_point"), base_arr, "arr")
    item: Leg = {
        "train": train(info),
        "from": s["from"]["name"],
        "to": s["to"]["name"],
        "departure": api.local(s.get("departure_date_time")),
        "arrival": api.local(s.get("arrival_date_time")),
        "delay_min": api.delay_min(base_dep, s.get("departure_date_time")),
        "cancelled": "NO_SERVICE" in _effects(s, index),
        "skipped": False,
        "realtime": start is not None,
    }
    if start:
        if start.delay_min is not None:
            item["departure"] = api.shift(base_dep, start.delay_min)
            item["delay_min"] = start.delay_min
        item["cancelled"] = item["cancelled"] or start.cancelled
        item["skipped"] = start.skipped or bool(end and end.skipped)
    if end and end.delay_min is not None:
        item["arrival"] = api.shift(base_arr, end.delay_min)
    return item
