"""Brocade / HPE B-Series SAN switches: SSH CLI session, Fabric OS output
parsers, probing, inventory collection and FC interface sync."""
import re
import time

import paramiko

from netbox_sync import netbox
from netbox_sync.config import (SWITCH_USER, SWITCH_PASS, SWITCH_PORT,
                                _env_bool, log)
from netbox_sync.models import SWITCH_MODEL_MAP
from netbox_sync.netbox import get_or_create_inventory_role
from netbox_sync.utils import (normalize_model, _invalid_serial,
                                _make_add_item, is_port_open)
from netbox_sync.report import classify_error, record_probe_failure


class BrocadeSwitchSession:
    """Thin SSH wrapper that runs Brocade Fabric OS CLI commands and returns
    raw text output. Works on HPE B-Series (Brocade OEM) firmware.

    Uses exec_command per call rather than an interactive shell — Fabric OS
    only allows one exec channel at a time, so calls are serialized."""

    def __init__(self, ip, port=22, timeout=20):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.client = None

    def login(self):
        self.client = paramiko.SSHClient()
        if _env_bool("SWITCH_STRICT_HOST_KEY", False):
            # Verify switch host keys against the system known_hosts
            self.client.load_system_host_keys()
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.ip, port=self.port,
            username=SWITCH_USER, password=SWITCH_PASS,
            timeout=self.timeout, allow_agent=False, look_for_keys=False,
        )
        # Verify transport is open
        if not self.client.get_transport() or not self.client.get_transport().is_active():
            raise RuntimeError(f"SSH transport not active for {self.ip}")

    def logout(self):
        try:
            if self.client:
                self.client.close()
        except Exception: pass
        self.client = None

    def run(self, command):
        """Run a CLI command via exec_command and return its stdout text.
        Brocade Fabric OS does not echo the command on exec stdout; output is
        just the command result. A trailing prompt line (if any) is stripped."""
        if not self.client:
            raise RuntimeError("SSH client not open")
        # Fabric OS rejects parallel exec channels; retry briefly if busy.
        last_err = None
        for _ in range(5):
            try:
                stdin, stdout, stderr = self.client.exec_command(
                    command, timeout=self.timeout)
                out = stdout.read().decode(errors="ignore")
                # Drain stderr to keep channel clean
                try: stderr.read()
                except Exception: pass
                return self._strip_prompt(out, command)
            except paramiko.SSHException as exc:
                last_err = exc
                time.sleep(0.5)
            except Exception as exc:
                last_err = exc
                break
        raise RuntimeError(f"exec_command '{command}' failed on {self.ip}: {last_err}")

    @staticmethod
    def _strip_prompt(text, command):
        lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            # Drop echoed command line (some firmware echoes it on exec too)
            if s == command.strip():
                continue
            # Drop trailing prompt: "admin@switch:>" or "switch>"
            if re.match(r'^[\w.-]+@[\w.-]+[>:]+\s*$', s) or re.match(r'^[\w.-]+[>:]\s*$', s):
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

# ── CLI output parsers ───────────────────────────────────────────────────────

def _parse_switchshow(text):
    """Parse `switchshow` output into key/value headers + port rows."""
    headers = {}
    ports = []
    in_ports = False
    for line in text.splitlines():
        s = line.rstrip()
        if not s: continue
        # Header: "Index Port Address Media Speed State Proto [Comment]"
        if re.match(r'^\s*Index\s+Port\s+Address\s+Media\s+Speed\s+State\s+Proto(\s+Comment)?\s*$', s, re.IGNORECASE):
            in_ports = True
            continue
        # Skip the "=" separator line that follows the header
        if in_ports and re.match(r'^=+\s*$', s):
            continue
        if in_ports:
            # Brocade port line:
            #   " 0  0  010000  id  N16  Online  FC  F-Port  51:40:2e:c0:18:1b:52:20"
            #   " 5  5  010500  id  N16  No_Light  FC  (Ports on Demand ...)"
            m = re.match(
                r'^\s*(\d+)\s+(\d+)\s+([0-9a-fA-F]+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$',
                s,
            )
            if m:
                ports.append({
                    "index":     int(m.group(1)),
                    "port":      int(m.group(2)),
                    "address":   m.group(3),
                    "media":     m.group(4),
                    "speed":     m.group(5),
                    "state":     m.group(6),
                    "proto":     m.group(7) or "",
                    "comment":   (m.group(8) or "").strip(),
                })
                continue
        # Header lines: "key: value" (before the port table)
        m = re.match(r'^([A-Za-z][\w \-/]+?):\s+(.+)$', s)
        if m and not in_ports:
            key = m.group(1).strip().lower().replace(" ", "_")
            headers[key] = m.group(2).strip()
    return headers, ports

