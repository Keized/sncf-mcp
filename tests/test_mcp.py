import json
from typing import Any

import pytest
from helpers import alert_entity, env, stop_update, trip_entity
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from sncf_mcp import tools

pytestmark = pytest.mark.anyio

STATION = {"id": "stop_area:SNCF:87191007", "name": "Thionville"}
TRIPS = trip_entity(
    "OCESN88726F1187_F:TER:x", "20261124", [stop_update("StopPoint:OCETrain TER-87191007", 300)]
)
ROUTES: dict[str, dict[str, Any]] = {
    "/places": {"places": [{**STATION, "x": 1}]},
    "/departures": {
        "departures": [
            {
                "display_informations": {"commercial_mode": "TER", "trip_short_name": "88726"},
                "stop_date_time": {
                    "base_departure_date_time": "20261124T100000",
                    "departure_date_time": "20261124T100000",
                },
                "stop_point": {"codes": [{"type": "UIC", "value": "87191007"}]},
                "links": [],
            }
        ]
    },
    "/journeys": {
        "journeys": [
            {
                "duration": 2700,
                "nb_transfers": 0,
                "sections": [
                    {
                        "type": "public_transport",
                        "display_informations": {"commercial_mode": "TER"},
                        "from": {"name": "Thionville"},
                        "to": {"name": "Metz"},
                        "departure_date_time": "20261124T100000",
                        "arrival_date_time": "20261124T104500",
                    }
                ],
            }
        ]
    },
    "/disruptions": {"disruptions": [{"severity": {"name": "API"}}]},
}


async def call(client: Client, name: str, arguments: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    result = await client.call_tool(name, arguments)
    return result.is_error, result.structured_content or {}


async def test_tools_list_publishes_output_schemas() -> None:
    mcp = MCPServer("t")
    tools.register(mcp, env().services)
    async with Client(mcp) as client:
        listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == {"find_station", "departures", "journeys", "disruptions"}
    for tool in listed:
        assert tool.output_schema is not None
        assert tool.annotations and tool.annotations.read_only_hint
        assert tool.description
    schemas = {t.name: json.dumps(t.output_schema) for t in listed}
    assert '"delay_min"' in schemas["departures"]
    assert '"legs"' in schemas["journeys"]
    assert '"from"' in schemas["disruptions"]


async def test_tools_call_through_mcp() -> None:
    mcp = MCPServer("t")
    e = env(ROUTES, TRIPS, alert_entity("Incident gare", stop="StopPoint:OCETrain TER-87191007"))
    tools.register(mcp, e.services)
    async with Client(mcp) as client:
        error, found = await call(client, "find_station", {"query": "thion"})
        assert (error, found["result"]) == (False, [STATION])

        error, deps = await call(client, "departures", {"station_id": STATION["id"]})
        [dep] = deps["result"]
        assert (error, dep["delay_min"], dep["time"], dep["realtime"]) == (
            False,
            5,
            "2026-11-24 10:05",
            True,
        )
        assert "realtime_unavailable" not in dep

        error, trips = await call(client, "journeys", {"from_station": "a", "to_station": "b"})
        [journey] = trips["result"]
        assert (error, journey["duration_min"], journey["legs"][0]["to"]) == (False, 45, "Metz")

        error, alerts = await call(client, "disruptions", {})
        assert (error, alerts["result"][0]["title"]) == (False, "Incident gare")
    assert e.api_seen[0].url.params["q"] == "thion"


async def test_tool_error_is_reported() -> None:
    mcp = MCPServer("t")
    tools.register(mcp, env().services)
    async with Client(mcp) as client:
        result = await client.call_tool("find_station", {"query": "x"})
    assert result.is_error
    assert "SNCF_API_TOKEN is not set" in result.content[0].text  # type: ignore[union-attr]


async def test_invalid_datetime_is_reported() -> None:
    mcp = MCPServer("t")
    tools.register(mcp, env().services)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "departures", {"station_id": "stop_area:SNCF:87191007", "from_datetime": "demain"}
        )
    assert result.is_error
    assert "from_datetime must be ISO 8601" in result.content[0].text  # type: ignore[union-attr]
