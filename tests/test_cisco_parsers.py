"""Tests for the Cisco IOS / IOS-XE CLI output parsers."""
import netbox_sync.collectors.cisco as mod


SHOW_SWITCH_STACK = """Switch/Stack Mac Address : 247e.1269.0200 - Local Mac Address
Mac persistency wait time: Indefinite
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------------------------------
*1       Active   247e.1269.0200     5      V07     Ready
 2       Standby  247e.12e4.a300     1      V07     Ready
"""

SHOW_SWITCH_SINGLE = """Switch/Stack Mac Address : d009.c86a.fc80 - Local Mac Address
Mac persistency wait time: Indefinite
                                             H/W   Current
Switch#   Role    Mac Address     Priority Version  State
-------------------------------------------------------------------------------------
*1       Active   d009.c86a.fc80     1      V03     Ready
"""

SHOW_INVENTORY_STACK = """NAME: "c38xx Stack", DESCR: "c38xx Stack"
PID: WS-C3850-24T-S    , VID: V07  , SN: FOC2148U0U9

NAME: "Switch 1", DESCR: "WS-C3850-24T-S"
PID: WS-C3850-24T-S    , VID: V07  , SN: FOC2148U0U9

NAME: "Switch 1 - Power Supply A", DESCR: "Switch 1 - Power Supply A"
PID: PWR-C1-350WAC     , VID: V01  , SN: ART2144FABT

NAME: "Switch 2", DESCR: "WS-C3850-24T-S"
PID: WS-C3850-24T-S    , VID: V07  , SN: FCW2148F0RA

NAME: "Switch 2 - Power Supply A", DESCR: "Switch 2 - Power Supply A"
PID: PWR-C1-350WAC     , VID: V01  , SN: ART2144FAAN
"""


def test_parse_show_switch_stack():
    out = mod._parse_show_switch(SHOW_SWITCH_STACK)
    assert out == [
        {"member": 1, "role": "Active", "mac": "247e.1269.0200", "state": "Ready"},
        {"member": 2, "role": "Standby", "mac": "247e.12e4.a300", "state": "Ready"},
    ]


def test_parse_show_switch_single():
    out = mod._parse_show_switch(SHOW_SWITCH_SINGLE)
    assert len(out) == 1
    assert out[0]["role"] == "Active"


def test_stack_members_joins_serials():
    switch_rows = mod._parse_show_switch(SHOW_SWITCH_STACK)
    inv_rows = mod._parse_show_inventory(SHOW_INVENTORY_STACK)
    stack = mod._stack_members(switch_rows, inv_rows)
    assert stack == [
        {"member": 1, "role": "Active", "mac": "247e.1269.0200",
         "state": "Ready", "serial": "FOC2148U0U9"},
        {"member": 2, "role": "Standby", "mac": "247e.12e4.a300",
         "state": "Ready", "serial": "FCW2148F0RA"},
    ]


def test_stack_members_skips_members_without_serial():
    # a member with no 'Switch N' inventory row (e.g. not ready) is dropped
    switch_rows = mod._parse_show_switch(SHOW_SWITCH_STACK)
    inv_rows = [r for r in mod._parse_show_inventory(SHOW_INVENTORY_STACK)
                if r.get("name") != "Switch 2"]
    stack = mod._stack_members(switch_rows, inv_rows)
    assert len(stack) == 1
    assert stack[0]["serial"] == "FOC2148U0U9"


SHOW_VERSION_IOSXE = """Cisco IOS Software [Fuji], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 16.9.4, RELEASE SOFTWARE (fc2)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2019 by Cisco Systems, Inc.

SW1 uptime is 12 weeks, 3 days, 4 hours, 22 minutes
System returned to ROM by Reload Command
System image file is "flash:cat9k_iosxe.16.09.04.SPA.bin"

cisco C9300-48U (X86) processor with 1316432K/6147K bytes of memory.
Processor board ID FOC2345X0AB

Model Number                       : C9300-48U
System Serial Number               : FOC2345X0AB
"""

SHOW_VERSION_CLASSIC = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(2)E7, RELEASE SOFTWARE (fc3)
Technical Support: http://www.cisco.com/techsupport

SW2 uptime is 30 weeks, 2 days, 1 hour, 5 minutes
System returned to ROM by power-on

