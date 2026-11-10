# VTP-based broadcast domains (VLAN groups) — design spec

**Date:** 2026-07-29
**Status:** Approved
**Scope:** Replace per-site VLAN objects with per-broadcast-domain **VLAN groups** so overlapping VLAN IDs at one site are never merged. Domains are identified via VTP; groups are named BD1, BD2… per site.

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Domain identification | `show vtp status` → VTP domain name (verified live: `example` on the user's C9200L) |
| Fallback when VTP domain empty | Per-switch group keyed by hostname (unknowns never merge) |
| Group naming | **Always BD1, BD2…** per site, stable across runs via marker-description key lookup |
| Identity key | Group description `netbox-sync: vtp=<key>` |
| NetBox modeling | VLANs get `group=<id>` and **no site field** — uniqueness per `(group, vid)`, avoiding the `(site, vid)` collision |
| Migration | Self-healing: legacy site-scoped marked VLANs swept at processed sites |

## 2. NetBox mechanics

NetBox 3.x VLAN uniqueness is `(site, vid)` **and** `(group, vid)`. Two VLANs
with the same vid at the same site can only coexist if they are in different
groups AND have `site=null`. VLAN groups are scoped (`scope_type="dcim.site"`,
`scope_id=<site>`), so the model becomes: group = broadcast domain at site X,
VLANs inside it carry no site.

## 3. Components

**`_parse_vtp_status(text)`** → `{"domain": str|None, "mode": str|None}`.
Domain from the header block (`VTP Domain Name : X`, empty → None); mode only
from the `Feature VLAN:` section (later MST/UNKNOWN sections have their own
mode lines — verified against real IOS-XE 17 output).

**`ensure_vlan_group(site_id, key)`** → group id.
- Scans marked groups at the site scope (`description` starts with
  `netbox-sync: vtp=`); exact key match → reuse.
- Otherwise creates `BD<n>` (n = max BD number among marked groups + 1),
  slug `bd<n>`, description `netbox-sync: vtp=<key>`, scope `dcim.site`.

**`sync_cisco_vlans(group_id, hostname, vlans)`** (reworked from site-scoped)
→ `{vid: id}`. Lookup/create per `(vid, group_id)`; VLANs created with
`group`, no `site`. Marker rules unchanged (manual VLANs untouched but
reused for linkage).

**`sweep_stale_vlans(group_id, seen)`** — marked VLANs in the group not seen
this run → delete.

**`sweep_legacy_site_vlans(site_id)`** — migration: deletes marker-owned
group-less VLANs at the site (the previous per-site implementation's
records). Only runs for sites with processed switches.

**Collector** gains `"vtp"` in the return dict (`show vtp status`, WARN on
failure → `{"domain": None, "mode": None}`).

**`run_sync`** — per switch: `key = vtp.domain or hostname or ip` →
`ensure_vlan_group(site_id, key)` → group-scoped sync → track
`group_vlan_seen[group_id]` and `legacy_sites`. After the Cisco loop: group
sweeps, then legacy sweeps.

## 4. Error handling & tests

- Group/VLAN failures WARN-and-continue per switch; sweep errors ERROR-logged.
- Fixture for `_parse_vtp_status` is the user's real switch output.
- Fake-harness tests: group reuse/next-BD-name/scope fields; group-scoped
  VLAN create/update/manual-reuse; group sweep; legacy sweep
  (deletes group-less marked, keeps grouped + unmarked).

## 5. Non-goals (YAGNI)

- No stale empty group deletion (harmless empty BD groups may linger).
- No VTP-pruning/VLAN-pruning logic, no MST/L2VPN instances, no non-Cisco
  broadcast domains.
