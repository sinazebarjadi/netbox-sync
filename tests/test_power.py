"""Power-monitoring: model->watts table, CLI power parsing, and the
power-port/feed/aggregation logic against in-memory fakes."""
import pytest

import netbox_sync.config as cfg
import netbox_sync.collectors.power as pw
import netbox_sync.netbox as nbx
from tests.test_netbox_sync import FakeEndpoint, FakeRecord, _fake_api


@pytest.fixture(autouse=True)
def _reset_netbox_cache():
    """get_netbox() caches the api in a module-global; clear it so each test's
    monkeypatched api is the one actually used."""
    nbx.nb = None
    yield
    nbx.nb = None


# ── config.power_watts_for_model ─────────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("ProLiant DL380 Gen9", 500),
    ("ProLiant DL380 Gen10", 600),
    ("C9500-48Y4C", 650),
    ("C9300X-24Y", 450),
    ("WS-C2960X-48TS-L", 90),
    ("FortiGate 100F", 60),
    ("FortiGate 60F", 25),
    ("Copper Patch Panel", 0),
    ("Fiber Patch Panel", 0),
])
def test_power_watts_known_models(model, expected):
    assert cfg.power_watts_for_model(model) == expected


def test_power_watts_unknown_uses_default():
    assert cfg.power_watts_for_model("Totally-Unknown-9000") == cfg.POWER_DEFAULT_WATTS


def test_power_watts_none_and_empty():
    assert cfg.power_watts_for_model(None) == cfg.POWER_DEFAULT_WATTS
    assert cfg.power_watts_for_model("") == cfg.POWER_DEFAULT_WATTS


def test_power_watts_longest_match_wins():
    # 'c9300x' must beat 'c9300'
    assert cfg.power_watts_for_model("C9300X-24Y") == 450
    assert cfg.power_watts_for_model("C9300-24P") == 350


# ── cisco CLI power parsing ──────────────────────────────────────────────────

class _FakeSess:
    def __init__(self, out): self._out = out
    def run(self, cmd): return self._out


def test_cisco_power_classic_ios():
    out = "System Power Total (AC) 70 Watts\nSystem Power Used (AC)  33 Watts\n"
    assert pw.cisco_power_watts(_FakeSess(out)) == 33


def test_cisco_power_iosxe_capacity_only_returns_none():
    out = ("Power  Model No  Type  Capacity  Status\n"
           "PS0    C9K-PWR-650WAC-R  AC  650 W  ok  good\n")
    assert pw.cisco_power_watts(_FakeSess(out)) is None


def test_cisco_power_multiple_switches_sum():
    out = ("System Power Used (AC)  33 Watts\n"
           "System Power Used (AC)  40 Watts\n")
    assert pw.cisco_power_watts(_FakeSess(out)) == 73


# ── collect_device_power dispatch ────────────────────────────────────────────

def test_dispatch_prefers_live_redfish():
    data = {"summary": {"power_watts": 412}}
    w, src = pw.collect_device_power(family="server", data=data, model="DL380 Gen9")
    assert (w, src) == (412, "live")


def test_dispatch_cisco_cli():
    sess = _FakeSess("System Power Used (AC)  33 Watts\n")
    w, src = pw.collect_device_power(family="cisco", session=sess, model="WS-C2960X-48TS-L")
    assert (w, src) == (33, "cli")


def test_dispatch_falls_back_to_default():
    w, src = pw.collect_device_power(family="cisco", session=None, model="C9500-48Y4C")
    assert (w, src) == (650, "default")


# ── NetBox power-port / feed logic ───────────────────────────────────────────

def _power_api():
    rack = FakeRecord(1, name="R16")
    feed = FakeRecord(5, rack_id=1, rack=rack, voltage=220, amperage=25, max_utilization=100)
    # pynetbox exposes both dev.rack (nested) and a rack_id convenience field
    dev = FakeRecord(7, rack=rack, rack_id=1)
    return _fake_api(
        racks=FakeEndpoint([rack]),
        power_feeds=FakeEndpoint([feed]),
        power_ports=FakeEndpoint([]),
        cables=FakeEndpoint([]),
        devices=FakeEndpoint([dev]),
    )


def test_sync_device_power_creates_port_and_cable(monkeypatch):
    api = _power_api()
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    ok = nbx.sync_device_power(7, 500)
    assert ok is True
    assert len(api.dcim.power_ports.created) == 1
    assert api.dcim.power_ports.created[0]["allocated_draw"] == 500
    assert len(api.dcim.cables.created) == 1


def test_sync_device_power_idempotent(monkeypatch):
    api = _power_api()
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    assert nbx.sync_device_power(7, 500) is True
    assert nbx.sync_device_power(7, 500) is True   # same watts -> no port update
    assert len(api.dcim.power_ports.created) == 1  # no duplicate port


def test_sync_device_power_no_rack_skips(monkeypatch):
    api = _power_api()
    api.dcim.devices.items[0].rack_id = None
    api.dcim.devices.items[0].rack = None
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    assert nbx.sync_device_power(7, 500) is False
    assert len(api.dcim.power_ports.created) == 0


def test_sync_rack_power_aggregation(monkeypatch):
    api = _power_api()
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    nbx.sync_device_power(7, 500)
    # sanity: the port must be discoverable in the rack
    assert len(nbx._power_ports_in_rack(api, 1)) == 1
    # 220V * 25A * 100% = 5500W; 500W -> ~9.1%
    total, util = nbx.sync_rack_power(1, per_device_watts={7: 500})
    assert total == 500
    assert util == pytest.approx(9.1, abs=0.05)


def test_sync_rack_power_no_feed_returns_none(monkeypatch):
    api = _fake_api(racks=FakeEndpoint([FakeRecord(9, name="RX")]),
                    power_feeds=FakeEndpoint([]))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    assert nbx.sync_rack_power(9) is None
