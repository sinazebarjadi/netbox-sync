"""Redfish (HPE iLO) session, probing and per-server inventory collection."""
import time

import requests

from netbox_sync.config import REDFISH_USER, REDFISH_PASS, REDFISH_PORT, log
from netbox_sync.models import SERVER_MODEL_MAP
from netbox_sync.netbox import get_or_create_inventory_role
from netbox_sync.utils import (normalize_model, gib_from_bytes, _to_int,
                               _capacity_to_bytes, _pick, _make_add_item,
                               _get_location, _get_oem, _chassis_url,
                                is_port_open, name_cpu, name_ram, name_disk,
                                name_psu, name_nic, name_hba, is_ssd)
from netbox_sync.report import classify_error, record_probe_failure


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

def probe_redfish(ip, retries=3, retry_delay=5):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, REDFISH_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Server", ip, "unreachable",
                                 f"port {REDFISH_PORT} closed or timed out")
            return None
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
        except Exception as exc:
            if attempt < retries: time.sleep(retry_delay); continue
            record_probe_failure("Server", ip, "no data", classify_error(exc))
    return None


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
                    role_id=get_or_create_inventory_role("CPU"))
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
                    role_id=get_or_create_inventory_role("Memory"))
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
                        role_id=get_or_create_inventory_role("Controller"))
                for d in (stor.get("Drives") or []):
                    drv = rf.get(d["@odata.id"])
                    if not isinstance(drv, dict): continue
                    if (drv.get("Status") or {}).get("State") == "Absent": continue
                    cap = _capacity_to_bytes(drv)
                    if cap: disk_total_bytes += cap
                    role_id = get_or_create_inventory_role("SSD") if is_ssd(drv) \
                          else get_or_create_inventory_role("HDD")
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
                            role_id=get_or_create_inventory_role("Controller"))
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
                            role_id = get_or_create_inventory_role("SSD") \
                                      if is_ssd(drv) else get_or_create_inventory_role("HDD")
                            add_item(
                                name=name_disk(drv),
                                manufacturer=drv.get("Manufacturer"),
                                part_number=drv.get("PartNumber") or drv.get("Model"),
                                serial=drv.get("SerialNumber"),
                                description=f"Model={drv.get('Model')} CapacityGB={drv.get('CapacityGB')} "
                                            f"MediaType={drv.get('MediaType')}",
                                role_id=role_id)
                            drive_idx += 1
                except Exception as exc:
                    log("DEBUG", f"  SmartStorage fallback skipped on {host}: {exc}")

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
                            role_id=get_or_create_inventory_role("PSU"))
        except Exception as exc:
            log("DEBUG", f"  PSU collection skipped on {host}: {exc}")

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
                role_id=get_or_create_inventory_role("Battery"))

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
                        role_id=get_or_create_inventory_role("Battery"))
        except Exception as exc:
            log("DEBUG", f"  Battery (Gen10) collection skipped on {host}: {exc}")

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
                        role_id=get_or_create_inventory_role("NIC"))
                except Exception: pass
        except Exception as exc:
            log("DEBUG", f"  NIC collection skipped on {host}: {exc}")

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
                        role_id = get_or_create_inventory_role("HBA") \
                                  if any(k in dname for k in ("HBA","FC","Fibre")) \
                                  else get_or_create_inventory_role("NIC")
                        add_item(
                            name=dname[:64],
                            manufacturer=dev.get("Manufacturer") or sys.get("Manufacturer"),
                            part_number=dev.get("ProductPartNumber") or dev.get("PartNumber"),
                            serial=serial,
                            description=f"ProductVersion={dev.get('ProductVersion')} "
                                        f"FirmwareVersion={dev.get('FirmwareVersion')}",
                            role_id=role_id)
                    except Exception: pass
        except Exception as exc:
            log("DEBUG", f"  PCIe FRU collection skipped on {host}: {exc}")

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
                    role_id=get_or_create_inventory_role("HBA"))
        except Exception as exc:
            log("DEBUG", f"  HBA pseudo-serial collection skipped on {host}: {exc}")

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
