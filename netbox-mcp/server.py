# NetBox MCP server (FastMCP) — read-only inventory for Brain Bot.
#
# Start (SSE, production):  NETBOX_URL=... NETBOX_TOKEN=... python server.py --sse
# Local STDIO test:         python server.py
#
# Mount path: the server is fronted by nginx at /mcp/ (same host:443 as
# NetBox), so routes live under /mcp/sse and /mcp/ on the public URL while
# the backend itself listens on 127.0.0.1:8901 with paths /sse and /mcp.
"""MCP server: NetBox inventory tools (GET-only, read-only token)."""
import argparse
import json

import os

from mcp.server.fastmcp import FastMCP

import netbox_api as nb

# SSE binding comes from env (MCP_HOST/MCP_PORT) — stdio is the default run mode.
mcp = FastMCP("netbox",
              host=os.environ.get("MCP_HOST", "127.0.0.1"),
              port=int(os.environ.get("MCP_PORT", "8901")))


def _compact(dev):
    if not dev:
        return None
    cf = dev.get("custom_fields") or {}
    site = dev.get("site") or {}
    rack = dev.get("rack") or {}
    role = dev.get("role") or {}
    dtype = dev.get("device_type") or {}
    mfr = getattr(dtype, "get", lambda *a: None)("manufacturer") if isinstance(dtype, dict) else None
    return {
        "id": dev.get("id"),
        "name": dev.get("name"),
        "serial": dev.get("serial"),
        "asset_tag": dev.get("asset_tag"),
        "status": (dev.get("status") or {}).get("value") if isinstance(dev.get("status"), dict) else dev.get("status"),
        "role": role.get("name") if isinstance(role, dict) else role,
        "device_type": dtype.get("model") if isinstance(dtype, dict) else dtype,
        "manufacturer": (mfr.get("name") if isinstance(mfr, dict) else mfr),
        "site": site.get("name") if isinstance(site, dict) else site,
        "rack": rack.get("name") if isinstance(rack, dict) else rack,
        "position": dev.get("position"),
        "face": dev.get("face", {}).get("value") if isinstance(dev.get("face"), dict) else dev.get("face"),
        "primary_ip4": (cf.get("bmc_ip") or (dev.get("primary_ip4") or {}).get("address") if isinstance(dev.get("primary_ip4"), dict) else cf.get("bmc_ip")),
        "custom_fields": {k: v for k, v in cf.items() if v not in (None, "", False)},
    }


@mcp.tool()
def netbox_get_device(identifier: str) -> str:
    """Full NetBox record for one device: resolve by exact name, serial, or
    management IP. Returns name, role, manufacturer, model, serial, asset tag,
    site/rack/U position, management IP, hardware specs (CPU/RAM/disk), power
    draw, status, and all set custom fields."""
    try:
        dev = nb.get_device(identifier)
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    if not dev:
        return json.dumps({"found": False, "identifier": identifier})
    return json.dumps({"found": True, "device": _compact(dev)}, indent=2)


@mcp.tool()
def netbox_search_devices(query: str, limit: int = 10) -> str:
    """Search NetBox devices by name/serial substring. Returns compact cards:
    name, serial, role, model, site, rack."""
    try:
        devs = nb.search_devices(query, limit=min(max(limit, 1), 50))
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    return json.dumps({"count": len(devs), "devices": [_compact(d) for d in devs]}, indent=2)


@mcp.tool()
def netbox_rack_inventory(rack_name: str, site: str = "") -> str:
    """Every device in a named rack (optionally site-qualified) with U positions,
    the rack's power feed(s), and a device list. Use to answer 'what is in
    rack R16?'."""
    try:
        r = nb.get_rack_inventory(rack_name, site or None)
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    if not r:
        return json.dumps({"found": False, "rack": rack_name})
    return json.dumps({
        "found": True,
        "rack": {"name": r["rack"].get("name"), "u_height": r["rack"].get("u_height"),
                 "site": (r["rack"].get("site") or {}).get("name")},
        "power_feeds": [
            {"name": f.get("name"), "voltage": f.get("voltage"), "amperage": f.get("amperage"),
             "max_utilization": f.get("max_utilization"), "description": f.get("description")}
            for f in (r.get("power_feeds") or [])],
        "devices": [
            {"name": d.get("name"), "position": d.get("position"), "serial": d.get("serial"),
             "role": (d.get("role") or {}).get("name"),
             "model": (d.get("device_type") or {}).get("model")} for d in r["devices"]],
    }, indent=2)


