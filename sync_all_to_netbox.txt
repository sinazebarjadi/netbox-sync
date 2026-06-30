#!/usr/bin/env python3
"""
sync_all_to_netbox.py
Merged automation: scans IP ranges for iLO/Redfish BMCs (servers) AND
HPE storage arrays, auto-creates/updates/removes devices and
inventory in NetBox. Runs at 00:00 and 12:00 daily.
"""
import hashlib
import os
import re
import time
import socket
import ipaddress
import urllib3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET

import requests
import pynetbox
import schedule
from dotenv import load_dotenv

from models import SERVER_MODEL_MAP, STORAGE_MODEL_MAP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# credentials
# ═══════════════════════════════════════════════════════════════════════════════
NETBOX_URL   = os.getenv("NETBOX_URL")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")
REDFISH_USER = os.getenv("REDFISH_USER")
REDFISH_PASS = os.getenv("REDFISH_PASS")

STORAGE_USER = os.getenv("STORAGE_USER")
STORAGE_PASS = os.getenv("STORAGE_PASS")

if not NETBOX_URL or not NETBOX_TOKEN:
    raise RuntimeError("NETBOX_URL/NETBOX_TOKEN missing in .env")
if not REDFISH_USER or not REDFISH_PASS:
    raise RuntimeError("REDFISH_USER/REDFISH_PASS missing in .env")
if not STORAGE_USER or not STORAGE_PASS:
    raise RuntimeError("STORAGE_USER/STORAGE_PASS missing in .env")

nb = None

def get_netbox():
    global nb
    if nb is not None:
        return nb
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    nb.http_session.verify = False
    return nb

# ═══════════════════════════════════════════════════════════════════════════════
# config – ranges
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULT_BMC_RANGES = [
    "192.0.2.0/27",
    "198.51.100.0/27",
]
BMC_RANGES = DEFAULT_BMC_RANGES
if os.getenv("BMC_RANGES"):
    BMC_RANGES = [r.strip() for r in os.getenv("BMC_RANGES").split(",") if r.strip()]

DEFAULT_STORAGE_RANGES = [
    "192.0.2.16/32",
    "198.51.100.16/32",
]
STORAGE_RANGES = DEFAULT_STORAGE_RANGES
if os.getenv("STORAGE_RANGES"):
    STORAGE_RANGES = [r.strip() for r in os.getenv("STORAGE_RANGES").split(",") if r.strip()]

REDFISH_PORT  = int(os.getenv("REDFISH_PORT", "443"))
STORAGE_PORT  = int(os.getenv("STORAGE_PORT", "443"))
STORAGE_AUTH_HASH = os.getenv("STORAGE_AUTH_HASH", "sha256").lower()
SCAN_WORKERS  = int(os.getenv("SCAN_WORKERS", "20"))
SERVER_ROLE   = os.getenv("DEFAULT_ROLE_NAME", "Server")
STORAGE_ROLE  = os.getenv("DEFAULT_STORAGE_ROLE", "Storage")
DEFAULT_MFR   = "HPE"
DEFAULT_SITE  = os.getenv("DEFAULT_SITE_NAME", "")

# ═══════════════════════════════════════════════════════════════════════════════
# inventory item role IDs (from NetBox)
# ═══════════════════════════════════════════════════════════════════════════════
ROLE_HDD        = 1
ROLE_SSD        = 2
ROLE_CPU        = 3
ROLE_MEMORY     = 4
ROLE_NIC        = 5
ROLE_PSU        = 6
ROLE_CONTROLLER = 7
ROLE_HBA        = 8
ROLE_BATTERY    = 9
ROLE_SAS_EXP    = 10

# ═══════════════════════════════════════════════════════════════════════════════
# site keyword mapping
# ═══════════════════════════════════════════════════════════════════════════════
# Example mapping — replace with your own site keywords, or set SITE_KEYWORD_MAP
# in your .env as "keyword1:Site1,keyword2:Site2"
SITE_KEYWORD_MAP = [
    ("site1", "Site1"),
    ("site2", "Site2"),
]
if os.getenv("SITE_KEYWORD_MAP"):
    SITE_KEYWORD_MAP = [
        tuple(pair.split(":", 1))
        for pair in os.getenv("SITE_KEYWORD_MAP").split(",")
        if ":" in pair
    ]
SITE_UNKNOWN = DEFAULT_SITE or "Unknown"

