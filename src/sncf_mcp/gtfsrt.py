import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
SERVICE_ALERTS_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"
CACHE_SECONDS = 30
TIMEOUT = 5.0
TOLERANCE = 120
MAX_GAP = 12 * 3600
UNKNOWN = float("inf")
MAX_STALE = 300
PARIS = ZoneInfo("Europe/Paris")
SKIPPED = 1
TRIP_CANCELED = 3
EFFECTS = {
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    9: "STOP_MOVED",
}

Field = tuple[int, int | bytes]


@dataclass(frozen=True)
class StopUpdate:
    stop: str
    skipped: bool
    dep_delay: int | None
    dep_time: int | None
    arr_delay: int | None
    arr_time: int | None


@dataclass(frozen=True)
class Trip:
    cancelled: bool
    stops: tuple[StopUpdate, ...]


@dataclass(frozen=True)
class Alert:
    effect: str | None
    title: str
    message: str
    begin: int | None
    end: int | None
    stops: tuple[str, ...]
    trips: tuple[str, ...]


TripFeed = dict[tuple[str, str], list[Trip]]


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated protobuf")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _fields(buf: bytes) -> list[Field]:
    out: list[Field] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        wire = key & 7
        value: int | bytes
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = buf[i : i + size], i + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, i = buf[i : i + size], i + size
        else:
            raise ValueError("unsupported protobuf wire type")
        out.append((key >> 3, value))
    return out


def _msgs(fields: list[Field], number: int) -> list[list[Field]]:
    return [_fields(v) for n, v in fields if n == number and isinstance(v, bytes)]


def _int(fields: list[Field], number: int) -> int | None:
    for n, v in fields:
        if n == number and isinstance(v, int):
            return v - (1 << 64) if v >= 1 << 63 else v
    return None


def _str(fields: list[Field], number: int) -> str:
    for n, v in fields:
        if n == number and isinstance(v, bytes):
            return v.decode("utf-8", "replace")
    return ""


def train_number(trip_id: str) -> str | None:
    match = re.match(r"OCE[A-Z]{2}(\d+)", trip_id)
    return match.group(1) if match else None


def uic(stop_id: str) -> str:
    return stop_id.rsplit("-", 1)[-1]


def _event(fields: list[Field], number: int) -> tuple[int | None, int | None]:
    events = _msgs(fields, number)
    return (_int(events[0], 1), _int(events[0], 2)) if events else (None, None)


def parse_trip_updates(data: bytes) -> TripFeed:
    feed: TripFeed = {}
    for entity in _msgs(_fields(data), 2):
        for update in _msgs(entity, 3):
            trips = _msgs(update, 1)
            number = train_number(_str(trips[0], 1)) if trips else None
            if not trips or not number:
                continue
            stops = []
            for stu in _msgs(update, 2):
                dep_delay, dep_time = _event(stu, 3)
                arr_delay, arr_time = _event(stu, 2)
                stops.append(
                    StopUpdate(
                        _str(stu, 4),
                        _int(stu, 5) == SKIPPED,
                        dep_delay,
                        dep_time,
                        arr_delay,
                        arr_time,
                    )
                )
            cancelled = _int(trips[0], 4) == TRIP_CANCELED
            feed.setdefault((number, _str(trips[0], 3)), []).append(Trip(cancelled, tuple(stops)))
    return feed


def _translated(fields: list[Field], number: int) -> str:
    texts: dict[str, str] = {}
    for container in _msgs(fields, number):
        for translation in _msgs(container, 1):
            texts[_str(translation, 2)] = _str(translation, 1)
    return texts.get("fr") or texts.get("en") or next(iter(texts.values()), "")


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_alerts(data: bytes) -> list[Alert]:
    alerts = []
    for entity in _msgs(_fields(data), 2):
        for alert in _msgs(entity, 5):
            periods = _msgs(alert, 1)
            stops, trips = [], []
            for selector in _msgs(alert, 5):
                if stop := _str(selector, 5):
                    stops.append(uic(stop))
                for trip in _msgs(selector, 4):
                    if number := train_number(_str(trip, 1)):
                        trips.append(number)
            alerts.append(
                Alert(
                    EFFECTS.get(_int(alert, 7) or 0),
                    _plain(_translated(alert, 10)),
                    _plain(_translated(alert, 11)),
                    _int(periods[0], 1) if periods else None,
                    _int(periods[0], 2) if periods else None,
                    tuple(stops),
                    tuple(trips),
                )
            )
    return alerts


