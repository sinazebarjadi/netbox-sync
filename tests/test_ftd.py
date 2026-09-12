"""Tests for the Cisco FTD (via FMC) collector and NetBox ensure/offline."""
from types import SimpleNamespace

import pytest

import netbox_sync.collectors.ftd as ftd
import netbox_sync.netbox as nbx


# ── live-captured fixtures (FMC 7.x, 2026-09-09) ────────────────────────────

DEVICE_DETAIL = {
    "name": "HQ-FTD-01",
    "model": "Cisco Secure Firewall 3110 Threat Defense",
    "sw_version": "7.6.4",
    "hostName": "172.31.250.200",
    "healthStatus": "green",
    "ftdMode": "ROUTED",
    "isConnected": True,
    "deviceGroup": {"name": "HQ"},
    "metadata": {"deviceSerialNumber": "FJZ2909JHFH"},
}

DEVICE_DETAIL_NO_SERIAL = {
    "name": "Afra-FTD-02", "model": "Cisco Secure Firewall 3110 Threat Defense",
    "sw_version": "7.6.4", "hostName": "192.168.19.3",
    "healthStatus": "green", "ftdMode": "ROUTED", "isConnected": True,
    "deviceGroup": {"name": "Afra"}, "metadata": {},
}


def test_parse_device_detail():
    out = ftd._parse_device_detail(DEVICE_DETAIL)
    assert out == {
        "name": "HQ-FTD-01",
        "model": "Cisco Secure Firewall 3110 Threat Defense",
        "serial": "FJZ2909JHFH",
        "mgmt_ip": "172.31.250.200",
        "sw_version": "7.6.4",
        "health": "green",
        "mode": "ROUTED",
        "group": "HQ",
        "connected": True,
    }


def test_parse_device_detail_missing_serial():
    out = ftd._parse_device_detail(DEVICE_DETAIL_NO_SERIAL)
    assert out["serial"] is None
    assert out["mgmt_ip"] == "192.168.19.3"
    assert out["group"] == "Afra"


def test_session_login_and_get(monkeypatch):
    """Token generated on init (Basic->header), used for later calls, 401 regen."""
    calls = {"posts": 0, "gets": 0}

    class _Resp:
        def __init__(self, status, headers=None, payload=None):
            self.status_code = status
            self.headers = headers or {}
            self._payload = payload or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._payload

    class _S:
        def __init__(self):
            self.headers = {}
            self.verify = False
        def post(self, url, auth=None, timeout=None):
            calls["posts"] += 1
            return _Resp(204, {"X-auth-access-token": "TOK123",
                               "DOMAINS": '[{"name":"Global","uuid":"u-1"}]'})
        def get(self, url, timeout=None):
            calls["gets"] += 1
            if "X-auth-access-token" not in self.headers:
                return _Resp(401)
            return _Resp(200, payload={"items": [{"id": "a"}], "paging": {"count": 1}})

    monkeypatch.setattr(ftd.requests, "Session", _S)
    sess = ftd.FMCSession("192.168.16.143")
    assert sess.domain_uuid == "u-1"
    assert sess.domain_name == "Global"
    items = sess.get_items("/api/fmc_config/v1/domain/u-1/devices/devicerecords")
    assert items == [{"id": "a"}]
    assert calls["posts"] >= 1


# ── NetBox ensure / offline ─────────────────────────────────────────────────

def _ftd_setup(monkeypatch):
    from tests.test_netbox_sync import FakeEndpoint
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device",
                        lambda serial, role_name=None: next(
                            (d for d in devices_ep.items
                             if getattr(d, "serial", None) == serial), None))
    return devices_ep


def test_ensure_ftd_creates_and_matches_by_serial(monkeypatch):
    devices_ep = _ftd_setup(monkeypatch)
    ftd_rec = {"name": "HQ-FTD-01", "model": "Cisco Secure Firewall 3110 Threat Defense",
               "serial": "FJZ2909JHFH", "mgmt_ip": "172.31.250.200",
               "sw_version": "7.6.4", "health": "green", "mode": "ROUTED",
               "group": "HQ", "connected": True}
    dev_id = nbx.ensure_ftd_device(ftd_rec, fmc_ip="192.168.16.143")

    payload = devices_ep.created[0]
    assert payload["name"] == "HQ-FTD-01"
    assert payload["serial"] == "FJZ2909JHFH"
    cf = payload["custom_fields"]
    assert cf["ftd_ip"] == "172.31.250.200"
    assert cf["ftd_enabled"] is True
    assert cf["ftd_model"] == "Cisco Secure Firewall 3110 Threat Defense"
    assert cf["ftd_firmware"] == "7.6.4"
    assert cf["ftd_health"] == "green"
    assert cf["ftd_mode"] == "ROUTED"
    assert cf["ftd_group"] == "HQ"
    assert cf["ftd_fmc"] == "192.168.16.143"

    dev_id2 = nbx.ensure_ftd_device(ftd_rec, fmc_ip="192.168.16.143")
    assert dev_id2 == dev_id
    assert len(devices_ep.created) == 1


def test_ensure_ftd_adopts_ae_record_by_serial(monkeypatch):
    from tests.test_netbox_sync import FakeRecord
    devices_ep = _ftd_setup(monkeypatch)
    existing = FakeRecord(50, name="HQ-FTD-01", serial="FJZ2909JHFH",
                          site_id=13, role_id=99,
                          role=SimpleNamespace(id=99, name="Firewall"),
                          custom_fields={"ae_asset_id": "7"})
    devices_ep.items.append(existing)
    existing._endpoint = devices_ep

    ftd_rec = {"name": "HQ-FTD-01", "model": "Cisco Secure Firewall 3110 Threat Defense",
               "serial": "FJZ2909JHFH", "mgmt_ip": "172.31.250.200",
               "sw_version": "7.6.4", "health": "green", "mode": "ROUTED",
               "group": "HQ", "connected": True}
    dev_id = nbx.ensure_ftd_device(ftd_rec, fmc_ip="192.168.16.143")
    assert dev_id == 50
    assert len(devices_ep.created) == 0
    assert devices_ep.updated[-1]["role"] == 12   # role corrected to FTD


def test_mark_ftd_offline(monkeypatch):
    from tests.test_netbox_sync import FakeRecord, FakeEndpoint
    dev = FakeRecord(9, name="HQ-FTD-01", custom_fields={"ftd_enabled": True})
    devices_ep = FakeEndpoint([dev])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))
    nbx.mark_ftd_offline(9, "HQ-FTD-01")
    assert devices_ep.updated[0]["status"] == "offline"
    assert devices_ep.updated[0]["custom_fields"]["ftd_enabled"] is False