# ═══════════════════════════════════════════════════════════════════════════════
# model name normalization — see models.py (SERVER_MODEL_MAP / STORAGE_MODEL_MAP)
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════════
def log(level, msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")

def slugify(s):
    return re.sub(r'[^a-z0-9-]', '-', s.lower().strip())[:50].strip('-')

def normalize_model(model, model_map):
    if not model: return None
    return model_map.get(model.strip().lower(), model.strip())

def resolve_site_from_name(server_name):
    name_lower = (server_name or "").lower()
    for keyword, site in SITE_KEYWORD_MAP:
        if keyword in name_lower:
            return site
    return SITE_UNKNOWN

def gib_from_bytes(v):
    try: return int(round(int(v) / (1024**3)))
    except Exception: return None

def _to_int(v):
    try: return int(v)
    except Exception:
        try: return int(float(v))
        except Exception: return None

def _capacity_to_bytes(obj):
    if not isinstance(obj, dict): return None
    if obj.get("CapacityBytes") is not None:
        try: return int(obj["CapacityBytes"])
        except Exception: pass
    for k, mult in [("CapacityGiB", 1024**3), ("CapacityMiB", 1024**2),
                    ("CapacityGB",  1000**3), ("CapacityMB",  1000**2)]:
        if obj.get(k) is not None:
            try: return int(float(obj[k]) * mult)
            except Exception: pass
    return None

def _pick(d, keys):
    if not isinstance(d, dict): return None
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip(): return v
    return None

def _invalid_serial(serial):
    s = str(serial or "").strip()
    return not s or s.upper() in ("N/A", "NOT AVAILABLE", "UNKNOWN", "NONE", "0", "")

def _add_inventory_item(inventory, name, manufacturer, part_number, serial, description, role_id=None):
    serial = str(serial or "").strip()
    if _invalid_serial(serial): return
    if serial in inventory: return
    inventory[serial] = {
        "name":         str(name or "Unknown").strip()[:64],
        "manufacturer": str(manufacturer or "").strip() or None,
        "part_number":  str(part_number or "").strip() or None,
        "serial":       serial,
        "description":  str(description or "").strip()[:200],
        "role":         role_id,
    }

def _make_add_item(inventory):
    def add_item(name, manufacturer, part_number, serial, description, role_id=None):
        _add_inventory_item(inventory, name, manufacturer, part_number, serial, description, role_id)
    return add_item

def _get_location(obj):
    if not isinstance(obj, dict): return None
    pl = obj.get("PhysicalLocation") or {}
    sl = (pl.get("PartLocation") or {}).get("ServiceLabel") if isinstance(pl, dict) else None
    if sl: return sl
    loc = obj.get("Location")
    if isinstance(loc, str): return loc
    if isinstance(loc, dict): return loc.get("Info") or loc.get("ServiceLabel")
    for k in ["Bay", "Slot", "Position", "Id", "Name"]:
        v = obj.get(k)
        if v:
            if isinstance(v, str): return v
            if isinstance(v, dict): return v.get("ServiceLabel") or v.get("Info")
    return None

def _get_oem(sys_data):
    if not isinstance(sys_data, dict): return {}
    oem = sys_data.get("Oem") or {}
    if isinstance(oem, dict): return oem.get("Hpe") or oem.get("Hp") or {}
    return {}

def _chassis_url(sys):
    cl = sys.get("Links", {}).get("Chassis") if isinstance(sys.get("Links"), dict) else None
    if isinstance(cl, list) and cl: return cl[0].get("@odata.id") if isinstance(cl[0], dict) else None
    if isinstance(cl, dict): return cl.get("@odata.id")
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# smart item naming (server)
# ═══════════════════════════════════════════════════════════════════════════════
_STANDARD_SIZES_GB = [
    1, 2, 4, 8, 16, 32, 64,
    120, 128, 160, 200, 240, 250, 300, 320, 400, 480, 500, 512,
    600, 800, 900, 960, 1000, 1200, 1600, 1800, 1920, 2000, 2400,
    3000, 3200, 3840, 4000, 6000, 7680, 8000, 10000, 12000,
    14000, 15000, 16000, 18000, 20000, 24000,
]

def _snap_to_standard(val_gb):
    for std in _STANDARD_SIZES_GB:
        if abs(val_gb - std) / std <= 0.05:
            return std
    return None

def _bytes_to_human(b):
    if not b: return None
    for unit, div in [("TB", 1e12), ("GB", 1e9), ("MB", 1e6)]:
        val = b / div
        if val >= 1:
            if unit == "GB":
                snapped = _snap_to_standard(val)
                if snapped: return f"{snapped}GB"
            if unit == "TB":
                val_gb = b / 1e9
                snapped = _snap_to_standard(val_gb)
                if snapped and snapped >= 1000:
                    tb = snapped / 1000
                    if tb == int(tb): return f"{int(tb)}TB"
                    s = f"{tb:.1f}".rstrip('0').rstrip('.')
                    return f"{s}TB"
            if val == int(val): return f"{int(val)}{unit}"
            s = f"{val:.1f}".rstrip('0').rstrip('.')
            return f"{s}{unit}"
    return f"{b}B"

def _mib_to_human(mib):
    if not mib: return None
    gib = mib / 1024
    if gib >= 1: return f"{int(round(gib))}GB"
    return f"{mib}MB"

def name_cpu(p):
    model = p.get("Model") or ""
    short = re.sub(r'^(Intel|AMD)\s+', '', model).strip()
    return short[:64] if short else "CPU"

def name_ram(mm):
    cap_mib  = _to_int(mm.get("CapacityMiB"))
    speed    = _to_int(mm.get("OperatingSpeedMhz") or mm.get("OperatingSpeedMHz"))
    mem_type = mm.get("MemoryDeviceType") or mm.get("MemoryType") or "RAM"
    mem_type = re.sub(r'\s+SDRAM.*', '', str(mem_type)).strip()
    cap_str  = _mib_to_human(cap_mib) if cap_mib else None
    if cap_str and speed:  return f"RAM {cap_str} {speed}"
    if cap_str:            return f"RAM {cap_str}"
    return "RAM"

def name_disk(drv):
    cap_b     = _capacity_to_bytes(drv)
    media     = (drv.get("MediaType") or "").upper()
    protocol  = (drv.get("Protocol") or ("SAS" if drv.get("CapableSpeedGbs") else "")).upper()
    cap_str   = _bytes_to_human(cap_b) if cap_b else None
    if not protocol:
        rpm = drv.get("RotationSpeedRPM")
        if media == "SSD": protocol = "SSD"
        elif rpm: protocol = "SAS" if int(rpm) > 0 else "SATA"
    prefix = "SSD" if media == "SSD" else "HDD"
    parts = [prefix]
    if cap_str: parts.append(cap_str)
    if protocol and protocol not in ("SSD",): parts.append(protocol)
    return " ".join(parts)

def name_psu(psu):
    watts = psu.get("PowerCapacityWatts")
    model = _pick(psu, ["Model", "Name"]) or ""
    if not watts:
        m = re.search(r'(\d{3,4})\s*W', model, re.IGNORECASE)
        if m: watts = m.group(1)
    if watts: return f"PSU {watts}W"
    return "PSU"

def name_nic(adapter_name, pci_info=None):
    aname = adapter_name or "NIC"
    short = re.search(r'(\d+\w*(?:SFP\+?|FLR|FLB|T\b|i\b))', aname)
    model_short = short.group(1) if short else None
    loc_short = ""
    if pci_info:
        loc = pci_info.get("DeviceLocation") or ""
        loc_short = (loc.replace("PCI-E Slot", "Slot")
                       .replace("Flexible LOM", "FlexLOM")
                       .replace("Embedded LOM", "EmbLOM")
                       .replace("Embedded", "Emb")
                       .replace(" ", "").strip())
    if model_short and loc_short: return f"{model_short}-{loc_short}"
    if model_short: return model_short
    if loc_short: return f"NIC-{loc_short}"
    return "NIC"

def name_hba(name_str, device_location):
    loc_short = (device_location or "")
    loc_short = (loc_short.replace("PCI-E Slot","Slot").replace(" ","").strip())
    short = re.search(r'(\d+\w*(?:Gb|GFC|HBA|FC))', name_str or "")
    model_short = short.group(1) if short else None
    if model_short and loc_short: return f"HBA-{model_short}-{loc_short}"
    if loc_short: return f"HBA-{loc_short}"
    return "HBA"

def is_ssd(drv):
    media = (drv.get("MediaType") or "").upper()
    if media == "SSD": return True
    if media == "HDD": return False
    model = (drv.get("Model") or "").upper()
    if any(k in model for k in ("SSD", "FLASH", "MLC", "TLC", "NVME", "SRI")):
        return True
    return bool(re.search(r'\b(?:EO|RI|WI)\b', model))

# ═══════════════════════════════════════════════════════════════════════════════
# smart item naming (storage)
# ═══════════════════════════════════════════════════════════════════════════════
def parse_storage_size_bytes(size_str, size_numeric=None):
    if size_str:
        m = re.match(r"([\d.]+)\s*(TB|GB|MB)", str(size_str).strip(), re.IGNORECASE)
        if m:
            val = float(m.group(1))
            mult = {"TB": 1024 ** 4, "GB": 1024 ** 3, "MB": 1024 ** 2}
            return int(val * mult[m.group(2).upper()])
    if size_numeric is not None:
        try:
            n = int(size_numeric)
            if n <= 0: return None
            return n * 1024 * 1024
        except Exception: pass
    return None

def is_ssd_storage(props):
    # Field names differ between "show disks" (drive-type, model)
    # and "show disk-parameters" (disk-type, disk-description)
    model = str(
        props.get("model") or
        props.get("disk-description") or
        props.get("description") or ""
    ).upper()
    dtype = str(
        props.get("drive-type") or     # show disks (newer firmware)
        props.get("disk-type") or      # show disk-parameters (older firmware)
        props.get("type") or ""
    ).upper()
    if "SSD" in dtype or "FLASH" in dtype: return True
    if "SSD" in model or "FLASH" in model: return True
    if "HDD" in dtype or "SAS" in dtype or "SATA" in dtype: return False
    return False

def name_storage_disk(props):
    media = "SSD" if is_ssd_storage(props) else "HDD"
    # "show disks" → size field; "show disk-parameters" → total-size field
    size  = props.get("size") or props.get("total-size") or props.get("formatted-size")
    model = props.get("model") or props.get("disk-description")
    parts = [media]
    if size:  parts.append(str(size))
    elif model: parts.append(str(model)[:30])
    return " ".join(parts)[:64]

def name_storage_psu(props):
    loc = props.get("location") or props.get("enclosure-id") or ""
    return f"PSU {loc}".strip()[:64]

def name_storage_controller(props):
    cid = props.get("controller-id") or props.get("durable-id") or "CTRL"
    return f"Controller {cid}"[:64]


# ═══════════════════════════════════════════════════════════════════════════════
# IP scanning
# ═══════════════════════════════════════════════════════════════════════════════
def expand_ranges(ranges):
    ips = []
    for cidr in ranges:
        net = ipaddress.ip_network(cidr, strict=False)
        if net.num_addresses == 1:
            ips.append(str(net.network_address))
        else:
            ips.extend(str(h) for h in net.hosts())
    return ips

def is_port_open(ip, port, timeout=3):
    try:
        with socket.create_connection((ip, port), timeout=timeout): return True
    except Exception: return False

# ═══════════════════════════════════════════════════════════════════════════════
# Redfish (server) session
# ═══════════════════════════════════════════════════════════════════════════════
class RedfishSession:
    def __init__(self, host):
        self.base = f"https://{host}"
        self.s    = requests.Session()
        self.s.headers.update({"OData-Version": "4.0"})
        self.token = None
        self.session_location = None

    def login(self):
        r = self.s.post(f"{self.base}/redfish/v1/SessionService/Sessions/",
                        json={"UserName": REDFISH_USER, "Password": REDFISH_PASS},
                        verify=False, timeout=30)
        r.raise_for_status()
        self.token = r.headers.get("X-Auth-Token")
        self.session_location = r.headers.get("Location")
        if not self.token or not self.session_location:
            raise RuntimeError("Redfish login ok but missing token/location")

    def get(self, path):
        r = self.s.get(f"{self.base}{path}",
                       headers={"X-Auth-Token": self.token},
                       verify=False, timeout=30)
        r.raise_for_status()
        return r.json()

    def logout(self):
        if not self.token or not self.session_location: return
        url = self.session_location if self.session_location.startswith("http") \
              else f"{self.base}{self.session_location}"
        try: self.s.delete(url, headers={"X-Auth-Token": self.token},
                           verify=False, timeout=10)
        except Exception: pass

def _resolve_server_name(rf, sys_data):
    serial = (sys_data.get("SerialNumber") or "").strip()
    model  = (sys_data.get("Model") or "").strip()
    hn = (sys_data.get("HostName") or "").strip()
    if hn and hn.lower() not in ("", "localhost", "computer system"):
        return hn
    try:
        mgr_col  = rf.get("/redfish/v1/Managers/")
        mgr      = rf.get(mgr_col["Members"][0]["@odata.id"])
        hp       = (mgr.get("Oem") or {})
        hp       = hp.get("Hp") or hp.get("Hpe") or {}
        srv_name = (hp.get("ServerName") or "").strip()
        if srv_name and srv_name.lower() not in ("", "computer system"):
            return srv_name
        ilo_name = (hp.get("iLOName") or mgr.get("HostName") or "").strip()
        if ilo_name and ilo_name.lower() not in ("", "manager", "ilo"):
            return ilo_name
    except Exception: pass
    asset = (sys_data.get("AssetTag") or "").strip()
    if asset and asset.lower() not in ("", "unknown"): return asset
    ip = rf.base.replace("https://", "").replace("http://", "")
    normalized = normalize_model(model, SERVER_MODEL_MAP) if model else None
    if normalized and normalized != "Unknown" and serial:
        return f"{normalized}-{serial}"
    if normalized and normalized != "Unknown":
        return f"{normalized}-{ip}"
    if serial: return f"HPE-{serial}"
    return f"HPE-{ip}"

def probe_redfish(ip):
    if not is_port_open(ip, REDFISH_PORT): return None
    host = f"{ip}:{REDFISH_PORT}"
    try:
        rf = RedfishSession(host)
        rf.login()
        try:
            root   = rf.get("/redfish/v1/")
            syscol = rf.get(root["Systems"]["@odata.id"])
            sys    = rf.get(syscol["Members"][0]["@odata.id"])
            name   = _resolve_server_name(rf, sys)
            return {
                "ip":           ip,
                "host":         host,
                "serial":       sys.get("SerialNumber"),
                "model":        sys.get("Model"),
                "hostname":     name,
                "manufacturer": sys.get("Manufacturer") or "HPE",
            }
        finally:
            rf.logout()
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# Storage session
# ═══════════════════════════════════════════════════════════════════════════════
class StorageSession:
    API_PREFIX = "/api/"

    def __init__(self, ip, port=443):
        self.ip = ip
        self.base = f"https://{ip}:{port}"
        self.session = requests.Session()
        self.session.verify = False
        self.session_key = None

    def _credential_hash(self, hash_type):
        cred = f"{STORAGE_USER}_{STORAGE_PASS}".encode()
        if hash_type == "md5":
            return hashlib.md5(cred).hexdigest()
        return hashlib.sha256(cred).hexdigest()

    def login(self):
        errors = []
        for hash_type in (STORAGE_AUTH_HASH, "sha256", "md5"):
            if hash_type in errors: continue
            try:
                xml = self._request(f"login/{self._credential_hash(hash_type)}")
                status = self._response_status(xml)
                self.session_key = status["response"]
                self.session.cookies.set("wbisessionkey", self.session_key)
                self.session.cookies.set("wbiusername", STORAGE_USER)
                return
            except Exception as exc:
                errors.append(hash_type)
                last_error = exc
        raise RuntimeError(f"Storage login failed for {self.ip}: {last_error}")

    def logout(self):
        if not self.session_key: return
        try: self._request("exit")
        except Exception: pass
        finally: self.session_key = None

    def _headers(self):
        headers = {"dataType": "api"}
        if self.session_key:
            headers["sessionKey"] = self.session_key
        return headers

    def _quick_request(self, path):
        url = f"{self.base}{self.API_PREFIX}{path.lstrip('/')}"
        try:
            r = self.session.get(url, headers={"dataType": "api"}, verify=False, timeout=5)
            if r.status_code != 200:
                return None
            return ET.fromstring(r.text)
        except Exception:
            return None

    def quick_probe(self):
        """Fast check without login – is this a storage XML API?"""
        xml = self._quick_request("login/check")
        if xml is not None:
            return True
        xml = self._quick_request("show/system")
        if xml is not None:
            return True
        return False

    def _request(self, path, method="GET"):
        url = f"{self.base}{self.API_PREFIX}{path.lstrip('/')}"
        r = self.session.request(method, url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        if r.text.strip().startswith("*"):
            raise RuntimeError(f"STORAGE_RATE_LIMIT:{r.text.strip()}")
        try:
            return ET.fromstring(r.text)
        except ET.ParseError as exc:
            raise RuntimeError(f"Invalid XML from {url}: {exc}") from exc

    @staticmethod
    def _response_status(xml_root):
        status = xml_root.find("./OBJECT[@name='status']")
        if status is None:
            raise RuntimeError("Storage response missing status object")
        props = {p.get("name"): (p.text or "").strip() for p in status.findall("PROPERTY")}
        if props.get("response-type", "").lower() != "success":
            raise RuntimeError(props.get("response") or props.get("response-type") or "Storage API error")
        return props

    def show(self, command, retries=4, retry_delay=5):
        for attempt in range(1, retries + 1):
            try:
                xml = self._request(f"show/{command}")
                self._response_status(xml)
                return self._parse_objects(xml)
            except RuntimeError as exc:
                if "STORAGE_RATE_LIMIT" in str(exc):
                    if attempt < retries:
                        log("WARN", f"  Rate-limit on show {command} ({self.ip}), "
                                    f"retry {attempt}/{retries - 1} in {retry_delay}s ...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                raise

    @staticmethod
    def _parse_objects(xml_root):
        objects = []
        for obj in xml_root.findall("OBJECT"):
            basetype = obj.get("basetype")
            if not basetype or basetype == "status": continue
            props = {"basetype": basetype, "name": obj.get("name"), "oid": obj.get("oid")}
            for prop in obj.findall("PROPERTY"):
                props[prop.get("name")] = (prop.text or "").strip()
            objects.append(props)
        return objects

def probe_storage(ip):
    if not is_port_open(ip, STORAGE_PORT): return None
    storage = StorageSession(ip, STORAGE_PORT)
    if not storage.quick_probe():
        return None
    try:
        storage.login()
        system_rows = storage.show("system")
        version_rows = storage.show("versions")
        if not system_rows: return None

        system = system_rows[0]
        serial = system.get("serial-number") or system.get("midplane-serial-number")
        product = system.get("product-id") or system.get("vendor-name") or "Storage"
        system_name = system.get("system-name") or system.get("system-contact") or f"storage-{ip.replace('.', '-')}"
        firmware = None
        for row in version_rows:
            fw = row.get("bundle-version") or row.get("sc-firmware") or row.get("firmware-version")
            if fw: firmware = fw; break

        return {
            "ip":           ip,
            "serial":       serial,
            "model":        normalize_model(product, STORAGE_MODEL_MAP) or product,
            "hostname":     system_name.strip(),
            "manufacturer": system.get("vendor-name") or DEFAULT_MFR,
            "health":       system.get("health"),
            "firmware":     firmware,
        }
    except Exception:
        return None
    finally:
        storage.logout()

# ═══════════════════════════════════════════════════════════════════════════════
# unified scanner
# ═══════════════════════════════════════════════════════════════════════════════
def scan_all():
    all_found = {"servers": [], "storage": []}

    bmc_ips = expand_ranges(BMC_RANGES)
    log("INFO", f"Scanning {len(bmc_ips)} IPs across {len(BMC_RANGES)} BMC ranges ...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(probe_redfish, ip): ip for ip in bmc_ips}
        for f in as_completed(futures):
            r = f.result()
            if r:
                log("INFO", f"  + SERVER {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["servers"].append(r)
    log("INFO", f"Server scan done: {len(all_found['servers'])} found.")

    server_ips = {h["ip"] for h in all_found["servers"]}
    all_storage_ips = expand_ranges(STORAGE_RANGES)
    storage_ips = [ip for ip in all_storage_ips if ip not in server_ips]
    skipped = len(all_storage_ips) - len(storage_ips)
    if skipped:
        log("INFO", f"Skipped {skipped} IP(s) in storage ranges already found as servers.")

    if storage_ips:
        log("INFO", f"Scanning {len(storage_ips)} IPs for storage ...")
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(probe_storage, ip): ip for ip in storage_ips}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    log("INFO", f"  + STORAGE {r['ip']}  {r['model']}  s/n={r['serial']}")
                    all_found["storage"].append(r)
        log("INFO", f"Storage scan done: {len(all_found['storage'])} found.")
    else:
        log("WARN", "No storage ranges to scan (all excluded or none configured).")

    return all_found

# ═══════════════════════════════════════════════════════════════════════════════
# NetBox CRUD helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _get_or_create(endpoint, lookup, create):
    obj = endpoint.get(**lookup)
    if obj: return obj.id
    return endpoint.create(create).id

def get_or_create_manufacturer(name):
    if not name: return None
    name = name.strip()
    return _get_or_create(get_netbox().dcim.manufacturers, {"name": name},
                          {"name": name, "slug": slugify(name)})

def get_or_create_device_type(model, mfr_id, model_map=None):
    m = normalize_model(model, model_map) or model or "Unknown"
    return _get_or_create(get_netbox().dcim.device_types, {"model": m},
                          {"model": m, "slug": slugify(m), "manufacturer": mfr_id})

def get_or_create_role(name, color="9e9e9e"):
    api = get_netbox()
    r = api.dcim.device_roles.get(name=name)
    if r: return r.id
    r = api.dcim.device_roles.get(slug=slugify(name))
    if r: return r.id
    return api.dcim.device_roles.create(
        {"name": name, "slug": slugify(name), "color": color}).id

def get_or_create_site(name):
    return _get_or_create(get_netbox().dcim.sites, {"name": name},
                          {"name": name, "slug": slugify(name), "status": "active"})

def find_device(serial, role_name=None):
    """Search by serial only — custom field filters are unreliable in this NetBox."""
    if _invalid_serial(serial):
        return None
    api = get_netbox()
    results = list(api.dcim.devices.filter(serial=serial.strip()))
    if not results:
        return None
    if role_name:
        match = [d for d in results if d.role and d.role.name == role_name]
        return match[0] if match else None
    return results[0]

# ═══════════════════════════════════════════════════════════════════════════════
# device ensure / mark offline
# ═══════════════════════════════════════════════════════════════════════════════
def _device_name(probe, prefix="server"):
    hn = probe.get("hostname") or f"{prefix}-{probe['ip'].replace('.', '-')}"
    return hn.strip()[:64]

def ensure_server_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "HPE")
    role_id = get_or_create_role(SERVER_ROLE)
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, SERVER_MODEL_MAP)
    name = _device_name(probe)
    api = get_netbox()
    dev = find_device(serial, role_name=SERVER_ROLE)
    # Secondary: find by name+site+role
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = next((c for c in cands if not (c.custom_fields or {}).get("storage_ip")), None)
        if dev: log("INFO", f"  Found server by name+site: {name} (id={dev.id})")
    if dev:
        api.dcim.devices.update([{
            "id": dev.id, "name": name, "status": "active",
            "site": site_id, "device_type": dtype_id, "role": role_id,
            "custom_fields": {"bmc_ip": probe["ip"], "redfish_enabled": True},
            **({"serial": serial} if not _invalid_serial(serial) else {}),
        }])
        log("INFO", f"  Server updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create({
        "name": name, "device_type": dtype_id, "role": role_id,
        "site": site_id, "serial": serial if not _invalid_serial(serial) else "",
        "status": "active",
        "custom_fields": {"bmc_ip": probe["ip"], "redfish_enabled": True},
    })
    log("INFO", f"  Server created: {name} (id={new.id})")
    return new.id

def ensure_storage_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or DEFAULT_MFR)
    role_id = get_or_create_role(STORAGE_ROLE, "2196f3")
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, STORAGE_MODEL_MAP)
    name = _device_name(probe, prefix="storage")
    api = get_netbox()
    dev = find_device(serial, role_name=STORAGE_ROLE)
    # Secondary: find by name+site+role (storage names unique per site)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = next((c for c in cands if not (c.custom_fields or {}).get("bmc_ip")), None)
        if dev: log("INFO", f"  Found storage by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id,
        "custom_fields": {
            "storage_ip":       probe["ip"],
            "storage_enabled":  True,
            "storage_health":   probe.get("health"),
            "storage_firmware": probe.get("firmware"),
            "storage_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload, "role": role_id}])
        log("INFO", f"  Storage updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create({**payload, "role": role_id})
    log("INFO", f"  Storage created: {name} (id={new.id})")
    return new.id

def mark_server_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"redfish_enabled": False},
        }])
        log("WARN", f"  Server marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark server offline {dev_name}: {e}")

