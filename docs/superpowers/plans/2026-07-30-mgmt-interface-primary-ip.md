# Interface Labels + Primary-IP Carrier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FortiGate alias→label; primary IPv4 assigned to the real management interface (FortiGate subif / Cisco SVI) with synthetic `mgmt` fallback.

**Architecture:** `ensure_primary_ip` gains an `iface_name` parameter; FortiGate matches the IP from cmdb data; Cisco adds `show ip interface brief` + SVI creation; call order moves after interface sync.

**Tech Stack:** Python 3.9+, pytest fake harness.

**Spec:** `docs/superpowers/specs/2026-07-30-mgmt-interface-primary-ip-design.md`

## Global Constraints

- Backward compatible: `iface_name=None` → behavior identical to today.
- Label set only when alias is non-empty.
- Fallback to synthetic `mgmt` (with WARN) when the named carrier isn't found.
- Tests: `.\.venv\Scripts\python.exe -m pytest tests\ -q` (120 passing at plan time).

---

### Task 1: FortiGate alias → label

**Files:**
- Modify: `netbox_sync/collectors/fortigate.py`
- Test: `tests/test_fortigate.py`, `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `_fg_interfaces` port dicts gain `"alias"`; `sync_fortigate_interfaces` sets `"label"` when alias non-empty.

- [ ] **Step 1: Failing tests**

In `tests/test_fortigate.py` — extend `CMDB_IFACES` port1 row with
`"alias": "UPLINK-CORE"` and add:

```python
def test_fg_interfaces_capture_alias():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["alias"] == "UPLINK-CORE"
    assert ports["port2"]["alias"] == ""
```

In `tests/test_netbox_sync.py::test_sync_fortigate_interfaces_bulk_and_vlan_subif`
add alias to the port1 input (`"alias": "UPLINK-CORE"`) and assert
`by_name["port1"]["label"] == "UPLINK-CORE"`.

- [ ] **Step 2: Run to verify RED** — `pytest tests\test_fortigate.py tests\test_netbox_sync.py -q` → 2 FAIL.

- [ ] **Step 3: Implement**

In `_fg_interfaces`, both the monitor-merge and cmdb-union port dicts gain:

```python
            "alias": c.get("alias") or "",
```

In `sync_fortigate_interfaces`, after each payload is built add:

```python
        if p.get("alias"):
            payload["label"] = str(p["alias"])[:64]
```

- [ ] **Step 4: GREEN + commit** — `pytest tests\ -q` (122 pass); `git commit -m "Map FortiGate interface alias to NetBox label"`

---

### Task 2: `iface_name` carrier for ensure_primary_ip

**Files:**
- Modify: `netbox_sync/netbox.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `ensure_primary_ip(dev_id, ip, hostname=None, iface_name=None) -> int`.

- [ ] **Step 1: Failing tests**

```python
def test_primary_ip_assigned_to_named_iface(monkeypatch):
    dev = FakeRecord(7, name="FW-DC-01", primary_ip4=None)
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    api = _ipam_api([], dev, [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "FW-DC-01", iface_name="MGMT54")

    # no synthetic mgmt interface created; IP assigned to the named one
    assert api.dcim.interfaces.created == []
    upd = api.ipam.ip_addresses.updated[0]
    assert upd["assigned_object_id"] == 70
    assert api.dcim.devices.updated[0]["primary_ip4"] is not None


def test_primary_ip_named_iface_missing_falls_back_to_mgmt(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev, [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "SW1", iface_name="Vlan999")

    # synthetic mgmt interface created as fallback
    assert api.dcim.interfaces.created[0]["name"] == "mgmt"
```

- [ ] **Step 2: Run to verify RED** — `pytest tests\test_netbox_sync.py -k primary_ip -q` → 2 FAIL (TypeError: unexpected keyword).

- [ ] **Step 3: Implement** (in `netbox_sync/netbox.py`)

Change the signature to
`def ensure_primary_ip(dev_id, ip, hostname=None, iface_name=None):` and
replace the assignment branch with:

```python
    assigned_iface = getattr(ip_rec, "assigned_object_id", None)
    if getattr(ip_rec, "assigned_object_type", None) == "dcim.interface" \
            and assigned_iface:
        iface = api.dcim.interfaces.get(id=assigned_iface)
        iface_dev = None
        if iface is not None:
            d = getattr(iface, "device", None)
            iface_dev = getattr(d, "id", None) if d is not None \
                        else getattr(iface, "device_id", None)
        if iface_dev != dev_id:
            log("WARN", f"  primary IPv4 {ip} is assigned to another device — "
                        f"leaving device id={dev_id} unchanged")
            return ip_id
    else:
        iface = None
        if iface_name:
            iface = api.dcim.interfaces.get(device_id=dev_id, name=iface_name)
            if iface is None:
                log("WARN", f"  carrier interface {iface_name} not found on "
                            f"device id={dev_id} — using synthetic mgmt")
        if iface is None:
            iface = _get_or_create_mgmt_iface(api, dev_id)
        api.ipam.ip_addresses.update([{
            "id": ip_id,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface.id,
        }])
```

