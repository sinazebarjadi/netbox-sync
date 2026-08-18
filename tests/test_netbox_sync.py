"""Tests for NetBox-facing sync logic, using in-memory fakes (no network).

Covers:
- sync_inventory reconciliation semantics (stale deletion, duplicate
  cleanup, update-vs-create) -- characterization tests guarding the refactor
- get_or_create_inventory_role name-based resolution + caching
- inventory role resolution at collector call sites (regression guard for
  the hardcoded ROLE_* ID migration)
- config validation, log-level filtering, TLS/SSH security options
"""
from types import SimpleNamespace

import pytest

import netbox_sync.collectors.brocade as brc
import netbox_sync.collectors.msa as msa
import netbox_sync.config as cfg
import netbox_sync.netbox as nbx
from netbox_sync import utils


# ── In-memory pynetbox fakes ─────────────────────────────────────────────────

class FakeRecord:
    def __init__(self, id, endpoint=None, **fields):
        self.id = id
        self._endpoint = endpoint
        self.deleted = False
        for k, v in fields.items():
            setattr(self, k, v)

    def delete(self):
        self.deleted = True
        if self._endpoint is not None:
            self._endpoint.deleted_ids.append(self.id)


class FakeEndpoint:
    """Mimics a pynetbox endpoint: filter/get/create/update."""

    def __init__(self, items=None):
        self.items = list(items or [])
        for i in self.items:
            i._endpoint = self
        self.created = []
        self.updated = []
        self.deleted_ids = []
        self.create_calls = 0   # invocation counts — proves bulk usage
        self.update_calls = 0
        self._next_id = 9000

    def _alive(self):
        return [i for i in self.items if not i.deleted]

    def filter(self, **kwargs):
        return [i for i in self._alive()
                if all(self._match(i, k, v) for k, v in kwargs.items())]

    @staticmethod
    def _match(rec, key, value):
        val = getattr(rec, key, None)
        if val is None and key.startswith("cf_"):
            val = (getattr(rec, "custom_fields", None) or {}).get(key[3:])
        return val == value

    def get(self, **kwargs):
        matches = self.filter(**kwargs)
        return matches[0] if matches else None

    def create(self, payload):
        self.create_calls += 1
        payloads = payload if isinstance(payload, list) else [payload]
        self.created.extend(payloads)
        records = []
        for p in payloads:
            rec = FakeRecord(self._next_id, endpoint=self, **p)
            self._next_id += 1
            # NetBox's device_id filter matches the device relation — model it
            if not hasattr(rec, "device_id") and hasattr(rec, "device"):
                rec.device_id = rec.device
            # same for the site/role relation filters
            if not hasattr(rec, "site_id") and hasattr(rec, "site"):
                rec.site_id = rec.site
            if not hasattr(rec, "role_id") and hasattr(rec, "role"):
                rec.role_id = rec.role
            self.items.append(rec)
            records.append(rec)
        return records if isinstance(payload, list) else records[0]

    def update(self, payload_list):
        self.update_calls += 1
        self.updated.extend(payload_list)
        # Apply updates to records (NetBox mutates them server-side)
        by_id = {i.id: i for i in self.items}
        for p in payload_list:
            rec = by_id.get(p.get("id"))
            if rec is not None:
                for k, v in p.items():
                    if k != "id":
                        setattr(rec, k, v)
        return True


@pytest.fixture(autouse=True)
def _clear_role_cache():
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()
    yield
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()


def _fake_api(**endpoints):
    return SimpleNamespace(dcim=SimpleNamespace(**endpoints))


# ── sync_inventory reconciliation ────────────────────────────────────────────

def _item(serial, name="Item"):
    return {"name": name, "manufacturer": "HPE", "part_number": "PN",
            "serial": serial, "description": "", "role": 4}


def test_sync_inventory_deletes_stale_and_dupes_and_upserts(monkeypatch):
    existing = [
        FakeRecord(1, serial="KEEP", device_id=7),
        FakeRecord(2, serial="STALE", device_id=7),
        FakeRecord(3, serial="DUP", device_id=7),
        FakeRecord(4, serial="DUP", device_id=7),
    ]
    ep = FakeEndpoint(existing)
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    nbx.sync_inventory(7, {"KEEP": _item("KEEP"),
                           "DUP": _item("DUP"),
                           "NEW": _item("NEW")})

    # STALE removed; both DUP duplicates removed
    assert set(ep.deleted_ids) == {2, 3, 4}
    # KEEP updated in place, DUP + NEW created fresh
    assert {u["id"] for u in ep.updated} == {1}
    assert {c["serial"] for c in ep.created} == {"DUP", "NEW"}
    # Exactly one live item per serial at the end
    live_serials = sorted(i.serial for i in ep._alive())
    assert live_serials == ["DUP", "KEEP", "NEW"]


def test_sync_inventory_single_fetch_and_no_per_item_get(monkeypatch):
    """The refactor must not re-fetch the list or .get() per item (N+1)."""
    ep = FakeEndpoint([FakeRecord(1, serial="A", device_id=7)])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    filter_calls = []
    orig_filter = ep.filter
    def counting_filter(**kw):
        filter_calls.append(kw)
        return orig_filter(**kw)
    ep.filter = counting_filter
    def fail_get(**kw):
        raise AssertionError("per-item .get() must not be used")
    ep.get = fail_get

    nbx.sync_inventory(7, {"A": _item("A"), "B": _item("B")})
    assert len(filter_calls) == 1


# ── ensure_server_device blank-serial adoption (no duplicates) ───────────────

def _server_probe(**over):
    p = {"serial": "", "manufacturer": "HPE", "hostname": "Afra-Host-06",
         "ip": "192.168.19.43", "model": "ProLiant DL380 Gen9"}
    p.update(over)
    return p


def _patch_server_helpers(monkeypatch, devices_ep):
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 5)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 7)
    monkeypatch.setattr(nbx, "resolve_site", lambda hn, ip: "NewSite")
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 33)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a: 44)


def test_blank_serial_server_adopted_by_bmc_ip(monkeypatch):
    """Hostname AND site both changed, serial blank: the BMC IP is the only
    stable identity — the existing device must be adopted, not duplicated."""
    existing = FakeRecord(11, name="old-hostname", role_id=7, site_id=99,
                          serial="", custom_fields={"bmc_ip": "192.168.19.43"})
    ep = FakeEndpoint([existing])
    _patch_server_helpers(monkeypatch, ep)

    dev_id = nbx.ensure_server_device(_server_probe())
    assert dev_id == 11
    assert ep.create_calls == 0
    assert ep.updated and ep.updated[0]["site"] == 33  # moved to current site


def test_blank_serial_server_adopted_by_name_any_site(monkeypatch):
    """Site mapping drifted (Default vs Afranet): same name+role elsewhere is
    adopted instead of creating a duplicate."""
    existing = FakeRecord(12, name="Afra-Host-06", role_id=7, site_id=99,
                          serial="", custom_fields={"bmc_ip": "10.9.9.9"})
    ep = FakeEndpoint([existing])
    _patch_server_helpers(monkeypatch, ep)

    dev_id = nbx.ensure_server_device(_server_probe())
    assert dev_id == 12
    assert ep.create_calls == 0


def test_blank_serial_server_ambiguous_name_creates(monkeypatch):
    """Two different-site devices share the name and no BMC IP matches —
    ambiguous, so a new device is created rather than merging blindly."""
    ep = FakeEndpoint([
        FakeRecord(13, name="Afra-Host-06", role_id=7, site_id=1,
                   serial="", custom_fields={"bmc_ip": "10.1.1.1"}),
        FakeRecord(14, name="Afra-Host-06", role_id=7, site_id=2,
                   serial="", custom_fields={"bmc_ip": "10.2.2.2"}),
    ])
    _patch_server_helpers(monkeypatch, ep)

    nbx.ensure_server_device(_server_probe())
    assert ep.create_calls == 1


# ── inventory item role resolution ───────────────────────────────────────────

def _roles_endpoint():
    return FakeEndpoint([
        FakeRecord(42, name="HDD", slug="hdd"),
        FakeRecord(43, name="SSD", slug="ssd"),
        FakeRecord(44, name="PSU", slug="psu"),
        FakeRecord(45, name="Controller", slug="controller"),
        FakeRecord(46, name="SAS Exp", slug="sas-exp"),
        FakeRecord(47, name="SFP", slug="sfp"),
        FakeRecord(48, name="Fan", slug="fan"),
        FakeRecord(49, name="Module", slug="module"),
    ])


def test_inventory_role_resolved_by_name_and_cached(monkeypatch):
    ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    rid1 = nbx.get_or_create_inventory_role("HDD")
    rid2 = nbx.get_or_create_inventory_role("HDD")

    assert rid1 == rid2
    assert len(ep.created) == 1
    assert ep.created[0]["name"] == "HDD"
    assert ep.created[0]["slug"] == "hdd"


def test_inventory_role_finds_existing_by_name(monkeypatch):
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))
    assert nbx.get_or_create_inventory_role("SSD") == 43
    assert ep.created == []


def test_manufacturer_lookup_is_cached(monkeypatch):
    """Repeated get_or_create_manufacturer calls must not re-hit the API
    (inventory sync calls it once per item — N+1 without caching)."""
    ep = FakeEndpoint([FakeRecord(7, name="HPE", slug="hpe")])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(manufacturers=ep))
    calls = []
    orig_get = ep.get
    def counting_get(**kw):
        calls.append(kw)
        return orig_get(**kw)
    ep.get = counting_get

    assert nbx.get_or_create_manufacturer("HPE") == 7
    assert nbx.get_or_create_manufacturer("HPE") == 7
    assert len(calls) == 1


def test_device_role_lookup_is_cached(monkeypatch):
    ep = FakeEndpoint([FakeRecord(3, name="Server", slug="server")])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(device_roles=ep))
    calls = []
    orig_get = ep.get
    def counting_get(**kw):
        calls.append(kw)
        return orig_get(**kw)
    ep.get = counting_get

    assert nbx.get_or_create_role("Server") == 3
    assert nbx.get_or_create_role("Server") == 3
    assert len(calls) == 1


