"""run_sync: the main reconciliation job — scan, ensure devices, collect and
sync inventory, sync SAN interfaces, then mark unreachable devices offline."""
import time

from netbox_sync.collectors.brocade import san_collect_inventory, sync_san_interfaces
from netbox_sync.collectors.cisco import (cisco_collect_inventory,
                                          sync_cisco_interfaces,
                                          ensure_vlan_group,
                                          sync_cisco_vlans,
                                          sync_interface_vlans,
                                          ensure_svi_interface,
                                          sweep_stale_vlans,
                                          sweep_legacy_site_vlans,
                                          sync_cdp_cables,
                                          sync_camera_cable,
                                          build_mac_map,
                                          _site_vlan_index,
                                          _mac_to_cisco,
                                          _cisco_mac_lookup,
                                          _norm_sw_name,
                                          _broadcast_components,
                                          _component_key,
                                          _sweep_stale_groups)
from netbox_sync.collectors.fortigate import (fortigate_collect,
                                              sync_fortigate_interfaces,
                                              resolve_fortigate_vlans,
                                              _fortigate_iface_mac)
from netbox_sync.collectors.hikvision import hikvision_collect
from netbox_sync.collectors.dahua import dahua_collect
from netbox_sync.collectors.unv import unv_collect
from netbox_sync.collectors.msa import storage_collect_inventory
from netbox_sync.collectors.redfish import rf_collect_inventory
from netbox_sync.collectors.ruckus import (ruckus_collect, probe_ruckus,
                                           _ruckus_role_and_cluster,
                                           _parse_ha_map)
from netbox_sync.collectors.unifi import unifi_collect
from netbox_sync.config import (log, BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES, FORTIGATE_RANGES, RUCKUS_RANGES,
                                RUCKUS_HA_MAP, HIKVISION_RANGES, UNIFI_RANGES,
                                DAHUA_RANGES, UNV_RANGES)
from netbox_sync.ipam import (_prefix_from_ip, _iface_addr_with_prefixlen,
                              ensure_prefix, ensure_host_ip,
                              _containing_prefix, _prefix_masklen,
                              sweep_stale_prefixes, sweep_stale_host_ips,
                              sync_nat_ips, sweep_nat_ips,
                              sync_nat_services, sweep_nat_services,
                              sync_parent_prefixes, sweep_stale_parents)
from netbox_sync.netbox import (get_netbox, ensure_server_device,
                                ensure_storage_device, ensure_san_switch_device,
                                ensure_cisco_device, ensure_fortigate_device,
                                ensure_ruckus_device, ensure_ap_device,
                                ensure_hikvision_device, ensure_camera_device,
                                ensure_dahua_device, ensure_unv_device,
                                ensure_camera_interface,
                                CAMERA_IFACE_NAME,
                                ensure_primary_ip,
                                mark_server_offline, mark_storage_offline,
                                mark_san_offline, mark_cisco_offline,
                                mark_fortigate_offline, mark_ap_offline,
                                mark_ruckus_offline, mark_hikvision_offline,
                                mark_dahua_offline, mark_unv_offline,
                                mark_camera_offline, mark_unifi_offline,
                                ensure_unifi_console, get_or_create_site,
                                sync_wireless_lans, sweep_wireless_lans,
                                ensure_custom_fields_if_set,
                                ensure_custom_fields,
                                _check_offline,
                                sync_inventory)
from netbox_sync.scanner import scan_all
from netbox_sync.utils import resolve_site


def sync_unifi_wlans(data, console_name, desc_site_votes, site_indexes,
                     group_vlan_seen, legacy_sites):
    """Aggregate a UniFi console's WLANs console-globally (unique SSIDs,
    first site wins) and resolve VLAN bindings per site (first unique match
    wins; missing VLANs are created in the majority-AP site)."""
    desc_by_name = {s["name"]: s["desc"] for s in data["sites"]}
    # UniFi site -> NetBox site: majority of its APs' resolved sites.
    desc_site = {d: max(v, key=v.get)
                 for d, v in desc_site_votes.items()}
    site_id_by_name = {}
    wlan_by_ssid = {}
    for sname, wlist in (data["wlans"] or {}).items():
        for w in wlist:
            entry = wlan_by_ssid.setdefault(
                w["ssid"], dict(w, vlan_id=None, _bindings=[]))
            vid = (data["networks"].get(sname) or {}).get(
                w.get("networkconf_id"))
            if vid:
                entry["_bindings"].append((desc_by_name.get(sname), vid))
    vid_map = {}
    missing = {}   # netbox site name -> [{vid, name, status}]
    for entry in wlan_by_ssid.values():
        for desc, vid in entry["_bindings"]:
            site_name = desc_site.get(desc)
            if not site_name:
                continue   # no APs at that UniFi site -> can't place
            site_id = site_id_by_name.get(site_name)
            if site_id is None:
                site_id = get_or_create_site(site_name)
                site_id_by_name[site_name] = site_id
            site_index = site_indexes.get(site_id)
            if site_index is None:
                site_index = _site_vlan_index(site_id)
                site_indexes[site_id] = site_index
            matches = site_index.get(vid, [])
            if len(matches) == 1:
                entry["vlan_id"] = vid
                vid_map[vid] = matches[0][1]
                break
        else:
            if entry["_bindings"]:
                desc, vid = entry["_bindings"][0]
                site_name = desc_site.get(desc)
                if site_name:
                    entry["vlan_id"] = vid
                    missing.setdefault(site_name, []).append(
                        {"vid": vid, "name": f"VLAN{vid:04d}",
                         "status": "active"})
    for site_name, vlans in missing.items():
        site_id = site_id_by_name[site_name]
        group_id = ensure_vlan_group(site_id, console_name)
        created = sync_cisco_vlans(group_id, console_name, vlans)
        vid_map.update(created)
        group_vlan_seen.setdefault(group_id, set()).update(created.keys())
        legacy_sites.add(site_id)
    seen_ssids = sync_wireless_lans(console_name,
                                    list(wlan_by_ssid.values()),
                                    vid_map, group_prefix="UniFi")
    sweep_wireless_lans(console_name, seen_ssids)
    log("INFO", f"  [OK] UniFi {data['summary'].get('reported_ip')} — "
                f"{len(data['aps'])} APs, "
                f"{len(seen_ssids)} WLANs, "
                f"{len(data['sites'])} sites synced")


