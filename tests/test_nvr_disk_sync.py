"""NVR physical-disk sync tests.

Contract (real-serial-only):
- A disk becomes an Inventory Item ONLY when the NVR reports the physical
  drive's own serial number.
- NEVER fabricate a serial from the NVR serial, MAC, or slot.
- Disks with no reported serial are skipped and logged as unavailable.
- Idempotent: same real serial -> update, never duplicate.
- Dahua/UNV: no disk API -> write nothing.
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
    return {"slot": slot, "name": f"Disk {slot} ({cap})", "type": "SATA",
            "capacity": cap, "status": status, "property": "RW",
            "model": model, "serial": serial}


def _patch_netbox(monkeypatch):
    calls = {"manufacturers": [], "roles": []}
    monkeypatch.setattr(sync_mod, "get_or_create_manufacturer",
                        lambda n: (calls["manufacturers"].append(n), 55)[1])
    monkeypatch.setattr(sync_mod, "get_or_create_inventory_role",
                        lambda n: (calls["roles"].append(n), 7)[1])
    return calls


def _sync(items, hdds, probe=None, vendor="Hikvision"):
    return sync_mod._sync_nvr_disks(api=_fake_api(inventory_items=items),
                                    dev_id=42,
                                    probe=probe or {"serial": "NVR-1"},
                                    hdds=hdds, vendor=vendor)


def test_real_serial_creates_item_with_model_and_manufacturer(monkeypatch):
    items = FakeEndpoint([])
    calls = _patch_netbox(monkeypatch)
    _sync(items, [_hdd(model="WD40PURZ", serial="WD-SN-001")])

    assert items.create_calls == 1
    p = items.created[0]
    assert p["serial"] == "WD-SN-001"
    assert p["part_id"] == "WD40PURZ"
    assert calls["manufacturers"] == ["WD40PURZ"]


def test_no_real_serial_writes_nothing(monkeypatch, capsys):
    items = FakeEndpoint([])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(slot="1", serial=None, cap="16TB"),
                  _hdd(slot="2", serial=None, cap="16TB")],
          probe={"serial": "FK2666199", "ip": "10.0.0.1"})

    assert items.create_calls == 0 and items.update_calls == 0
    assert "NO REAL HDD SERIAL" in capsys.readouterr().out


def test_mixed_serials_only_syncs_real(monkeypatch):
    items = FakeEndpoint([])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(slot="1", serial="WD-REAL-SN-1", cap="16TB"),
                  _hdd(slot="2", serial=None, cap="16TB"),
                  _hdd(slot="3", serial="WD-REAL-SN-3", cap="16TB")])

    assert items.create_calls == 2
    assert sorted(i["serial"] for i in items.created) == ["WD-REAL-SN-1",
                                                          "WD-REAL-SN-3"]


def test_never_substitutes_nvr_serial_mac_or_slot(monkeypatch):
    items = FakeEndpoint([])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(slot="4", serial=None, cap="4TB")],
          probe={"serial": "FK2666199", "mac": "aa:bb:cc:dd:ee:ff"})

    assert items.create_calls == 0
    for it in items.created + items.updated:
        s = it.get("serial", "")
        assert "FK2666199" not in s and "SLOT" not in s and ":" not in s


def test_missing_model_falls_back_to_unknown(monkeypatch):
    items = FakeEndpoint([])
    calls = _patch_netbox(monkeypatch)
    _sync(items, [_hdd(model=None)])

    assert calls["manufacturers"] == ["Unknown"]
    assert "part_id" not in items.created[0]


def test_same_real_serial_updates_in_place(monkeypatch):
    items = FakeEndpoint([FakeRecord(7, serial="WD-SN-001", device_id=42,
                                     name="old",
                                     role=type("R", (), {"name": "HDD"})())])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(serial="WD-SN-001")])

    assert items.create_calls == 0 and items.update_calls == 1
    assert items.updated[0]["id"] == 7
    assert items.updated[0]["serial"] == "WD-SN-001"


def test_update_is_idempotent(monkeypatch):
    items = FakeEndpoint([])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(serial="WD-SN-001")])
    first_calls = items.create_calls
    _sync(items, [_hdd(serial="WD-SN-001")])
    assert items.create_calls == first_calls
    assert items.update_calls == 1


def test_dahua_no_hdds_writes_nothing():
    items = FakeEndpoint([])
    _sync(items, [], vendor="Dahua")
    assert items.create_calls == 0 and items.update_calls == 0


def test_unv_no_hdds_writes_nothing():
    items = FakeEndpoint([])
    _sync(items, [], vendor="Uniview")
    assert items.create_calls == 0 and items.update_calls == 0


def test_real_serial_does_not_steal_unrelated_item(monkeypatch):
    """A disk with a real NVR serial that is NOT in NetBox creates a new item;
    it must never adopt an unrelated item that has a different real serial."""
    items = FakeEndpoint([FakeRecord(12, serial="WCC7K7TPPYXD", device_id=42,
                                     name="WCC7K7TPPYXD (4TB)",
                                     role=type("R", (), {"name": "HDD"})())])
    _patch_netbox(monkeypatch)
    _sync(items, [_hdd(serial="REAL-NVR-SERIAL", cap="4TB")])

    assert items.create_calls == 1
    assert items.created[0]["serial"] == "REAL-NVR-SERIAL"
    assert items.updated == []


def test_parse_hdd_capacity_is_decimal_tb():
    from netbox_sync.collectors import hikvision as hk
    xml = ('<hddList xmlns="http://www.isapi.org/ver20/XMLSchema">'
           '<hdd><id>1</id><hddType>SATA</hddType><capacity>17166336</capacity>'
           '<status>ok</status><property>RW</property><hddSerialNumber></hddSerialNumber>'
           '<hddModel></hddModel></hdd></hddList>')
    hdds = hk._parse_hdd_list(xml)
    assert hdds[0]["capacity"] == "16TB"
