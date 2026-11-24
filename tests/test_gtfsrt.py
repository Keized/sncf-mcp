import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
import pytest
from helpers import (
    PAST,
    alert_entity,
    env,
    stop_update,
    trip_entity,
)

from sncf_mcp import api, gtfsrt, tools

pytestmark = pytest.mark.anyio

THIONVILLE = "stop_area:SNC:thionville"


def departure(number: str, base: str, uic: str | None = "87191007") -> dict[str, Any]:
    codes = [{"type": "UIC", "value": uic}] if uic else [{"type": "source", "value": "x"}]
    return {
        "display_informations": {
            "commercial_mode": "TER",
            "trip_short_name": number,
            "direction": "Metz",
        },
        "stop_date_time": {"base_departure_date_time": base, "departure_date_time": base},
        "stop_point": {"codes": codes},
        "links": [],
    }


TRIPS = (
    trip_entity(
        "OCESN88726F1187_F:TER:x", "20261124", [stop_update("StopPoint:OCETrain TER-87191007", 300)]
    )
    + trip_entity(
        "OCESN2807F1187_F:TGV:x",
        "20261124",
        [stop_update("StopPoint:OCETGV INOUI-87191007", None, None, skipped=True)],
    )
    + trip_entity("OCESN9999F1187_F:TER:x", "20261124", [], canceled=True)
)


async def test_departures_delay_skipped_and_planned() -> None:
    e = env(
        {
            "/departures": {
                "departures": [
                    departure("88726", "20261124T100000"),
                    departure("2807", "20261124T101000"),
                    departure("9999", "20261124T102000"),
                    departure("1234", "20261124T103000"),
                ]
            }
        },
        TRIPS,
    )
    late, skipped, cancelled, planned = await e.departures(THIONVILLE)
    assert (late["delay_min"], late["time"], late["realtime"]) == (5, "2026-11-24 10:05", True)
    assert skipped["skipped"] and skipped["realtime"]
    assert cancelled["cancelled"]
    assert (planned["realtime"], planned["delay_min"], planned["skipped"]) == (False, 0, False)
    assert "realtime_unavailable" not in planned


async def test_station_without_uic_matches_on_scheduled_time() -> None:
    base = "20261124T100000"
    epoch = int(datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=gtfsrt.PARIS).timestamp())
    stop = stop_update("StopPoint:OCETrain TER-82001000", 600, epoch + 600)
    e = env(
        {"/departures": {"departures": [departure("88522", base, uic=None)]}},
        trip_entity("OCESN88522F1187_F:TER:x", "20261124", [stop]),
        b"",
    )
    [dep] = await e.departures("stop_area:lux")
    assert dep["delay_min"] == 10
    assert dep["time"] == "2026-11-24 10:10"


async def test_feed_down_degrades_to_planned() -> None:
    e = env({"/departures": {"departures": [departure("88726", "20261124T100000")]}}, None, None)
    [dep] = await e.departures(THIONVILLE)
    assert dep["realtime_unavailable"] is True
    assert (dep["delay_min"], dep["realtime"]) == (0, False)


async def test_feed_is_cached() -> None:
    e = env({"/departures": {"departures": []}}, TRIPS, b"")
    await e.departures(THIONVILLE)
    await e.departures(THIONVILLE)
    assert e.feed_seen == ["/resource/sncf-gtfs-rt-trip-updates"]


async def test_journey_leg_realtime() -> None:
    point = {"codes": [{"type": "UIC", "value": "87191007"}]}
    section = {
        "type": "public_transport",
        "display_informations": {"commercial_mode": "TER", "trip_short_name": "88726"},
        "from": {"name": "Thionville", "stop_point": point},
        "to": {"name": "Metz", "stop_point": {"codes": []}},
        "departure_date_time": "20261124T100000",
        "arrival_date_time": "20261124T104500",
        "links": [],
    }
    journey = {"duration": 2700, "nb_transfers": 0, "sections": [section]}
    e = env({"/journeys": {"journeys": [journey]}}, TRIPS)
    [result] = await e.journeys("a", "b")
    assert result["legs"][0]["delay_min"] == 5
    assert result["departure"] == "2026-11-24 10:05"
    assert result["legs"][0]["realtime"]


