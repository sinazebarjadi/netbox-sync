"""Cisco Catalyst (IOS / IOS-XE) switches: netmiko SSH session, CLI output
parsers, probing, inventory collection, interface sync and CDP/LLDP cable
reconciliation."""
import re
import time

from netmiko import ConnectHandler

from netbox_sync import netbox
from netbox_sync.config import (CISCO_USER, CISCO_PASS, CISCO_PORT, log)
from netbox_sync.models import CISCO_MODEL_MAP
from netbox_sync.utils import (normalize_model, _invalid_serial,
                                _make_add_item, is_port_open)
from netbox_sync.report import classify_error, record_probe_failure

# ── CLI output parsers ───────────────────────────────────────────────────────

def _parse_show_version(text):
    """Parse `show version` for both classic IOS and IOS-XE dialects."""
    out = {"hostname": None, "model": None, "serial": None, "ios_version": None}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(\S+)\s+uptime is\b', s, re.IGNORECASE)
        if m and not out["hostname"]:
            out["hostname"] = m.group(1)
        m = re.search(r'Version\s+(\S+?)(?:,|\s+RELEASE)', s)
        if m and not out["ios_version"]:
            out["ios_version"] = m.group(1)
        m = re.match(r'^cisco\s+(\S+)\s+\(', s, re.IGNORECASE)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Model\s+(?:number|Number)\s*:\s*(\S+)', s)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Processor board ID\s+(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
        m = re.match(r'^System Serial Number\s*:\s*(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
    return out

def _parse_show_inventory(text):
    """Parse `show inventory` NAME/DESCR + PID/VID/SN line pairs."""
    rows = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^NAME:\s*"([^"]*)",\s*DESCR:\s*"([^"]*)"', s, re.IGNORECASE)
        if m:
            if cur: rows.append(cur)
            cur = {"name": m.group(1), "descr": m.group(2),
                   "pid": None, "vid": None, "sn": None}
            continue
        m = re.match(r'^PID:\s*([^,]*),\s*VID:\s*([^,]*),\s*SN:\s*(.*)$', s, re.IGNORECASE)
        if m and cur is not None:
            cur["pid"] = m.group(1).strip() or None
            cur["vid"] = m.group(2).strip() or None
            cur["sn"]  = m.group(3).strip() or None
    if cur: rows.append(cur)
    return rows

_INTF_STATUS_RE = re.compile(
    r'^(\S+)\s+(.*?)\s+'
    r'(connected|notconnect|disabled|err-disabled|inactive|monitoring|suspended)\s+'
    r'(\S+)\s+(\S+)\s+(\S+)\s+(.*)$', re.IGNORECASE)

def _parse_interfaces_status(text):
    """Parse the fixed-width `show interfaces status` table. The Name column
    is optional and free-form, so the row is anchored on the status keyword."""
    ports = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        if re.match(r'^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type\s*$',
                    s, re.IGNORECASE):
            continue
        m = _INTF_STATUS_RE.match(s)
        if not m: continue
        ports.append({
            "port":    m.group(1),
            "name":    m.group(2).strip(),
            "status":  m.group(3),
            "vlan":    m.group(4),
            "duplex":  m.group(5),
            "speed":   m.group(6),
            "type":    m.group(7).strip(),
        })
    return ports

_INTF_PREFIXES = (("TwentyFiveGigE", "Twe"), ("FortyGigabitEthernet", "Fo"),
                  ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
                  ("FastEthernet", "Fa"), ("HundredGigE", "Hu"),
                  ("Port-channel", "Po"), ("Ethernet", "Eth"))

def _short_intf(name):
    """GigabitEthernet1/0/1 -> Gi1/0/1 (CDP/LLDP use long names, the
    interfaces-status table uses short ones)."""
    n = (name or "").strip()
    for long, short in _INTF_PREFIXES:
        if n.startswith(long):
            return short + n[len(long):]
    return n

def _normalize_cdp_id(device_id):
    """CDP device IDs may carry a domain suffix (SW2.example.com) — strip it."""
    return (device_id or "").split(".")[0].strip()

def _parse_cdp_detail(text):
    """Parse `show cdp neighbors detail` into per-entry dicts."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Device ID:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = {"device_id": m.group(1), "platform": "",
                   "local_intf": None, "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^IP address:\s*(\S+)', s, re.IGNORECASE)
        if m and not cur["ip"]:
            cur["ip"] = m.group(1); continue
        m = re.match(r'^Platform:\s*([^,]+),', s, re.IGNORECASE)
        if m:
            cur["platform"] = m.group(1).strip(); continue
        m = re.match(r'^Interface:\s*(\S+?),\s*Port ID \(outgoing port\):\s*(\S+)',
                     s, re.IGNORECASE)
        if m:
            cur["local_intf"], cur["remote_intf"] = m.group(1), m.group(2)
            continue
    if cur and cur.get("local_intf"): entries.append(cur)
    return entries

def _parse_lldp_detail(text):
    """Parse `show lldp neighbors detail` into the same shape as CDP.
    Only used as a fallback when CDP yields no entries."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Local Intf:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = {"device_id": None, "platform": "",
                   "local_intf": m.group(1), "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^System Name:\s*(.+)$', s, re.IGNORECASE)
        if m:
            cur["device_id"] = m.group(1).strip(); continue
        m = re.match(r'^Port id:\s*(\S+)', s, re.IGNORECASE)
        if m:
            cur["remote_intf"] = m.group(1); continue
    if cur and (cur.get("local_intf") or cur.get("device_id")):
        entries.append(cur)
    return [e for e in entries if e.get("device_id")]

def _parse_vlan_brief(text):
    """Parse `show vlan brief`: vid, name, status. Ports column ignored."""
    vlans = []
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r'^(\d+)\s+(.+?)\s+(active|act/unsup|suspended|shutdown)\b',
                     s, re.IGNORECASE)
        if not m: continue
        vlans.append({"vid": int(m.group(1)), "name": m.group(2).strip(),
                      "status": m.group(3).lower()})
    return vlans

def _expand_vlan_list(spec):
    """Expand "1,10,20-25,100" into a set of vids. Returns None for the
    default all-VLANs range — the caller maps that to mode 'tagged-all'
    instead of an explicit tagged list."""
    s = (spec or "").strip().lower()
    if not s or s in ("all", "1-4094", "1-4096"):
        return None
    vids = set()
    for part in s.split(","):
        part = part.strip()
        if not part: continue
        m = re.match(r'^(\d+)(?:-(\d+))?$', part)
        if not m: continue
        lo = int(m.group(1)); hi = int(m.group(2) or lo)
        vids.update(range(lo, min(hi, 4094) + 1))
    return vids

def _parse_ip_interface_brief(text):
    """Parse `show ip interface brief` -> {interface: ip}; unassigned skipped."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+', s)
        if m:
            out[m.group(1)] = m.group(2)
    return out

def _mac_to_cisco(mac):
    """00:09:0F:09:00:24 -> 0009.0f09.0024 (Cisco CLI form); None if invalid."""
    s = re.sub(r'[^0-9a-fA-F]', '', mac or '').lower()
    if len(s) != 12: return None
    return f"{s[0:4]}.{s[4:8]}.{s[8:12]}"

def _norm_mac(mac):
    """Any common MAC form -> lowercase colon form ('b4:0b:44:12:ab:cd');
    None when the input doesn't hold exactly 12 hex digits."""
    s = re.sub(r'[^0-9a-fA-F]', '', mac or '').lower()
    if len(s) != 12:
        return None
    return ":".join(s[i:i+2] for i in range(0, 12, 2))

def _parse_mac_table_entry(text):
    """Parse `show mac address-table address <mac>` rows -> [{vid, mac, port}].
    MAC normalized to lowercase colon form."""
    rows = []
    for line in text.splitlines():
        m = re.match(r'^\s*(\d+)\s+([0-9a-fA-F.]{14})\s+\S+\s+(\S+)', line)
        if m:
            mac = re.sub(r'[^0-9a-fA-F]', '', m.group(2)).lower()
            rows.append({"vid": int(m.group(1)),
                         "mac": ":".join(mac[i:i+2] for i in range(0, 12, 2)),
                         "port": m.group(3)})
    return rows

def _parse_mac_table(text):
    """Parse full `show mac address-table` output -> [{vid, mac, port}].
    Same row format as the single-address variant; header/footer lines and
    the 'Total Mac Addresses' line never match the row regex."""
    return _parse_mac_table_entry(text)

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

def _parse_interfaces_trunk(text):
    """Parse `show interfaces trunk` into per-port dicts. Tracks the
    sectioned tables: main (mode/native), 'Vlans allowed on trunk',
    'Vlans allowed and active in management domain'."""
    trunks = {}
    section = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        low = s.lower()
        if "vlans allowed on trunk" in low:
            section = "allowed"; continue
        if "vlans allowed and active" in low:
            section = "active"; continue
        if "vlans in spanning tree" in low:
            section = None; continue
        if re.match(r'^Port\s+Mode\s+Encapsulation', s, re.IGNORECASE):
            section = "main"; continue
        m = re.match(r'^(\S+)\s+(on|desirable|auto|trunk|off|nonegotiate)\s+'
                     r'(\S+)\s+(\S+)\s+(\d+)\s*$', s, re.IGNORECASE)
        if m and section in (None, "main"):
            trunks[m.group(1)] = {"port": m.group(1), "mode": m.group(2).lower(),
                                  "native": int(m.group(5)),
                                  "allowed": None, "active": None}
            continue
        m2 = re.match(r'^(\S+)\s+([\d,\-]+)\s*$', s)
        if m2 and section in ("allowed", "active") and m2.group(1) in trunks:
            trunks[m2.group(1)][section] = m2.group(2)
    return list(trunks.values())

def _eth_interface_type(speed, type_str=None):
    """Map interfaces-status speed/type to a NetBox interface type choice.
    Modular (SFP) ports map to the -x- types; unknown/auto -> 'other'."""
    s = (speed or "").lower().replace("a-", "").strip()
    t = (type_str or "").lower()
    sfpish = any(k in t for k in ("sfp", "gbic", "basesx", "baselx",
                                  "basesr", "baselr", "basezx"))
    if s == "100":   return "100base-tx"
    if s == "1000":  return "1000base-x-sfp" if sfpish else "1000base-t"
    if s in ("10g", "10000"):
        return "10gbase-x-sfpp" if sfpish else "10gbase-t"
    if s == "25g":   return "25gbase-x-sfp28"
    if s == "40g":   return "40gbase-x-qsfpp"
    if s == "100g":  return "100gbase-x-qsfp28"
    return "other"

# ── session (netmiko) ────────────────────────────────────────────────────────

class CiscoSwitchSession:
    """Thin netmiko wrapper for Catalyst IOS/IOS-XE. netmiko owns prompt
    detection, paging (`terminal length 0`) and privilege handling."""

    def __init__(self, ip, port=None, timeout=20):
        self.ip = ip
        self.port = port or CISCO_PORT
        self.timeout = timeout
        self.conn = None

    def login(self):
        self.conn = ConnectHandler(
            device_type="cisco_ios",
            host=self.ip, port=self.port,
            username=CISCO_USER, password=CISCO_PASS,
            conn_timeout=self.timeout, auth_timeout=self.timeout,
            banner_timeout=self.timeout,
        )

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

# ── probe + inventory collection ─────────────────────────────────────────────

def probe_cisco_switch(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        # One quick port check is enough for dead IPs (a dead host fails
        # fast and definitively); OFFLINE_THRESHOLD covers transient drops.
        if not is_port_open(ip, CISCO_PORT, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Cisco switch", ip, "unreachable",
                                 f"port {CISCO_PORT} closed or timed out")
            return None
        sess = CiscoSwitchSession(ip)
        try:
            sess.login()
            try:
                info = _parse_show_version(sess.run("show version"))
                if not (info.get("serial") or info.get("model")):
                    raise RuntimeError("show version yielded no serial/model")
                model = (normalize_model(info.get("model"), CISCO_MODEL_MAP)
                         or info.get("model"))
                return {
                    "ip":           ip,
                    "host":         f"{ip}:{CISCO_PORT}",
                    "serial":       info.get("serial"),
                    "model":        model,
                    "hostname":     (info.get("hostname")
                                     or f"cisco-{ip.replace('.', '-')}"),
                    "manufacturer": "Cisco",
                    "firmware":     info.get("ios_version"),
                }
            finally:
                sess.logout()
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Cisco switch", ip, "no data", classify_error(exc))
            return None
    return None

def _inventory_item_from_row(row, add_item):
    """Classify a `show inventory` row into PSU/Fan/SFP/Module and add it."""
    serial = (row.get("sn") or "").strip()
    if _invalid_serial(serial):
        return
    label = f"{row.get('name', '')} {row.get('descr', '')}".lower()
    if "power supply" in label:
        role = "PSU"
    elif "fan" in label:
        role = "Fan"
    elif "sfp" in label or "transceiver" in label or "gbic" in label:
        role = "SFP"
    else:
        role = "Module"
    add_item(
        name=str(row.get("descr") or row.get("name") or "Module")[:64],
        manufacturer="Cisco",
        part_number=row.get("pid") or None,
        serial=serial,
        description=f"Name={row.get('name')} Descr={row.get('descr')} VID={row.get('vid')}",
        role_id=netbox.get_or_create_inventory_role(role),
    )

def cisco_collect_inventory(ip):
    """Full inventory pull: identity, inventory rows, ports, CDP/LLDP neighbors."""
    sess = CiscoSwitchSession(ip)
    sess.login()
    try:
        ver = _parse_show_version(sess.run("show version"))
        inv_rows = _parse_show_inventory(sess.run("show inventory"))
        ports = _parse_interfaces_status(sess.run("show interfaces status"))
        log("INFO", f"  cisco show: {len(inv_rows)} inventory rows, {len(ports)} ports")

        try:
            neighbors = _parse_cdp_detail(sess.run("show cdp neighbors detail"))
            log("INFO", f"  cdp neighbors: {len(neighbors)}")
        except Exception as exc:
            neighbors = []
            log("WARN", f"  show cdp neighbors detail failed: {exc}")
        if not neighbors:
            try:
                neighbors = _parse_lldp_detail(sess.run("show lldp neighbors detail"))
                log("INFO", f"  lldp neighbors: {len(neighbors)}")
            except Exception as exc:
                log("WARN", f"  show lldp neighbors detail failed: {exc}")

        try:
            vlans = _parse_vlan_brief(sess.run("show vlan brief"))
            log("INFO", f"  vlans: {len(vlans)}")
        except Exception as exc:
            vlans = []
            log("WARN", f"  show vlan brief failed: {exc}")
        try:
            trunks = _parse_interfaces_trunk(sess.run("show interfaces trunk"))
            log("INFO", f"  trunks: {len(trunks)}")
        except Exception as exc:
            trunks = []
            log("WARN", f"  show interfaces trunk failed: {exc}")

        try:
            vtp = _parse_vtp_status(sess.run("show vtp status"))
            log("INFO", f"  vtp domain: {vtp.get('domain')}")
        except Exception as exc:
            vtp = {"domain": None, "mode": None}
            log("WARN", f"  show vtp status failed: {exc}")

        try:
            ip_brief = _parse_ip_interface_brief(sess.run("show ip interface brief"))
            log("INFO", f"  ip brief: {len(ip_brief)} addressed interfaces")
        except Exception as exc:
            ip_brief = {}
            log("WARN", f"  show ip interface brief failed: {exc}")

        try:
            mac_table = _parse_mac_table(sess.run("show mac address-table"))
            log("INFO", f"  mac table: {len(mac_table)} entries")
        except Exception as exc:
            mac_table = []
            log("WARN", f"  show mac address-table failed: {exc}")

        inventory = {}
        add_item = _make_add_item(inventory)
        for row in inv_rows:
            _inventory_item_from_row(row, add_item)

        summary = {
            "serial":    ver.get("serial"),
            "model":     (normalize_model(ver.get("model"), CISCO_MODEL_MAP)
                          or ver.get("model")),
            "firmware":  ver.get("ios_version"),
            "hostname":  (ver.get("hostname") or "").strip(),
            "port_count": len(ports),
        }
        return {"summary": summary, "ports": ports,
                "neighbors": neighbors, "inventory": inventory,
                "vlans": vlans, "trunks": trunks, "vtp": vtp,
                "ip_brief": ip_brief, "mac_table": mac_table}
    finally:
        sess.logout()

# ── interfaces ───────────────────────────────────────────────────────────────

def sync_cisco_interfaces(dev_id, ports):
    """Create/update NetBox interfaces per switchport; delete stale ones.
    Description carries status/vlan/duplex/speed + the port's description."""
    api = netbox.get_netbox()
    existing = {}
    for iface in list(api.dcim.interfaces.filter(device_id=dev_id)):
        existing[str(iface.name)] = iface

    seen = set()
    updates, creates = [], []
    for p in ports:
        name = p["port"]
        seen.add(name)
        desc_parts = [f"status={p.get('status')}", f"vlan={p.get('vlan')}",
                      f"duplex={p.get('duplex')}", f"speed={p.get('speed')}"]
        if p.get("name"):
            desc_parts.append(p["name"])
        payload = {
            "device":     dev_id,
            "name":       name,
            "type":       _eth_interface_type(p.get("speed"), p.get("type")),
            "enabled":    p.get("status", "").lower() == "connected",
            "description": " | ".join(desc_parts)[:200],
            "mgmt_only":  False,
        }
        if name in existing:
            updates.append({"id": existing[name].id, **payload})
        else:
            creates.append(payload)
    # Bulk write: one HTTP call per operation regardless of port count
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
                continue   # never delete management interfaces
            desc = getattr(iface, "description", None) or ""
            if desc.startswith("netbox-sync: SVI"):
                continue   # our SVIs are reconciled by the SVI section
            try: iface.delete()
            except Exception: pass

def ensure_svi_interface(dev_id, name, vid_map, mgmt_only=True):
    """Get-or-create an SVI (e.g. Vlan50) as a virtual interface;
    untagged_vlan parsed from the VlanNN name when present in vid_map.
    mgmt_only is True for the management carrier SVI, False otherwise."""
    api = netbox.get_netbox()
    existing = api.dcim.interfaces.get(device_id=dev_id, name=name)
    if existing:
        return existing.id
    payload = {"device": dev_id, "name": name, "type": "virtual",
               "enabled": True, "mgmt_only": mgmt_only,
               "description": "netbox-sync: SVI"}
    m = re.match(r'^Vlan(\d+)$', name)
    if m and int(m.group(1)) in vid_map:
        payload["untagged_vlan"] = vid_map[int(m.group(1))]
        # NetBox requires a mode before untagged_vlan is accepted
        payload["mode"] = "access"
    return api.dcim.interfaces.create(payload).id

# ── broadcast-domain topology (CDP connected components) ─────────────────────

def _norm_sw_name(name):
    """Normalize a switch hostname for graph matching: strip domain suffix,
    casefold."""
    return (name or "").split(".")[0].strip().lower()

def _broadcast_components(names, edges):
    """Union-find connected components over switch names. Edges are
    (name, name) pairs; names not in the node set are ignored."""
    parent = {n: n for n in names}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    comps = {}
    for n in names:
        comps.setdefault(find(n), set()).add(n)
    return list(comps.values())

def _component_key(members, vtp_by_name):
    """Stable group key for a broadcast-domain component: the first
    non-empty VTP domain (hostname-sorted, casefolded), else the first
    sorted hostname."""
    for name in sorted(members):
        d = (vtp_by_name.get(name) or "").strip()
        if d:
            return d.lower()
    return sorted(members)[0]

def _cisco_mac_lookup(ip, cisco_mac):
    """Ask one switch for a specific MAC; return the SET of VLANs it is
    learned in (the MAC may appear in several). Used for FortiGate VLAN
    disambiguation."""
    sess = CiscoSwitchSession(ip)
    try:
        sess.login()
        rows = _parse_mac_table_entry(
            sess.run(f"show mac address-table address {cisco_mac}"))
        return {r["vid"] for r in rows}
    finally:
        sess.logout()

def build_mac_map(collected):
    """Build {mac: (switch_ip, port, vid)} from all switches' MAC tables.

    `collected` is the Cisco pass list of (probe, dev_id, data). Only access
    ports (numeric VLAN in the interfaces-status data) are accepted, and
    ports that carry a CDP/LLDP neighbor are skipped: a camera MAC seen on
    a trunk or port-channel belongs to a downstream switch, which reports
    it on a real access port. CDP/LLDP uses long interface names, MAC tables
    short ones — both sides are normalized through _short_intf. On
    duplicate MACs the first switch in collection order wins."""
    mac_map = {}
    for probe, _dev_id, data in collected:
        uplinks = {_short_intf(n.get("local_intf"))
                   for n in (data.get("neighbors") or [])}
        # Only access ports (numeric VLAN column in `show interfaces status`)
        # may terminate a camera cable: this excludes trunks, routed ports
        # and port-channels — MACs learned over a LAG uplink are reported on
        # the Po interface, which never appears in CDP/LLDP neighbor lists.
        access_ports = {p["port"] for p in (data.get("ports") or [])
                        if (p.get("vlan") or "").strip().isdigit()}
        for row in (data.get("mac_table") or []):
            port = row.get("port")
            if _short_intf(port) in uplinks:
                continue
            if port not in access_ports:
                continue
            mac = row.get("mac")
            if not mac:
                continue
            if mac in mac_map:
                if mac_map[mac][:2] != (probe["ip"], port):
                    log("WARN", f"  mac {mac} seen on {mac_map[mac][0]}:"
                                f"{mac_map[mac][1]} and {probe['ip']}:{port}"
                                " — keeping the first")
                continue
            mac_map[mac] = (probe["ip"], port, row.get("vid"))
    return mac_map

# ── VLANs ────────────────────────────────────────────────────────────────────

# Ownership marker: only VLANs whose description starts with this prefix are
# updated/deleted by the sync. Manual VLANs are never modified.
VLAN_MARKER = "netbox-sync:"

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

def sync_cisco_vlans(group_id, hostname, vlans):
    """Get-or-create each VLAN in IPAM for the VLAN group; refresh
    marker-owned records. Returns {vid: netbox_id} for interface linkage.
    One list fetch per group (no per-VLAN GETs); bulk update."""
    api = netbox.get_netbox()
    by_vid = {v.vid: v for v in api.ipam.vlans.filter(group_id=group_id)}
    vid_map = {}
    update_batch = []
    for v in vlans:
        vid = v["vid"]
        payload = {"vid": vid, "name": v.get("name") or f"VLAN{vid:04d}",
                   "status": "active",
                   "description": f"{VLAN_MARKER} last seen {hostname}"}
        existing = by_vid.get(vid)
        if existing:
            if (existing.description or "").startswith(VLAN_MARKER):
                update_batch.append({"id": existing.id, **payload})
            vid_map[vid] = existing.id
            continue
        try:
            rec = api.ipam.vlans.create({**payload, "group": group_id})
            vid_map[vid] = rec.id
            by_vid[vid] = rec
        except Exception as exc:
            log("WARN", f"  vlan {vid}: create failed on {hostname}: {exc}")
    if update_batch:
        api.ipam.vlans.update(update_batch)
    return vid_map

def sync_interface_vlans(dev_id, ports, trunks, vid_map):
    """Wire VLAN linkage on switch interfaces: access untagged, trunk
    native + tagged (or tagged-all for the default range)."""
    api = netbox.get_netbox()
    by_name = {str(i.name): i
               for i in api.dcim.interfaces.filter(device_id=dev_id)}
    trunk_by_port = {t["port"]: t for t in trunks}
    updates = []
    for p in ports:
        iface = by_name.get(p["port"])
        if not iface: continue
        vlan_col = (p.get("vlan") or "").strip().lower()
        if vlan_col == "routed": continue
        t = trunk_by_port.get(p["port"])
        payload = None
        if t or vlan_col == "trunk":
            payload = {"id": iface.id}
            native = (t or {}).get("native")
            if native in vid_map:
                payload["untagged_vlan"] = vid_map[native]
            expanded = _expand_vlan_list((t or {}).get("active")
                                         or (t or {}).get("allowed"))
            if expanded is None:
                payload["mode"] = "tagged-all"
            else:
                payload["mode"] = "tagged"
                payload["tagged_vlans"] = [vid_map[v] for v in sorted(expanded)
                                           if v in vid_map]
        elif vlan_col.isdigit() and int(vlan_col) in vid_map:
            payload = {"id": iface.id, "mode": "access",
                       "untagged_vlan": vid_map[int(vlan_col)]}
        if payload:
            updates.append(payload)
    if updates:
        api.dcim.interfaces.update(updates)   # one bulk PATCH for all ports

def sweep_stale_vlans(group_id, seen_vids):
    """Delete marker-owned VLANs in the group that no processed switch
    reported this run. Manual (unmarked) VLANs are never touched."""
    api = netbox.get_netbox()
    for vlan in list(api.ipam.vlans.filter(group_id=group_id)):
        if not (vlan.description or "").startswith(VLAN_MARKER):
            continue
        if vlan.vid not in seen_vids:
            try:
                vlan.delete()
                log("INFO", f"  vlan {vlan.vid} (group {group_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete stale vlan {vlan.vid}: {exc}")

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

def _sweep_stale_groups(site_id, fed_group_ids, key_by_name):
    """Migration sweep: delete marker-owned groups at the site that are no
    longer valid — case-variant duplicates of a fed group (e.g. 'Snapp')
    or abandoned per-switch hostname fallbacks whose switch joined a
    component. Manual groups and fed groups are never touched."""
    api = netbox.get_netbox()
    groups = list(api.ipam.vlan_groups.filter(scope_type="dcim.site", scope_id=site_id))
    fed_keys = {g.description[len(VLAN_GROUP_MARKER):].lower()
                for g in groups if g.id in fed_group_ids
                and (g.description or "").startswith(VLAN_GROUP_MARKER)}
    for g in groups:
        desc = g.description or ""
        if not desc.startswith(VLAN_GROUP_MARKER):
            continue
        if g.id in fed_group_ids:
            continue
        key = desc[len(VLAN_GROUP_MARKER):].lower()
        stale = (key in fed_keys) or \
                (key in key_by_name and key_by_name[key] != key)
        if not stale:
            continue
        sweep_stale_vlans(g.id, set())
        if not list(api.ipam.vlans.filter(group_id=g.id)):
            try:
                g.delete()
                log("INFO", f"  stale VLAN group {g.name} (site {site_id}) deleted")
            except Exception as exc:
                log("WARN", f"  could not delete stale group {g.name}: {exc}")

# ── CDP/LLDP cable reconciliation ────────────────────────────────────────────

# Ownership marker: only cables whose description starts with this prefix are
# managed (refreshed/deleted) by the sync. Manual cabling is never touched.
CABLE_MARKER = "netbox-sync:"

# Sub-marker for camera<->switch cables: owned solely by sync_camera_cable —
# the CDP/LLDP reconciler must never sweep, adopt, or create over them.
MAC_TABLE_CABLE_MARKER = f"{CABLE_MARKER} mac-table"

def _cable_iface_ids(cable):
    for t in (getattr(cable, "a_terminations", None) or []) + \
             (getattr(cable, "b_terminations", None) or []):
        # pynetbox returns GenericListObject (attribute access), tests use dicts
        oid = t.get("object_id") if isinstance(t, dict) \
              else getattr(t, "object_id", None)
        if oid is not None:
            yield oid

def sync_cdp_cables(dev_id, neighbors, protocol="cdp"):
    """Reconcile NetBox cables for one device from CDP/LLDP neighbor data.

    Both ends must resolve to existing NetBox interfaces; anything else is
    skipped (DEBUG). Only marker-owned cables are managed. The description
    records the discovery protocol ('cdp' or 'lldp')."""
    api = netbox.get_netbox()
    local_ifaces = {str(i.name): i
                    for i in api.dcim.interfaces.filter(device_id=dev_id)}
    existing_cables = list(api.dcim.cables.filter(device_id=dev_id))
    marked, unmarked = [], []
    for c in existing_cables:
        desc = c.description or ""
        if desc.startswith(MAC_TABLE_CABLE_MARKER):
            unmarked.append(c)   # camera cables: protected, never CDP-managed
        elif desc.startswith(CABLE_MARKER):
            marked.append(c)
        else:
            unmarked.append(c)   # manual cables

    marked_by_iface = {}
    for c in marked:
        for oid in _cable_iface_ids(c):
            marked_by_iface.setdefault(oid, c)

    peer_dev_cache = {}
    seen_cable_ids = set()

    for n in neighbors:
        local = local_ifaces.get(_short_intf(n.get("local_intf")))
        if not local:
            log("DEBUG", f"  cdp: local iface {n.get('local_intf')} not found, skipping")
            continue
        peer_name = _normalize_cdp_id(n.get("device_id"))
        if not peer_name:
            continue
        if peer_name not in peer_dev_cache:
            try:
                peer_dev_cache[peer_name] = api.dcim.devices.get(name=peer_name)
            except Exception:
                peer_dev_cache[peer_name] = None
        peer_dev = peer_dev_cache[peer_name]
        if not peer_dev:
            log("DEBUG", f"  cdp: neighbor {peer_name} not in NetBox, skipping")
            continue
        peer_iface = api.dcim.interfaces.get(
            device_id=peer_dev.id, name=_short_intf(n.get("remote_intf")))
        if not peer_iface:
            log("DEBUG", f"  cdp: iface {n.get('remote_intf')} not found on "
                        f"{peer_name}, skipping")
            continue

        desc = (f"{CABLE_MARKER} {protocol} {local.name} <-> "
                f"{peer_name} {peer_iface.name}")
        existing = marked_by_iface.get(local.id) or marked_by_iface.get(peer_iface.id)
        if existing:
            seen_cable_ids.add(existing.id)
            api.dcim.cables.update([{"id": existing.id, "description": desc}])
            continue
        if any(local.id in _cable_iface_ids(c) or peer_iface.id in _cable_iface_ids(c)
               for c in unmarked):
            log("DEBUG", f"  cdp: manual cable exists on {local.name} or "
                        f"{peer_iface.name}, leaving untouched")
            continue
        try:
            cable = api.dcim.cables.create({
                "a_terminations": [{"object_type": "dcim.interface",
                                    "object_id": local.id}],
                "b_terminations": [{"object_type": "dcim.interface",
                                    "object_id": peer_iface.id}],
                "description": desc,
            })
            seen_cable_ids.add(cable.id)
            log("INFO", f"  cdp: cabled {local.name} <-> {peer_name} {peer_iface.name}")
        except Exception as exc:
            log("WARN", f"  cdp: could not create cable {local.name} "
                        f"<-> {peer_name} {peer_iface.name}: {exc}")

    for c in marked:
        if c.id not in seen_cable_ids:
            try:
                c.delete()
                log("INFO", f"  cdp: removed stale cable id={c.id}")
            except Exception: pass

def sync_camera_cable(cam_dev_id, cam_name, cam_iface_id, mac, mac_map,
                      switch_by_ip):
    """Reconcile one camera<->switch cable from the MAC-table map.

    Keep-on-absence: when the camera MAC is in no switch table this run
    (aged out / idle camera), existing marked cables are left untouched —
    a cable is only moved on positive evidence of a new port. Manual
    (unmarked) cables are never modified or created over."""
    api = netbox.get_netbox()
    mac = _norm_mac(mac)
    if not mac:
        return
    hit = mac_map.get(mac)
    if not hit:
        log("DEBUG", f"  camera {cam_name}: mac {mac} not in any switch "
                     "table — keeping existing cable")
        return
    sw_ip, port, _vid = hit
    sw = (switch_by_ip or {}).get(sw_ip)
    if not sw:
        log("WARN", f"  camera {cam_name}: switch {sw_ip} has no NetBox "
                    "device this run — skipping cable")
        return
    sw_iface = api.dcim.interfaces.get(device_id=sw["dev_id"], name=port)
    if not sw_iface:
        log("WARN", f"  camera {cam_name}: iface {port} not found on "
                    f"{sw['name']} — skipping cable")
        return

    desc = (f"{MAC_TABLE_CABLE_MARKER} {netbox.CAMERA_IFACE_NAME} <-> "
            f"{sw['name']} {port}")
    term_a = [{"object_type": "dcim.interface", "object_id": cam_iface_id}]
    term_b = [{"object_type": "dcim.interface", "object_id": sw_iface.id}]

    cables = list(api.dcim.cables.filter(device_id=cam_dev_id))
    marked = [c for c in cables
              if (c.description or "").startswith(MAC_TABLE_CABLE_MARKER)]
    unmarked = [c for c in cables
                if not (c.description or "").startswith(MAC_TABLE_CABLE_MARKER)]
    mine = next((c for c in marked
                 if cam_iface_id in set(_cable_iface_ids(c))), None)

    if mine:
        try:
            if set(_cable_iface_ids(mine)) == {cam_iface_id, sw_iface.id}:
                api.dcim.cables.update([{"id": mine.id, "description": desc}])
                return
            if any(cam_iface_id in set(_cable_iface_ids(c))
                   or sw_iface.id in set(_cable_iface_ids(c))
                   for c in unmarked):
                log("DEBUG", f"  camera {cam_name}: manual cable blocks the "
                             f"move to {sw['name']} {port}, leaving untouched")
                return
            api.dcim.cables.update([{"id": mine.id,
                                     "a_terminations": term_a,
                                     "b_terminations": term_b,
                                     "description": desc}])
            log("INFO", f"  camera {cam_name}: cable moved to "
                        f"{sw['name']} {port}")
        except Exception as exc:
            log("WARN", f"  camera {cam_name}: cable update failed: {exc}")
        return

    if any(cam_iface_id in set(_cable_iface_ids(c))
           or sw_iface.id in set(_cable_iface_ids(c)) for c in unmarked):
        log("DEBUG", f"  camera {cam_name}: manual cable present on "
                     f"{netbox.CAMERA_IFACE_NAME} or {port}, leaving untouched")
        return
    try:
        api.dcim.cables.create({"a_terminations": term_a,
                                "b_terminations": term_b,
                                "description": desc})
        log("INFO", f"  camera {cam_name}: cabled "
                    f"{netbox.CAMERA_IFACE_NAME} <-> {sw['name']} {port}")
    except Exception as exc:
        log("WARN", f"  camera {cam_name}: cable create failed: {exc}")
