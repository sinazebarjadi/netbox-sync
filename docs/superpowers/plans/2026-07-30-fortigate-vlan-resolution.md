# FortiGate VLAN Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FortiGate VLANs match existing switch VLANs (unique reuse, overlap disambiguated via switch MAC tables); only FortiGate-only VLANs are created (per-device group).

**Architecture:** `fnsysctl ifconfig -a` MACs + targeted `show mac address-table address` lookups; `resolve_fortigate_vlans` pure function; run_sync tracks switch group membership and builds per-site vid indexes.

**Tech Stack:** Python 3.9+, netmiko, pytest fake harness.

**Spec:** `docs/superpowers/specs/2026-07-30-fortigate-vlan-resolution-design.md`

## Global Constraints

- vid unique at site → reuse; none → per-device group; multi → MAC disambiguation → per-device group + WARN on failure.
- `group_vlan_seen` for the per-device group gets ONLY created vids (migration: shared vids swept out automatically).
- Cisco-family-disabled → empty site index → per-device group for everything.
- Tests: `.\.venv\Scripts\python.exe -m pytest tests\ -q` (126 passing at plan time).

---

### Task 1: Parsers + collector MACs

**Files:**
- Modify: `netbox_sync/collectors/fortigate.py`, `netbox_sync/collectors/cisco.py`
- Test: `tests/test_fortigate.py`, `tests/test_cisco_parsers.py`

**Interfaces:**
- Produces: `fortigate._parse_ifconfig_a(text) -> dict[str, str]` (name → colon-lowercase MAC); `cisco._mac_to_cisco(mac) -> str`; `cisco._parse_mac_table_entry(text) -> [{"vid","mac","port"}]`; `fortigate_collect` return gains `"vlan_macs": {vid: mac}`; `cisco._cisco_mac_lookup(ip, mac) -> int | None`.

- [ ] **Step 1: Failing tests** — `tests/test_fortigate.py`:

```python
IFCONFIG_A = """AP MGMT\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:24
\tinet addr:172.31.2.1  Bcast:172.31.2.255  Mask:255.255.255.0

PartnerNet\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:26
\tinet addr:203.0.113.184  Bcast:203.0.113.191  Mask:255.255.255.240
"""


def test_parse_ifconfig_a():
    out = mod._parse_ifconfig_a(IFCONFIG_A)
    assert out == {"AP MGMT": "00:09:0f:09:00:24",
                   "PartnerNet": "00:09:0f:09:00:26"}
```

`tests/test_cisco_parsers.py`:

```python
def test_mac_to_cisco():
    assert mod._mac_to_cisco("00:09:0F:09:00:24") == "0009.0f09.0024"


MAC_TABLE = """          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  51    0009.0f09.0024    DYNAMIC     Te1/1/1
"""


def test_parse_mac_table_entry():
    rows = mod._parse_mac_table_entry(MAC_TABLE)
    assert rows == [{"vid": 51, "mac": "00:09:0f:09:00:24", "port": "Te1/1/1"}]
```

- [ ] **Step 2: Run to verify RED** — 4 FAIL (AttributeError).

- [ ] **Step 3: Implement**

In `fortigate.py` (parser section):

```python
def _parse_ifconfig_a(text):
    """Parse `fnsysctl ifconfig -a` blocks: name -> MAC (lowercase colon)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^(.+?)\tLink encap:Ethernet\s+HWaddr\s+([0-9A-Fa-f:]{17})', line)
        if m:
            out[m.group(1).strip()] = m.group(2).lower()
    return out
```

In `fortigate_collect`, after the transceivers block add:

```python
        ifc_out = _ssh_run_or_none(sess, "fnsysctl ifconfig -a", "ifconfig")
        if_macs = _parse_ifconfig_a(ifc_out) if ifc_out is not None else {}
```

and compute after the SSH section:

```python
    vlan_macs = {}
    for v in vlans:
        mac = if_macs.get(v["name"])
        if mac:
            vlan_macs[v["vid"]] = mac
```

Extend the return dict with `"vlan_macs": vlan_macs`.

In `cisco.py` (parser section):

