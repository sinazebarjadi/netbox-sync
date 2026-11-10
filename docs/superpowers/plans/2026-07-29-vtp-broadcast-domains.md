# VTP Broadcast Domains Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move VLAN sync from per-site VLANs to per-broadcast-domain VLAN groups (BD1, BD2…) identified via VTP, so overlapping VLAN IDs at one site never merge.

**Architecture:** `_parse_vtp_status` feeds a per-switch domain key; `ensure_vlan_group` resolves/creates marker-owned site-scoped groups; `sync_cisco_vlans` and the sweeps become group-scoped; legacy site VLANs self-clean.

**Tech Stack:** Python 3.9+, netmiko, pynetbox, pytest fake harness.

**Spec:** `docs/superpowers/specs/2026-07-29-vtp-broadcast-domains-design.md`

## Global Constraints

- Group key = VTP domain when present, else hostname, else IP.
- Group description is the identity key: `netbox-sync: vtp=<key>`; names are BD<n> (max+1 among marked groups at the site).
- VLANs carry `group`, never `site`; lookup per `(vid, group_id)`.
- Manual (unmarked) VLANs/groups never modified or deleted.
- Legacy sweep only runs for sites with processed switches this run.
- Tests: `.\.venv\Scripts\python.exe -m pytest tests\ -q` (93 passing at plan time).

---

