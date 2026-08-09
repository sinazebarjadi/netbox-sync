"""HPE MSA storage: XML API session, probing and per-array inventory
collection (with MSA 2040/2060 firmware-difference handling)."""
import hashlib
import ssl
import time
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter

from netbox_sync.config import (STORAGE_USER, STORAGE_PASS, STORAGE_PORT,
                                STORAGE_AUTH_HASH, DEFAULT_MFR, log)
from netbox_sync.models import STORAGE_MODEL_MAP
from netbox_sync.netbox import get_or_create_inventory_role
from netbox_sync.utils import (normalize_model, gib_from_bytes, _make_add_item,
                               is_port_open, parse_storage_size_bytes,
                               is_ssd_storage, name_storage_disk,
                               name_storage_psu, name_storage_controller)


class _LegacyTLSAdapter(HTTPAdapter):
    """Older MSA arrays (G1/G2 firmware) offer only legacy TLS — weak certs /
    cipher suites that modern OpenSSL (SECLEVEL>=1) rejects with
    SSLV3_ALERT_HANDSHAKE_FAILURE. Lower the security level for the storage
    session only (verify stays off either way for these self-signed boxes)."""

    def __init__(self, *args, **kwargs):
        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        try:
            self._ssl_context.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(connections, maxsize, block, **kwargs)


class StorageSession:
    API_PREFIX = "/api/"

    def __init__(self, ip, port=443):
        self.ip = ip
        self.base = f"https://{ip}:{port}"
        self.session = requests.Session()
        self.session.mount("https://", _LegacyTLSAdapter())
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
                # Check status but don't discard the response if it's just an
                # Info-level message (e.g. "Rates may vary"). The XML may still
                # contain the requested data alongside the info status object.
                # Only raise on Error-level status or when there are no data
                # objects at all.
                status_props = {}
                status_obj = xml.find("./OBJECT[@name='status']")
                if status_obj is not None:
                    status_props = {p.get("name"): (p.text or "").strip()
                                    for p in status_obj.findall("PROPERTY")}
                resp_type = status_props.get("response-type", "").lower()

                if resp_type == "error":
                    raise RuntimeError(status_props.get("response") or "Storage API error")

                if resp_type == "info":
                    # Info-level (e.g. "Rates may vary") -- the data may still
                    # be present. Parse objects and return them if we got any.
                    objects = self._parse_objects(xml)
                    if objects:
                        return objects
                    # No data objects -- treat as rate-limit and retry
                    raise RuntimeError(f"STORAGE_RATE_LIMIT:{status_props.get('response', '')}")

                # Success or unknown status -- parse and return
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