```python
def _mac_to_cisco(mac):
    s = re.sub(r'[^0-9a-fA-F]', '', mac or '').lower()
    if len(s) != 12: return None
    return f"{s[0:4]}.{s[4:8]}.{s[8:12]}"

def _parse_mac_table_entry(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r'^\s*(\d+)\s+([0-9a-fA-F.]{14})\s+\S+\s+(\S+)', line)
        if m:
            mac = re.sub(r'[^0-9a-fA-F]', '', m.group(2)).lower()
            rows.append({"vid": int(m.group(1)),
                         "mac": ":".join(mac[i:i+2] for i in range(0, 12, 2)),
                         "port": m.group(3)})
    return rows
```

And the lookup helper (after `ensure_svi_interface`):

```python
def _cisco_mac_lookup(ip, cisco_mac):
    """Ask one switch for a specific MAC; return the VLAN it is learned in
    (or None). Used for FortiGate VLAN disambiguation."""
    sess = CiscoSwitchSession(ip)
    try:
        sess.login()
        rows = _parse_mac_table_entry(
            sess.run(f"show mac address-table address {cisco_mac}"))
        return rows[0]["vid"] if rows else None
    finally:
        sess.logout()
```

- [ ] **Step 4: GREEN + commit** — `pytest tests\ -q` (129 pass); `git commit -m "Add ifconfig/MAC-table parsers and per-MAC switch lookup"`

---

### Task 2: Resolution + `_site_vlan_index`

