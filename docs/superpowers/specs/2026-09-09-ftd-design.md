# Cisco FTD (Firepower Threat Defense) family — design spec

**Date:** 2026-09-09
**Status:** Approved (access verified live)
**Scope:** Import Cisco FTD devices as NetBox devices — identity, model, serial, firmware, mgmt IP, health — discovered **via the FMC** (Firepower Management Center) REST API. The FMC is only the management plane; the FTD devices are the inventory targets. Read-only.

---

## 1. Verified facts (live, 2026-09-09, FMC 192.168.16.143, readonly netbox account)

**Auth (token-based):** `POST /api/fmc_platform/v1/auth/generatetoken` with HTTP Basic auth → `204` + `X-auth-access-token` response header + `DOMAINS` header (JSON list of `{name, uuid}`). Subsequent calls use `X-auth-access-token: <token>`. Domain here: `Global` / `e276abec-e0f2-11e3-8169-6d9ed49b625f`.

**Device list:** `GET /api/fmc_config/v1/domain/{uuid}/devices/devicerecords` → 4 FTDs.

**Device detail:** `GET .../devices/devicerecords/{id}` →
- `model`: "Cisco Secure Firewall 3110 Threat Defense"
- `sw_version`: "7.6.4"
- `hostName`: the FTD's **management IP** (e.g. `172.31.250.200`)
- `metadata.deviceSerialNumber`: the **chassis serial** (e.g. `FJZ2909JHFH`)
- `healthStatus` (green), `ftdMode` (ROUTED), `isConnected` (true), `deviceGroup.name` (HQ/Afra)

The 4 FTDs (two HA pairs, but **each has its own distinct mgmt IP** — FTD HA does NOT share a mgmt IP like FortiWeb/FortiGate):

| Name | Mgmt IP | Serial | Group |
|------|---------|--------|-------|
| HQ-FTD-01 | 172.31.250.200 | FJZ2909JHFH | HQ |
| HQ-FTD-02 | 172.31.250.201 | FJZ2914026S | HQ |
| Afra-FTD-01 | 192.168.19.2 | FJZ2909EL9L | Afra |
| Afra-FTD-02 | 192.168.19.3 | FJZ29090AWW | Afra |

## 2. Design

**Collector (`netbox_sync/collectors/ftd.py`)**
- `FMCSession`: generates a token on init (Basic auth → `X-auth-access-token`), parses the `DOMAINS` header for the domain uuid, `get(path)` returns parsed JSON (handles `items` paging). Re-generates on 401.
- `probe_ftd(ip)` → probe the FMC itself: token generation succeeds + domain uuid → returns an FMC probe dict (the FMC is a device too? — NO, per user "the FMC itself is not important". So the FMC is NOT added as a device; it's only the API endpoint. `probe_ftd` returns a marker that this IP is a reachable FMC, used only to trigger FTD enumeration.)
- `ftd_collect(ip)` → enumerate all FTD device records + details → `{ftds: [{name, model, serial, mgmt_ip, sw_version, health, mode, group, connected}]}`.

**NetBox (`netbox.py`)**
- `ensure_ftd_device(ftd)`: match by chassis serial (`metadata.deviceSerialNumber`), else name+site+role. Role `FTD` (new). Custom fields `ftd_ip` (the FTD's mgmt IP), `ftd_enabled`, `ftd_model`, `ftd_firmware`, `ftd_health`, `ftd_mode`, `ftd_group`, `ftd_fmc` (the managing FMC IP). Manufacturer `Cisco`.
- **Each FTD is its own device with its own mgmt IP** (no shared-IP/VRF needed — FTD HA keeps separate mgmt IPs). Primary IPv4 = the FTD's `hostName` (mgmt IP) on the synthetic mgmt interface.
- `mark_ftd_offline` (status offline + `ftd_enabled=False`).
- Site resolution: `resolve_site(hostname, mgmt_ip)` — the mgmt IP drives SITE_IP_MAP (HQ FTDs → 172.31.x → HQ; Afra FTDs → 192.168.19.x → Afranet).

**Config (`config.py`)**
- `FMC_RANGES` (opt-in, empty default) — IPs of FMC appliances to query for FTDs. `FMC_USER`, `FMC_PASS`, `FMC_PORT` (default 443). Creds validated only when ranges set.

**Scanner / sync**
- New `ftds` family key. The scanner probes FMC IPs (token works); each reachable FMC's FTDs are enumerated in run_sync.
- `run_sync`: for each found FMC → `ftd_collect` → for each FTD → `ensure_ftd_device` + primary IP. Offline sweep via `cf_ftd_enabled` keyed on `ftd_ip` (the FTD's mgmt IP, not the FMC's).
- `report.py` label: `ftds: "Cisco FTDs"`.

**Custom fields** (registry): `ftd_ip`(text), `ftd_enabled`(bool), `ftd_model`(text), `ftd_firmware`(text), `ftd_health`(text), `ftd_mode`(text), `ftd_group`(text), `ftd_fmc`(text).

## 3. Testing

Parsers from live-captured fixtures (4 FTDs); token session (Basic→token→401 regen); ensure/match/offline against fakes; scanner gating.

## 4. Non-goals (v1)

- The FMC itself is NOT a NetBox device (per user).
- No FTD interfaces/ACLs/NAT/routing (policy objects) — device-level inventory only.
- No HA-pair merging/splitting logic — each FTD is independent (they have distinct mgmt IPs); `ftd_group` records the administrative grouping.
- No FMC HA (single FMC here).
