"""NVR disk inventory is ME-only: no NVR scanning, no vendor disk logic.

Contract:
- sync.py must NOT contain _sync_nvr_disks or any NVR disk collection call.
- hikvision_collect must NOT return or fetch an "hdds" key.
- dahua_collect / unv_collect must not collect disks (unchanged).
- ME component sync (used_by -> NVR) attaches real-serial HDDs to the NVR and
  is idempotent.
"""
import inspect
import pytest

import netbox_sync.sync as sync_mod
import netbox_sync.collectors.hikvision as hik


def test_sync_module_has_no_nvr_disk_sync():
    src = inspect.getsource(sync_mod)
    assert "_sync_nvr_disks" not in src
    assert "data.get(\"hdds\"" not in src
    assert not hasattr(sync_mod, "_sync_nvr_disks")


def test_hikvision_collect_has_no_hdd_logic():
    src = inspect.getsource(hik)
    assert "_parse_hdd_list" not in src
    assert "Storage/hdd" not in src
    assert not hasattr(hik, "_parse_hdd_list")


def test_hikvision_collect_returns_no_hdds(monkeypatch):
    """hikvision_collect result must not contain an hdds key."""
    class _FakeSession:
        def __init__(self, *a, **k): pass
        def get(self, path):
            if path == "/ISAPI/System/deviceInfo":
                return ("<DeviceInfo><deviceName>N</deviceName></DeviceInfo>")
            if path == "/ISAPI/ContentMgmt/InputProxy/channels":
                return ("<InputProxyChannelList><InputProxyChannel>"
                        "<id>1</id><name>C1</name></InputProxyChannel>"
                        "</InputProxyChannelList>")
            if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                return "<InputProxyChannelStatusList/>"
            if path.endswith("/deviceInfo"):
                return ("<DeviceInfo><serialNumber>S1</serialNumber>"
                        "<macAddress>b4:0b:44:12:ab:cd</macAddress></DeviceInfo>")
            raise RuntimeError(path)
        def logout(self): pass

    monkeypatch.setattr(hik, "HikvisionSession", _FakeSession)
    out = hik.hikvision_collect("10.0.0.1")
    assert "hdds" not in out
    assert out["summary"]["name"] == "N"


# ── ME component -> NVR attach (real serial only) ────────────────────────────

from netbox_sync.assetexplorer_sync import _ensure_inventory_item
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api
import netbox_sync.assetexplorer_sync as ae_sync
from netbox_sync.collectors.assetexplorer import _normalize


def _hdd_rec(nvr_name, nvr_serial):
    return _normalize({
        "id": "700", "name": "WD8PURZ-85BUY0", "org_serial_number": "WD8PURZ-85BUY0",
        "product_type": {"name": "HARD-CCTV", "internal_name": None},
        "used_by_asset": {"name": nvr_name, "org_serial_number": nvr_serial},
        "site": {"name": "HQ"}, "state": {"name": "In Use"},
        "udf_fields": {"udf_sline_601": "8TB"},
        "product": {"name": "WD Purple 8TB", "manufacturer": "Western Digital"},
    })


def test_me_hdd_attaches_to_nvr_and_is_idempotent(monkeypatch):
    parent = FakeRecord(9657, name="HQ-NVR-02", serial="FK2666191")
    devices = FakeEndpoint([parent])
    items = FakeEndpoint([])
    api = _fake_api(devices=devices, inventory_items=items)

    monkeypatch.setattr(ae_sync, "get_or_create_inventory_role", lambda n: 8)
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_site", lambda n: 33)
    monkeypatch.setattr(ae_sync, "get_or_create_device_type", lambda *a, **k: 44)
    monkeypatch.setattr(ae_sync, "get_or_create_role", lambda n, *a: 7)

    rec = _hdd_rec("HQ-NVR-02", "FK2666191")
    # Parent must be indexed by serial and name for _ensure_inventory_item
    existing_devices = {"fk2666191": parent}
    by_name = {"hq-nvr-02": [parent]}
    rc = _ensure_inventory_item(api, rec, existing_devices, by_name)
    assert rc == "created"
    assert items.create_calls == 1
    p = items.created[0]
    assert p["device"] == 9657
    assert p["serial"] == "WD8PURZ-85BUY0"
    assert "8TB" in p["name"] or "8TB" in p["description"]

    # second run: idempotent — same serial, no duplicate.
    # Give created items a real device object so the second-run serial scan
    # can read c.device.name (FakeEndpoint stores device as an int id).
    for item in items.items:
        item.device = parent
    rc = _ensure_inventory_item(api, rec, {}, {})
    assert rc in ("skipped", "updated")
    assert items.create_calls == 1