def test_storage_collectors_resolve_roles_by_name(monkeypatch):
    """Collector call sites must use name-resolved role IDs, not hardcoded
    constants (regression guard for the ROLE_* migration)."""
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    inv = {}
    add = utils._make_add_item(inv)
    msa._collect_disk_storage(
        {"serial-number": "D1", "drive-type": "SAS", "size": "1.8TB"}, add)
    msa._collect_disk_storage(
        {"serial-number": "D2", "drive-type": "SSD", "size": "480GB"}, add)
    msa._collect_psu_storage({"serial-number": "P1", "location": "1.1"}, add)
    msa._collect_controller_storage(
        {"serial-number": "C1", "controller-id": "A"}, add)
    msa._collect_fru_storage({"serial-number": "F1", "fru-name": "Exp"}, add)

    assert inv["D1"]["role"] == 42   # HDD by name
    assert inv["D2"]["role"] == 43   # SSD by name
    assert inv["P1"]["role"] == 44   # PSU by name
    assert inv["C1"]["role"] == 45   # Controller by name
    assert inv["F1"]["role"] == 46   # SAS Exp by name
    assert ep.created == []          # all resolved, none created


# ── Cisco inventory role classification ──────────────────────────────────────

def test_cisco_inventory_roles_classified(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    inv = {}
    add = utils._make_add_item(inv)
    cisco._inventory_item_from_row(
        {"name": "Power Supply Module 0", "descr": "350W AC Power Supply",
         "pid": "PWR-C1-350WAC", "vid": "V01", "sn": "LIT23456789"}, add)
    cisco._inventory_item_from_row(
        {"name": "Fan Tray 0", "descr": "Fan Tray",
         "pid": "C9300-FAN-1", "vid": "V01", "sn": "FAN123456"}, add)
    cisco._inventory_item_from_row(
        {"name": "GigabitEthernet1/1/1", "descr": "1000BaseSX SFP",
         "pid": "GLC-SX-MMD", "vid": "V01", "sn": "FNS12345678"}, add)
    cisco._inventory_item_from_row(
        {"name": "Switch 1", "descr": "C9300-48U",
         "pid": "C9300-48U", "vid": "V02", "sn": "FOC2345X0AB"}, add)

    assert inv["LIT23456789"]["role"] == 44   # PSU
    assert inv["FAN123456"]["role"] == 48     # Fan
    assert inv["FNS12345678"]["role"] == 47   # SFP
    assert inv["FOC2345X0AB"]["role"] == 49   # Module
    assert inv["LIT23456789"]["part_number"] == "PWR-C1-350WAC"
    assert ep.created == []


# ── Cisco device ensure ──────────────────────────────────────────────────────

def test_ensure_cisco_device_creates_with_custom_fields(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    dev_id = nbx.ensure_cisco_device({
        "ip": "192.0.2.65", "serial": "FOC2345X0AB", "model": "C9300-48U",
        "hostname": "SW1", "manufacturer": "Cisco", "firmware": "16.9.4",
    })
    assert len(devices_ep.created) == 1
    payload = devices_ep.created[0]
    assert payload["serial"] == "FOC2345X0AB"
    assert payload["status"] == "active"
    assert payload["custom_fields"]["cisco_ip"] == "192.0.2.65"
    assert payload["custom_fields"]["cisco_enabled"] is True
    assert payload["custom_fields"]["cisco_model"] == "C9300-48U"
    assert dev_id is not None


# ── Cisco interface sync ─────────────────────────────────────────────────────

def test_sync_cisco_interfaces_update_create_delete(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="Gi1/0/1", device_id=7),
        FakeRecord(2, name="Gi1/0/9", device_id=7),   # stale -> deleted
    ])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    ports = [
        {"port": "Gi1/0/1", "name": "Uplink", "status": "connected",
         "vlan": "trunk", "duplex": "full", "speed": "1000",
         "type": "1000BaseSX SFP"},
        {"port": "Gi1/0/2", "name": "", "status": "notconnect",
         "vlan": "1", "duplex": "auto", "speed": "auto",
         "type": "10/100/1000BaseTX"},
    ]
    cisco.sync_cisco_interfaces(7, ports)

    assert {u["id"] for u in ifaces_ep.updated} == {1}
    assert ifaces_ep.updated[0]["type"] == "1000base-x-sfp"
    assert ifaces_ep.updated[0]["enabled"] is True
    assert len(ifaces_ep.created) == 1
    assert ifaces_ep.created[0]["name"] == "Gi1/0/2"
    assert ifaces_ep.created[0]["type"] == "other"
    assert ifaces_ep.created[0]["enabled"] is False
    assert ifaces_ep.deleted_ids == [2]
    # bulk: one HTTP call per operation, not one per interface
    assert ifaces_ep.update_calls == 1
    assert ifaces_ep.create_calls == 1


def test_interface_and_vlan_syncs_are_bulk(monkeypatch):
    """Performance guard: N items must sync in O(1) HTTP calls, not O(N)."""
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(i, name=f"Gi1/0/{i}", device_id=7) for i in range(1, 8)
    ])
    api = SimpleNamespace(
        dcim=SimpleNamespace(interfaces=ifaces_ep),
        ipam=SimpleNamespace(vlans=FakeEndpoint()))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [{"port": f"Gi1/0/{i}", "name": "", "status": "connected",
              "vlan": "10", "duplex": "full", "speed": "1000",
              "type": "10/100/1000BaseTX"} for i in range(1, 8)]
    cisco.sync_cisco_interfaces(7, ports)
    assert ifaces_ep.update_calls == 1          # 7 interfaces, 1 bulk PATCH

    cisco.sync_interface_vlans(7, ports, [], {10: 110})
    assert ifaces_ep.update_calls == 2          # +1 more bulk PATCH for all VLANs


def test_inventory_sync_is_bulk(monkeypatch):
    ep = FakeEndpoint([FakeRecord(1, serial="A", device_id=7)])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    nbx.sync_inventory(7, {"A": _item("A"), "B": _item("B"), "C": _item("C")})
    assert ep.update_calls == 1                 # 1 update (A) in one call
    assert ep.create_calls == 1                 # 2 creates (B, C) in one call
    assert len(ep.created) == 2


# ── Cisco CDP cable sync ─────────────────────────────────────────────────────

def _cisco_cable_api(local_ifaces, peer_dev, peer_ifaces, cables):
    return _fake_api(
        devices=FakeEndpoint([peer_dev] if peer_dev else []),
        interfaces=FakeEndpoint(local_ifaces + peer_ifaces),
        cables=FakeEndpoint(cables),
    )

_PEER = FakeRecord(5, name="SW2")
_LOCAL_IFACE = FakeRecord(11, name="Gi1/0/1", device_id=7)
_PEER_IFACE = FakeRecord(55, name="Gi1/0/24", device_id=5)
_NEIGHBORS = [{"device_id": "SW2", "platform": "", "ip": None,
               "local_intf": "GigabitEthernet1/0/1",
               "remote_intf": "GigabitEthernet1/0/24"}]


