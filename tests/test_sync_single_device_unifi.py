"""Tests for sync_single_device UniFi AP handling.

Contract:
- The UniFi single-device path must sync APs (ensure_ap_device per AP),
  not just the console device.
- Offline sweep marks APs that vanished from the controller.
"""
import pytest

import sync_single_device as ssd


def test_families_includes_unifi():
    assert "unifi" in ssd.FAMILIES


class _FakeDevices:
    def __init__(self, existing=None):
        self._existing = existing or []
        self.updated = []
    def filter(self, **kw):
        return self._existing
    def update(self, payload):
        self.updated.extend(payload)
        return True


def _fake_api(existing=None):
    devices = _FakeDevices(existing)
    api = type("API", (), {})()
    api.dcim = type("DCIM", (), {"devices": devices})()
    return api, devices


def _make_probe():
    return {"ip": "1.2.3.4", "serial": "uuid-1", "hostname": "unifi-1",
            "model": "UniFi OS Console", "firmware": "7.5"}


def _make_data(aps):
    return {"summary": {"name": "unifi-1", "version": "7.5"},
            "sites": [{"name": "s1", "desc": "S1"}],
            "aps": aps, "wlans": {}, "networks": {}}


def test_unifi_path_syncs_aps(monkeypatch):
    """Run the unifi branch and assert ensure_ap_device is called per AP."""
    calls = {"ensure": []}

    api, devices = _fake_api()
    monkeypatch.setattr(ssd, "get_netbox", lambda: api)
    monkeypatch.setattr(ssd, "ensure_custom_fields", lambda: None)

    probe = _make_probe()
    data = _make_data([
        {"mac": "aa", "name": "AP1", "ip": "10.0.0.1", "model": "U7PG2", "group": "S1"},
        {"mac": "bb", "name": "AP2", "ip": "10.0.0.2", "model": "U7PG2", "group": "S1"},
    ])
    monkeypatch.setattr(ssd, "unifi_collect", lambda ip: data)
    monkeypatch.setattr(ssd, "ensure_unifi_console",
                        lambda probe, ap_count=0, site_count=0: 99)
    monkeypatch.setattr(ssd, "ensure_ap_device",
                        lambda ap, wlc, manufacturer=None:
                        (calls["ensure"].append(ap["mac"]), 1)[1])
    monkeypatch.setattr(ssd, "ensure_primary_ip", lambda *a, **k: None)
    monkeypatch.setattr(ssd, "mark_ap_offline", lambda dev_id, name: None)

    rc = ssd._sync_unifi("1.2.3.4", probe)
    assert rc == 0
    assert sorted(calls["ensure"]) == ["aa", "bb"]


def test_unifi_path_offline_sweep(monkeypatch):
    """An AP in NetBox that is not on the controller gets marked offline."""
    calls = {"ensure": [], "offline": []}
    stale = type("D", (), {"id": 99, "name": "GONE",
                           "custom_fields": {"wap_mac": "gone"}})()

    api, devices = _fake_api(existing=[stale])
    monkeypatch.setattr(ssd, "get_netbox", lambda: api)
    monkeypatch.setattr(ssd, "ensure_custom_fields", lambda: None)

    probe = _make_probe()
    data = _make_data([
        {"mac": "aa", "name": "AP1", "ip": "10.0.0.1", "model": "U7PG2", "group": "S1"},
    ])
    monkeypatch.setattr(ssd, "unifi_collect", lambda ip: data)
    monkeypatch.setattr(ssd, "ensure_unifi_console",
                        lambda probe, ap_count=0, site_count=0: 99)
    monkeypatch.setattr(ssd, "ensure_ap_device",
                        lambda ap, wlc, manufacturer=None:
                        (calls["ensure"].append(ap["mac"]), 1)[1])
    monkeypatch.setattr(ssd, "ensure_primary_ip", lambda *a, **k: None)
    monkeypatch.setattr(ssd, "mark_ap_offline",
                        lambda dev_id, name: calls["offline"].append(dev_id))

    rc = ssd._sync_unifi("1.2.3.4", probe)
    assert rc == 0
    assert calls["offline"] == [99]