def mark_storage_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"storage_enabled": False},
        }])
        log("WARN", f"  Storage marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark storage offline {dev_name}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# inventory collection – Redfish (server)
# ═══════════════════════════════════════════════════════════════════════════════
def rf_collect_inventory(host):
    rf = RedfishSession(host)
    rf.login()
    try:
        root      = rf.get("/redfish/v1/")
        syscol    = rf.get(root["Systems"]["@odata.id"])
        sys       = rf.get(syscol["Members"][0]["@odata.id"])
        sys_odata = sys.get("@odata.id")
        oem_data  = _get_oem(sys)

        inventory = {}

        add_item = _make_add_item(inventory)

        # CPU
        cpu_model = cpu_sockets = cpu_cores = cpu_threads = None
        ps = sys.get("ProcessorSummary") or {}
        cpu_model   = _pick(ps, ["Model"])
        cpu_sockets = _to_int(ps.get("Count"))
        cpu_cores   = _to_int(ps.get("CoreCount"))
        cpu_threads = _to_int(ps.get("ThreadCount"))

        procs_link = (sys.get("Processors") or {}).get("@odata.id") \
                     if isinstance(sys.get("Processors"), dict) else None
        if procs_link:
            models, sockets, cores, threads = [], 0, 0, 0
            for m in rf.get(procs_link).get("Members", []):
                p = rf.get(m["@odata.id"])
                if (p.get("Status") or {}).get("State") == "Absent": continue
                sockets += 1
                if p.get("Model"): models.append(p["Model"])
                cores   += _to_int(p.get("TotalCores"))   or 0
                threads += _to_int(p.get("TotalThreads")) or 0
                add_item(
                    name=name_cpu(p),
                    manufacturer=p.get("Manufacturer"),
                    part_number=None,
                    serial=_pick(p, ["SerialNumber"]),
                    description=f"Model={p.get('Model')} Cores={p.get('TotalCores')} Threads={p.get('TotalThreads')}",
                    role_id=ROLE_CPU)
            cpu_sockets = cpu_sockets or (sockets or None)
            cpu_cores   = cpu_cores   or (cores   or None)
            cpu_threads = cpu_threads or (threads or None)
            if models and not cpu_model: cpu_model = max(set(models), key=models.count)

        # RAM
        ram_gib = None
        ms = sys.get("MemorySummary") or {}
        if ms.get("TotalSystemMemoryGiB") is not None:
            try: ram_gib = int(round(float(ms["TotalSystemMemoryGiB"])))
            except Exception: pass

        mem_link = (sys.get("Memory") or {}).get("@odata.id") \
                   if isinstance(sys.get("Memory"), dict) else None
        if mem_link:
            total_mib = 0
            for m in rf.get(mem_link).get("Members", []):
                mm = rf.get(m["@odata.id"])
                if (mm.get("Status") or {}).get("State") == "Absent": continue
                cap = _to_int(mm.get("CapacityMiB"))
                if cap: total_mib += cap
                add_item(
                    name=name_ram(mm),
                    manufacturer=mm.get("Manufacturer"),
                    part_number=_pick(mm, ["PartNumber","PartNumberString"]),
                    serial=_pick(mm, ["SerialNumber","SerialNumberString"]),
                    description=f"Model={mm.get('Model')} CapacityMiB={mm.get('CapacityMiB')} "
                                f"SpeedMHz={mm.get('OperatingSpeedMhz')} Type={mm.get('MemoryDeviceType')}",
                    role_id=ROLE_MEMORY)
            if ram_gib is None and total_mib:
                ram_gib = int(round(total_mib / 1024))

        # Storage (Redfish)
        disk_total_bytes = 0
        drive_idx = 0

        stor_link = (sys.get("Storage") or {}).get("@odata.id") \
                    if isinstance(sys.get("Storage"), dict) else None
        if not stor_link:
            try:
                cu = _chassis_url(sys)
                if cu:
                    ch = rf.get(cu)
                    stor_link = (ch.get("Storage") or {}).get("@odata.id") \
                                if isinstance(ch.get("Storage"), dict) else None
            except Exception: pass

        if stor_link:
            for sm in rf.get(stor_link).get("Members", []):
                stor = rf.get(sm["@odata.id"])
                cr = stor.get("StorageControllers") or stor.get("Controllers")
                ctrls = []
                if isinstance(cr, list): ctrls = cr
                elif isinstance(cr, dict):
                    cl = cr.get("@odata.id") or cr.get("href")
                    if cl:
                        for m2 in (rf.get(cl).get("Members") or []):
                            u = m2.get("@odata.id") or m2.get("href")
                            if u:
                                try: ctrls.append(rf.get(u))
                                except Exception: pass
                for ctrl in ctrls:
                    if not isinstance(ctrl, dict): continue
                    if (ctrl.get("Status") or {}).get("State") == "Absent": continue
                    add_item(
                        name=f"Controller-{_get_location(ctrl) or 'CTRL'}",
                        manufacturer=ctrl.get("Manufacturer"),
                        part_number=_pick(ctrl, ["PartNumber","SKU","SparePartNumber","ProductId"]),
                        serial=_pick(ctrl, ["SerialNumber"]),
                        description=f"Model={_pick(ctrl,['Model','ProductName','Name'])} "
                                    f"Firmware={_pick(ctrl,['FirmwareVersion','Version'])}",
                        role_id=ROLE_CONTROLLER)
                for d in (stor.get("Drives") or []):
                    drv = rf.get(d["@odata.id"])
                    if not isinstance(drv, dict): continue
                    if (drv.get("Status") or {}).get("State") == "Absent": continue
                    cap = _capacity_to_bytes(drv)
                    if cap: disk_total_bytes += cap
                    role_id = ROLE_SSD if is_ssd(drv) else ROLE_HDD
                    add_item(
                        name=name_disk(drv),
                        manufacturer=drv.get("Manufacturer"),
                        part_number=_pick(drv, ["PartNumber","Model"]),
                        serial=_pick(drv, ["SerialNumber"]),
                        description=f"Model={drv.get('Model')} Capacity={drv.get('CapacityBytes')} "
                                    f"MediaType={drv.get('MediaType')} Protocol={drv.get('Protocol')}",
                        role_id=role_id)
                    drive_idx += 1

        # HPE SmartStorage fallback (Gen9) — only when Redfish yielded no drives
        if drive_idx == 0:
            sl_obj = (oem_data.get("Links") or {}).get("SmartStorage") or {} \
                     if isinstance(oem_data.get("Links"), dict) else {}
            smart_url = sl_obj.get("@odata.id") or sl_obj.get("href") \
                        if isinstance(sl_obj, dict) else None
            if not smart_url and sys_odata:
                smart_url = sys_odata.rstrip("/") + "/SmartStorage/"
            if smart_url:
                try:
                    smart = rf.get(smart_url)
                    ac_obj = (smart.get("Links") or {}).get("ArrayControllers") or {}
                    cl = ac_obj.get("@odata.id") or ac_obj.get("href") \
                         or smart_url.rstrip("/") + "/ArrayControllers/"
                    for cm in rf.get(cl).get("Members", []):
                        ctrl = rf.get(cm["@odata.id"])
                        add_item(
                            name=f"Controller-{_get_location(ctrl) or 'CTRL'}",
                            manufacturer=ctrl.get("Manufacturer"),
                            part_number=_pick(ctrl, ["PartNumber","SKU","SparePartNumber","ProductId"]),
                            serial=_pick(ctrl, ["SerialNumber"]),
                            description=f"Model={_pick(ctrl,['Model','ProductName','Name'])} "
                                        f"Firmware={_pick(ctrl,['FirmwareVersion','Version'])}",
                            role_id=ROLE_CONTROLLER)
                        pd_info = ctrl.get("PhysicalDrives") or (ctrl.get("Links") or {}).get("PhysicalDrives") or {}
                        if isinstance(pd_info, dict):
                            pu = pd_info.get("@odata.id") or pd_info.get("href")
                            members = rf.get(pu).get("Members") or [] if pu else []
                        elif isinstance(pd_info, list): members = pd_info
                        else: members = []
                        for pdm in members:
                            u = pdm.get("@odata.id") or pdm.get("href")
                            if not u: continue
                            drv = rf.get(u)
                            cap = _capacity_to_bytes(drv)
                            if cap: disk_total_bytes += cap
                            role_id = ROLE_SSD if is_ssd(drv) else ROLE_HDD
                            add_item(
                                name=name_disk(drv),
                                manufacturer=drv.get("Manufacturer"),
                                part_number=drv.get("PartNumber") or drv.get("Model"),
                                serial=drv.get("SerialNumber"),
                                description=f"Model={drv.get('Model')} CapacityGB={drv.get('CapacityGB')} "
                                            f"MediaType={drv.get('MediaType')}",
                                role_id=role_id)
                            drive_idx += 1
                except Exception: pass

        # Power Supplies
        try:
            cu = _chassis_url(sys)
            if cu:
                chassis = rf.get(cu)
                pl = (chassis.get("Power") or {}).get("@odata.id") \
                     if isinstance(chassis.get("Power"), dict) else None
                if pl:
                    for psu in rf.get(pl).get("PowerSupplies", []):
                        if not isinstance(psu, dict): continue
                        if (psu.get("Status") or {}).get("State") == "Absent": continue
                        add_item(
                            name=name_psu(psu),
                            manufacturer=psu.get("Manufacturer"),
                            part_number=_pick(psu, ["PartNumber","SparePartNumber","Model"]),
                            serial=_pick(psu, ["SerialNumber"]),
                            description=f"Model={_pick(psu,['Model','Name'])} "
                                        f"LineInputVoltage={psu.get('LineInputVoltage')} "
                                        f"PowerCapacityW={psu.get('PowerCapacityWatts')}",
                            role_id=ROLE_PSU)
        except Exception: pass

        # Battery Gen9
        for bat in (oem_data.get("Battery") or []):
            if not isinstance(bat, dict): continue
            if not bat.get("SerialNumber"): continue
            idx = bat.get("Index") or "1"
            add_item(
                name=f"Battery {idx}",
                manufacturer="HPE",
                part_number=bat.get("Model") or bat.get("Spare"),
                serial=bat["SerialNumber"],
                description=f"Model={bat.get('ProductName')} "
                            f"FirmwareVersion={bat.get('FirmwareVersion')} "
                            f"Condition={bat.get('Condition')}",
                role_id=ROLE_BATTERY)

        # Battery Gen10
        try:
            cu = _chassis_url(sys)
            if cu:
                chassis_hpe = _get_oem(rf.get(cu)) or {}
                for bat in (chassis_hpe.get("SmartStorageBattery") or []):
                    if not isinstance(bat, dict): continue
                    if not bat.get("SerialNumber"): continue
                    idx = bat.get("Index") or "1"
                    add_item(
                        name=f"Battery {idx}",
                        manufacturer="HPE",
                        part_number=bat.get("Model") or bat.get("SparePartNumber"),
                        serial=bat["SerialNumber"],
                        description=f"Model={bat.get('ProductName','Smart Storage Battery')} "
                                    f"FirmwareVersion={bat.get('FirmwareVersion')} "
                                    f"MaximumCapWatts={bat.get('MaximumCapWatts')} "
                                    f"ChargeLevel={bat.get('ChargeLevelPercent')}%",
                        role_id=ROLE_BATTERY)
        except Exception: pass

        # Network Adapters
        try:
            uefi_to_pci = {}
            try:
                pci_col = rf.get(sys_odata.rstrip("/") + "/PCIDevices/")
                items = pci_col.get("Items") or []
                if not items:
                    for m in (pci_col.get("Members") or []):
                        if "Name" in m: items.append(m)
                        else:
                            try: items.append(rf.get(m["@odata.id"]))
                            except Exception: pass
                for item in items:
                    if isinstance(item, dict) and item.get("UEFIDevicePath"):
                        uefi_to_pci[item["UEFIDevicePath"]] = item
            except Exception: pass

            for m in (rf.get(sys_odata.rstrip("/") + "/NetworkAdapters/").get("Members") or []):
                try:
                    adapter = rf.get(m["@odata.id"])
                    if not isinstance(adapter, dict): continue
                    serial = adapter.get("SerialNumber")
                    if not serial: continue
                    ports = adapter.get("PhysicalPorts") or []
                    pci_info = None
                    for port in ports:
                        pp = port.get("UEFIDevicePath")
                        if pp and pp in uefi_to_pci: pci_info = uefi_to_pci[pp]; break
                    if not pci_info:
                        ap = adapter.get("UEFIDevicePath")
                        if ap and ap in uefi_to_pci: pci_info = uefi_to_pci[ap]
                    aname = adapter.get("Name") or "NIC"
                    fw = (adapter.get("Firmware") or {}).get("Current", {}).get("VersionString")
                    macs = " ".join(p.get("MacAddress","") for p in ports[:2] if p.get("MacAddress"))
                    add_item(
                        name=name_nic(aname, pci_info),
                        manufacturer="HPE",
                        part_number=adapter.get("PartNumber"),
                        serial=serial,
                        description=f"Model={aname} FW={fw} MACs={macs}",
                        role_id=ROLE_NIC)
                except Exception: pass
        except Exception: pass

        # PCIe FRUs Gen10 (with real SerialNumber)
        try:
            pci_link = None
            pl_obj = (oem_data.get("Links") or {}).get("PCIDevices") or {} \
                     if isinstance(oem_data.get("Links"), dict) else {}
            pci_link = pl_obj.get("@odata.id") or pl_obj.get("href") \
                       if isinstance(pl_obj, dict) else None
            if not pci_link:
                try:
                    cu = _chassis_url(sys)
                    if cu:
                        ch = rf.get(cu)
                        pcie = ch.get("PCIeDevices") or {}
                        pci_link = pcie.get("@odata.id") or pcie.get("href") \
                                   if isinstance(pcie, dict) else None
                except Exception: pass
            if pci_link:
                for m in (rf.get(pci_link).get("Members") or []):
                    try:
                        dev = rf.get(m["@odata.id"])
                        serial = dev.get("SerialNumber") if isinstance(dev, dict) else None
                        if not serial: continue
                        dname = dev.get("ProductName") or dev.get("Name") or "PCIe"
                        role_id = ROLE_HBA if any(k in dname for k in ("HBA","FC","Fibre")) \
                                  else ROLE_NIC
                        add_item(
                            name=dname[:64],
                            manufacturer=dev.get("Manufacturer") or sys.get("Manufacturer"),
                            part_number=dev.get("ProductPartNumber") or dev.get("PartNumber"),
                            serial=serial,
                            description=f"ProductVersion={dev.get('ProductVersion')} "
                                        f"FirmwareVersion={dev.get('FirmwareVersion')}",
                            role_id=role_id)
                    except Exception: pass
        except Exception: pass

        # HBA pseudo-serial (Gen9 iLO4)
        try:
            pci_col = rf.get(sys_odata.rstrip("/") + "/PCIDevices/")
            pci_items = pci_col.get("Items") or []
            if not pci_items:
                for m in (pci_col.get("Members") or []):
                    if "Name" in m: pci_items.append(m)
                    else:
                        try: pci_items.append(rf.get(m["@odata.id"]))
                        except Exception: pass

            for item in pci_items:
                if not isinstance(item, dict): continue
                device_location = item.get("DeviceLocation") or ""
                name_str        = item.get("Name") or ""
                structured_name = item.get("StructuredName") or ""
                device_type     = item.get("DeviceType") or ""

                if "Embedded" in device_location or "LOM" in device_location: continue
                if device_type in ("SATA Controller",): continue

                is_hba = any(k in name_str for k in
                             ("HBA","FC","Fibre","Emulex","QLogic","Brocade","SN1100","SN1200"))
                if not is_hba: continue
                if not structured_name: continue

                subsystem_id  = item.get("SubsystemDeviceID") or "0"
                pseudo_serial = f"{structured_name}-{subsystem_id}"

                already = any(device_location.replace("PCI-E ","").replace(" ","") in v.get("name","")
                              for s, v in inventory.items() if not s.startswith("PCI."))
                if already: continue

                fw_version = None
                item_uefi  = item.get("UEFIDevicePath") or ""
                try:
                    fw_inv = rf.get(sys_odata.rstrip("/") + "/FirmwareInventory/")
                    for key, entries in (fw_inv.get("Current") or {}).items():
                        if not isinstance(entries, list): continue
                        for entry in entries:
                            if item_uefi and item_uefi in (entry.get("UEFIDevicePaths") or []):
                                fw_version = entry.get("VersionString"); break
                        if fw_version: break
                except Exception: pass

                add_item(
                    name=name_hba(name_str, device_location),
                    manufacturer=sys.get("Manufacturer") or "HPE",
                    part_number=None,
                    serial=pseudo_serial,
                    description=f"Model={name_str} Slot={device_location} "
                                f"FW={fw_version} (pseudo-serial: no serial via iLO4)",
                    role_id=ROLE_HBA)
        except Exception: pass

        disk_total_gib = gib_from_bytes(disk_total_bytes) if disk_total_bytes else None
        return {
            "summary": {
                "model":          sys.get("Model"),
                "serial":         sys.get("SerialNumber"),
                "power_state":    sys.get("PowerState"),
                "bios_version":   sys.get("BiosVersion"),
                "cpu_model":      cpu_model,
                "cpu_sockets":    cpu_sockets,
                "cpu_cores":      cpu_cores,
                "cpu_threads":    cpu_threads,
                "ram_gib":        ram_gib,
                "disk_total_gib": disk_total_gib,
            },
            "inventory": inventory,
        }
    finally:
        rf.logout()


# ═══════════════════════════════════════════════════════════════════════════════
# inventory collection – storage
# ═══════════════════════════════════════════════════════════════════════════════
def storage_collect_inventory(ip):
    storage = StorageSession(ip, STORAGE_PORT)
    storage.login()
    time.sleep(5)
    try:
        inventory = {}
        disk_total_bytes = 0
        disk_count = 0

        add_item = _make_add_item(inventory)

        show_commands = [
            ("controllers",    "controllers",    _collect_controller_storage),
            ("power-supplies", "power-supplies", _collect_psu_storage),
            ("frus",           "enclosure-fru",  _collect_fru_storage),
            ("disks",          None,             _collect_disk_storage),
        ]

        for command, expected_type, collector in show_commands:
            rows = None
            if command == "disks":
                disk_commands = ["disks", "disk-parameters", "disk-statistics"]
                for dcmd in disk_commands:
                    try:
                        rows = storage.show(dcmd)
                        log("INFO", f"    disk command '{dcmd}' succeeded on {ip}")
                        break
                    except Exception as exc:
                        msg = str(exc)
                        if "Rates may vary" in msg or "STORAGE_RATE_LIMIT" in msg:
                            log("WARN", f"  Rate-limit on show {dcmd} ({ip}), trying next command ...")
                            try:
                                storage.logout()
                            except Exception:
                                pass
                            time.sleep(10)
                            try:
                                storage.login()
                                time.sleep(5)
                            except Exception as login_exc:
                                log("WARN", f"  Re-login failed for {ip}: {login_exc}")
                                break
                        else:
                            log("WARN", f"  show {dcmd} failed on {ip}: {exc}")
                            break
                if rows is None:
                    log("WARN", f"  All disk commands failed on {ip} — disks will not be synced")
            else:
                try:
                    rows = storage.show(command)
                except Exception as exc:
                    log("WARN", f"  show {command} failed on {ip}: {exc}")

            if rows is None:
                continue

            actual_types = set(r.get("basetype") for r in rows)
            matched = 0
            for row in rows:
                bt = row.get("basetype") or ""
                if command == "disks":
                    if "drive" not in bt.lower():
                        continue
                elif expected_type and bt != expected_type:
                    continue
                added_bytes = collector(row, add_item)
                matched += 1
                if command == "disks" and added_bytes:
                    disk_total_bytes += added_bytes
                    disk_count += 1
            log("INFO", f"    show {command}: {len(rows)} rows, basetypes={actual_types}, matched={matched}")

        summary = {
            "serial":       None,
            "model":        None,
            "health":       None,
            "firmware":     None,
            "disk_count":   disk_count,
            "disk_total_gib": gib_from_bytes(disk_total_bytes),
        }

        try:
            system = storage.show("system")[0]
            summary["serial"] = system.get("serial-number")
            summary["model"] = normalize_model(system.get("product-id"), STORAGE_MODEL_MAP) or system.get("product-id")
            summary["health"] = system.get("health")
        except Exception: pass

        try:
            for row in storage.show("versions"):
                fw = row.get("bundle-version") or row.get("sc-firmware") or row.get("firmware-version")
                if fw: summary["firmware"] = fw; break
        except Exception: pass

        return {"summary": summary, "inventory": inventory}
    finally:
        storage.logout()


def _collect_disk_storage(row, add_item):
    # Support both "show disks" and "show disk-parameters" field names
    serial = row.get("serial-number")
    size_str = row.get("size") or row.get("total-size") or row.get("formatted-size")
    size_num = row.get("size-numeric") or row.get("total-size-numeric")
    cap = parse_storage_size_bytes(size_str, size_num)
    role_id = ROLE_SSD if is_ssd_storage(row) else ROLE_HDD
    model = row.get("model") or row.get("disk-description") or row.get("description")
    location = row.get("location") or row.get("slot")
    health = row.get("health") or row.get("disk-state")
    add_item(
        name=name_storage_disk(row),
        manufacturer=row.get("vendor") or row.get("manufacturer") or DEFAULT_MFR,
        part_number=model,
        serial=serial,
        description=(f"Location={location} Model={model} "
                     f"Size={size_str} Health={health} "
                     f"Type={row.get('drive-type') or row.get('disk-type')}"),
        role_id=role_id,
    )
    return cap or 0

def _collect_controller_storage(row, add_item):
    serial = row.get("serial-number")
    add_item(
        name=name_storage_controller(row),
        manufacturer=DEFAULT_MFR,
        part_number=row.get("hardware-version") or row.get("model"),
        serial=serial,
        description=f"Controller={row.get('controller-id')} IP={row.get('ip-address')} "
                    f"FW={row.get('sc-firmware') or row.get('firmware-version')} Health={row.get('health')}",
        role_id=ROLE_CONTROLLER,
    )
    return 0

def _collect_psu_storage(row, add_item):
    serial = row.get("serial-number")
    add_item(
        name=name_storage_psu(row),
        manufacturer=DEFAULT_MFR,
        part_number=row.get("part-number") or row.get("model"),
        serial=serial,
        description=f"Location={row.get('location')} Health={row.get('health')} Status={row.get('status')}",
        role_id=ROLE_PSU,
    )
    return 0

def _collect_fru_storage(row, add_item):
    serial = row.get("serial-number")
    part = row.get("part-number") or row.get("fru-shortname")
    name = row.get("fru-name") or row.get("name") or "FRU"
    add_item(
        name=str(name)[:64],
        manufacturer=DEFAULT_MFR,
        part_number=part,
        serial=serial,
        description=f"Location={row.get('location')} Health={row.get('health')}",
        role_id=ROLE_SAS_EXP,
    )
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# NetBox inventory sync (shared)
# ═══════════════════════════════════════════════════════════════════════════════
def sync_inventory(dev_id, new_inventory):
    api = get_netbox()
    by_serial = {}
    for item in list(api.dcim.inventory_items.filter(device_id=dev_id)):
        s = str(item.serial or "").strip()
        if s: by_serial.setdefault(s, []).append(item)
    for s, items in by_serial.items():
        if len(items) > 1:
            for item in items: item.delete()

    new_serials = set(new_inventory.keys())
    for item in list(api.dcim.inventory_items.filter(device_id=dev_id)):
        if item.serial and item.serial not in new_serials:
            item.delete()

    for serial, item in new_inventory.items():
        mfr_id = get_or_create_manufacturer(item.get("manufacturer"))
        payload = {
            "device":      dev_id,
            "name":        item["name"],
            "manufacturer": mfr_id,
            "part_id":     item.get("part_number") or "",
            "serial":      serial,
            "description": item.get("description") or "",
            **({"role": item["role"]} if item.get("role") else {}),
        }
        existing = api.dcim.inventory_items.get(device_id=dev_id, serial=serial)
        if existing:
            api.dcim.inventory_items.update([{"id": existing.id, **payload}])
        else:
            api.dcim.inventory_items.create(payload)


# ═══════════════════════════════════════════════════════════════════════════════
# main sync job
# ═══════════════════════════════════════════════════════════════════════════════
def run_sync():
    log("INFO", "=" * 60)
    log("INFO", "Unified sync started (servers + storage)")
    log("INFO", "=" * 60)

    found = scan_all()
    api = get_netbox()

    # ── Process servers ───────────────────────────────────────────────────────
    live_server_ips = {h["ip"] for h in found["servers"]}
    for probe in found["servers"]:
        ip = probe["ip"]
        host = probe["host"]
        log("INFO", f"Processing SERVER {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_server_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_server_device failed for {ip}: {e}"); continue

        try:
            data = rf_collect_inventory(host)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        s   = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "bmc_ip":                 ip,
                    "redfish_enabled":        True,
                    "redfish_model":          s.get("model"),
                    "redfish_power_state":    s.get("power_state"),
                    "redfish_bios_version":   s.get("bios_version"),
                    "redfish_cpu_model":      s.get("cpu_model"),
                    "redfish_cpu_sockets":    s.get("cpu_sockets"),
                    "redfish_cpu_cores":      s.get("cpu_cores"),
                    "redfish_cpu_threads":    s.get("cpu_threads"),
                    "redfish_ram_gib":        s.get("ram_gib"),
                    "redfish_disk_total_gib": s.get("disk_total_gib"),
                },
            }
            if s.get("serial"): payload["serial"] = s["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  server update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Server {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Process storage ──────────────────────────────────────────────────────
    live_storage_ips = {h["ip"] for h in found["storage"]}
    for probe in found["storage"]:
        ip = probe["ip"]
        log("INFO", f"Processing STORAGE {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_storage_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_storage_device failed for {ip}: {e}"); continue

        try:
            data = storage_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "storage_ip":                 ip,
                    "storage_enabled":            True,
                    "storage_health":             summary.get("health") or probe.get("health"),
                    "storage_firmware":           summary.get("firmware") or probe.get("firmware"),
                    "storage_model":              summary.get("model") or probe.get("model"),
                    "storage_disk_count":         summary.get("disk_count"),
                    "storage_total_capacity_gib": summary.get("disk_total_gib"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  storage update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Storage {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Mark unreachable devices offline ─────────────────────────────────────
    log("INFO", "Checking for unreachable servers (Redfish) ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_redfish_enabled=True)):
            bmc_ip = (dev.custom_fields or {}).get("bmc_ip")
            if not bmc_ip: continue
            ip = bmc_ip.split("/")[0].strip()
            if ip not in live_server_ips:
                mark_server_offline(dev.id, dev.name)
    except Exception as e:
        log("ERROR", f"Server offline check failed: {e}")

    log("INFO", "Checking for unreachable storage ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_storage_enabled=True)):
            storage_ip = (dev.custom_fields or {}).get("storage_ip")
            if not storage_ip: continue
            ip = str(storage_ip).split("/")[0].strip()
            if ip not in live_storage_ips:
                mark_storage_offline(dev.id, dev.name)
    except Exception as e:
        log("ERROR", f"Storage offline check failed: {e}")

    log("INFO", "Unified sync complete")
    log("INFO", "=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# scheduler
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        schedule.every().day.at("00:00").do(run_sync)
        schedule.every().day.at("12:00").do(run_sync)
        log("INFO", "Scheduler started — runs at 00:00 and 12:00 daily.")
        log("INFO", "Running initial unified sync now ...")
        run_sync()
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
