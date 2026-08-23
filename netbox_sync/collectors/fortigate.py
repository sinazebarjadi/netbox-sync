"""FortiGate firewalls: REST API session (identity/interfaces/VLANs) plus
SSH extras (LLDP neighbors, SFP transceivers)."""
import re
import time

import requests
from netmiko import ConnectHandler

from netbox_sync import netbox
from netbox_sync.config import (FORTIGATE_USER, FORTIGATE_PASS, FORTIGATE_PORT,
                                FORTIGATE_SSH_PORT, log)
from netbox_sync.models import FORTIGATE_MODEL_MAP
from netbox_sync.utils import (normalize_model, _invalid_serial,
                                _make_add_item, is_port_open)
from netbox_sync.report import classify_error, record_probe_failure

class FortiGateAuthError(RuntimeError):
    """Session login rejected / not honored — credentials wrong."""

# ── REST API session + mappers ───────────────────────────────────────────────

class FortiGateSession:
    """FortiOS session-based auth: POST /logincheck with admin username and
    secretkey to obtain session cookies (the documented username/password
    alternative to api-user tokens). Re-logs in on 401 (expired session)."""

    def __init__(self, ip, port, timeout=30):
        self.base = f"https://{ip}:{port}"
        self.s = requests.Session()
        self.s.verify = False
        self.timeout = timeout
        self._login()

    def _login(self):
        r = self.s.post(f"{self.base}/logincheck",
                        data={"username": FORTIGATE_USER,
                              "secretkey": FORTIGATE_PASS},
                        timeout=self.timeout)
        # post-login-banner flow (if enabled) requires a disclaimer confirm
        if "logindisclaimer" in r.text:
            self.s.post(f"{self.base}/logindisclaimer",
                        data={"confirm": 1}, timeout=self.timeout)

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        if r.status_code == 401:
            self._login()
            r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
            if r.status_code == 401:
                raise FortiGateAuthError(
                    "FortiGate auth rejected — check FORTIGATE_USER/FORTIGATE_PASS")
        r.raise_for_status()
        return r.json()

def _fg_status(data):
    """Map /monitor/system/status JSON to identity fields. FortiOS puts the
    serial at TOP level and splits the model into model_name/model_number
    (7.2.x); older builds nest serial_number inside results."""
    results = data.get("results") or data
    model_name   = (results.get("model_name") or "").strip()
    model_number = (results.get("model_number") or "").strip()
    if model_name and model_number:
        model = f"{model_name} {model_number}"       # "FortiGate 1800F"
    else:
        model = (results.get("model") or model_number or model_name or None)
    return {
        "hostname": results.get("hostname"),
        "serial":   data.get("serial") or results.get("serial_number"),
        "model":    model,
        "version":  data.get("version") or results.get("version"),
    }

def _fg_speed(m):
    s = str(m.get("speed") or "")
    digits = re.match(r'^(\d+)', s)
    return int(digits.group(1)) if digits else None

