"""Venture sync: ME udf_sline_901 -> NetBox `venture` custom field.

Covers:
- NetBox CUSTOM_FIELDS registry carries the venture field
- _normalize extracts venture from udf_sline_901 (and only for it)
- _payload carries venture into new devices
- enrichment fills venture ONLY when empty in NetBox, never overwrites
- ME writer helpers select exactly 4-digit-tag CCTV/Server/Storage Device assets
"""
from types import SimpleNamespace

from netbox_sync.collectors import assetexplorer as ae_mod
import netbox_sync.assetexplorer_sync as ae_sync
import netbox_sync.netbox as nb_mod
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api
from tests.test_assetexplorer import (_asset, _patch_helpers, _patch_fetch)
from netbox_sync.collectors.assetexplorer import _normalize


def _venture_cf_names():
    return [n for (n, _t, _l) in nb_mod.CUSTOM_FIELDS]


# ── registry ───────────────────────────────────────────────────────────────

def test_netbox_registry_has_venture_field():
    assert "venture" in _venture_cf_names()


def test_venture_field_type_and_label():
    row = [r for r in nb_mod.CUSTOM_FIELDS if r[0] == "venture"]
    assert row and row[0][1] == "text"
    assert "venture" in row[0][2].lower()


# ── normalization ─────────────────────────────────────────────────────────

def test_normalize_extracts_venture_from_udf_901():
    rec = _normalize(_asset(udf_fields={"udf_sline_901": "Central"}))
    assert rec["venture"] == "Central"


def test_normalize_venture_defaults_none_when_field_missing():
    rec = _normalize(_asset(udf_fields={"udf_sline_601": "960GB"}))
    assert rec["venture"] is None


def test_normalize_venture_defaults_none_when_no_udf():
    rec = _normalize(_asset())
    assert rec["venture"] is None


def test_normalize_venture_empty_string_becomes_none():
    rec = _normalize(_asset(udf_fields={"udf_sline_901": ""}))
    assert rec["venture"] is None


def test_normalize_venture_none_stays_none():
    rec = _normalize(_asset(udf_fields={"udf_sline_901": None}))
    assert rec["venture"] is None


def test_normalize_venture_preserved_for_all_types():
    for ptype in ("Server", "CCTV", "Storage Device"):
        rec = _normalize(_asset(product_type={"internal_name": ptype},
                                udf_fields={"udf_sline_901": "Central"}))
        assert rec["venture"] == "Central", ptype


# ── payload (new device creation) ──────────────────────────────────────────

def test_payload_carries_venture_into_custom_fields(monkeypatch):
    _patch_helpers(monkeypatch, FakeEndpoint([]))
    rec = _normalize(_asset(udf_fields={"udf_sline_901": "Central"}))
    p = ae_sync._payload(rec)
    assert p["custom_fields"]["venture"] == "Central"


def test_payload_venture_absent_value_when_none(monkeypatch):
    _patch_helpers(monkeypatch, FakeEndpoint([]))
    rec = _normalize(_asset())
    p = ae_sync._payload(rec)
    assert p["custom_fields"].get("venture") is None


# ── enrichment: fill empty venture, never overwrite ────────────────────────

