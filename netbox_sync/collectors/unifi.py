"""UniFi OS consoles (UDM/CloudKey/UniFi OS Server, Network Application):
session-based login via the legacy /api/login endpoint, site/device/WLAN/
network parsers, probe and collection."""
import time

import requests
import urllib3

from netbox_sync.config import (UNIFI_USER, UNIFI_PASS, UNIFI_PORT, log)
from netbox_sync.utils import is_port_open
from netbox_sync.report import classify_error, record_probe_failure

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class UniFiSession:
    """Session-cookie client for a UniFi OS console. Auth is the legacy
    internal API flow: POST /api/login {username, password} -> 'unifises'
    cookie. (The newer /api/auth/login 401s on consoles like UniFi OS 10.2
    when the account is a dedicated local admin; the legacy endpoint is the
    de-facto standard for automation.)"""

    def __init__(self, ip, port=UNIFI_PORT, timeout=20):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base = f"https://{ip}:{port}"
        self.session = requests.Session()
        self.session.verify = False

    def login(self):
        r = self.session.post(
            f"{self.base}/api/login",
            json={"username": UNIFI_USER, "password": UNIFI_PASS,
                  "remember": True},
            timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"UniFi login failed: HTTP {r.status_code}")
        meta = (r.json().get("meta") or {})
        if meta.get("rc") != "ok":
            raise RuntimeError(f"UniFi login failed: {meta.get('msg')}")
        csrf = r.headers.get("x-csrf-token")
        if csrf:
            self.session.headers.update({"X-CSRF-Token": csrf})

    def get(self, path):
        """GET an API path; returns the envelope's 'data' list."""
        r = self.session.get(f"{self.base}{path}", timeout=self.timeout)
        if r.status_code == 401:   # expired session -> single re-login retry
            self.login()
            r = self.session.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if (body.get("meta") or {}).get("rc") != "ok":
            raise RuntimeError(f"UniFi GET {path} failed: "
                               f"{(body.get('meta') or {}).get('msg')}")
        return body.get("data") or []

    def logout(self):
        try:
            self.session.post(f"{self.base}/api/logout", timeout=5)
        except Exception:
            pass

# ── parsers ──────────────────────────────────────────────────────────────────

def _parse_sites(envelope):
    """`GET /api/self/sites` -> [{name, desc}] in console order."""
    return [{"name": s.get("name"), "desc": s.get("desc") or s.get("name")}
            for s in (envelope.get("data") or [])
            if s.get("name")]


def _parse_devices(envelope):
    """`GET /api/s/<site>/stat/device` -> AP dicts shaped for
    netbox.ensure_ap_device (MAC identity). Only type 'uap' (access points);
    switches/gateways (usw/ugw) are out of scope for v1. The UniFi 'serial'
    field is the MAC without separators, so the MAC stays the identity."""
    aps = []
    for d in (envelope.get("data") or []):
        if d.get("type") != "uap":
            continue
        mac = (d.get("mac") or "").lower()
        if not mac:
            continue
        aps.append({
            "mac": mac,
            "model": d.get("model") or "",
            "name": d.get("name") or mac,
            "group": None,          # filled from the UniFi site desc by caller
            "ip": d.get("ip"),
            "approved": bool(d.get("adopted", True)),
            "firmware": d.get("version"),
            "state": d.get("state"),
        })
    return aps


def _parse_wlans(envelope):
    """`GET /api/s/<site>/rest/wlanconf` -> WLAN dicts (ssid/security/VLAN
    binding). Passphrases are never present in this read-only view. 'auth' is
    normalized to the shared sync_wireless_lans vocabulary (open/wpa2/802.1x)
    and 'name' mirrors the SSID for marker descriptions."""
    wlans = []
    for w in (envelope.get("data") or []):
        ssid = w.get("name")
        if not ssid:
            continue
        sec = (w.get("security") or "").lower()
        if sec in ("", "open"):
            auth = "open"
        elif "8021x" in sec or "dot1x" in sec:
            auth = "802.1x"
        else:                      # wpa / wpa2 / wpa3 personal
            auth = "wpa2"
        wlans.append({
            "ssid": ssid,
            "name": ssid,
            "auth": auth,
            "security": w.get("security") or "",
            "wpa_mode": w.get("wpa_mode") or "",
            "wpa_enc": w.get("wpa_enc") or "",
            "enabled": bool(w.get("enabled", True)),
            "hide_ssid": bool(w.get("hide_ssid", False)),
            "is_guest": bool(w.get("is_guest", False)),
            "networkconf_id": w.get("networkconf_id"),
        })
    return wlans