async def test_disruptions_from_alerts_filtered_by_station() -> None:
    alerts = (
        alert_entity("Incident gare", stop="StopPoint:OCETrain TER-87191007")
        + alert_entity("Retard train", trip_id="OCESN88726F")
        + alert_entity("Travaux à Thionville centre")
        + alert_entity("Ailleurs", stop="StopPoint:OCETrain TER-87000000")
        + alert_entity("Ancien", stop="StopPoint:OCETrain TER-87191007", end=PAST + 1)
    )
    e = env(
        {
            "/stop_points": {
                "stop_points": [
                    {
                        "codes": [{"type": "UIC", "value": "87191007"}],
                        "stop_area": {"name": "Thionville"},
                    }
                ]
            }
        },
        TRIPS,
        alerts,
    )
    result = await e.disruptions(station_id=THIONVILLE)
    assert [d["title"] for d in result] == [
        "Incident gare",
        "Retard train",
        "Travaux à Thionville centre",
    ]
    assert result[0]["effect"] == "SIGNIFICANT_DELAYS"
    assert result[0]["message"] == "Details here"
    assert len(await e.disruptions()) == 4


async def test_disruptions_fallback_and_warning() -> None:
    e = env({"/disruptions": {"disruptions": [{"severity": {"name": "API"}}]}}, TRIPS, b"")
    assert (await e.disruptions())[0]["title"] == "API"
    e = env({}, None, None)
    assert "unavailable" in (await e.disruptions())[0]["warning"]


def epoch(base: str) -> int:
    return int(datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=gtfsrt.PARIS).timestamp())


async def test_night_train_found_under_previous_day() -> None:
    stop = stop_update("StopPoint:OCETrain TER-87191007", 2400)
    e = env(
        {"/departures": {"departures": [departure("88726", "20261124T081000")]}},
        trip_entity("OCESN88726F1187_F:TER:x", "20261123", [stop]),
        b"",
    )
    [dep] = await e.departures(THIONVILLE)
    assert (dep["delay_min"], dep["realtime"]) == (40, True)


async def test_trip_without_stop_keeps_api_delay() -> None:
    other = stop_update("StopPoint:OCETrain TER-87000000", 60)
    item = departure("88726", "20261124T100000")
    item["stop_date_time"]["departure_date_time"] = "20261124T101500"
    e = env(
        {"/departures": {"departures": [item]}},
        trip_entity("OCESN88726F1187_F:TER:x", "20261124", [other]),
    )
    [dep] = await e.departures(THIONVILLE)
    assert (dep["delay_min"], dep["time"], dep["realtime"]) == (15, "2026-11-24 10:15", False)


async def test_loop_trip_picks_stop_closest_to_scheduled() -> None:
    first = stop_update("StopPoint:OCETrain TER-87191007", 60, epoch("20261124T080000") + 60)
    second = stop_update("StopPoint:OCETrain TER-87191007", 900, epoch("20261124T100000") + 900)
    e = env(
        {"/departures": {"departures": [departure("88726", "20261124T100000")]}},
        trip_entity("OCESN88726F1187_F:TER:x", "20261124", [first, second]),
        b"",
    )
    [dep] = await e.departures(THIONVILLE)
    assert dep["delay_min"] == 15


async def test_absolute_time_without_delay() -> None:
    stop = stop_update("StopPoint:OCETrain TER-87191007", None, epoch("20261124T102000"))
    e = env(
        {"/departures": {"departures": [departure("88726", "20261124T100000")]}},
        trip_entity("OCESN88726F1187_F:TER:x", "20261124", [stop]),
        b"",
    )
    [dep] = await e.departures(THIONVILLE)
    assert (dep["delay_min"], dep["time"]) == (20, "2026-11-24 10:20")