def _parse_version(text):
    """Parse `version` / `firmwareshow` output."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', line.strip())
        if m:
            out[m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
    return out

def _parse_nsshow(text):
    """Parse `nsshow` output. Returns list of dicts per logged-in device."""
    entries = []
    cur = {}
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        # Each entry begins with a line containing "Port Id:" or "Port Name:"
        if re.match(r'^Port\s+(Id|Name|World Wide Node Name|World Wide Port Name)\s*:', s, re.IGNORECASE):
            if cur:
                entries.append(cur); cur = {}
        m = re.match(r'^([A-Za-z][\w ]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur:
        entries.append(cur)
    return entries

def _parse_sfpshow(text):
    """Parse `sfpshow` output. Returns list of dicts per port SFP.

    Supports two formats emitted by Fabric OS:
      1. Compact one-line-per-port (older/common firmware):
         "Port  0: id (sw) Vendor: BROCADE  Serial No: HAA...  Speed: 4,8,16_Gbps"
      2. Detailed multi-line key:value blocks (newer firmware):
         "Port  0:\\n  Identifier: ...\\n  Vendor: ..."
    """
    rows = []
    lines = text.splitlines()

    # Detect compact format: a "Port <n>: <data>" line with content after
    # the colon. Detailed-format blocks have bare "Port <n>:" headers (no
    # trailing content), so they must not be treated as compact.
    compact = any(re.match(r'^\s*Port\s+\d+\s*:\s*\S', ln) for ln in lines)

    if compact:
        for ln in lines:
            s = ln.strip()
            if not s: continue
            m = re.match(r'^\s*Port\s+(\d+)\s*:\s*(.*)$', s)
            if not m: continue
            port = int(m.group(1))
            rest = m.group(2)
            row = {"port": port}
            # Pull "Vendor: X", "Serial No: Y", "Speed: Z" out of the rest.
            # Each value extends until the next known key or end of string.
            keys = ("Vendor", "Serial No", "Speed", "Part Number", "Part No")
            for key in keys:
                mm = re.search(
                    rf'{re.escape(key)}\s*:\s*(.+?)(?=\s+(?:{"|".join(re.escape(k) for k in keys)})\s*:|$)',
                    rest,
                )
                if mm:
                    k = key.lower().replace(" ", "_")
                    row[k] = mm.group(1).strip()
            rows.append(row)
        return rows

    # Detailed multi-line format
    cur = {}
    for line in lines:
        s = line.strip()
        if not s: continue
        # New block boundary: a "Port N:" line or an "Identifier:" line
        if re.match(r'^\s*Port\s+\d+\s*:', s, re.IGNORECASE) or re.match(r'^Identifier\s*:', s, re.IGNORECASE):
            if cur:
                rows.append(cur); cur = {}
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur:
        rows.append(cur)
    return rows

def _wwn_normalize(wwn):
    """Normalize a WWN to colon-separated lowercase form."""
    if not wwn: return None
    s = re.sub(r'[^0-9a-fA-F]', '', str(wwn)).lower()
    if len(s) != 16: return None
    return ":".join(s[i:i+2] for i in range(0, 16, 2))

def _parse_chassisshow(text):
    """Parse `chassisshow` output for the real chassis serial + model.

    Brocade `chassisshow` returns key/value pairs like:
        Chassis PID: BES-6510
        Chassis Serial No: XXXXXXXXX
        ...
    Returns a dict with lowercased-underscore keys."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            out[key] = m.group(2).strip()
    return out

# ── probe + inventory collection ─────────────────────────────────────────────