# NVR names used as cam_nvr linkage keys must be unique per NVR. Hikvision
# NVRs with an unconfigured deviceName all report "Network Video Recorder" —
# sharing one key made each NVR's camera sweep offline the OTHER NVRs'
# cameras (219 cameras mass-offlined on 2026-08-11).
_GENERIC_NVR_NAMES = {"", "nvr", "network video recorder"}


def _unique_nvr_name(raw_name, hostname, ip, family):
    raw = (raw_name or "").strip()
    if raw.lower() in _GENERIC_NVR_NAMES:
        raw = (hostname or "").strip()
    if raw.lower() in _GENERIC_NVR_NAMES:
        raw = f"{family.lower()}-nvr-{ip.replace('.', '-')}"
    return raw


def process_nvrs(probes, collect_fn, ensure_fn, family, mac_map,
                 switch_by_ip, api):
    """Process one NVR family end to end (Hikvision / Dahua / Uniview):
    collect, ensure the NVR device, refresh nvr_* fields + primary IP, then
    sync each camera (device, eth0, primary IP, MAC-table cable) and sweep
    cameras that disappeared."""
    live_ips = set()
    for probe in probes:
        ip = probe["ip"]
        live_ips.add(ip)
        log("INFO", f"Processing {family} NVR {ip}  "
                    f"({probe.get('model')} / {probe.get('serial')})")
        data = None
        for collect_attempt in (1, 2):
            try:
                data = collect_fn(ip)
                break
            except KeyboardInterrupt: raise
            except Exception as e:
                if collect_attempt == 1:
                    # flaky WAN links: probe succeeded but collect timed out —
                    # one retry before giving up on the NVR for this run
                    log("WARN", f"  {family} collection failed for {ip} "
                                f"({e}) — retrying in 5s")
                    time.sleep(5)
                else:
                    log("ERROR", f"  {family} collection failed for {ip}: {e}")
        if data is None:
            # collection failed (restricted account, flaky link, ...), but the
            # probe succeeded — still ensure the NVR device itself so it shows
            # up in NetBox; cameras sync on a later successful run. The camera
            # sweep stays safe: no channel/serial evidence => it skips.
            try:
                dev_id = ensure_fn(probe)
                cf = {"nvr_ip": ip, "nvr_enabled": True,
                      "nvr_model": probe.get("model"),
                      "nvr_firmware": probe.get("firmware")}
                api.dcim.devices.update([{"id": dev_id, "status": "active",
                                          "custom_fields": cf}])
                nvr_name = _unique_nvr_name(None, probe.get("hostname"), ip, family)
                ensure_primary_ip(dev_id, ip, nvr_name)
            except Exception as e:
                log("ERROR", f"  ensure device failed for {ip}: {e}")
            continue

        try:
            dev_id = ensure_fn(probe)
        except Exception as e:
            log("ERROR", f"  ensure device failed for {ip}: {e}"); continue

        nvr_name = _unique_nvr_name(data["summary"].get("name"),
                                    probe.get("hostname"), ip, family)
        try:
            cf = {"nvr_ip": ip, "nvr_enabled": True,
                  "nvr_model": data["summary"].get("model") or probe.get("model"),
                  "nvr_firmware": data["summary"].get("firmware") or probe.get("firmware"),
                  "nvr_camera_count": len(data["cameras"])}
            api.dcim.devices.update([{"id": dev_id, "status": "active",
                                      "custom_fields": cf}])
        except Exception as e:
            log("ERROR", f"  NVR update failed for {ip}: {e}")

        try:
            ensure_primary_ip(dev_id, ip, nvr_name)
        except Exception as e:
            log("WARN", f"  NVR primary IPv4 sync failed for {ip}: {e}")

        # Cameras -> separate devices, linked to the NVR via cam_nvr.
        seen_camera_serials = set()
        seen_camera_channels = set()
        for cam in data["cameras"]:
            serial = (cam.get("serial") or "").strip()
            if serial:
                # what the NVR reported — independent of sync success, so a
                # failed ensure can never cause a false offline marking
                seen_camera_serials.add(serial)
            if cam.get("channel") is not None:
                # channel presence alone proves the camera is still attached —
                # a serial fetch failure (NVR rate-limit) must never offline it
                seen_camera_channels.add(str(cam["channel"]))
            try:
                cam_dev = ensure_camera_device(
                    cam, nvr_name, manufacturer=cam.get("manufacturer"))
                cam_iface = None
                if mac_map:
                    try:
                        cam_iface = ensure_camera_interface(
                            cam_dev, bool(cam.get("online")))
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} interface sync failed: {e}")
                if cam.get("ip"):
                    try:
                        ensure_primary_ip(
                            cam_dev, cam["ip"], cam.get("name"),
                            iface_name=CAMERA_IFACE_NAME if cam_iface else None)
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} primary IP failed: {e}")
                if cam_iface and cam.get("mac") and mac_map:
                    try:
                        sync_camera_cable(cam_dev, cam.get("name"), cam_iface,
                                          cam["mac"], mac_map, switch_by_ip)
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} cable sync failed: {e}")
            except Exception as e:
                log("ERROR", f"  camera sync failed for ch{cam.get('channel')}: {e}")

        # Cameras no longer reported by this NVR -> offline (never deleted).
        # "Reported" = serial seen OR channel still listed (a serial-fetch
        # failure on a rate-limited NVR must not offline a live camera).
        try:
            for d in list(api.dcim.devices.filter(cf_cam_nvr=nvr_name,
                                                  cf_cam_enabled=True)):
                serial = (d.custom_fields or {}).get("cam_serial") or ""
                # fall back to the device serial field when cam_serial unset
                if not serial:
                    serial = (d.serial or "").strip()
                ch = str((d.custom_fields or {}).get("cam_channel") or "")
                if ch and ch in seen_camera_channels:
                    continue
                if serial and serial not in seen_camera_serials:
                    mark_camera_offline(d.id, d.name)
        except Exception as e:
            log("ERROR", f"  camera offline sweep failed for {ip}: {e}")

        log("INFO", f"  [OK] {family} NVR {ip} — {len(data['cameras'])} cameras synced")
    return live_ips


