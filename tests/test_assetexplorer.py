"""Tests for AssetExplorer sync: normalization, role/status mapping, and the
serial-based non-interference guard (discovery-managed devices untouched)."""
from types import SimpleNamespace

import pytest

from netbox_sync.collectors.assetexplorer import (_normalize, AE_TYPE_TO_ROLE,
                                                  AE_STATE_TO_STATUS,
                                                  AE_COMPONENT_TYPES)
import netbox_sync.assetexplorer_sync as ae_sync
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api


def _asset(**kw):
    base = {
        "id": "32", "name": "AFRA-HOST-04", "org_serial_number": "6CU8474FTV",
        "manufacturer": "HP", "asset_tag": "41510140",
        "state": {"name": "In Use"},
        "department": {"name": "ICT"},
        "site": {"name": "Afranet"},
        "location": "SHN R16 U15-16", "description": "ROMK-CL01-516",
        "product": {"name": "HP DL380 G10", "manufacturer": "HP"},
        "product_type": {"internal_name": "Server"},
    }
    base.update(kw)
    return base


# ── normalization / filtering ────────────────────────────────────────────────

def test_normalize_full_asset():
    rec = _normalize(_asset())
    assert rec["serial"] == "6CU8474FTV"
    assert rec["role"] == "Server"
    assert rec["asset_tag"] == "41510140"
    assert rec["is_component"] is False


def test_normalize_skips_no_serial():
    assert _normalize(_asset(org_serial_number="")) is None
    assert _normalize(_asset(org_serial_number=None)) is None


def test_normalize_skips_unmapped_type():
    assert _normalize(_asset(product_type={"internal_name": "Workstation"})) is None
    assert _normalize(_asset(product_type=None)) is None


@pytest.mark.parametrize("ae_type,role", list(AE_TYPE_TO_ROLE.items()))
def test_all_mapped_types(ae_type, role):
    rec = _normalize(_asset(product_type={"name": ae_type, "internal_name": None}))
    assert rec["role"] == role


@pytest.mark.parametrize("comp_type,role", list(AE_COMPONENT_TYPES.items()))
def test_all_component_types(comp_type, role):
    rec = _normalize(_asset(product_type={"name": comp_type, "internal_name": None}))
    assert rec["is_component"] is True
    assert rec["component_role"] == role


def test_ensure_inventory_item_attaches_to_parent(monkeypatch):
    """Component with used_by matching an existing device -> attached as Inventory Item."""
    parent = FakeRecord(10, name="R16-ToR-SW02", serial="FOC2206X0K1", custom_fields={})
    inv_ep = FakeEndpoint([])
    devices_ep = FakeEndpoint([parent])

    monkeypatch.setattr(ae_sync, "get_netbox", lambda: _fake_api(devices=devices_ep, inventory_items=inv_ep))
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_inventory_role", lambda n: 8)

    rec = _normalize(_asset(
        name="LIT20250K6Y", org_serial_number="LIT20250K6Y",
        product_type={"name": "Power Module", "internal_name": None},
        used_by_asset={"name": "R16-ToR-SW02", "org_serial_number": "FOC2206X0K1"}
    ))
    ae_sync._ensure_inventory_item(_fake_api(devices=devices_ep, inventory_items=inv_ep),
                                  rec, {"foc2206x0k1": parent}, {"r16-tor-sw02": [parent]})
    assert len(inv_ep.created) == 1
    assert inv_ep.created[0]["device"] == 10
    assert inv_ep.created[0]["serial"] == "LIT20250K6Y"


