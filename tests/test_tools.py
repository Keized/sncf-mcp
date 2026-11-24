from typing import Any

import httpx
import pytest
from helpers import env, reply

from sncf_mcp.api import SncfApiError

pytestmark = pytest.mark.anyio


async def test_find_station() -> None:
    e = env(
        reply({"places": [{"id": "stop_area:SNCF:87191007", "name": "Thionville", "x": 1}]}),
    )
    assert await e.find_station("thionville") == [
        {"id": "stop_area:SNCF:87191007", "name": "Thionville"}
    ]
    assert e.api_seen[0].url.params["q"] == "thionville"


async def test_departures_delay_cancel_platform() -> None:
    def dep(code: str, base: str, real: str, links: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "display_informations": {
                "commercial_mode": "TER",
                "code": "C50",
                "trip_short_name": code,
                "headsign": "Metz",
                "direction": "Metz (Metz)",
            },
            "stop_date_time": {
                "base_departure_date_time": base,
                "departure_date_time": real,
                "data_freshness": "realtime",
            },
            "stop_point": {"platform_code": "3"},
            "links": links,
        }

    body = {
        "departures": [
            dep("88512", "20261124T080000", "20261124T080700", []),
            dep(
                "88514", "20261124T090000", "20261124T090000", [{"type": "disruption", "id": "d1"}]
            ),
        ],
        "disruptions": [{"id": "d1", "severity": {"effect": "NO_SERVICE"}}],
    }
    e = env(reply(body))
    result = await e.departures("sa:1", "2026-11-24T08:00:00")
    assert result[0]["delay_min"] == 7
    assert result[0]["platform"] == "3"
    assert result[0]["train"] == "TER 88512"
    assert result[0]["time"] == "2026-11-24 08:07"
    assert not result[0]["cancelled"]
    assert result[1]["cancelled"]
    assert e.api_seen[0].url.params["from_datetime"] == "20261124T080000"
    assert e.api_seen[0].url.params["data_freshness"] == "realtime"


async def test_departures_converts_utc_offset_to_paris() -> None:
    e = env(reply({"departures": []}))
    await e.departures("sa:1", "2026-11-24T07:00:00+00:00")
    assert e.api_seen[0].url.params["from_datetime"] == "20261124T080000"


async def test_journeys_compact() -> None:
    body = {
        "journeys": [
            {
                "departure_date_time": "20261124T080000",
                "arrival_date_time": "20261124T091500",
                "duration": 4500,
                "nb_transfers": 0,
                "sections": [
                    {"type": "waiting"},
                    {
                        "type": "public_transport",
                        "display_informations": {
                            "commercial_mode": "TER",
                            "code": "C50",
                            "trip_short_name": "88835",
                        },
                        "from": {"name": "Metz"},
                        "to": {"name": "Luxembourg"},
                        "departure_date_time": "20261124T080500",
                        "arrival_date_time": "20261124T091500",
                        "base_departure_date_time": "20261124T080000",
                        "links": [],
                    },
                ],
            }
        ]
    }
    e = env(reply(body))
    result = await e.journeys("a", "b", "2026-11-24T08:00")
    assert result[0]["duration_min"] == 75
    assert result[0]["legs"][0]["delay_min"] == 5
    assert result[0]["legs"][0]["to"] == "Luxembourg"
    assert result[0]["legs"][0]["train"] == "TER 88835"
    assert e.api_seen[0].url.params["from"] == "a"


async def test_disruptions_none_is_empty_list() -> None:
    body = {
        "error": {"id": "unknown_object", "message": "ptref : Filters: Unable to find object"},
        "disruptions": [],
    }
    e = env(reply(body, 404))
    assert await e.disruptions() == []


async def test_disruptions_paths_and_times() -> None:
    body = {
        "disruptions": [
            {
                "status": "active",
                "severity": {"effect": "SIGNIFICANT_DELAYS", "name": "Retard"},
                "messages": [{"text": "Incident"}],
                "application_periods": [{"begin": "20261124T070000", "end": "20261124T100000"}],
            }
        ]
    }
    e = env(reply(body))
    result = await e.disruptions(station_id="sa:1")
    assert e.api_seen[0].url.path.endswith("/stop_areas/sa:1/disruptions")
    assert result[0]["from"] == "2026-11-24 08:00"
    assert result[0]["message"] == "Incident"
    await e.disruptions(line_id="l:1")
    assert e.api_seen[1].url.path.endswith("/lines/l:1/disruptions")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "rejected the token"), (429, "quota"), (500, "error 500"), (404, "Nothing found")],
)
async def test_http_errors_are_clean(status: int, expected: str) -> None:
    e = env(reply({}, status))
    with pytest.raises(SncfApiError, match=expected):
        await e.find_station("x")


async def test_timeout_message() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    e = env(boom)
    with pytest.raises(SncfApiError, match="timed out"):
        await e.find_station("x")


async def test_missing_token() -> None:
    e = env()
    with pytest.raises(SncfApiError, match="SNCF_API_TOKEN is not set"):
        await e.find_station("x")


async def test_invalid_json_is_clean() -> None:
    e = env(lambda _: httpx.Response(200, content="<html>maintenance</html>"))
    with pytest.raises(SncfApiError, match="invalid response"):
        await e.find_station("x")


async def test_ids_are_encoded_in_path() -> None:
    e = env(reply({"departures": []}))
    await e.departures("x/../../journeys?from=a#", None)
    path = e.api_seen[0].url.raw_path.split(b"?")[0]
    assert path.endswith(b"/stop_areas/x%2F..%2F..%2Fjourneys%3Ffrom%3Da%23/departures")


async def test_count_is_bounded() -> None:
    e = env(reply({"departures": [], "journeys": []}))
    await e.departures("sa:1", None, 100000)
    await e.departures("sa:1", None, -1)
    await e.journeys("a", "b", None, 0)
    assert [r.url.params["count"] for r in e.api_seen] == ["50", "1", "1"]


async def test_malformed_api_data_degrades() -> None:
    body = {
        "departures": [
            {
                "display_informations": {"code": "1"},
                "stop_date_time": {
                    "base_departure_date_time": "garbage",
                    "departure_date_time": "20261124T080000.123",
                },
                "links": [{"type": "disruption"}],
            }
        ]
    }
    e = env(reply(body))
    result = await e.departures("sa:1")
    assert result[0]["time"] == "2026-11-24 08:00"
    assert result[0]["scheduled"] is None
    assert result[0]["delay_min"] == 0
    assert not result[0]["cancelled"]


@pytest.mark.parametrize("body", [{"error": "bad gateway"}, ["bad"]])
async def test_non_object_error_body_is_clean(body: Any) -> None:
    e = env(reply(body, 502))
    with pytest.raises(SncfApiError, match="error 502"):
        await e.find_station("x")