cisco WS-C2960X-48FPS-L (APM86XXX) processor (revision B0) with 524288K bytes of memory.
Processor board ID FOC98765432
"""


def test_parse_show_version_iosxe():
    out = mod._parse_show_version(SHOW_VERSION_IOSXE)
    assert out["hostname"] == "SW1"
    assert out["model"] == "C9300-48U"
    assert out["serial"] == "FOC2345X0AB"
    assert out["ios_version"] == "16.9.4"


def test_parse_show_version_classic_ios():
    out = mod._parse_show_version(SHOW_VERSION_CLASSIC)
    assert out["hostname"] == "SW2"
    assert out["model"] == "WS-C2960X-48FPS-L"
    assert out["serial"] == "FOC98765432"
    assert out["ios_version"] == "15.2(2)E7"


SHOW_INVENTORY = """NAME: "Switch 1", DESCR: "C9300-48U"
PID: C9300-48U         , VID: V02  , SN: FOC2345X0AB

NAME: "Power Supply Module 0", DESCR: "350W AC Power Supply"
PID: PWR-C1-350WAC     , VID: V01  , SN: LIT23456789

NAME: "Fan Tray 0", DESCR: "Fan Tray"
PID: C9300-FAN-1       , VID: V01  , SN:

NAME: "GigabitEthernet1/1/1", DESCR: "1000BaseSX SFP"
PID: GLC-SX-MMD        , VID: V01  , SN: FNS12345678
"""


def test_parse_show_inventory():
    rows = mod._parse_show_inventory(SHOW_INVENTORY)
    assert len(rows) == 4
    assert rows[0]["pid"] == "C9300-48U"
    assert rows[0]["sn"] == "FOC2345X0AB"
    assert rows[1]["name"] == "Power Supply Module 0"
    assert rows[2]["pid"] == "C9300-FAN-1"
    assert rows[2]["sn"] is None
    assert rows[3]["descr"] == "1000BaseSX SFP"


INTERFACES_STATUS = """Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   Uplink to SW2      connected    trunk        full   1000 1000BaseSX SFP
Gi1/0/2   Server-01          connected    100        a-full a-1000 10/100/1000BaseTX
Gi1/0/3                        notconnect   1            auto   auto 10/100/1000BaseTX
Gi1/0/4                        disabled     1            auto   auto 10/100/1000BaseTX
Te1/1/1                        connected    trunk        full    10G SFP-10GBase-SR
"""


def test_parse_interfaces_status():
    ports = mod._parse_interfaces_status(INTERFACES_STATUS)
    assert len(ports) == 5
    p0 = ports[0]
    assert p0["port"] == "Gi1/0/1"
    assert p0["name"] == "Uplink to SW2"
    assert p0["status"] == "connected"
    assert p0["vlan"] == "trunk"
    assert p0["speed"] == "1000"
    assert p0["type"] == "1000BaseSX SFP"
    assert ports[2]["name"] == ""
    assert ports[2]["status"] == "notconnect"
    assert ports[4]["port"] == "Te1/1/1"
    assert ports[4]["speed"] == "10G"


CDP_DETAIL = """-------------------------
Device ID: SW2
Entry address(es):
  IP address: 10.0.0.2
Platform: cisco WS-C2960X-48FPS-L,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): GigabitEthernet1/0/24
Holdtime : 157 sec

-------------------------
Device ID: SW3.example.com
Entry address(es):
  IP address: 10.0.0.3
Platform: cisco C9300-48U,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/2,  Port ID (outgoing port): TenGigabitEthernet1/1/1
Holdtime : 164 sec
"""


def test_parse_cdp_detail():
    entries = mod._parse_cdp_detail(CDP_DETAIL)
    assert len(entries) == 2
    e0 = entries[0]
    assert e0["device_id"] == "SW2"
    assert e0["platform"] == "cisco WS-C2960X-48FPS-L"
    assert e0["local_intf"] == "GigabitEthernet1/0/1"
    assert e0["remote_intf"] == "GigabitEthernet1/0/24"
    assert e0["ip"] == "10.0.0.2"
    assert entries[1]["device_id"] == "SW3.example.com"


LLDP_DETAIL = """------------------------------------------------
Local Intf: Gi1/0/1
Chassis id: 001c.73ab.cd00
Port id: Gi1/0/24
Port Description: GigabitEthernet1/0/24
System Name: SW2

