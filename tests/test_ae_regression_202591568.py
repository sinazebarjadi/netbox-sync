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