- [ ] **Step 4: GREEN + commit** — `pytest tests\ -q` (122 pass); `git commit -m "ensure_primary_ip: assign to named carrier interface with mgmt fallback"`

---

### Task 3: Cisco `show ip interface brief` + SVI creation

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_cisco_parsers.py`, `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `_parse_ip_interface_brief(text) -> dict[str, str]`; collect return gains `"ip_brief"`; `ensure_svi_interface(dev_id, name, vid_map) -> id|None`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_cisco_parsers.py
IP_BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES NVRAM  administratively down down
Vlan50                 172.31.1.103    YES NVRAM  up                    up
GigabitEthernet1/0/1   unassigned      YES unset  up                    up
"""


def test_parse_ip_interface_brief():
    out = mod._parse_ip_interface_brief(IP_BRIEF)
    assert out == {"Vlan50": "172.31.1.103"}
```

```python
# tests/test_netbox_sync.py
def test_ensure_svi_interface_creates_virtual_with_vlan(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    iid = cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    created = ifaces_ep.created[0]
    assert created["name"] == "Vlan50"
    assert created["type"] == "virtual"
    assert created["untagged_vlan"] == 500
    assert created["mgmt_only"] is True
    assert iid is not None

    # second call reuses
    iid2 = cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    assert ifaces_ep.create_calls == 1


def test_ensure_svi_interface_non_vlan_name(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))
    cisco.ensure_svi_interface(7, "Loopback0", {})
    assert ifaces_ep.created[0]["untagged_vlan"] is None or \
           "untagged_vlan" not in ifaces_ep.created[0]
```

- [ ] **Step 2: Run to verify RED** — `pytest tests\test_cisco_parsers.py tests\test_netbox_sync.py -q` → FAILs.

- [ ] **Step 3: Implement** (in `netbox_sync/collectors/cisco.py`)

Parser (append to the parser section):

```python
def _parse_ip_interface_brief(text):
    """Parse `show ip interface brief` -> {interface: ip}; unassigned skipped."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+', s)
        if m:
            out[m.group(1)] = m.group(2)
    return out
```

In `cisco_collect_inventory`, after the vtp block add:

```python
        try:
            ip_brief = _parse_ip_interface_brief(sess.run("show ip interface brief"))
            log("INFO", f"  ip brief: {len(ip_brief)} addressed interfaces")
        except Exception as exc:
            ip_brief = {}
            log("WARN", f"  show ip interface brief failed: {exc}")
```

and extend the return dict with `"ip_brief": ip_brief`.

Helper (append near `sync_cisco_interfaces`):

```python
def ensure_svi_interface(dev_id, name, vid_map):
    """Get-or-create an SVI (e.g. Vlan50) as a virtual mgmt_only interface;
    untagged_vlan parsed from the VlanNN name when present in vid_map."""
    api = netbox.get_netbox()
    existing = api.dcim.interfaces.get(device_id=dev_id, name=name)
    if existing:
        return existing.id
    payload = {"device": dev_id, "name": name, "type": "virtual",
               "enabled": True, "mgmt_only": True,
               "description": "netbox-sync: SVI (management)"}
    m = re.match(r'^Vlan(\d+)$', name)
    if m and int(m.group(1)) in vid_map:
        payload["untagged_vlan"] = vid_map[int(m.group(1))]
    return api.dcim.interfaces.create(payload).id
```

- [ ] **Step 4: GREEN + commit** — `pytest tests\ -q` (125 pass); `git commit -m "Cisco: parse show ip interface brief; add SVI helper"`

---

### Task 4: Wire carriers in run_sync + docs + release

**Files:**
- Modify: `netbox_sync/sync.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1–3.

- [ ] **Step 1: FortiGate block** — move the `ensure_primary_ip` call to AFTER
  `sync_fortigate_interfaces`, and compute the carrier from collected ports:

```python
        carrier = None
        for p in ports:
            if (p.get("ip") or "").split(" ")[0] == ip:
                carrier = p["name"]
                break
        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"),
                              iface_name=carrier)
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")
```

(delete the earlier `ensure_primary_ip` call in that block.)

- [ ] **Step 2: Cisco block** — likewise move the call after
  `sync_interface_vlans`, unpack `ip_brief = data["ip_brief"]`, and:

```python
        carrier = ip_brief.get(ip)
        if carrier:
            synced_names = {p["port"] for p in ports}
            if carrier not in synced_names:
                try:
                    ensure_svi_interface(dev_id, carrier, vid_map)
                except Exception as e:
                    log("WARN", f"  SVI creation failed for {carrier} on {ip}: {e}")
        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"),
                              iface_name=carrier)
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")
```

- [ ] **Step 3: README (EN+FA)** — extend the primary-IPv4 bullet: the IP is assigned to the real management interface when identifiable (FortiGate VLAN subinterface / Cisco SVI, created as a virtual interface when missing), synthetic `mgmt` otherwise; FortiGate interface aliases map to NetBox labels.

- [ ] **Step 4: Verify + push**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\sync.py netbox_sync\collectors\cisco.py netbox_sync\collectors\fortigate.py netbox_sync\netbox.py
```
Expected: 125 pass, compile OK. Then `git push origin main`.
