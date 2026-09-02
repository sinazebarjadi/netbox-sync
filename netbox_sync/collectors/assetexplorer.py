"""ManageEngine AssetExplorer REST API: session and asset collection.

Auth: technician API key (header `technician_key`). The v3 list endpoint
takes an `input_data` JSON payload with list_info pagination.
"""
import json

import requests
import urllib3

from netbox_sync.config import (AE_URL, AE_API_KEY, log)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Only these AE product types become NetBox devices (infra-relevant).
# Anything else (Workstation, Printer, Software, no type, ...) is skipped.
# Custom types (CCTV, NVR, etc.) have internal_name=None — the fallback to
# `name` below handles them.
AE_TYPE_TO_ROLE = {
    "Server":        "Server",
    "Switch":        "Switch",
    "Router":        "Router",
    "Firewall":      "Firewall",
    "Access Point":  "Access Point",
    "Storage Device": "Storage",
    "CCTV":          "Camera",    # custom type — cameras
    "NVR":           "NVR",       # custom type — NVRs
}

# Component product types (Inventory Items, attached to a device or warehouse)
AE_COMPONENT_TYPES = {
    "Power Module":  "PSU",
    "HARD-Hardware": "HDD",       # server disks
    "HARD-CCTV":     "HDD",       # NVR disks
    "NM Module":     "Module",    # network expansion modules
}

AE_STATE_TO_STATUS = {
    "In Use":   "active",
    "In Store": "inventory",
    # NetBox has no "expired" device status — decommissioning is the closest
    "Expired":  "decommissioning",
}


def ae_fetch_assets():
    """Fetch all assets from AssetExplorer -> (records, stats).

    records: normalized dicts ready for sync (serial + mapped product type).
    stats:   {"fetched": N, "skipped_no_type": M, "skipped_no_serial": K}
    """
    if not (AE_URL and AE_API_KEY):
        raise RuntimeError("AE_URL / AE_API_KEY not configured in .env")

    base = AE_URL.rstrip("/")
    headers = {"technician_key": AE_API_KEY}
    out = []
    stats = {"fetched": 0, "skipped_no_type": 0, "skipped_no_serial": 0}
    start = 1
    while True:
        r = requests.get(
            f"{base}/api/v3/assets", headers=headers, verify=False, timeout=60,
            params={"input_data": json.dumps(
                {"list_info": {"row_count": 200, "start_index": start}})})
        r.raise_for_status()
        data = r.json()
        batch = data.get("assets") or []
        stats["fetched"] += len(batch)
        for a in batch:
            rec = _normalize(a, stats)
            if rec:
                out.append(rec)
        if not (data.get("list_info") or {}).get("has_more_rows"):
            break
        start += len(batch)
        if start > 50000:          # safety stop against API quirks
            log("WARN", "  AE pagination exceeded 50k — stopping")
            break
    log("INFO", f"  assetexplorer: {stats['fetched']} fetched, "
                f"{len(out)} syncable, "
                f"{stats['skipped_no_type']} without mapped product type, "
                f"{stats['skipped_no_serial']} without serial")
    return out, stats


def _normalize(a, stats=None):
    serial = (a.get("org_serial_number") or "").strip()
    pt = a.get("product_type") or {}
    ptype = (pt.get("internal_name") or pt.get("name") or "").strip()
    if not ptype:
        if stats is not None: stats["skipped_no_type"] += 1
        return None

    # Check if it's a device role OR a component type
    role = AE_TYPE_TO_ROLE.get(ptype)
    component_role = AE_COMPONENT_TYPES.get(ptype)
    if not role and not component_role:
        if stats is not None: stats["skipped_no_type"] += 1
        return None
    if not serial:
        if stats is not None: stats["skipped_no_serial"] += 1
        return None

    state = ((a.get("state") or {}).get("name") or "").strip()
    used_by = a.get("used_by_asset") or {}
    udf = a.get("udf_fields") or {}
    # Product name (e.g. "WD64PURZ-85BWUY0" or "power switch") for inventory items
    product_name = ((a.get("product") or {}).get("name") or "").strip() or None
    # Capacity is in udf_sline_601 (e.g. "960GB", "8TB")
    capacity = (udf.get("udf_sline_601") or "").strip() or None

    return {
        "ae_id":          a.get("id"),
        "name":           (a.get("name") or "").strip() or f"AE-{a.get('id')}",
        "serial":         serial,
        "role":           role,
        "component_role": component_role,
        "is_component":   bool(component_role),
        "manufacturer":   ((a.get("manufacturer")
                            or (a.get("product") or {}).get("manufacturer")
                            or "").strip() or None),
        "model":          ((a.get("product") or {}).get("name") or "").strip() or None,
        "part_number":    udf.get("udf_sline_602") or product_name,
        "capacity":       capacity,
        "asset_tag":      (a.get("asset_tag") or "").strip() or None,
        "site":           ((a.get("site") or {}).get("name") or "").strip() or None,
        "status":         AE_STATE_TO_STATUS.get(state, "inventory"),
        "department":     ((a.get("department") or {}).get("name")
                           or "").strip() or None,
        "location":       (a.get("location") or "").strip() or None,
        "description":    (a.get("description") or "").strip() or None,
        "used_by_name":   (used_by.get("name") or "").strip() or None,
        "used_by_serial": (used_by.get("org_serial_number") or "").strip() or None,
    }
