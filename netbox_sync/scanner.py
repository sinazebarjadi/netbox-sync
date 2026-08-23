"""Unified scanner: probes all configured IP ranges in parallel thread pools
and returns discovered devices grouped by family."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from netbox_sync.collectors.brocade import probe_san_switch
from netbox_sync.collectors.cisco import probe_cisco_switch
from netbox_sync.collectors.fortigate import probe_fortigate
from netbox_sync.collectors.hikvision import probe_hikvision
from netbox_sync.collectors.dahua import probe_dahua
from netbox_sync.collectors.unv import probe_unv
from netbox_sync.collectors.msa import probe_storage
from netbox_sync.collectors.redfish import probe_redfish
from netbox_sync.collectors.ruckus import probe_ruckus
from netbox_sync.collectors.unifi import probe_unifi
from netbox_sync.config import (BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES, FORTIGATE_RANGES, RUCKUS_RANGES,
                                HIKVISION_RANGES, UNIFI_RANGES,
                                DAHUA_RANGES, UNV_RANGES,
                                SCAN_WORKERS, log)
from netbox_sync.utils import expand_ranges
from netbox_sync.report import classify_error, record_probe_failure


def _drain_pool(ex, futures, on_hit):
    """Collect probe results. On Ctrl+C, cancel pending probes and shut down
    without waiting — the abort stays responsive while in-flight probes
    (up to ~20s of port-timeout retries each) finish in the background."""
    try:
        for f in as_completed(futures):
            try:
                r = f.result()
            except KeyboardInterrupt:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as exc:
                record_probe_failure(
                    on_hit.__family__, futures[f], "no data",
                    classify_error(exc))
            else:
                if r:
                    on_hit(r)
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True)


def scan_all():
    all_found = {"servers": [], "storage": [], "san_switches": [], "cisco_switches": [],
                 "fortigates": [], "ruckus": [], "hikvision_nvrs": [], "unifi": [],
                 "dahua_nvrs": [], "unv_nvrs": []}

    bmc_ips = expand_ranges(BMC_RANGES)
    if bmc_ips:
        log("INFO", f"Scanning {len(bmc_ips)} IPs across {len(BMC_RANGES)} BMC ranges ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_redfish, ip): ip for ip in bmc_ips}
        def _on_server(r):
            log("INFO", f"  + SERVER {r['ip']}  {r['model']}  s/n={r['serial']}")
            all_found["servers"].append(r)
        _on_server.__family__ = "Server"
        _drain_pool(ex, futures, _on_server)
        log("INFO", f"Server scan done: {len(all_found['servers'])} found.")
    else:
        log("INFO", "BMC ranges empty — skipping server scan.")

    server_ips = {h["ip"] for h in all_found["servers"]}
    all_storage_ips = expand_ranges(STORAGE_RANGES)
    storage_ips = [ip for ip in all_storage_ips if ip not in server_ips]
    skipped = len(all_storage_ips) - len(storage_ips)
    if skipped:
        log("INFO", f"Skipped {skipped} IP(s) in storage ranges already found as servers.")

    if storage_ips:
        log("INFO", f"Scanning {len(storage_ips)} IPs for storage ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_storage, ip): ip for ip in storage_ips}
        def _on_storage(r):
            log("INFO", f"  + STORAGE {r['ip']}  {r['model']}  s/n={r['serial']}")
            all_found["storage"].append(r)
        _on_storage.__family__ = "Storage"
        _drain_pool(ex, futures, _on_storage)
        log("INFO", f"Storage scan done: {len(all_found['storage'])} found.")
    else:
        log("INFO", "No storage IPs to scan (ranges empty or all excluded).")

    # ── SAN switches (SSH on port 22) ────────────────────────────────────────
    used_ips = server_ips | {h["ip"] for h in all_found["storage"]}
    all_san_ips = expand_ranges(SAN_RANGES)
    san_ips = [ip for ip in all_san_ips if ip not in used_ips]
    skipped_san = len(all_san_ips) - len(san_ips)
    if skipped_san:
        log("INFO", f"Skipped {skipped_san} IP(s) in SAN ranges already found as server/storage.")
    if san_ips:
        log("INFO", f"Scanning {len(san_ips)} IPs for SAN switches (SSH) ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_san_switch, ip): ip for ip in san_ips}
        def _on_san(r):
            log("INFO", f"  + SAN {r['ip']}  {r.get('model')}  wwn={r.get('wwn')}")
            all_found["san_switches"].append(r)
        _on_san.__family__ = "SAN switch"
        _drain_pool(ex, futures, _on_san)
        log("INFO", f"SAN switch scan done: {len(all_found['san_switches'])} found.")
    else:
        log("INFO", "No SAN switch IPs to scan (ranges empty or all excluded).")

    # ── Cisco switches (SSH, opt-in family) ─────────────────────────────────
    if CISCO_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["san_switches"]}
        all_cisco_ips = expand_ranges(CISCO_RANGES)
        cisco_ips = [ip for ip in all_cisco_ips if ip not in used_ips]
        skipped_cisco = len(all_cisco_ips) - len(cisco_ips)
        if skipped_cisco:
            log("INFO", f"Skipped {skipped_cisco} IP(s) in Cisco ranges already found.")
        if cisco_ips:
            log("INFO", f"Scanning {len(cisco_ips)} IPs for Cisco switches (SSH) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_cisco_switch, ip): ip for ip in cisco_ips}
            def _on_cisco(r):
                log("INFO", f"  + CISCO {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["cisco_switches"].append(r)
            _on_cisco.__family__ = "Cisco switch"
            _drain_pool(ex, futures, _on_cisco)
            log("INFO", f"Cisco scan done: {len(all_found['cisco_switches'])} found.")
        else:
            log("INFO", "No Cisco IPs to scan (all excluded).")
    else:
        log("INFO", "Cisco ranges not configured — skipping Cisco scan.")

    # ── FortiGates (REST API, opt-in family) ────────────────────────────────
    if FORTIGATE_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["cisco_switches"]}
        all_fg_ips = expand_ranges(FORTIGATE_RANGES)
        fg_ips = [ip for ip in all_fg_ips if ip not in used_ips]
        skipped_fg = len(all_fg_ips) - len(fg_ips)
        if skipped_fg:
            log("INFO", f"Skipped {skipped_fg} IP(s) in FortiGate ranges already found.")
        if fg_ips:
            log("INFO", f"Scanning {len(fg_ips)} IPs for FortiGates (API) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_fortigate, ip): ip for ip in fg_ips}
            def _on_fg(r):
                log("INFO", f"  + FORTIGATE {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["fortigates"].append(r)
            _on_fg.__family__ = "FortiGate"
            _drain_pool(ex, futures, _on_fg)
            log("INFO", f"FortiGate scan done: {len(all_found['fortigates'])} found.")
        else:
            log("INFO", "No FortiGate IPs to scan (all excluded).")
    else:
        log("INFO", "FortiGate ranges not configured — skipping FortiGate scan.")

    # ── Ruckus ZoneDirectors (SSH, opt-in family) ───────────────────────────
    if RUCKUS_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["fortigates"]}
        all_ruckus_ips = expand_ranges(RUCKUS_RANGES)
        ruckus_ips = [ip for ip in all_ruckus_ips if ip not in used_ips]
        if ruckus_ips:
            log("INFO", f"Scanning {len(ruckus_ips)} IPs for Ruckus ZDs (SSH) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_ruckus, ip): ip for ip in ruckus_ips}
            def _on_ruckus(r):
                log("INFO", f"  + RUCKUS {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["ruckus"].append(r)
            _on_ruckus.__family__ = "Ruckus"
            _drain_pool(ex, futures, _on_ruckus)
            log("INFO", f"Ruckus scan done: {len(all_found['ruckus'])} found.")
        else:
            log("INFO", "No Ruckus IPs to scan (all excluded).")
    else:
        log("INFO", "Ruckus ranges not configured — skipping Ruckus scan.")

    # ── UniFi OS consoles (HTTPS API, opt-in family) ────────────────────────
    if UNIFI_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["ruckus"]}
        all_unifi_ips = expand_ranges(UNIFI_RANGES)
        unifi_ips = [ip for ip in all_unifi_ips if ip not in used_ips]
        skipped_unifi = len(all_unifi_ips) - len(unifi_ips)
        if skipped_unifi:
            log("INFO", f"Skipped {skipped_unifi} IP(s) in UniFi ranges already found.")
        if unifi_ips:
            log("INFO", f"Scanning {len(unifi_ips)} IPs for UniFi consoles (API) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_unifi, ip): ip for ip in unifi_ips}
            def _on_unifi(r):
                log("INFO", f"  + UNIFI {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["unifi"].append(r)
            _on_unifi.__family__ = "UniFi console"
            _drain_pool(ex, futures, _on_unifi)
            log("INFO", f"UniFi scan done: {len(all_found['unifi'])} found.")
        else:
            log("INFO", "No UniFi IPs to scan (all excluded).")
    else:
        log("INFO", "UniFi ranges not configured — skipping UniFi scan.")

    # ── Dahua NVRs (HTTP CGI, opt-in family) ────────────────────────────────
    if DAHUA_RANGES:
        all_dahua_ips = expand_ranges(DAHUA_RANGES)
        dahua_ips = [ip for ip in all_dahua_ips if ip not in used_ips]
        skipped_dahua = len(all_dahua_ips) - len(dahua_ips)
        if skipped_dahua:
            log("INFO", f"Skipped {skipped_dahua} IP(s) in Dahua ranges already found.")
        if dahua_ips:
            log("INFO", f"Scanning {len(dahua_ips)} IPs for Dahua NVRs (HTTP) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_dahua, ip): ip for ip in dahua_ips}
            def _on_dahua(r):
                log("INFO", f"  + DAHUA {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["dahua_nvrs"].append(r)
            _on_dahua.__family__ = "Dahua NVR"
            _drain_pool(ex, futures, _on_dahua)
            log("INFO", f"Dahua scan done: {len(all_found['dahua_nvrs'])} found.")
        else:
            log("INFO", "No Dahua IPs to scan (all excluded).")
    else:
        log("INFO", "Dahua ranges not configured — skipping Dahua scan.")

    # ── Uniview NVRs (HTTP LAPI, opt-in family) ──────────────────────────────
    if UNV_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["dahua_nvrs"]}
        all_unv_ips = expand_ranges(UNV_RANGES)
        unv_ips = [ip for ip in all_unv_ips if ip not in used_ips]
        skipped_unv = len(all_unv_ips) - len(unv_ips)
        if skipped_unv:
            log("INFO", f"Skipped {skipped_unv} IP(s) in UNV ranges already found.")
        if unv_ips:
            log("INFO", f"Scanning {len(unv_ips)} IPs for Uniview NVRs (HTTP) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_unv, ip): ip for ip in unv_ips}
            def _on_unv(r):
                log("INFO", f"  + UNV {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["unv_nvrs"].append(r)
            _on_unv.__family__ = "Uniview NVR"
            _drain_pool(ex, futures, _on_unv)
            log("INFO", f"Uniview scan done: {len(all_found['unv_nvrs'])} found.")
        else:
            log("INFO", "No Uniview IPs to scan (all excluded).")
    else:
        log("INFO", "Uniview ranges not configured — skipping Uniview scan.")

    # ── Hikvision NVRs (HTTP ISAPI, opt-in family) ──────────────────────────
    if HIKVISION_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["ruckus"]} \
                          | {h["ip"] for h in all_found["unifi"]} \
                          | {h["ip"] for h in all_found["dahua_nvrs"]} \
                          | {h["ip"] for h in all_found["unv_nvrs"]}
        all_nvr_ips = expand_ranges(HIKVISION_RANGES)
        nvr_ips = [ip for ip in all_nvr_ips if ip not in used_ips]
        if nvr_ips:
            log("INFO", f"Scanning {len(nvr_ips)} IPs for Hikvision NVRs (HTTP) ...")
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_hikvision, ip): ip for ip in nvr_ips}
            def _on_nvr(r):
                log("INFO", f"  + NVR {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["hikvision_nvrs"].append(r)
            _on_nvr.__family__ = "Hikvision NVR"
            _drain_pool(ex, futures, _on_nvr)
            log("INFO", f"Hikvision scan done: {len(all_found['hikvision_nvrs'])} found.")
        else:
            log("INFO", "No Hikvision IPs to scan (all excluded).")
    else:
        log("INFO", "Hikvision ranges not configured — skipping Hikvision scan.")

    return all_found