async def test_early_train_rounds_like_api() -> None:
    e = env(
        {"/departures": {"departures": [departure("88726", "20261124T100000")]}},
        trip_entity("OCESN88726F1187_F:TER:x", "20261124", [stop_update("S-87191007", -30)]),
    )
    [dep] = await e.departures(THIONVILLE)
    assert dep["delay_min"] == api.delay_min("20261124T100000", "20261124T095930") == -1


async def test_failed_refresh_keeps_last_valid_feed() -> None:
    e = env(trips=TRIPS)
    feed = e.services.trips
    first = await feed.get()
    assert first
    e.feeds["trip-updates"] = None
    feed._fetched = None
    assert await feed.get() == first
    feed._fetched = None
    feed._valid_at = time.monotonic() - gtfsrt.MAX_STALE - 1
    assert await feed.get() is None


async def test_concurrent_calls_fetch_once() -> None:
    e = env(trips=TRIPS)
    await asyncio.gather(*(e.services.trips.get() for _ in range(5)))
    assert e.feed_seen == ["/resource/sncf-gtfs-rt-trip-updates"]


async def test_disruptions_unknown_line_returns_empty() -> None:
    e = env({}, TRIPS, alert_entity("Retard"))
    assert await e.disruptions(line_id="line:XYZ") == []


def test_compact_disruption_partial_period() -> None:
    result = tools._compact({"application_periods": [{"begin": "20261124T080000"}]})
    assert (result["from"], result["until"]) == ("2026-11-24 09:00", None)


async def test_previous_day_trip_far_from_scheduled_is_ignored() -> None:
    yesterday = epoch("20261123T003000") + 2400
    stop = stop_update("StopPoint:OCETrain TER-87191007", 2400, yesterday)
    e = env(
        {"/departures": {"departures": [departure("1234", "20261124T003000")]}},
        trip_entity("OCESN1234F1187_F:TER:x", "20261123", [stop], canceled=True),
        b"",
    )
    [dep] = await e.departures(THIONVILLE)
    assert (dep["delay_min"], dep["realtime"], dep["cancelled"]) == (0, False, False)


async def test_trips_sharing_number_and_date_are_all_kept() -> None:
    first = stop_update("StopPoint:OCETrain TER-87000000", 60)
    second = stop_update("StopPoint:OCETrain TER-87191007", 300)
    e = env(
        {"/departures": {"departures": [departure("8500", "20261124T100000")]}},
        trip_entity("OCESN8500F1187_F:TER:a", "20261124", [first])
        + trip_entity("OCESN8500F1187_F:TER:b", "20261124", [second]),
        b"",
    )
    [dep] = await e.departures(THIONVILLE)
    assert (dep["delay_min"], dep["realtime"]) == (5, True)


async def test_fetch_time_is_taken_after_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    def slow(_: httpx.Request) -> httpx.Response:
        clock[0] += 5
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        cached = gtfsrt.trip_updates(client)
        await cached.get()
    assert (cached._fetched, cached._valid_at) == (5, 5)


async def test_short_line_code_is_not_searched_in_alert_text() -> None:
    e = env(
        {"/lines/line:SNCF:A": {"lines": [{"code": "A", "name": "Ligne Alpha"}]}},
        TRIPS,
        alert_entity("Le train a été supprimé") + alert_entity("Travaux ligne alpha"),
    )
    result = await e.disruptions(line_id="line:SNCF:A")
    assert [d["title"] for d in result] == ["Travaux ligne alpha"]


async def test_disruptions_without_token_and_empty_feed_is_empty() -> None:
    e = env(None, TRIPS, b"")
    assert await e.disruptions() == []
    assert await e.disruptions(station_id=THIONVILLE) == []
