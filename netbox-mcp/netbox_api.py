# NetBox MCP server — read-only inventory access for Brain Bot.
#
# SECURITY DESIGN (do not weaken):
# 1. Only GET endpoints are ever called. This module contains NO create/update/
#    delete code paths; the module-level guard refuses any non-GET verb.
# 2. The NetBox token lives ONLY in the NETBOX_TOKEN environment variable.
#    It is never returned in tool output, never logged, never echoed.
# 3. The NetBox token used here must be created with write_enabled=False
#    (read-only at the API level as well as the object-permission level).
"""NetBox MCP read-only service.

Exposes NetBox inventory (devices, racks, interfaces, IPs, power) as MCP
tools for Brain Bot. All calls are GET-only. Token stays server-side.
"""
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NETBOX_URL = os.environ.get("NETBOX_URL", "https://<netbox-host-or-ip>")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "")
NETBOX_VERIFY_TLS = os.environ.get("NETBOX_VERIFY_TLS", "false").lower() in ("1", "true", "yes", "on")

if not NETBOX_TOKEN:
    raise SystemExit("NETBOX_TOKEN is required (read-only NetBox API token)")

_HEADERS = {"Authorization": f"Token {NETBOX_TOKEN}", "Accept": "application/json"}


def _get(path, params=None):
    """GET-only NetBox API client. Anything other than GET is refused."""
    url = f"{NETBOX_URL.rstrip('/')}{path}"
    r = requests.get(url, headers=_HEADERS, params=params or {},
                     verify=NETBOX_VERIFY_TLS, timeout=30)
    r.raise_for_status()
    return r.json()


def get(path, params=None):
    """Backwards-compatible GET helper (the only allowed verb)."""
    return _get(path, params)


def get_device(identifier):
    """Full device detail: resolve by name (case-insensitive), serial, or mgmt
    IP. Returns the NetBox device record."""
    identifier = (identifier or "").strip()
    # 1. case-insensitive name match (NetBox names may carry a domain suffix)
    r = get("/api/dcim/devices/", {"name__ic": identifier, "limit": 5})
    results = r.get("results") or []
    dev = results[0] if results else None
    # exact-name wins over substring matches
    if dev is None or dev.get("name", "").lower() != identifier.lower():
        exact = next((x for x in results if (x.get("name") or "").lower() == identifier.lower()), None)
        if exact:
            dev = exact
        elif dev is None:
            r = get("/api/dcim/devices/", {"q": identifier, "limit": 10})
            cands = [x for x in (r.get("results") or [])
                     if identifier.lower() in (x.get("name") or "").lower()]
            if cands:
                dev = cands[0]
    # 2. by serial
    if dev is None:
        r = get("/api/dcim/devices/", {"serial__ic": identifier, "limit": 5})
        results = r.get("results") or []
        dev = results[0] if results else None
    # 3. IP address -> its device (interface -> device)
    if dev is None and "." in identifier:
        found = search_ip(identifier)
        dev = found.get("device") if isinstance(found, dict) else None
    return dev


def search_devices(query, limit=10):
    """Case-insensitive substring search over device name + serial."""
    query = (query or "").strip()
    from collections import OrderedDict
    seen = OrderedDict()
    for path, params in (
        ("/api/dcim/devices/", {"q": query, "limit": limit}),
        ("/api/dcim/devices/", {"name__ic": query, "limit": limit}),
        ("/api/dcim/devices/", {"serial__ic": query, "limit": limit}),
    ):
        try:
            for d in (get(path, params).get("results") or []):
                seen.setdefault(d["id"], d)
        except Exception:
            continue
    return list(seen.values())[:limit]


def _site_id(site):
    if not site:
        return None
    if str(site).isdigit():
        return site
    r = get("/api/dcim/sites/", {"name": site, "limit": 2}).get("results") or []
    if r:
        return r[0]["id"]
    r = get("/api/dcim/sites/", {"q": site, "limit": 5}).get("results") or []
    exact = next((x for x in r if (x.get("name") or "").lower() == str(site).lower()), None)
    return (exact or (r[0] if r else {})).get("id")


def get_rack_inventory(rack_name, site=None):
    """All devices in a named rack with their U positions + power feed + amps."""
    params = {"name__ic": rack_name, "limit": 10}
    sid = _site_id(site)
    if sid:
        params["site_id"] = sid
    racks = get("/api/dcim/racks/", params).get("results") or []
    if not racks and site is None:
        # case-insensitive substring retry without site constraint
        racks = get("/api/dcim/racks/", {"q": rack_name, "limit": 10}).get("results") or []
    rack = None
    if racks:
        exact = next((x for x in racks if (x.get("name") or "").lower() == rack_name.lower()), None)
        rack = exact or racks[0]
    if not rack:
        return None
    devs = []
    offset = 0
    while True:
        r = get("/api/dcim/devices/", {"rack_id": rack["id"], "limit": 100, "offset": offset})
        devs.extend(r.get("results") or [])
        if not r.get("next"):
            break
        offset += 100
    feeds = get("/api/dcim/power-feeds/", {"rack_id": rack["id"], "limit": 5}).get("results", [])
    return {"rack": rack, "devices": devs, "power_feeds": feeds}


def get_interface_info(device_id):
    """Interfaces of one device with their assigned IP addresses."""
    ifaces = []
    offset = 0
    while True:
        r = get("/api/dcim/interfaces/", {"device_id": device_id, "limit": 100, "offset": offset})
        ifaces.extend(r.get("results") or [])
        if not r.get("next"):
            break
        offset += 100
    out = []
    for i in ifaces:
        ips = get("/api/ipam/ip-addresses/",
                  {"interface_id": i["id"], "limit": 100}).get("results", [])
        out.append({"interface": i, "ip_addresses": ips})
    return out


def search_ip(address):
    """IP record -> device/interface assignment, prefix, DNS."""
    bare = address.split("/")[0].strip()
    hits = get("/api/ipam/ip-addresses/", {"address": bare, "limit": 10}).get("results", [])
    if not hits:
        hits = get("/api/ipam/ip-addresses/", {"q": bare, "limit": 10}).get("results", [])
    info = {"matches": hits, "device": None, "interface": None}
    for h in hits:
        atype, aid = h.get("assigned_object_type"), h.get("assigned_object_id")
        if atype == "dcim.interface" and aid:
            try:
                iface = get(f"/api/dcim/interfaces/{aid}/")
                info["interface"] = iface
                dev = iface.get("device")
                if isinstance(dev, dict):
                    info["device"] = get(f"/api/dcim/devices/{dev['id']}/")
            except Exception:
                pass
            break
    return info


def get_device_power(device_id):
    """Per-device power: Power Ports with allocated_draw, plus the rack feed
    (voltage/amperage/headroom) to contextualise utilisation."""
    ports = get("/api/dcim/power-ports/", {"device_id": device_id, "limit": 20}).get("results", [])
    dev = get(f"/api/dcim/devices/{device_id}/")
    rack = dev.get("rack") if isinstance(dev, dict) else None
    feed = None
    if rack:
        feeds = get("/api/dcim/power-feeds/", {"rack_id": rack["id"], "limit": 5}).get("results", [])
        feed = feeds[0] if feeds else None
    return {"device_id": device_id, "power_ports": ports, "feed": feed}