@mcp.tool()
def netbox_interface_info(device: str) -> str:
    """All interfaces of a device (name/UUID/mode) with assigned IP addresses."""
    try:
        d = nb.get_device(device)
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    if not d:
        return json.dumps({"found": False, "device": device})
    infos = nb.get_interface_info(d["id"])
    out = []
    for it in infos:
        i = it["interface"]
        out.append({"interface": {"name": i.get("name"), "type": i.get("type", {}).get("value") if isinstance(i.get("type"), dict) else i.get("type"),
                                  "mac_address": i.get("mac_address"), "mtu": i.get("mtu"),
                                  "mode": (i.get("mode") or {}).get("value") if isinstance(i.get("mode"), dict) else None},
                    "ip_addresses": [{"address": a.get("address"), "status": a.get("status", {}).get("value") if isinstance(a.get("status"), dict) else a.get("status"),
                                      "dns_name": a.get("dns_name"), "description": a.get("description")} for a in it["ip_addresses"]]})
    return json.dumps({"device": d.get("name"), "count": len(out), "interfaces": out}, indent=2)


@mcp.tool()
def netbox_lookup_ip(address: str) -> str:
    """Who owns an IP: device, interface, prefix, DNS, status/description."""
    try:
        info = nb.search_ip(address)
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    return json.dumps({
        "query": address,
        "ip_records": [{"address": h.get("address"),
                        "status": h.get("status", {}).get("value") if isinstance(h.get("status"), dict) else h.get("status"),
                        "dns_name": h.get("dns_name"), "description": h.get("description")} for h in info["matches"]],
        "interface": {"name": info["interface"].get("name"), "mac": info["interface"].get("mac_address")} if info["interface"] else None,
        "device": _compact(info["device"]) if info["device"] else None,
    }, indent=2)


@mcp.tool()
def netbox_device_power(device: str) -> str:
    """Power draw for one device: measured watts on its PSU1 port (live Redfish
    reading where available, else model default) and the rack feed electrical
    spec (voltage, amps, utilization headroom)."""
    try:
        d = nb.get_device(device)
    except Exception as e:
        return json.dumps({"error": f"NetBox query failed: {e}"})
    if not d:
        return json.dumps({"found": False, "device": device})
    info = nb.get_device_power(d["id"])
    feed = info.get("feed") or {}
    return json.dumps({
        "device": d.get("name"),
        "power_ports": [{"name": p.get("name"), "type": p.get("type", {}).get("value") if isinstance(p.get("type"), dict) else p.get("type"),
                         "allocated_draw_w": p.get("allocated_draw"),
                         "maximum_draw_w": p.get("maximum_draw")} for p in info["power_ports"]],
        "feed": {"name": feed.get("name"), "voltage": feed.get("voltage"), "amperage": feed.get("amperage"),
                 "max_utilization": feed.get("max_utilization"), "description": feed.get("description")} if feed else None,
    }, indent=2)


def main():
    parser = argparse.ArgumentParser(description="NetBox read-only MCP server")
    parser.add_argument("--sse", action="store_true",
                        help="Serve SSE on MCP_HOST:MCP_PORT (default stdio), mounted at MCP_MOUNT_PATH (default /mcp)")
    parser.add_argument("--http", dest="http", action="store_true",
                        help="Serve Streamable HTTP on MCP_HOST:MCP_PORT instead of stdio")
    args = parser.parse_args()
    if args.sse:
        mcp.run(transport="sse", mount_path=os.environ.get("MCP_MOUNT_PATH", "/mcp"))
    elif args.http:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
