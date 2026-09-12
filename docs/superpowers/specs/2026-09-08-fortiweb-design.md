# FortiWeb (WAF) family — design spec

**Date:** 2026-09-08
**Status:** Approved (access verified live)
**Scope:** Import FortiWeb WAF appliances as NetBox devices — identity, model, firmware, HA/operation mode, mgmt IP — following the existing per-family pattern (probe → ensure → offline sweep). Read-only.

---

## 1. Verified facts (live, 2026-09-08, readonly `netbox` account on :58291)

Two appliances, both answering the FortiWeb REST API:

| IP | hostName | serialNumber | model | firmware | operationMode | haStatus | ifaces |
|----|----------|--------------|-------|----------|---------------|----------|--------|
| 192.168.60.252 | `FortiWeb-Azadegan` | FV-1KET122900020 | 1000E | 7.2.12 build0437 | Reverse Proxy | Standalone | 17 |
| 192.168.19.130 | *(unset)* | FV-1KFS124000458 | 1000F | 7.2.12 build0437 | Reverse Proxy | Active-Passive | 20 |

**Auth (per Fortinet KB, confirmed):** FortiWeb has **no dedicated API users**. Every request carries an `Authorization` header = **Base64 of `{"username","password","vdom":"root"}`**. No session, no cookie, no token endpoint. Works with the existing readonly account.

**Endpoints used:**
- `GET /api/v2.0/system/status.systemstatus` → identity: `hostName`, `serialNumber`, `firmwareVersion` (`"FortiWeb-1000E 7.2.12,build0437(GA),251023"`), `operationMode`, `haStatus`, `systemTime`, up_*, `readonly`.
- `GET /api/v2.0/system/status.systemresource` → cpu/mem/disk/session (optional health).
- `GET /api/v2.0/cmdb/system/interface` → interface list (physical ports + mgmt), `ip` as `a.b.c.d/len`, `allowaccess`. The configured mgmt1 IP is a factory default (`192.168.1.99`) on both — the *real* mgmt IP is the probed one.

## 2. Design

**Collector (`netbox_sync/collectors/fortiweb.py`)**
- `FortiWebSession`: builds the Base64 `Authorization` header once; `get(path)` returns the envelope's `results`; raises on non-2xx / `errcode`. No login/logout (stateless header auth).
- `_parse_system_status(results)` → `{name, serial, model, version, op_mode, ha_status}`; model parsed out of `firmwareVersion` (`FortiWeb-1000E 7.2.12,...` → model `FortiWeb 1000E`, version `7.2.12`).
- `_parse_interfaces(results)` → `{port_count, mgmt_ip}` (count physical ports; mgmt IP = the probed IP, since mgmt1 is a factory default).
- `probe_fortiweb(ip)` → standard probe dict (serial/model/hostname/firmware/manufacturer "Fortinet"); `fortiweb_collect(ip)` → `{summary, resources, interfaces}`.

**NetBox (`netbox.py`)**
- `ensure_fortiweb_device(probe, extra)`: match by serial → else name+site+role. Custom fields `fortiweb_ip`, `fortiweb_enabled`, `fortiweb_model`, `fortiweb_firmware`, `fortiweb_mode` (operation mode), `fortiweb_ha` (HA status), `fortiweb_port_count` (integer). Hostname fallback: `fortiweb-<ip-dashed>` when `hostName` is unset (box #2).
- `mark_fortiweb_offline` (status offline + `fortiweb_enabled=False`).
- Role: new `WAF` role (via `FORTIWEB_ROLE`, default `WAF`). Manufacturer `Fortinet`.
- Primary IPv4 = the probed mgmt IP on the synthetic `mgmt` interface (existing `ensure_primary_ip`).

**Config (`config.py`)**
- `FORTIWEB_RANGES` (opt-in, empty default), `FORTIWEB_USER`, `FORTIWEB_PASS`, `FORTIWEB_PORT` (default **58291** — matches this deployment's custom port), `FORTIWEB_ROLE` (default `WAF`). Creds validated only when ranges set.

**Scanner / sync**
- New `fortiwebs` family key; probe pool after FortiGate; exclude IPs already claimed by other families (a FortiWeb won't answer the FortiGate `/api/v2` shape, but skip-claimed avoids double-probing).
- `run_sync`: process each found FortiWeb → ensure device → refresh CFs → primary IP → offline sweep via `cf_fortiweb_enabled`.
- `report.py` label: `fortiwebs: "FortiWeb WAFs"`.

**Custom fields** — added to the `CUSTOM_FIELDS` registry (auto-created at sync start): `fortiweb_ip`(text), `fortiweb_enabled`(bool), `fortiweb_model`(text), `fortiweb_firmware`(text), `fortiweb_mode`(text), `fortiweb_ha`(text), `fortiweb_port_count`(integer).

## 3. Testing

Parsers from the live-captured fixtures (both boxes); session header construction (base64 payload shape); ensure/match/offline against the in-memory fakes; scanner family gating.

## 4. Non-goals (v1)

- No server pools / virtual servers / protected-hostnames (the WAF policy objects) — device-level inventory only.
- No HA peer *merging* (box #2 is Active-Passive; its peer is probed separately if it's in `FORTIWEB_RANGES`). Serial-first matching already prevents duplicates.
- No interface/VLAN/cable sync (FortiWeb ports are mostly 0.0.0.0 physicals; not useful topology here).
- No resource/graph history (cpu/mem read but not persisted).
