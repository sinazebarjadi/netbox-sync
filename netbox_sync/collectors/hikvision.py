"""Hikvision NVRs: ISAPI-over-HTTP digest session, identity/channel/status
parsers, probe and collection. Cameras attached to an NVR are returned as a
list of dicts; the NVR itself is the only device this family models."""
import time
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from netbox_sync.config import (HIKVISION_USER, HIKVISION_PASS, HIKVISION_PORT,
                                log)
from netbox_sync.utils import is_port_open

_ISAPI_NS = "http://www.isapi.org/ver20/XMLSchema"


class HikvisionSession:
    """Minimal ISAPI client: HTTP digest auth against a plain-HTTP NVR."""

    def __init__(self, ip, port=None, timeout=15):
        self.ip = ip
        self.port = port or HIKVISION_PORT
        self.base = f"http://{ip}:{self.port}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(HIKVISION_USER, HIKVISION_PASS)
        self.session.verify = False

    def get(self, path):
        url = f"{self.base}{path if path.startswith('/') else '/' + path}"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def logout(self):
        try:
            self.session.close()
        except Exception:
            pass


# ── parsers ──────────────────────────────────────────────────────────────────

def _root(xml_text):
    """Parse ISAPI XML, tolerating the default namespace by matching on local
    names (we strip the nsmap via wildcard lookups)."""
    return ET.fromstring(xml_text)


def _findall_local(root, tag):
    """Find all descendants with a given local name, ignoring namespace."""
    return root.findall(f".//{{{_ISAPI_NS}}}{tag}") or root.findall(f".//{tag}")


def _child_text(elem, tag):
    """Text of the first child with a given local name (namespace-agnostic)."""
    for child in elem:
        if child.tag.split("}")[-1] == tag:
            return (child.text or "").strip()
    return None


def _parse_device_info(xml_text):
    """`GET /ISAPI/System/deviceInfo` -> NVR identity dict. Also used for the
    per-channel proxied camera deviceInfo (which carries the camera MAC)."""
    root = _root(xml_text)
    return {
        "name":        _child_text(root, "deviceName"),
        "model":       _child_text(root, "model"),
        "serial":      _child_text(root, "serialNumber"),
        "mac":         _child_text(root, "macAddress"),
        "firmware":    _child_text(root, "firmwareVersion"),
        "device_type": _child_text(root, "deviceType"),
        "channel":     _child_text(root, "channelID"),
    }


def _parse_channels(xml_text):
    """`GET /ISAPI/ContentMgmt/InputProxy/channels` -> per-camera dicts."""
    root = _root(xml_text)
    cameras = []
    for chan in _findall_local(root, "InputProxyChannel"):
        src = None
        for child in chan:
            if child.tag.split("}")[-1] == "sourceInputPortDescriptor":
                src = child
                break
        cameras.append({
            "channel":  _child_text(chan, "id"),
            "name":     _child_text(chan, "name"),
            "ip":       _child_text(src, "ipAddress") if src is not None else None,
            "model":    _child_text(src, "model") if src is not None else None,
            "serial":   _child_text(src, "serialNumber") if src is not None else None,
            "firmware": _child_text(src, "firmwareVersion") if src is not None else None,
        })
    return cameras


def _parse_channel_status(xml_text):
    """`GET .../channels/status` -> {channel_id: online_bool}."""
    root = _root(xml_text)
    status = {}
    for entry in _findall_local(root, "InputProxyChannelStatus"):
        cid = _child_text(entry, "id")
        online = (_child_text(entry, "online") or "").lower() == "true"
        if cid is not None:
            status[cid] = online
    return status


# ── probe / collect ──────────────────────────────────────────────────────────

def probe_hikvision(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, HIKVISION_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        sess = HikvisionSession(ip)
        try:
            info = _parse_device_info(sess.get("/ISAPI/System/deviceInfo"))
            if not (info.get("serial") or info.get("model")):
                raise RuntimeError("deviceInfo yielded no serial/model")
            return {
                "ip":           ip,
                "host":         f"{ip}:{HIKVISION_PORT}",
                "serial":       info.get("serial"),
                "model":        info.get("model"),
                "hostname":     info.get("name") or f"nvr-{ip.replace('.', '-')}",
                "reported_ip":  ip,
                "mac":          info.get("mac"),
                "manufacturer": "Hikvision",
                "firmware":     info.get("firmware"),
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None


def hikvision_collect(ip):
    """Full collection: NVR identity + camera list with online status and MAC.
    Camera MACs are not in the channel list — each camera's MAC is fetched from
    the NVR-proxied `/ISAPI/ContentMgmt/InputProxy/channels/<id>/deviceInfo`."""
    sess = HikvisionSession(ip)
    try:
        info = _parse_device_info(sess.get("/ISAPI/System/deviceInfo"))
        cameras = _parse_channels(
            sess.get("/ISAPI/ContentMgmt/InputProxy/channels"))
        try:
            status = _parse_channel_status(
                sess.get("/ISAPI/ContentMgmt/InputProxy/channels/status"))
        except Exception as exc:
            log("WARN", f"  hikvision channel status failed for {ip}: {exc}")
            status = {}
        for cam in cameras:
            cam["online"] = status.get(cam["channel"], False)
            cam["mac"] = None
            if cam.get("channel"):
                # Big NVRs rate-limit the proxied per-channel calls with
                # 503s — retry with backoff before giving up (a missing
                # serial/MAC here used to cause false offline markings).
                ci = None
                for attempt in (0, 2, 5):
                    if attempt:
                        time.sleep(attempt)
                    try:
                        ci = _parse_device_info(sess.get(
                            f"/ISAPI/ContentMgmt/InputProxy/channels/"
                            f"{cam['channel']}/deviceInfo"))
                        break
                    except Exception as exc:
                        last_exc = exc
                if ci is None:
                    log("WARN", f"  camera ch{cam['channel']} deviceInfo "
                                f"failed after retries: {last_exc}")
                    continue
                cam["mac"] = ci.get("mac")
                # the proxied deviceInfo is authoritative for model/serial/fw
                cam["model"] = ci.get("model") or cam.get("model")
                cam["serial"] = ci.get("serial") or cam.get("serial")
                cam["firmware"] = ci.get("firmware") or cam.get("firmware")
        log("INFO", f"  hikvision: {len(cameras)} cameras "
                    f"({sum(1 for c in cameras if c['online'])} online, "
                    f"{sum(1 for c in cameras if c.get('mac'))} with MAC)")
        return {
            "summary": {
                "name": info.get("name"), "model": info.get("model"),
                "serial": info.get("serial"), "mac": info.get("mac"),
                "firmware": info.get("firmware"),
            },
            "cameras": cameras,
        }
    finally:
        sess.logout()