def run_sync():
    log("INFO", "=" * 60)
    log("INFO", "Unified sync started (servers + storage + SAN + Cisco switches)")
    log("INFO", "=" * 60)

    # Custom fields MUST exist before anything runs: every lookup/sweep uses
    # cf_* filters, and NetBox silently ignores filters on nonexistent fields
    # (matches ALL devices — mass-offlined a fresh NetBox once).
    try:
        ensure_custom_fields()
    except Exception as e:
        log("ERROR", f"  custom-field bootstrap failed: {e}")

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
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

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
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

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

    # ── Process SAN switches ──────────────────────────────────────────────────
    live_san_ips = {h["ip"] for h in found["san_switches"]}
    for probe in found["san_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing SAN SWITCH {ip}  ({probe.get('model')} / wwn={probe.get('wwn')})")

        try:
            dev_id = ensure_san_switch_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_san_switch_device failed for {ip}: {e}"); continue

        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

        try:
            data = san_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  SAN inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        nameserver = data["nameserver"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "san_switch_ip":        ip,
                    "san_switch_enabled":   True,
                    "san_switch_wwn":       summary.get("wwn") or probe.get("wwn"),
                    "san_switch_firmware":  summary.get("firmware") or probe.get("firmware"),
                    "san_switch_model":     summary.get("model") or probe.get("model"),
                    "san_switch_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  SAN switch update failed for {ip}: {e}")

        try:
            sync_san_interfaces(dev_id, ports, nameserver)
            log("INFO", f"  [OK] SAN {ip} — {len(ports)} ports, {len(nameserver)} nameserver entries")
        except Exception as e:
            log("ERROR", f"  SAN interface sync failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] SAN {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  SAN inventory sync failed for {ip}: {e}")

    # ── Process Cisco switches ────────────────────────────────────────────────
    live_cisco_ips = {h["ip"] for h in found["cisco_switches"]}
    group_vlan_seen = {}
    switch_group_ips = {}
    site_indexes = {}
    site_prefix_seen = {}
    legacy_sites = set()

    # Pass 1: ensure devices and collect everything — broadcast domains are
    # derived from the CDP topology, which needs every switch's data first.
    collected = []
    for probe in found["cisco_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing CISCO {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_cisco_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_cisco_device failed for {ip}: {e}"); continue

        try:
            data = cisco_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  Cisco inventory collection failed for {ip}: {e}"); continue

        collected.append((probe, dev_id, data))

    # Build the CDP topology: nodes = switches, edges = same-site adjacency.
    norm_of_ip = {}
    site_of_ip = {}
    for probe, dev_id, data in collected:
        ip = probe["ip"]
        norm_of_ip[ip] = _norm_sw_name(probe.get("hostname"))
        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_of_ip[ip] = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_of_ip[ip] = None
    name_to_ip = {n: ip for ip, n in norm_of_ip.items() if n}
    edges = []
    for probe, dev_id, data in collected:
        a_ip = probe["ip"]
        a = norm_of_ip[a_ip]
        for n in data["neighbors"]:
            b = _norm_sw_name(n.get("device_id"))
            if b and b in name_to_ip \
                    and site_of_ip.get(a_ip) == site_of_ip.get(name_to_ip[b]):
                edges.append((a, b))
    key_by_name = {}
    for members in _broadcast_components(set(norm_of_ip.values()), edges):
        vtp_by_name = {norm_of_ip[p["ip"]]: (d["vtp"].get("domain") or "")
                       for p, _, d in collected if norm_of_ip[p["ip"]] in members}
        key = _component_key(members, vtp_by_name)
        for m in members:
            key_by_name[m] = key
    if key_by_name:
        log("INFO", f"  Broadcast domains from CDP topology: "
                    f"{sorted(set(key_by_name.values()))}")

    # Pass 2: process each switch with its component's group
    for probe, dev_id, data in collected:
        ip = probe["ip"]
        summary = data["summary"]
        ports = data["ports"]
        neighbors = data["neighbors"]
        vlans = data["vlans"]
        trunks = data["trunks"]
        vtp = data["vtp"]
        ip_brief = data["ip_brief"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "cisco_ip":         ip,
                    "cisco_enabled":    True,
                    "cisco_firmware":   summary.get("firmware") or probe.get("firmware"),
                    "cisco_model":      summary.get("model") or probe.get("model"),
                    "cisco_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  Cisco switch update failed for {ip}: {e}")

        site_id = site_of_ip.get(ip)
        vid_map = {}
        if site_id:
            try:
                key = key_by_name.get(norm_of_ip[ip]) or norm_of_ip[ip] or ip
                group_id = ensure_vlan_group(site_id, key)
                vid_map = sync_cisco_vlans(group_id, probe.get("hostname") or "", vlans)
                group_vlan_seen.setdefault(group_id, set()).update(vid_map.keys())
                switch_group_ips.setdefault(group_id, []).append(probe["ip"])
                legacy_sites.add(site_id)
            except Exception as e:
                log("WARN", f"  VLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")

        try:
            sync_cisco_interfaces(dev_id, ports)
            log("INFO", f"  [OK] Cisco {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  Cisco interface sync failed for {ip}: {e}")

        if vid_map:
            try:
                sync_interface_vlans(dev_id, ports, trunks, vid_map)
                log("INFO", f"  [OK] Cisco {ip} — VLAN linkage synced")
            except Exception as e:
                log("ERROR", f"  Cisco VLAN linkage failed for {ip}: {e}")

        # Primary IPv4 goes on the real management interface (SVI from
        # show ip interface brief) when identifiable; synthetic mgmt fallback.
        carrier = ip_brief.get(ip)
        if carrier:
            if carrier not in {p["port"] for p in ports}:
                try:
                    ensure_svi_interface(dev_id, carrier, vid_map)
                except Exception as e:
                    log("WARN", f"  SVI creation failed for {carrier} on {ip}: {e}")
        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"),
                              iface_name=carrier)
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

        # IPAM: SVI host addresses inside their containing prefixes
        seen_host_ips = set()
        try:
            for iface_name, addr in ip_brief.items():
                pfx_rec = _containing_prefix(addr)
                if not pfx_rec:
                    log("DEBUG", f"  SVI {iface_name} {addr}: no containing prefix — skipped")
                    continue
                if iface_name not in {p["port"] for p in ports} and iface_name != carrier:
                    try:
                        ensure_svi_interface(dev_id, iface_name, vid_map,
                                             mgmt_only=False)
                    except Exception as e:
                        log("WARN", f"  SVI {iface_name} creation failed for {ip}: {e}")
                masklen = _prefix_masklen(pfx_rec.prefix)
                ip_id = ensure_host_ip(dev_id, f"{addr}/{masklen}", iface_name,
                                       probe.get("hostname") or ip, iface_name)
                if ip_id:
                    seen_host_ips.add(addr)
            log("INFO", f"  [OK] Cisco {ip} — {len(seen_host_ips)} SVI host IPs synced")
        except Exception as e:
            log("WARN", f"  SVI host IP sync failed for {ip}: {e}")
        try:
            sweep_stale_host_ips(dev_id, seen_host_ips | ({ip} if carrier else set()))
        except Exception as e:
            log("ERROR", f"  host IP sweep failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Cisco {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  Cisco inventory sync failed for {ip}: {e}")

        try:
            sync_cdp_cables(dev_id, neighbors)
            log("INFO", f"  [OK] Cisco {ip} — {len(neighbors)} neighbors processed")
        except Exception as e:
            log("ERROR", f"  Cisco cable sync failed for {ip}: {e}")

    # MAC -> switch-port map for camera cabling. Empty when no Cisco
    # switches were scanned (family disabled) — cabling then no-ops.
    mac_map = build_mac_map(collected)
    switch_by_ip = {p["ip"]: {"dev_id": d, "name": p.get("hostname") or p["ip"]}
                    for p, d, _ in collected}
    if mac_map:
        log("INFO", f"  camera cabling: MAC map holds {len(mac_map)} entries "
                    f"from {len(collected)} switch(es)")

    # ── Process FortiGates ────────────────────────────────────────────────────
    live_fortigate_ips = {h["ip"] for h in found["fortigates"]}
    nat_seen = set()
    for probe in found["fortigates"]:
        ip = probe["ip"]
        log("INFO", f"Processing FORTIGATE {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            data = fortigate_collect(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  FortiGate inventory collection failed for {ip}: {e}"); continue

        ha = data.get("ha") or {}
        try:
            dev_id = ensure_fortigate_device(probe, ha=ha)
        except Exception as e:
            log("ERROR", f"  ensure_fortigate_device failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        vlans = data["vlans"]
        neighbors = data["neighbors"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "fortigate_ip":         ip,
                    "fortigate_enabled":    True,
                    "fortigate_firmware":   summary.get("firmware") or probe.get("firmware"),
                    "fortigate_model":      summary.get("model") or probe.get("model"),
                    "fortigate_port_count": summary.get("port_count"),
                },
            }
            # Only the primary unit may stamp the cluster device serial
            if summary.get("serial") and (not ha.get("clustered")
                                          or (probe.get("serial") or "")
                                          == (ha.get("primary_serial") or "")):
                payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  FortiGate update failed for {ip}: {e}")

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

                def _get_mac(vid):
                    name = next((v["name"] for v in vlans if v["vid"] == vid),
                                None)
                    if not name: return None
                    try:
                        return _fortigate_iface_mac(ip, name)
                    except Exception:
                        return None

                def _mac_lookup(vid, mac):
                    if not mac: return None
                    cmac = _mac_to_cisco(mac)
                    if not cmac: return None
                    for cand_gid, _vlan_id in site_index.get(vid, []):
                        for sw_ip in switch_group_ips.get(cand_gid, []):
                            try:
                                if vid in _cisco_mac_lookup(sw_ip, cmac):
                                    return cand_gid
                            except Exception:
                                continue
                    return None

                vid_map, missing = resolve_fortigate_vlans(
                    site_index, vlans, _get_mac, _mac_lookup)
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

        try:
            sync_fortigate_interfaces(dev_id, ports, vid_map)
            log("INFO", f"  [OK] FortiGate {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  FortiGate interface sync failed for {ip}: {e}")

        # IPAM: prefixes from interface IPs (real masks) + gateway addresses
        if site_id:
            seen_pfx = site_prefix_seen.setdefault(site_id, set())
            hostname = probe.get("hostname") or ip
            try:
                for p in ports:
                    pfx = _prefix_from_ip(p.get("ip"))
                    if not pfx:
                        continue
                    ensure_prefix(pfx, site_id, vid_map.get(p.get("vlanid")),
                                  hostname, p["name"])
                    seen_pfx.add(pfx)
                log("INFO", f"  [OK] FortiGate {ip} — {len(seen_pfx)} prefixes synced")
            except Exception as e:
                log("WARN", f"  IPAM prefix sync failed for {ip}: {e}")
            seen_host_ips = set()
            try:
                for p in ports:
                    addr_masked, bare = _iface_addr_with_prefixlen(p.get("ip"))
                    if not addr_masked:
                        continue
                    ip_id = ensure_host_ip(dev_id, addr_masked, p["name"],
                                           hostname, p["name"])
                    if ip_id:
                        seen_host_ips.add(bare)
                log("INFO", f"  [OK] FortiGate {ip} — {len(seen_host_ips)} gateway IPs synced")
            except Exception as e:
                log("WARN", f"  gateway IP sync failed for {ip}: {e}")
            try:
                sweep_stale_host_ips(dev_id, seen_host_ips | {ip})
            except Exception as e:
                log("ERROR", f"  host IP sweep failed for {ip}: {e}")

        # Primary IPv4 goes on the real carrier subinterface (matched from
        # cmdb interface IPs) when identifiable; synthetic mgmt fallback.
        # On HA clusters, only the primary unit may set the cluster's primary
        # IPv4 — a secondary probe never repoints it.
        _is_primary_probe = (not ha.get("clustered")
                             or (probe.get("serial") or "")
                             == (ha.get("primary_serial") or ""))
        if _is_primary_probe:
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
        else:
            log("INFO", f"  {ip} is the secondary HA unit — primary IPv4 follows the primary")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] FortiGate {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  FortiGate inventory sync failed for {ip}: {e}")

        # NAT: VIPs become external IPs with nat_inside to their mapped
        # addresses (per-port fidelity via NetBox Services); pools plain
        fw = data.get("firewall") or {}
        try:
            nat_seen.update(sync_nat_ips(fw.get("vips"), fw.get("ippools")))
            svc_seen = sync_nat_services(dev_id, fw.get("vips"))
            sweep_nat_services(dev_id, svc_seen)
            log("INFO", f"  [OK] FortiGate {ip} — {len(fw.get('vips') or [])} vips, "
                        f"{len(fw.get('ippools') or [])} pools -> IPAM + services")
        except Exception as e:
            log("ERROR", f"  NAT IPAM sync failed for {ip}: {e}")

        try:
            sync_cdp_cables(dev_id, neighbors, protocol="lldp")
            log("INFO", f"  [OK] FortiGate {ip} — {len(neighbors)} neighbors processed")
        except Exception as e:
            log("ERROR", f"  FortiGate cable sync failed for {ip}: {e}")

    if found["fortigates"]:
        try:
            sweep_nat_ips(nat_seen)
        except Exception as e:
            log("ERROR", f"  NAT IP sweep failed: {e}")

    # ── Process Ruckus ZoneDirectors ──────────────────────────────────────────
    live_ruckus_ips = {h["ip"] for h in found["ruckus"]}
    ruckus_ha_map = _parse_ha_map(RUCKUS_HA_MAP)
    for probe in found["ruckus"]:
        ip = probe["ip"]
        log("INFO", f"Processing RUCKUS {ip}  ({probe.get('model')} / {probe.get('serial')})")
        role, vip = _ruckus_role_and_cluster(ip, ruckus_ha_map)
        try:
            data = ruckus_collect(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  Ruckus collection failed for {ip}: {e}"); continue

        # cluster identity comes only from vip/primary probes
        eff_probe = probe if role != "secondary" else dict(probe, serial="")
        try:
            dev_id = ensure_ruckus_device(eff_probe, role, vip)
        except Exception as e:
            log("ERROR", f"  ensure_ruckus_device failed for {ip}: {e}"); continue

        wlc_name = (data["summary"].get("name") or probe.get("hostname") or ip)
        try:
            cf = {"wlc_ip": ip, "wlc_enabled": True,
                  "wlc_model": data["summary"].get("model") or probe.get("model"),
                  "wlc_firmware": data["summary"].get("version") or probe.get("firmware"),
                  "wlc_ap_count": len(data["aps"]),
                  "wlc_ha_role": role}
            if vip:
                cf["wlc_vip"] = vip
            payload = {"id": dev_id, "status": "active", "custom_fields": cf}
            if role != "secondary" and data["summary"].get("serial"):
                payload["serial"] = data["summary"]["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  ZD update failed for {ip}: {e}")

        try:
            ensure_primary_ip(dev_id, vip or probe.get("reported_ip") or ip,
                              wlc_name)
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

        seen_macs = set()
        for ap in data["aps"]:
            try:
                ap_dev = ensure_ap_device(ap, wlc_name)
                seen_macs.add(ap["mac"])
                if ap.get("ip"):
                    try:
                        ensure_primary_ip(ap_dev, ap["ip"], ap.get("name"))
                    except Exception as e:
                        log("WARN", f"  AP {ap.get('name')} primary IP failed: {e}")
            except Exception as e:
                log("ERROR", f"  AP sync failed for {ap.get('mac')}: {e}")
        try:
            for d in list(api.dcim.devices.filter(cf_wap_wlc=wlc_name,
                                                  cf_wap_enabled=True)):
                mac = (d.custom_fields or {}).get("wap_mac")
                if mac and mac not in seen_macs:
                    mark_ap_offline(d.id, d.name)
        except Exception as e:
            log("ERROR", f"  AP offline sweep failed for {ip}: {e}")

        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_id = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_id = None
        if site_id:
            try:
                site_index = site_indexes.get(site_id)
                if site_index is None:
                    site_index = _site_vlan_index(site_id)
                    site_indexes[site_id] = site_index
                vid_map = {}
                missing = []
                for w in data["wlans"]:
                    vid = w.get("vlan_id")
                    if not vid:
                        continue
                    matches = site_index.get(vid, [])
                    if len(matches) == 1:
                        vid_map[vid] = matches[0][1]
                    else:
                        missing.append({"vid": vid,
                                        "name": w.get("name") or f"VLAN{vid:04d}",
                                        "status": "active"})
                if missing:
                    group_id = ensure_vlan_group(site_id, wlc_name)
                    created = sync_cisco_vlans(group_id, wlc_name, missing)
                    vid_map.update(created)
                    group_vlan_seen.setdefault(group_id, set()).update(created.keys())
                    legacy_sites.add(site_id)
                seen_ssids = sync_wireless_lans(wlc_name, data["wlans"], vid_map)
                sweep_wireless_lans(wlc_name, seen_ssids)
                log("INFO", f"  [OK] Ruckus {ip} — {len(data['aps'])} APs, "
                            f"{len(seen_ssids)} WLANs synced")
            except Exception as e:
                log("ERROR", f"  Ruckus WLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping WLAN sync")

    # ZD offline: a cluster is offline only when the VIP AND all its units
    # are unreachable; standalone ZDs go offline when their IP is missing.
    if RUCKUS_RANGES:
        try:
            for dev in list(api.dcim.devices.filter(cf_wlc_enabled=True)):
                cf = dev.custom_fields or {}
                vip = cf.get("wlc_vip")
                if vip:
                    units = ruckus_ha_map.get(vip, {})
                    live = vip in live_ruckus_ips \
                        or units.get("primary") in live_ruckus_ips \
                        or units.get("secondary") in live_ruckus_ips
                    if not live:
                        mark_ruckus_offline(dev.id, dev.name)
                elif cf.get("wlc_ip") and cf["wlc_ip"] not in live_ruckus_ips:
                    mark_ruckus_offline(dev.id, dev.name)
        except Exception as e:
            log("ERROR", f"  Ruckus offline sweep failed: {e}")

    # ── Process UniFi OS consoles ────────────────────────────────────────────
    # One console manages many sites. APs reuse the shared AP machinery
    # (wap_* fields; wap_group = UniFi site desc, wap_wlc = console name).
    # WLANs are aggregated console-globally (unique SSIDs, first site wins);
    # VLAN bindings are resolved per site, first unique match wins.
    live_unifi_ips = {h["ip"] for h in found["unifi"]}
    for probe in found["unifi"]:
        ip = probe["ip"]
        log("INFO", f"Processing UNIFI {ip}  ({probe.get('model')} / {probe.get('serial')})")
        try:
            data = unifi_collect(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  UniFi collection failed for {ip}: {e}"); continue

        try:
            dev_id = ensure_unifi_console(probe, ap_count=len(data["aps"]),
                                          site_count=len(data["sites"]))
        except Exception as e:
            log("ERROR", f"  ensure_unifi_console failed for {ip}: {e}"); continue

        console_name = (data["summary"].get("name") or probe.get("hostname") or ip)
        try:
            version = data["summary"].get("version") or probe.get("firmware")
            api.dcim.devices.update([{"id": dev_id, "status": "active",
                                      "custom_fields": {"unifi_version": version}}])
        except Exception as e:
            log("ERROR", f"  UniFi console update failed for {ip}: {e}")

        try:
            ensure_primary_ip(dev_id, ip, console_name)
        except Exception as e:
            log("WARN", f"  UniFi primary IPv4 sync failed for {ip}: {e}")

        # APs: NetBox site comes from the standard SITE_IP_MAP resolution
        # (longest-prefix on the AP's IP, then keyword, then default) — the
        # UniFi site desc is kept only in wap_group. Each UniFi site's
        # majority AP site is remembered for the VLAN resolution below.
        desc_site_votes = {}   # unifi site desc -> {netbox site name: count}
        seen_macs = set()
        for ap in data["aps"]:
            desc = ap.get("group") or ""
            try:
                ap_dev = ensure_ap_device(ap, console_name,
                                          manufacturer="Ubiquiti")
                seen_macs.add(ap["mac"])
                site_name = resolve_site(ap.get("name") or "",
                                         ap.get("ip") or "")
                votes = desc_site_votes.setdefault(desc, {})
                votes[site_name] = votes.get(site_name, 0) + 1
                if ap.get("ip"):
                    try:
                        ensure_primary_ip(ap_dev, ap["ip"], ap.get("name"))
                    except Exception as e:
                        log("WARN", f"  AP {ap.get('name')} primary IP failed: {e}")
            except Exception as e:
                log("ERROR", f"  UniFi AP sync failed for {ap.get('mac')}: {e}")
        try:
            for d in list(api.dcim.devices.filter(cf_wap_wlc=console_name,
                                                  cf_wap_enabled=True)):
                mac = (d.custom_fields or {}).get("wap_mac")
                if mac and mac not in seen_macs:
                    mark_ap_offline(d.id, d.name)
        except Exception as e:
            log("ERROR", f"  UniFi AP offline sweep failed for {ip}: {e}")

        try:
            sync_unifi_wlans(data, console_name, desc_site_votes,
                             site_indexes, group_vlan_seen, legacy_sites)
        except Exception as e:
            log("ERROR", f"  UniFi WLAN sync failed for {ip}: {e}")
    # ── Process NVRs (Hikvision / Dahua / Uniview) ───────────────────────────
    # The NVR is the device; each camera becomes its own device (serial is the
    # identity) with the parent NVR recorded in cam_nvr. Camera IPs are set as
    # primary IPs on the camera device (on eth0 when MAC cabling is active).
    # All three vendors share process_nvrs and the vendor-neutral nvr_*/cam_*
    # custom fields; offline sweeps are manufacturer-scoped (see below).
    live_hikvision_ips = process_nvrs(found["hikvision_nvrs"], hikvision_collect,
                                      ensure_hikvision_device, "Hikvision",
                                      mac_map, switch_by_ip, api)
    live_dahua_ips = process_nvrs(found["dahua_nvrs"], dahua_collect,
                                  ensure_dahua_device, "Dahua",
                                  mac_map, switch_by_ip, api)
    live_unv_ips = process_nvrs(found["unv_nvrs"], unv_collect,
                                ensure_unv_device, "UNV",
                                mac_map, switch_by_ip, api)

    # ── IPAM parent prefixes from SITE_IP_MAP (containers for discovered ones)
    try:
        parent_seen = sync_parent_prefixes()
        for site_id, pfxs in parent_seen.items():
            site_prefix_seen.setdefault(site_id, set()).update(pfxs)
        if parent_seen:
            log("INFO", f"  IPAM parent prefixes synced for {len(parent_seen)} site(s)")
    except Exception as e:
        log("WARN", f"  parent prefix sync failed: {e}")

    # ── Sweep stale marker-owned VLANs per group + legacy site VLANs ─────────
    # Prefixes first: marked stale prefixes may reference stale VLANs, and
    # NetBox blocks VLAN deletion while a prefix depends on it (409).
    for site_id in legacy_sites:
        try:
            sweep_stale_prefixes(site_id, site_prefix_seen.get(site_id, set()))
        except Exception as e:
            log("ERROR", f"  prefix sweep failed for site {site_id}: {e}")
    try:
        sweep_stale_parents()
    except Exception as e:
        log("ERROR", f"  parent prefix sweep failed: {e}")
    for group_id, seen in group_vlan_seen.items():
        try:
            sweep_stale_vlans(group_id, seen)
        except Exception as e:
            log("ERROR", f"  VLAN sweep failed for group {group_id}: {e}")
    for site_id in legacy_sites:
        try:
            sweep_legacy_site_vlans(site_id)
        except Exception as e:
            log("ERROR", f"  legacy VLAN sweep failed for site {site_id}: {e}")
        try:
            _sweep_stale_groups(site_id, set(group_vlan_seen.keys()), key_by_name)
        except Exception as e:
            log("ERROR", f"  stale group sweep failed for site {site_id}: {e}")

    # ── Mark unreachable devices offline ─────────────────────────────────────
    # A device must be missing from OFFLINE_THRESHOLD consecutive scans before
    # being marked offline. This prevents transient iLO slowness under load
    # from causing false offline markings. Families whose ranges are disabled
    # are NOT swept — disabling a family must never affect its devices.
    _offline_sweep(api, bool(BMC_RANGES), "cf_redfish_enabled", "bmc_ip",
                   live_server_ips, mark_server_offline, "servers (Redfish)")
    _offline_sweep(api, bool(STORAGE_RANGES), "cf_storage_enabled", "storage_ip",
                   live_storage_ips, mark_storage_offline, "storage")
    _offline_sweep(api, bool(SAN_RANGES), "cf_san_switch_enabled", "san_switch_ip",
                   live_san_ips, mark_san_offline, "SAN switches")
    _offline_sweep(api, bool(CISCO_RANGES), "cf_cisco_enabled", "cisco_ip",
                   live_cisco_ips, mark_cisco_offline, "Cisco switches")
    _offline_sweep(api, bool(FORTIGATE_RANGES), "cf_fortigate_enabled", "fortigate_ip",
                   live_fortigate_ips, mark_fortigate_offline, "FortiGates")
    _offline_sweep(api, bool(HIKVISION_RANGES), "cf_nvr_enabled", "nvr_ip",
                   live_hikvision_ips, mark_hikvision_offline, "Hikvision NVRs",
                   mfr="Hikvision")
    _offline_sweep(api, bool(DAHUA_RANGES), "cf_nvr_enabled", "nvr_ip",
                   live_dahua_ips, mark_dahua_offline, "Dahua NVRs",
                   mfr="Dahua")
    _offline_sweep(api, bool(UNV_RANGES), "cf_nvr_enabled", "nvr_ip",
                   live_unv_ips, mark_unv_offline, "Uniview NVRs",
                   mfr="Uniview")
    _offline_sweep(api, bool(UNIFI_RANGES), "cf_unifi_enabled", "unifi_ip",
                   live_unifi_ips, mark_unifi_offline, "UniFi consoles")

    # Custom-field hygiene: every CF stays ui_visible=if-set (including any
    # added manually between runs).
    try:
        ensure_custom_fields_if_set()
    except Exception as e:
        log("WARN", f"  custom-field visibility normalization failed: {e}")

    log("INFO", "Unified sync complete")
    log("INFO", "=" * 60)


def _offline_sweep(api, enabled, cf_field, ip_field, live_ips, mark_fn, label,
                   mfr=None):
    """One family's offline pass: every enabled device whose stored IP was not
    seen this scan gets a miss via _check_offline. No-op when the family is
    disabled (empty ranges) so it never offlines its existing devices.

    mfr scopes the sweep to devices of one manufacturer — required for the NVR
    families, which share the nvr_* custom fields across vendors: without it
    the Dahua sweep would offline Hikvision NVRs (and vice versa)."""
    if not enabled:
        return
    log("INFO", f"Checking for unreachable {label} ...")
    try:
        for dev in list(api.dcim.devices.filter(**{cf_field: True})):
            if mfr and (getattr(getattr(dev, "manufacturer", None), "name", "")
                        or "").lower() != mfr.lower():
                continue
            stored_ip = (dev.custom_fields or {}).get(ip_field)
            if not stored_ip: continue
            ip = str(stored_ip).split("/")[0].strip()
            _check_offline(ip, live_ips, dev.id, dev.name, mark_fn, label)
    except Exception as e:
        log("ERROR", f"{label} offline check failed: {e}")
