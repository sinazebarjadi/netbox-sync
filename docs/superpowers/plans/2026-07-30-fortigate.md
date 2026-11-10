# FortiGate Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add FortiGate firewalls as a fifth family — REST API for device/interfaces/VLANs, SSH for LLDP cables and SFP transceivers, per-device tokens from a gitignored file.

**Architecture:** New `collectors/fortigate.py` (API session + SSH extras + pure mappers), config token-file loader, netbox ensure/mark functions, reuse of `ensure_vlan_group`/`sync_cisco_vlans`/`sync_inventory`/cable reconciliation, scanner + run_sync wiring.

**Tech Stack:** Python 3.9+, requests, netmiko (`fortinet`), pynetbox, pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-fortigate-design.md`

## Global Constraints

- Opt-in: `FORTIGATE_RANGES` empty = disabled; creds validated only when ranges set.
- Token file: `<ip[:port]> <token>` per line, `#` comments; default path `<repo>/fortigate_tokens.txt`; **gitignored**; per-IP port override (default 443).
- VLAN subinterfaces → NetBox interfaces `type=virtual`, `mode=tagged`, `untagged_vlan` (when vid in the group map).
- Cables: `sync_cdp_cables(..., protocol="lldp")` — description carries the protocol label.
- v1 queries `vdom=root` only. Bulk writes everywhere (performance guard).
- Tests: `.\.venv\Scripts\python.exe -m pytest tests\ -q` (105 passing at plan time).

---

### Task 1: Config — token file loader + env + validation

**Files:**
- Modify: `netbox_sync/config.py`
- Modify: `.gitignore`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `FORTIGATE_USER/PASS`, `FORTIGATE_PORT: int` (443), `FORTIGATE_SSH_PORT: int` (22), `FORTIGATE_RANGES: list` (default `[]`), `FORTIGATE_ROLE: str` ("Firewall"), `FORTIGATE_TOKEN_FILE: str`, `FORTIGATE_TOKENS: dict[str, tuple[int, str]]`, `_load_fortigate_tokens(path) -> dict`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
# ── FortiGate token file ─────────────────────────────────────────────────────

def test_fortigate_token_file_parsing(tmp_path):
    f = tmp_path / "tokens.txt"
    f.write_text(
        "# comment line\n"
        "\n"
        "172.31.1.1 token-one\n"
        "172.31.1.2:8443 token-two\n"
        "badline\n",
        encoding="utf-8")
    tokens = cfg._load_fortigate_tokens(str(f))
    assert tokens == {"172.31.1.1": (443, "token-one"),
                      "172.31.1.2": (8443, "token-two")}


def test_fortigate_token_file_missing(tmp_path):
    assert cfg._load_fortigate_tokens(str(tmp_path / "nope.txt")) == {}


def test_validate_config_fortigate_requirements(monkeypatch, tmp_path):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("FORTIGATE_USER", raising=False)
    monkeypatch.delenv("FORTIGATE_PASS", raising=False)
    monkeypatch.setenv("FORTIGATE_RANGES", "192.0.2.0/29")
    f = tmp_path / "tokens.txt"
    f.write_text("192.0.2.1 tok\n", encoding="utf-8")
    monkeypatch.setenv("FORTIGATE_TOKEN_FILE", str(f))
    with pytest.raises(RuntimeError, match="FORTIGATE_USER"):
        cfg._validate_config()
    monkeypatch.setenv("FORTIGATE_USER", "u")
    monkeypatch.setenv("FORTIGATE_PASS", "p")
    cfg._validate_config()   # creds + non-empty token file -> passes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k fortigate -q`
Expected: FAIL — `AttributeError: ... no attribute '_load_fortigate_tokens'` and validation not raising.

- [ ] **Step 3: Implement** (in `netbox_sync/config.py`)

After the CISCO credentials block add:

```python
FORTIGATE_USER = os.getenv("FORTIGATE_USER")
FORTIGATE_PASS = os.getenv("FORTIGATE_PASS")
```

In `_validate_config()`, after the Cisco check add (note: the env var is
read at call time, not via the module constant, so tests/cron can override):

