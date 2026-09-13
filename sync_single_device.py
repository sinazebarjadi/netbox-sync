#!/usr/bin/env python3
"""Sync a single device to NetBox by IP, without running the full discovery.

Usage:
    python sync_single_device.py <ip> [--type server|storage|san|cisco|fortigate|ruckus|unifi|hikvision|dahua|unv|ftd]

Examples:
    python sync_single_device.py 192.168.19.70
    python sync_single_device.py 172.31.2.202 --type ruckus
    python sync_single_device.py 192.168.244.66 --type dahua
    python sync_single_device.py 192.168.16.143 --type ftd    # FMC IP, syncs all managed FTDs
"""
import argparse
import sys

from netbox_sync.config import log, _validate_config
from netbox_sync.netbox import (get_netbox, ensure_custom_fields,
                                ensure_server_device, ensure_storage_device,
                                ensure_san_switch_device, ensure_cisco_device,
                                ensure_fortigate_device, ensure_ruckus_device,
                                ensure_unifi_console, ensure_hikvision_device,
                                ensure_dahua_device, ensure_unv_device,
                                ensure_ftd_device)
from netbox_sync.collectors.redfish import probe_redfish, rf_collect_inventory
from netbox_sync.collectors.msa import probe_storage, storage_collect_inventory
from netbox_sync.collectors.brocade import probe_san_switch, san_collect_inventory
from netbox_sync.collectors.cisco import probe_cisco_switch, cisco_collect_inventory
from netbox_sync.collectors.fortigate import probe_fortigate, fortigate_collect
from netbox_sync.collectors.ruckus import probe_ruckus, ruckus_collect
from netbox_sync.collectors.unifi import probe_unifi, unifi_collect
from netbox_sync.collectors.hikvision import probe_hikvision, hikvision_collect
from netbox_sync.collectors.dahua import probe_dahua, dahua_collect
from netbox_sync.collectors.unv import probe_unv, unv_collect
from netbox_sync.collectors.ftd import probe_ftd, ftd_collect
from netbox_sync.sync import process_nvrs, sync_inventory, ensure_primary_ip

FAMILIES = {
    "server":    (probe_redfish, rf_collect_inventory, ensure_server_device),
    "storage":   (probe_storage, storage_collect_inventory, ensure_storage_device),
    "san":       (probe_san_switch, san_collect_inventory, ensure_san_switch_device),
    "cisco":     (probe_cisco_switch, cisco_collect_inventory, ensure_cisco_device),
    "fortigate": (probe_fortigate, fortigate_collect, ensure_fortigate_device),
    "ruckus":    (probe_ruckus, ruckus_collect, ensure_ruckus_device),
    "unifi":     (probe_unifi, unifi_collect, ensure_unifi_console),
    "hikvision": (probe_hikvision, hikvision_collect, ensure_hikvision_device),
    "dahua":     (probe_dahua, dahua_collect, ensure_dahua_device),
    "unv":       (probe_unv, unv_collect, ensure_unv_device),
    "ftd":       (probe_ftd, ftd_collect, ensure_ftd_device),
}


def main():
    parser = argparse.ArgumentParser(description="Sync a single device to NetBox by IP")
    parser.add_argument("ip", help="IP address of the device (for FTD: the FMC management IP)")
    parser.add_argument("--type", choices=list(FAMILIES.keys()),
                        help="Device family (auto-detect if omitted)")
    args = parser.parse_args()

    try:
        _validate_config()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1

    api = get_netbox()
    ensure_custom_fields()

    ip = args.ip
    families = [args.type] if args.type else list(FAMILIES.keys())

    for fam in families:
        probe_fn, collect_fn, ensure_fn = FAMILIES[fam]
        log("INFO", f"Probing {ip} as {fam} ...")
        probe = probe_fn(ip)
        if not probe:
            log("WARN", f"  {ip} did not respond as {fam}")
            continue

        log("INFO", f"  {ip} identified as {fam}: {probe.get('model')} / {probe.get('serial')}")
        try:
            data = collect_fn(ip)

            if fam == "ftd":
                # FTD path: the IP is the FMC; enumerate all managed FTDs
                synced = 0
                for ftd in data["ftds"]:
                    mgmt_ip = ftd.get("mgmt_ip")
                    try:
                        dev_id = ensure_fn(ftd, fmc_ip=ip)
                        if mgmt_ip:
                            ensure_primary_ip(dev_id, mgmt_ip, ftd.get("name"))
                        synced += 1
                    except Exception as e:
                        log("ERROR", f"  FTD sync failed for {ftd.get('name')}: {e}")
                log("INFO", f"  [OK] FMC {ip} — {synced} FTDs synced")
                return 0

            dev_id = ensure_fn(probe)
            ensure_primary_ip(dev_id, ip, probe.get("hostname"))
            if fam in ("hikvision", "dahua", "unv"):
                # NVR path: process cameras + HDDs
                process_nvrs([probe], collect_fn, ensure_fn, fam, {}, {}, api)
            else:
                inv = data.get("inventory", {})
                sync_inventory(dev_id, inv)
                log("INFO", f"  [OK] {fam} {ip} synced (id={dev_id}, {len(inv)} items)")
            return 0
        except Exception as e:
            log("ERROR", f"  {fam} sync failed for {ip}: {e}")
            return 1

    log("ERROR", f"  {ip} did not match any known device family")
    return 1


if __name__ == "__main__":
    sys.exit(main())