### Task 1: `_parse_vtp_status` + collection

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_cisco_parsers.py`

**Interfaces:**
- Produces: `_parse_vtp_status(text) -> {"domain": str|None, "mode": str|None}`; `cisco_collect_inventory` return gains `"vtp"` key (same shape; `{"domain": None, "mode": None}` on failure).

- [ ] **Step 1: Write the failing test** (append to `tests/test_cisco_parsers.py`; fixture is real switch output)

```python
VTP_STATUS = """VTP Version capable             : 1 to 3
VTP version running             : 3
VTP Domain Name                 : example
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
    assert out["domain"] == "example"
    assert out["mode"] == "client"   # Feature VLAN mode, not the MST one


def test_parse_vtp_status_empty_domain():
    out = mod._parse_vtp_status("VTP Domain Name                 : \n")
    assert out["domain"] is None
    assert out["mode"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cisco_parsers.py -q`
Expected: 2 FAIL — `AttributeError: ... no attribute '_parse_vtp_status'`.

- [ ] **Step 3: Implement** (append to the parser section of `netbox_sync/collectors/cisco.py`)

```python
def _parse_vtp_status(text):
    """Parse `show vtp status`: domain name from the header block, operating
    mode only from the 'Feature VLAN:' section (later feature sections like
    MST have their own mode lines)."""
    out = {"domain": None, "mode": None}
    in_feature_vlan = False
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^VTP Domain Name\s*:\s*(.*)$', s, re.IGNORECASE)
        if m:
            d = m.group(1).strip()
            out["domain"] = d or None
            continue
        if re.match(r'^Feature VLAN\s*:', s, re.IGNORECASE):
            in_feature_vlan = True
            continue
        if re.match(r'^Feature \w+\s*:', s, re.IGNORECASE):
            in_feature_vlan = False
            continue
        m = re.match(r'^VTP Operating Mode\s*:\s*(\S+)', s, re.IGNORECASE)
        if m and in_feature_vlan and not out["mode"]:
            out["mode"] = m.group(1).lower()
    return out
```

- [ ] **Step 4: Collect it** (in `cisco_collect_inventory`, after the trunks block)

```python
        try:
            vtp = _parse_vtp_status(sess.run("show vtp status"))
            log("INFO", f"  vtp domain: {vtp.get('domain')}")
        except Exception as exc:
            vtp = {"domain": None, "mode": None}
            log("WARN", f"  show vtp status failed: {exc}")
```

Extend the return dict with `"vtp": vtp`.

- [ ] **Step 5: Run tests + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q` → 95 pass.

```bash
git add netbox_sync/collectors/cisco.py tests/test_cisco_parsers.py
git commit -m "Parse show vtp status and collect it per switch"
```

---

### Task 2: Group resolution + group-scoped VLAN sync + sweeps

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `VLAN_GROUP_MARKER = "netbox-sync: vtp="`; `ensure_vlan_group(site_id: int, key: str) -> int`; `sync_cisco_vlans(group_id: int, hostname: str, vlans: list[dict]) -> dict[int, int]` (signature CHANGES from site_id to group_id); `sweep_stale_vlans(group_id: int, seen_vids: set[int])`; `sweep_legacy_site_vlans(site_id: int)`.

- [ ] **Step 1: Update `_vlan_api` helper and existing VLAN tests** (in `tests/test_netbox_sync.py`)

Replace `_vlan_api` with:

```python
def _vlan_api(vlan_items, group_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(vlans=FakeEndpoint(vlan_items),
                             vlan_groups=FakeEndpoint(group_items or [])))
```

Update `test_sync_cisco_vlans_create_update_and_manual_reuse`: records get
`group_id=8` instead of `site_id=3`, and the call becomes
`cisco.sync_cisco_vlans(8, "SW1", [...])`; the create assertion checks
`["group"] == 8` instead of `["site"] == 3`.

Update `test_sweep_stale_vlans`: records get `group_id=8` instead of
`site_id=3`; call becomes `cisco.sweep_stale_vlans(8, {10, 40})`.

- [ ] **Step 2: Add the new failing tests**

```python
def test_ensure_vlan_group_reuses_by_key_and_names_next_bd(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g1 = FakeRecord(60, name="BD1", description="netbox-sync: vtp=example",
                    scope_type="dcim.site", scope_id=3)
    g2 = FakeRecord(61, name="BD3", description="netbox-sync: vtp=other",
                    scope_type="dcim.site", scope_id=3)
    manual = FakeRecord(62, name="BD2", description="manual group",
                        scope_type="dcim.site", scope_id=3)
    api = _vlan_api([], [g1, g2, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # existing key -> reused, nothing created
    assert cisco.ensure_vlan_group(3, "example") == 60
    assert api.ipam.vlan_groups.created == []

    # new key -> created with next FREE BD number among marked groups (BD3+1)
    gid = cisco.ensure_vlan_group(3, "campus-b")
    created = api.ipam.vlan_groups.created[0]
    assert created["name"] == "BD4"
    assert created["slug"] == "bd4"
    assert created["description"] == "netbox-sync: vtp=campus-b"
    assert created["scope_type"] == "dcim.site"
    assert created["scope_id"] == 3
    assert gid is not None


def test_sweep_legacy_site_vlans(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    legacy = FakeRecord(50, vid=10, site_id=3, group=None,
                        description="netbox-sync: last seen SW1")
    grouped = FakeRecord(51, vid=10, site_id=None, group=8,
                         description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=20, site_id=3, group=None,
                        description="manual vlan")
    api = _vlan_api([legacy, grouped, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_legacy_site_vlans(3)

    assert api.ipam.vlans.deleted_ids == [50]   # only the group-less marked one
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k "vlan_group or legacy" -q`
Expected: FAIL — `AttributeError: ... no attribute 'ensure_vlan_group'`.

- [ ] **Step 4: Implement** (in `netbox_sync/collectors/cisco.py`)

Append before `sync_cisco_vlans`:

```python
# Group identity lives in the description ("netbox-sync: vtp=<key>") so BD
# numbering stays stable across runs; the display name is just BD1, BD2...
VLAN_GROUP_MARKER = "netbox-sync: vtp="

def ensure_vlan_group(site_id, key):
    """Find or create the marker-owned VLAN group for (site, key).
    New groups are named BD<n> = max BD number among marked groups + 1."""
    api = netbox.get_netbox()
    want_desc = f"{VLAN_GROUP_MARKER}{key}"
    max_bd = 0
    for g in api.ipam.vlan_groups.filter(scope_type="dcim.site", scope_id=site_id):
        desc = g.description or ""
        if not desc.startswith(VLAN_GROUP_MARKER):
            continue
        if desc == want_desc:
            return g.id
        m = re.match(r'^BD(\d+)$', g.name or "")
        if m:
            max_bd = max(max_bd, int(m.group(1)))
    n = max_bd + 1
    return api.ipam.vlan_groups.create({
        "name": f"BD{n}", "slug": f"bd{n}", "description": want_desc,
        "scope_type": "dcim.site", "scope_id": site_id}).id
```

Rework `sync_cisco_vlans` — change the signature to
`def sync_cisco_vlans(group_id, hostname, vlans):`, the lookup to
`api.ipam.vlans.get(vid=vid, group_id=group_id)`, and the create payload to
`{**payload, "group": group_id}`. Everything else unchanged.

Rework `sweep_stale_vlans` — change the signature to
`def sweep_stale_vlans(group_id, seen_vids):` and the filter to
`api.ipam.vlans.filter(group_id=group_id)`; adjust log wording to
`group {group_id}`.

Append the legacy sweep:

```python
def sweep_legacy_site_vlans(site_id):
    """Migration cleanup: delete marker-owned SITE-scoped (group-less)
    VLANs — superseded by VLAN groups. Only called for sites with
    processed switches this run."""
    api = netbox.get_netbox()
    for vlan in list(api.ipam.vlans.filter(site_id=site_id)):
        if not (vlan.description or "").startswith(VLAN_MARKER):
            continue
        if getattr(vlan, "group", None):
            continue   # group-scoped VLANs are handled by the group sweep
        try:
            vlan.delete()
            log("INFO", f"  legacy site vlan {vlan.vid} (site {site_id}) deleted — moved to VLAN group")
        except Exception as exc:
            log("WARN", f"  could not delete legacy vlan {vlan.vid}: {exc}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (97).

- [ ] **Step 6: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add VLAN groups (BD naming) and group-scoped VLAN sync + sweeps"
```

---

### Task 3: Wire + docs + release

**Files:**
- Modify: `netbox_sync/sync.py`
- Modify: `README.md`

- [ ] **Step 1: Wire `run_sync`** (Cisco block)

Extend the cisco import with `ensure_vlan_group, sweep_legacy_site_vlans`.
Replace `site_vlan_seen = {}` with:

```python
    group_vlan_seen = {}
    legacy_sites = set()
```

Unpack `vtp = data["vtp"]` next to `vlans`/`trunks`, and replace the
`vid_map` block with:

```python
        vid_map = {}
        if site_id:
            try:
                key = (vtp.get("domain") or probe.get("hostname") or ip)
                group_id = ensure_vlan_group(site_id, key)
                vid_map = sync_cisco_vlans(group_id, probe.get("hostname") or "", vlans)
                group_vlan_seen.setdefault(group_id, set()).update(vid_map.keys())
                legacy_sites.add(site_id)
            except Exception as e:
                log("WARN", f"  VLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")
```

Replace the post-loop sweep with:

```python
    # ── Sweep stale marker-owned VLANs per group + legacy site VLANs ─────────
    for group_id, seen in group_vlan_seen.items():
        try:
            sweep_stale_vlans(group_id, seen)
        except Exception as e:
            log("ERROR", f"  VLAN sweep failed for group {group_id}: {e}")
    for site_id in legacy_sites:
        try:
            sweep_legacy_site_vlans(site_id)
        except Exception as e:
            log("ERROR", f"  legacy VLAN sweep failed for site {site_id}: {e}")
```

- [ ] **Step 2: README (EN)** — replace the "VLAN sync (Cisco)" paragraph with:

```markdown
## VLAN sync (Cisco)

VLANs from `show vlan brief` are created/updated in IPAM grouped by **broadcast domain**: each switch's VTP domain (`show vtp status`; per-switch fallback when empty) maps to a site-scoped **VLAN group** named `BD1`, `BD2`… (stable across runs; the VTP key lives in the group description). Overlapping VLAN IDs at one site coexist in different groups. Interfaces get their VLAN linkage as before. Marker-owned (`netbox-sync:`) VLANs no longer reported by any switch in the group are deleted after each run; manual VLANs/groups are never modified or deleted.
```

- [ ] **Step 3: README (FA)** — mirror:

```markdown
## همگام‌سازی VLAN (سیسکو)

VLANهای `show vlan brief` بر اساس **دامنه broadcast** در IPAM گروه‌بندی می‌شوند: دامنه VTP هر سوئیچ (`show vtp status`؛ در صورت خالی بودن، به‌صورت per-switch) به یک **VLAN group** با نام `BD1`، `BD2`… نگاشت می‌شود (پایدار بین اجراها؛ کلید VTP در description گروه نگه‌داری می‌شود). VLANهای با ID هم‌پوشان در یک سایت در گروه‌های جداگانه کنار هم قرار می‌گیرند. اتصال VLAN رابط‌ها مانند قبل انجام می‌شود. VLANهای علامت‌دار (`netbox-sync:`) که دیگر هیچ سوئیچی در گروه گزارش نکند پس از هر اجرا حذف می‌شوند؛ VLANها/گروه‌های دستی هرگز تغییر یا حذف نمی‌شوند.
```

- [ ] **Step 4: Verify**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\sync.py netbox_sync\collectors\cisco.py
.\.venv\Scripts\python.exe -c "from netbox_sync.sync import run_sync; print('WIRING OK')"
```
Expected: 97 pass, compile OK, WIRING OK.

- [ ] **Step 5: Commit + push**

```bash
git add netbox_sync/sync.py README.md
git commit -m "Wire VTP-based VLAN groups into run_sync; document (EN+FA)"
git push origin main
```
