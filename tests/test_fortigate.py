"""Tests for the FortiGate REST API mappers (pure JSON -> dict)."""
import pytest
import requests

import netbox_sync.collectors.fortigate as mod


STATUS_JSON = {"http_method": "GET", "results": {
    "hostname": "FW-DC-01", "serial_number": "FGT60FTK2100XXXX",
    "model": "FortiGate-60F", "version": "v7.2.4"}}

MONITOR_IFACES = {"results": {
    "port1": {"link": True, "speed": "1000full", "duplex": "full"},
    "port2": {"link": False, "speed": "1000", "duplex": "full"},
    "port1.10": {"link": True, "speed": "1000full"}}}

CMDB_IFACES = {"results": [
    {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0",
     "alias": "UPLINK-CORE"},
    {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
    {"name": "port1.10", "type": "vlan", "vlanid": 10,
     "interface": "port1", "ip": "10.10.10.1 255.255.255.0"},
]}


def test_fg_status():
    out = mod._fg_status(STATUS_JSON)
    assert out == {"hostname": "FW-DC-01", "serial": "FGT60FTK2100XXXX",
                   "model": "FortiGate-60F", "version": "v7.2.4"}


# Real /monitor/system/status from a FortiGate 1800F running FortiOS 7.2.13:
# serial is TOP LEVEL, model is split into model_name/model_number.
STATUS_JSON_72 = {"http_method": "GET",
                  "serial": "FG180FTK21901250", "version": "v7.2.13",
                  "results": {"model_name": "FortiGate",
                              "model_number": "1800F", "model": "FG180F",
                              "hostname": "HQ",
                              "log_disk_status": "not_available"}}


def test_fg_status_fortios_72_real_shape():
    out = mod._fg_status(STATUS_JSON_72)
    assert out["serial"] == "FG180FTK21901250"
    assert out["model"] == "FortiGate 1800F"
    assert out["hostname"] == "HQ"
    assert out["version"] == "v7.2.13"


def test_fg_interfaces_merge():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["link"] is True
    assert ports["port1"]["speed_mbps"] == 1000
    assert ports["port2"]["link"] is False
    assert ports["port2"]["speed_mbps"] == 1000
    assert ports["port1.10"]["vlanid"] == 10
    assert ports["port1.10"]["parent"] == "port1"


def test_fg_interfaces_capture_alias():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["alias"] == "UPLINK-CORE"
    assert ports["port2"]["alias"] == ""


def test_fg_interfaces_include_cmdb_vlan_subifs():
    """monitor/system/interface on FortiOS 7.2 reports ONLY physical ports —
    VLAN subinterfaces exist only in cmdb config and must be unioned in."""
    mon = {"results": {
        "port1": {"link": True, "speed": "1000full"},
        "port2": {"link": False, "speed": "1000"}}}
    cmdb = {"results": [
        {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
        {"name": "AP MGMT", "type": "vlan", "vlanid": 51,
         "interface": "Core Switch", "ip": "172.31.2.1 255.255.255.0"},
        {"name": "PartnerNet", "type": "vlan", "vlanid": 10,
         "interface": "PO5", "ip": "203.0.113.184 255.255.255.240"},
    ]}
    ports = {p["name"]: p for p in mod._fg_interfaces(mon, cmdb)}
    assert len(ports) == 4
    assert ports["AP MGMT"]["vlanid"] == 51
    assert ports["AP MGMT"]["parent"] == "Core Switch"
    assert ports["AP MGMT"]["link"] is True
    assert ports["PartnerNet"]["vlanid"] == 10


def test_fg_interfaces_include_aggregates_with_members():
    """Aggregates live only in cmdb (not in monitor) and must be added with
    their member lists for LAG linkage."""
    mon = {"results": {
        "port33": {"link": True, "speed": "10000full"},
        "port34": {"link": True, "speed": "10000full"}}}
    cmdb = {"results": [
        {"name": "port33", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "port34", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "Core Switch", "type": "aggregate",
         "member": [{"interface-name": "port33"},
                    {"interface-name": "port34"}]},
    ]}
    ports = {p["name"]: p for p in mod._fg_interfaces(mon, cmdb)}
    assert "Core Switch" in ports
    assert ports["Core Switch"]["type"] == "lag"
    assert ports["Core Switch"]["members"] == ["port33", "port34"]


def test_ssh_command_failure_detected():
    class FakeSess:
        def run(self, cmd):
            return "8757: Unknown action 0\nCommand fail. Return code -1"
    assert mod._ssh_run_or_none(FakeSess(), "diagnose lldp neighbor-summary",
                                "lldp") is None

    class GoodSess:
        def run(self, cmd):
            return "port1 00:1c:73:ab:cd:ef SW1 B,R 120 Gi1/0/1"
    assert mod._ssh_run_or_none(GoodSess(), "x", "lldp") is not None


IFCONFIG_A = """AP MGMT\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:24
\tinet addr:172.31.2.1  Bcast:172.31.2.255  Mask:255.255.255.0

PartnerNet\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:26
\tinet addr:203.0.113.184  Bcast:203.0.113.191  Mask:255.255.255.240
"""


def test_parse_ifconfig_a():
    out = mod._parse_ifconfig_a(IFCONFIG_A)
    assert out == {"AP MGMT": "00:09:0f:09:00:24",
                   "PartnerNet": "00:09:0f:09:00:26"}


# Real HA output from the user's FG180F pair (FortiOS 7.2.13)
HA_STATS = {"results": [
    {"hostname": "HQ", "serial_no": "FG180FTK21901250", "sessions": 13720},
    {"hostname": "HQ-Secondary", "serial_no": "FG180FTK22900291", "sessions": 1478},
]}
HA_CHECKSUMS = {"results": [
    {"is_manage_primary": True, "is_root_primary": True,
     "is_manage_master": 1, "is_root_master": 1,
     "serial_no": "FG180FTK21901250"},
    {"is_manage_primary": False, "is_root_primary": False,
     "is_manage_master": 0, "is_root_master": 0,
     "serial_no": "FG180FTK22900291"},
]}
HA_CFG = {"results": {"group-id": 0, "group-name": "Z-Cluster-FW", "mode": "a-p"}}


def test_fg_ha_clustered_real_fixture():
    ha = mod._fg_ha(HA_STATS, HA_CHECKSUMS, HA_CFG)
    assert ha["clustered"] is True
    assert ha["group_name"] == "Z-Cluster-FW"
    assert ha["mode"] == "a-p"
    assert ha["primary_serial"] == "FG180FTK21901250"
    assert ha["primary_hostname"] == "HQ"
    by_serial = {u["serial"]: u for u in ha["units"]}
    assert by_serial["FG180FTK21901250"]["is_primary"] is True
    assert by_serial["FG180FTK22900291"]["is_primary"] is False


def test_fg_ha_standalone():
    ha = mod._fg_ha({"results": [{"hostname": "FGT-1", "serial_no": "S1"}]},
                    {"results": []}, {"results": {}})
    assert ha["clustered"] is False
    assert ha["group_name"] == ""


# Real-shape firewall NAT fixtures (trimmed from the live FG180F)
FW_VIP = {"results": [
    {"name": "ServiceA-443", "extip": "203.0.113.164", "extport": 443,
     "mappedip": [{"range": "172.31.5.53"}], "mappedport": 443,
     "protocol": "tcp", "portforward": "enable", "status": "enable"},
]}

FW_IPPOOL = {"results": [
    {"name": "203.0.113.186", "type": "overload",
     "startip": "203.0.113.186", "endip": "203.0.113.186"},
]}


def test_fg_firewall_vips():
    out = mod._fg_firewall_vips(FW_VIP)
    assert out == [{"kind": "vip", "name": "ServiceA-443",
                    "extip": "203.0.113.164", "extport": 443,
                    "mappedip": ["172.31.5.53"], "mappedport": 443,
                    "protocol": "tcp", "portforward": "enable",
                    "status": "enable"}]


def test_fg_firewall_ippools():
    out = mod._fg_firewall_ippools(FW_IPPOOL)
    assert out == [{"kind": "pool", "name": "203.0.113.186",
                    "type": "overload", "startip": "203.0.113.186",
                    "endip": "203.0.113.186"}]


def test_fg_vlans():
    vlans = mod._fg_vlans(CMDB_IFACES)
    assert vlans == [{"vid": 10, "name": "port1.10", "status": "active"}]


def test_fg_interface_type():
    assert mod._fg_interface_type(100) == "100base-tx"
    assert mod._fg_interface_type(1000) == "1000base-t"
    assert mod._fg_interface_type(10000) == "10gbase-t"
    assert mod._fg_interface_type(None) == "other"


def test_fortigate_session_login_flow(monkeypatch):
    """Sessions log in via /logincheck with the admin secretkey and retry on 401."""
    calls = []

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text
            self._text = text
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(self.status_code)
        def json(self):
            return {"ok": True}

    class FakeSession(requests.Session):
        verify = False
        def post(self, url, data=None, timeout=None):
            calls.append(("POST", url, data))
            return FakeResp(200)
        def get(self, url, timeout=None):
            calls.append(("GET", url, None))
            if len([c for c in calls if c[0] == "GET"]) == 1:
                return FakeResp(401)
            return FakeResp(200)

    monkeypatch.setattr(mod, "FORTIGATE_USER", "netbox")
    monkeypatch.setattr(mod, "FORTIGATE_PASS", "s3cret")
    monkeypatch.setattr(mod.requests, "Session", FakeSession)

    sess = mod.FortiGateSession("10.0.0.1", 58291)
    assert calls[0] == ("POST", "https://10.0.0.1:58291/logincheck",
                        {"username": "netbox", "secretkey": "s3cret"})

    out = sess.get("/api/v2/monitor/system/status")
    assert out == {"ok": True}
    # 401 on first GET -> re-login (second POST) then retry
    assert [c[0] for c in calls].count("POST") == 2


LLDP_SUMMARY = """-----------------------------------------------------------------------------
Port        Device ID          SysName          Capabilities  TTL   Port ID
port1       00:1c:73:ab:cd:ef  SW-BRANCH-02      B,R           120   Gi1/0/1
port2       00:1c:73:ab:cd:00  R4-Core-LAN-SW   B,R           120   Twe1/0/31
"""


def test_parse_lldp_summary():
    entries = mod._parse_lldp_summary(LLDP_SUMMARY)
    assert len(entries) == 2
    assert entries[0]["device_id"] == "SW-BRANCH-02"
    assert entries[0]["local_intf"] == "port1"
    assert entries[0]["remote_intf"] == "Gi1/0/1"


TRANSCEIVERS = """Port 1  : SFP/SFP+ (10G)
   Vendor            : FINISAR CORP.
   Part Number       : FTLX8571D3BCL
   Serial Number     : ABC123456

Port 2  : SFP/SFP+ (10G)
   Vendor            : CISCO
   Part Number       : SFP-10G-SR
   Serial Number     : DEF789012
"""


def test_parse_transceivers():
    rows = mod._parse_transceivers(TRANSCEIVERS)
    assert len(rows) == 2
    assert rows[0]["vendor"] == "FINISAR CORP."
    assert rows[0]["part_number"] == "FTLX8571D3BCL"
    assert rows[0]["serial_number"] == "ABC123456"
    assert rows[1]["port"] == 2


def test_fg_get_raises_auth_error_on_persistent_401():
    """A 401 persisting AFTER a fresh re-login means rejected credentials."""
    class FakeResp:
        status_code = 401
        text = ""
        def raise_for_status(self):
            raise requests.HTTPError(401)
        def json(self):
            return {}

    class FakeSession(requests.Session):
        verify = False
        def post(self, url, data=None, timeout=None):
            class R:
                status_code = 200
                text = ""
            return R()
        def get(self, url, timeout=None):
            return FakeResp()

    sess = object.__new__(mod.FortiGateSession)
    sess.base = "https://10.0.0.1:58291"
    sess.s = FakeSession()
    sess.timeout = 5

    with pytest.raises(mod.FortiGateAuthError):
        sess.get("/api/v2/monitor/system/status")


def test_probe_fortigate_fails_fast_on_auth_rejection(monkeypatch):
    """Auth rejection is a config error: no retry sleeps, immediate None."""
    sleeps = []
    monkeypatch.setattr(mod, "FORTIGATE_USER", "u")
    monkeypatch.setattr(mod, "FORTIGATE_PASS", "p")
    monkeypatch.setattr(mod, "is_port_open", lambda *a, **kw: True)
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))

    class RejectingSession:
        def __init__(self, ip, port, timeout=30):
            pass
        def get(self, path):
            raise mod.FortiGateAuthError("rejected")

    monkeypatch.setattr(mod, "FortiGateSession", RejectingSession)
    assert mod.probe_fortigate("10.0.0.1", retries=2, retry_delay=3) is None
    assert sleeps == []