def _event_of(s: StopUpdate, kind: str) -> tuple[int | None, int | None]:
    return (s.dep_delay, s.dep_time) if kind == "dep" else (s.arr_delay, s.arr_time)


def _match(
    trip: Trip, station: str | None, scheduled: int, kind: str
) -> tuple[StopUpdate | None, float]:
    best, gap = None, UNKNOWN
    for s in trip.stops:
        if station and uic(s.stop) != station:
            continue
        delay, at = _event_of(s, kind)
        distance = abs(at - (delay or 0) - scheduled) if at is not None else UNKNOWN
        if distance > (MAX_GAP if station else TOLERANCE) and (not station or distance < UNKNOWN):
            continue
        if best is None or distance < gap:
            best, gap = s, distance
    return best, gap


def epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=PARIS).timestamp())


def stop_delay(stop: StopUpdate, scheduled: int, kind: str = "dep") -> int | None:
    for k in (kind, "arr" if kind == "dep" else "dep"):
        delay, at = _event_of(stop, k)
        if delay is not None:
            return delay
        if k == kind and at is not None:
            return at - scheduled
    return None


def locate(
    feed: TripFeed, number: str, planned: datetime, station: str | None, kind: str = "dep"
) -> tuple[Trip, StopUpdate | None] | None:
    scheduled = epoch(planned)
    trips = [
        trip
        for days in (0, 1)
        for trip in feed.get((number, (planned - timedelta(days=days)).strftime("%Y%m%d")), [])
    ]
    matches = [(trip, *_match(trip, station, scheduled, kind)) for trip in trips]
    found = [m for m in matches if m[1] is not None]
    if found:
        trip, stop, _ = min(found, key=lambda m: m[2])
        return trip, stop
    same_day = feed.get((number, planned.strftime("%Y%m%d")), [])
    return next(((t, None) for t in same_day if t.cancelled), None)


def station_trains(feed: TripFeed, station: str) -> set[str]:
    return {
        n
        for (n, _), trips in feed.items()
        if any(uic(s.stop) == station for t in trips for s in t.stops)
    }


class CachedFeed[T]:
    def __init__(self, url: str, parse: Callable[[bytes], T], client: httpx.AsyncClient) -> None:
        self.url = url
        self.client = client
        self._parse = parse
        self._lock = asyncio.Lock()
        self._value: T | None = None
        self._fetched: float | None = None
        self._valid_at: float | None = None

    def _fresh(self) -> bool:
        return self._fetched is not None and time.monotonic() - self._fetched < CACHE_SECONDS

    async def get(self) -> T | None:
        if self._fresh():
            return self._value
        async with self._lock:
            if self._fresh():
                return self._value
            try:
                resp = await self.client.get(self.url)
                resp.raise_for_status()
                self._value = await asyncio.to_thread(self._parse, resp.content)
                self._valid_at = time.monotonic()
            except (httpx.HTTPError, ValueError):
                now = time.monotonic()
                if self._valid_at is None or now - self._valid_at >= MAX_STALE:
                    self._value, self._valid_at = None, None
            self._fetched = time.monotonic()
            return self._value


def trip_updates(client: httpx.AsyncClient) -> CachedFeed[TripFeed]:
    return CachedFeed(TRIP_UPDATES_URL, parse_trip_updates, client)


def service_alerts(client: httpx.AsyncClient) -> CachedFeed[list[Alert]]:
    return CachedFeed(SERVICE_ALERTS_URL, parse_alerts, client)
