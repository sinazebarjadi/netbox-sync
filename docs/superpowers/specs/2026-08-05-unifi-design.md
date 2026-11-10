# UniFi (UniFi OS console) family — design spec

**Date:** 2026-08-05
**Status:** Approved (full scope: console + all 28 sites + WLANs)
**Scope:** Import UniFi OS console devices, all access points across all sites, and WLANs from every site into NetBox — mirroring the Ruckus pattern with shared AP/Wireless-LAN machinery.

---

## 1. Verified facts (live, 2026-08-05, console 172.31.5.67:8443)

- Console: **UniFi OS 10.2.105**, multi-site: **28 sites** (`/api/self/sites`), **118 APs** (all `U7PG2`) across 25 sites.
- **Auth that works:** legacy `POST /api/login {username, password}` → `unifises` session cookie (200 rc=ok). The newer `/api/auth/login` returns 401 on this console; `/api/v1/auth/login` is login-required too but the legacy flow is what works here. Internet research (Art of WiFi's UniFi API guide) confirms: the internal Network Application API with a **dedicated local admin account** is the de-facto standard for automation (cloud UI.com accounts break on MFA).
- **Devices** (`/api/s/<site>/stat/device`): `mac`, `name`, `model` (`U7PG2`), `serial` (MAC without colons), `ip`, `version` (firmware), `state`, `adopted`, `site_id`, `uptime`, `hw_caps`, `board_rev`.
- **WLANs** (`/api/s/<site>/rest/wlanconf`): `name`, `security`, `wpa_mode`, `wpa_enc`, `pmf_mode`, `enabled`, `hide_ssid`, `is_guest`, `networkconf_id`, `site_id`, `wlan_band(s)`, `minrate`, `dtim`.
- **Networks** (`/api/s/<site>/rest/networkconf`): LAN/VLAN configs with `vlan` ids (for WLAN→VLAN linkage).
- **Sites** (`/api/self/sites` and `/api/stat/sites`): `name`, `desc` (human names: ExamplePay, Site-West, Site-Uni, HQ-Server Room, HQ-F6, HQ-General, Cities-* …).

## 2. Design

**Collector (`netbox_sync/collectors/unifi.py`)**
- `UniFiSession`: legacy `/api/login` (unifises cookie), `get(path)` with JSON envelope (`meta.rc` checked, `data` returned).
- Parsers: `_parse_devices`, `_parse_sites`, `_parse_wlans`, `_parse_networks`.
- `probe_unifi(ip)` → status (`/status`: server_version, uuid) + login check; `unifi_collect(ip)` → `{console, sites, aps_by_site, wlans_by_site, networks_by_site}`.

**Console device** — role `Wireless Controller` (shared with Ruckus ZD); custom fields `unifi_ip`, `unifi_enabled`, `unifi_version` (server_version), `unifi_ap_count`, `unifi_sites`; offline via `unifi_enabled`. Name from `/api/system` (authed) or host IP fallback.

**AP devices** — **reuse the existing Ruckus AP machinery exactly**: `ensure_ap_device` (MAC identity via `wap_mac`), fields `wap_group` = UniFi site desc, `wap_wlc` = console name, `wap_enabled`. Model (`U7PG2`), serial (MAC-derived), primary IPv4 per AP. Vanished APs marked offline, never deleted.

**WLANs** — **reuse the Ruckus Wireless LAN machinery**: `sync_wireless_lans` + `sweep_wireless_lans` (marker `netbox-sync: <console name>`). Auth mapping: `security`/`wpa_mode` → open / wpa-personal / wpa-enterprise (guest WLANs noted in description). VLAN linkage: `networkconf_id` → `/api/s/<site>/rest/networkconf` `vlan` id → resolved against the site's groups (unique match → link; else create in a per-site UniFi group? No — reuse existing resolution: unique match → link; otherwise per-device group keyed by console+site name).

**Site mapping** — AP NetBox site = the **standard `resolve_site` resolution (SITE_IP_MAP longest-prefix on the AP's IP, then keyword, then default)** — identical to every other family. The UniFi site `desc` is kept only in `wap_group`. WLAN→VLAN resolution sites are derived per UniFi site from the **majority of its APs' resolved sites** (a UniFi site with no APs gets no VLAN bindings). ~~Exact name match on desc, else `resolve_site` fallback~~ (superseded 2026-08-05: desc-based sites were wrong — most APs belong to HQ/NXP per SITE_IP_MAP).

**Config** — `UNIFI_RANGES` (console IPs, empty = disabled), `UNIFI_USER/PASS`, `UNIFI_PORT` (8443); creds validated only when ranges set.

## 3. Testing

Parsers with live-captured fixtures (device/site/wlan/network records); auth login-flow with a fake session; AP adaptation to `ensure_ap_device` shape; auth mapping; per-site VLAN resolution reuse; sweep reuse.

## 4. Non-goals

- No clients/stations, no radio/channel stats, no DPI/health data, no switch/gateway (USG/USW) devices in v1 (type `uap` only), no API-key (official API) support in v1, no config management, no passphrases (PSK never synced).
