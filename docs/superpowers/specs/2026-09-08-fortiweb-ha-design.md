# FortiWeb HA — two-device model (shared mgmt IP, distinct serials)

**Date:** 2026-09-08
**Status:** Approved (user chose "Two devices, shared IP")
**Scope:** FortiWeb HA pairs (Active-Passive) appear as **two NetBox devices** — one per physical node — sharing the same management IP, each with its own serial. Same model to be applied to other HA families (FortiGate) as a follow-up.

---

## 1. Verified facts (live, 2026-09-08, Afranet 192.168.19.130)

`GET /api/v2.0/system/status.systemstatus` on an HA node returns a `cluster_members` list:

```json
"cluster": "WAF-HA",
"haStatus": "Active-Passive",
"cluster_members": [
  {"hostname": "FortiWeb-Afranet", "dev_sn": "FV-1KFS124000458", "role": "Primary"},
  {"hostname": "FortiWeb",         "dev_sn": "FV-1KFS124000505", "role": "Secondary"}
]
```

`GET /api/v2.0/cmdb/system/ha` → `group-name: "WAF-HA"`, `mode: active-passive`, **`ha-mgmt-status: disable`** → both nodes share the probed mgmt IP (no per-node mgmt IP). This matches the requirement: same MGMT IP, different serials.

## 2. Design

**Collector (`fortiweb.py`)**
- `_parse_cluster_members(results)` → `[{hostname, serial, role}]` from `cluster_members` (empty list when Standalone / no cluster). Role normalized: Primary/Secondary.
- `fortiweb_collect` adds `ha_members` + `ha_group` (from `cluster` / cmdb `system/ha` `group-name`) to the result.

**NetBox (`netbox.py`)**
- `ensure_fortiweb_device(probe, extra)` — when `extra["ha_members"]` has >1 member:
  - **Primary node** (role == Primary, or the probed serial): the main device. Name = its hostname (or `fortiweb-<ip>` fallback), serial = its serial, `fortiweb_ha_role=primary`, `fortiweb_ha_group`, `fortiweb_ha_peer` = the peer's `hostname (serial)`.
  - **Passive node(s)** (role != Primary): a separate device per node. Name = its hostname if set, else `<primary-hostname>-HA<n>`; serial = its serial; `fortiweb_ha_role=secondary`, same `fortiweb_ip` (shared mgmt IP), `fortiweb_ha_group`, `fortiweb_ha_peer` = primary.
  - Each node matched by its own serial (role-agnostic, per the AE-merge fix) → no duplicates.
- **Shared mgmt IP:** a new `ensure_shared_primary_ip(dev_id, ip, hostname)` that — unlike `ensure_primary_ip` — *deliberately* assigns the same IPAM address to this device's mgmt interface even when it's already on the peer (HA). NetBox permits one IP on multiple interfaces; each node sets it as its own primary_ip4.
  - The existing `ensure_primary_ip` keeps its protective "don't steal another device's IP" behavior for non-HA devices; only HA nodes use the shared variant.

**Custom fields** (registry): add `fortiweb_ha_role` (text: primary/secondary), `fortiweb_ha_group` (text), `fortiweb_ha_peer` (text). (`fortiweb_ha` already holds the raw haStatus string.)

**Offline sweep** — unchanged: `cf_fortiweb_enabled` on each node; a node missing from the scan offlines independently. (Both nodes share the mgmt IP, so a dead passive node still *responds* via the shared IP — acceptable for v1; the serial-level identity is what matters for inventory.)

## 3. Testing

- `_parse_cluster_members` from the live fixture (Primary + Secondary) and the Standalone case (empty).
- `ensure_fortiweb_device` HA path: creates two devices, distinct serials, shared `fortiweb_ip`, correct roles/peers; idempotent re-run (no dupes).
- `ensure_shared_primary_ip`: assigns an IP already on another device without the "left unchanged" refusal.

## 4. Non-goals / follow-ups

- **FortiGate HA alignment** (currently merges a pair into one cluster device) — separate change; will follow the same two-device model if you want it there too.
- No per-node mgmt IP (none exists — `ha-mgmt-status: disable`).
- No failover-time IP moves; the shared IP is static on both nodes.