**Files:**
- Modify: `netbox_sync/collectors/fortigate.py`, `netbox_sync/collectors/cisco.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: Task 1. Produces: `cisco._site_vlan_index(api, site_id) -> {vid: [(group_id, vlan_id)]}`; `fortigate.resolve_fortigate_vlans(site_index, vlans, vlan_macs, mac_lookup) -> (vid_map, missing)`.

- [ ] **Step 1: Failing tests**

```python
def test_site_vlan_index(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g = FakeRecord(8, name="BD1", description="netbox-sync: vtp=example",
                   scope_type="dcim.site", scope_id=3)
    manual_g = FakeRecord(9, name="X", description="manual",
                          scope_type="dcim.site", scope_id=3)
    vlans = [FakeRecord(50, vid=10, group_id=8),
             FakeRecord(51, vid=20, group_id=8),
             FakeRecord(52, vid=10, group_id=9)]
    api = _vlan_api(vlans, [g, manual_g])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    index = cisco._site_vlan_index(3)
    assert index == {10: [(8, 50)], 20: [(8, 51)]}


def test_resolve_fortigate_vlans_paths():
    import netbox_sync.collectors.fortigate as fg
    site_index = {10: [(8, 50)], 20: [(8, 51), (9, 60)], 30: []}
    vlans = [{"vid": 10, "name": "A", "status": "active"},
             {"vid": 20, "name": "B", "status": "active"},
             {"vid": 30, "name": "C", "status": "active"},
             {"vid": 40, "name": "D", "status": "active"}]
    macs = {20: "00:09:0f:09:00:26"}
    lookup = lambda vid, mac: 9 if (vid, mac) == (20, "00:09:0f:09:00:26") else None

    vid_map, missing = fg.resolve_fortigate_vlans(site_index, vlans, macs, lookup)

    assert vid_map == {10: 50, 20: 60}     # unique reused; overlap resolved
    assert [v["vid"] for v in missing] == [30, 40]   # none + unresolved overlap
```

- [ ] **Step 2: Run to verify RED** — FAIL (AttributeError).

- [ ] **Step 3: Implement**

In `cisco.py` (after `ensure_vlan_group`):

```python
def _site_vlan_index(site_id):
    """Map every vid at the site to [(group_id, vlan_id)] using marker-owned
    VLAN groups only (manual groups ignored)."""
    api = netbox.get_netbox()
    index = {}
    for g in api.ipam.vlan_groups.filter(scope_type="dcim.site", scope_id=site_id):
        if not (g.description or "").startswith(VLAN_GROUP_MARKER):
            continue
        for vlan in api.ipam.vlans.filter(group_id=g.id):
            index.setdefault(vlan.vid, []).append((g.id, vlan.id))
    return index
```

In `fortigate.py` (before `sync_fortigate_interfaces`):

```python
def resolve_fortigate_vlans(site_vlan_index, vlans, vlan_macs, mac_lookup):
    """Match FortiGate VLANs to existing switch VLANs.
    unique -> reuse; none -> missing (create per-device); overlap ->
    mac_lookup(vid, mac) -> group_id (else missing + caller warns)."""
    vid_map, missing = {}, []
    for v in vlans:
        vid = v["vid"]
        matches = site_vlan_index.get(vid, [])
        if len(matches) == 1:
            vid_map[vid] = matches[0][1]
        elif not matches:
            missing.append(v)
        else:
            mac = vlan_macs.get(vid)
            gid = mac_lookup(vid, mac) if mac else None
            if gid:
                vid_map[vid] = next(vlan_id for g, vlan_id in matches if g == gid)
            else:
                missing.append(v)
    return vid_map, missing
```

- [ ] **Step 4: GREEN + commit** — `pytest tests\ -q` (131 pass); `git commit -m "Add site VLAN index and FortiGate VLAN resolution"`

---

### Task 3: run_sync wiring + docs + release

**Files:**
- Modify: `netbox_sync/sync.py`, `README.md`

**Interfaces:**
- Consumes: Tasks 1–2.

- [ ] **Step 1: Cisco block** — after `group_id = ensure_vlan_group(...)` add membership tracking (init `switch_group_ips = {}` next to `group_vlan_seen`):

```python
                switch_group_ips.setdefault(group_id, []).append(probe["ip"])
```

- [ ] **Step 2: FortiGate block** — replace the VLAN section with:

```python
        site_id = None
        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_id = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_id = None

        vid_map = {}
        if site_id:
            try:
                site_index = site_indexes.get(site_id)
                if site_index is None:
                    site_index = _site_vlan_index(site_id)
                    site_indexes[site_id] = site_index

                def _mac_lookup(vid, mac):
                    if not mac: return None
                    cmac = _mac_to_cisco(mac)
                    if not cmac: return None
                    for cand_gid, _vlan_id in site_index.get(vid, []):
                        for sw_ip in switch_group_ips.get(cand_gid, []):
                            try:
                                if _cisco_mac_lookup(sw_ip, cmac) == vid:
                                    return cand_gid
                            except Exception:
                                continue
                    return None

                vlan_macs = data.get("vlan_macs", {})
                vid_map, missing = resolve_fortigate_vlans(
                    site_index, vlans, vlan_macs, _mac_lookup)
                if missing:
                    group_id = ensure_vlan_group(site_id, probe.get("hostname") or ip)
                    created = sync_cisco_vlans(group_id, probe.get("hostname") or "", missing)
                    vid_map.update(created)
                    group_vlan_seen.setdefault(group_id, set()).update(created.keys())
                    legacy_sites.add(site_id)
                    log("INFO", f"  [OK] FortiGate {ip} — {len(vid_map) - len(created)} VLANs reused, {len(created)} created")
                else:
                    log("INFO", f"  [OK] FortiGate {ip} — all {len(vid_map)} VLANs reused from switches")
            except Exception as e:
                log("WARN", f"  FortiGate VLAN resolution failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")
```

(init `site_indexes = {}` next to `group_vlan_seen`; extend imports with
`_site_vlan_index, _mac_to_cisco, _cisco_mac_lookup` from cisco and
`resolve_fortigate_vlans` from fortigate.)

- [ ] **Step 3: README (EN+FA)** — FortiGate VLAN bullet: VLANs are matched to the switches' existing VLANs (unique reuse; overlap disambiguated via the switch MAC table using the subinterface MAC from `fnsysctl ifconfig -a`; only FortiGate-only VLANs are created in a per-device group).

- [ ] **Step 4: Verify**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\sync.py netbox_sync\collectors\fortigate.py netbox_sync\collectors\cisco.py
.\.venv\Scripts\python.exe -c "from netbox_sync.sync import run_sync; print('WIRING OK')"
```
Expected: 131 pass, WIRING OK. Then push.
