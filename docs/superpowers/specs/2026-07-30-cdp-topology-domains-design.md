# CDP-topology broadcast domains — design spec

**Date:** 2026-07-30
**Status:** Approved
**Scope:** Replace VTP/hostname group keys with CDP-topology connected components: switches that see each other via CDP share a broadcast-domain group; plus group-key casefolding and stale-group migration.

---

## 1. Background (live evidence)

- `BD1 (vtp=example)` and `BD2 (vtp=Example)` are the **same** domain split by case.
- `BD4/BD5/BD6` hold three duplicates of `CCTV-MGMT` (vid 20) — per-switch fallback groups (empty VTP domain) for switches the user confirms are one shared L2 island.

## 2. Design

**Two-pass Cisco processing** in `run_sync`:
1. Pass 1: ensure device + `cisco_collect_inventory` for every switch; store results. Build the CDP graph: nodes = switches (hostname normalized: strip domain suffix, casefold), edges = CDP adjacency **restricted to same-site pairs**; non-switch neighbors ignored.
2. Connected components (union-find) = broadcast domains.
3. Pass 2: per-switch processing (unchanged except group key now comes from the component).

**Component group key** (`_component_key`): first non-empty VTP domain in hostname-sorted member order, **casefolded**; else first sorted hostname. Isolated switch → VTP domain if set, else hostname (unchanged).

**Migration (`_sweep_stale_groups`)** — a marker-owned group at a processed site is stale when:
- its key case-folds to a key of a group fed this run but it isn't that group (case-variant duplicate, e.g. `Example`), or
- its key is a processed switch's hostname that now resolves to a different (component) key (abandoned per-switch fallback).

Stale groups: all their marked VLANs deleted, then the group itself deleted when empty. Manual groups never touched.

**FortiGate:** unchanged — `_site_vlan_index` sees the deduplicated groups.

## 3. Components

- `_norm_sw_name(name)` → strip domain suffix, casefold
- `_broadcast_components(names, edges)` → union-find components
- `_component_key(members, vtp_by_name)` → casefolded VTP-domain or first hostname
- `_sweep_stale_groups(site_id, fed_group_ids, key_by_name)` → migration sweep
- run_sync Cisco block restructured into two passes (collect+ensure → topology → process)

## 4. Testing

- Union-find components: two islands + singleton; same-site edge restriction honored by caller
- Component key: VTP-preferred with casefolding, hostname fallback
- Stale-group sweep: case-variant dup, abandoned fallback, fed kept, manual kept, empty-group deletion

## 5. Non-goals

- No cross-site edges (domains never span sites).
- No LLDP edges (CDP only — Cisco neighbors are CDP-primary already; LLDP fallback neighbors share the same parser output shape and may be included later).
- No historical topology persistence (computed fresh per run).
