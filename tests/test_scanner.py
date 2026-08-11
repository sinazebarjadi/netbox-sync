"""Tests for the unified scanner: family skipping and the probe pool."""
import pytest

import netbox_sync.scanner as scanner


def _fail_probe(ip):
    raise AssertionError(f"probe must not be called for {ip}")


@pytest.fixture
def no_families(monkeypatch):
    for attr in ("BMC_RANGES", "STORAGE_RANGES", "SAN_RANGES", "CISCO_RANGES",
                 "FORTIGATE_RANGES", "RUCKUS_RANGES", "UNIFI_RANGES"):
        monkeypatch.setattr(scanner, attr, [])
    for fn in ("probe_redfish", "probe_storage", "probe_san_switch",
               "probe_cisco_switch", "probe_fortigate", "probe_ruckus",
               "probe_unifi"):
        monkeypatch.setattr(scanner, fn, _fail_probe)


def test_scan_all_skips_disabled_families(no_families):
    found = scanner.scan_all()
    assert found == {"servers": [], "storage": [], "san_switches": [],
                     "cisco_switches": [], "fortigates": [], "ruckus": [],
                     "hikvision_nvrs": [], "unifi": [],
                     "dahua_nvrs": [], "unv_nvrs": []}


def test_scan_all_collects_found_devices(monkeypatch, no_families):
    monkeypatch.setattr(scanner, "BMC_RANGES", ["192.0.2.0/30"])
    def fake_probe(ip):
        if ip == "192.0.2.1":
            return {"ip": ip, "host": f"{ip}:443", "serial": "S1",
                    "model": "HPE DL360 G10", "hostname": "srv1",
                    "manufacturer": "HPE"}
        return None
    monkeypatch.setattr(scanner, "probe_redfish", fake_probe)

    found = scanner.scan_all()
    assert [s["ip"] for s in found["servers"]] == ["192.0.2.1"]
    assert found["storage"] == []


# ── offline sweep gating ─────────────────────────────────────────────────────

import netbox_sync.sync as sync_mod
import netbox_sync.netbox as nbx
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api


def test_offline_sweep_skipped_when_family_disabled(monkeypatch):
    """A disabled family (empty ranges) must not touch its NetBox devices —
    otherwise disabling a family would offline its fleet after
    OFFLINE_THRESHOLD runs."""
    devices_ep = FakeEndpoint([
        FakeRecord(1, cf_redfish_enabled=True,
                   custom_fields={"bmc_ip": "192.0.2.5"}),
    ])
    calls = []
    monkeypatch.setattr(sync_mod, "_check_offline", lambda *a: calls.append(a))

    sync_mod._offline_sweep(_fake_api(devices=devices_ep), False,
                            "cf_redfish_enabled", "bmc_ip", set(),
                            nbx.mark_server_offline, "Server")
    assert calls == []


def test_offline_sweep_processes_devices_when_enabled(monkeypatch):
    devices_ep = FakeEndpoint([
        FakeRecord(1, name="srv1", cf_redfish_enabled=True,
                   custom_fields={"bmc_ip": "192.0.2.5"}),
        FakeRecord(2, name="srv2", cf_redfish_enabled=True,
                   custom_fields={"bmc_ip": "192.0.2.6/32"}),
    ])
    calls = []
    monkeypatch.setattr(sync_mod, "_check_offline", lambda *a: calls.append(a))

    sync_mod._offline_sweep(_fake_api(devices=devices_ep), True,
                            "cf_redfish_enabled", "bmc_ip", {"192.0.2.5"},
                            nbx.mark_server_offline, "Server")
    # both devices examined; the /32 suffix is stripped to a bare IP
    assert [c[0] for c in calls] == ["192.0.2.5", "192.0.2.6"]
    assert {c[2] for c in calls} == {1, 2}   # dev ids

