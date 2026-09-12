"""FortiWeb WAF appliances: header-token REST API session, identity/interface
parsers, probe and collection.

FortiWeb has no dedicated API users (unlike FortiGate). Every request carries
an `Authorization` header = Base64 of {"username","password","vdom":"root"}.
Stateless — no login, no cookie, no token endpoint. Verified live against
FortiWeb 1000E/1000F 7.2.12 with a readonly admin account on port 58291.
"""
import base64
import json
import time

import requests
import urllib3

from netbox_sync.config import (FORTIWEB_USER, FORTIWEB_PASS, FORTIWEB_PORT,
                                log)
from netbox_sync.utils import is_port_open

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FortiWebAuthError(RuntimeError):
    """Authorization header rejected — credentials wrong."""


def _auth_header(user, pwd, vdom="root"):
    return base64.b64encode(json.dumps(
        {"username": user, "password": pwd, "vdom": vdom}).encode()).decode()


class FortiWebSession:
    """FortiWeb /api/v2.0 client. get(path) returns the envelope's 'results'
    (dict or list) and raises on transport errors, non-2xx, or errcode."""

    def __init__(self, ip, port=None, timeout=20):
        self.base = f"https://{ip}:{port or FORTIWEB_PORT}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({
            "Authorization": _auth_header(FORTIWEB_USER, FORTIWEB_PASS),
            "Content-Type": "application/json",
        })

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        if r.status_code in (401, 403):
            raise FortiWebAuthError(
                f"FortiWeb auth rejected ({r.status_code}) — "
                "check FORTIWEB_USER/FORTIWEB_PASS")
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and body.get("errcode") not in (None, 0):
            raise RuntimeError(f"FortiWeb {path} error: "
                               f"{body.get('message') or body.get('errcode')}")
        return body.get("results") if isinstance(body, dict) else body

# ── parsers ──────────────────────────────────────────────────────────────────

def _parse_system_status(results):
    """status.systemstatus results -> identity. firmwareVersion looks like
    'FortiWeb-1000E 7.2.12,build0437(GA),251023' -> model + version split.
    NOTE: hostName here is unreliable (None on some boxes / HA members) — the
    real hostname lives in cmdb/system/global; see fortiweb_collect."""
    fw = (results.get("firmwareVersion") or "").strip()
    model = version = None
    if fw:
        # "FortiWeb-1000E 7.2.12,build..." -> ("FortiWeb 1000E", "7.2.12")
        head, _, rest = fw.partition(" ")
        model = head.replace("FortiWeb-", "FortiWeb ").strip() or None
        version = (rest.split(",", 1)[0].strip() or None) if rest else None
    return {
        "name":     (results.get("hostName") or "").strip() or None,
        "serial":   (results.get("serialNumber") or "").strip() or None,
        "model":    model,
        "version":  version,
        "op_mode":  (results.get("operationMode") or "").strip() or None,
        "ha_status": (results.get("haStatus") or "").strip() or None,
        "firmware_raw": fw or None,
    }


def _parse_global_hostname(results):
    """cmdb/system/global results -> hostname (the authoritative source)."""
    return (results.get("hostname") or "").strip() or None


def _parse_cluster_members(results):
    """status.systemstatus 'cluster_members' -> [{hostname, serial, role}].
    Empty list for Standalone / non-clustered. role normalized to
    'primary'/'secondary'."""
    members = []
    for m in (results.get("cluster_members") or []):
        if not isinstance(m, dict):
            continue
        serial = (m.get("dev_sn") or "").strip()
        if not serial:
            continue
        role = (m.get("role") or "").strip().lower()
        members.append({
            "hostname": (m.get("hostname") or "").strip() or None,
            "serial": serial,
            "role": "primary" if role == "primary" else "secondary",
        })
    return members


def _parse_interfaces(results):
    """cmdb system/interface results -> {port_count}. Physical ports only;
    mgmt IPs are factory defaults here, so the real mgmt IP is the probed one."""
    ports = [i for i in (results or [])
             if isinstance(i, dict) and i.get("type") == "physical"]
    return {"port_count": len(ports)}

# ── probe & collect ──────────────────────────────────────────────────────────

def probe_fortiweb(ip, retries=2, retry_delay=3):
    """Identify a FortiWeb: TCP open + status.systemstatus yields a serial."""
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, FORTIWEB_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        sess = FortiWebSession(ip)
        try:
            info = _parse_system_status(
                sess.get("/api/v2.0/system/status.systemstatus"))
            if not (info.get("serial") or info.get("model")):
                raise RuntimeError("systemstatus yielded no serial/model")
            return {
                "ip": ip,
                "host": f"{ip}:{FORTIWEB_PORT}",
                "serial": info.get("serial"),
                "model": info.get("model"),
                "hostname": info.get("name") or f"fortiweb-{ip.replace('.', '-')}",
                "reported_ip": ip,
                "mac": None,
                "manufacturer": "Fortinet",
                "firmware": info.get("version"),
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None


def fortiweb_collect(ip):
    """Full collection: identity + operation/HA mode + resource snapshot +
    interface/port count. Hostname comes from cmdb/system/global (authoritative);
    status.systemstatus's hostName is None on some boxes / HA members."""
    sess = FortiWebSession(ip)
    raw_status = sess.get("/api/v2.0/system/status.systemstatus")
    status = _parse_system_status(raw_status)
    members = _parse_cluster_members(raw_status)
    ha_group = (raw_status.get("cluster") or "").strip() or None \
        if isinstance(raw_status, dict) else None
    try:
        gh = _parse_global_hostname(sess.get("/api/v2.0/cmdb/system/global"))
        if gh:
            status["name"] = gh
    except Exception as e:
        log("WARN", f"  fortiweb {ip}: system/global hostname failed: {e}")
    try:
        resources = sess.get("/api/v2.0/system/status.systemresource")
    except Exception as e:
        resources = {}
        log("WARN", f"  fortiweb {ip}: systemresource failed: {e}")
    try:
        ifaces = _parse_interfaces(
            sess.get("/api/v2.0/cmdb/system/interface"))
    except Exception as e:
        ifaces = {"port_count": None}
        log("WARN", f"  fortiweb {ip}: interfaces failed: {e}")
    return {
        "summary": status,
        "resources": resources,
        "interfaces": ifaces,
        "ha_members": members,     # [{hostname, serial, role}] — empty if Standalone
        "ha_group": ha_group,      # cluster name, e.g. "WAF-HA"
    }