```python
    if os.getenv("FORTIGATE_RANGES"):
        if not os.getenv("FORTIGATE_USER") or not os.getenv("FORTIGATE_PASS"):
            missing.append("FORTIGATE_USER/FORTIGATE_PASS (required when FORTIGATE_RANGES is set)")
        token_path = os.getenv("FORTIGATE_TOKEN_FILE", FORTIGATE_TOKEN_FILE)
        if not _load_fortigate_tokens(token_path):
            missing.append(f"FortiGate token file missing or empty ({token_path})")
```

After the CISCO_RANGES block add:

```python
# FortiGate family is opt-in: empty default means "disabled".
FORTIGATE_RANGES = _parse_ranges("FORTIGATE_RANGES", [])

FORTIGATE_PORT     = int(os.getenv("FORTIGATE_PORT", "443"))
FORTIGATE_SSH_PORT = int(os.getenv("FORTIGATE_SSH_PORT", "22"))
FORTIGATE_ROLE     = os.getenv("DEFAULT_FORTIGATE_ROLE", "Firewall")
FORTIGATE_TOKEN_FILE = os.getenv(
    "FORTIGATE_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fortigate_tokens.txt"))

def _load_fortigate_tokens(path):
    """Load per-device FortiGate API tokens: "<ip[:port]> <token>" per line.
    '#' comments and blank lines allowed; port defaults to 443."""
    tokens = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        if os.getenv("FORTIGATE_RANGES"):
            log("WARN", f"FortiGate token file not found: {path}")
        return tokens
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            log("WARN", f"FortiGate token file: bad line {s!r} — skipped")
            continue
        host, token = parts[0], parts[1]
        if ":" in host:
            ip, port_s = host.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                log("WARN", f"FortiGate token file: bad port in {s!r} — skipped")
                continue
        else:
            ip, port = host, 443
        tokens[ip] = (port, token)
    return tokens

FORTIGATE_TOKENS = _load_fortigate_tokens(FORTIGATE_TOKEN_FILE)
```

`.gitignore` — append:

```
# FortiGate API tokens (secrets)
fortigate_tokens.txt
```

- [ ] **Step 4: Run tests to verify they pass + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q` → 108 pass.

```bash
git add netbox_sync/config.py .gitignore tests/test_netbox_sync.py
git commit -m "Add FortiGate config and per-device token file loader"
```

---

### Task 2: API session, probe, collect, mappers

**Files:**
- Create: `netbox_sync/collectors/fortigate.py`
- Modify: `netbox_sync/models.py` (add `FORTIGATE_MODEL_MAP`)
- Test: `tests/test_fortigate.py` (new)

**Interfaces:**
- Produces: `FortiGateSession(ip, port, token)` (`.get(path) -> dict`); `probe_fortigate(ip, retries=2, retry_delay=3) -> dict|None`; `fortigate_collect(ip) -> {"summary","ports","vlans","neighbors","inventory"}`; `_fg_status(data)`, `_fg_interfaces(mon, cmdb)`, `_fg_vlans(cmdb)`, `_fg_interface_type(mbps)`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_fortigate.py`

```python
"""Tests for the FortiGate REST API mappers (pure JSON -> dict)."""
import netbox_sync.collectors.fortigate as mod


STATUS_JSON = {"http_method": "GET", "results": {
    "hostname": "FW-DC-01", "serial_number": "FGT60FTK2100XXXX",
    "model": "FortiGate-60F", "version": "v7.2.4"}}

MONITOR_IFACES = {"results": {
    "port1": {"link": True, "speed": "1000full", "duplex": "full"},
    "port2": {"link": False, "speed": "1000", "duplex": "full"},
    "port1.10": {"link": True, "speed": "1000full"}}}

CMDB_IFACES = {"results": [
    {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
    {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
    {"name": "port1.10", "type": "vlan", "vlanid": 10,
     "interface": "port1", "ip": "10.10.10.1 255.255.255.0"},
]}


def test_fg_status():
    out = mod._fg_status(STATUS_JSON)
    assert out == {"hostname": "FW-DC-01", "serial": "FGT60FTK2100XXXX",
                   "model": "FortiGate-60F", "version": "v7.2.4"}


def test_fg_interfaces_merge():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["link"] is True
    assert ports["port1"]["speed_mbps"] == 1000
    assert ports["port2"]["link"] is False
    assert ports["port2"]["speed_mbps"] == 1000
    assert ports["port1.10"]["vlanid"] == 10
    assert ports["port1.10"]["parent"] == "port1"


def test_fg_vlans():
    vlans = mod._fg_vlans(CMDB_IFACES)
    assert vlans == [{"vid": 10, "name": "port1.10", "status": "active"}]


def test_fg_interface_type():
    assert mod._fg_interface_type(100) == "100base-tx"
    assert mod._fg_interface_type(1000) == "1000base-t"
    assert mod._fg_interface_type(10000) == "10gbase-t"
    assert mod._fg_interface_type(None) == "other"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_fortigate.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `netbox_sync/collectors/fortigate.py`**

```python
"""FortiGate firewalls: REST API session (identity/interfaces/VLANs) plus
SSH extras (LLDP neighbors, SFP transceivers)."""
import re
import time