def _fg_interfaces(monitor_data, cmdb_data):
    """Merge /monitor/system/interface (link/speed) with /cmdb config.
    Monitor only reports base interfaces (no VLAN subinterfaces on FortiOS
    7.x), so cmdb vlan rows absent from monitor are unioned in."""
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
            "alias": c.get("alias") or "",
            "members": [],
        })
    for c in cfg:
        if not isinstance(c, dict): continue
        if c.get("type") != "vlan" or c.get("name") in mon:
            continue
        ports.append({
            "name": c.get("name"),
            "link": True,   # configured subinterface; monitor has no stats
            "speed_mbps": None,
            "type": "vlan",
            "ip": c.get("ip") or "",
            "vlanid": c.get("vlanid"),
            "parent": c.get("interface") or "",
            "alias": c.get("alias") or "",
            "members": [],
        })
    # Aggregates are not reported by monitor either — add them from cmdb so
    # subinterfaces/members have parents to link to.
    for c in cfg:
        if not isinstance(c, dict): continue
        if c.get("type") != "aggregate" or c.get("name") in mon:
            continue
        ports.append({
            "name": c.get("name"),
            "link": True,
            "speed_mbps": None,
            "type": "lag",
            "ip": c.get("ip") or "",
            "vlanid": None,
            "parent": "",
            "alias": c.get("alias") or "",
            "members": [m.get("interface-name") for m in (c.get("member") or [])
                        if m.get("interface-name")],
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


# ── firewall NAT mappers (VIPs + IP pools) ───────────────────────────────────

def _names(objs):
    return [o.get("name") for o in (objs or []) if isinstance(o, dict) and o.get("name")]

def _fg_firewall_vips(data):
    out = []
    for v in (data.get("results") or []):
        mapped = [m.get("range") for m in (v.get("mappedip") or [])
                  if isinstance(m, dict)]
        out.append({
            "kind": "vip", "name": v.get("name"),
            "extip": v.get("extip"), "extport": v.get("extport"),
            "mappedip": mapped, "mappedport": v.get("mappedport"),
            "protocol": v.get("protocol"),
            "portforward": v.get("portforward"), "status": v.get("status"),
        })
    return out

def _fg_firewall_ippools(data):
    return [{"kind": "pool", "name": p.get("name"), "type": p.get("type"),
             "startip": p.get("startip"), "endip": p.get("endip")}
            for p in (data.get("results") or [])]


def _fg_ha(stats_data, checksums_data, ha_cfg_data):
    """Build the HA picture from /monitor/system/ha-statistics (units),
    /monitor/system/ha-checksums (roles) and /cmdb/system/ha (group/mode)."""
    cfg = ha_cfg_data.get("results") or {}
    primary_serials = {r.get("serial_no")
                       for r in (checksums_data.get("results") or [])
                       if r.get("is_manage_primary") or r.get("is_root_primary")}
    units = []
    primary_hostname = None
    for r in (stats_data.get("results") or []):
        serial = r.get("serial_no")
        is_primary = serial in primary_serials
        units.append({"hostname": r.get("hostname"), "serial": serial,
                      "is_primary": is_primary})
        if is_primary and not primary_hostname:
            primary_hostname = r.get("hostname")
    primary_serial = next(iter(primary_serials), None)
    if primary_serial is None and units:
        primary_serial = units[0]["serial"]
    return {
        "clustered": len(units) > 1,
        "group_name": cfg.get("group-name") or "",
        "mode": cfg.get("mode") or "",
        "primary_serial": primary_serial,
        "primary_hostname": primary_hostname or (units[0]["hostname"] if units else None),
        "units": units,
    }

# ── SSH extras (LLDP + transceivers) ─────────────────────────────────────────

# FortiOS prints command failures INLINE (netmiko sees no exception) —
# detect them instead of silently parsing an error page to zero rows.
_FG_CMD_FAIL = re.compile(r'(Unknown action|Command fail|command parse error)',
                          re.IGNORECASE)

def _ssh_run_or_none(sess, command, label):
    """Run a FortiOS command; return None (with an informative WARN) when the
    command errors or is rejected/unsupported on this device."""
    try:
        out = sess.run(command)
    except Exception as exc:
        log("WARN", f"  {label} failed: {exc}")
        return None
    if _FG_CMD_FAIL.search(out or ""):
        log("WARN", f"  {label} not available on this device (command rejected)")
        return None
    return out

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
        if not s or s.startswith("-") or re.match(r'^Port\s', s):
            continue
        m = re.match(r'^(\S+)\s+([0-9a-fA-F:]{17})\s+(.+?)\s+([A-Z,]+)\s+(\d+)\s+(\S+)$', s)
        if not m: continue
        entries.append({"device_id": m.group(3), "platform": "",
                        "local_intf": m.group(1), "remote_intf": m.group(6),
                        "ip": None})
    return entries

def _parse_ifconfig_a(text):
    """Parse `fnsysctl ifconfig -a` blocks: interface name -> MAC
    (lowercase colon form)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^(.+?)\tLink encap:Ethernet\s+HWaddr\s+([0-9A-Fa-f:]{17})', line)
        if m:
            out[m.group(1).strip()] = m.group(2).lower()
    return out

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
    port = FORTIGATE_PORT
    if not (FORTIGATE_USER and FORTIGATE_PASS):
        log("DEBUG", f"  no FortiGate basic-auth creds configured — skipping {ip}")
        return None
    for attempt in range(1, retries + 1):
        # One quick port check is enough for dead IPs (same reasoning as Cisco)
        if not is_port_open(ip, port, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("FortiGate", ip, "unreachable",
                                 f"port {port} closed or timed out")
            return None
        try:
            status = _fg_status(
                FortiGateSession(ip, port).get("/api/v2/monitor/system/status"))
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
        except FortiGateAuthError:
            log("WARN", f"  FortiGate {ip}: auth rejected — skipping")
            record_probe_failure("FortiGate", ip, "no data", "authentication failure")
            return None
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("FortiGate", ip, "no data", classify_error(exc))
            return None
    return None

def fortigate_collect(ip):
    if not (FORTIGATE_USER and FORTIGATE_PASS):
        raise RuntimeError(f"no FortiGate basic-auth creds configured")
    port = FORTIGATE_PORT
    fg = FortiGateSession(ip, port)
    status = _fg_status(fg.get("/api/v2/monitor/system/status"))
    mon = fg.get("/api/v2/monitor/system/interface")
    cmdb = fg.get("/api/v2/cmdb/system/interface?vdom=root")
    ports = _fg_interfaces(mon, cmdb)
    vlans = _fg_vlans(cmdb)
    log("INFO", f"  fortigate api: {len(ports)} interfaces, {len(vlans)} vlans")

    try:
        ha = _fg_ha(fg.get("/api/v2/monitor/system/ha-statistics"),
                    fg.get("/api/v2/monitor/system/ha-checksums"),
                    fg.get("/api/v2/cmdb/system/ha"))
        if ha["clustered"]:
            log("INFO", f"  ha cluster: {ha['group_name']} ({ha['mode']}), "
                        f"primary={ha['primary_hostname']}")
    except Exception as exc:
        ha = {"clustered": False, "group_name": "", "mode": "",
              "primary_serial": None, "primary_hostname": None, "units": []}
        log("WARN", f"  ha status collection failed: {exc}")

    firewall = {"vips": [], "ippools": []}
    for path, key, mapper in (
            ("/api/v2/cmdb/firewall/vip", "vips", _fg_firewall_vips),
            ("/api/v2/cmdb/firewall/ippool", "ippools", _fg_firewall_ippools)):
        try:
            firewall[key] = mapper(fg.get(f"{path}?vdom=root"))
        except Exception as exc:
            log("WARN", f"  firewall {key} collection failed: {exc}")
    log("INFO", f"  firewall: {len(firewall['vips'])} vips, "
                f"{len(firewall['ippools'])} pools")

    neighbors = []
    inventory = {}
    sess = FortiGateSSHSession(ip)
    try:
        sess.login()
        lldp_out = _ssh_run_or_none(sess, "diagnose lldp neighbor-summary", "lldp")
        if lldp_out is not None:
            neighbors = _parse_lldp_summary(lldp_out)
            log("INFO", f"  lldp neighbors: {len(neighbors)}")
        sfp_out = _ssh_run_or_none(sess, "diagnose sys transceiver list", "transceivers")
        if sfp_out is not None:
            add = _make_add_item(inventory)
            for row in _parse_transceivers(sfp_out):
                serial = row.get("serial_number")
                if _invalid_serial(serial): continue
                add(name=f"SFP Port {row.get('port')}",
                    manufacturer=row.get("vendor") or "Unknown",
                    part_number=row.get("part_number"), serial=serial,
                    description=f"Port={row.get('port')}",
                    role_id=netbox.get_or_create_inventory_role("SFP", "4caf50"))
            log("INFO", f"  transceivers: {len(inventory)}")
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
            "neighbors": neighbors, "inventory": inventory, "ha": ha,
            "firewall": firewall}

def _fortigate_iface_mac(ip, iface_name):
    """Fetch ONE interface's MAC via fnsysctl ifconfig "<name>".
    Deliberately per-interface: `ifconfig -a` pages long output in the
    fnsysctl context and hangs netmiko — the single-interface form is
    short and reliable. Only called during overlap disambiguation."""
    sess = FortiGateSSHSession(ip)
    try:
        sess.login()
        out = _ssh_run_or_none(sess, f'fnsysctl ifconfig "{iface_name}"',
                                 "ifconfig")
        if out is None:
            return None
        macs = _parse_ifconfig_a(out)
        return next(iter(macs.values()), None)
    finally:
        sess.logout()

# ── VLAN resolution (match against switch VLANs) ─────────────────────────────

def resolve_fortigate_vlans(site_vlan_index, vlans, get_mac, mac_lookup):
    """Match FortiGate VLANs to existing switch VLANs.
    unique -> reuse; none -> missing (create per-device); overlap ->
    get_mac(vid) lazily (single-interface ifconfig), then
    mac_lookup(vid, mac) -> group_id (else missing)."""
    vid_map, missing = {}, []
    for v in vlans:
        vid = v["vid"]
        matches = site_vlan_index.get(vid, [])
        if len(matches) == 1:
            vid_map[vid] = matches[0][1]
        elif not matches:
            missing.append(v)
        else:
            mac = get_mac(vid) if get_mac else None
            gid = mac_lookup(vid, mac) if mac else None
            if gid:
                vid_map[vid] = next(vlan_id for g, vlan_id in matches if g == gid)
            else:
                missing.append(v)
    return vid_map, missing

# ── interfaces ───────────────────────────────────────────────────────────────

def sync_fortigate_interfaces(dev_id, ports, vid_map):
    """Two-pass bulk sync: LAG interfaces first (children must exist before
    they are referenced), then physical ports (with `lag` links) and VLAN
    subinterfaces (with `parent` links)."""
    api = netbox.get_netbox()
    existing = {str(i.name): i
                for i in api.dcim.interfaces.filter(device_id=dev_id)}
    seen = set()

    # Pass 1: LAG interfaces
    lag_updates, lag_creates = [], []
    for p in ports:
        if p.get("type") != "lag":
            continue
        name = p["name"]
        seen.add(name)
        payload = {"device": dev_id, "name": name, "type": "lag",
                   "enabled": p.get("link", False),
                   "description": f"type=aggregate ip={p.get('ip')}"[:200],
                   "mgmt_only": False}
        if p.get("alias"):
            payload["label"] = str(p["alias"])[:64]
        if name in existing:
            lag_updates.append({"id": existing[name].id, **payload})
        else:
            lag_creates.append(payload)
    if lag_updates:
        api.dcim.interfaces.update(lag_updates)
    name_to_id = {name: iface.id for name, iface in existing.items()}
    if lag_creates:
        try:
            recs = api.dcim.interfaces.create(lag_creates)
            if hasattr(recs, "id"):      # single Record -> wrap
                recs = [recs]
            for r in recs:
                name_to_id[str(r.name)] = r.id
        except Exception as e:
            log("WARN", f"  Could not create LAG interfaces: {e}")

    # Pass 2: physical ports (lag membership) + VLAN subinterfaces (parent)
    lag_of_member = {}
    for p in ports:
        if p.get("type") == "lag":
            for m in p.get("members", []):
                lag_of_member[m] = p["name"]
    updates, creates = [], []
    for p in ports:
        if p.get("type") == "lag":
            continue
        name = p["name"]
        seen.add(name)
        if p.get("type") == "vlan" and p.get("vlanid") is not None:
            payload = {"device": dev_id, "name": name, "type": "virtual",
                       "enabled": p.get("link", False),
                       "description": f"vlanid={p['vlanid']} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
            parent_id = name_to_id.get(p.get("parent") or "")
            if parent_id:
                payload["parent"] = parent_id
            if p["vlanid"] in vid_map:
                payload["mode"] = "tagged"
                payload["untagged_vlan"] = vid_map[p["vlanid"]]
        else:
            payload = {"device": dev_id, "name": name,
                       "type": _fg_interface_type(p.get("speed_mbps")),
                       "enabled": bool(p.get("link")),
                       "description": f"type={p.get('type')} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
            lag_name = lag_of_member.get(name)
            if lag_name and name_to_id.get(lag_name):
                payload["lag"] = name_to_id[lag_name]
        if p.get("alias"):
            payload["label"] = str(p["alias"])[:64]
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
                continue   # never delete management interfaces
            try: iface.delete()
            except Exception: pass