def probe_storage(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, STORAGE_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        storage = StorageSession(ip, STORAGE_PORT)
        if not storage.quick_probe():
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        try:
            storage.login()
            system_rows = storage.show("system")
            version_rows = storage.show("versions")
            if not system_rows: raise RuntimeError("empty system response")

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
            if attempt < retries:
                time.sleep(retry_delay); continue
            return None
        finally:
            try: storage.logout()
            except Exception: pass
    return None


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
                # ── Enrichment context ────────────────────────────────────────
                # MSA 2040 firmware permanently rate-limits "show disks", so
                # model/size/firmware are NOT available via the API. We enrich
                # the disk-statistics rows with inferable fields from other
                # endpoints:
                #   - drive-bus-type  from show enclosures (controller field)
                #   - array-drive-type from show disk-groups (per RAID group)
                #   - SSD vs HDD      inferred from disk-group name (SSD/HDD)
                enriched_drive_bus = None
                try:
                    enc_rows = storage.show("enclosures")
                    for er in enc_rows:
                        if er.get("basetype") == "controllers" and er.get("drive-bus-type"):
                            enriched_drive_bus = er.get("drive-bus-type")
                            break
                    if enriched_drive_bus:
                        log("INFO", f"    inferred drive-bus-type={enriched_drive_bus} from enclosures")
                except Exception as exc:
                    log("WARN", f"  show enclosures for drive-bus-type failed: {exc}")

                # Fetch disk-groups to infer per-disk type (SAS/SSD) from
                # group names and array-drive-type fields.
                disk_group_types = {}   # {pool-dg-name: {type, raid, bus}}
                try:
                    dg_rows = storage.show("disk-groups")
                    for dg in dg_rows:
                        if dg.get("basetype") == "disk-groups":
                            dg_name = dg.get("name") or ""
                            dg_info = {
                                "array-drive-type": dg.get("array-drive-type"),
                                "raidtype": dg.get("raidtype"),
                                "diskcount": dg.get("diskcount"),
                                "name": dg_name,
                            }
                            disk_group_types[dg_name] = dg_info
                    log("INFO", f"    found {len(disk_group_types)} disk-groups for type inference")
                except Exception as exc:
                    log("WARN", f"  show disk-groups failed: {exc}")

                # Dual-source strategy for MSA 2040/2060 compatibility:
                #
                #   "show disks"           -- MSA 2060: per-disk rows with
                #                            size, model, firmware, drive-type.
                #                            MSA 2040: permanently rate-limited.
                #   "show disk-statistics"  -- MSA 2040: per-disk rows with
                #                            serial-number, location, durable-id,
                #                            I/O stats.  No size/model/firmware.
                #
                # We fetch BOTH (when available) and merge by serial-number
                # so each NetBox inventory item carries every field that either
                # endpoint exposes, plus inferred fields from enclosures/disk-groups.
                disk_rows_full = None     # from "show disks" (rich, may fail)
                disk_rows_stats = None    # from "show disk-statistics" (always works on MSA 2040)

                # 1. Try "show disks" (rich data). Tolerate rate-limit / failure.
                try:
                    disk_rows_full = storage.show("disks")
                    log("INFO", f"    disk command 'disks' succeeded on {ip}: {len(disk_rows_full)} rows")
                    if disk_rows_full:
                        bts = set(r.get("basetype") for r in disk_rows_full)
                        log("DEBUG", f"    disks: {len(disk_rows_full)} rows, basetypes={bts}")
                        sample = disk_rows_full[0]
                        log("DEBUG", f"    disks sample keys: {list(sample.keys())}")
                except Exception as exc:
                    msg = str(exc)
                    if "Rates may vary" in msg or "STORAGE_RATE_LIMIT" in msg:
                        log("WARN", f"  Rate-limit on show disks ({ip}), will use disk-statistics only")
                        try: storage.logout()
                        except Exception: pass
                        time.sleep(10)
                        try:
                            storage.login()
                            time.sleep(5)
                        except Exception as le:
                            log("WARN", f"  Re-login failed for {ip}: {le}")
                    else:
                        log("WARN", f"  show disks failed on {ip}: {exc}")
                    disk_rows_full = None

                # 2. Try "show disk-statistics" (always works on MSA 2040).
                #    Retries once if the session died during the disks attempt.
                for stat_attempt in range(2):
                    try:
                        disk_rows_stats = storage.show("disk-statistics")
                        log("INFO", f"    disk command 'disk-statistics' succeeded on {ip}: {len(disk_rows_stats)} rows")
                        if disk_rows_stats:
                            bts = set(r.get("basetype") for r in disk_rows_stats)
                            log("DEBUG", f"    disk-statistics: {len(disk_rows_stats)} rows, basetypes={bts}")
                            sample = disk_rows_stats[0]
                            log("DEBUG", f"    disk-statistics sample keys: {list(sample.keys())}")
                        break
                    except Exception as exc:
                        msg = str(exc)
                        if "Rates may vary" in msg or "STORAGE_RATE_LIMIT" in msg:
                            log("WARN", f"  Rate-limit on show disk-statistics ({ip}), retrying ...")
                            try: storage.logout()
                            except Exception: pass
                            time.sleep(10)
                            try:
                                storage.login(); time.sleep(5)
                            except Exception as le:
                                log("WARN", f"  Re-login failed for {ip}: {le}")
                                break
                        else:
                            log("WARN", f"  show disk-statistics failed on {ip}: {exc}")
                            break

                # 3. Merge: build a {serial: merged_row} dict from both sources.
                #    Fields from "show disks" (rich) take precedence; fields
                #    from "disk-statistics" fill the gaps (serial, location,
                #    I/O stats, power-on-hours).
                merged_by_serial = {}
                # Start with disk-statistics (always available on MSA 2040)
                if disk_rows_stats:
                    for row in disk_rows_stats:
                        serial = (row.get("serial-number") or "").strip()
                        if serial:
                            merged_by_serial[serial] = dict(row)
                # Overlay "show disks" rows (richer data wins)
                if disk_rows_full:
                    for row in disk_rows_full:
                        serial = (row.get("serial-number") or "").strip()
                        if not serial:
                            continue
                        if serial in merged_by_serial:
                            # Merge: disks fields override, stats fields fill gaps
                            base = merged_by_serial[serial]
                            for k, v in row.items():
                                if v and not base.get(k):
                                    base[k] = v
                            # Ensure the richer basetype is used for filtering
                            base["basetype"] = row.get("basetype") or base.get("basetype")
                        else:
                            merged_by_serial[serial] = dict(row)

                if not merged_by_serial:
                    # Last resort: try disk-parameters (global params only,
                    # but some firmwares emit per-disk rows here too)
                    try:
                        dp_rows = storage.show("disk-parameters")
                        for row in dp_rows:
                            serial = (row.get("serial-number") or "").strip()
                            if serial:
                                merged_by_serial[serial] = dict(row)
                    except Exception as exc:
                        log("WARN", f"  show disk-parameters failed on {ip}: {exc}")

                # 4. Enrich merged rows with inferred fields from enclosures
                #    and disk-groups. This adds drive-bus-type and
                #    array-drive-type to rows that only have disk-statistics data.
                if enriched_drive_bus or disk_group_types:
                    for serial, row in merged_by_serial.items():
                        # Drive bus type (SAS) from controller
                        if enriched_drive_bus and not row.get("drive-bus-type"):
                            row["drive-bus-type"] = enriched_drive_bus
                        # If no drive-type/disk-type yet, try to infer from
                        # disk-group names. The location field (e.g. "1.1")
                        # doesn't map to disk-groups directly, but we can set
                        # a default type from the most common disk-group type.
                        if not row.get("drive-type") and not row.get("disk-type"):
                            # Use the first disk-group's array-drive-type as
                            # a reasonable default (most disks are SAS on MSA 2040)
                            for dg_name, dg_info in disk_group_types.items():
                                if "SSD" in dg_name.upper():
                                    row["inferred-ssd"] = True
                                elif "HDD" in dg_name.upper():
                                    row["inferred-ssd"] = False
                                if dg_info.get("array-drive-type") and not row.get("drive-type"):
                                    row["drive-type"] = dg_info["array-drive-type"]
                                break  # just use the first group as default

                rows = list(merged_by_serial.values()) if merged_by_serial else None
                if rows is None:
                    log("WARN", f"  All disk commands failed on {ip} -- disks will not be synced")
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
                    # Accept any basetype that represents a physical disk:
                    #   MSA 2060: "drive"
                    #   MSA 2040: "disk-statistics", "disk-parameters"
                    bt_lower = bt.lower()
                    if "drive" not in bt_lower and "disk" not in bt_lower:
                        continue
                    # Skip global/non-per-disk rows (e.g. disk-parameters on
                    # MSA 2040 returns a single row of global params with no
                    # serial-number -- we only want rows that represent an
                    # actual disk with an identity).
                    if not (row.get("serial-number") or row.get("durable-id")):
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
    # Collect every useful field a merged "show disks" + "show disk-statistics"
    # row can expose. On MSA 2040, size/model/firmware are unavailable (show
    # disks is permanently rate-limited), but drive-bus-type (SAS) and
    # array-drive-type are inferred from enclosures + disk-groups and injected
    # into the row by storage_collect_inventory before this is called.
    serial = row.get("serial-number")
    if not serial:
        return 0

    # Size / capacity -- try every known field name
    size_str = (row.get("size") or row.get("total-size")
                or row.get("formatted-size") or row.get("raw-size")
                or row.get("capacity") or row.get("disk-size"))
    size_num = (row.get("size-numeric") or row.get("total-size-numeric")
                or row.get("raw-size-numeric") or row.get("capacity-numeric"))
    cap = parse_storage_size_bytes(size_str, size_num)

    # Model / part number
    model = (row.get("model") or row.get("disk-description")
             or row.get("description") or row.get("product-id")
             or row.get("vendor-product-id"))

    # Manufacturer / vendor
    vendor = (row.get("vendor") or row.get("manufacturer")
              or row.get("vendor-name") or DEFAULT_MFR)

    # Firmware version
    firmware = (row.get("firmware-version") or row.get("firmware")
                or row.get("drive-firmware") or row.get("sc-firmware"))

    # Interface / type -- use explicit field, or inferred drive-bus-type
    drive_type = (row.get("drive-type") or row.get("disk-type")
                  or row.get("type") or row.get("drive-form-factor")
                  or row.get("interface"))
    drive_bus = row.get("drive-bus-type")   # inferred from controller

    # Use inferred-ssd flag if present (from disk-group name matching)
    if row.get("inferred-ssd") is True:
        role_id = get_or_create_inventory_role("SSD")
    elif row.get("inferred-ssd") is False:
        role_id = get_or_create_inventory_role("HDD")
    else:
        role_id = get_or_create_inventory_role("SSD") \
                  if is_ssd_storage(row) else get_or_create_inventory_role("HDD")

    # Location / slot
    location = (row.get("location") or row.get("slot")
                or row.get("durable-id"))
    health = (row.get("health") or row.get("disk-state")
              or row.get("status") or row.get("health-reason"))

    # I/O stats from disk-statistics (MSA 2040)
    extra = []
    if row.get("power-on-hours"):
        extra.append(f"PowerOnHours={row.get('power-on-hours')}")
    if row.get("data-read"):
        extra.append(f"Read={row.get('data-read')}")
    if row.get("data-written"):
        extra.append(f"Written={row.get('data-written')}")
    if row.get("iops"):
        extra.append(f"IOPS={row.get('iops')}")
    if row.get("number-of-reads"):
        extra.append(f"Reads={row.get('number-of-reads')}")
    if row.get("number-of-writes"):
        extra.append(f"Writes={row.get('number-of-writes')}")
    if row.get("number-of-media-errors-1"):
        extra.append(f"MediaErrors={row.get('number-of-media-errors-1')}")
    if row.get("number-of-nonmedia-errors-1"):
        extra.append(f"NonMediaErrors={row.get('number-of-nonmedia-errors-1')}")

    # Build a rich description with every field we found (or inferred)
    desc_parts = [f"Location={location}"]
    if model:       desc_parts.append(f"Model={model}")
    if size_str:    desc_parts.append(f"Size={size_str}")
    if drive_type:  desc_parts.append(f"Type={drive_type}")
    elif drive_bus: desc_parts.append(f"Bus={drive_bus}")
    if firmware:    desc_parts.append(f"FW={firmware}")
    if health:      desc_parts.append(f"Health={health}")
    if vendor and vendor != DEFAULT_MFR:
        desc_parts.append(f"Vendor={vendor}")
    if extra:
        desc_parts.append(" ".join(extra))

    add_item(
        name=name_storage_disk(row),
        manufacturer=vendor,
        part_number=model,
        serial=serial,
        description=" ".join(desc_parts)[:200],
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
        role_id=get_or_create_inventory_role("Controller"),
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
        role_id=get_or_create_inventory_role("PSU"),
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
        role_id=get_or_create_inventory_role("SAS Exp"),
    )
    return 0
