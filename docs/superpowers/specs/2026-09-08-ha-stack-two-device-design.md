# HA / stack two-device model — FortiGate HA + Cisco stacks

**Date:** 2026-09-08
**Status:** Approved (extends the FortiWeb two-device model to FortiGate HA and Cisco stacks)
**Scope:** An HA pair / switch stack appears as **one device per physical unit** in NetBox — each with its own serial, all sharing the same management IP. Replaces the FortiGate "merge into one cluster device" behavior; adds stack-member modeling to Cisco.

---

## 1. Verified facts (live, 2026-09-08)

**FortiGate HA** (172.31.5.1, cluster `Z-Cluster-FW`, mode a-p): `_fg_ha` already returns
`units = [{hostname, serial, is_primary}]` → `HQ` (FG180FTK21901250, primary) + `HQ-Secondary` (FG180FTK22900291). Shared mgmt IP = the probed one.

**Cisco stacks** — 6 two-member stacks found (172.31.1.3/.5/.6/.194/.254, 172.31.20.254), all Active+Standby:
- `show switch` → per-member `Switch#`, `Role` (Active/Standby), `Mac Address`, `State`.
- `show inventory` → a `Switch N` entry per member with that member's chassis `SN` (e.g. Switch 1=FOC2148U0U9, Switch 2=FCW2148F0RA). The `... Stack` chassis entry shares the Active's serial.
- The stack shares one mgmt IP (the probed IP).

**Shared-IP mechanism (proven on FortiWeb):** NetBox forbids one address on two devices and rejects global-table duplicates, so a non-primary unit's copy of the mgmt IP goes in the **`HA` VRF** with **role `vrrp`** (`ensure_shared_primary_ip`). Each unit sets it as primary.

## 2. Design

### FortiGate HA → two devices
- `ensure_fortigate_device(probe, ha)` — when `ha["clustered"]` and ≥2 units with serials:
  - **Primary unit**: main device, name = primary hostname, serial = primary serial, `fortigate_ha_role=primary`, `fortigate_ha_group`, `fortigate_ha_peer` = secondary. Owns the mgmt IP (normal `ensure_primary_ip`).
  - **Secondary unit(s)**: separate device each, name = its hostname (FortiGate units have hostnames: `HQ-Secondary`), serial = its serial, `fortigate_ha_role=secondary`, same `fortigate_ip`, peer = primary. Shared IP via `ensure_shared_primary_ip`.
  - Each matched by its own serial, role-agnostic (adopts AE-created records, no dupes).
  - **Replaces** the current merge-into-one-cluster behavior. The existing single cluster device (if present) is adopted as the primary (serial match) — no orphan.
- Interfaces/VLANs/IPAM/NAT/cables stay attached to the **primary** device only (the cluster's config lives there); the secondary is an inventory/identity device.

### Cisco stacks → member devices
- Collector: `_parse_show_switch(text)` → `[{member, role, mac, state}]`; map `Switch N` serials from `_parse_show_inventory`. `cisco_collect_inventory` adds `stack = [{member, role, mac, serial}]`.
- `ensure_cisco_device(probe, stack)` — when ≥2 members with serials:
  - **Active member**: main device (keeps the switch's existing identity/serial), `cisco_stack_role=active`, `cisco_stack_members` count, peer list. Owns mgmt IP, interfaces, VLANs, cables, CDP.
  - **Standby member(s)**: separate device, name = `<switch-name>-M<n>`, serial = member serial, `cisco_stack_role=standby`, same `cisco_ip`, shared IP via `ensure_shared_primary_ip`.
  - Matched by serial (role-agnostic). Single-member stacks → unchanged (one device).
- Stack member interfaces/PSUs/modules already in inventory stay on the active device.

### Custom fields (registry)
- FortiGate: reuse existing `fortigate_ha_group/_mode/_peer`; add `fortigate_ha_role` already exists. No new CFs needed.
- Cisco: add `cisco_stack_role` (text: active/standalone), `cisco_stack_members` (integer), `cisco_stack_peer` (text).

## 3. Testing
- `_fg_ha` units → two devices, shared IP, roles/peers; standalone unchanged; idempotent.
- `_parse_show_switch` from live fixture; stack serial mapping; two-member ensure → two devices shared IP; single-member → one device.

## 4. Non-goals / notes
- No per-unit config sync (units share the cluster/stack config; only identity + serial + role per unit).
- No stack-cable/StackPort topology modeling (StackPort serials are cables, not chassis).
- FortiGate secondary is identity-only (no interfaces/VLANs/NAT on it).