def test_ensure_inventory_item_creates_warehouse_device_when_unattached(monkeypatch):
    """Unattached component -> attached ONLY to the HQ Warehouse-Stock container."""
    inv_ep = FakeEndpoint([])
    devices_ep = FakeEndpoint([])

    monkeypatch.setattr(ae_sync, "get_netbox", lambda: _fake_api(devices=devices_ep, inventory_items=inv_ep))
    monkeypatch.setattr(ae_sync, "get_or_create_site", lambda n: 33)
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_device_type", lambda *a, **k: 44)
    monkeypatch.setattr(ae_sync, "get_or_create_role", lambda n, *a: 7)
    monkeypatch.setattr(ae_sync, "get_or_create_inventory_role", lambda n: 8)

    rec = _normalize(_asset(
        name="S6M7NE0TA04794", org_serial_number="S6M7NE0TA04794",
        product_type={"name": "HARD-Hardware", "internal_name": None},
        site={"name": "Afranet"},   # Even if site is Afranet, unattached stock goes to HQ
        udf_fields={"udf_sline_601": "960GB"}
    ))
    ae_sync._ensure_inventory_item(_fake_api(devices=devices_ep, inventory_items=inv_ep),
                                  rec, {}, {})
    # 1 warehouse device created (HQ only) + 1 inventory item created with capacity
    assert len(devices_ep.created) == 1
    assert devices_ep.created[0]["name"] == "Warehouse-Stock-HQ"
    assert len(inv_ep.created) == 1
    assert inv_ep.created[0]["serial"] == "S6M7NE0TA04794"
    assert "960GB" in inv_ep.created[0]["name"]
    assert "Capacity: 960GB" in inv_ep.created[0]["description"]
    rec = _normalize(_asset(product_type={"name": "CCTV", "internal_name": None}))
    assert rec["role"] == "Camera"


def test_custom_type_nvr_maps_to_nvr():
    rec = _normalize(_asset(product_type={"name": "NVR", "internal_name": None}))
    assert rec["role"] == "NVR"


@pytest.mark.parametrize("state,status", list(AE_STATE_TO_STATUS.items()))
def test_state_mapping(state, status):
    rec = _normalize(_asset(state={"name": state}))
    assert rec["status"] == status


def test_state_unknown_defaults_inventory():
    rec = _normalize(_asset(state={"name": "Retired"}))
    assert rec["status"] == "inventory"


def test_missing_optionals():
    rec = _normalize(_asset(manufacturer=None, product={}, site=None,
                            department=None, location=None, description=None,
                            asset_tag=None))
    assert rec["manufacturer"] is None and rec["site"] is None
    assert rec["department"] is None and rec["model"] is None


# ── sync: create / update / skip guard ───────────────────────────────────────

