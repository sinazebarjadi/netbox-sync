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
