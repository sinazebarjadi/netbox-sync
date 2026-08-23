"""Ruckus ZoneDirector (ZD) wireless controllers: interactive-shell session,
identity/AP/WLAN parsers, probe and collection."""
import re
import time

import paramiko

from netbox_sync.config import (RUCKUS_USER, RUCKUS_PASS, RUCKUS_PORT,
                                RUCKUS_HA_MAP, log)
from netbox_sync.utils import is_port_open
from netbox_sync.report import classify_error, record_probe_failure

# ── HA map ───────────────────────────────────────────────────────────────────

def _parse_ha_map(value):
    """Parse RUCKUS_HA_MAP: 'vip:primary,secondary;vip2:primary,secondary'
    -> {vip: {"primary": ip, "secondary": ip}}."""
    out = {}
    for pair in (value or "").split(";"):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        vip, units = pair.split(":", 1)
        parts = [u.strip() for u in units.split(",") if u.strip()]
        if len(parts) >= 2:
            out[vip.strip()] = {"primary": parts[0], "secondary": parts[1]}
    return out

# ── interactive shell session ────────────────────────────────────────────────

class RuckusSession:
    """Paramiko interactive shell for the ZoneDirector CLI: two-step login
    ('Please login:'/'Password:') then 'enable' for privileged mode."""

    def __init__(self, ip, timeout=20):
        self.ip = ip
        self.timeout = timeout
        self.client = None
        self.chan = None

    def login(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(hostname=self.ip, port=RUCKUS_PORT,
                            username=RUCKUS_USER, password=RUCKUS_PASS,
                            timeout=self.timeout, allow_agent=False,
                            look_for_keys=False)
        self.chan = self.client.invoke_shell()
        self.chan.settimeout(self.timeout)
        self._until("login:")
        self.chan.send(RUCKUS_USER + "\n")
        self._until("Password:")
        self.chan.send(RUCKUS_PASS + "\n")
        self._read_idle()
        self.chan.send("enable\n")
        self._until("#")

    def _until(self, marker, total=None):
        buf = b""
        start = time.time()
        total = total or self.timeout
        while time.time() - start < total:
            if self.chan.recv_ready():
                data = self.chan.recv(65535)
                if not data:
                    break
                buf += data
                if marker in buf.decode(errors="ignore"):
                    return
            else:
                time.sleep(0.1)

    def _read_idle(self, idle=2.0):
        buf = b""
        last = time.time()
        start = time.time()
        total = max(self.timeout * 4, idle + 5)
        while time.time() - start < total:
            if self.chan.recv_ready():
                data = self.chan.recv(65535)
                if not data:
                    break
                buf += data
                last = time.time()
            else:
                if time.time() - last > idle:
                    break
                time.sleep(0.1)
        return buf.decode(errors="ignore")

    def run(self, command):
        if not self.chan:
            raise RuntimeError("shell not open")
        self.chan.send(command + "\n")
        out = self._read_idle().replace("\r", "")
        lines = []
        for ln in out.splitlines():
            s = ln.strip()
            if not s or s == command.strip() or s.startswith("ruckus"):
                continue
            lines.append(ln.rstrip())
        return "\n".join(lines)

    def logout(self):
        try:
            if self.chan:
                self.chan.close()
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.chan = self.client = None

# ── parsers ──────────────────────────────────────────────────────────────────

def _parse_sysinfo(text):
    """Parse `show sysinfo` System Overview block."""
    out = {"name": None, "ip": None, "mac": None, "model": None,
           "serial": None, "version": None}
    keys = {"name": "name", "ip address": "ip", "mac address": "mac",
            "model": "model", "serial number": "serial", "version": "version"}
    for line in text.splitlines():
        m = re.match(r'^\s*([^=]+?)\s*=\s*(.*)$', line)
        if not m:
            continue
        k = keys.get(m.group(1).strip().lower())
        if k and not out[k]:
            out[k] = m.group(2).strip()
    return out


def _parse_ap_all(text):
    """Parse `show ap all` ID blocks -> per-AP dicts (MAC is the identity)."""
    aps = []
    cur = None
    for line in text.splitlines():
        if re.match(r'^\s+\d+:\s*$', line):
            if cur and cur.get("mac"):
                aps.append(cur)
            cur = {}
            continue
        if cur is None:
            continue
        m = re.match(r'^\s*([^=]+?)\s*=\s*(.*)$', line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "mac address":
            cur["mac"] = val
        elif key == "model":
            cur["model"] = val
        elif key == "device name":
            cur["name"] = val
        elif key == "group name":
            cur["group"] = val
        elif key == "ip address" and "ip" not in cur:
            cur["ip"] = val
        elif key == "approved":
            cur["approved"] = val.lower() == "yes"
    if cur and cur.get("mac"):
        aps.append(cur)
    for ap in aps:
        ap.setdefault("name", ap["mac"])
        ap.setdefault("approved", False)
        ap.setdefault("group", "")
        ap.setdefault("model", "")
        ap.setdefault("ip", None)
    return aps


def probe_ruckus(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, RUCKUS_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Ruckus", ip, "unreachable",
                                 f"port {RUCKUS_PORT} closed or timed out")
            return None
        sess = RuckusSession(ip)
        try:
            sess.login()
            info = _parse_sysinfo(sess.run("show sysinfo"))
            if not (info.get("serial") or info.get("model")):
                raise RuntimeError("sysinfo yielded no serial/model")
            return {
                "ip": ip,
                "host": f"{ip}:{RUCKUS_PORT}",
                "serial": info.get("serial"),
                "model": info.get("model"),
                "hostname": info.get("name") or f"ruckus-{ip.replace('.', '-')}",
                "reported_ip": info.get("ip"),
                "mac": info.get("mac"),
                "manufacturer": "Ruckus",
                "firmware": info.get("version"),
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None


def _parse_wlan_all(text):
    """Parse `show wlan all` ID blocks -> WLANs (ssid/auth/encryption/vlan).
    Passphrases are deliberately never captured."""
    wlans = []
    cur = None
    for line in text.splitlines():
        if re.match(r'^\s+\d+:\s*$', line):
            if cur and (cur.get("name") or cur.get("ssid")):
                wlans.append(cur)
            cur = {}
            continue
        if cur is None:
            continue
        m = re.match(r'^\s*([^=]+?)\s*=\s*(.*)$', line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "name":
            cur["name"] = val
        elif key == "ssid":
            cur["ssid"] = val
        elif key == "authentication":
            cur["auth"] = val
        elif key == "encryption":
            cur["encryption"] = val
        elif key == "vlan-id":
            try:
                cur["vlan_id"] = int(val)
            except ValueError:
                pass
    if cur and (cur.get("name") or cur.get("ssid")):
        wlans.append(cur)
    for w in wlans:
        w.setdefault("vlan_id", None)
        w.setdefault("auth", "")
        w.setdefault("encryption", "")
    return wlans


def _ruckus_role_and_cluster(ip, ha_map):
    """Classify a probed address: VIP (cluster), primary/secondary unit of a
    VIP pair, or standalone. Returns (role, vip_or_None)."""
    if ip in ha_map:
        return "vip", ip
    for vip, units in ha_map.items():
        if ip == units.get("primary"):
            return "primary", vip
        if ip == units.get("secondary"):
            return "secondary", vip
    return "standalone", None


def ruckus_collect(ip):
    """Full collection: sysinfo identity + AP list + WLAN list."""
    sess = RuckusSession(ip)
    sess.login()
    try:
        info = _parse_sysinfo(sess.run("show sysinfo"))
        aps = _parse_ap_all(sess.run("show ap all"))
        wlans = _parse_wlan_all(sess.run("show wlan all"))
        log("INFO", f"  ruckus: {len(aps)} APs, {len(wlans)} WLANs")
        return {
            "summary": {
                "name": info.get("name"), "reported_ip": info.get("ip"),
                "mac": info.get("mac"), "model": info.get("model"),
                "serial": info.get("serial"), "version": info.get("version"),
            },
            "aps": aps,
            "wlans": wlans,
        }
    finally:
        sess.logout()
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, RUCKUS_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Ruckus", ip, "unreachable",
                                 f"port {RUCKUS_PORT} closed or timed out")
            return None
        sess = RuckusSession(ip)
        try:
            sess.login()
            info = _parse_sysinfo(sess.run("show sysinfo"))
            if not (info.get("serial") or info.get("model")):
                raise RuntimeError("sysinfo yielded no serial/model")
            return {
                "ip": ip,
                "host": f"{ip}:{RUCKUS_PORT}",
                "serial": info.get("serial"),
                "model": info.get("model"),
                "hostname": info.get("name") or f"ruckus-{ip.replace('.', '-')}",
                "reported_ip": info.get("ip"),
                "mac": info.get("mac"),
                "manufacturer": "Ruckus",
                "firmware": info.get("version"),
            }
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Ruckus", ip, "no data", classify_error(exc))
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None
