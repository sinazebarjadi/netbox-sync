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
# Different chassis families expose live draw through different CLI output:
#   classic IOS (2960X/3850):  'show power' -> "System Power Used (AC) 33 Watts"
#   IOS-XE lite (C9200L):      'show power detail' -> "Instantaneous  40" column,
#                              plus a "POE Power  <num> <consumed>" summary row.
#   High-end IOS-XE (C9300/C9500/C3850-XS): PSU capacity only — no live figure.
_SYS_POWER_USED_RE = re.compile(
    r'System\s+Power\s+Used\s*\(AC\)\s*[:\s]\s*(\d+(?:\.\d+)?)\s*W',
    re.IGNORECASE)
# C9200L POE consumed row in the Power Summary: "POE Power  477  87  1480"
_C9KL_POE_ROW_RE = re.compile(r'^\s*POE\s+Power\s+\d+\s+(\d+)\s+', re.IGNORECASE)


def _parse_show_power_used(out):
    total = 0.0
    found = False
    for m in _SYS_POWER_USED_RE.finditer(out or ""):
        total += float(m.group(1))
        found = True
    return int(round(total)) if found and total > 0 else None


def _parse_show_power_detail_c9kl(out):
    """C9200L live draw = System Power Consumed + POE Power Consumed, from the
    'Power Summary' table of 'show power detail'. These are the authoritative
    instantaneous consumption values (not budget/allocated or PSU capacity).
    Returns None when the summary isn't present (non-PoE or older platforms)."""
    sys_w = None
    poe_w = None
    for line in (out or "").splitlines():
        m = re.match(r'^\s*System\s+Power\s+\d+\s+(\d+)\s+', line, re.IGNORECASE)
        if m and sys_w is None:
            sys_w = int(m.group(1)); continue
        m = _C9KL_POE_ROW_RE.match(line)
        if m and poe_w is None:
            poe_w = int(m.group(1))
    if sys_w is not None or poe_w is not None:
        return (sys_w or 0) + (poe_w or 0)
    return None


def cisco_power_watts(sess):
    """Live AC draw for a Cisco switch, trying every CLI variant that can
    report it. Returns an int watt value or None when the platform exposes no
    instantaneous consumption (C9300/C9500 high-end chassis)."""
    # 1. classic IOS: 'show power' -> "System Power Used (AC) N Watts"
    try:
        w = _parse_show_power_used(sess.run("show power"))
        if w:
            return w
    except Exception as exc:
        log("DEBUG", f"  cisco show power failed: {exc}")
    # 2. C9200L / IOS-XE lite: 'show power detail' instantaneous + PoE consumed
    try:
        w = _parse_show_power_detail_c9kl(sess.run("show power detail"))
        if w:
            return w
    except Exception as exc:
        log("DEBUG", f"  cisco show power detail failed: {exc}")
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
