# Primary IPv4 for Discovered Devices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create/update an IPAM address for each discovered device's management IP and set it as the device's `primary_ip4` in NetBox, for all four families.

**Architecture:** `utils._mgmt_prefixlen(ip)` derives the mask from containing scan ranges (`/32` fallback); `netbox.ensure_primary_ip(dev_id, ip, hostname)` reuses existing IPAM records or creates one with a `netbox-sync: mgmt` marker, then points the device at it (no write when already correct). Called from `run_sync` after each `ensure_*_device`.

**Tech Stack:** Python 3.9+, stdlib `ipaddress`, pynetbox, pytest with the existing fake harness.

**Spec:** `docs/superpowers/specs/2026-07-29-primary-ipv4-design.md`

## Global Constraints

- All four families; servers use their BMC/iLO IP.
- Existing IPAM records are reused unchanged regardless of mask/data.
- New records: `status=active`, `description="netbox-sync: mgmt"`, sanitized lowercase `dns_name` (omitted when empty).
- No device write when `primary_ip4` already points at the right IP.
- Failures are WARN-and-continue (never abort a device's sync).
- No airflow, no platform fields, no interface assignment, no stale-IP cleanup.
- Tests run with `.\.venv\Scripts\python.exe -m pytest tests\ -q` (81 passing at plan time).

---

### Task 1: `_mgmt_prefixlen` + `ensure_primary_ip`

**Files:**
- Modify: `netbox_sync/utils.py`
- Modify: `netbox_sync/netbox.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `utils._mgmt_prefixlen(ip) -> int`; `netbox.ensure_primary_ip(dev_id: int, ip: str, hostname: str|None = None) -> int` (IPAM address id).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
# ── primary IPv4 ─────────────────────────────────────────────────────────────

def test_mgmt_prefixlen_from_ranges(monkeypatch):
    monkeypatch.setattr(utils, "BMC_RANGES", ["10.0.0.0/24"])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/27"])
    assert utils._mgmt_prefixlen("10.0.0.5") == 24
    assert utils._mgmt_prefixlen("172.31.1.5") == 27
    assert utils._mgmt_prefixlen("192.0.2.9") == 32   # no range contains it
    assert utils._mgmt_prefixlen("junk") == 32        # invalid tolerated


def _ipam_api(ip_items, device_record):
    # api.ipam is a separate pynetbox app from api.dcim — model both
    return SimpleNamespace(
        dcim=SimpleNamespace(
            devices=FakeEndpoint([device_record] if device_record else [])),
        ipam=SimpleNamespace(ip_addresses=FakeEndpoint(ip_items)))


def test_primary_ip_created_with_range_mask(monkeypatch):
    monkeypatch.setattr(utils, "BMC_RANGES", [])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/24"])
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "SW-BRANCH-02")

    created = api.ipam.ip_addresses.created[0]
    assert created["address"] == "172.31.1.103/24"
    assert created["dns_name"] == "sw-branch-02"
    assert created["description"] == "netbox-sync: mgmt"
    assert created["status"] == "active"
    assert {u["id"] for u in api.dcim.devices.updated} == {7}
    assert api.dcim.devices.updated[0]["primary_ip4"] == ip_id


def test_primary_ip_reuses_existing(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    existing_ip = FakeRecord(50, address="172.31.1.103")
    api = _ipam_api([existing_ip], dev)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert ip_id == 50
    assert api.ipam.ip_addresses.created == []   # reused, no new record


def test_primary_ip_no_write_when_already_correct(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=FakeRecord(50))
    existing_ip = FakeRecord(50, address="172.31.1.103")
    api = _ipam_api([existing_ip], dev)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert api.dcim.devices.updated == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k "primary_ip or mgmt_prefixlen" -q`
Expected: FAIL — `AttributeError: ... no attribute 'ensure_primary_ip'` / `' _mgmt_prefixlen'`.

- [ ] **Step 3: Implement `_mgmt_prefixlen`** (in `netbox_sync/utils.py`)

Extend the config import line to:

```python
from netbox_sync.config import (SITE_KEYWORD_MAP, SITE_UNKNOWN, SITE_IP_MAP,
                                BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES)
```

Append:

```python
def _mgmt_prefixlen(ip):
    """Prefix length for a management IP: the prefix length of the first
    configured scan range (BMC, storage, SAN, Cisco — in that order) that
    contains it; 32 when no range does."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except (ValueError, AttributeError):
        return 32
    for ranges in (BMC_RANGES, STORAGE_RANGES, SAN_RANGES, CISCO_RANGES):
        for cidr in ranges:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if addr in net:
                    return net.prefixlen
            except (ValueError, TypeError):
                continue
    return 32
```

- [ ] **Step 4: Implement `ensure_primary_ip`** (in `netbox_sync/netbox.py`)

Add `import re` to the module imports, extend the utils import to include
`_mgmt_prefixlen`, and append after `find_device`:

```python
def _sanitize_dns_name(hostname):
    h = re.sub(r'[^a-z0-9.-]', '', (hostname or "").lower())[:63]
    return h or None

def ensure_primary_ip(dev_id, ip, hostname=None):
    """Create/update the management IP in IPAM and set it as the device's
    primary IPv4. Existing IPAM records are reused unchanged (any mask);
    new ones get the scan-range-derived prefix length."""
    api = get_netbox()
    existing = list(api.ipam.ip_addresses.filter(address=str(ip)))
    if existing:
        ip_id = existing[0].id
    else:
        payload = {
            "address": f"{ip}/{_mgmt_prefixlen(ip)}",
            "status": "active",
            "description": "netbox-sync: mgmt",
        }
        dns = _sanitize_dns_name(hostname)
        if dns:
            payload["dns_name"] = dns
        ip_id = api.ipam.ip_addresses.create(payload).id
    dev = api.dcim.devices.get(id=dev_id)
    current = getattr(getattr(dev, "primary_ip4", None), "id", None) if dev else None
    if current != ip_id:
        api.dcim.devices.update([{"id": dev_id, "primary_ip4": ip_id}])
    return ip_id
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (85).

- [ ] **Step 6: Commit**

```bash
git add netbox_sync/utils.py netbox_sync/netbox.py tests/test_netbox_sync.py
git commit -m "Add ensure_primary_ip with scan-range-derived mask"
```

---

### Task 2: Call sites + docs + release

**Files:**
- Modify: `netbox_sync/sync.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `netbox.ensure_primary_ip` (Task 1).

- [ ] **Step 1: Wire the call sites** (in `netbox_sync/sync.py`)

Add `ensure_primary_ip` to the netbox imports, then insert immediately after
each of the four `dev_id = ensure_*_device(probe)` try/except blocks (servers,
storage, SAN switches, Cisco switches), before inventory collection:

```python
        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")
```

- [ ] **Step 2: README (EN)** — in "What it does", add a bullet after item 4:

```markdown
5. **Records each device's management IP** in IPAM (mask derived from the scan range, `/32` fallback; marker description `netbox-sync: mgmt`) and sets it as the device's **primary IPv4** in NetBox.
```

(renumber the following bullets accordingly).

- [ ] **Step 3: README (FA)** — mirror as a bullet:

```markdown
5. **IP مدیریتی هر دستگاه** در IPAM ثبت می‌شود (ماسک از روی بازه اسکن، با پیش‌فرض `/32`؛ توضیح علامت‌دار `netbox-sync: mgmt`) و به‌عنوان **primary IPv4** دستگاه در NetBox تنظیم می‌گردد.
```

- [ ] **Step 4: Verify**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\sync.py netbox_sync\netbox.py netbox_sync\utils.py
```
Expected: 85 pass, compile OK.

- [ ] **Step 5: Commit + push**

```bash
git add netbox_sync/sync.py README.md
git commit -m "Set management IP as device primary IPv4 for all families"
git push origin main
```
