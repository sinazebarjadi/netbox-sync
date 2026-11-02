import asyncio
import json
import os

import httpx

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared._httpx_utils import McpHttpClientFactory


def _unverified_client_factory(headers=None, timeout=None, auth=None):
    kwargs = {}
    if headers is not None:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(verify=False, follow_redirects=True, **kwargs)


async def main():
    url = os.environ.get("MCP_URL", "https://<netbox-host>/mcp/sse")
    async with sse_client(url, httpx_client_factory=_unverified_client_factory) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS:", names)

            for ident in ["AFRA-HOST-10", "CZJ2470YCC", "192.168.19.47"]:
                r = await session.call_tool("netbox_get_device", {"identifier": ident})
                d = json.loads(r.content[0].text)
                dev = d.get("device") or {}
                print(f"get_device({ident!r}): found={d.get('found')} "
                      f"name={dev.get('name')!r} serial={dev.get('serial')!r} "
                      f"rack={dev.get('rack')!r} U={dev.get('position')}")

            r = await session.call_tool("netbox_rack_inventory", {"rack_name": "R16", "site": "Afranet"})
            d = json.loads(r.content[0].text)
            print(f"rack_inventory(R16): found={d.get('found')} devices={len(d.get('devices', []))}")

            r = await session.call_tool("netbox_device_power", {"device": "Afra-Host-10.snapp.net"})
            d = json.loads(r.content[0].text)
            print(f"device_power: {d.get('power_ports')}")

        print("E2E OK")

asyncio.run(main())