def test_cdp_cable_created_when_both_ends_resolve(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert len(api.dcim.cables.created) == 1
    payload = api.dcim.cables.created[0]
    assert payload["a_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 11}]
    assert payload["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 55}]
    assert payload["description"].startswith(cisco.CABLE_MARKER)


def test_cdp_cable_dedupes_existing_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []          # no duplicate
    assert {u["id"] for u in api.dcim.cables.updated} == {9}
    assert api.dcim.cables.deleted_ids == []      # seen -> kept


def test_cdp_cable_skips_unresolvable_neighbor(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], None, [], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [{"device_id": "UNKNOWN", "platform": "",
                               "ip": None, "local_intf": "GigabitEthernet1/0/1",
                               "remote_intf": "Gi0/1"}])
    assert api.dcim.cables.created == []


def test_cdp_cable_preserves_unmarked_and_conflicts(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    manual = FakeRecord(8, device_id=7, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []        # conflict -> no create
    assert api.dcim.cables.deleted_ids == []    # manual cable preserved


def test_cdp_cable_deletes_stale_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    stale = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                       a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                       b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [stale])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [])   # nothing seen this run

    assert api.dcim.cables.deleted_ids == [9]


class _GLO:
    """Stand-in for pynetbox GenericListObject (attribute access only)."""
    def __init__(self, object_id, object_type="dcim.interface"):
        self.object_id = object_id
        self.object_type = object_type


def test_cable_iface_ids_handles_generic_objects():
    """pynetbox returns GenericListObject terminations, not dicts — the
    dedupe must parse them (this was the cable-flap root cause)."""
    import netbox_sync.collectors.cisco as cisco
    cable = SimpleNamespace(
        a_terminations=[{"object_id": 1}, _GLO(2)],
        b_terminations=[_GLO(3), {"object_id": 4, "object_type": "x"}])
    assert sorted(cisco._cable_iface_ids(cable)) == [1, 2, 3, 4]


def test_validate_config_fortigate_requirements(monkeypatch, tmp_path):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("FORTIGATE_USER", raising=False)
    monkeypatch.delenv("FORTIGATE_PASS", raising=False)
    monkeypatch.setenv("FORTIGATE_RANGES", "192.0.2.0/29")
    with pytest.raises(RuntimeError, match="FORTIGATE_USER"):
        cfg._validate_config()
    monkeypatch.setenv("FORTIGATE_USER", "u")
    monkeypatch.setenv("FORTIGATE_PASS", "p")
    cfg._validate_config()   # basic-auth creds present -> passes


def test_validate_config_dahua_unv_requirements(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("DAHUA_RANGES", "192.0.2.10/32")
    monkeypatch.delenv("DAHUA_USER", raising=False)
    monkeypatch.delenv("DAHUA_PASS", raising=False)
    with pytest.raises(RuntimeError, match="DAHUA_USER"):
        cfg._validate_config()
    monkeypatch.setenv("DAHUA_USER", "u")
    monkeypatch.setenv("DAHUA_PASS", "p")
    cfg._validate_config()

    monkeypatch.setenv("UNV_RANGES", "192.0.2.11/32")
    monkeypatch.delenv("UNV_USER", raising=False)
    monkeypatch.delenv("UNV_PASS", raising=False)
    with pytest.raises(RuntimeError, match="UNV_USER"):
        cfg._validate_config()
    monkeypatch.setenv("UNV_USER", "u")
    monkeypatch.setenv("UNV_PASS", "p")
    cfg._validate_config()


# ── Ruckus config + sysinfo parser ───────────────────────────────────────────

def test_ruckus_ha_map_parsing():
    import netbox_sync.collectors.ruckus as ruckus
    out = ruckus._parse_ha_map("172.31.2.202:172.31.2.201,172.31.2.200")
    assert out == {"172.31.2.202": {"primary": "172.31.2.201",
                                    "secondary": "172.31.2.200"}}
    out2 = ruckus._parse_ha_map("10.0.0.5:10.0.0.6,10.0.0.7;10.1.0.5:10.1.0.6,10.1.0.7")
    assert out2["10.1.0.5"] == {"primary": "10.1.0.6", "secondary": "10.1.0.7"}
    assert ruckus._parse_ha_map("") == {}


SYSINFO = """System Overview:
  Name= Ruckus-Controller_02
  IP Address= 172.31.2.201
  IPv6 Address= fc00::2
  MAC Address= 38:45:3b:33:a9:40
  Uptime= 39d 5h 56m
  Model= ZD1200
  Licensed APs= 48
  Serial Number= 352138000988
  Version= 10.5.1.0 build 276
"""


def test_parse_sysinfo():
    import netbox_sync.collectors.ruckus as ruckus
    out = ruckus._parse_sysinfo(SYSINFO)
    assert out == {"name": "Ruckus-Controller_02", "ip": "172.31.2.201",
                   "mac": "38:45:3b:33:a9:40", "model": "ZD1200",
                   "serial": "352138000988", "version": "10.5.1.0 build 276"}


AP_ALL = """AP:
  ID:
    1:
      MAC Address= 70:47:77:1b:a3:80
      Model= r550
      Approved= Yes
      Device Name= F13-AP-W
      Group Name= F13
      Network Setting:
        IP Type= Static
        IP Address= 172.31.2.214
        Netmask= 255.255.255.0
        Gateway= 172.31.2.1
    2:
      MAC Address= 70:47:77:1b:b0:11
      Model= r350
      Approved= Yes
      Device Name= F11-AP-E
      Group Name= IOT-Group
      Network Setting:
        IP Type= Static
        IP Address= 172.31.2.220
        Netmask= 255.255.255.0
        Gateway= 172.31.2.1
"""


def test_parse_ap_all():
    import netbox_sync.collectors.ruckus as ruckus
    aps = ruckus._parse_ap_all(AP_ALL)
    assert len(aps) == 2
    assert aps[0] == {"mac": "70:47:77:1b:a3:80", "model": "r550",
                      "name": "F13-AP-W", "group": "F13",
                      "ip": "172.31.2.214", "approved": True}
    assert aps[1]["model"] == "r350"
    assert aps[1]["group"] == "IOT-Group"
    assert aps[1]["ip"] == "172.31.2.220"


# ── Ruckus AP device sync ────────────────────────────────────────────────────

def test_ensure_ap_device_creates_and_matches_by_mac(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    ap = {"mac": "70:47:77:1b:a3:80", "model": "r550", "name": "F13-AP-W",
          "group": "F13", "ip": "172.31.2.214", "approved": True}
    dev_id = nbx.ensure_ap_device(ap, "Ruckus-Controller_02")

    payload = devices_ep.created[0]
    assert payload["name"] == "F13-AP-W"
    cf = payload["custom_fields"]
    assert cf["wap_mac"] == "70:47:77:1b:a3:80"
    assert cf["wap_group"] == "F13"
    assert cf["wap_wlc"] == "Ruckus-Controller_02"
    assert cf["wap_enabled"] is True

    # second call with the same MAC -> updates, never duplicates
    dev_id2 = nbx.ensure_ap_device(ap, "Ruckus-Controller_02")
    assert dev_id2 == dev_id
    assert len(devices_ep.created) == 1


def test_ensure_ap_device_disambiguates_duplicate_names(monkeypatch):
    """Two APs named "F1" whose sites resolve to ONE NetBox site: NetBox
    enforces name uniqueness per site, so the second gets a stable MAC
    suffix; a later run keeps names stable and never duplicates."""
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    ap1 = {"mac": "b4:fb:e4:c3:48:4b", "model": "U7PG2", "name": "F1",
           "group": "Mollasadra", "ip": "192.168.236.17", "approved": True}
    ap2 = {"mac": "b4:fb:e4:c3:55:68", "model": "U7PG2", "name": "F1",
           "group": "Pardis", "ip": "192.168.236.18", "approved": True}
    nbx.ensure_ap_device(ap1, "unifi-x", manufacturer="Ubiquiti")
    nbx.ensure_ap_device(ap2, "unifi-x", manufacturer="Ubiquiti")
    assert [p["name"] for p in devices_ep.created] == ["F1", "F1 (5568)"]

    # repeat run: matched by wap_mac, suffixed name stays, nothing duplicated
    nbx.ensure_ap_device(ap2, "unifi-x", manufacturer="Ubiquiti")
    assert len(devices_ep.created) == 2
    assert devices_ep.updated[-1]["name"] == "F1 (5568)"

    # a third same-named AP never adopts the first's device via name+site
    ap3 = {"mac": "b4:fb:e4:c3:99:99", "model": "U7PG2", "name": "F1",
           "group": "Sharif", "ip": "192.168.236.19", "approved": True}
    nbx.ensure_ap_device(ap3, "unifi-x", manufacturer="Ubiquiti")
    assert devices_ep.created[-1]["name"] == "F1 (9999)"
    assert devices_ep.created[-1]["custom_fields"]["wap_mac"] == ap3["mac"]


def test_mark_ap_offline(monkeypatch):
    dev = FakeRecord(7, name="F13-AP-W", custom_fields={"wap_enabled": True})
    devices_ep = FakeEndpoint([dev])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))

    nbx.mark_ap_offline(7, "F13-AP-W")
    assert devices_ep.updated[0]["status"] == "offline"
    assert devices_ep.updated[0]["custom_fields"]["wap_enabled"] is False


# ── Ruckus WLAN sync ─────────────────────────────────────────────────────────

WLAN_ALL = """WLAN Service:
  ID:
    1:
      NAME = Smart Plug
      SSID = Smart Plug
      Authentication = open
      Encryption = wpa2
      VLAN-ID = 109
    2:
      NAME = CorpNet
      SSID = CorpNet
      Authentication = 802.1x
      Encryption = wpa2
      VLAN-ID = 10
"""


def test_parse_wlan_all():
    import netbox_sync.collectors.ruckus as ruckus
    wlans = ruckus._parse_wlan_all(WLAN_ALL)
    assert wlans == [
        {"name": "Smart Plug", "ssid": "Smart Plug", "auth": "open",
         "encryption": "wpa2", "vlan_id": 109},
        {"name": "CorpNet", "ssid": "CorpNet", "auth": "802.1x",
         "encryption": "wpa2", "vlan_id": 10},
    ]


def _wireless_api(lan_items=None, group_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(vlans=FakeEndpoint()),
        wireless=SimpleNamespace(
            wireless_lans=FakeEndpoint(lan_items or []),
            wireless_lan_groups=FakeEndpoint(group_items or [])))


def test_sync_wireless_lans_creates_with_auth_map_and_vlan(monkeypatch):
    api = _wireless_api()
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    wlans = [{"name": "Smart Plug", "ssid": "Smart Plug", "auth": "open",
              "encryption": "wpa2", "vlan_id": 109},
             {"name": "CorpNet", "ssid": "CorpNet", "auth": "802.1x",
              "encryption": "wpa2", "vlan_id": 10}]

    seen = nbx.sync_wireless_lans("Ruckus-Controller_02", wlans, {109: 500, 10: 501})

    groups = api.wireless.wireless_lan_groups.created
    assert groups[0]["name"] == "ZD Ruckus-Controller_02"
    by_ssid = {w["ssid"]: w for w in api.wireless.wireless_lans.created}
    assert by_ssid["Smart Plug"]["auth_type"] == "open"
    assert by_ssid["Smart Plug"]["vlan"] == 500
    assert by_ssid["CorpNet"]["auth_type"] == "wpa-enterprise"
    assert by_ssid["CorpNet"]["vlan"] == 501
    assert seen == {"Smart Plug", "CorpNet"}


def test_sweep_wireless_lans(monkeypatch):
    stale = FakeRecord(50, ssid="OldNet",
                       description="netbox-sync: Ruckus-Controller_02 OldNet")
    keep = FakeRecord(51, ssid="Smart Plug",
                      description="netbox-sync: Ruckus-Controller_02 Smart Plug")
    manual = FakeRecord(52, ssid="ManualNet", description="manual")
    api = _wireless_api([stale, keep, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.sweep_wireless_lans("Ruckus-Controller_02", {"Smart Plug"})
    assert api.wireless.wireless_lans.deleted_ids == [50]


# ── UniFi session + parsers ──────────────────────────────────────────────────

UNIFI_STATUS = {"meta": {"rc": "ok", "up": True,
                         "server_version": "10.2.105",
                         "uuid": "6dd002d7-b9f1-4625-84f2-b7b4f9400c16"},
                "data": []}

UNIFI_SITES = {"meta": {"rc": "ok"}, "data": [
    {"name": "default", "desc": "Default", "_id": "588f25805bdbb3cf25db3fe1"},
    {"name": "08r8os8i", "desc": "SnappPay", "_id": "62f73ef0567d3214303522d7"},
    {"name": "ressysr4", "desc": "HQ-General", "_id": "64d2169dff7ccc1690b5cea6"},
]}

UNIFI_DEVICES = {"meta": {"rc": "ok"}, "data": [
    {"_id": "67a38c6a0cfd72475b8ec7f7", "mac": "f4:e2:c6:13:da:0f",
     "name": "F5-GamingRoom", "model": "U7PG2", "serial": "F4E2C613DA0F",
     "ip": "192.168.254.14", "version": "6.6.77.15402", "type": "uap",
     "state": 0, "adopted": True, "uptime": None},
    {"_id": "67fa51eb667d7102b6961a55", "mac": "b4:fb:e4:c3:48:4b",
     "name": "F3-S", "model": "U7PG2", "serial": "B4FBE4C3484B",
     "ip": "192.168.236.17", "version": "6.8.2.15592", "type": "uap",
     "state": 1, "adopted": True, "uptime": 954532},
    {"_id": "6474dfd9ff7ccc11802fa6bb", "mac": "78:45:58:26:96:b4",
     "name": "usw-mini", "model": "USW-MINI", "serial": "7845582696B4",
     "ip": "172.31.2.253", "version": "6.6.77.15402", "type": "usw",
     "state": 1, "adopted": True, "uptime": 1000},
]}

UNIFI_WLANS = {"meta": {"rc": "ok"}, "data": [
    {"_id": "w1", "name": "Smart Plug", "security": "wpa2",
     "wpa_mode": "wpa2", "wpa_enc": "aes", "enabled": True,
     "hide_ssid": False, "is_guest": False, "networkconf_id": "n1",
     "site_id": "s1"},
    {"_id": "w2", "name": "CorpNet", "security": "8021x",
     "wpa_mode": "wpa3", "wpa_enc": "aes", "enabled": True,
     "hide_ssid": False, "is_guest": False, "networkconf_id": "n2",
     "site_id": "s1"},
]}

UNIFI_NETWORKS = {"meta": {"rc": "ok"}, "data": [
    {"_id": "n1", "name": "IOT", "vlan": 109, "site_id": "s1"},
    {"_id": "n2", "name": "Corp", "vlan": 10, "site_id": "s1"},
]}


def test_unifi_parse_sites_devices_wlans_networks():
    import netbox_sync.collectors.unifi as unifi
    sites = unifi._parse_sites(UNIFI_SITES)
    assert sites == [
        {"name": "default", "desc": "Default"},
        {"name": "08r8os8i", "desc": "SnappPay"},
        {"name": "ressysr4", "desc": "HQ-General"},
    ]
    aps = unifi._parse_devices(UNIFI_DEVICES)
    assert aps == [
        {"mac": "f4:e2:c6:13:da:0f", "model": "U7PG2", "name": "F5-GamingRoom",
         "group": None, "ip": "192.168.254.14", "approved": True,
         "firmware": "6.6.77.15402", "state": 0},
        {"mac": "b4:fb:e4:c3:48:4b", "model": "U7PG2", "name": "F3-S",
         "group": None, "ip": "192.168.236.17", "approved": True,
         "firmware": "6.8.2.15592", "state": 1},
    ]   # type usw filtered out; only uap
    wlans = unifi._parse_wlans(UNIFI_WLANS)
    assert wlans[0]["ssid"] == "Smart Plug"
    assert wlans[0]["security"] == "wpa2"
    assert wlans[0]["auth"] == "wpa2"        # personal -> shared vocab
    assert wlans[0]["networkconf_id"] == "n1"
    assert wlans[1]["auth"] == "802.1x"      # enterprise -> shared vocab
    nets = unifi._parse_networks(UNIFI_NETWORKS)
    assert nets == {"n1": 109, "n2": 10}


# ── Ruckus HA resolution + controller device ─────────────────────────────────

def test_ruckus_role_and_cluster():
    import netbox_sync.collectors.ruckus as ruckus
    ha_map = {"172.31.2.202": {"primary": "172.31.2.201", "secondary": "172.31.2.200"}}
    assert ruckus._ruckus_role_and_cluster("172.31.2.202", ha_map) == ("vip", "172.31.2.202")
    assert ruckus._ruckus_role_and_cluster("172.31.2.201", ha_map) == ("primary", "172.31.2.202")
    assert ruckus._ruckus_role_and_cluster("172.31.2.200", ha_map) == ("secondary", "172.31.2.202")
    assert ruckus._ruckus_role_and_cluster("10.0.0.9", ha_map) == ("standalone", None)


def test_ensure_ruckus_device_cluster_and_secondary_preserves(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    vip_probe = {"ip": "172.31.2.202", "serial": "352138000988",
                 "model": "ZD1200", "hostname": "Ruckus-Controller_02",
                 "reported_ip": "172.31.2.201", "mac": "38:45:3b:33:a9:40",
                 "manufacturer": "Ruckus", "firmware": "10.5.1.0 build 276"}
    dev_id = nbx.ensure_ruckus_device(vip_probe, "vip", "172.31.2.202")
    payload = devices_ep.created[0]
    assert payload["name"] == "Ruckus-Controller_02"
    assert payload["serial"] == "352138000988"
    assert payload["custom_fields"]["wlc_vip"] == "172.31.2.202"
    assert payload["custom_fields"]["wlc_ha_role"] == "vip"

    # a probe from the SECONDARY unit must not overwrite cluster identity
    sec_probe = dict(vip_probe, ip="172.31.2.200",
                     serial="999999999999", hostname="Ruckus-Controller_03")
    dev_id2 = nbx.ensure_ruckus_device(sec_probe, "secondary", "172.31.2.202")
    assert dev_id2 == dev_id
    assert len(devices_ep.created) == 1
    upd = devices_ep.updated[0]
    assert "name" not in upd and "serial" not in upd
    assert upd["custom_fields"]["wlc_ha_role"] == "secondary"


# ── FortiGate device + interfaces ────────────────────────────────────────────

def test_ensure_fortigate_device_creates(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    nbx.ensure_fortigate_device({
        "ip": "192.0.2.70", "serial": "FGT60FTK21000001",
        "model": "FortiGate 60F", "hostname": "FGT-DC-01",
        "manufacturer": "Fortinet", "firmware": "v7.2.4"})
    payload = devices_ep.created[0]
    assert payload["serial"] == "FGT60FTK21000001"
    assert payload["custom_fields"]["fortigate_ip"] == "192.0.2.70"
    assert payload["custom_fields"]["fortigate_enabled"] is True


_HA = {"clustered": True, "group_name": "Z-Cluster-FW", "mode": "a-p",
       "primary_serial": "FG180FTK21901250", "primary_hostname": "HQ",
       "units": [
           {"hostname": "HQ", "serial": "FG180FTK21901250", "is_primary": True},
           {"hostname": "HQ-Secondary", "serial": "FG180FTK22900291",
            "is_primary": False}]}


def test_ensure_fortigate_device_cluster_creates_with_ha_fields(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    # probe comes from the SECONDARY unit — device must still be the cluster (HQ)
    nbx.ensure_fortigate_device({
        "ip": "192.0.2.71", "serial": "FG180FTK22900291",
        "model": "FortiGate 1800F", "hostname": "HQ-Secondary",
        "manufacturer": "Fortinet", "firmware": "v7.2.13"}, ha=_HA)

    payload = devices_ep.created[0]
    assert payload["name"] == "HQ"                          # cluster name, not unit name
    assert payload["serial"] == "FG180FTK21901250"          # primary serial
    cf = payload["custom_fields"]
    assert cf["fortigate_ha_group"] == "Z-Cluster-FW"
    assert cf["fortigate_ha_mode"] == "a-p"
    assert "HQ-Secondary (FG180FTK22900291)" in cf["fortigate_ha_peer"]
    assert cf["fortigate_ha_role"] == "secondary"           # probed unit's role


def test_ensure_fortigate_device_cluster_finds_by_peer_serial(monkeypatch):
    existing = FakeRecord(7, name="HQ", serial="FG180FTK21901250",
                          custom_fields={})
    devices_ep = FakeEndpoint([existing])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    def _find(serial, role_name=None):
        if serial == "FG180FTK22900291":
            return existing       # peer serial resolves to the same cluster
        return None
    monkeypatch.setattr(nbx, "find_device", _find)

    dev_id = nbx.ensure_fortigate_device({
        "ip": "192.0.2.71", "serial": "FG180FTK22900291",
        "model": "FortiGate 1800F", "hostname": "HQ-Secondary",
        "manufacturer": "Fortinet", "firmware": "v7.2.13"}, ha=_HA)

    assert dev_id == 7                    # updated, not duplicated
    assert devices_ep.created == []


def test_sync_fortigate_interfaces_bulk_and_vlan_subif(monkeypatch):
    import netbox_sync.collectors.fortigate as fg
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="port1", device_id=7),
        FakeRecord(2, name="port9", device_id=7, mgmt_only=False),
    ])
    api = _fake_api(interfaces=ifaces_ep)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"name": "port1", "link": True, "speed_mbps": 1000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "",
         "alias": "UPLINK-CORE"},
        {"name": "port1.10", "link": True, "speed_mbps": 1000,
         "type": "vlan", "ip": "10.10.10.1/24", "vlanid": 10, "parent": "port1",
         "alias": ""},
    ]
    fg.sync_fortigate_interfaces(7, ports, {10: 110})

    by_name = {}
    for u in ifaces_ep.updated:
        rec = next(i for i in ifaces_ep.items if i.id == u["id"])
        by_name[rec.name] = u
    assert by_name["port1"]["type"] == "1000base-t"
    assert by_name["port1"]["label"] == "UPLINK-CORE"
    created = {c["name"]: c for c in ifaces_ep.created}
    assert created["port1.10"]["type"] == "virtual"
    assert created["port1.10"]["untagged_vlan"] == 110
    assert created["port1.10"]["mode"] == "tagged"
    assert created["port1.10"]["parent"] == 1     # subinterface under its parent
    assert "label" not in created["port1.10"]   # empty alias -> no label key
    assert ifaces_ep.deleted_ids == [2]
    assert ifaces_ep.update_calls == 1 and ifaces_ep.create_calls == 1


def test_sync_fortigate_interfaces_lag_and_member_linkage(monkeypatch):
    import netbox_sync.collectors.fortigate as fg
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="port33", device_id=7),
        FakeRecord(2, name="port34", device_id=7),
    ])
    api = _fake_api(interfaces=ifaces_ep)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"name": "port33", "link": True, "speed_mbps": 10000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "", "alias": ""},
        {"name": "port34", "link": True, "speed_mbps": 10000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "", "alias": ""},
        {"name": "Core Switch", "link": True, "speed_mbps": None,
         "type": "lag", "members": ["port33", "port34"], "ip": "",
         "vlanid": None, "parent": "", "alias": ""},
    ]
    fg.sync_fortigate_interfaces(7, ports, {})

    created = {c["name"]: c for c in ifaces_ep.created}
    assert created["Core Switch"]["type"] == "lag"
    lag_id = next(i.id for i in ifaces_ep.items if i.name == "Core Switch")
    by_id = {u["id"]: u for u in ifaces_ep.updated}
    assert by_id[1]["lag"] == lag_id
    assert by_id[2]["lag"] == lag_id


