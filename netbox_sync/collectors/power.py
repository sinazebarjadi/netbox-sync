"""Power-consumption collection: per-device live draw where the management
interface exposes it, else a configurable model-based default.

Sources, in priority order:
  - Server (HPE iLO)  -> Redfish /Chassis/x/Power (see redfish._read_power_watts)
  - Cisco IOS classic -> 'show power' "System Power Used (AC) N Watts"
  - Everything else   -> config.power_watts_for_model(device_type)

The public entry point is collect_device_power(...), which returns an int watt
value or None (caller then falls back to the model default)."""
import re

from netbox_sync.config import log, power_watts_for_model


# ── Cisco CLI power ──────────────────────────────────────────────────────────

_SYS_POWER_USED_RE = re.compile(
    r'System\s+Power\s+Used\s*\(AC\)\s*[:\s]\s*(\d+(?:\.\d+)?)\s*W',
    re.IGNORECASE)


def cisco_power_watts(sess):
    """Live AC draw for a Cisco switch from 'show power'. Classic IOS prints
    'System Power Used (AC) 33 Watts'; IOS-XE (C9K) reports only PSU capacity,
    so this returns None there and the caller uses the model default."""
    try:
        out = sess.run("show power")
    except Exception as exc:
        log("DEBUG", f"  cisco show power failed: {exc}")
        return None
    total = 0.0
    found = False
    for m in _SYS_POWER_USED_RE.finditer(out or ""):
        total += float(m.group(1))
        found = True
    if found and total > 0:
        return int(round(total))
    return None


# ── per-device dispatch ──────────────────────────────────────────────────────

def collect_device_power(*, family, ip=None, session=None, data=None, model=None):
    """Return (watts:int, source:str). `family` is one of 'server', 'cisco',
    'fortigate', 'storage', 'san', 'nvr', ... `data` is the collector result
    (carries live power when the collector fetched it); `session` is an open
    CLI session for families that read power lazily. Falls back to the model
    default when no live reading is available."""
    # 1. live value already collected by the device collector (e.g. redfish)
    if data:
        summary = data.get("summary") or {}
        w = summary.get("power_watts")
        if isinstance(w, (int, float)) and w > 0:
            return int(round(w)), "live"

    # 2. Cisco CLI read (classic IOS only)
    if family == "cisco" and session is not None:
        w = cisco_power_watts(session)
        if w:
            return w, "cli"

    # 3. model default
    return power_watts_for_model(model), "default"
