"""Dahua NVRs: HTTP digest CGI API — session, key=value table parsers,
probing and camera collection.

Verified live against a Dahua NVR6XX-4KS2 (see spec). Camera data comes from
the `RemoteDevice` config table: slot index N maps 1:1 to channel N+1. The
table's `Mac` field is unreliable for ONVIF-registered cameras (empty or
ff:ff:ff:ff:ff:ff) and is dropped in that case. Channel online state is not
readable with a non-admin-rights account (403), so `online` mirrors the
registration `Enable` flag; offline detection stays sweep-based."""
import re
import time

import requests
from requests.auth import HTTPDigestAuth

from netbox_sync.config import DAHUA_USER, DAHUA_PASS, DAHUA_PORT, log
from netbox_sync.utils import is_port_open
from netbox_sync.report import classify_error, record_probe_failure

# ── session ──────────────────────────────────────────────────────────────────

class DahuaSession:
    """Thin wrapper over requests with digest auth; get() returns raw text."""

    def __init__(self, ip, port=None, timeout=15):
        self.base = f"http://{ip}:{port or DAHUA_PORT}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.auth = HTTPDigestAuth(DAHUA_USER, DAHUA_PASS)
        self.s.verify = False

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def logout(self):
        try:
            self.s.close()
        except Exception:
            pass

# ── parsers (key=value text) ─────────────────────────────────────────────────

def _kv(text):
    """Parse 'key=value' lines into a dict (first occurrence wins)."""
    out = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k not in out:
            out[k.strip()] = v.strip()
    return out

def _parse_system_info(text):
    """magicBox getSystemInfo -> {serial, model, device_type}. The model
    series lives in updateSerial (e.g. NVR6XX-4KS2)."""
    kv = _kv(text)
    return {
        "serial": kv.get("serialNumber"),
        "model": kv.get("updateSerial") or kv.get("deviceType"),
        "device_type": kv.get("deviceType"),
    }

def _parse_machine_name(text):
    """magicBox getMachineName -> name or None when generic ('NVR')."""
    kv = _kv(text)
    name = kv.get("name") or ""
    return None if name.strip().lower() in ("", "nvr") else name.strip()

def _parse_software_version(text):
    """magicBox getSoftwareVersion -> firmware ('4.002....,build:...' -> the
    version part only)."""
    kv = _kv(text)
    return (kv.get("version") or "").split(",")[0].strip() or None

def _parse_device_class(text):
    """magicBox getDeviceClass -> class string ('NVR')."""
    return _kv(text).get("class")

def _parse_channel_titles(text):
    """ChannelTitle config -> {channel_int: name}; table index n == channel
    n+1 on Dahua NVRs."""
    titles = {}
    for m in re.finditer(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)", text or ""):
        titles[int(m.group(1)) + 1] = m.group(2).strip()
    return titles

def _norm_mac(raw):
    """Normalize to lowercase colon form; None when empty, invalid, or the
    all-ff placeholder Dahua returns for ONVIF cameras."""
    s = re.sub(r"[^0-9a-fA-F]", "", raw or "").lower()
    if len(s) != 12 or s == "ffffffffffff":
        return None
    return ":".join(s[i:i+2] for i in range(0, 12, 2))

def _guess_camera_manufacturer(model):
    """Dahua NVRs here register Hikvision IPCs via ONVIF; model prefixes tell
    the true maker (DS-2... = Hikvision)."""
    if (model or "").startswith("DS-2"):
        return "Hikvision"
    return "Dahua"

def _parse_remote_devices(text):
    """RemoteDevice config table -> normalized camera dicts. Slot index maps
    1:1 to channel (slot 0 == channel 1)."""
    slots = {}
    for m in re.finditer(
            r"table\.RemoteDevice\.uuid:\S+?(\d+)\.([A-Za-z0-9_]+)=(.*)",
            text or ""):
        slot, key, val = int(m.group(1)), m.group(2), m.group(3).strip()
        slots.setdefault(slot, {})[key] = val
    cams = []
    for slot in sorted(slots):
        e = slots[slot]
        if not e.get("Address"):
            continue
        model = e.get("DeviceType") or None
        cams.append({
            "channel":      slot + 1,
            "name":         None,   # filled from ChannelTitle by the collector
            "ip":           e.get("Address"),
            "model":        model,
            "serial":       e.get("SerialNo") or None,
            "firmware":     e.get("Version") or None,
            "mac":          _norm_mac(e.get("Mac")),
            "online":       (e.get("Enable") or "").strip().lower() == "true",
            "manufacturer": _guess_camera_manufacturer(model),
        })
    return cams

def _try(sess, path, parser):
    """Best-effort GET+parse: returns None on any failure. Some accounts lack
    rights for individual magicBox actions (403 Authority:check failure) — a
    missing cosmetic field must never sink the probe/collect."""
    try:
        return parser(sess.get(path))
    except Exception as exc:
        log("DEBUG", f"  dahua {path} failed: {exc}")
        return None

# ── probe + collect ──────────────────────────────────────────────────────────

def probe_dahua(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, DAHUA_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Dahua NVR", ip, "unreachable",
                                 f"port {DAHUA_PORT} closed or timed out")
            return None
        sess = DahuaSession(ip)
        try:
            info = _parse_system_info(
                sess.get("/cgi-bin/magicBox.cgi?action=getSystemInfo"))
            dev_class = _parse_device_class(
                sess.get("/cgi-bin/magicBox.cgi?action=getDeviceClass"))
            if not info.get("serial") or (dev_class or "").upper() != "NVR":
                raise RuntimeError("not a Dahua NVR (no serial/class)")
            name = _try(sess, "/cgi-bin/magicBox.cgi?action=getMachineName",
                        _parse_machine_name)
            return {
                "ip":           ip,
                "host":         f"{ip}:{DAHUA_PORT}",
                "serial":       info.get("serial"),
                "model":        info.get("model"),
                "hostname":     name or f"dahua-{ip.replace('.', '-')}",
                "reported_ip":  ip,
                "mac":          None,
                "manufacturer": "Dahua",
                "firmware":     _try(sess,
                                     "/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
                                     _parse_software_version),
            }
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Dahua NVR", ip, "no data", classify_error(exc))
            return None
        finally:
            sess.logout()
    return None

def dahua_collect(ip):
    """Full collection: NVR identity + camera list (channel/IP/model/serial/
    firmware; MAC only when the table carries a real one)."""
    sess = DahuaSession(ip)
    try:
        info = _parse_system_info(
            sess.get("/cgi-bin/magicBox.cgi?action=getSystemInfo"))
        name = _try(sess, "/cgi-bin/magicBox.cgi?action=getMachineName",
                    _parse_machine_name)
        firmware = _try(sess,
                        "/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
                        _parse_software_version)
        remote = _parse_remote_devices(
            sess.get("/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"))
        try:
            titles = _parse_channel_titles(
                sess.get("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"))
        except Exception as exc:
            titles = {}
            log("WARN", f"  dahua {ip}: ChannelTitle failed: {exc}")
        for cam in remote:
            title = titles.get(cam["channel"])
            cam["name"] = title or f"dahua-cam-ch{cam['channel']}"
        log("INFO", f"  dahua: {len(remote)} cameras "
                    f"({sum(1 for c in remote if c.get('mac'))} with MAC)")
        return {
            "summary": {
                "name":     name or f"dahua-{ip.replace('.', '-')}",
                "model":    info.get("model"),
                "serial":   info.get("serial"),
                "firmware": firmware,
            },
            "cameras": remote,
        }
    finally:
        sess.logout()
