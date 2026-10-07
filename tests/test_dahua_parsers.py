"""Tests for the Dahua CGI API parsers (key=value tables)."""
import netbox_sync.collectors.dahua as mod


SYSTEM_INFO = """serialNumber=4C0048DPAJ8F3C9
deviceType=31
processor=ST7108
updateSerial=NVR6XX-4KS2
"""

REMOTE_DEVICES = """table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Name=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=192.168.252.25
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Mac=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.SerialNo=DS-2CD1143G0-I20211208AAWRJ21084244
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType=DS-2CD1143G0-I
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Vendor=Onvif
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Version=V5.7.1 build 211102
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Address=192.168.252.26
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Mac=ff:ff:ff:ff:ff:ff
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.SerialNo=DS-2CD1143G0-I20211208AAWRJ21084253
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.DeviceType=DS-2CD1143G0-I
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Address=192.168.252.28
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Mac=b4:0b:44:12:ab:cd
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.SerialNo=IPC-UNKNOWN-123
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.DeviceType=IPCamera
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_3.Enable=false
"""

CHANNEL_TITLES = """table.ChannelTitle[0].Name=GF-Entrance
table.ChannelTitle[1].Name=GF-Corridor
table.ChannelTitle[2].Name=
"""


def test_parse_system_info():
    out = mod._parse_system_info(SYSTEM_INFO)
    assert out["serial"] == "4C0048DPAJ8F3C9"
    assert out["model"] == "NVR6XX-4KS2"
    assert out["device_type"] == "31"


def test_parse_software_version():
    out = mod._parse_software_version("version=4.002.0000000.2.R,build:2023-02-20\n")
    assert out == "4.002.0000000.2.R"


def test_parse_machine_name_generic_returns_none():
    assert mod._parse_machine_name("name=NVR\n") is None
    assert mod._parse_machine_name("name=Branch-NVR-01\n") == "Branch-NVR-01"


def test_parse_channel_titles_are_one_based():
    titles = mod._parse_channel_titles(CHANNEL_TITLES)
    assert titles[1] == "GF-Entrance"
    assert titles[2] == "GF-Corridor"
    assert titles[3] == ""


def test_parse_remote_devices():
    cams = mod._parse_remote_devices(REMOTE_DEVICES)
    # slot 3 has no Address -> skipped
    assert [c["channel"] for c in cams] == [1, 2, 3]
    c1, c2, c3 = cams
    assert c1["ip"] == "192.168.252.25"
    # serial is normalized: extract actual hardware serial after AWR marker
    assert c1["serial"] == "J21084244"
    assert c1["model"] == "DS-2CD1143G0-I"
    assert c1["firmware"] == "V5.7.1 build 211102"
    assert c1["online"] is True
    assert c1["mac"] is None                    # empty MAC dropped
    assert c1["manufacturer"] == "Hikvision"    # DS-2* model prefix
    assert c2["mac"] is None                    # ff:ff:.. placeholder dropped
    assert c2["online"] is True
    assert c3["online"] is False
    assert c3["mac"] == "b4:0b:44:12:ab:cd"     # real MAC normalized
    assert c3["manufacturer"] == "Dahua"        # unknown model prefix


def test_norm_mac():
    assert mod._norm_mac("ff:ff:ff:ff:ff:ff") is None
    assert mod._norm_mac("FF:FF:FF:FF:FF:FF") is None
    assert mod._norm_mac("") is None
    assert mod._norm_mac(None) is None
    assert mod._norm_mac("B4:0B:44:12:AB:CD") == "b4:0b:44:12:ab:cd"