def test_offline_sweep_scopes_by_manufacturer(monkeypatch):
    """The NVR vendors share the nvr_* custom fields; a Dahua sweep must never
    examine Hikvision devices (or it would offline them on sight)."""
    hik = FakeRecord(1, name="nvr-hik", cf_nvr_enabled=True,
                     custom_fields={"nvr_ip": "192.168.230.66"})
    hik.manufacturer = type("M", (), {"name": "Hikvision"})()
    dah = FakeRecord(2, name="nvr-dahua", cf_nvr_enabled=True,
                     custom_fields={"nvr_ip": "192.168.252.5"})
    dah.manufacturer = type("M", (), {"name": "Dahua"})()
    devices_ep = FakeEndpoint([hik, dah])
    calls = []
    monkeypatch.setattr(sync_mod, "_check_offline", lambda *a: calls.append(a))

    sync_mod._offline_sweep(_fake_api(devices=devices_ep), True,
                            "cf_nvr_enabled", "nvr_ip", set(),
                            lambda *a: None, "Dahua NVRs", mfr="Dahua")
    assert [c[0] for c in calls] == ["192.168.252.5"]   # only the Dahua NVR


def test_offline_sweep_without_mfr_keeps_legacy_behavior(monkeypatch):
    dev = FakeRecord(1, name="nvr-hik", cf_nvr_enabled=True,
                     custom_fields={"nvr_ip": "192.168.230.66"})
    devices_ep = FakeEndpoint([dev])
    calls = []
    monkeypatch.setattr(sync_mod, "_check_offline", lambda *a: calls.append(a))

    sync_mod._offline_sweep(_fake_api(devices=devices_ep), True,
                            "cf_nvr_enabled", "nvr_ip", set(),
                            lambda *a: None, "Hikvision NVRs")
    assert len(calls) == 1

def test_process_nvrs_sweep_keeps_camera_when_channel_still_reported(monkeypatch):
    """Serial fetch failed (503) but the channel is still in the list ->
    the camera must NOT be marked offline."""
    existing = FakeRecord(5, name="C3", serial="",
                          custom_fields={"cam_nvr": "NVR1", "cam_enabled": True,
                                         "cam_serial": "S1", "cam_channel": 3})
    api = _fake_api(devices=FakeEndpoint([existing]))
    offlined = []
    monkeypatch.setattr(sync_mod, "mark_camera_offline",
                        lambda i, n: offlined.append((i, n)))
    monkeypatch.setattr(sync_mod, "ensure_camera_device", lambda *a, **k: 99)
    monkeypatch.setattr(sync_mod, "ensure_primary_ip", lambda *a, **k: None)

    data = {"summary": {"name": "NVR1", "model": "M", "firmware": "F"},
            "cameras": [{"channel": 3, "serial": None, "name": "C3",
                         "online": True, "ip": None}]}
    sync_mod.process_nvrs([{"ip": "10.0.0.9"}], lambda ip: data,
                          lambda probe: 7, "Test", {}, {}, api)
    assert offlined == []


def test_process_nvrs_sweep_offlines_truly_missing_camera(monkeypatch):
    """Serial not reported AND channel not listed -> offline."""
    existing = FakeRecord(5, name="C9", serial="",
                          custom_fields={"cam_nvr": "NVR1", "cam_enabled": True,
                                         "cam_serial": "S9", "cam_channel": 9})
    api = _fake_api(devices=FakeEndpoint([existing]))
    offlined = []
    monkeypatch.setattr(sync_mod, "mark_camera_offline",
                        lambda i, n: offlined.append((i, n)))
    monkeypatch.setattr(sync_mod, "ensure_camera_device", lambda *a, **k: 99)
    monkeypatch.setattr(sync_mod, "ensure_primary_ip", lambda *a, **k: None)

    data = {"summary": {"name": "NVR1", "model": "M", "firmware": "F"},
            "cameras": [{"channel": 3, "serial": "S1", "name": "C3",
                         "online": True, "ip": None}]}
    sync_mod.process_nvrs([{"ip": "10.0.0.9"}], lambda ip: data,
                          lambda probe: 7, "Test", {}, {}, api)
    assert offlined == [(5, "C9")]
