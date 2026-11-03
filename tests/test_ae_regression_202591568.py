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


def test_identical_names_different_sites_do_not_trigger_stale_serial(monkeypatch):
    """Root cause regression (live 202591568): another AE asset shares the
    NetBox device's *name* ('Salon') but lives at a different site (Ardabil).
    That must NOT invalidate the exact serial match for the Khorramabad
    device — the stale-serial guard is site-scoped. Expected: match the
    existing automation camera, update only its missing tag, create nothing."""
    from types import SimpleNamespace
    existing = FakeRecord(
        11561, serial="202591568", name="Salon", asset_tag=None,
        site_id=99, site=SimpleNamespace(name="Khorramabad"),
        custom_fields={
            "cam_serial": "202591568",
            "cam_nvr": "Khorramabad-NVR",
            "cam_channel": 4,
            "cam_ip": "192.168.88.71",
        },
    )
    ep = FakeEndpoint([existing])
    _patch_helpers(monkeypatch, ep)
    khorramabad_rec = _normalize(_asset(
        id="4250",
        name="202591568",
        org_serial_number="202591568",
        asset_tag="35310436",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "Khorramabad"},
    ))
    ardebil_rec = _normalize(_asset(
        id="9999",
        name="Salon",
        org_serial_number="811685318",
        asset_tag="35310399",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "Ardabil"},
    ))
    _patch_fetch(monkeypatch, [khorramabad_rec, ardebil_rec])
    ae_sync.sync_assetexplorer()
    assert ep.create_calls == 0, f"created {ep.created} instead of matching id=11561"
    assert ep.update_calls == 1
    assert ep.updated[0]["id"] == 11561
    assert ep.updated[0]["asset_tag"] == "35310436"


def test_live_camera_never_vetoed_by_me_naming(monkeypatch):
    """Live NVR-discovered camera holds serial FK3348988 as 'F12.5-Stairs_Down'.
    ME has a DIFFERENT asset 'F12.5-Stairs_Down' with serial FK3348917 at the
    same site (HQ) — a genuine ME naming collision, not a stale serial.
    AE must keep the automation record and only fill missing fields."""
    automation_cam = FakeRecord(
        9601, serial="FK3348988", name="F12.5-Stairs_Down", asset_tag=None,
        custom_fields={
            "cam_serial": "FK3348988",
            "cam_nvr": "HQ-NVR-03",
            "cam_channel": 21,
            "cam_ip": "172.31.140.12",
        },
    )
    ep = FakeEndpoint([automation_cam])
    _patch_helpers(monkeypatch, ep)
    # AE record for the OTHER camera with the same title
    rec_alias = _normalize(_asset(
        id="4240",
        name="F12.5-Stairs_Down",
        org_serial_number="FK3348917",
        asset_tag="2124",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "HQ"},
    ))
    # AE record assigning OUR serial to a different name
    rec_mine = _normalize(_asset(
        id="735",
        name="ZAFR-F12.5-Emergency Stairs_Up",
        org_serial_number="FK3348988",
        asset_tag="2135",
        product_type={"internal_name": None, "name": "CCTV"},
        site={"name": "HQ"},
    ))
    _patch_fetch(monkeypatch, [rec_mine, rec_alias])
    ae_sync.sync_assetexplorer()
    # no new device may be created
    assert ep.create_calls == 0, f"created {ep.created}"
    # the live camera keeps its identity (serial NEVER rewritten by name);
    # the serial-matched record contributes its own tag. The alias record
    # (different serial) must not change this device's identity, and must
    # not be used to adopt or create anything here.
    upd = [u for u in ep.updated if u.get("id") == 9601]
    assert upd, "live camera must be updated by its serial-matched record"
    assert upd[0]["asset_tag"] == "2135"
    assert all(u.get("serial", "FK3348988") == "FK3348988" for u in ep.updated)
    assert not any(u.get("id") != 9601 for u in ep.updated)


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