def test_cdp_cables_protocol_label(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS, protocol="lldp")
    assert " lldp " in api.dcim.cables.created[0]["description"]


def test_ensure_svi_interface_creates_virtual_with_vlan(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    iid = cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    created = ifaces_ep.created[0]
    assert created["name"] == "Vlan50"
    assert created["type"] == "virtual"
    assert created["untagged_vlan"] == 500
    assert created["mode"] == "access"   # required by NetBox for untagged_vlan
    assert created["mgmt_only"] is True
    assert iid is not None

    # second call reuses
    cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    assert ifaces_ep.create_calls == 1


def test_ensure_svi_interface_non_vlan_name(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))
    cisco.ensure_svi_interface(7, "Loopback0", {})
    assert "untagged_vlan" not in ifaces_ep.created[0]


# ── IPAM prefix sync ─────────────────────────────────────────────────────────

def test_prefix_from_ip_and_iface_addr():
    import netbox_sync.ipam as ipam
    assert ipam._prefix_from_ip("172.31.2.1 255.255.255.0") == "172.31.2.0/24"
    assert ipam._prefix_from_ip("10.19.128.1 255.255.255.0") == "10.19.128.0/24"
    assert ipam._prefix_from_ip("79.127.120.184 255.255.255.240") == "79.127.120.176/28"
    assert ipam._prefix_from_ip("0.0.0.0 0.0.0.0") is None
    assert ipam._prefix_from_ip("") is None
    assert ipam._prefix_from_ip(None) is None
    assert ipam._iface_addr_with_prefixlen("172.31.2.1 255.255.255.0") == \
        ("172.31.2.1/24", "172.31.2.1")
    assert ipam._iface_addr_with_prefixlen("0.0.0.0 0.0.0.0") == (None, None)
    assert ipam._iface_addr_with_prefixlen("") == (None, None)
    assert ipam._prefix_from_ip("10.19.128.1 255.255.255.0") == "10.19.128.0/24"
    assert ipam._prefix_from_ip("79.127.120.184 255.255.255.240") == "79.127.120.176/28"
    assert ipam._prefix_from_ip("0.0.0.0 0.0.0.0") is None
    assert ipam._prefix_from_ip("") is None
    assert ipam._prefix_from_ip(None) is None


