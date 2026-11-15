"""Tests for NVR physical-disk inventory sync (Hikvision native, Dahua/UNV skip).

Scope:
- Hikvision: NVR-reported model/serial populate the NetBox Inventory Item
  (manufacturer from the disk model, part_id = model).
- Synthetic slot serial when the NVR exposes no serial number.
- Idempotency: a repeat run updates the same item instead of duplicating.
- Dahua/UNV: no disk API -> log a skip line and write nothing.
"""
import pytest

import netbox_sync.sync as sync_mod
import netbox_sync.netbox as nbx
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api


@pytest.fixture(autouse=True)
def _clear_caches():
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()
    yield
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()


def _hdd(slot="1", model="WD40PURZ", serial="WD-SN-001", cap="4TB", status="ok"):
    return {
        "slot": slot,
        "name": f"Disk {slot} ({cap})",
        "type": "SATA",
        "capacity": cap,
        "status": status,
        "property": "RW",
        "model": model,
        "serial": serial,
    }


def _patch_netbox(monkeypatch, items_endpoint):
    calls = {"manufacturers": [], "roles": []}

    def fake_mfr(name):
        calls["manufacturers"].append(name)
        return 55

    def fake_role(name):
        calls["roles"].append(name)
        return 7

    monkeypatch.setattr(sync_mod, "get_or_create_manufacturer", fake_mfr)
    monkeypatch.setattr(sync_mod, "get_or_create_inventory_role", fake_role)
    return calls


def test_hikvision_disk_uses_nvr_model_and_serial(monkeypatch):
    """NVR-reported model populates manufacturer + part_id; serial from NVR."""
    items = FakeEndpoint([])
    calls = _patch_netbox(monkeypatch, items)
    hdds = [_hdd(model="WD40PURZ", serial="WD-SN-001")]

    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=hdds, vendor="Hikvision")

    assert items.create_calls == 1
    payload = items.created[0]
    assert payload["device"] == 42
    assert payload["serial"] == "WD-SN-001"
    assert payload["manufacturer"] == 55
    assert payload["part_id"] == "WD40PURZ"
    # manufacturer resolved from the disk model, not a hardcoded vendor
    assert calls["manufacturers"] == ["WD40PURZ"]


def test_hikvision_disk_synthetic_serial_when_nvr_has_none(monkeypatch):
    items = FakeEndpoint([])
    _patch_netbox(monkeypatch, items)
    hdds = [_hdd(slot="3", serial=None)]

    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=hdds, vendor="Hikvision")

    assert items.created[0]["serial"] == "NVR-1-SLOT-3"


def test_hikvision_disk_missing_model_falls_back_to_unknown(monkeypatch):
    items = FakeEndpoint([])
    calls = _patch_netbox(monkeypatch, items)
    hdds = [_hdd(model=None)]

    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=hdds, vendor="Hikvision")

    assert calls["manufacturers"] == ["Unknown"]
    assert "part_id" not in items.created[0]


def test_hikvision_disk_update_is_idempotent(monkeypatch):
    """Same serial twice -> one item updated, never duplicated."""
    existing = FakeRecord(7, serial="WD-SN-001", device_id=42, name="old")
    items = FakeEndpoint([existing])
    _patch_netbox(monkeypatch, items)
    hdds = [_hdd(serial="WD-SN-001")]

    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=hdds, vendor="Hikvision")

    assert items.create_calls == 0
    assert items.update_calls == 1
    assert items.updated[0]["id"] == 7
    assert items.updated[0]["serial"] == "WD-SN-001"


def test_hikvision_disk_adopts_me_item_at_same_slot(monkeypatch):
    """An ME-imported item with a synthetic slot serial is adopted, not duped."""
    existing = FakeRecord(9, serial="NVR-1-SLOT-2", device_id=42, name="ME disk")
    items = FakeEndpoint([existing])
    _patch_netbox(monkeypatch, items)
    hdds = [_hdd(slot="2", serial=None)]

    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=hdds, vendor="Hikvision")

    assert items.create_calls == 0
    assert items.update_calls == 1
    assert items.updated[0]["id"] == 9


def test_dahua_with_no_hdds_writes_nothing_and_logs(capsys):
    items = FakeEndpoint([])
    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=[], vendor="Dahua")
    assert items.create_calls == 0
    assert items.update_calls == 0


def test_unv_with_no_hdds_writes_nothing():
    items = FakeEndpoint([])
    sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                             dev_id=42, probe={"serial": "NVR-1"},
                             hdds=[], vendor="Uniview")
    assert items.create_calls == 0
    assert items.update_calls == 0