System Description:
Cisco IOS Software, C2960X Software
"""


def test_parse_lldp_detail():
    entries = mod._parse_lldp_detail(LLDP_DETAIL)
    assert len(entries) == 1
    assert entries[0]["device_id"] == "SW2"
    assert entries[0]["local_intf"] == "Gi1/0/1"
    assert entries[0]["remote_intf"] == "Gi1/0/24"


def test_short_intf():
    assert mod._short_intf("GigabitEthernet1/0/1") == "Gi1/0/1"
    assert mod._short_intf("TenGigabitEthernet1/1/1") == "Te1/1/1"
    assert mod._short_intf("FastEthernet0/1") == "Fa0/1"
    assert mod._short_intf("Port-channel1") == "Po1"
    assert mod._short_intf("Gi1/0/1") == "Gi1/0/1"


def test_eth_interface_type():
    assert mod._eth_interface_type("100", "10/100BaseTX") == "100base-tx"
    assert mod._eth_interface_type("a-1000", "10/100/1000BaseTX") == "1000base-t"
    assert mod._eth_interface_type("1000", "1000BaseSX SFP") == "1000base-x-sfp"
    assert mod._eth_interface_type("10G", "SFP-10GBase-SR") == "10gbase-x-sfpp"
    assert mod._eth_interface_type("10G", "10GBase-T") == "10gbase-t"
    assert mod._eth_interface_type("auto", "10/100/1000BaseTX") == "other"
    assert mod._eth_interface_type("", "") == "other"


VLAN_BRIEF = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/3, Gi1/0/4
10   USERS                            active    Gi1/0/2
20   SERVER VLAN                      active
100  fddi-default                     act/unsup
"""


def test_parse_vlan_brief():
    vlans = mod._parse_vlan_brief(VLAN_BRIEF)
    assert vlans == [
        {"vid": 1, "name": "default", "status": "active"},
        {"vid": 10, "name": "USERS", "status": "active"},
        {"vid": 20, "name": "SERVER VLAN", "status": "active"},
        {"vid": 100, "name": "fddi-default", "status": "act/unsup"},
    ]


INTERFACES_TRUNK = """Port        Mode             Encapsulation  Status        Native vlan
Gi1/0/1     on               802.1q         trunking      1
Te1/1/1     on               802.1q         trunking      10

Port        Vlans allowed on trunk
Gi1/0/1     1-4094
Te1/1/1     1,10,20

Port        Vlans allowed and active in management domain
Gi1/0/1     1,10
Te1/1/1     10,20-22

Port        Vlans in spanning tree forwarding state and not pruned
Gi1/0/1     1,10
"""


def test_parse_interfaces_trunk():
    trunks = {t["port"]: t for t in mod._parse_interfaces_trunk(INTERFACES_TRUNK)}
    assert trunks["Gi1/0/1"]["native"] == 1
    assert trunks["Gi1/0/1"]["allowed"] == "1-4094"
    assert trunks["Gi1/0/1"]["active"] == "1,10"
    assert trunks["Te1/1/1"]["native"] == 10
    assert trunks["Te1/1/1"]["active"] == "10,20-22"


def test_expand_vlan_list():
    assert mod._expand_vlan_list("1,10,20-22") == {1, 10, 20, 21, 22}
    assert mod._expand_vlan_list("1-4094") is None
    assert mod._expand_vlan_list("all") is None
    assert mod._expand_vlan_list("") is None


IP_BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES NVRAM  administratively down down
Vlan50                 172.31.1.103    YES NVRAM  up                    up
GigabitEthernet1/0/1   unassigned      YES unset  up                    up
"""


def test_parse_ip_interface_brief():
    out = mod._parse_ip_interface_brief(IP_BRIEF)
    assert out == {"Vlan50": "172.31.1.103"}


def test_mac_to_cisco():
    assert mod._mac_to_cisco("00:09:0F:09:00:24") == "0009.0f09.0024"


MAC_TABLE = """          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  51    0009.0f09.0024    DYNAMIC     Te1/1/1
"""


def test_parse_mac_table_entry():
    rows = mod._parse_mac_table_entry(MAC_TABLE)
    assert rows == [{"vid": 51, "mac": "00:09:0f:09:00:24", "port": "Te1/1/1"}]


# ── broadcast domain topology ────────────────────────────────────────────────

def test_norm_sw_name():
    assert mod._norm_sw_name("F12-CCTV-SW-02.example.com") == "f12-cctv-sw-02"
    assert mod._norm_sw_name("R4-Core-LAN-SW") == "r4-core-lan-sw"
    assert mod._norm_sw_name("") == ""


def test_broadcast_components():
    names = {"sw-a", "sw-b", "sw-c", "sw-d", "sw-e"}
    edges = [("sw-a", "sw-b"), ("sw-b", "sw-c"), ("sw-d", "sw-d"),
             ("sw-d", "ghost")]     # ghost edges ignored by caller semantics
    comps = {frozenset(c) for c in mod._broadcast_components(names, edges)}
    assert comps == {frozenset({"sw-a", "sw-b", "sw-c"}),
                     frozenset({"sw-d"}), frozenset({"sw-e"})}


def test_component_key_prefers_vtp_domain_casefolded():
    members = {"f_-1-cctv-sw", "f12-cctv-sw-02"}
    vtp = {"f_-1-cctv-sw": "", "f12-cctv-sw-02": "Snapp"}
    assert mod._component_key(members, vtp) == "snapp"


def test_component_key_first_hostname_when_no_vtp():
    members = {"f_-4-cctv-sw-n-01", "f12-cctv-sw-02", "f_-1-cctv-sw"}
    vtp = {"f_-4-cctv-sw-n-01": "", "f12-cctv-sw-02": "", "f_-1-cctv-sw": ""}
    assert mod._component_key(members, vtp) == "f12-cctv-sw-02"


VTP_STATUS = """VTP Version capable             : 1 to 3
VTP version running             : 3
VTP Domain Name                 : snapp
VTP Pruning Mode                : Disabled (Operationally Disabled)
VTP Traps Generation            : Disabled
Device ID                       : d009.c86a.fc80