def _prefix_api(prefix_items):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(prefixes=FakeEndpoint(prefix_items),
                             ip_addresses=FakeEndpoint()))


def test_ensure_prefix_create_refresh_and_manual(monkeypatch):
    import netbox_sync.ipam as ipam
    marked = FakeRecord(50, prefix="10.0.0.0/24",
                        description="netbox-sync: last seen OLD")
    manual = FakeRecord(51, prefix="10.1.0.0/24", description="manual prefix")
    api = _prefix_api([marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # marked existing -> refreshed with scope+vlan
    pid = ipam.ensure_prefix("10.0.0.0/24", 3, 110, "FGT-DC-01", "VLAN10")
    assert pid == 50
    assert {u["id"] for u in api.ipam.prefixes.updated} == {50}
    assert api.ipam.prefixes.updated[0]["vlan"] == 110
    assert api.ipam.prefixes.updated[0]["scope_id"] == 3
    assert api.ipam.prefixes.updated[0]["scope_type"] == "dcim.site"

    # manual existing -> reused untouched
    pid = ipam.ensure_prefix("10.1.0.0/24", 3, 111, "FGT-DC-01", "VLAN11")
    assert pid == 51
    assert len(api.ipam.prefixes.updated) == 1   # no update for manual

    # missing -> created marked with scope+vlan
    pid = ipam.ensure_prefix("10.2.0.0/24", 3, 112, "FGT-DC-01", "VLAN12")
    assert api.ipam.prefixes.created[0]["prefix"] == "10.2.0.0/24"
    assert api.ipam.prefixes.created[0]["scope_id"] == 3
    assert api.ipam.prefixes.created[0]["vlan"] == 112
    assert api.ipam.prefixes.created[0]["description"].startswith("netbox-sync:")


def test_parent_prefixes_container_and_sweep(monkeypatch):
    import netbox_sync.ipam as ipam
    monkeypatch.setattr(ipam, "SITE_IP_MAP",
                        [(ipam.ipaddress.ip_network("172.31.0.0/16"), "HQ")])
    api = _prefix_api([])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 3)

    seen = ipam.sync_parent_prefixes()
    created = api.ipam.prefixes.created[0]
    assert created["prefix"] == "172.31.0.0/16"
    assert created["status"] == "container"
    assert created["scope_id"] == 3
    assert created["scope_type"] == "dcim.site"
    assert seen == {3: {"172.31.0.0/16"}}

    # entry removed from map -> parent swept
    monkeypatch.setattr(ipam, "SITE_IP_MAP", [])
    ipam.sweep_stale_parents()
    assert api.ipam.prefixes.deleted_ids == [api.ipam.prefixes.items[0].id]


def _host_ip_api(ip_items, iface_items, prefix_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint(iface_items)),
        ipam=SimpleNamespace(ip_addresses=FakeEndpoint(ip_items),
                             prefixes=FakeEndpoint(prefix_items or [])))