def test_venture_filled_when_empty_in_netbox(monkeypatch):
    """Serial match + venture empty in NetBox -> populate from ME."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="1431",
                          custom_fields={"bmc_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(
        asset_tag="1431",
        udf_fields={"udf_sline_901": "Central"}))])
    ae_sync.sync_assetexplorer()
    upd = ep.updated[0]
    assert upd["custom_fields"]["venture"] == "Central"
    # existing cf must be preserved
    assert upd["custom_fields"]["bmc_ip"] == "10.0.0.1"


class _TagConflictEndpoint(FakeEndpoint):
    """Reject an update that includes a conflicting tag, allowing no-tag retry."""
    def update(self, items):
        if any("asset_tag" in u for u in items):
            raise Exception("asset tag already exists")
        return super().update(items)


def test_cab_survives_conflicting_asset_tag(monkeypatch):
    """An unrelated tag conflict must not discard the Venture update."""
    existing = FakeRecord(31, serial="811690091", name="F6-Dining Room",
                          asset_tag="35310551",
                          custom_fields={"ae_asset_id": "1577"})
    ep = _TagConflictEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_cab_asset_with_serial("811690091"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1, ep.updated
    upd = ep.updated[0]
    assert "asset_tag" not in upd
    assert upd["custom_fields"]["venture"] == "CAB"


def _box_asset_with_serial(serial, tag="35300061", product_type="CCTV",
                           name="15056-Zafar"):
    return _asset(id="9100", asset_tag=tag, name=name,
                  org_serial_number=serial,
                  product_type={"internal_name": product_type},
                  udf_fields={"udf_sline_901": "BOX"})


def test_box_rules_cover_requested_prefixes_only():
    box_cases = ["35300061", "35300123", "43100010", "44500001"]
    rejected_cases = ["3530012", "353001234", "3530006a", ""]
    for tag in box_cases:
        assert ae_sync._venture_assignment({"asset_tag": tag}) == ("BOX", True), tag
    assert ae_sync._venture_assignment({"asset_tag": "35310054"}) == ("CAB", True)
    assert ae_sync._venture_assignment({"asset_tag": "1431"}) == ("Central", False)
    # Four-digit prefixes remain Central, not BOX.
    assert ae_sync._venture_assignment({"asset_tag": "3530"}) == ("Central", False)
    assert ae_sync._venture_assignment({"asset_tag": "4310"}) == ("Central", False)
    assert ae_sync._venture_assignment({"asset_tag": "4450"}) == ("Central", False)
    assert ae_sync._venture_assignment({"asset_tag": "91010140"}) is None
    for tag in rejected_cases:
        assert ae_sync._venture_assignment({"asset_tag": tag}) is None, tag


def test_box_write_target_roles_include_camera_switch_ap():
    for product_type in ("CCTV", "Switch", "Access Point"):
        rec = _normalize(_box_asset_with_serial("SN123", product_type=product_type))
        assert ae_sync._is_venture_write_target(rec), product_type
    rec = _normalize(_box_asset_with_serial("SN123", product_type="Firewall", tag="35300061"))
    assert not ae_sync._is_venture_write_target(rec)


def test_box_overrides_existing_venture_on_serial_match_only(monkeypatch):
    existing = FakeRecord(41, serial="FB0225232", name="15056-Zafar",
                          asset_tag="35300061",
                          custom_fields={"venture": "Central",
                                         "cam_ip": "192.168.80.24",
                                         "cam_nvr": "GF-NVR"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_box_asset_with_serial("FB0225232"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1
    cf = ep.updated[0]["custom_fields"]
    assert cf["venture"] == "BOX"
    assert cf["cam_ip"] == "192.168.80.24"
    assert cf["cam_nvr"] == "GF-NVR"


def test_box_does_not_touch_different_serial_same_tag(monkeypatch):
    existing = FakeRecord(42, serial="FB0225230", name="Different Serial",
                          asset_tag="35300061",
                          custom_fields={"venture": "Central"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_box_asset_with_serial("FB0225232"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated


def test_box_idempotent_when_already_correct(monkeypatch):
    existing = FakeRecord(43, serial="FB0225232", name="15056-Zafar",
                          asset_tag="35300061",
                          custom_fields={"ae_department": "ICT",
                                         "venture": "BOX", "cam_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_box_asset_with_serial("FB0225232"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated


def _nxp_asset_with_serial(serial):
    return _asset(id="9200", asset_tag="35350001", name="GF0000192",
                  org_serial_number=serial,
                  product_type={"internal_name": "CCTV"},
                  udf_fields={"udf_sline_901": "NXP"})


def _new_rule_asset_with_serial(serial, tag, product_type, venture):
    return _asset(id="9300", asset_tag=tag, name="NewRule", org_serial_number=serial,
                  product_type={"internal_name": product_type},
                  udf_fields={"udf_sline_901": venture})


def test_new_venture_rules_cover_exact_requested_prefixes_and_values():
    expected = {
        "41400001": ("BOX", True),
        "43150001": ("NXP", True), "44550001": ("NXP", True),
        "43110001": ("CAB", True), "44510001": ("CAB", True),
        "48510001": ("CAB", True), "41410001": ("CAB", True),
        "41510001": ("CAB", True), "43140001": ("CAB", True),
        "30910001": ("CAB", True),
        # Existing rules must remain intact
        "35350001": ("NXP", True), "35300001": ("BOX", True),
        "35310001": ("CAB", True),
    }
    for tag, expected_rule in expected.items():
        assert ae_sync._venture_assignment({"asset_tag": tag}) == expected_rule, tag
    # Exact 4-digit prefixes remain Central, not the new 8-digit rule
    for tag in ("4140", "4315", "4455", "4311", "4451"):
        assert ae_sync._venture_assignment({"asset_tag": tag}) == ("Central", False), tag
    for tag in ("4140001", "414000001", "4140000a", ""):
        assert ae_sync._venture_assignment({"asset_tag": tag}) is None, tag


def test_new_venture_rules_admit_router_nvr_firewall_roles(monkeypatch):
    # New rules include product types present in ME for those prefixes.
    for product_type in ("NVR", "Firewall", "Router"):
        rec = _normalize(_new_rule_asset_with_serial("SN1", "41400001", product_type, "BOX"))
        assert ae_sync._is_venture_write_target(rec), product_type
    rec = _normalize(_new_rule_asset_with_serial("SN1", "42100001", "Server", None))
    assert not ae_sync._is_venture_write_target(rec)


def test_4140_box_overwrites_serial_matched_device_only(monkeypatch):
    existing = FakeRecord(61, serial="4C0048DPAJ8F3C9", name="Sadeghiyeh-NVR-04",
                          asset_tag="41400002", role="Storage",
                          custom_fields={"venture": "Central", "nvr_ip": "172.31.4.7"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_new_rule_asset_with_serial("4C0048DPAJ8F3C9", "41400002", "NVR", "BOX"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1
    cf = ep.updated[0]["custom_fields"]
    assert cf["venture"] == "BOX"
    assert cf["nvr_ip"] == "172.31.4.7"


def test_nxp_cab_prefixes_idempotency_and_no_other_serial(monkeypatch):
    matching = FakeRecord(62, serial="FS123", name="FS123", asset_tag="43150001",
                          custom_fields={"ae_department": "ICT", "venture": "NXP"})
    other = FakeRecord(63, serial="FS124", name="Other", asset_tag="43150001",
                       custom_fields={"venture": "BOX"})
    ep = FakeEndpoint([matching, other])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_new_rule_asset_with_serial("FS123", "43150001", "CCTV", "NXP"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated


def test_nxp_rule_covers_exact_3535_prefixed_eight_digit_tags_only():
    for tag in ("35350001", "35350040"):
        assert ae_sync._venture_assignment({"asset_tag": tag}) == ("NXP", True), tag
    assert ae_sync._venture_assignment({"asset_tag": "3535"}) == ("Central", False)
    assert ae_sync._venture_assignment({"asset_tag": "3535000"}) is None
    assert ae_sync._venture_assignment({"asset_tag": "353500000"}) is None
    assert ae_sync._venture_assignment({"asset_tag": "3535000a"}) is None
    assert ae_sync._venture_assignment({"asset_tag": "35300061"}) == ("BOX", True)


def test_nxp_sync_writes_box_value_only_for_matching_serial(monkeypatch):
    existing = FakeRecord(51, serial="GF0000192", name="GF0000192",
                          asset_tag="35350001",
                          custom_fields={"venture": "Central", "cam_ip": "10.19.4.10"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_nxp_asset_with_serial("GF0000192"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1
    cf = ep.updated[0]["custom_fields"]
    assert cf["venture"] == "NXP"
    assert cf["cam_ip"] == "10.19.4.10"


def test_nxp_ids_idempotent_and_no_cross_serial_touch(monkeypatch):
    matching = FakeRecord(52, serial="GF0000192", name="GF0000192",
                          asset_tag="35350001",
                          custom_fields={"ae_department": "ICT", "venture": "NXP"})
    other = FakeRecord(53, serial="GF0000193", name="Other Camera",
                       asset_tag="35350002",
                       custom_fields={"venture": "Central"})
    ep = FakeEndpoint([matching, other])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_nxp_asset_with_serial("GF0000192"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated


def _cab_asset_with_serial(serial):
    return _asset(id="9000", asset_tag="35310054", name="MLSD-Real-Camera",
                  org_serial_number=serial,
                  product_type={"internal_name": "CCTV"},
                  department={"name": "ICT"},
                  udf_fields={"udf_sline_901": "CAB"})


def test_targeted_cab_overrides_existing_venture_only(monkeypatch):
    """For an exact 3531nnnn ME tag, CAB must replace whatever Venture the
    matching NetBox device already has; other device fields do not change."""
    existing = FakeRecord(20, serial="743355394", name="MLSD-Real-Camera",
                          asset_tag="35310054", role="Camera", site="HQ",
                          custom_fields={"venture": "Central",
                                         "cam_nvr": "MLSD-NVR-01",
                                         "cam_ip": "192.168.62.1",
                                         "bmc_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_cab_asset_with_serial("743355394"))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1
    upd = ep.updated[0]
    assert upd["id"] == 20
    cf = upd["custom_fields"]
    assert cf["venture"] == "CAB"
    assert cf["cam_nvr"] == "MLSD-NVR-01"
    assert cf["cam_ip"] == "192.168.62.1"
    assert cf["bmc_ip"] == "10.0.0.1"
    assert "asset_tag" not in upd and "serial" not in upd and "name" not in upd


def test_targeted_cab_does_not_touch_different_serial_device(monkeypatch):
    """A shared 8-digit tag must never cross-link a different serial."""
    existing = FakeRecord(21, serial="C79938095", name="Other-Camera",
                          site=SimpleNamespace(name="HQ-Other"),
                          asset_tag="35310054",
                          custom_fields={"venture": "Central"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_cab_asset_with_serial("743355394"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated


def test_targeted_cab_update_is_idempotent_when_already_correct(monkeypatch):
    existing = FakeRecord(20, serial="743355394", name="MLSD-Real-Camera",
                          asset_tag="35310054",
                          custom_fields={"ae_department": "ICT",
                                         "venture": "CAB",
                                         "cam_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_cab_asset_with_serial("743355394"))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == [], ep.updated
    """NetBox already has a venture value -> AE must NOT overwrite it."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="91010140",
                          custom_fields={"venture": "East"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(
        asset_tag="91010140",
        udf_fields={"udf_sline_901": "Central"}))])
    ae_sync.sync_assetexplorer()
    # Other field updates are fine; the assertion is on venture only.
    for upd in ep.updated:
        cf = upd.get("custom_fields") or {}
        assert cf.get("venture") == "East", upd