Feature VLAN:
--------------
VTP Operating Mode                : Client
Number of existing VLANs          : 57
Maximum VLANs supported locally   : 1024

Feature MST:
--------------
VTP Operating Mode                : Transparent
"""


def test_parse_vtp_status_real_output():
    out = mod._parse_vtp_status(VTP_STATUS)
    assert out["domain"] == "snapp"
    assert out["mode"] == "client"   # Feature VLAN mode, not the MST one


def test_parse_vtp_status_empty_domain():
    out = mod._parse_vtp_status("VTP Domain Name                 : \n")
    assert out["domain"] is None
    assert out["mode"] is None


SHOW_MAC_TABLE = """SW1#show mac address-table
          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
   1    0009.0f09.0024    DYNAMIC     Gi1/0/5
  10    b40b.4412.abcd    DYNAMIC     Gi1/0/7
  10    b40b.4412.abcd    STATIC      CPU
Total Mac Addresses for this criterion: 3
"""


def test_parse_mac_table():
    rows = mod._parse_mac_table(SHOW_MAC_TABLE)
    assert rows == [
        {"vid": 1, "mac": "00:09:0f:09:00:24", "port": "Gi1/0/5"},
        {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/7"},
        {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "CPU"},
    ]


def test_parse_mac_table_empty():
    assert mod._parse_mac_table("SW1#show mac address-table\n") == []


def test_norm_mac():
    assert mod._norm_mac("B4:0B:44:12:AB:CD") == "b4:0b:44:12:ab:cd"
    assert mod._norm_mac("b40b.4412.abcd") == "b4:0b:44:12:ab:cd"
    assert mod._norm_mac("not-a-mac") is None
    assert mod._norm_mac(None) is None
    assert mod._norm_mac("") is None


def test_build_mac_map_skips_uplink_ports():
    collected = [
        ({"ip": "10.0.0.1", "hostname": "SW1"}, 7, {
            "neighbors": [{"device_id": "SW2", "platform": "", "ip": None,
                           "local_intf": "GigabitEthernet1/0/1",
                           "remote_intf": "Gi0/1"}],
            "ports": [{"port": "Gi1/0/1", "vlan": "1"}, {"port": "Gi1/0/5", "vlan": "10"}],
            "mac_table": [
                {"vid": 1, "mac": "00:09:0f:09:00:24", "port": "Gi1/0/1"},
                {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/5"},
            ],
        }),
    ]
    m = mod.build_mac_map(collected)
    # Gi1/0/1 is a CDP uplink (long name in neighbors) -> excluded
    assert m == {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/5", 10)}


def test_build_mac_map_first_switch_wins_on_duplicates():
    collected = [
        ({"ip": "10.0.0.1"}, 7, {"neighbors": [],
                                 "ports": [{"port": "Gi1/0/5", "vlan": "10"}],
                                 "mac_table": [
            {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/5"}]}),
        ({"ip": "10.0.0.2"}, 8, {"neighbors": [],
                                 "ports": [{"port": "Gi2/0/9", "vlan": "10"}],
                                 "mac_table": [
            {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi2/0/9"}]}),
    ]
    m = mod.build_mac_map(collected)
    assert m["b4:0b:44:12:ab:cd"] == ("10.0.0.1", "Gi1/0/5", 10)


def test_build_mac_map_handles_missing_keys():
    assert mod.build_mac_map([]) == {}
    collected = [({"ip": "10.0.0.1"}, 7, {})]   # no neighbors/mac_table keys
    assert mod.build_mac_map(collected) == {}


def test_build_mac_map_skips_port_channel_and_trunk():
    collected = [
        ({"ip": "10.0.0.1"}, 7, {
            "neighbors": [],
            "ports": [{"port": "Po1", "vlan": "trunk"},
                      {"port": "Gi1/0/5", "vlan": "10"}],
            "mac_table": [
                {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Po1"},
                {"vid": 10, "mac": "00:09:0f:09:00:24", "port": "Gi1/0/5"},
            ],
        }),
    ]
    m = mod.build_mac_map(collected)
    assert m == {"00:09:0f:09:00:24": ("10.0.0.1", "Gi1/0/5", 10)}