def _parse_networks(envelope):
    """`GET /api/s/<site>/rest/networkconf` -> {networkconf_id: vlan_id}
    (VLAN-tagged networks only; untagged networks map to None and are
    dropped)."""
    out = {}
    for n in (envelope.get("data") or []):
        nid, vlan = n.get("_id"), n.get("vlan")
        if nid and isinstance(vlan, int):
            out[nid] = vlan
    return out

# ── probe & collect ──────────────────────────────────────────────────────────

def _status(ip, port, timeout=10):
    """Unauthenticated GET /status -> {up, server_version, uuid} or None."""
    try:
        r = requests.get(f"https://{ip}:{port}/status",
                         verify=False, timeout=timeout)
        if r.status_code != 200:
            return None
        meta = r.json().get("meta") or {}
        if not meta.get("up"):
            return None
        return {"server_version": meta.get("server_version"),
                "uuid": meta.get("uuid")}
    except Exception:
        return None


def probe_unifi(ip, retries=2, retry_delay=3):
    """Identify a UniFi OS console: TCP reachable + /status answers 'up' +
    login succeeds. Returns the standard probe dict or None."""
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, UNIFI_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("UniFi console", ip, "unreachable",
                                 f"port {UNIFI_PORT} closed or timed out")
            return None
        st = _status(ip, UNIFI_PORT)
        if not st:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("UniFi console", ip, "no data",
                                 "status API did not report a running console")
            return None
        sess = UniFiSession(ip)
        try:
            try:
                sess.login()
            except RuntimeError as exc:     # auth/api rejection — not transient
                log("WARN", f"  {exc} — skipping {ip}")
                record_probe_failure("UniFi console", ip, "no data",
                                     classify_error(exc))
                return None
            hostname = f"unifi-{ip.replace('.', '-')}"
            try:
                for row in sess.get("/api/system"):
                    if row.get("hostname"):
                        hostname = row["hostname"]
                        break
            except Exception:
                pass
            return {
                "ip": ip,
                "host": f"{ip}:{UNIFI_PORT}",
                "serial": st.get("uuid"),
                "model": "UniFi OS Console",
                "hostname": hostname,
                "reported_ip": ip,
                "mac": None,
                "manufacturer": "Ubiquiti",
                "firmware": st.get("server_version"),
            }
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("UniFi console", ip, "no data", classify_error(exc))
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None


def unifi_collect(ip):
    """Full collection across every site on the console:
    {summary, sites, aps: [{..., group: site-desc, site_name}],
     wlans: {site_name: [wlan]}, networks: {site_name: {net_id: vlan}}}."""
    sess = UniFiSession(ip)
    sess.login()
    try:
        st = _status(ip, UNIFI_PORT) or {}
        hostname = f"unifi-{ip.replace('.', '-')}"
        try:
            for row in sess.get("/api/system"):
                if row.get("hostname"):
                    hostname = row["hostname"]
                    break
        except Exception:
            pass
        sites = _parse_sites({"data": sess.get("/api/self/sites")})
        aps, wlans, networks = [], {}, {}
        for site in sites:
            sname, sdesc = site["name"], site["desc"]
            try:
                dev = sess.get(f"/api/s/{sname}/stat/device")
                for ap in _parse_devices({"data": dev}):
                    ap["group"] = sdesc
                    ap["site_name"] = sname
                    aps.append(ap)
            except Exception as e:
                log("WARN", f"  unifi {ip} site {sname}: devices failed: {e}")
            try:
                wlans[sname] = _parse_wlans(
                    {"data": sess.get(f"/api/s/{sname}/rest/wlanconf")})
            except Exception as e:
                wlans[sname] = []
                log("WARN", f"  unifi {ip} site {sname}: wlans failed: {e}")
            try:
                networks[sname] = _parse_networks(
                    {"data": sess.get(f"/api/s/{sname}/rest/networkconf")})
            except Exception as e:
                networks[sname] = {}
                log("WARN", f"  unifi {ip} site {sname}: networks failed: {e}")
        log("INFO", f"  unifi {ip}: {len(sites)} sites, {len(aps)} APs, "
                    f"{sum(len(w) for w in wlans.values())} WLANs")
        return {
            "summary": {
                "name": hostname,
                "version": st.get("server_version"),
                "uuid": st.get("uuid"),
                "reported_ip": ip,
            },
            "sites": sites,
            "aps": aps,
            "wlans": wlans,
            "networks": networks,
        }
    finally:
        sess.logout()
