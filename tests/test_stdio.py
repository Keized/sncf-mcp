import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.anyio

SERVER = StdioServerParameters(
    command=sys.executable,
    args=["-c", "from sncf_mcp.app import main; main(['--transport', 'stdio'])"],
    env={"SNCF_API_TOKEN": "dummy"},
)


async def test_stdio_transport_lists_tools() -> None:
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert names == {"find_station", "departures", "journeys", "disruptions"}
