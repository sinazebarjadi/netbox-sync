import pytest
import netbox_sync.assetexplorer_sync as ae_sync
from netbox_sync.collectors.assetexplorer import _normalize
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api
from tests.test_assetexplorer import _patch_helpers, _patch_fetch, _asset


def test_camera_serial_match_wins_over_name_mismatch(monkeypatch):
    """Live regression: serial 202591568 exists on automation camera 'Salon'.
    AE record name is '202591568'. The AE sync must match by serial (not create)."""
    existing = FakeRecord(
        11561, serial="202591568", name="Salon", asset_tag=None,
        custom_fields={
            "cam_serial": "202591568",
            "cam_nvr": "Khorramabad-NVR",
            "cam_channel": 4,
            "cam_ip": "192.168.88.71",
        },
    )
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(
        id="4250",
        name="202591568",
        org_serial_number="202591568",
        asset_tag="35310436",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "Khorramabad"},
    ))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    # must NOT create a duplicate — updating the existing record is correct
    assert ep.create_calls == 0, (
        f"created {ep.created} for serial already on automation camera id=11561"
    )
    # existing record has no tag yet, so exactly one update populating it
    assert ep.update_calls == 1
    assert ep.updated[0]["id"] == 11561
    assert ep.updated[0]["asset_tag"] == "35310436"


def test_existing_index_prefers_automation_camera_over_me_placeholder(monkeypatch):
    """Index arbitration: both a placeholder ('202591568', no cam evidence) and
    the automation camera ('Salon', cam_serial set) share the serial. The AE
    sync must resolve to the automation camera — the placeholder must never
    become the indexed record."""
    automation_cam = FakeRecord(
        11561, serial="202591568", name="Salon", asset_tag=None,
        custom_fields={
            "cam_serial": "202591568",
            "cam_nvr": "Khorramabad-NVR",
            "cam_channel": 4,
            "cam_ip": "192.168.88.71",
            "ae_asset_id": "4250",
            "ae_department": "ICT",
        },
    )
    placeholder = FakeRecord(
        11597, serial="202591568", name="202591568", asset_tag=None,
        custom_fields={
            "ae_asset_id": "4250",
            "ae_department": "ICT",
        },
    )
    ep = FakeEndpoint([automation_cam, placeholder])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(
        id="4250",
        name="202591568",
        org_serial_number="202591568",
        asset_tag="35310436",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "Khorramabad"},
    ))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    # must NOT create another device no matter which copy is in the index
    assert ep.create_calls == 0, f"created: {ep.created}"
    # and must update the AUTOMATION camera (id=11561), not the placeholder
    upd = [u for u in ep.updated if u.get("id") == 11561]
    assert upd and upd[0]["asset_tag"] == "35310436"
    assert not any(u.get("id") == 11597 for u in ep.updated)


def test_camera_serial_match_never_overwrites_existing_tag(monkeypatch):
    """Same invariant, tag side: when the automation record already carries an
    asset tag, AE must not overwrite it (a missing-field enrichment such as
    ae_department may still apply — that does not violate tag authority)."""
    existing = FakeRecord(
        11561, serial="202591568", name="Salon", asset_tag="EXISTING-TAG",
        custom_fields={
            "cam_serial": "202591568",
            "cam_nvr": "Khorramabad-NVR",
            "cam_channel": 4,
            "cam_ip": "192.168.88.71",
        },
    )
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    rec = _normalize(_asset(
        id="4250",
        name="202591568",
        org_serial_number="202591568",
        asset_tag="35310436",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "Khorramabad"},
    ))
    _patch_fetch(monkeypatch, [rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0
    assert not any(u.get("asset_tag") == "35310436" for u in ep.updated)
    if ep.updated:
        # only missing-field enrichment is allowed; the tag is not among them
        assert not any("asset_tag" in u for u in ep.updated)