import requests
from netmiko import ConnectHandler

from netbox_sync import netbox
from netbox_sync.config import (FORTIGATE_USER, FORTIGATE_PASS, FORTIGATE_PORT,
                                FORTIGATE_SSH_PORT, FORTIGATE_TOKENS, log)
from netbox_sync.models import FORTIGATE_MODEL_MAP
from netbox_sync.utils import (normalize_model, _invalid_serial,
                               _make_add_item, is_port_open)

# ── REST API session + mappers ───────────────────────────────────────────────

class FortiGateSession:
    def __init__(self, ip, port, token, timeout=30):
        self.base = f"https://{ip}:{port}"
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"Authorization": f"Bearer {token}"})
        self.timeout = timeout

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

def _fg_status(data):
    results = data.get("results") or data
    return {
        "hostname": results.get("hostname"),
        "serial":   results.get("serial_number"),
        "model":    results.get("model") or results.get("model_name"),
        "version":  results.get("version"),
    }

def _fg_speed(m):
    s = str(m.get("speed") or "")
    digits = re.match(r'^(\d+)', s)
    return int(digits.group(1)) if digits else None

def _fg_interfaces(monitor_data, cmdb_data):
    """Merge /monitor/system/interface (link/speed) with /cmdb config."""
    mon = monitor_data.get("results") or {}
    cfg = cmdb_data.get("results") or []
    cfg_by_name = {c.get("name"): c for c in cfg if isinstance(c, dict)}
    ports = []
    for name, m in mon.items():
        if not isinstance(m, dict): continue
        c = cfg_by_name.get(name, {})
        ports.append({
            "name": name,
            "link": bool(m.get("link")),
            "speed_mbps": _fg_speed(m),
            "type": c.get("type") or "",
            "ip": c.get("ip") or "",
            "vlanid": c.get("vlanid"),
            "parent": c.get("interface") or "",
        })
    return ports

def _fg_vlans(cmdb_data):
    out = []
    for c in (cmdb_data.get("results") or []):
        if not isinstance(c, dict): continue
        if c.get("type") == "vlan" and c.get("vlanid") is not None:
            out.append({"vid": int(c["vlanid"]),
                        "name": c.get("name") or f"VLAN{int(c['vlanid']):04d}",
                        "status": "active"})
    return out

def _fg_interface_type(speed_mbps):
    return {100: "100base-tx", 1000: "1000base-t",
            10000: "10gbase-t", 25000: "25gbase-x-sfp28",
            40000: "40gbase-x-qsfpp"}.get(speed_mbps, "other")

# ── SSH extras (LLDP + transceivers) ─────────────────────────────────────────

class FortiGateSSHSession:
    def __init__(self, ip, timeout=20):
        self.ip = ip
        self.timeout = timeout
        self.conn = None

    def login(self):
        self.conn = ConnectHandler(
            device_type="fortinet", host=self.ip, port=FORTIGATE_SSH_PORT,
            username=FORTIGATE_USER, password=FORTIGATE_PASS,
            conn_timeout=self.timeout, auth_timeout=self.timeout,
            banner_timeout=self.timeout)

    def run(self, command):
        if not self.conn:
            raise RuntimeError("SSH session not open")
        return self.conn.send_command(command, read_timeout=self.timeout)

    def logout(self):
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception: pass
        self.conn = None

