"""Configuration: .env loading, credentials, scan ranges, logging, validation.

Everything in this module is read from the environment at import time, right
after load_dotenv() runs, so importing any netbox_sync module picks up the
user's .env exactly like the old monolith did.
"""
import ipaddress
import os
from datetime import datetime

import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()
# Cron-safe fallback: dotenv's upward search normally finds the repo .env
# from any cwd, but make it explicit if NETBOX_URL is still missing.
if not os.getenv("NETBOX_URL"):
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# ── credentials ──────────────────────────────────────────────────────────────
NETBOX_URL   = os.getenv("NETBOX_URL")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")
REDFISH_USER = os.getenv("REDFISH_USER")
REDFISH_PASS = os.getenv("REDFISH_PASS")

STORAGE_USER = os.getenv("STORAGE_USER")
STORAGE_PASS = os.getenv("STORAGE_PASS")

SWITCH_USER = os.getenv("SWITCH_USER")
SWITCH_PASS = os.getenv("SWITCH_PASS")

CISCO_USER = os.getenv("CISCO_USER")
CISCO_PASS = os.getenv("CISCO_PASS")

FORTIGATE_USER = os.getenv("FORTIGATE_USER")
FORTIGATE_PASS = os.getenv("FORTIGATE_PASS")

RUCKUS_USER = os.getenv("RUCKUS_USER")
RUCKUS_PASS = os.getenv("RUCKUS_PASS")

UNIFI_USER = os.getenv("UNIFI_USER")
UNIFI_PASS = os.getenv("UNIFI_PASS")

HIKVISION_USER = os.getenv("HIKVISION_USER")
HIKVISION_PASS = os.getenv("HIKVISION_PASS")

DAHUA_USER = os.getenv("DAHUA_USER")
DAHUA_PASS = os.getenv("DAHUA_PASS")

UNV_USER = os.getenv("UNV_USER")
UNV_PASS = os.getenv("UNV_PASS")

# FortiWeb shares the Fortinet readonly admin account by default (same
# FORTIGATE_USER/PASS) unless explicitly overridden.
FORTIWEB_USER = os.getenv("FORTIWEB_USER") or os.getenv("FORTIGATE_USER")
FORTIWEB_PASS = os.getenv("FORTIWEB_PASS") or os.getenv("FORTIGATE_PASS")

FMC_USER = os.getenv("FMC_USER")
FMC_PASS = os.getenv("FMC_PASS")

# ManageEngine AssetExplorer (offline/inventory asset source, opt-in)
AE_URL     = os.getenv("AE_URL")        # e.g. https://172.31.5.155
AE_API_KEY = os.getenv("AE_API_KEY")    # technician API key

REQUIRED_ENV_VARS = ("NETBOX_URL", "NETBOX_TOKEN",
                     "REDFISH_USER", "REDFISH_PASS",
                     "STORAGE_USER", "STORAGE_PASS",
                     "SWITCH_USER", "SWITCH_PASS")