def test_ensure_host_ip_create_with_mask_and_assignment(monkeypatch):
    import netbox_sync.ipam as ipam
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    api = _host_ip_api([], [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = ipam.ensure_host_ip(7, "172.31.2.1/24", "MGMT54",
                                "FGT-DC-01", "AP MGMT")

    created = api.ipam.ip_addresses.created[0]
    assert created["address"] == "172.31.2.1/24"
    assert created["status"] == "active"
    assert created["description"].startswith("netbox-sync: if ")
    assert created["assigned_object_type"] == "dcim.interface"
    assert created["assigned_object_id"] == 70
    assert api.ipam.ip_addresses.updated == []   # created with assignment
    assert ip_id is not None


def test_ensure_host_ip_reuses_existing(monkeypatch):
    import netbox_sync.ipam as ipam
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    existing = FakeRecord(50, address="172.31.2.1",
                        assigned_object_type=None, assigned_object_id=None)
    api = _host_ip_api([existing], [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = ipam.ensure_host_ip(7, "172.31.2.1/24", "MGMT54",
                                "FGT-DC-01", "AP MGMT")
    assert ip_id == 50
    assert api.ipam.ip_addresses.created == []
    assert api.ipam.ip_addresses.updated[0]["assigned_object_id"] == 70


def test_containing_prefix_longest_match(monkeypatch):
    import netbox_sync.ipam as ipam
    broad = FakeRecord(50, prefix="172.31.0.0/16")
    specific = FakeRecord(51, prefix="172.31.2.0/24")
    api = _host_ip_api([], [], [broad, specific])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    p = ipam._containing_prefix("172.31.2.44")
    assert p.id == 51

    assert ipam._containing_prefix("10.9.9.9") is None


def test_sweep_stale_prefixes(monkeypatch):
    import netbox_sync.ipam as ipam
    seen = FakeRecord(50, prefix="10.0.0.0/24", scope_id=3,
                      description="netbox-sync: last seen SW1")
    stale = FakeRecord(51, prefix="10.1.0.0/24", scope_id=3,
                       description="netbox-sync: last seen SW1")
    other_site = FakeRecord(54, prefix="10.4.0.0/24", scope_id=9,
                            description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, prefix="10.2.0.0/24", scope_id=3,
                        description="manual prefix")
    api = _prefix_api([seen, stale, other_site, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ipam.sweep_stale_prefixes(3, {"10.0.0.0/24", "10.9.0.0/24"})

    assert api.ipam.prefixes.deleted_ids == [51]


def test_sweep_stale_host_ips(monkeypatch):
    import netbox_sync.ipam as ipam
    seen = FakeRecord(50, address="172.31.2.1/24", device_id=7,
                      description="netbox-sync: if FGT AP MGMT")
    stale = FakeRecord(51, address="172.31.9.9/24", device_id=7,
                       description="netbox-sync: if FGT OLD")
    mgmt = FakeRecord(52, address="172.31.5.1/32", device_id=7,
                      description="netbox-sync: mgmt")
    manual = FakeRecord(53, address="10.1.1.1/24", device_id=7,
                        description="manual")
    api = _host_ip_api([seen, stale, mgmt, manual], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ipam.sweep_stale_host_ips(7, {"172.31.2.1"})

    assert api.ipam.ip_addresses.deleted_ids == [51]


def test_sync_nat_ips_vip_pool_and_sweep(monkeypatch):
    import netbox_sync.ipam as ipam
    api = _host_ip_api([], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    vips = [{"name": "TimeKeeping-443", "extip": "77.104.83.164",
             "extport": 443, "mappedip": ["172.31.5.53"], "mappedport": 443,
             "protocol": "tcp", "portforward": "enable", "status": "enable"}]
    pools = [{"name": "79.127.120.186", "type": "overload",
              "startip": "79.127.120.186", "endip": "79.127.120.186"}]
    ipam.sync_nat_ips(vips, pools)

    by_addr = {r.address.split("/")[0]: r for r in api.ipam.ip_addresses.items}
    ext = by_addr["77.104.83.164"]
    inside = by_addr["172.31.5.53"]
    assert ext.nat_inside == inside.id
    assert "TimeKeeping-443" in ext.description
    assert "nat inside" in inside.description
    assert "79.127.120.186" in by_addr

    # sweep: stale marked NAT IPs removed, manual kept
    manual = FakeRecord(99, address="9.9.9.9", device_id=7, description="manual")
    stale = FakeRecord(98, address="8.8.8.8", device_id=7,
                       description="netbox-sync: nat OLD-VIP")
    for r in (manual, stale):
        r._endpoint = api.ipam.ip_addresses
    api.ipam.ip_addresses.items.extend([manual, stale])
    ipam.sweep_nat_ips({"77.104.83.164", "172.31.5.53", "79.127.120.186"})
    assert 98 in api.ipam.ip_addresses.deleted_ids
    assert 99 not in api.ipam.ip_addresses.deleted_ids


def _services_api(service_items, ip_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(services=FakeEndpoint(service_items),
                             ip_addresses=FakeEndpoint(ip_items or [])))


def test_sync_nat_services_per_vip(monkeypatch):
    import netbox_sync.ipam as ipam
    ext_ip = FakeRecord(50, address="77.104.83.164/32")
    api = _services_api([], [ext_ip])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    vips = [
        {"name": "TimeKeeping-443", "extip": "77.104.83.164", "extport": 443,
         "mappedip": ["172.31.5.53"], "mappedport": 443, "protocol": "tcp",
         "portforward": "enable", "status": "enable"},
        {"name": "Avid App-7625", "extip": "77.104.83.164", "extport": 7625,
         "mappedip": ["172.31.5.60"], "mappedport": 7625, "protocol": "tcp",
         "portforward": "enable", "status": "enable"},
    ]
    seen = ipam.sync_nat_services(7, vips)

    created = {s["name"]: s for s in api.ipam.services.created}
    assert set(created) == {"TimeKeeping-443", "Avid App-7625"}
    s1 = created["TimeKeeping-443"]
    assert s1["protocol"] == "tcp"
    assert s1["ports"] == [443]
    assert s1["ipaddresses"] == [50]
    assert "172.31.5.53" in s1["description"]
    assert created["Avid App-7625"]["ipaddresses"] == [50]   # shared extip -> separate services
    assert seen == {"TimeKeeping-443", "Avid App-7625"}


def test_svc_ports_parsing():
    import netbox_sync.ipam as ipam
    assert ipam._svc_ports(443) == [443]
    assert ipam._svc_ports("21114-21119") == [21114, 21115, 21116, 21117, 21118, 21119]
    assert ipam._svc_ports("junk") == []
    assert ipam._svc_ports(None) == []


def test_sweep_nat_services(monkeypatch):
    import netbox_sync.ipam as ipam
    stale = FakeRecord(60, name="OLD-SVC", device_id=7,
                       description="netbox-sync: nat OLD-SVC")
    keep = FakeRecord(61, name="TimeKeeping-443", device_id=7,
                      description="netbox-sync: nat TimeKeeping-443")
    manual = FakeRecord(62, name="my-svc", device_id=7, description="manual")
    api = _services_api([stale, keep, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ipam.sweep_nat_services(7, {"TimeKeeping-443"})
    assert api.ipam.services.deleted_ids == [60]


def test_site_vlan_index(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g = FakeRecord(8, name="BD1", description="netbox-sync: vtp=snapp",
                    scope_type="dcim.site", scope_id=3)
    manual_g = FakeRecord(9, name="X", description="manual",
                          scope_type="dcim.site", scope_id=3)
    vlans = [FakeRecord(50, vid=10, group_id=8),
             FakeRecord(51, vid=20, group_id=8),
             FakeRecord(52, vid=10, group_id=9)]
    api = _vlan_api(vlans, [g, manual_g])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    index = cisco._site_vlan_index(3)
    assert index == {10: [(8, 50)], 20: [(8, 51)]}


def test_resolve_fortigate_vlans_paths():
    import netbox_sync.collectors.fortigate as fg
    site_index = {10: [(8, 50)], 20: [(8, 51), (9, 60)], 30: []}
    vlans = [{"vid": 10, "name": "A", "status": "active"},
             {"vid": 20, "name": "B", "status": "active"},
             {"vid": 30, "name": "C", "status": "active"},
             {"vid": 40, "name": "D", "status": "active"}]
    get_mac = lambda vid: "00:09:0f:09:00:26" if vid == 20 else None
    lookup = lambda vid, mac: 9 if (vid, mac) == (20, "00:09:0f:09:00:26") else None

    vid_map, missing = fg.resolve_fortigate_vlans(site_index, vlans, get_mac, lookup)

    assert vid_map == {10: 50, 20: 60}     # unique reused; overlap resolved
    assert [v["vid"] for v in missing] == [30, 40]   # none + unresolved overlap


# ── config validation ────────────────────────────────────────────────────────

REQUIRED_VARS = ["NETBOX_URL", "NETBOX_TOKEN", "REDFISH_USER", "REDFISH_PASS",
                 "STORAGE_USER", "STORAGE_PASS", "SWITCH_USER", "SWITCH_PASS"]


def test_validate_config_ok_when_all_vars_present(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    cfg._validate_config()  # must not raise


def test_validate_config_lists_missing_vars(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("NETBOX_TOKEN")
    monkeypatch.delenv("SWITCH_PASS")
    with pytest.raises(RuntimeError, match="NETBOX_TOKEN"):
        cfg._validate_config()
    with pytest.raises(RuntimeError, match="SWITCH_PASS"):
        cfg._validate_config()


# ── Cisco config ─────────────────────────────────────────────────────────────

def test_cisco_ranges_default_empty_and_parse(monkeypatch):
    import importlib
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == []
    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29, 198.51.100.0/29")
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == ["192.0.2.0/29", "198.51.100.0/29"]
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)


def test_empty_range_env_disables_family(monkeypatch):
    """Set-but-empty range env vars must disable the family ([]), NOT fall
    back to the placeholder defaults — this is how users turn families off."""
    import importlib
    monkeypatch.setenv("BMC_RANGES", "")
    monkeypatch.setenv("STORAGE_RANGES", "")
    monkeypatch.setenv("SAN_RANGES", "")
    importlib.reload(cfg)
    assert cfg.BMC_RANGES == []
    assert cfg.STORAGE_RANGES == []
    assert cfg.SAN_RANGES == []
    # Unset env vars still fall back to the documented placeholder defaults
    monkeypatch.delenv("BMC_RANGES")
    importlib.reload(cfg)
    assert cfg.BMC_RANGES == cfg.DEFAULT_BMC_RANGES


def test_site_ip_map_parsing_and_sort(monkeypatch):
    import importlib
    monkeypatch.setenv(
        "SITE_IP_MAP",
        "172.31.0.0/16:HQ,172.31.1.0/24:Branch,bad-entry,10.0.0.0/8:Net")
    importlib.reload(cfg)
    assert [(str(n), s) for n, s in cfg.SITE_IP_MAP] == [
        ("172.31.1.0/24", "Branch"),   # /24 beats /16 beats /8 (longest first)
        ("172.31.0.0/16", "HQ"),
        ("10.0.0.0/8", "Net"),
    ]
    monkeypatch.setenv("SITE_IP_MAP", "not-a-cidr:X")
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # invalid CIDR skipped, no crash
    monkeypatch.delenv("SITE_IP_MAP", raising=False)
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # unset -> empty (backward compatible)


def test_validate_config_requires_cisco_creds_only_when_ranges_set(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    monkeypatch.delenv("CISCO_USER", raising=False)
    monkeypatch.delenv("CISCO_PASS", raising=False)
    cfg._validate_config()  # no ranges -> no creds needed

    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29")
    with pytest.raises(RuntimeError, match="CISCO_USER"):
        cfg._validate_config()


# ── log level filtering ──────────────────────────────────────────────────────

def test_debug_logs_hidden_by_default(capsys, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "INFO")
    cfg.log("DEBUG", "dbg-hidden")
    cfg.log("INFO", "info-shown")
    out = capsys.readouterr().out
    assert "dbg-hidden" not in out
    assert "info-shown" in out


def test_debug_logs_shown_when_level_is_debug(capsys, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "DEBUG")
    cfg.log("DEBUG", "dbg-shown")
    assert "dbg-shown" in capsys.readouterr().out


# ── security options ─────────────────────────────────────────────────────────

class _FakeNetboxAPI:
    def __init__(self):
        self.http_session = SimpleNamespace(verify=None)


def test_netbox_tls_verify_defaults_off(monkeypatch):
    fake = _FakeNetboxAPI()
    monkeypatch.setattr(nbx.pynetbox, "api", lambda *a, **k: fake)
    monkeypatch.delenv("NETBOX_VERIFY_TLS", raising=False)
    monkeypatch.setattr(nbx, "nb", None)
    nbx.get_netbox()
    assert fake.http_session.verify is False


def test_netbox_tls_verify_can_be_enabled(monkeypatch):
    fake = _FakeNetboxAPI()
    monkeypatch.setattr(nbx.pynetbox, "api", lambda *a, **k: fake)
    monkeypatch.setenv("NETBOX_VERIFY_TLS", "true")
    monkeypatch.setattr(nbx, "nb", None)
    nbx.get_netbox()
    assert fake.http_session.verify is True


class _FakeTransport:
    def is_active(self):
        return True


class _FakeSSHClient:
    instance = None

    def __init__(self):
        _FakeSSHClient.instance = self
        self.policy = None
        self.host_keys_loaded = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_system_host_keys(self):
        self.host_keys_loaded = True

    def connect(self, **kwargs):
        pass

    def get_transport(self):
        return _FakeTransport()

    def close(self):
        pass


def test_ssh_host_key_policy_defaults_to_auto_add(monkeypatch):
    monkeypatch.setattr(brc.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.delenv("SWITCH_STRICT_HOST_KEY", raising=False)
    brc.BrocadeSwitchSession("192.0.2.1").login()
    assert isinstance(_FakeSSHClient.instance.policy,
                      brc.paramiko.AutoAddPolicy)
    assert _FakeSSHClient.instance.host_keys_loaded is False


def test_ssh_host_key_policy_strict_when_enabled(monkeypatch):
    monkeypatch.setattr(brc.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setenv("SWITCH_STRICT_HOST_KEY", "true")
    brc.BrocadeSwitchSession("192.0.2.1").login()
    assert isinstance(_FakeSSHClient.instance.policy,
                      brc.paramiko.RejectPolicy)
    assert _FakeSSHClient.instance.host_keys_loaded is True


# ── primary IPv4 ─────────────────────────────────────────────────────────────

def test_mgmt_prefixlen_from_ranges(monkeypatch):
    monkeypatch.setattr(utils, "BMC_RANGES", ["10.0.0.0/24"])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/27"])
    assert utils._mgmt_prefixlen("10.0.0.5") == 24
    assert utils._mgmt_prefixlen("172.31.1.5") == 27
    assert utils._mgmt_prefixlen("192.0.2.9") == 32   # no range contains it
    assert utils._mgmt_prefixlen("junk") == 32        # invalid tolerated


def _ipam_api(ip_items, device_record, iface_items=None):
    # api.ipam is a separate pynetbox app from api.dcim — model both
    return SimpleNamespace(
        dcim=SimpleNamespace(
            devices=FakeEndpoint([device_record] if device_record else []),
            interfaces=FakeEndpoint(iface_items or [])),
        ipam=SimpleNamespace(ip_addresses=FakeEndpoint(ip_items)))


def test_primary_ip_created_assigned_and_set(monkeypatch):
    """Full flow: IPAM record created with range mask, a mgmt_only interface
    is created, the IP is ASSIGNED to it (NetBox requires assignment before
    primary_ip4 is accepted), then primary_ip4 is set."""
    monkeypatch.setattr(utils, "BMC_RANGES", [])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/24"])
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "F10-SW-W-02")

    created = api.ipam.ip_addresses.created[0]
    assert created["address"] == "172.31.1.103/24"
    assert created["dns_name"] == "f10-sw-w-02"
    assert created["description"] == "netbox-sync: mgmt"

    # mgmt interface created on the device
    assert len(api.dcim.interfaces.created) == 1
    iface = api.dcim.interfaces.created[0]
    assert iface["device"] == 7
    assert iface["type"] == "virtual"
    assert iface["mgmt_only"] is True

    # IP assigned to that interface, then device primary set
    iface_id = api.dcim.interfaces.items[-1].id
    assert {"id": ip_id, "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface_id} in api.ipam.ip_addresses.updated
    assert {u["id"] for u in api.dcim.devices.updated} == {7}
    assert api.dcim.devices.updated[0]["primary_ip4"] == ip_id


def test_primary_ip_reuses_existing_and_mgmt_iface(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    existing_ip = FakeRecord(50, address="172.31.1.103",
                             assigned_object_type=None, assigned_object_id=None)
    mgmt_iface = FakeRecord(60, name="mgmt", device_id=7, mgmt_only=True)
    api = _ipam_api([existing_ip], dev, [mgmt_iface])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert ip_id == 50
    assert api.ipam.ip_addresses.created == []      # reused IP record
    assert api.dcim.interfaces.created == []        # reused mgmt interface
    assert api.ipam.ip_addresses.updated[0]["assigned_object_id"] == 60


def test_primary_ip_skipped_when_assigned_to_other_device(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    foreign_iface = FakeRecord(99, name="Gi0/1", device_id=42)
    taken_ip = FakeRecord(50, address="172.31.1.103",
                          assigned_object_type="dcim.interface",
                          assigned_object_id=99)
    api = _ipam_api([taken_ip], dev, [])
    api.dcim.interfaces.items.append(foreign_iface)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    # never hijack an IP assigned to another device
    assert api.dcim.devices.updated == []
    assert api.ipam.ip_addresses.updated == []


def test_primary_ip_no_write_when_already_correct(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=FakeRecord(50))
    own_iface = FakeRecord(60, name="mgmt", device_id=7, mgmt_only=True)
    own_ip = FakeRecord(50, address="172.31.1.103",
                        assigned_object_type="dcim.interface",
                        assigned_object_id=60)
    api = _ipam_api([own_ip], dev, [own_iface])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert api.dcim.devices.updated == []
    assert api.ipam.ip_addresses.updated == []


def test_primary_ip_assigned_to_named_iface(monkeypatch):
    dev = FakeRecord(7, name="FGT-DC-01", primary_ip4=None)
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    api = _ipam_api([], dev, [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "FGT-DC-01", iface_name="MGMT54")

    # no synthetic mgmt interface created; IP assigned to the named one
    assert api.dcim.interfaces.created == []
    upd = api.ipam.ip_addresses.updated[0]
    assert upd["assigned_object_id"] == 70
    assert api.dcim.devices.updated[0]["primary_ip4"] is not None


def test_primary_ip_named_iface_missing_falls_back_to_mgmt(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev, [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "SW1", iface_name="Vlan999")

    # synthetic mgmt interface created as fallback
    assert api.dcim.interfaces.created[0]["name"] == "mgmt"


# ── Cisco VLAN sync ──────────────────────────────────────────────────────────

def _vlan_api(vlan_items, group_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(vlans=FakeEndpoint(vlan_items),
                             vlan_groups=FakeEndpoint(group_items or [])))


def test_sync_cisco_vlans_create_update_and_manual_reuse(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(50, vid=10, group_id=8, description="netbox-sync: last seen OLD")
    manual = FakeRecord(51, vid=20, group_id=8, description="manual vlan")
    api = _vlan_api([marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    vid_map = cisco.sync_cisco_vlans(8, "SW1", [
        {"vid": 10, "name": "USERS", "status": "active"},
        {"vid": 20, "name": "SERVERS", "status": "active"},
        {"vid": 30, "name": "GUEST", "status": "active"},
    ])

    assert vid_map[10] == 50
    assert vid_map[20] == 51
    # marked existing -> updated; manual -> untouched; new -> created with group
    assert {u["id"] for u in api.ipam.vlans.updated} == {50}
    assert len(api.ipam.vlans.created) == 1
    assert api.ipam.vlans.created[0]["vid"] == 30
    assert api.ipam.vlans.created[0]["group"] == 8
    assert api.ipam.vlans.created[0]["description"].startswith(cisco.VLAN_MARKER)
    assert vid_map[30] is not None


def test_sync_interface_vlans_access_trunk_and_tagged_all(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="Gi1/0/1", device_id=7),
        FakeRecord(2, name="Gi1/0/2", device_id=7),
        FakeRecord(3, name="Gi1/0/3", device_id=7),
        FakeRecord(4, name="Gi1/0/4", device_id=7),
    ])
    api = SimpleNamespace(
        dcim=SimpleNamespace(interfaces=ifaces_ep),
        ipam=SimpleNamespace(vlans=FakeEndpoint()))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"port": "Gi1/0/1", "name": "", "status": "connected", "vlan": "10",
         "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"},
        {"port": "Gi1/0/2", "name": "", "status": "connected", "vlan": "trunk",
         "duplex": "full", "speed": "1000", "type": "1000BaseSX SFP"},
        {"port": "Gi1/0/3", "name": "", "status": "connected", "vlan": "trunk",
         "duplex": "full", "speed": "10G", "type": "SFP-10GBase-SR"},
        {"port": "Gi1/0/4", "name": "", "status": "connected", "vlan": "routed",
         "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"},
    ]
    trunks = [
        {"port": "Gi1/0/2", "mode": "on", "native": 1,
         "allowed": "1-4094", "active": "1-4094"},
        {"port": "Gi1/0/3", "mode": "on", "native": 10,
         "allowed": "1,10,20-22", "active": "10,20-22"},
    ]
    vid_map = {1: 101, 10: 110, 20: 120, 21: 121, 22: 122, 99: 199}
    cisco.sync_interface_vlans(7, ports, trunks, vid_map)

    by_id = {u["id"]: u for u in ifaces_ep.updated}
    assert by_id[1]["mode"] == "access" and by_id[1]["untagged_vlan"] == 110
    assert by_id[2]["mode"] == "tagged-all"      # 1-4094 -> no explicit list
    assert by_id[2]["untagged_vlan"] == 101
    assert by_id[3]["mode"] == "tagged"
    assert by_id[3]["untagged_vlan"] == 110
    assert by_id[3]["tagged_vlans"] == [110, 120, 121, 122]
    assert 4 not in by_id                        # routed -> untouched


def test_sweep_stale_vlans(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    seen = FakeRecord(50, vid=10, group_id=8, description="netbox-sync: last seen SW1")
    stale = FakeRecord(51, vid=20, group_id=8, description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=30, group_id=8, description="manual vlan")
    api = _vlan_api([seen, stale, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_stale_vlans(8, {10, 40})

    assert api.ipam.vlans.deleted_ids == [51]


def test_ensure_vlan_group_reuses_by_key_and_names_next_bd(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g1 = FakeRecord(60, name="BD1", description="netbox-sync: vtp=snapp",
                    scope_type="dcim.site", scope_id=3)
    g2 = FakeRecord(61, name="BD3", description="netbox-sync: vtp=other",
                    scope_type="dcim.site", scope_id=3)
    manual = FakeRecord(62, name="BD2", description="manual group",
                        scope_type="dcim.site", scope_id=3)
    api = _vlan_api([], [g1, g2, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # existing key -> reused, nothing created
    assert cisco.ensure_vlan_group(3, "snapp") == 60
    assert api.ipam.vlan_groups.created == []

    # new key -> created with next FREE BD number among marked groups (BD3+1)
    gid = cisco.ensure_vlan_group(3, "campus-b")
    created = api.ipam.vlan_groups.created[0]
    assert created["name"] == "BD4"
    assert created["slug"] == "bd4"
    assert created["description"] == "netbox-sync: vtp=campus-b"
    assert created["scope_type"] == "dcim.site"
    assert created["scope_id"] == 3
    assert gid is not None


def test_sweep_legacy_site_vlans(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    legacy = FakeRecord(50, vid=10, site_id=3, group=None,
                        description="netbox-sync: last seen SW1")
    grouped = FakeRecord(51, vid=10, site_id=None, group=8,
                         description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=20, site_id=3, group=None,
                        description="manual vlan")
    api = _vlan_api([legacy, grouped, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_legacy_site_vlans(3)

    assert api.ipam.vlans.deleted_ids == [50]   # only the group-less marked one


def test_sweep_stale_groups_migration(monkeypatch):
    """Stale marked groups (case-variant duplicate, abandoned hostname
    fallback) are emptied and deleted; fed groups and manual groups stay."""
    import netbox_sync.collectors.cisco as cisco
    g_snapp   = FakeRecord(1, name="BD1", description="netbox-sync: vtp=snapp",
                           scope_type="dcim.site", scope_id=3)
    g_SNAPP   = FakeRecord(2, name="BD2", description="netbox-sync: vtp=Snapp",
                           scope_type="dcim.site", scope_id=3)
    g_fb      = FakeRecord(4, name="BD4", description="netbox-sync: vtp=f12-cctv-sw-02",
                           scope_type="dcim.site", scope_id=3)
    manual_g  = FakeRecord(9, name="X", description="manual group",
                           scope_type="dcim.site", scope_id=3)
    vlans = [
        FakeRecord(50, vid=201, group_id=1, description="netbox-sync: x"),
        FakeRecord(51, vid=202, group_id=2, description="netbox-sync: x"),
        FakeRecord(52, vid=20,  group_id=4, description="netbox-sync: x"),
        FakeRecord(53, vid=999, group_id=9, description="manual vlan"),
    ]
    api = _vlan_api(vlans, [g_snapp, g_SNAPP, g_fb, manual_g])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # BD1(snapp) is fed this run; f12-cctv-sw-02 moved to a component group
    cisco._sweep_stale_groups(
        3, fed_group_ids={1},
        key_by_name={"f12-cctv-sw-02": "f_-1-cctv-sw"})

    # BD2 (case-variant) and BD4 (abandoned fallback) lost their VLANs,
    # then were deleted themselves; BD1 and the manual group untouched
    assert set(api.ipam.vlans.deleted_ids) == {51, 52}
    assert set(api.ipam.vlan_groups.deleted_ids) == {2, 4}


def test_interface_syncs_preserve_mgmt_interfaces(monkeypatch):
    """The synthetic mgmt interface must survive the stale-interface cleanup
    in both Cisco and SAN interface syncs."""
    import netbox_sync.collectors.brocade as brocade_mod
    import netbox_sync.collectors.cisco as cisco_mod
    for mod, sync_fn, port in (
            (cisco_mod, cisco_mod.sync_cisco_interfaces,
             {"port": "Gi1/0/1", "name": "", "status": "connected",
              "vlan": "1", "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"}),
            (brocade_mod, brocade_mod.sync_san_interfaces,
             {"index": 0, "port": 0, "address": "010000", "media": "id",
              "speed": "N16", "state": "Online", "proto": "FC", "comment": ""})):
        ifaces_ep = FakeEndpoint([
            FakeRecord(1, name="mgmt", device_id=7, mgmt_only=True),
            FakeRecord(2, name="stale-iface", device_id=7, mgmt_only=False),
        ])
        monkeypatch.setattr(nbx, "get_netbox",
                            lambda: _fake_api(interfaces=ifaces_ep))
        if sync_fn is cisco_mod.sync_cisco_interfaces:
            sync_fn(7, [port])
        else:
            sync_fn(7, [port], [])
        assert 1 not in ifaces_ep.deleted_ids   # mgmt preserved
        assert 2 in ifaces_ep.deleted_ids       # stale removed

# ── Camera interface + camera→switch cabling ────────────────────────────────

def test_camera_interface_created_when_missing(monkeypatch):
    ifaces = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces))

    iface_id = nbx.ensure_camera_interface(100, online=False)

    assert len(ifaces.created) == 1
    payload = ifaces.created[0]
    assert payload["device"] == 100
    assert payload["name"] == "eth0"
    assert payload["type"] == "1000base-t"
    assert payload["enabled"] is False
    assert payload["description"] == "netbox-sync: camera LAN"
    assert iface_id is not None


def test_camera_interface_refreshes_enabled_only_on_drift(monkeypatch):
    existing = FakeRecord(11, name="eth0", device_id=100, enabled=True)
    ifaces = FakeEndpoint([existing])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces))

    assert nbx.ensure_camera_interface(100, online=True) == 11
    assert ifaces.update_calls == 0          # already in sync -> no write

    assert nbx.ensure_camera_interface(100, online=False) == 11
    assert ifaces.updated == [{"id": 11, "enabled": False}]
    assert ifaces.created == []


_CAM_IFACE = FakeRecord(11, name="eth0", device_id=100)
_SW_IFACE = FakeRecord(55, name="Gi1/0/5", device_id=7)
_SW_IFACE2 = FakeRecord(77, name="Gi1/0/9", device_id=7)
_CAM_MAC_MAP = {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/5", 10)}
_CAM_SWITCHES = {"10.0.0.1": {"dev_id": 7, "name": "SW1"}}


def _cam_cable_api(sw_ifaces, cables):
    return _fake_api(interfaces=FakeEndpoint([_CAM_IFACE] + sw_ifaces),
                     cables=FakeEndpoint(cables))


def test_camera_cable_created(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cam_cable_api([_SW_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert len(api.dcim.cables.created) == 1
    payload = api.dcim.cables.created[0]
    assert payload["a_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 11}]
    assert payload["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 55}]
    assert payload["description"] == "netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5"


def test_camera_cable_refreshed_when_unchanged(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert {u["id"] for u in api.dcim.cables.updated} == {9}
    # refresh only — terminations untouched
    assert "a_terminations" not in api.dcim.cables.updated[0]


def test_camera_cable_moved_when_mac_found_elsewhere(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE, _SW_IFACE2], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    moved_map = {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/9", 10)}

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            moved_map, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert len(api.dcim.cables.updated) == 1
    upd = api.dcim.cables.updated[0]
    assert upd["id"] == 9
    assert upd["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 77}]


def test_camera_cable_kept_when_mac_absent(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            {}, _CAM_SWITCHES)   # empty map: aged out

    assert api.dcim.cables.created == []
    assert api.dcim.cables.updated == []
    assert api.dcim.cables.deleted_ids == []    # keep-on-absence


def test_camera_cable_never_overrides_manual_cable(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    manual = FakeRecord(8, device_id=100, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert api.dcim.cables.updated == []


def test_camera_cable_skips_when_switch_iface_missing(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cam_cable_api([], [])   # switch interface not in NetBox
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []


def test_cdp_sweep_preserves_mac_table_cables(monkeypatch):
    """Camera cables (netbox-sync: mac-table ...) share the CABLE_MARKER
    prefix; the CDP reconciler must never sweep or adopt them."""
    import netbox_sync.collectors.cisco as cisco
    cam_cable = FakeRecord(10, device_id=7,
                           description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                           a_terminations=[{"object_type": "dcim.interface", "object_id": 99}],
                           b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [cam_cable])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.deleted_ids == []
    assert 10 not in {u["id"] for u in api.dcim.cables.updated}


def test_camera_cable_move_blocked_by_manual_cable(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    manual = FakeRecord(8, device_id=100, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 88}])
    api = _cam_cable_api([_SW_IFACE, _SW_IFACE2], [marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    moved_map = {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/9", 10)}

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            moved_map, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert api.dcim.cables.updated == []      # move blocked by the manual cable

# ── Camera name-collision suffix ─────────────────────────────────────────────

def test_camera_device_suffixed_when_name_taken_by_other_role(monkeypatch):
    """A camera titled 'GF' at a site where an AP named 'GF' already exists
    must be created as 'GF-cam<ch>' (deterministic), not fail with NetBox's
    per-site name-uniqueness 400."""
    ap_role = FakeRecord(50, name="Access Point")
    ap_dev = FakeRecord(60, name="GF", site_id=7, role_id=50)
    devices_ep = FakeEndpoint([ap_dev])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda name, color: 51)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda name: 7)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda m, mfr: 77)
    monkeypatch.setattr(nbx, "resolve_site", lambda name, ip: "S")
    monkeypatch.setattr(nbx, "find_device", lambda serial, role_name=None: None)

    cam = {"name": "GF", "channel": 11, "ip": "192.168.252.33",
           "model": "DS-2CD1143G0-I", "serial": "DS-2CD1143G0-I20211208AAWRJ21084000",
           "online": True}
    dev_id = nbx.ensure_camera_device(cam, "dahua-nvr", manufacturer="Hikvision")

    assert devices_ep.created[0]["name"] == "GF-cam11"
    assert dev_id is not None


def test_camera_update_keeps_suffix_when_plain_name_taken(monkeypatch):
    """A serial-matched camera previously suffixed ('GF-cam11') must NOT be
    renamed back to 'GF' while a UniFi AP holds that name — the rename 400s,
    and the failure then cascaded into a false offline sweep."""
    serial = "DS-2CD1143G0-I20211208AAWRJ21084215"
    ap_dev = FakeRecord(141, name="GF", site_id=7, role_id=50)   # the UniFi AP
    cam_dev = FakeRecord(270, name="GF-cam11", site_id=7, role_id=51,
                         role=SimpleNamespace(name="Camera"),
                         serial=serial,
                         custom_fields={"cam_serial": serial})
    devices_ep = FakeEndpoint([ap_dev, cam_dev])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda name, color: 51)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda name: 7)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda m, mfr: 77)
    monkeypatch.setattr(nbx, "resolve_site", lambda name, ip: "S")

    cam = {"channel": 11, "name": "GF", "ip": "192.168.252.33",
           "model": "DS-2CD1143G0-I", "serial": serial, "online": True}
    dev_id = nbx.ensure_camera_device(cam, "dahua-192-168-252-5")

    assert dev_id == 270
    assert devices_ep.updated[-1]["name"] == "GF-cam11"   # NOT renamed to "GF"
    assert devices_ep.created == []


def test_camera_adoption_never_steals_other_cameras_device(monkeypatch):
    """Name+site+role adoption must skip devices that already carry a
    DIFFERENT cam_serial — that device belongs to another camera."""
    other = FakeRecord(90, name="GF", site_id=7, role_id=51,
                       serial="OTHER-SERIAL",
                       custom_fields={"cam_serial": "OTHER-SERIAL"})
    devices_ep = FakeEndpoint([other])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda name, color: 51)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda name: 7)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda m, mfr: 77)
    monkeypatch.setattr(nbx, "resolve_site", lambda name, ip: "S")
    monkeypatch.setattr(nbx, "find_device", lambda serial, role_name=None: None)

    cam = {"channel": 12, "name": "GF", "ip": "192.168.252.34",
           "model": "DS-2CD1143G0-I", "serial": "NEW-SERIAL", "online": True}
    dev_id = nbx.ensure_camera_device(cam, "dahua-192-168-252-5")

    assert dev_id != 90                                  # not adopted
    assert devices_ep.created[0]["name"] == "GF-cam12"   # new, suffixed
    assert devices_ep.created[0]["serial"] == "NEW-SERIAL"

# ── Custom-field UI visibility normalization ─────────────────────────────────

class _FakeCFEndpoint:
    def __init__(self, records):
        self._records = records
        self.updated = []
    def all(self):
        return list(self._records)
    def update(self, payload_list):
        self.updated.extend(payload_list)
        return True


def test_custom_fields_normalized_to_if_set(monkeypatch):
    recs = [
        FakeRecord(1, ui_visible={"value": "always", "label": "Always"}),
        FakeRecord(2, ui_visible={"value": "if-set", "label": "If set"}),
        FakeRecord(3, ui_visible="hidden"),
    ]
    ep = _FakeCFEndpoint(recs)
    api = SimpleNamespace(extras=SimpleNamespace(custom_fields=ep))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_custom_fields_if_set()

    assert ep.updated == [{"id": 1, "ui_visible": "if-set"},
                          {"id": 3, "ui_visible": "if-set"}]


def test_custom_fields_noop_when_all_if_set(monkeypatch):
    ep = _FakeCFEndpoint([FakeRecord(1, ui_visible={"value": "if-set"}),
                          # pynetbox choice fields stringify to the label
                          FakeRecord(2, ui_visible="If set")])
    api = SimpleNamespace(extras=SimpleNamespace(custom_fields=ep))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_custom_fields_if_set()

    assert ep.updated == []


def test_ensure_custom_fields_creates_missing(monkeypatch):
    """A fresh NetBox has none of the tool's CFs: ensure_custom_fields()
    creates them all (dcim.device, ui_visible=if-set), skips existing ones,
    and still normalizes visibility on the rest."""
    existing = FakeRecord(1, name="bmc_ip", ui_visible={"value": "always"})

    class _CFFake:
        def __init__(self):
            self.created = []
            self.updated = []
        def all(self):
            return [existing] + [FakeRecord(100 + i, name=p["name"],
                                            ui_visible=p.get("ui_visible"))
                                 for i, p in enumerate(self.created)]
        def create(self, payload):
            self.created.append(payload)
            return FakeRecord(999, **payload)
        def update(self, payload_list):
            self.updated.extend(payload_list)
            return True

    ep = _CFFake()
    api = SimpleNamespace(extras=SimpleNamespace(custom_fields=ep))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_custom_fields()

    created_names = [p["name"] for p in ep.created]
    assert len(created_names) == len(nbx.CUSTOM_FIELDS) - 1   # bmc_ip existed
    assert "bmc_ip" not in created_names
    for p in ep.created:
        assert p["object_types"] == ["dcim.device"]
        assert p["ui_visible"] == "if-set"
        assert p["type"] in ("text", "integer", "boolean")
    # visibility normalization still ran on the pre-existing field
    assert {"id": 1, "ui_visible": "if-set"} in ep.updated


def test_custom_fields_registry_sanity():
    names = [n for n, _, _ in nbx.CUSTOM_FIELDS]
    assert len(names) == len(set(names))            # no duplicates
    assert all(t in ("text", "integer", "boolean")
               for _, t, _ in nbx.CUSTOM_FIELDS)
    assert all(label for _, _, label in nbx.CUSTOM_FIELDS)