def _patch_helpers(monkeypatch, devices_ep):
    monkeypatch.setattr(ae_sync, "get_netbox",
                        lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(ae_sync, "ensure_custom_fields", lambda: None)
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_device_type", lambda *a, **k: 44)
    monkeypatch.setattr(ae_sync, "get_or_create_role", lambda n, *a: 7)
    monkeypatch.setattr(ae_sync, "get_or_create_site", lambda n: 33)


def _patch_fetch(monkeypatch, records):
    stats = {"fetched": len(records), "skipped_no_type": 0,
             "skipped_no_serial": 0}
    monkeypatch.setattr(ae_sync, "ae_fetch_assets", lambda: (records, stats))


def test_creates_new_device_for_unknown_serial(monkeypatch):
    ep = FakeEndpoint([])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 1
    p = ep.created[0]
    assert p["serial"] == "6CU8474FTV"
    assert p["asset_tag"] == "41510140"
    assert p["custom_fields"]["ae_asset_id"] == "32"
    assert p["custom_fields"]["ae_department"] == "ICT"
    assert p["custom_fields"]["ae_location"] == "SHN R16 U15-16"


def test_asset_tag_synced_when_empty_in_netbox(monkeypatch):
    """Serial found + empty asset tag in NetBox -> populate tag from ME."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="",
                          custom_fields={"bmc_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.update_calls == 1
    upd = ep.updated[0]
    assert upd["id"] == 9
    assert upd["asset_tag"] == "41510140"


def test_existing_asset_tag_in_netbox_is_never_overwritten(monkeypatch):
    """Serial found + already has a tag in NetBox -> ME MUST NOT overwrite it."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="EXISTING-TAG",
                          custom_fields={"bmc_ip": "10.0.0.1",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(asset_tag="NEW-ME-TAG"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


def test_no_change_when_asset_tag_already_correct(monkeypatch):
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="41510140",
                          custom_fields={"bmc_ip": "10.0.0.1",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


def test_no_change_when_ae_tag_empty(monkeypatch):
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="KEEP-ME",
                          custom_fields={"bmc_ip": "10.0.0.1",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(asset_tag=None))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


def test_asset_tag_match_case_insensitive(monkeypatch):
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv",
                          asset_tag="41510140",
                          custom_fields={"ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(asset_tag="41510140"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.update_calls == 0


def test_skips_discovery_managed_device(monkeypatch):
    """Serial found, tag and department already set -> untouched."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="41510140",
                          custom_fields={"bmc_ip": "10.0.0.1",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


def test_skip_only_even_for_ae_owned_device(monkeypatch):
    """Serial found -> only missing fields may be filled; others never overwritten."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="old-name",
                          asset_tag="41510140",
                          custom_fields={"ae_asset_id": "32",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


def test_no_duplicate_when_ae_lists_same_serial_twice(monkeypatch):
    ep = FakeEndpoint([])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset())
    _patch_fetch(monkeypatch, [rec, dict(rec)])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 1   # second occurrence sees the created serial


def test_serial_match_is_case_insensitive(monkeypatch):
    """NetBox has 'MXQ62800GS', AE reports 'mxq62800gs' -> match, tag sync."""
    existing = FakeRecord(9, serial="MXQ62800GS", name="srv",
                          asset_tag="",
                          custom_fields={"ae_asset_id": "32"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(org_serial_number="mxq62800gs"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.update_calls == 1
    assert ep.updated[0]["asset_tag"] == "41510140"


def test_case_insensitive_serial_still_skips_discovery_managed(monkeypatch):
    """Case-differing serial matches an existing device -> never a duplicate."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="41510140",
                          custom_fields={"bmc_ip": "10.0.0.1",
                                         "ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(org_serial_number="6cu8474ftv"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


# ── create-time collision handling (from production run 2026-08-29) ─────────

class _CollisionEndpoint(FakeEndpoint):
    """Fails device create with the given NetBox 400 error once, then delegates."""
    def __init__(self, items=None, fail_payload=None):
        super().__init__(items)
        self.fail_payload = fail_payload

    def create(self, payload):
        if self.fail_payload is not None:
            err, self.fail_payload = self.fail_payload, None
            raise RuntimeError(f"The request failed with code 400 Bad Request: {err}")
        return super().create(payload)


def test_create_retries_without_tag_when_tag_taken(monkeypatch):
    ep = _CollisionEndpoint([], fail_payload={"asset_tag": [
        "device with this asset tag already exists."]})
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 1   # only the successful (tag-less) create counted
    assert "asset_tag" not in ep.created[0]


def test_name_collision_adopts_blank_serial_device(monkeypatch):
    """Blank-serial device in NetBox + AE asset with serial: the name fallback
    matches FIRST (before any create), so only the asset tag syncs and the
    create/adopt path is never reached — no duplicate, no serial hijack."""
    existing = FakeRecord(21, name="AFRA-HOST-06", serial="", site_id=33,
                          role_id=7, asset_tag=None, custom_fields={})
    ep = _CollisionEndpoint([existing], fail_payload={"__all__": [
        "Device name must be unique per site."]})
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(
        name="AFRA-HOST-06", org_serial_number="MXQ62800GS"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0                       # never creates
    assert ep.updated and ep.updated[0]["id"] == 21
    assert ep.updated[0]["asset_tag"] == "41510140"
    assert "name" not in ep.updated[0]
    assert "serial" not in ep.updated[0]


def test_name_collision_with_different_serial_is_skipped(monkeypatch):
    """Same name but a real different serial -> never hijack; counted as failure."""
    existing = FakeRecord(21, name="AFRA-HOST-06", serial="OTHER123", site_id=33,
                          role_id=7, asset_tag=None, custom_fields={})
    ep = _CollisionEndpoint([existing], fail_payload={"__all__": [
        "Device name must be unique per site."]})
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(
        name="AFRA-HOST-06", org_serial_number="MXQ62800GS"))])
    ae_sync.sync_assetexplorer()
    assert not any(u.get("serial") == "MXQ62800GS" for u in ep.updated)


def test_tag_conflict_on_update_is_warn_not_failure(monkeypatch, capsys):
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv", asset_tag="",
                          custom_fields={"ae_department": "ICT"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    ep.update = lambda rows: (_ for _ in ()).throw(RuntimeError(
        "400 Bad Request: {'asset_tag': ['device with this asset tag already exists.']}"))
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()   # must not raise
    assert "held by another" in capsys.readouterr().out


# ── name fallback (serial missing in NetBox) ────────────────────────────────

def test_name_fallback_syncs_tag_when_serial_missing_in_netbox(monkeypatch):
    """AE has serial MXQ62800GS but the NetBox device has blank serial —
    serial lookup misses, name fallback matches, asset tag synced, no create."""
    existing = FakeRecord(21, name="AFRA-HOST-06", serial="", asset_tag=None,
                          site=SimpleNamespace(name="Afranet"), custom_fields={})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="AFRA-HOST-06",
                            org_serial_number="MXQ62800GS"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.update_calls == 1
    # Only asset_tag and ae_department enriched; name/serial untouched
    assert ep.updated[0]["id"] == 21
    assert ep.updated[0]["asset_tag"] == "41510140"
    assert "name" not in ep.updated[0]
    assert "serial" not in ep.updated[0]


def test_name_fallback_ambiguous_name_skips(monkeypatch, capsys):
    """Two devices share the name, neither at the AE site -> skip everything."""
    ep = FakeEndpoint([
        FakeRecord(1, name="HOST-01", serial="", asset_tag=None,
                   site=SimpleNamespace(name="OtherA"), custom_fields={}),
        FakeRecord(2, name="HOST-01", serial="", asset_tag=None,
                   site=SimpleNamespace(name="OtherB"), custom_fields={}),
    ])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="HOST-01", org_serial_number="NEW123"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.update_calls == 0
    out = capsys.readouterr().out
    assert "ambiguous" in out


def test_name_fallback_with_site_disambiguation(monkeypatch):
    """Duplicate names but one device sits at the AE site -> match that one."""
    target = FakeRecord(2, name="HOST-01", serial="", asset_tag=None,
                        site=SimpleNamespace(name="Afranet"), custom_fields={})
    ep = FakeEndpoint([
        FakeRecord(1, name="HOST-01", serial="", asset_tag=None,
                   site=SimpleNamespace(name="OtherA"), custom_fields={}),
        target,
    ])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="HOST-01", org_serial_number="NEW123"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.update_calls == 1
    assert ep.updated[0]["id"] == 2
    assert "name" not in ep.updated[0]
    assert "serial" not in ep.updated[0]


def test_serial_less_ae_asset_matched_by_name_not_created(monkeypatch):
    """AE asset with no serial: name match -> tag sync; never created."""
    existing = FakeRecord(5, name="INV-THING", serial="", asset_tag=None,
                          site=SimpleNamespace(name="Afranet"), custom_fields={})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="INV-THING"))
    rec["serial"] = ""        # simulate serial-less AE asset
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated[0]["asset_tag"] == "41510140"


def test_serial_less_and_nameless_match_skips_cleanly(monkeypatch):
    """AE asset with no serial and no NetBox name match -> skipped, no create."""
    ep = FakeEndpoint([])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="GHOST-01"))
    rec["serial"] = ""
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0 and ep.update_calls == 0


# ── serial-conflict: stale serial on the wrong-named device ─────────────────

def test_serial_held_by_different_name_uses_name_fallback(monkeypatch, capsys):
    """NetBox has serial MXQ714055Z on 'Afra-Host-101' but AE says that serial
    belongs to 'AFRA-HOST-103'. Must NOT update Afra-Host-101's tag; must
    fall through to name matching and find the real AFRA-HOST-103."""
    wrong = FakeRecord(1, name="Afra-Host-101", serial="MXQ714055Z",
                       asset_tag="41510149",
                       site=SimpleNamespace(name="Afranet"),
                       custom_fields={"ae_department": "ICT"})
    right = FakeRecord(2, name="Afra-Host-103", serial="", asset_tag=None,
                       site=SimpleNamespace(name="Afranet"), custom_fields={})
    ep = FakeEndpoint([wrong, right])
    _patch_helpers(monkeypatch, ep)
    # AE has BOTH records — that's how the stale serial is detected
    rec_wrong = _normalize(_asset(name="AFRA-HOST-101", org_serial_number="USE6287HAY",
                                  asset_tag="41510149"))
    rec_right = _normalize(_asset(name="AFRA-HOST-103", org_serial_number="MXQ714055Z",
                                  asset_tag="41510148"))
    _patch_fetch(monkeypatch, [rec_wrong, rec_right])
    ae_sync.sync_assetexplorer()
    # wrong device untouched
    assert not any(u.get("id") == 1 for u in ep.updated)
    # right device got the tag; name/serial NOT overwritten
    upd = [u for u in ep.updated if u.get("id") == 2]
    assert upd and upd[0]["asset_tag"] == "41510148"
    assert "name" not in upd[0]
    assert "serial" not in upd[0]
    assert "held by" in capsys.readouterr().out


def test_serial_conflict_warns_and_does_not_hijack(monkeypatch, capsys):
    """AE says serial X belongs to name A, but NetBox has serial X on name B
    and AE also has name B with a DIFFERENT serial -> stale serial in NetBox.
    The tag for name A must NOT be written to device B."""
    wrong = FakeRecord(1, name="Afra-Host-101", serial="MXQ714055Z",
                       asset_tag="41510149",
                       site=SimpleNamespace(name="Afranet"), custom_fields={})
    ep = FakeEndpoint([wrong])
    _patch_helpers(monkeypatch, ep)
    # AE: AFRA-HOST-101 has serial USE6287HAY (the real one), and
    #     AFRA-HOST-103 has serial MXQ714055Z (which NetBox wrongly holds on 101)
    rec_a = _normalize(_asset(name="AFRA-HOST-101", org_serial_number="USE6287HAY",
                              asset_tag="41510149"))
    rec_b = _normalize(_asset(name="AFRA-HOST-103", org_serial_number="MXQ714055Z",
                              asset_tag="41510148"))
    _patch_fetch(monkeypatch, [rec_a, rec_b])
    ae_sync.sync_assetexplorer()
    # device id=1 must NOT get tag 41510148 (that belongs to AFRA-HOST-103)
    assert not any(u.get("asset_tag") == "41510148" for u in ep.updated)
    assert "stale serial" in capsys.readouterr().out


# ── inventory item idempotency ───────────────────────────────────────────────

def test_inventory_item_skipped_when_unchanged(monkeypatch):
    """Existing inventory item with identical fields -> no update (skipped)."""
    parent = FakeRecord(10, name="R16-ToR-SW02", serial="FOC2206X0K1", custom_fields={})
    existing_item = FakeRecord(50, device_id=10, name="LIT20250K6Y",
                               serial="LIT20250K6Y", role=8, manufacturer=5,
                               part_id="power switch",
                               description="Department: ICT | Asset Tag: 41510140")
    inv_ep = FakeEndpoint([existing_item])
    devices_ep = FakeEndpoint([parent])

    monkeypatch.setattr(ae_sync, "get_netbox",
                        lambda: _fake_api(devices=devices_ep, inventory_items=inv_ep))
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_inventory_role", lambda n: 8)

    rec = _normalize(_asset(
        name="LIT20250K6Y", org_serial_number="LIT20250K6Y",
        product_type={"name": "Power Module", "internal_name": None},
        product={"name": "power switch", "manufacturer": "CISCO"},
        used_by_asset={"name": "R16-ToR-SW02", "org_serial_number": "FOC2206X0K1"},
        description="",
    ))
    rc = ae_sync._ensure_inventory_item(
        _fake_api(devices=devices_ep, inventory_items=inv_ep),
        rec, {"foc2206x0k1": parent}, {"r16-tor-sw02": [parent]})
    assert rc == "skipped"
    assert inv_ep.update_calls == 0 and inv_ep.create_calls == 0


def test_inventory_item_updated_when_field_changes(monkeypatch):
    """Existing item with a different description -> updated (changed)."""
    parent = FakeRecord(10, name="R16-ToR-SW02", serial="FOC2206X0K1", custom_fields={})
    existing_item = FakeRecord(50, device_id=10, name="LIT20250K6Y",
                               serial="LIT20250K6Y", role=8, manufacturer=5,
                               description="old desc")
    inv_ep = FakeEndpoint([existing_item])
    devices_ep = FakeEndpoint([parent])

    monkeypatch.setattr(ae_sync, "get_netbox",
                        lambda: _fake_api(devices=devices_ep, inventory_items=inv_ep))
    monkeypatch.setattr(ae_sync, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(ae_sync, "get_or_create_inventory_role", lambda n: 8)

    rec = _normalize(_asset(
        name="LIT20250K6Y", org_serial_number="LIT20250K6Y",
        product_type={"name": "Power Module", "internal_name": None},
        used_by_asset={"name": "R16-ToR-SW02", "org_serial_number": "FOC2206X0K1"},
        description="new desc",
    ))
    rc = ae_sync._ensure_inventory_item(
        _fake_api(devices=devices_ep, inventory_items=inv_ep),
        rec, {"foc2206x0k1": parent}, {"r16-tor-sw02": [parent]})
    assert rc == "updated"
    assert inv_ep.update_calls == 1
    assert inv_ep.created == []


# ── hardware-suffix serial matching (Hikvision camera serials) ───────────────

def test_suffix_match_camera_serial(monkeypatch):
    """NVR-reported long serial in NetBox; ME stores the short hardware serial —
    the trailing-9-char suffix links them."""
    existing = FakeRecord(7, name="Cam GF-01",
                          serial="I20240302AAWRFB0225316", asset_tag=None,
                          site=SimpleNamespace(name="HQ"), custom_fields={})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="Cam GF-01", org_serial_number="FB0225316"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated[0]["id"] == 7
    assert ep.updated[0]["asset_tag"] == "41510140"


def test_suffix_match_ambiguous_skips(monkeypatch, capsys):
    """Two devices share the same trailing-9 suffix -> skip, never guess."""
    ep = FakeEndpoint([
        FakeRecord(1, name="Cam A", serial="A20240101AAWRFB0225316",
                   asset_tag=None, site=SimpleNamespace(name="HQ"),
                   custom_fields={}),
        FakeRecord(2, name="Cam B", serial="B20240202BBXXFB0225316",
                   asset_tag=None, site=SimpleNamespace(name="HQ"),
                   custom_fields={}),
    ])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="Cam A", org_serial_number="FB0225316"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.update_calls == 0 and ep.create_calls == 0
    assert "ambiguous" in capsys.readouterr().out


def test_exact_serial_still_wins_over_suffix(monkeypatch):
    """Exact serial match must take precedence; suffix never overrides it."""
    exact = FakeRecord(1, name="Cam Exact", serial="FB0225316", asset_tag=None,
                       site=SimpleNamespace(name="HQ"), custom_fields={})
    ep = FakeEndpoint([
        exact,
        FakeRecord(2, name="Cam Long", serial="I20240302AAWRFB0225316",
                   asset_tag=None, site=SimpleNamespace(name="HQ"),
                   custom_fields={}),
    ])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(name="Cam Exact", org_serial_number="FB0225316"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.updated[0]["id"] == 1
