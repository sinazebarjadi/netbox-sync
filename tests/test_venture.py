"""Venture sync: ME udf_sline_901 -> NetBox custom field.

ME supplies Venture dynamically; no Asset Tag prefix rules or static values.
"""
from netbox_sync.collectors.assetexplorer import _normalize
import netbox_sync.assetexplorer_sync as ae_sync
import netbox_sync.netbox as nb_mod
from tests.test_assetexplorer import (_asset, _patch_helpers, _patch_fetch)
from tests.test_netbox_sync import FakeEndpoint, FakeRecord


def test_venture_is_registered_as_custom_field_on_device():
    row = next(r for r in nb_mod.CUSTOM_FIELDS if r[0] == "venture")
    assert row[1] == "text"
    assert row[2] == "Venture (AssetExplorer)"


def test_venture_read_from_me_udf_fields():
    rec = _normalize(_asset(udf_fields={"udf_sline_901": "Central"}))
    assert rec["venture"] == "Central"


def test_venture_normalization_absent_values():
    for udf in (None, {}, {"udf_sline_901": ""}, {"udf_sline_901": None},
                {"udf_sline_901": "   "}, {"udf_sline_601": "960GB"}):
        rec = _asset()
        if udf is not None:
            rec["udf_fields"] = udf
        assert _normalize(rec)["venture"] is None


def test_venture_passed_through_on_new_device_payload(monkeypatch):
    _patch_helpers(monkeypatch, FakeEndpoint([]))
    rec = _normalize(_asset(udf_fields={"udf_sline_901": "NXP"}))
    p = ae_sync._payload(rec)
    assert p["custom_fields"]["venture"] == "NXP"
    assert p["custom_fields"]["ae_asset_id"] == rec["ae_id"]


def test_venture_enrichment_uses_me_value_not_tag(monkeypatch):
    """Venture fills empty only and preserves every other custom field."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="35310054",
                          custom_fields={"cam_nvr": "HQ-NVR-01", "bmc_ip": "10.0.0.1"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(asset_tag="35310054",
                            udf_fields={"udf_sline_901": "BOX"}))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert len(ep.updated) == 1
    upd = ep.updated[0]
    cf = upd["custom_fields"]
    assert cf["venture"] == "BOX"
    assert cf["cam_nvr"] == "HQ-NVR-01"
    assert cf["bmc_ip"] == "10.0.0.1"


def test_venture_existing_value_preserved_when_me_changes(monkeypatch):
    """Same as Department: ME enriches an empty field, never overwrites."""
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="35310054", custom_fields={"venture": "Central"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(asset_tag="35310054",
                            udf_fields={"udf_sline_901": "NXP"}))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    for upd in ep.updated:
        assert upd["custom_fields"]["venture"] == "Central"


def test_blank_me_venture_does_not_write_venture(monkeypatch):
    for udf in ({}, {"udf_sline_901": None}, {"udf_sline_901": ""}, {"udf_sline_901": "   "}):
        existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                              asset_tag="91010140", custom_fields={})
        ep = FakeEndpoint([existing])
        _patch_helpers(monkeypatch, ep)
        rec = _normalize(_asset(asset_tag="91010140", udf_fields=udf))
        _patch_fetch(monkeypatch, [rec])
        ae_sync.sync_assetexplorer()
        for upd in ep.updated:
            assert "venture" not in upd.get("custom_fields", {})


def test_existing_venture_idempotent(monkeypatch):
    existing = FakeRecord(9, serial="6CU8474FTV", name="srv-real",
                          asset_tag="91010140",
                          custom_fields={"ae_department": "ICT", "venture": "CAB"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    _patch_fetch(monkeypatch, [_normalize(_asset(
        asset_tag="91010140", udf_fields={"udf_sline_901": "CAB"}))])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert ep.updated == []


def test_different_serial_never_receives_venture_even_same_tag(monkeypatch):
    existing = FakeRecord(9, serial="OTHER-SERIAL", name="Other",
                          asset_tag="91010140", custom_fields={"venture": "Central"})
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(asset_tag="91010140", udf_fields={"udf_sline_901": "NXP"}))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.updated == []


def test_no_rule_api_or_static_prefix_rules_remain():
    assert not hasattr(ae_sync, "_VENTURE_RULES")
    assert not hasattr(ae_sync, "_venture_assignment")
    assert not hasattr(ae_sync, "_is_venture_write_target")