def _parse_lldp_summary(text):
    """Parse `diagnose lldp neighbor-summary` into CDP-shaped neighbors."""
    entries = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("-") or s.lower().startswith("port"):
            continue
        m = re.match(r'^(\S+)\s+([0-9a-fA-F:]{17})\s+(.+?)\s+([A-Z,]+)\s+(\d+)\s+(\S+)$', s)
        if not m: continue
        entries.append({"device_id": m.group(3), "platform": "",
                        "local_intf": m.group(1), "remote_intf": m.group(6),
                        "ip": None})
    return entries

def _parse_transceivers(text):
    """Parse `diagnose sys transceiver list`: per-port vendor/part/serial."""
    rows = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^Port\s+(\d+)\s*:', s)
        if m:
            if cur: rows.append(cur)
            cur = {"port": int(m.group(1))}
            continue
        if cur is None: continue
        m = re.match(r'^(Vendor|Part Number|Serial Number)\s*:\s*(.+)$', s)
        if m:
            key = m.group(1).lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur: rows.append(cur)
    return rows

# ── probe + collect ──────────────────────────────────────────────────────────

def probe_fortigate(ip, retries=2, retry_delay=3):
    entry = FORTIGATE_TOKENS.get(ip)
    if not entry:
        log("DEBUG", f"  no FortiGate API token for {ip} — skipping")
        return None
    port, token = entry
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, port, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        try:
            status = _fg_status(
                FortiGateSession(ip, port, token).get("/api/v2/monitor/system/status"))
            if not (status.get("serial") or status.get("model")):
                raise RuntimeError("status yielded no serial/model")
            return {
                "ip": ip, "host": f"{ip}:{port}",
                "serial": status.get("serial"),
                "model": (normalize_model(status.get("model"), FORTIGATE_MODEL_MAP)
                          or status.get("model")),
                "hostname": (status.get("hostname")
                             or f"fortigate-{ip.replace('.', '-')}"),
                "manufacturer": "Fortinet",
                "firmware": status.get("version"),
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None

def fortigate_collect(ip):
    entry = FORTIGATE_TOKENS.get(ip)
    if not entry:
        raise RuntimeError(f"no FortiGate API token for {ip}")
    port, token = entry
    fg = FortiGateSession(ip, port, token)
    status = _fg_status(fg.get("/api/v2/monitor/system/status"))
    mon = fg.get("/api/v2/monitor/system/interface")
    cmdb = fg.get("/api/v2/cmdb/system/interface?vdom=root")
    ports = _fg_interfaces(mon, cmdb)
    vlans = _fg_vlans(cmdb)
    log("INFO", f"  fortigate api: {len(ports)} interfaces, {len(vlans)} vlans")

    neighbors = []
    inventory = {}
    sess = FortiGateSSHSession(ip)
    try:
        sess.login()
        try:
            neighbors = _parse_lldp_summary(sess.run("diagnose lldp neighbor-summary"))
            log("INFO", f"  lldp neighbors: {len(neighbors)}")
        except Exception as exc:
            log("WARN", f"  lldp neighbor-summary failed: {exc}")
        try:
            add = _make_add_item(inventory)
            for row in _parse_transceivers(sess.run("diagnose sys transceiver list")):
                serial = row.get("serial_number")
                if _invalid_serial(serial): continue
                add(name=f"SFP Port {row.get('port')}",
                    manufacturer=row.get("vendor") or "Unknown",
                    part_number=row.get("part_number"), serial=serial,
                    description=f"Port={row.get('port')}",
                    role_id=netbox.get_or_create_inventory_role("SFP", "4caf50"))
            log("INFO", f"  transceivers: {len(inventory)}")
        except Exception as exc:
            log("WARN", f"  transceiver list failed: {exc}")
    except Exception as exc:
        log("WARN", f"  fortigate ssh failed for {ip}: {exc}")
    finally:
        try: sess.logout()
        except Exception: pass

    summary = {
        "serial": status.get("serial"),
        "model": (normalize_model(status.get("model"), FORTIGATE_MODEL_MAP)
                  or status.get("model")),
        "firmware": status.get("version"),
        "hostname": (status.get("hostname") or "").strip(),
        "port_count": len(ports),
    }
    return {"summary": summary, "ports": ports, "vlans": vlans,
            "neighbors": neighbors, "inventory": inventory}
```

Add to `netbox_sync/models.py`:

```python
# ── FortiGate firewalls ──────────────────────────────────────────────────────
# Keys: raw /monitor/system/status "model" string, lowercased.
FORTIGATE_MODEL_MAP = {
    "fortigate-60f":  "FortiGate 60F",
    "fortigate-100f": "FortiGate 100F",
    "fortigate-200f": "FortiGate 200F",
    "fortigate-40f":  "FortiGate 40F",
    "fortigate-80f":  "FortiGate 80F",
}
```

- [ ] **Step 4: Run tests to verify they pass + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q` → 112 pass.

```bash
git add netbox_sync/collectors/fortigate.py netbox_sync/models.py tests/test_fortigate.py
git commit -m "Add FortiGate API session, probe, collect and mappers"
```

---

### Task 3: SSH parser fixtures

**Files:**
- Test: `tests/test_fortigate.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
LLDP_SUMMARY = """-----------------------------------------------------------------------------
Port        Device ID          SysName          Capabilities  TTL   Port ID
port1       00:1c:73:ab:cd:ef  SW-BRANCH-02      B,R           120   Gi1/0/1
port2       00:1c:73:ab:cd:00  R4-Core-LAN-SW   B,R           120   Twe1/0/31
"""


def test_parse_lldp_summary():
    entries = mod._parse_lldp_summary(LLDP_SUMMARY)
    assert len(entries) == 2
    assert entries[0]["device_id"] == "SW-BRANCH-02"
    assert entries[0]["local_intf"] == "port1"
    assert entries[0]["remote_intf"] == "Gi1/0/1"


TRANSCEIVERS = """Port 1  : SFP/SFP+ (10G)
   Vendor            : FINISAR CORP.
   Part Number       : FTLX8571D3BCL
   Serial Number     : ABC123456

Port 2  : SFP/SFP+ (10G)
   Vendor            : CISCO
   Part Number       : SFP-10G-SR
   Serial Number     : DEF789012
"""


def test_parse_transceivers():
    rows = mod._parse_transceivers(TRANSCEIVERS)
    assert len(rows) == 2
    assert rows[0]["vendor"] == "FINISAR CORP."
    assert rows[0]["part_number"] == "FTLX8571D3BCL"
    assert rows[0]["serial_number"] == "ABC123456"
    assert rows[1]["port"] == 2
```

- [ ] **Step 2: Run + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q` → 114 pass.

```bash
git add tests/test_fortigate.py
git commit -m "Add FortiGate LLDP and transceiver parser fixtures"
```

---

### Task 4: NetBox side — ensure/mark, interfaces, cable protocol

**Files:**
- Modify: `netbox_sync/netbox.py`
- Modify: `netbox_sync/collectors/cisco.py` (protocol param on `sync_cdp_cables`)
- Modify: `netbox_sync/collectors/fortigate.py` (add `sync_fortigate_interfaces`)
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `ensure_fortigate_device(probe) -> int`; `mark_fortigate_offline(dev_id, dev_name)`; `sync_fortigate_interfaces(dev_id, ports, vid_map)`; `sync_cdp_cables(dev_id, neighbors, protocol="cdp")`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
# ── FortiGate device + interfaces ────────────────────────────────────────────

def test_ensure_fortigate_device_creates(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    nbx.ensure_fortigate_device({
        "ip": "192.0.2.70", "serial": "FGT60FTK2100XXXX",
        "model": "FortiGate 60F", "hostname": "FW-DC-01",
        "manufacturer": "Fortinet", "firmware": "v7.2.4"})
    payload = devices_ep.created[0]
    assert payload["serial"] == "FGT60FTK2100XXXX"
    assert payload["custom_fields"]["fortigate_ip"] == "192.0.2.70"
    assert payload["custom_fields"]["fortigate_enabled"] is True


def test_sync_fortigate_interfaces_bulk_and_vlan_subif(monkeypatch):
    import netbox_sync.collectors.fortigate as fg
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="port1", device_id=7),
        FakeRecord(2, name="port9", device_id=7, mgmt_only=False),
    ])
    api = _fake_api(interfaces=ifaces_ep)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"name": "port1", "link": True, "speed_mbps": 1000,
         "type": "physical", "ip": "", "vlanid": None, "parent": ""},
        {"name": "port1.10", "link": True, "speed_mbps": 1000,
         "type": "vlan", "ip": "10.10.10.1/24", "vlanid": 10, "parent": "port1"},
    ]
    fg.sync_fortigate_interfaces(7, ports, {10: 110})

    by_name = {}
    for u in ifaces_ep.updated:
        rec = next(i for i in ifaces_ep.items if i.id == u["id"])
        by_name[rec.name] = u
    assert by_name["port1"]["type"] == "1000base-t"
    created = {c["name"]: c for c in ifaces_ep.created}
    assert created["port1.10"]["type"] == "virtual"
    assert created["port1.10"]["untagged_vlan"] == 110
    assert created["port1.10"]["mode"] == "tagged"
    assert ifaces_ep.deleted_ids == [2]
    assert ifaces_ep.update_calls == 1 and ifaces_ep.create_calls == 1


def test_cdp_cables_protocol_label(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS, protocol="lldp")
    assert " lldp " in api.dcim.cables.created[0]["description"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k "fortigate or protocol" -q`
Expected: FAIL (AttributeError on ensure/sync; protocol TypeError).

- [ ] **Step 3: Implement**

In `netbox_sync/netbox.py`: extend config import with `FORTIGATE_ROLE`, models import with `FORTIGATE_MODEL_MAP`, and append (mirroring `ensure_cisco_device` / `mark_cisco_offline`):

```python
def ensure_fortigate_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Fortinet")
    role_id = get_or_create_role(FORTIGATE_ROLE, "c62828")
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, FORTIGATE_MODEL_MAP)
    name = _device_name(probe, prefix="fortigate")
    api = get_netbox()
    dev = find_device(serial, role_name=FORTIGATE_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found fortigate by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "fortigate_ip":       probe["ip"],
            "fortigate_enabled":  True,
            "fortigate_firmware": probe.get("firmware"),
            "fortigate_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  FortiGate updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  FortiGate created: {name} (id={new.id})")
    return new.id

def mark_fortigate_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"fortigate_enabled": False},
        }])
        log("WARN", f"  FortiGate marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark FortiGate offline {dev_name}: {e}")
```

In `netbox_sync/collectors/cisco.py`: change the signature to
`def sync_cdp_cables(dev_id, neighbors, protocol="cdp"):` and the description
line to `desc = (f"{CABLE_MARKER} {protocol} {local.name} <-> "
f"{peer_name} {peer_iface.name}")`.

Append to `netbox_sync/collectors/fortigate.py`:

```python
# ── interfaces ───────────────────────────────────────────────────────────────

def sync_fortigate_interfaces(dev_id, ports, vid_map):
    """Bulk create/update FortiGate interfaces: physical ports typed by
    speed, VLAN subinterfaces as virtual with untagged_vlan."""
    api = netbox.get_netbox()
    existing = {str(i.name): i
                for i in api.dcim.interfaces.filter(device_id=dev_id)}
    seen = set()
    updates, creates = [], []
    for p in ports:
        name = p["name"]
        seen.add(name)
        if p.get("type") == "vlan" and p.get("vlanid") is not None:
            payload = {"device": dev_id, "name": name, "type": "virtual",
                       "enabled": p.get("link", False),
                       "description": f"vlanid={p['vlanid']} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
            if p["vlanid"] in vid_map:
                payload["mode"] = "tagged"
                payload["untagged_vlan"] = vid_map[p["vlanid"]]
        else:
            payload = {"device": dev_id, "name": name,
                       "type": _fg_interface_type(p.get("speed_mbps")),
                       "enabled": bool(p.get("link")),
                       "description": f"type={p.get('type')} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
        if name in existing:
            updates.append({"id": existing[name].id, **payload})
        else:
            creates.append(payload)
    if updates:
        api.dcim.interfaces.update(updates)
    if creates:
        try:
            api.dcim.interfaces.create(creates)
        except Exception as e:
            log("WARN", f"  Could not create interfaces: {e}")
    for name, iface in existing.items():
        if name not in seen:
            if getattr(iface, "mgmt_only", False):
                continue
            try: iface.delete()
            except Exception: pass
```

- [ ] **Step 4: Run tests to verify they pass + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q` → 117 pass.

```bash
git add netbox_sync/netbox.py netbox_sync/collectors/cisco.py netbox_sync/collectors/fortigate.py tests/test_netbox_sync.py
git commit -m "Add FortiGate device layer, bulk interface sync, LLDP cable protocol"
```

---

### Task 5: Wiring + docs + release

**Files:**
- Modify: `netbox_sync/scanner.py`, `netbox_sync/sync.py`, `.env.example`, `README.md`

**Interfaces:**
- Consumes: all previous tasks. `scan_all()["fortigates"]` key.

- [ ] **Step 1: Scanner** — import `FORTIGATE_RANGES` and `probe_fortigate`; init `"fortigates": []` in `all_found`; append a fifth block after the Cisco one:

```python
    # ── FortiGates (REST API, opt-in family) ────────────────────────────────
    if FORTIGATE_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["cisco_switches"]}
        all_fg_ips = expand_ranges(FORTIGATE_RANGES)
        fg_ips = [ip for ip in all_fg_ips if ip not in used_ips]
        if fg_ips:
            log("INFO", f"Scanning {len(fg_ips)} IPs for FortiGates (API) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_fortigate, ip): ip for ip in fg_ips}
            def _on_fg(r):
                log("INFO", f"  + FORTIGATE {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["fortigates"].append(r)
            _drain_pool(ex, futures, _on_fg)
            log("INFO", f"FortiGate scan done: {len(all_found['fortigates'])} found.")
        else:
            log("INFO", "No FortiGate IPs to scan (all excluded).")
    else:
        log("INFO", "FortiGate ranges not configured — skipping FortiGate scan.")
```

- [ ] **Step 2: `run_sync`** — import `probe`/`collect`/`sync_fortigate_interfaces`, `ensure_fortigate_device`, `mark_fortigate_offline`. Add the processing block after Cisco (before the VLAN sweeps), mirroring the Cisco block: ensure → ensure_primary_ip → collect → custom-fields update → `ensure_vlan_group(site_id, probe.get("hostname") or ip)` → `sync_cisco_vlans(group_id, hostname, vlans)` → `sync_fortigate_interfaces(dev_id, ports, vid_map)` → `sync_inventory` → `sync_cdp_cables(dev_id, neighbors, protocol="lldp")`. Feed `group_vlan_seen` and `legacy_sites`. Add the offline sweep:

```python
    _offline_sweep(api, bool(FORTIGATE_RANGES), "cf_fortigate_enabled", "fortigate_ip",
                   live_fortigate_ips, mark_fortigate_offline, "FortiGates")
```

(extend the config import with `FORTIGATE_RANGES`.)

- [ ] **Step 3: .env.example** — append:

```dotenv
# FortiGate SSH credentials (for LLDP + transceivers) — required when FORTIGATE_RANGES is set
FORTIGATE_USER=changeme
FORTIGATE_PASS=changeme
FORTIGATE_PORT=443
FORTIGATE_SSH_PORT=22
# Per-device API tokens file: "<ip[:port]> <token>" per line (gitignored)
FORTIGATE_TOKEN_FILE=fortigate_tokens.txt

# Comma-separated CIDR ranges to scan for FortiGates (empty = family disabled)
# FORTIGATE_RANGES=192.0.2.96/29
DEFAULT_FORTIGATE_ROLE=Firewall
```

- [ ] **Step 4: README (EN+FA)** — add FortiGate to the intro family list; env table rows (FORTIGATE_USER/PASS/PORT/SSH_PORT/TOKEN_FILE/RANGES/DEFAULT_FORTIGATE_ROLE); custom-fields table for FortiGate (`fortigate_ip`, `fortigate_enabled`, `fortigate_firmware`, `fortigate_model`, `fortigate_port_count`); supported-hardware bullet (REST API + SSH extras, token-file format documented); offline paragraph adds `cf_fortigate_enabled`.

- [ ] **Step 5: Verify**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\scanner.py netbox_sync\sync.py netbox_sync\collectors\fortigate.py
.\.venv\Scripts\python.exe -c "from netbox_sync.sync import run_sync; print('WIRING OK')"
```
Expected: 117+ pass, compile OK, WIRING OK.

- [ ] **Step 6: Commit + push**

```bash
git add netbox_sync/scanner.py netbox_sync/sync.py .env.example README.md
git commit -m "Wire FortiGate family into scanner and run_sync; document (EN+FA)"
git push origin main
```
