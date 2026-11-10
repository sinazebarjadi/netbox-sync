# Ruckus ZoneDirector (ZD1200) family — design spec

**Date:** 2026-07-31
**Status:** Approved
**Scope:** Import Ruckus ZoneDirector wireless controllers, their access points, and WLANs (SSIDs) into NetBox — device, AP devices, and native Wireless LANs with VLAN linkage. Config-driven HA merging for controller pairs.

---

## 1. Verified facts (live probes, 2026-07-31)

- Transport: paramiko **interactive shell** required — ZD rejects `exec_command` ("Invalid argument"); login is two-step inside the shell: `Please login:` → user → `Password:` → pass → welcome; privileged commands need `enable` (no password) → `ruckus#`.
- `show sysinfo` gives: Name (`WLC-Controller_02`), IP Address (`172.31.2.201`), MAC, Model (`ZD1200`), Licensed APs, **Serial Number** (`352138000XXX`), **Version** (`10.5.1.0 build 276`), AP counts.
- `show ap all`: ID blocks with MAC Address, Model (`r550`), Device Name, Approved, Group Name, Network Setting → IP Address / Netmask / Gateway (36 APs live).
- `show wlan all`: ID blocks with NAME, SSID, Authentication (`open`), Encryption (`wpa2`), **VLAN-ID** (`109`) (15 WLANs live; passphrases visible in CLI — **never synced**).
- No device-side redundancy command exists on this firmware; HA topology comes from config: VIP `172.31.2.202`, primary `.201`, secondary `.200` (user-provided).
- NetBox is 4.x: native **Wireless LAN** objects exist (ssid, auth_type, vlan FK, group, description).

## 2. Design

**Collector (`netbox_sync/collectors/ruckus.py`)**
- `RuckusSession`: paramiko interactive shell; handles two-step login + `enable`; `run(cmd)` returns prompt-stripped output.
- Parsers (pure, real-fixture-tested): `_parse_sysinfo`, `_parse_ap_all`, `_parse_wlan_all`.
- `probe_ruckus(ip)` → sysinfo identity; `ruckus_collect(ip)` → `{summary, aps, wlans}`.

**Controller device** — role `Wireless Controller`, name/serial/firmware/MAC from sysinfo, `wlc_ip`/`wlc_enabled`/`wlc_model`/`wlc_firmware`/`wlc_serial`/`wlc_ap_count` custom fields. Primary IPv4 = reported sysinfo IP (fallback probed IP) via the mgmt interface.

**AP devices** — role `Access Point`; identity = **MAC** (APs have no serial; `wap_mac` custom field + MAC-based matching), name/model/IP (primary IPv4 on mgmt interface), `wap_group` (AP Group), `wap_wlc` (controller name), `wap_enabled`. Vanished APs (MAC absent from latest pull) are marked offline, never deleted.

**WLANs** — NetBox **Wireless LANs**: `ssid` (SSID field, NAME fallback), `auth_type` mapped (`open`→open; wpa2+passphrase→`wpa-personal`; 802.1x→`wpa-enterprise`; else open-with-fallback), `vlan` linked via the site's group machinery (unique vid match → link; none → create in per-device group keyed by controller hostname; multi → per-device group, no MAC disambiguation for wireless), grouped under a per-controller Wireless LAN Group (`ZD <hostname>`, marker description). **Passphrases/PSKs are never written.**

**HA merging (config-driven)** — `RUCKUS_HA_MAP=172.31.2.202:172.31.2.201,172.31.2.200` (vip:primary,secondary by position; multiple pairs separated by `;`). Cluster device matched by `wlc_vip` custom field or serial; **identity (name/serial/firmware) only updated from VIP or primary-role probes**; `wlc_vip`, `wlc_ha_role` (vip/primary/secondary/standalone), `wlc_ha_peer` fields; primary IPv4 = the VIP. Offline only when the VIP *and* all units of a pair are unreachable.

**Config** — `RUCKUS_USER/PASS`, `RUCKUS_PORT` (22), `RUCKUS_RANGES` (empty = disabled), `DEFAULT_RUCKUS_ROLE` (`Wireless Controller`), `DEFAULT_AP_ROLE` (`Access Point`), `RUCKUS_HA_MAP`. Creds validated only when ranges set.

## 3. Testing

Parsers with the captured real fixtures; HA-map parsing; probe→cluster-key resolution; AP ensure (MAC match/create); WLAN auth mapping + VLAN link; sweep logic with fakes.

## 4. Non-goals

- No client/station data, no rogue-device data, no radio/channel config, no AP-group objects in NetBox (kept as a custom field), no mesh data, no passphrase/PSK sync (ever), no ZD config management.
