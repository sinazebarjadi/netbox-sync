"""Uniview (UNV) NVRs: HTTP digest LAPI (JSON envelopes) — session, parsers,
probing and camera collection.

Verified live against an NVR302-16S2 (see spec). Identity comes from
/LAPI/V1.0/System/DeviceInfo (the bare path — /BasicInfo etc. don't exist on
this firmware). Per-channel data merges ChannelDetailInfos (name, online
status, manufacturer, IP, MAC) with DeviceInfos (serial, firmware). Unknown
paths are answered with HTTP 599 (ResponseCode 1) or a truncated chunked 404 —
both surface as request errors and are treated as "not supported"."""
import json
import re
import time

import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import ChunkedEncodingError

from netbox_sync.config import UNV_USER, UNV_PASS, UNV_PORT, log
from netbox_sync.utils import is_port_open
from netbox_sync.report import classify_error, record_probe_failure

# ── session ──────────────────────────────────────────────────────────────────

class UnvSession:
    """Digest-auth LAPI client. get() returns the envelope's Data object and
    raises on non-zero ResponseCode or transport errors."""

    def __init__(self, ip, port=None, timeout=15):
        self.base = f"http://{ip}:{port or UNV_PORT}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.auth = HTTPDigestAuth(UNV_USER, UNV_PASS)
        self.s.verify = False
        self.s.headers.update({"Accept": "application/json"})

    def get(self, path):
        try:
            r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        except ChunkedEncodingError as exc:
            # UNV 404s arrive as truncated chunked bodies
            raise RuntimeError(f"LAPI read failed for {path}: {exc}") from exc
        body = r.json()
        resp = body.get("Response") or {}
        if resp.get("ResponseCode") != 0:
            raise RuntimeError(
                f"LAPI {path} failed: {resp.get('ResponseString')} "
                f"(rc={resp.get('ResponseCode')}, sc={resp.get('StatusCode')})")
        return resp.get("Data") or {}

    def logout(self):
        try:
            self.s.close()
        except Exception:
            pass

# ── parsers (JSON envelope dicts) ────────────────────────────────────────────

def _lapi_data(text):
    """Parse an envelope body (str) -> Data; raises on non-success rc."""
    body = json.loads(text)
    resp = body.get("Response") or {}
    if resp.get("ResponseCode") != 0:
        raise RuntimeError(f"LAPI error: {resp.get('ResponseString')}")
    return resp.get("Data") or {}

def _parse_device_info(text):
    """/LAPI/V1.0/System/DeviceInfo -> NVR identity."""
    data = _lapi_data(text) if isinstance(text, str) else text
    return {
        "name":     data.get("DeviceName"),
        "model":    data.get("DeviceModel"),
        "serial":   data.get("SerialNumber"),
        "firmware": data.get("FirmwareVersion"),
    }

def _norm_mac(raw):
    s = re.sub(r"[^0-9a-fA-F]", "", raw or "").lower()
    if len(s) != 12 or s == "ffffffffffff":
        return None
    return ":".join(s[i:i+2] for i in range(0, 12, 2))

def _parse_channel_details(text):
    """ChannelDetailInfos -> per-channel dicts (name/online/manufacturer/model/
    IP/MAC). Status 1 = online."""
    data = _lapi_data(text) if isinstance(text, str) else text
    out = []
    for d in data.get("DetailInfos") or []:
        addr = d.get("AddressInfo") or {}
        out.append({
            "channel":      d.get("ID"),
            "name":         (d.get("Name") or "").strip() or None,
            "online":       d.get("Status") == 1,
            "manufacturer": (d.get("Manufacturer") or "").capitalize() or "Uniview",
            "model":        d.get("DeviceModel") or None,
            "ip":           addr.get("Address") or None,
            "mac":          _norm_mac(addr.get("MAC")),
        })
    return out

def _parse_ipc_device_infos(text):
    """Channels/System/DeviceInfos -> {channel_id: {serial, firmware, model}}."""
    data = _lapi_data(text) if isinstance(text, str) else text
    out = {}
    for d in data.get("DeviceInfos") or []:
        if d.get("ID") is None:
            continue
        out[d["ID"]] = {
            "serial":   d.get("SerialNumber") or None,
            "firmware": d.get("FirmwareVersion") or None,
            "model":    d.get("DeviceModel") or None,
        }
    return out

# ── probe + collect ──────────────────────────────────────────────────────────

def probe_unv(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, UNV_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Uniview NVR", ip, "unreachable",
                                 f"port {UNV_PORT} closed or timed out")
            return None
        sess = UnvSession(ip)
        try:
            info = _parse_device_info(sess.get("/LAPI/V1.0/System/DeviceInfo"))
            if not (info.get("serial") or info.get("model")):
                raise RuntimeError("no serial/model from DeviceInfo")
            return {
                "ip":           ip,
                "host":         f"{ip}:{UNV_PORT}",
                "serial":       info.get("serial"),
                "model":        info.get("model"),
                "hostname":     info.get("name") or f"unv-{ip.replace('.', '-')}",
                "reported_ip":  ip,
                "mac":          None,
                "manufacturer": "Uniview",
                "firmware":     info.get("firmware"),
            }
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Uniview NVR", ip, "no data", classify_error(exc))
            return None
        finally:
            sess.logout()
    return None

def unv_collect(ip):
    """Full collection: NVR identity + cameras (channel/name/online/IP/MAC/
    manufacturer + model/serial/firmware merged from DeviceInfos)."""
    sess = UnvSession(ip)
    try:
        info = _parse_device_info(sess.get("/LAPI/V1.0/System/DeviceInfo"))
        cameras = _parse_channel_details(
            sess.get("/LAPI/V1.0/Channels/System/ChannelDetailInfos"))
        try:
            ipc = _parse_ipc_device_infos(
                sess.get("/LAPI/V1.0/Channels/System/DeviceInfos"))
        except Exception as exc:
            ipc = {}
            log("WARN", f"  unv {ip}: Channels/System/DeviceInfos failed: {exc}")
        for cam in cameras:
            detail = ipc.get(cam["channel"]) or {}
            cam["serial"] = detail.get("serial")
            cam["firmware"] = detail.get("firmware")
            cam["model"] = cam["model"] or detail.get("model")
            if not cam["name"]:
                cam["name"] = f"unv-cam-ch{cam['channel']}"
        # Drop unassigned channel slots (16-ch NVR with 9 cameras reports
        # placeholders named "IP Camera N" with no IP and no serial).
        cameras = [c for c in cameras if c.get("ip") or c.get("serial")]
        log("INFO", f"  unv: {len(cameras)} cameras "
                    f"({sum(1 for c in cameras if c['online'])} online, "
                    f"{sum(1 for c in cameras if c.get('mac'))} with MAC)")
        return {
            "summary": {
                "name":     info.get("name") or f"unv-{ip.replace('.', '-')}",
                "model":    info.get("model"),
                "serial":   info.get("serial"),
                "firmware": info.get("firmware"),
            },
            "cameras": cameras,
        }
    finally:
        sess.logout()
