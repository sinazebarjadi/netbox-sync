"""Cisco FTD (Firepower Threat Defense) via the FMC (Firepower Management
Center) REST API: token-auth session, device-record enumeration, probe and
collection.

The FMC is only the management plane — the FTD devices are the inventory
targets. Auth is token-based: POST /api/fmc_platform/v1/auth/generatetoken
with HTTP Basic auth -> X-auth-access-token header, used for all later calls.
Verified live against FMC 7.x with a readonly admin account.
"""
import json
import time

import requests
import urllib3

from netbox_sync.config import (FMC_USER, FMC_PASS, FMC_PORT, log)
from netbox_sync.utils import is_port_open

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FMCAuthError(RuntimeError):
    """FMC token generation rejected — credentials wrong."""


class FMCSession:
    """FMC REST client. Generates an access token on init (Basic auth ->
    X-auth-access-token), re-generates on 401. get(path) returns parsed JSON;
    get_items(path) follows 'items' paging and returns the full list."""

    def __init__(self, ip, port=None, timeout=25):
        self.base = f"https://{ip}:{port or FMC_PORT}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = False
        self.domain_uuid = None
        self.domain_name = None
        self._login()

    def _login(self):
        r = self.s.post(f"{self.base}/api/fmc_platform/v1/auth/generatetoken",
                        auth=(FMC_USER, FMC_PASS), timeout=self.timeout)
        if r.status_code in (401, 403):
            raise FMCAuthError(
                f"FMC auth rejected ({r.status_code}) — check FMC_USER/FMC_PASS")
        r.raise_for_status()
        token = r.headers.get("X-auth-access-token")
        if not token:
            raise FMCAuthError("FMC did not return X-auth-access-token")
        self.s.headers.update({"X-auth-access-token": token})
        try:
            domains = json.loads(r.headers.get("DOMAINS") or "[]")
            if domains:
                self.domain_uuid = domains[0].get("uuid")
                self.domain_name = domains[0].get("name")
        except (ValueError, IndexError):
            pass

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        if r.status_code == 401:
            self._login()
            r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
            if r.status_code == 401:
                raise FMCAuthError("FMC token rejected — check FMC_USER/FMC_PASS")
        r.raise_for_status()
        return r.json()

    def get_items(self, path, limit=100):
        """Follow FMC 'items' paging -> full list."""
        items, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            body = self.get(f"{path}{sep}limit={limit}&offset={offset}")
            batch = body.get("items") or []
            items.extend(batch)
            paging = body.get("paging") or {}
            if len(items) >= paging.get("count", len(items)) or not batch:
                break
            offset += len(batch)
        return items

# ── parsers ──────────────────────────────────────────────────────────────────

def _parse_device_detail(rec):
    """A devicerecords detail record -> FTD dict. The chassis serial lives in
    metadata.deviceSerialNumber; hostName is the FTD's management IP."""
    md = rec.get("metadata") or {}
    grp = rec.get("deviceGroup") or {}
    return {
        "name":      (rec.get("name") or "").strip() or None,
        "model":     (rec.get("model") or "").strip() or None,
        "serial":    (md.get("deviceSerialNumber") or "").strip() or None,
        "mgmt_ip":   (rec.get("hostName") or "").strip() or None,
        "sw_version": (rec.get("sw_version") or "").strip() or None,
        "health":    (rec.get("healthStatus") or "").strip() or None,
        "mode":      (rec.get("ftdMode") or "").strip() or None,
        "group":     (grp.get("name") or "").strip() or None,
        "connected": bool(rec.get("isConnected", True)),
    }

# ── probe & collect ──────────────────────────────────────────────────────────

def probe_ftd(ip, retries=2, retry_delay=3):
    """Identify a reachable FMC (token generation succeeds + domain uuid).
    The FMC itself is NOT a device target — this only gates FTD enumeration."""
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, FMC_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        try:
            sess = FMCSession(ip)
            if not sess.domain_uuid:
                raise RuntimeError("no domain uuid from FMC")
            return {
                "ip": ip,
                "host": f"{ip}:{FMC_PORT}",
                "serial": None,
                "model": "Cisco FMC",
                "hostname": f"fmc-{ip.replace('.', '-')}",
                "reported_ip": ip,
                "mac": None,
                "manufacturer": "Cisco",
                "firmware": None,
                "_is_fmc": True,
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None


def ftd_collect(ip):
    """Enumerate all FTD device records managed by the FMC at `ip`.
    Returns {"ftds": [...], "fmc_domain": name}."""
    sess = FMCSession(ip)
    if not sess.domain_uuid:
        raise RuntimeError("FMC returned no domain uuid")
    base = f"/api/fmc_config/v1/domain/{sess.domain_uuid}/devices/devicerecords"
    ftds = []
    for rec in sess.get_items(base):
        did = rec.get("id")
        if not did:
            continue
        try:
            detail = sess.get(f"{base}/{did}")
            ftd = _parse_device_detail(detail)
            if ftd["name"] or ftd["serial"]:
                ftds.append(ftd)
        except Exception as e:
            log("WARN", f"  ftd {ip}: device {did} detail failed: {e}")
    log("INFO", f"  ftd {ip}: {len(ftds)} FTDs via FMC ({sess.domain_name})")
    return {"ftds": ftds, "fmc_domain": sess.domain_name, "fmc_ip": ip}