def test_venture_absent_me_value_is_noop(monkeypatch):
    """ME has no venture (None) -> never writes a venture key."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="91010140", custom_fields={})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset())])
    ae_sync.sync_assetexplorer()
    for upd in ep.updated:
        cf = upd.get("custom_fields") or {}
        assert "venture" not in cf


# ── ME write-target selection (Central + exact 8-digit Venture rules) ───────

def test_is_venture_write_target_playbook():
    cases = [
        ("1431", "Server", True),
        ("1431", "CCTV", True),
        ("2221", "Storage Device", True),
        ("91010140", "Server", False),     # no Venture rule
        ("1431", "Switch", False),         # Central stays original scope
        ("1431", "Access Point", False),
        ("35300061", "Switch", True),      # exact BOX prefix/type in scope
        ("43100010", "Switch", True),
        ("44500001", "CCTV", True),
        ("44500001", "Access Point", True),
        ("35310054", "CCTV", True),
        ("35310054", "Firewall", False),
        ("", "Server", False),
        (None, "Server", False),
        ("13", "Server", False),           # too short
        ("14315", "Server", False),        # too long
        ("Workstation", "Server", False),
    ]
    for tag, ptype, expected in cases:
        rec = _normalize(_asset(asset_tag=tag,
                                product_type={"internal_name": ptype}))
        if ptype == "Workstation":  # unmapped type -> _normalize drops it
            continue
        assert ae_sync._is_venture_write_target(rec) == expected, (tag, ptype)