def probe_san_switch(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, SWITCH_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("SAN switch", ip, "unreachable",
                                 f"port {SWITCH_PORT} closed or timed out")
            return None
        sess = BrocadeSwitchSession(ip, SWITCH_PORT)
        try:
            sess.login()
            sw = sess.run("switchshow")
            if not sw:
                if attempt < retries: time.sleep(retry_delay); continue
                record_probe_failure("SAN switch", ip, "no data",
                                     "switchshow returned no data")
                return None
            headers, _ = _parse_switchshow(sw)
            ver = sess.run("version")
            ver_map = _parse_version(ver) if ver else {}
            # chassisshow gives the real supplier serial + model (PID)
            chs = sess.run("chassisshow")
            chs_map = _parse_chassisshow(chs) if chs else {}
            model = (chs_map.get("supplier_part_num")
                      or chs_map.get("chassis_pid")
                      or headers.get("switchtype") or headers.get("switch_type")
                      or headers.get("model") or headers.get("product"))
            serial = (chs_map.get("serial_num")
                       or chs_map.get("factory_serial_num")
                       or chs_map.get("chassis_serial_number")
                       or chs_map.get("chassis_serial_no")
                       or chs_map.get("serial_number")
                       or headers.get("switchwwn") or headers.get("switch_wwn"))
            wwn = _wwn_normalize(headers.get("switchwwn") or headers.get("switch_wwn"))
            name = headers.get("switch_name") or headers.get("switchname") or f"san-{ip.replace('.', '-')}"
            fw = (ver_map.get("fabric_os") or ver_map.get("kernel") or
                  ver_map.get("firmware") or ver_map.get("version"))
            return {
                "ip":           ip,
                "host":         f"{ip}:{SWITCH_PORT}",
                "serial":       serial,
                "model":        model,
                "hostname":     name.strip(),
                "manufacturer": "Brocade",
                "wwn":          wwn,
                "firmware":     fw,
            }
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("SAN switch", ip, "no data", classify_error(exc))
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None

def san_collect_inventory(ip):
    """Full inventory pull for a SAN switch: identity, ports, nameserver, SFPs."""
    sess = BrocadeSwitchSession(ip, SWITCH_PORT)
    sess.login()
    try:
        sw_text = sess.run("switchshow")
        headers, ports = _parse_switchshow(sw_text)
        log("INFO", f"  switchshow: {len(sw_text)} bytes, {len(headers)} headers, {len(ports)} ports")
        ver_map = _parse_version(sess.run("version") or "")
        # chassisshow gives the real supplier serial + model (PID)
        try:
            chs_text = sess.run("chassisshow") or ""
            chs_map = _parse_chassisshow(chs_text) if chs_text else {}
            log("INFO", f"  chassisshow: {len(chs_text)} bytes, {len(chs_map)} fields")
        except Exception as exc:
            chs_map = {}
            log("WARN", f"  chassisshow failed: {exc}")
        # nameserver: combine nsshow + nscamshow to catch all logged-in devices
        ns_entries = []
        for cmd in ("nsshow", "nscamshow"):
            try:
                ns_text = sess.run(cmd) or ""
                ns_count = len(_parse_nsshow(ns_text))
                log("INFO", f"  {cmd}: {len(ns_text)} bytes, {ns_count} entries")
                ns_entries.extend(_parse_nsshow(ns_text))
            except Exception as exc:
                log("WARN", f"  {cmd} failed: {exc}")
        try:
            sfp_text = sess.run("sfpshow") or ""
            sfp_rows = _parse_sfpshow(sfp_text)
            log("INFO", f"  sfpshow: {len(sfp_text)} bytes, {len(sfp_rows)} SFPs")
        except Exception as exc:
            sfp_rows = []
            log("WARN", f"  sfpshow failed: {exc}")

        # Prefer chassisshow supplier (OEM) fields for real serial + model; fall back to switchshow
        chs_serial = (chs_map.get("serial_num")
                      or chs_map.get("factory_serial_num")
                      or chs_map.get("chassis_serial_number")
                      or chs_map.get("chassis_serial_no")
                      or chs_map.get("serial_number"))
        chs_model = (chs_map.get("supplier_part_num")
                     or chs_map.get("chassis_pid")
                     or chs_map.get("pid")
                     or chs_map.get("chassis_product_id"))
        sw_model_raw = (headers.get("switchtype") or headers.get("switch_type")
                        or headers.get("model"))
        summary = {
            "serial":    (chs_serial
                          or headers.get("switchwwn") or headers.get("switch_wwn")
                          or headers.get("serial_number")),
            "wwn":       _wwn_normalize(headers.get("switchwwn") or headers.get("switch_wwn")),
            "model":     (chs_model
                          or normalize_model(sw_model_raw, SWITCH_MODEL_MAP)
                          or sw_model_raw),
            "firmware":  (ver_map.get("fabric_os") or ver_map.get("kernel") or
                          ver_map.get("firmware") or ver_map.get("version")),
            "hostname":  (headers.get("switch_name") or headers.get("switchname") or "").strip(),
            "port_count": len(ports),
        }

        # Build inventory items: SFPs and FC port modules
        inventory = {}
        add_item = _make_add_item(inventory)

        # Map SFP rows to ports by index when possible (sfpshow is ordered)
        for idx, sfp in enumerate(sfp_rows):
            sfp_serial = (sfp.get("vendor_serial_number") or sfp.get("serial_no")
                          or sfp.get("serial_number"))
            if not _invalid_serial(sfp_serial):
                port_num = sfp.get("port", idx)
                add_item(
                    name=f"SFP Port {port_num}",
                    manufacturer=(sfp.get("vendor_name") or sfp.get("vendor")
                                  or "Brocade"),
                    part_number=(sfp.get("vendor_part_number")
                                 or sfp.get("part_number") or sfp.get("part_no")),
                    serial=sfp_serial,
                    description=(f"Port={port_num} Type={sfp.get('identifier') or 'SFP'} "
                                 f"Speed={sfp.get('speed') or sfp.get('speed_capability')} "
                                 f"Temp={sfp.get('temperature')} "
                                 f"VendorPN={sfp.get('vendor_part_number') or sfp.get('part_no')}"),
                    role_id=get_or_create_inventory_role("SFP", "4caf50"),
                )

        return {
            "summary":    summary,
            "ports":      ports,
            "nameserver": ns_entries,
            "sfp":        sfp_rows,
            "inventory":  inventory,
        }
    finally:
        sess.logout()

# ── FC interfaces ────────────────────────────────────────────────────────────

# Brocade speed token -> NetBox interface type choice string.
# Matched on the numeric part so "N16"/"16G"/"16" all map the same way.
_FC_SPEED_TYPES = {
    1:   "1gfc-sfp",
    2:   "2gfc-sfp",
    4:   "4gfc-sfp",
    8:   "8gfc-sfpp",
    16:  "16gfc-sfpp",
    32:  "32gfc-sfp28",
    64:  "64gfc-qsfpp",
    128: "128gfc-qsfp28",
}

def _fc_interface_type(speed):
    """Map a Brocade port speed string to a NetBox interface type choice.

    NetBox interface `type` is a choice string (not an ID), e.g.
    '8gfc-sfpp', '16gfc-sfpp', '32gfc-sfp28', 'other'. Returns 'other'
    if the speed is unknown or the port is offline."""
    s = (speed or "").lower().strip()
    m = re.match(r'^n?(\d+)', s)   # "N16" -> 16, "16G" -> 16, "16" -> 16
    if not m:
        return "other"
    return _FC_SPEED_TYPES.get(int(m.group(1)), "other")

def sync_san_interfaces(dev_id, ports, nameserver):
    """Create/update NetBox interfaces for each FC port on the switch.
    Connected device WWN (from nameserver) is stored in interface description."""
    api = netbox.get_netbox()
    existing = {}
    for iface in list(api.dcim.interfaces.filter(device_id=dev_id)):
        existing[str(iface.name)] = iface

    # Map port index -> logged-in WWNs (from nameserver entries that include port id)
    port_wwns = {}
    for ns in nameserver:
        # Port Id typically like "010c00" -> first 2 hex digits = switch port index (octant)
        pid = (ns.get("port_id") or "").lower()
        if len(pid) >= 2:
            try: idx = int(pid[:2], 16)
            except Exception: continue
            wwn = _wwn_normalize(ns.get("port_world_wide_name") or ns.get("world_wide_port_name"))
            if wwn: port_wwns.setdefault(idx, []).append(wwn)

    seen = set()
    updates, creates = [], []
    for p in ports:
        name = f"FC {p['port']}"
        seen.add(name)
        desc_parts = [f"speed={p.get('speed')}", f"state={p.get('state')}"]
        wwns = port_wwns.get(p["index"]) or []
        if wwns:
            desc_parts.append("WWNs=" + ",".join(wwns))
        elif p.get("comment"):
            desc_parts.append(p["comment"])
        payload = {
            "device":     dev_id,
            "name":       name,
            "type":       _fc_interface_type(p.get("speed")),
            "enabled":    p.get("state", "").lower() == "online",
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

    # Remove interfaces that no longer exist on the switch
    for name, iface in existing.items():
        if name not in seen:
            if getattr(iface, "mgmt_only", False):
                continue   # never delete management interfaces
            try: iface.delete()
            except Exception: pass
