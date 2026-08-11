"""Tests for the Hikvision ISAPI XML parsers."""
import netbox_sync.collectors.hikvision as mod


DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceName>CAM-12</deviceName>
  <model>DS-2CD2143G2-I</model>
  <serialNumber>ABCD123456789</serialNumber>
  <macAddress>b4:0b:44:12:ab:cd</macAddress>
  <firmwareVersion>V5.7.3</firmwareVersion>
  <deviceType>IPCamera</deviceType>
  <channelID>12</channelID>
</DeviceInfo>
"""


def test_parse_device_info_namespaced_with_channel():
    out = mod._parse_device_info(DEVICE_INFO)
    assert out["name"] == "CAM-12"
    assert out["model"] == "DS-2CD2143G2-I"
    assert out["serial"] == "ABCD123456789"
    assert out["mac"] == "b4:0b:44:12:ab:cd"
    assert out["firmware"] == "V5.7.3"
    assert out["channel"] == "12"

# ── Hikvision per-channel retry (503 rate-limit) ────────────────────────────

def test_hikvision_collect_retries_per_channel_deviceinfo(monkeypatch):
    import netbox_sync.collectors.hikvision as hk

    calls = {"n": 0}

    class _Resp:
        def __init__(self, text, code=200):
            self.text = text
            self.status_code = code

    class _FakeSession:
        def __init__(self, *a, **k): pass
        def get(self, path):
            if path == "/ISAPI/System/deviceInfo":
                return "<DeviceInfo><deviceName>N</deviceName></DeviceInfo>"
            if path == "/ISAPI/ContentMgmt/InputProxy/channels":
                return ("<InputProxyChannelList><InputProxyChannel>"
                        "<id>3</id><name>C3</name></InputProxyChannel>"
                        "</InputProxyChannelList>")
            if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                return ("<InputProxyChannelStatusList><InputProxyChannelStatus>"
                        "<id>3</id><online>true</online>"
                        "</InputProxyChannelStatus></InputProxyChannelStatusList>")
            if path.endswith("/deviceInfo"):
                calls["n"] += 1
                if calls["n"] < 3:
                    import requests
                    raise requests.HTTPError("503 Server Error")
                return ("<DeviceInfo><serialNumber>SER3</serialNumber>"
                        "<macAddress>b4:0b:44:12:ab:cd</macAddress></DeviceInfo>")
            raise RuntimeError(path)
        def logout(self): pass

    monkeypatch.setattr(hk, "HikvisionSession", _FakeSession)
    monkeypatch.setattr(hk.time, "sleep", lambda s: None)

    out = hk.hikvision_collect("10.0.0.1")
    cam = out["cameras"][0]
    assert calls["n"] == 3                     # failed twice, then succeeded
    assert cam["serial"] == "SER3"
    assert cam["mac"] == "b4:0b:44:12:ab:cd"


def test_hikvision_collect_tolerates_persistent_deviceinfo_failure(monkeypatch):
    import netbox_sync.collectors.hikvision as hk

    class _FakeSession:
        def __init__(self, *a, **k): pass
        def get(self, path):
            if path == "/ISAPI/System/deviceInfo":
                return "<DeviceInfo><deviceName>N</deviceName></DeviceInfo>"
            if path == "/ISAPI/ContentMgmt/InputProxy/channels":
                return ("<InputProxyChannelList><InputProxyChannel>"
                        "<id>3</id><name>C3</name></InputProxyChannel>"
                        "</InputProxyChannelList>")
            if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                return ("<InputProxyChannelStatusList/>")
            if path.endswith("/deviceInfo"):
                import requests
                raise requests.HTTPError("503 Server Error")
            raise RuntimeError(path)
        def logout(self): pass

    monkeypatch.setattr(hk, "HikvisionSession", _FakeSession)
    monkeypatch.setattr(hk.time, "sleep", lambda s: None)

    out = hk.hikvision_collect("10.0.0.1")
    assert out["cameras"][0]["serial"] is None or out["cameras"][0].get("serial") is None
    assert out["cameras"][0]["mac"] is None