def _validate_config():
    """Fail fast at startup if required .env variables are missing.
    Kept out of module scope so the modules stay importable for tests."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    # Cisco family is opt-in; its creds are required only when ranges are set.
    if os.getenv("CISCO_RANGES") and (not os.getenv("CISCO_USER")
                                      or not os.getenv("CISCO_PASS")):
        missing.append("CISCO_USER/CISCO_PASS (required when CISCO_RANGES is set)")
    # FortiGate family is opt-in: basic-auth admin creds required when ranges are set.
    if os.getenv("FORTIGATE_RANGES") and (not os.getenv("FORTIGATE_USER")
                                          or not os.getenv("FORTIGATE_PASS")):
        missing.append("FORTIGATE_USER/FORTIGATE_PASS (required when FORTIGATE_RANGES is set)")
    # Ruckus family is opt-in; SSH creds required only when ranges are set.
    if os.getenv("RUCKUS_RANGES") and (not os.getenv("RUCKUS_USER")
                                       or not os.getenv("RUCKUS_PASS")):
        missing.append("RUCKUS_USER/RUCKUS_PASS (required when RUCKUS_RANGES is set)")
    # UniFi family is opt-in; console creds required only when ranges are set.
    if os.getenv("UNIFI_RANGES") and (not os.getenv("UNIFI_USER")
                                      or not os.getenv("UNIFI_PASS")):
        missing.append("UNIFI_USER/UNIFI_PASS (required when UNIFI_RANGES is set)")
    # Hikvision family is opt-in; digest creds required only when ranges are set.
    if os.getenv("HIKVISION_RANGES") and (not os.getenv("HIKVISION_USER")
                                          or not os.getenv("HIKVISION_PASS")):
        missing.append("HIKVISION_USER/HIKVISION_PASS (required when HIKVISION_RANGES is set)")
    # Dahua family is opt-in; digest creds required only when ranges are set.
    if os.getenv("DAHUA_RANGES") and (not os.getenv("DAHUA_USER")
                                      or not os.getenv("DAHUA_PASS")):
        missing.append("DAHUA_USER/DAHUA_PASS (required when DAHUA_RANGES is set)")
    # Uniview family is opt-in; digest creds required only when ranges are set.
    if os.getenv("UNV_RANGES") and (not os.getenv("UNV_USER")
                                    or not os.getenv("UNV_PASS")):
        missing.append("UNV_USER/UNV_PASS (required when UNV_RANGES is set)")
    # FortiWeb family is opt-in; admin creds required only when ranges are set.
    # Falls back to FORTIGATE_USER/PASS, so only fails if both are unset.
    if os.getenv("FORTIWEB_RANGES") and not (FORTIWEB_USER and FORTIWEB_PASS):
        missing.append("FORTIWEB_USER/FORTIWEB_PASS (or FORTIGATE_USER/FORTIGATE_PASS) "
                       "(required when FORTIWEB_RANGES is set)")
    # FTD family (via FMC) is opt-in; FMC admin creds required when ranges set.
    if os.getenv("FMC_RANGES") and (not os.getenv("FMC_USER")
                                    or not os.getenv("FMC_PASS")):
        missing.append("FMC_USER/FMC_PASS (required when FMC_RANGES is set)")
    # AssetExplorer sync is opt-in; both settings required together.
    if bool(os.getenv("AE_URL")) != bool(os.getenv("AE_API_KEY")):
        missing.append("AE_URL and AE_API_KEY must be set together")
    if missing:
        raise RuntimeError(f"Missing required .env variables: {', '.join(missing)}")

def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# ── config – ranges ──────────────────────────────────────────────────────────
def _parse_ranges(env_name, default):
    """Range semantics: unset env var -> default; set-but-empty -> family
    disabled ([]); set -> comma-separated CIDR list."""
    val = os.getenv(env_name)
    if val is None:
        return list(default)
    return [r.strip() for r in val.split(",") if r.strip()]

DEFAULT_BMC_RANGES = [
    "192.0.2.0/27",
    "198.51.100.0/27",
]
BMC_RANGES = _parse_ranges("BMC_RANGES", DEFAULT_BMC_RANGES)

DEFAULT_STORAGE_RANGES = [
    "192.0.2.16/32",
    "198.51.100.16/32",
]
STORAGE_RANGES = _parse_ranges("STORAGE_RANGES", DEFAULT_STORAGE_RANGES)

DEFAULT_SAN_RANGES = [
    "192.0.2.32/29",
    "198.51.100.32/29",
]
SAN_RANGES = _parse_ranges("SAN_RANGES", DEFAULT_SAN_RANGES)

# Cisco family is opt-in: empty default means "disabled".
CISCO_RANGES = _parse_ranges("CISCO_RANGES", [])

# FortiGate family is opt-in: empty default means "disabled".
FORTIGATE_RANGES = _parse_ranges("FORTIGATE_RANGES", [])

# Ruckus family is opt-in: empty default means "disabled".
RUCKUS_RANGES = _parse_ranges("RUCKUS_RANGES", [])
RUCKUS_HA_MAP = os.getenv("RUCKUS_HA_MAP", "")

# Hikvision family is opt-in: empty default means "disabled".
HIKVISION_RANGES = _parse_ranges("HIKVISION_RANGES", [])

# Dahua family is opt-in: empty default means "disabled".
DAHUA_RANGES = _parse_ranges("DAHUA_RANGES", [])

# Uniview family is opt-in: empty default means "disabled".
UNV_RANGES = _parse_ranges("UNV_RANGES", [])

# FortiWeb family is opt-in: empty default means "disabled".
FORTIWEB_RANGES = _parse_ranges("FORTIWEB_RANGES", [])

# FTD family (via FMC) is opt-in: empty default means "disabled". Each range
# holds FMC appliance IPs whose managed FTDs are imported.
FMC_RANGES = _parse_ranges("FMC_RANGES", [])

# UniFi family is opt-in: empty default means "disabled". Each range holds
# UniFi OS console IPs (multi-site consoles are queried per site).
UNIFI_RANGES = _parse_ranges("UNIFI_RANGES", [])

FORTIGATE_PORT     = int(os.getenv("FORTIGATE_PORT", "443"))
FORTIGATE_SSH_PORT = int(os.getenv("FORTIGATE_SSH_PORT", "22"))
FORTIGATE_ROLE     = os.getenv("DEFAULT_FORTIGATE_ROLE", "Firewall")
RUCKUS_PORT        = int(os.getenv("RUCKUS_PORT", "22"))
RUCKUS_ROLE        = os.getenv("DEFAULT_RUCKUS_ROLE", "Wireless Controller")
UNIFI_PORT         = int(os.getenv("UNIFI_PORT", "8443"))
UNIFI_ROLE         = os.getenv("DEFAULT_UNIFI_ROLE", "Wireless Controller")
AP_ROLE            = os.getenv("DEFAULT_AP_ROLE", "Access Point")
HIKVISION_PORT     = int(os.getenv("HIKVISION_PORT", "80"))
HIKVISION_ROLE     = os.getenv("DEFAULT_HIKVISION_ROLE", "NVR")
HIKVISION_CAMERA_ROLE = os.getenv("DEFAULT_HIKVISION_CAMERA_ROLE", "Camera")
DAHUA_PORT         = int(os.getenv("DAHUA_PORT", "80"))
DAHUA_ROLE         = os.getenv("DEFAULT_DAHUA_ROLE", "NVR")
UNV_PORT           = int(os.getenv("UNV_PORT", "80"))
UNV_ROLE           = os.getenv("DEFAULT_UNV_ROLE", "NVR")
FORTIWEB_PORT      = int(os.getenv("FORTIWEB_PORT", "58291"))
FORTIWEB_ROLE      = os.getenv("DEFAULT_FORTIWEB_ROLE", "WAF")
FMC_PORT           = int(os.getenv("FMC_PORT", "443"))
FTD_ROLE           = os.getenv("DEFAULT_FTD_ROLE", "FTD")

REDFISH_PORT  = int(os.getenv("REDFISH_PORT", "443"))
STORAGE_PORT  = int(os.getenv("STORAGE_PORT", "443"))
SWITCH_PORT   = int(os.getenv("SWITCH_PORT", "22"))
STORAGE_AUTH_HASH = os.getenv("STORAGE_AUTH_HASH", "sha256").lower()
SCAN_WORKERS  = int(os.getenv("SCAN_WORKERS", "20"))
SERVER_ROLE   = os.getenv("DEFAULT_ROLE_NAME", "Server")
STORAGE_ROLE  = os.getenv("DEFAULT_STORAGE_ROLE", "Storage")
SWITCH_ROLE   = os.getenv("DEFAULT_SWITCH_ROLE", "SAN Switch")
CISCO_PORT    = int(os.getenv("CISCO_PORT", "22"))
CISCO_ROLE    = os.getenv("DEFAULT_CISCO_ROLE", "Switch")
DEFAULT_MFR   = "HPE"
DEFAULT_SITE  = os.getenv("DEFAULT_SITE_NAME", "")
OFFLINE_THRESHOLD = int(os.getenv("OFFLINE_THRESHOLD", "2"))

# ── power monitoring ─────────────────────────────────────────────────────────
# Master switch for per-device power collection + per-rack utilization sync.
POWER_SYNC_ENABLED = _env_bool("POWER_SYNC_ENABLED", True)
# Feed is left with max_utilization% headroom when computing capacity.
POWER_DEFAULT_FEED_MAX_UTIL = int(os.getenv("POWER_FEED_MAX_UTIL", "80"))
# Typical draw (watts) by device model keyword -> watts. Longest keyword match
# wins; fall through to POWER_DEFAULT_WATTS when nothing matches. Edit freely.
_default_watts = {
    "dl380 gen10": 600, "dl380 gen9": 500, "dl380": 500,
    "dl360 gen10": 450, "dl360 gen9": 400, "dl360": 400,
    "dl380p": 500, "dl380e": 450,
    "c9500": 650, "c9300x": 450, "c9300": 350, "c9200l": 130, "c9200": 130,
    "ws-c2960x-48": 90, "ws-c2960x-24": 70, "2960x": 90, "2960": 70,
    "c3850": 350, "c4500x": 500, "3560": 100, "3750": 130, "sf500": 40,
    "fortigate 1800f": 250, "fortigate 200g": 120, "fortigate 200f": 120,
    "fortigate 100f": 60, "fortigate 60f": 25, "fg-1800f": 250, "fg-200g": 120,
    "fg-100f": 60, "fg-60f": 25, "fortiweb": 150, "fwb-1000": 150,
    "msa 2050": 300, "msa 2052": 300, "msa 2040": 300, "msa 2042": 300,
    "msa 2060": 350, "msa": 300, "d2700": 250, "d3610": 300,
    "sn3000b": 120, "sn3600b": 120, "brocade": 120, "san switch": 120,
    "nvr": 60, "ds-96": 60, "ds-76": 40, "dhi-nvr": 60, "nvr608": 60,
    "ccr1016": 60, "mikrotik": 40, "zd1200": 50, "zonedirector": 50,
    "patch panel": 0, "blank": 0, "cable mgmt": 0, "cm021": 0,
    "camera": 10, "access point": 20, "uap": 20, "r550": 15,
}
def _parse_power_watts(env_value):
    """POWER_DEFAULT_WATTS_JSON overrides the built-in table (JSON object of
    model-keyword -> watts). Keys are lowercased for matching."""
    import json as _json
    if env_value:
        try:
            return {str(k).lower(): int(v) for k, v in _json.loads(env_value).items()}
        except Exception as e:
            log("WARN", f"POWER_DEFAULT_WATTS_JSON invalid JSON ({e}) — using built-ins")
    return dict(_default_watts)
POWER_WATTS = _parse_power_watts(os.getenv("POWER_DEFAULT_WATTS_JSON"))
POWER_DEFAULT_WATTS = int(os.getenv("POWER_DEFAULT_WATTS", "100"))

def power_watts_for_model(model):
    """Typical draw (watts) for a device model. Longest keyword match wins so
    'c9300x' beats 'c9300'; falls back to POWER_DEFAULT_WATTS."""
    m = (model or "").lower()
    best, best_len = None, 0
    for kw, watts in POWER_WATTS.items():
        if kw in m and len(kw) > best_len:
            best, best_len = watts, len(kw)
    return best if best is not None else POWER_DEFAULT_WATTS


# ── site keyword mapping ─────────────────────────────────────────────────────
# Example mapping — replace with your own site keywords, or set SITE_KEYWORD_MAP
# in your .env as "keyword1:Site1,keyword2:Site2"
SITE_KEYWORD_MAP = [
    ("site1", "Site1"),
    ("site2", "Site2"),
]
if os.getenv("SITE_KEYWORD_MAP"):
    SITE_KEYWORD_MAP = [
        tuple(pair.split(":", 1))
        for pair in os.getenv("SITE_KEYWORD_MAP").split(",")
        if ":" in pair
    ]
SITE_UNKNOWN = DEFAULT_SITE or "Unknown"

# ── logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

def log(level, msg):
    if _LOG_LEVELS.get(level, 20) < _LOG_LEVELS.get(LOG_LEVEL, 20):
        return
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")

# ── site assignment by IP range ──────────────────────────────────────────────
def _parse_site_ip_map(env_value):
    """Parse "cidr:Site,cidr:Site2" into [(IPv4Network, site)] sorted by
    prefix length descending (most specific first; stable on ties).
    Malformed entries are skipped with a WARN."""
    pairs = [p.strip() for p in (env_value or "").split(",") if p.strip()]
    out = []
    for pair in pairs:
        if ":" not in pair:
            log("WARN", f"SITE_IP_MAP entry {pair!r} is not 'cidr:Site' — skipped")
            continue
        cidr, site = pair.split(":", 1)
        try:
            out.append((ipaddress.ip_network(cidr.strip(), strict=False),
                        site.strip()))
        except ValueError as exc:
            log("WARN", f"SITE_IP_MAP entry {pair!r} has invalid CIDR ({exc}) — skipped")
    out.sort(key=lambda t: -t[0].prefixlen)
    return out

SITE_IP_MAP = _parse_site_ip_map(os.getenv("SITE_IP_MAP"))
