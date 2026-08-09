# NetBox HPE Sync

> **English** documentation below · مستندات **فارسی** در ادامه

A Python automation tool that automatically discovers **HPE ProLiant servers** (via Redfish/iLO), **HPE MSA storage arrays** (via the XML API), **Brocade / HPE B-Series SAN switches** (via SSH CLI), **Cisco Catalyst switches** (via SSH CLI, with CDP/LLDP cabling), and **FortiGate firewalls** (via the REST API + SSH, with LLDP cabling) on your network, then synchronizes their hardware inventory into [NetBox](https://github.com/netbox-community/netbox) DCIM. It creates, updates, and marks devices offline, and keeps per-component inventory (CPU, RAM, disks, PSUs, NICs, HBAs, controllers, batteries, FRUs, SFP transceivers, FC ports, switch modules, fans) in sync — you schedule it (cron, systemd timer, Task Scheduler).

---

## Table of Contents · فهرست

- [English](#english)
  - [What it does](#what-it-does)
  - [How it works (architecture)](#how-it-works-architecture)
  - [Repository files](#repository-files)
  - [Requirements](#requirements)
  - [Configuration (`.env`)](#configuration-env)
  - [NetBox prerequisites](#netbox-prerequisites)
  - [Running](#running)
  - [Supported hardware](#supported-hardware)
  - [Inventory items collected](#inventory-items-collected)
  - [How devices are matched](#how-devices-are-matched)
  - [Offline detection](#offline-detection)
- [فارسی](#فارسی)
  - [این برنامه چه کار می‌کند](#این-برنامه-چه-کار-می‌کند)
  - [نحوه کارکرد (معماری)](#نحوه-کارکرد-معماری)
  - [فایل‌های مخزن](#فایل‌های-مخزن)
  - [پیش‌نیازها](#پیش‌نیازها)
  - [پیکربندی (`.env`)](#پیکربندی-env)
  - [پیش‌نیازهای NetBox](#پیش‌نیازهای-netbox)
  - [اجرای برنامه](#اجرای-برنامه)
  - [سخت‌افزارهای پشتیبانی‌شده](#سخت‌افزارهای-پشتیبانی‌شده)
  - [آیتم‌های انبارداری جمع‌آوری‌شده](#آیتم‌های-انبارداری-جمع‌آوری‌شده)
  - [نحوه تطبیق دستگاه‌ها](#نحوه-تطبیق-دستگاه‌ها)
  - [تشخیص آفلاین](#تشخیص-آفلاین)

---

# English

## What it does

1. **Scans IP ranges** you define (CIDR notation) for three kinds of devices:
   - **HPE ProLiant servers** — detected via the Redfish API on the iLO BMC.
   - **HPE MSA storage arrays** — detected via the MSA XML API.
   - **Brocade / HPE B-Series SAN switches** — detected via SSH CLI (Fabric OS).
2. **Creates or updates** a NetBox **device** for each discovered server/storage/SAN-switch unit, including manufacturer, device type, role, site, serial, and custom fields (BMC IP, firmware, CPU/RAM/disk summaries, health…).
3. **Collects detailed hardware inventory** from each device (CPUs, RAM modules, disks, PSUs, NICs, HBAs, controllers, batteries, FRUs, SFP transceivers, FC ports) and syncs each component as a NetBox **inventory item** keyed by serial number.
4. **Removes stale inventory items** that are no longer reported by the device.
5. **Records each device's management IP** in IPAM (mask derived from the scan range, `/32` fallback; marker description `netbox-sync: mgmt`) and sets it as the device's **primary IPv4** in NetBox. The IP is assigned to the **real management interface** when it can be identified — the FortiGate VLAN subinterface (matched from interface IPs) or the Cisco SVI (from `show ip interface brief`, created as a virtual interface when missing); otherwise a synthetic `mgmt` interface (`virtual`, `mgmt_only`) holds the assignment. Management interfaces are never deleted by interface syncs. FortiGate interface **aliases** map to NetBox interface **labels**.
6. **Marks devices offline** in NetBox when they stop responding to the scan.
7. **Runs one full sync per invocation** and exits (cron-friendly exit codes + lock file) — you schedule it (cron, systemd timer, Task Scheduler).

## How it works (architecture)

```
                ┌──────────────────────────────────────────────┐
                │              sync_all_to_netbox.py            │
                │                                              │
   .env  ─────► │  load_dotenv() → config + credentials        │
                │                                              │
                │  ┌────────────┐    ┌──────────────────────┐   │
   BMC_RANGES   │  │  scan_all()│───►│ probe_redfish(ip)    │   │
   ───────────► │  │            │    │  RedfishSession      │   │
                │  │ ThreadPool │    │  → login + GET tree  │   │
   STORAGE_     │  │ Executor   │    └──────────────────────┘   │
   RANGES ────► │  │            │───►│ probe_storage(ip)    │   │
                │  └─────┬──────┘    │  StorageSession      │   │
                │        │           │  → login + XML show  │   │
                │        ▼           └──────────────────────┘   │
                │  found = {servers:[...], storage:[...], san_switches:[...]}       │
                │        │                                     │
                │        ▼                                     │
                │  for each server:                            │
                │    ensure_server_device()  →  NetBox device  │
                │    rf_collect_inventory() →  NetBox items    │
                │    sync_inventory()       →  diff by serial  │
                │                                              │
                │  for each storage:                           │
                │    ensure_storage_device() →  NetBox device  │
                │    storage_collect_inventory() → items      │
                │    sync_inventory()        →  diff by serial  │
                │                                              │
                │  for each SAN switch:                        │
                │    ensure_san_switch_device() → NetBox device │
                │    san_collect_inventory()  → items + FC ports │
                │    sync_inventory()         →  diff by serial  │
                │                                              │
                │  mark unreachable devices offline             │
                └──────────────────────────────────────────────┘
                                    │
                                    ▼
                         ┌────────────────────┐
                         │   NetBox (DCIM)    │
                         │  devices + items   │
                         └────────────────────┘
```

### Key design points

- **Idempotent**: every run reconciles NetBox state with reality. Safe to re-run.
- **Parallel scanning**: IP probing uses a thread pool (`SCAN_WORKERS`, default 20).
- **Resilient**: per-device `try/except` isolation — one failing host doesn't abort the run. MSA rate-limiting is handled with exponential backoff + re-login.
- **Serial-keyed inventory**: components are matched/updated by their serial number; components no longer reported are deleted, duplicates are cleaned up.
- **No real secrets in the repo**: all credentials, IPs, and site mappings live in `.env` (gitignored). Only `.env.example` is committed.

## Repository files

| File | Purpose |
|------|---------|
| `sync_all_to_netbox.py` | Thin entry point — validates config and runs the scheduler (`python sync_all_to_netbox.py` works exactly as before). |
| `netbox_sync/` | The implementation package: `config` (.env/credentials/logging), `utils` (naming helpers, IP tools), `netbox` (NetBox API layer: CRUD, device ensure/offline, inventory sync), `collectors/` (`redfish`, `msa`, `brocade`, `cisco`, `fortigate`, `ruckus`, `hikvision` sessions + inventory collection), `scanner` (parallel IP probing), `sync` (the `run_sync` orchestrator). |
| `netbox_sync/collectors/cisco.py` | Cisco Catalyst collector — netmiko SSH, IOS/IOS-XE CLI parsers, CDP/LLDP cable reconciliation. |
| `netbox_sync/collectors/fortigate.py` | FortiGate collector — REST API session + SSH extras (LLDP cables, SFP transceivers). |
| `netbox_sync/models.py` | Server (`SERVER_MODEL_MAP`), storage (`STORAGE_MODEL_MAP`), and SAN switch (`SWITCH_MODEL_MAP`) model-name normalization maps. Maps vendor strings (e.g. `proliant dl360 gen10`) to canonical NetBox device-type names (e.g. `HPE DL360 G10`). |
| `.env.example` | Template for your `.env` file. Copy to `.env` and fill in real values. |
| `requirements.txt` | Python dependencies (`requests`, `pynetbox`, `python-dotenv`, `paramiko`, `netmiko`). |
| `requirements-dev.txt` | Test dependencies (pytest); includes `requirements.txt`. |
| `tests/` | pytest suite for the CLI parsers, naming helpers and NetBox sync logic — runs entirely on in-memory fakes, no hardware needed. |
| `.gitignore` | Ignores `.env`, `__pycache__/`, venvs, and any personal working folders. |

## Requirements

- Python 3.9+
- A reachable **NetBox** instance (v3.x) with an API token
- Network access from the host running this script to:
  - iLO/BMC IPs on `REDFISH_PORT` (default 443)
  - MSA storage IPs on `STORAGE_PORT` (default 443)
  - SAN switch IPs on `SWITCH_PORT` (default 22, SSH)
- HPE ProLiant servers with iLO 4 / iLO 5 (Redfish capable)
- HPE MSA storage arrays (2040 / 2042 / 2050 / 2052 / 2060 class)
- Brocade / HPE B-Series SAN switches (Fabric OS, SSH-enabled)

Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit. **All sensitive values must live in `.env`** — never commit the real file.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `NETBOX_URL` | ✅ | — | Base URL of your NetBox instance (e.g. `https://netbox.example.com`). |
| `NETBOX_TOKEN` | ✅ | — | NetBox API token (read/write). |
| `NETBOX_VERIFY_TLS` | ❌ | `false` | Verify NetBox's TLS certificate. Keep `false` for self-signed certs. |
| `REDFISH_USER` | ✅ | — | BMC (iLO) username for Redfish login. |
| `REDFISH_PASS` | ✅ | — | BMC (iLO) password. |
| `REDFISH_PORT` | ❌ | `443` | TCP port for Redfish on the BMC. |
| `STORAGE_USER` | ✅ | — | MSA storage API username. |
| `STORAGE_PASS` | ✅ | — | MSA storage API password. |
| `STORAGE_PORT` | ❌ | `443` | TCP port for the MSA XML API. |
| `STORAGE_AUTH_HASH` | ❌ | `sha256` | Hash algorithm for MSA credential hash (`sha256` or `md5`). Falls back automatically if one fails. |
| `BMC_RANGES` | ❌* | example CIDRs | Comma-separated CIDR ranges to scan for servers. |
| `STORAGE_RANGES` | ❌* | example CIDRs | Comma-separated CIDR ranges to scan for storage. IPs already found as servers are skipped. |
| `SITE_KEYWORD_MAP` | ❌ | — | Comma-separated `keyword:SiteName` pairs — used as fallback when no `SITE_IP_MAP` range matches. A device whose hostname contains the keyword (case-insensitive) is assigned that site. e.g. `dc1:Datacenter1,hq:HQ`. |
| `SITE_IP_MAP` | ❌ | — | Comma-separated `cidr:SiteName` pairs. A device whose IP falls inside the CIDR is assigned that site; **longest prefix wins**. Checked **before** `SITE_KEYWORD_MAP`. e.g. `172.31.0.0/16:HQ,172.31.1.0/24:Branch`. |
| `SCAN_WORKERS` | ❌ | `20` | Thread-pool size for parallel IP scanning. |
| `OFFLINE_THRESHOLD` | ❌ | `2` | Consecutive scans a device must miss before it is marked offline (anti-flapping). |
| `LOG_LEVEL` | ❌ | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARN`, `ERROR`. |
| `DEFAULT_SITE_NAME` | ❌ | `Default` | Fallback site name when no keyword matches. |
| `DEFAULT_ROLE_NAME` | ❌ | `Server` | NetBox device role for servers. |
| `DEFAULT_STORAGE_ROLE` | ❌ | `Storage` | NetBox device role for storage arrays. |
| `SWITCH_USER` | ✅ | -- | SSH username for Brocade SAN switches. |
| `SWITCH_PASS` | ✅ | -- | SSH password for Brocade SAN switches. |
| `SWITCH_PORT` | ❌ | `22` | SSH port for SAN switch CLI. |
| `SWITCH_STRICT_HOST_KEY` | ❌ | `false` | Verify switch SSH host keys against the system `known_hosts` (MITM protection). |
| `SAN_RANGES` | ❌* | example CIDRs | Comma-separated CIDR ranges to scan for SAN switches. IPs already found as server/storage are skipped. |
| `DEFAULT_SWITCH_ROLE` | ❌ | `SAN Switch` | NetBox device role for SAN switches. |
| `CISCO_USER` | ❌* | — | SSH username for Cisco switches (required only when `CISCO_RANGES` is set). |
| `CISCO_PASS` | ❌* | — | SSH password for Cisco switches. |
| `CISCO_PORT` | ❌ | `22` | SSH port for Cisco switches. |
| `CISCO_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Cisco switches. Empty = family disabled. |
| `DEFAULT_CISCO_ROLE` | ❌ | `Switch` | NetBox device role for Cisco switches. |
| `FORTIGATE_USER` | ❌* | — | Admin username for FortiGates (REST API session auth (/logincheck) + SSH extras); required when `FORTIGATE_RANGES` is set. |
| `FORTIGATE_PASS` | ❌* | — | Admin password for FortiGates. |
| `FORTIGATE_PORT` | ❌ | `443` | REST API port. |
| `FORTIGATE_SSH_PORT` | ❌ | `22` | SSH port for FortiGates. |
| `FORTIGATE_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for FortiGates. Empty = family disabled. |
| `DEFAULT_FORTIGATE_ROLE` | ❌ | `Firewall` | NetBox device role for FortiGates. |
| `RUCKUS_USER` | ❌* | — | SSH username for Ruckus ZDs (required when `RUCKUS_RANGES` is set). |
| `RUCKUS_PASS` | ❌* | — | SSH password for Ruckus ZDs. |
| `RUCKUS_PORT` | ❌ | `22` | SSH port for Ruckus ZDs. |
| `RUCKUS_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Ruckus ZDs (VIPs and/or unit addresses). Empty = family disabled. |
| `RUCKUS_HA_MAP` | ❌ | — | HA pairs: `vip:primary,secondary` per pair, pairs separated by `;`. Merges a pair into one cluster device. |
| `DEFAULT_RUCKUS_ROLE` | ❌ | `Wireless Controller` | NetBox device role for controllers. |
| `DEFAULT_AP_ROLE` | ❌ | `Access Point` | NetBox device role for APs. |
| `HIKVISION_USER` | ❌* | — | HTTP digest username for Hikvision NVRs (required when `HIKVISION_RANGES` is set). |
| `HIKVISION_PASS` | ❌* | — | HTTP digest password for Hikvision NVRs. |
| `HIKVISION_PORT` | ❌ | `80` | HTTP ISAPI port for Hikvision NVRs. |
| `HIKVISION_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Hikvision NVRs. Empty = family disabled. |
| `DEFAULT_HIKVISION_ROLE` | ❌ | `NVR` | NetBox device role for NVRs. |
| `DEFAULT_HIKVISION_CAMERA_ROLE` | ❌ | `Camera` | NetBox device role for cameras (all NVR vendors). |
| `DAHUA_USER` | ❌* | — | HTTP digest username for Dahua NVRs (required when `DAHUA_RANGES` is set). |
| `DAHUA_PASS` | ❌* | — | HTTP digest password for Dahua NVRs. |
| `DAHUA_PORT` | ❌ | `80` | HTTP CGI port for Dahua NVRs. |
| `DAHUA_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Dahua NVRs. Empty = family disabled. |
| `DEFAULT_DAHUA_ROLE` | ❌ | `NVR` | NetBox device role for Dahua NVRs. |
| `UNV_USER` | ❌* | — | HTTP digest username for Uniview NVRs (required when `UNV_RANGES` is set). |
| `UNV_PASS` | ❌* | — | HTTP digest password for Uniview NVRs. |
| `UNV_PORT` | ❌ | `80` | HTTP LAPI port for Uniview NVRs. |
| `UNV_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Uniview NVRs. Empty = family disabled. |
| `DEFAULT_UNV_ROLE` | ❌ | `NVR` | NetBox device role for Uniview NVRs. |

> *The shipped defaults in `netbox_sync/config.py` are **documentation-only** placeholder CIDRs (`192.0.2.0/27` = TEST-NET). Set the ranges in `.env` to your real networks — or set a range **empty** (e.g. `BMC_RANGES=`) to disable that family entirely (no scanning and no offline marking for it).

### `.env` example

```dotenv
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=your-netbox-api-token

REDFISH_USER=netbox
REDFISH_PASS=changeme
REDFISH_PORT=443

STORAGE_USER=netbox
STORAGE_PASS=changeme
STORAGE_PORT=443
STORAGE_AUTH_HASH=sha256

SWITCH_USER=netbox
SWITCH_PASS=changeme
SWITCH_PORT=22

BMC_RANGES=192.0.2.0/27,198.51.100.0/27
STORAGE_RANGES=192.0.2.16/32,198.51.100.16/32
SAN_RANGES=192.0.2.32/29,198.51.100.32/29

SITE_KEYWORD_MAP=dc1:Datacenter1,hq:HQ

SCAN_WORKERS=20
DEFAULT_SITE_NAME=Default
DEFAULT_ROLE_NAME=Server
DEFAULT_STORAGE_ROLE=Storage
DEFAULT_SWITCH_ROLE=SAN Switch
```

## NetBox prerequisites

### 1. Inventory item roles

Inventory-item **roles** are resolved **by name** and **auto-created** on first use (then cached), so no manual setup is required. If you prefer to pre-create them (`/dcim/inventory-item-roles/`), the names must match exactly:

| Role name | Used for |
|-----------|----------|
| HDD | Hard disk drives |
| SSD | Solid-state drives |
| CPU | Processors |
| Memory | RAM modules |
| NIC | Network adapters |
| PSU | Power supplies |
| Controller | RAID / storage controllers |
| HBA | Host bus adapters / FC |
| Battery | Smart storage batteries |
| SAS Exp | SAS expanders / FRUs |
| SFP | SFP transceivers (SAN switches) |

> Upgrading from an older version that used hardcoded role IDs (1–12)? As long as your existing roles carry these names, they are found and reused — nothing breaks. Role IDs are DB-sequence-dependent and are no longer referenced anywhere.

### 2. Custom fields

The script writes **custom fields** on devices. They are **created automatically at the start of every sync** (object type `dcim | device`, visibility `if-set`) — no manual setup needed. The full list, for reference:

**For servers:**

| Custom field | Type | Label |
|--------------|------|-------|
| `bmc_ip` | Text | BMC IP |
| `redfish_enabled` | Boolean | Redfish enabled |
| `redfish_model` | Text | Redfish model |
| `redfish_power_state` | Text | Power state |
| `redfish_bios_version` | Text | BIOS version |
| `redfish_cpu_model` | Text | CPU model |
| `redfish_cpu_sockets` | Integer | CPU sockets |
| `redfish_cpu_cores` | Integer | CPU cores |
| `redfish_cpu_threads` | Integer | CPU threads |
| `redfish_ram_gib` | Integer | RAM (GiB) |
| `redfish_disk_total_gib` | Integer | Total disk (GiB) |

**For storage:**

| Custom field | Type | Label |
|--------------|------|-------|
| `storage_ip` | Text | Storage IP |
| `storage_enabled` | Boolean | Storage enabled |
| `storage_health` | Text | Health |
| `storage_firmware` | Text | Firmware |
| `storage_model` | Text | Model |
| `storage_disk_count` | Integer | Disk count |
| `storage_total_capacity_gib` | Integer | Total capacity (GiB) |

**For SAN switches (HPE B-Series / Brocade):**

| Custom field | Type | Label |
|--------------|------|-------|
| `san_switch_ip` | Text | SAN switch IP |
| `san_switch_enabled` | Boolean | SAN switch enabled |
| `san_switch_wwn` | Text | Switch WWN |
| `san_switch_firmware` | Text | Firmware (Fabric OS) |
| `san_switch_model` | Text | Model |
| `san_switch_port_count` | Integer | Port count |

**For Cisco Catalyst switches:**

| Custom field | Type | Label |
|--------------|------|-------|
| `cisco_ip` | Text | Cisco switch IP |
| `cisco_enabled` | Boolean | Cisco switch enabled |
| `cisco_firmware` | Text | IOS version |
| `cisco_model` | Text | Model |
| `cisco_port_count` | Integer | Port count |

**For FortiGate firewalls:**

| Custom field | Type | Label |
|--------------|------|-------|
| `fortigate_ip` | Text | FortiGate IP |
| `fortigate_enabled` | Boolean | FortiGate enabled |
| `fortigate_firmware` | Text | FortiOS version |
| `fortigate_model` | Text | Model |
| `fortigate_port_count` | Integer | Port count |
| `fortigate_ha_group` | Text | HA cluster group name |
| `fortigate_ha_mode` | Text | HA mode (a-p / a-a) |
| `fortigate_ha_peer` | Text | HA peer units |
| `fortigate_ha_role` | Text | Role of the probed unit |
**NAT → IPAM:** FortiGate **VIPs** become IPAM IP addresses for the external IP (`extip`) with NetBox's native **`nat_inside`** pointing at the mapped internal server's address; FortiGate **IP pools** become plain IPAM addresses for the SNAT range. Each port-forwarded VIP also becomes a **NetBox Service** (protocol + port) on the device, linked to the external IP with the mapped backend in its description — so VIPs sharing one `extip` keep full per-port fidelity (static no-port VIPs are represented by the address alone). Marker-owned (`netbox-sync: nat …`) NAT addresses/services no longer reported are swept after each run; manual entries are never touched.

**HA clusters:** a FortiGate HA pair (active-passive or active-active) becomes **one NetBox device** named and serialized after the primary unit — resolvable by any unit serial, so listing both units never duplicates. Peer units are recorded in the `fortigate_ha_*` fields, and the primary IPv4 follows the primary unit (a secondary probe never repoints it).

**For NVRs (Hikvision / Dahua / Uniview — shared vendor-neutral fields):**

| Custom field | Type | Label |
|--------------|------|-------|
| `nvr_ip` | Text | NVR IP |
| `nvr_enabled` | Boolean | NVR enabled |
| `nvr_model` | Text | Model |
| `nvr_firmware` | Text | Firmware version |
| `nvr_camera_count` | Integer | Number of attached cameras |

**For cameras (each camera is its own device, all NVR vendors):**

| Custom field | Type | Label |
|--------------|------|-------|
| `cam_ip` | Text | Camera IP |
| `cam_mac` | Text | Camera MAC (if known) |
| `cam_enabled` | Boolean | Camera enabled (online) |
| `cam_nvr` | Text | Parent NVR name |
| `cam_channel` | Integer | NVR channel number |
| `cam_model` | Text | Model |
| `cam_serial` | Text | Camera serial |

**For UniFi OS consoles:**

| Custom field | Type | Label |
|--------------|------|-------|
| `unifi_ip` | Text | UniFi console IP |
| `unifi_enabled` | Boolean | UniFi enabled |
| `unifi_version` | Text | UniFi OS version |
| `unifi_ap_count` | Integer | Number of managed APs |
| `unifi_sites` | Integer | Number of sites |

> The offline-detection loop filters devices via `cf_redfish_enabled=True` / `cf_storage_enabled=True` / `cf_san_switch_enabled=True` / `cf_cisco_enabled=True` / `cf_fortigate_enabled=True` / `cf_nvr_enabled=True` / `cf_unifi_enabled=True` (NetBox custom-field filter syntax).

### 3. Device roles & sites

`Server`, `Storage`, and `SAN Switch` device roles, `HPE` / `Brocade` manufacturers, and sites are **auto-created** if missing. You may also pre-create them.

## Running

```bash
cp .env.example .env   # then edit with your real values
pip install -r requirements.txt
python sync_all_to_netbox.py
```

Each invocation performs **one full sync and exits**: exit code `0` on success, `1` on error, `130` on Ctrl+C. A lock file (`netbox-sync.lock` in the repo root) prevents overlapping instances; a lock older than 24 h is treated as stale (crash recovery) and replaced.

```
[2026-07-29 00:00:01] [INFO] ============================================================
[2026-07-29 00:00:01] [INFO] Unified sync started (servers + storage + SAN + Cisco switches)
[2026-07-29 00:00:01] [INFO] ============================================================
[2026-07-29 00:00:01] [INFO] BMC ranges empty — skipping server scan.
[2026-07-29 00:00:01] [INFO] Scanning 1 IPs for Cisco switches (SSH) ...
[2026-07-29 00:00:02] [INFO]   + CISCO 192.0.2.65  C9200L-48T-4X  s/n=XXXXXXX
...
```

Press `Ctrl+C` to abort a running sync (during an active scan it may take up to ~20 seconds for in-flight probes to finish; pending probes are cancelled immediately).

### Schedule with cron (recommended)

```cron
# twice daily (00:00 and 12:00), logs appended to a file
0 0,12 * * * /opt/netbox-sync/.venv/bin/python /opt/netbox-sync/sync_all_to_netbox.py >> /var/log/netbox-sync.log 2>&1
```

A systemd timer or Windows Task Scheduler works just as well — anything that runs the command periodically. The script finds its `.env` next to the repo regardless of the working directory.

### Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

The suite covers the Brocade CLI parsers, MSA XML parsing, item naming, and the NetBox reconciliation logic (stale/duplicate cleanup, update-vs-create) against in-memory fakes — no hardware or NetBox instance required.

## Supported hardware

**Servers (Redfish / iLO):**
- HPE ProLiant DL360 / DL380 / DL320, Gen8 through Gen11
- iLO 4 (Gen9) and iLO 5 (Gen10/Gen11)
- Includes HPE SmartStorage fallback for Gen9 iLO4 (which lacks full Redfish storage data), with pseudo-serial generation for HBAs that don't expose serials.

**Storage (MSA XML API):**
- HPE MSA 2040, 2042, 2050, 2052, 2060
- Handles field-name differences between `show disks` (newer firmware) and `show disk-parameters` (older firmware).

**SAN switches (Brocade / HPE B-Series, SSH CLI):**
- HPE SN6010B/C, SN6500B/C, SN6700B, SN8600C, SN8700C and equivalent Brocade 300/320/5100/5300/6505/6510/6520/6547/7800/7840/DCX-4S/SX6.
- Connects via SSH and runs `switchshow`, `version`, `nsshow`, `nscamshow`, `sfpshow`.

**LAN switches (Cisco Catalyst, IOS / IOS-XE, SSH via netmiko):**
- Catalyst 2960X / 3650 / 3850 / 9200 / 9300 families (classic IOS and IOS-XE dialects).
- Connects via SSH and runs `show version`, `show inventory`, `show interfaces status`, `show vlan brief`, `show interfaces trunk`, `show cdp neighbors detail` (with `show lldp neighbors detail` as fallback).
- The Cisco family is **opt-in**: it only activates when `CISCO_RANGES` is set.

**Wireless controllers (Ruckus ZoneDirector, SSH):**
- ZD1200-class controllers via an interactive shell login (two-step `login:`/`Password:` + `enable`) — `show sysinfo` (identity), `show ap all` (APs), `show wlan all` (SSIDs).
- Controller device with `wlc_*` custom fields; each AP becomes an `Access Point` device (MAC is the identity — `wap_mac`), linked to its controller (`wap_wlc`) and group (`wap_group`); vanished APs are marked offline, never deleted.
- WLANs become native **Wireless LANs** (SSID + auth type + VLAN link from the site's groups). **Passphrases are never synced.**
- **HA pairs:** `RUCKUS_HA_MAP=vip:primary,secondary` (per pair, `;`-separated) merges a pair into one cluster device — identity from VIP/primary probes only, secondary probes update liveness only; primary IPv4 = the VIP.
- **Opt-in**: activates only when `RUCKUS_RANGES` is set.

**Wireless controllers (Ubiquiti UniFi OS, HTTPS API):**
- UniFi OS consoles (UDM / CloudKey / UniFi OS Server, Network Application 10.x) via the legacy session API: `POST /api/login` (session cookie) → `/api/self/sites`, per-site `/api/s/<site>/stat/device` (APs), `/api/s/<site>/rest/wlanconf` (WLANs), `/api/s/<site>/rest/networkconf` (VLAN bindings). Use a dedicated **local** admin account (cloud UI.com accounts break on MFA).
- The console becomes a **device** with `unifi_*` custom fields (identity = console uuid serial). Each AP reuses the shared AP machinery: `Access Point` role, MAC identity (`wap_mac`), `wap_group` = UniFi site name, `wap_wlc` = console name; the AP's NetBox site comes from the standard **SITE_IP_MAP resolution on the AP's IP** (same as every other family); vanished APs are marked offline, never deleted.
- WLANs from **all sites** become native **Wireless LANs** (group `UniFi <console>`), aggregated console-globally by SSID; the VLAN link is resolved per site from the WLAN's network binding (unique match in the site's marker-owned VLAN groups, else created in the site's UniFi group). **Passphrases are never synced.**
- **Opt-in**: activates only when `UNIFI_RANGES` is set.

**Firewalls (FortiGate, REST API + SSH extras):**
- FortiGate 40F / 60F / 80F / 100F / 200F class (FortiOS 6/7). Queries `/api/v2/monitor/system/status`, `/api/v2/monitor/system/interface`, `/api/v2/cmdb/system/interface` (VDOM `root`).
- Authentication is session-based: `POST /logincheck` with admin credentials (`FORTIGATE_USER`/`FORTIGATE_PASS`) — no API tokens, and the per-device token file was removed.
- SSH runs `diagnose lldp neighbor-summary` (cables) and `diagnose sys transceiver list` (SFP inventory).
- Aggregate (port-channel) interfaces are imported from cmdb as NetBox `lag` interfaces; member ports link via `lag`, VLAN subinterfaces link via `parent` to their LAG/parent interface.
- **Opt-in**: activates only when `FORTIGATE_RANGES` is set.
- VLAN subinterfaces are **matched to the switches' existing VLANs** instead of duplicated: a vid found in exactly one broadcast domain is reused, FortiGate-only VLANs are created in a per-device group, and overlaps are disambiguated by looking the subinterface's MAC up in the switches' MAC address tables (`fnsysctl ifconfig -a` on the FortiGate, `show mac address-table address <mac>` on the switches).

**NVRs (Hikvision, ISAPI over HTTP digest):**
- Hikvision NVRs via HTTP digest auth — `GET /ISAPI/System/deviceInfo` (identity), `GET /ISAPI/ContentMgmt/InputProxy/channels` (attached cameras), `GET .../channels/status` (online state).
- The NVR becomes a **device** with `nvr_*` custom fields (matched by serial). Each camera becomes **its own device** (role `Camera`, serial is the identity) with `cam_*` custom fields; the parent NVR is recorded in `cam_nvr`. Each camera's management IP is set as its **primary IPv4** (on the camera's `eth0` interface when camera cabling is active, else a synthetic `mgmt` interface).
- Camera MACs are collected per channel via the NVR-proxied `.../InputProxy/channels/<id>/deviceInfo` endpoint and stored in `cam_mac` — they feed the camera→switch cabling described below. Cameras no longer reported by an NVR are marked **offline**, never deleted.
- **Opt-in**: activates only when `HIKVISION_RANGES` is set.

**NVRs (Dahua, CGI over HTTP digest):**
- Dahua NVRs via HTTP digest auth — `magicBox.cgi?action=getSystemInfo`/`getDeviceClass`/`getSoftwareVersion`/`getMachineName` (identity), `configManager.cgi?action=getConfig&name=RemoteDevice` (per-channel camera IP/model/serial/firmware; slot N = channel N+1), `name=ChannelTitle` (camera names).
- Cameras become their own devices exactly like the Hikvision family (same `cam_*` fields, same `cam_nvr` link, same offline sweep). Caveats: ONVIF-registered cameras usually have **no reliable MAC** in the table (empty or `ff:ff:ff:ff:ff:ff` — dropped), so camera→switch cabling simply skips them; channel online-state configs are permission-gated, so `cam_enabled` mirrors the registration `Enable` flag. A camera title colliding with an existing same-site device name gets a deterministic `-cam<ch>` suffix.
- **Opt-in**: activates only when `DAHUA_RANGES` is set.

**NVRs (Uniview/UNV, LAPI over HTTP digest):**
- UNV NVRs via HTTP digest auth — `GET /LAPI/V1.0/System/DeviceInfo` (identity), `GET /LAPI/V1.0/Channels/System/ChannelDetailInfos` (per-channel name/online status/manufacturer/model/**IP+MAC**), `GET /LAPI/V1.0/Channels/System/DeviceInfos` (per-channel serial/firmware).
- Cameras become their own devices like the other NVR families; **MACs are real**, so camera→switch cabling works for UNV cameras out of the box. Unassigned channel slots (no IP and no serial) are skipped.
- **Opt-in**: activates only when `UNV_RANGES` is set.

### Camera → switch cabling

When the Cisco family is also enabled (`CISCO_RANGES` set and reachable),
each camera with a known MAC is cabled in NetBox to the switch port it is
learned on: one `eth0` interface per camera and a real cable between it and
the switch interface (description `netbox-sync: mac-table ...`). Cables are
managed like CDP cables — only marker-owned ones are touched. Because
switch MAC tables age out idle entries (~5 min), a cable is never deleted
when a camera's MAC is momentarily missing; it is only moved when the MAC
is positively found on a different port. With Cisco disabled, cabling is
silently skipped.

See `netbox_sync/models.py` for the full model alias maps. Add your own models there.

## Inventory items collected

**From each server (Redfish):**

| Component | Source | Key fields |
|----------|--------|------------|
| CPU | `/Systems/1/Processors` | model, cores, threads, serial |
| Memory | `/Systems/1/Memory` | capacity, speed, type, part number, serial |
| Disks | `/Storage/*/Drives` + SmartStorage fallback | model, capacity, media type, protocol, serial |
| Controllers | `StorageControllers` + SmartStorage | model, firmware, serial |
| PSU | `/Chassis/*/Power/PowerSupplies` | model, watts, serial |
| NIC | `/NetworkAdapters` + PCIe devices | name, firmware, MAC, part number, serial |
| HBA | PCIe FRUs (Gen10) + pseudo-serial (Gen9 iLO4) | name, firmware, serial |
| Battery | Oem.Hpe.Battery (Gen9) + SmartStorageBattery (Gen10) | model, firmware, serial |

**From each storage (MSA):**

| Component | MSA `show` command | Key fields |
|----------|--------------------|------------|
| Disks | `show disks` / `show disk-parameters` / `show disk-statistics` | model, size, type, serial, health |
| Controllers | `show controllers` | controller-id, firmware, IP, health, serial |
| PSUs | `show power-supplies` | location, health, status, serial |
| FRUs / SAS expanders | `show frus` / `show enclosure-fru` | name, location, health, serial |

**From each SAN switch (Brocade CLI):**

| Component | CLI command | Key fields |
|----------|-------------|------------|
| Switch identity | `switchshow` | switch WWN, model, switch name |
| Firmware | `version` | Fabric OS version |
| FC ports (NetBox interfaces) | `switchshow` | index, port, media, speed, state, proto, connected WWN |
| Name server (logged-in devices) | `nsshow` / `nscamshow` | port id, port WWN, node WWN |
| SFP transceivers | `sfpshow` | vendor, part number, serial, temperature |

## How devices are matched

Each discovered device is matched to an existing NetBox device by **serial number** (primary). If the serial is invalid/missing, a secondary lookup by **name + site + role** is used. This prevents duplicate devices across runs.

For storage, the secondary lookup also avoids clashing with a server that has the same name (it checks the `bmc_ip` custom field is absent).

## CDP/LLDP cabling (Cisco)

For each discovered Cisco switch, the script reads `show cdp neighbors detail` (falling back to `show lldp neighbors detail`) and creates **cables** in NetBox between the switch's interfaces and the resolved neighbor interfaces:

- A cable is only created when **both ends resolve**: the neighbor's hostname (domain-stripped) must match a NetBox device **and** the remote interface must exist on it. Anything else is skipped with a DEBUG log — notably Cisco↔server links, because server NICs are inventory *items* in this tool, not interfaces.
- Sync-created cables carry a `netbox-sync:` prefix in their description. Only **marked** cables are ever refreshed or deleted (stale ones disappear when the neighbor data no longer reports them). **Manually documented cables are never modified or deleted.**

## VLAN sync (Cisco)

VLANs from `show vlan brief` are created/updated in IPAM grouped by **broadcast domain derived from CDP topology**: switches that see each other as CDP neighbors (same-site edges) form connected components, and each component maps to a site-scoped **VLAN group** named `BD1`, `BD2`… The group key (in the description, `netbox-sync: vtp=<key>`) prefers a member's VTP domain (casefolded), else the first hostname — stable across runs. This handles empty-VTP (transparent) switches correctly: an island of CDP-connected switches shares one group instead of one group per switch. Overlapping VLAN IDs at one site coexist in different components' groups. Interfaces get their VLAN linkage (access untagged, trunk native + tagged) as before. Marker-owned (`netbox-sync:`) VLANs no longer reported by any group member, and stale duplicate groups (case variants, abandoned per-switch fallbacks), are deleted after each run; manual VLANs/groups are never modified or deleted.

## IPAM prefixes & host addresses

**Prefixes** are derived from FortiGate interface IPs (`ip + mask` in cmdb config — real masks, e.g. `172.31.2.0/24`, `79.127.120.176/28`) and created/updated in IPAM with site and VLAN links (marker `netbox-sync:`). Each `SITE_IP_MAP` CIDR is also synced as a **container** parent prefix (marker `netbox-sync: last seen parent <site>`), so discovered subnets nest into a clean hierarchy; parents are swept if their map entry is removed. **Gateway/host addresses**: FortiGate subinterface IPs are assigned to their subinterfaces; Cisco SVI IPs (`show ip interface brief`) are placed inside their longest matching prefix and assigned to their SVI (created as virtual interfaces when missing). Marker-owned prefixes and host IPs no longer reported are swept after each run; manual IPAM entries and `netbox-sync: mgmt` addresses are never touched.

## Offline detection

After each sync, the script queries NetBox for all devices where `redfish_enabled=True` (servers), `storage_enabled=True` (storage), `san_switch_enabled=True` (SAN switches), or `cisco_enabled=True` (Cisco switches). If a device's stored BMC/storage/SAN IP was **not** seen in the current scan, a miss counter is incremented; only after `OFFLINE_THRESHOLD` **consecutive misses** (default 2) is it marked `status=offline` and its `*_enabled` flag set to `false` — this prevents transient slowness from causing false offline markings. The device is **not** deleted — the next successful scan flips it back to `active` and resets the counter.

---

# فارسی

## این برنامه چه می‌کند

1. **بازه‌های IP** که شما تعریف کرده‌اید (به‌صورت CIDR) را برای پنج نوع دستگاه اسکن می‌کند:
   - **سرورهای HPE ProLiant** — از طریق API سِ Redfish روی iLO/BMC.
   - **آرایه‌های ذخیره‌سازی HPE MSA** — از طریق XML API اختصاصی MSA.
   - **سوئیچ‌های Brocade / HPE B-Series SAN** — از طریق CLI سِ SSH‏ (Fabric OS).
   - **سوئیچ‌های Cisco Catalyst** — از طریق CLI سِ SSH، به‌همراه کابل‌کشی CDP/LLDP.
   - **فایروال‌های FortiGate** — از طریق REST API و SSH، به‌همراه کابل‌کشی LLDP.
2. برای هر سرور یا ذخیره‌سازی کشف‌شده، یک **دستگاه (device)** در NetBox **ایجاد یا به‌روزرسانی** می‌کند؛ اطلاعاتی نظیر سازنده، نوع دستگاه، نقش، سایت، شماره سریال و فیلدهای سفارشی (IP بورد BMC، نسخه فریم‌ور، خلاصه CPU/RAM/دیسک، وضعیت سلامت و …).
3. **انventory دقیق سخت‌افزاری** هر دستگاه (CPU، ماژول‌های RAM، دیسک‌ها، پاورها، کارت‌های شبکه، HBA، کنترلرها، باتری‌ها و FRU) را جمع‌آوری می‌کند و هر قطعه را به‌عنوان یک **inventory item** با کلید شماره سریال در NetBox همگام می‌سازد.
4. **آیتم‌های قدیمی inventory** که دیگر توسط دستگاه گزارش نمی‌شوند را حذف می‌کند.
5. **IP مدیریتی هر دستگاه** در IPAM ثبت می‌شود (ماسک از روی بازه اسکن، با پیش‌فرض `/32`؛ توضیح علامت‌دار `netbox-sync: mgmt`) و به‌عنوان **primary IPv4** دستگاه در NetBox تنظیم می‌گردد. IP به **رابط مدیریتی واقعی** تخصیص می‌یابد وقتی قابل شناسایی باشد — زیررابط VLAN در FortiGate (از روی IP رابط‌ها) یا SVI در سیسکو (از `show ip interface brief`؛ در صورت نبود، به‌صورت رابط مجازی ساخته می‌شود)؛ در غیر این صورت یک رابط ساختگی `mgmt` (نوع `virtual` و `mgmt_only`) آن را نگه می‌دارد. رابط‌های مدیریتی هرگز توسط همگام‌سازی رابط‌ها حذف نمی‌شوند. **alias** رابط‌های FortiGate به **label** رابط در NetBox نگاشت می‌شود.
6. **دستگاه‌هایی که دیگر پاسخگو نیستند** را در NetBox به‌صورت آفلاین (offline) علامت‌گذاری می‌کند.
7. **هر اجرا یک همگام‌سازی کامل** انجام می‌دهد و خارج می‌شود (کد خروجی سازگار با cron + فایل قفل) — زمان‌بندی را خودتان انجام می‌دهید (cron، systemd timer، Task Scheduler).

## نحوه کارکرد (معماری)

```
                ┌──────────────────────────────────────────────┐
                │              sync_all_to_netbox.py            │
                │                                              │
   .env  ─────► │  load_dotenv() → پیکربندی و اعتبارها         │
                │                                              │
                │  ┌────────────┐    ┌──────────────────────┐   │
   BMC_RANGES   │  │  scan_all()│───►│ probe_redfish(ip)    │   │
   ───────────► │  │            │    │  RedfishSession      │   │
                │  │ ThreadPool │    │  → login + GET tree  │   │
   STORAGE_     │  │ Executor   │    └──────────────────────┘   │
   RANGES ────► │  │            │───►│ probe_storage(ip)    │   │
                │  └─────┬──────┘    │  StorageSession      │   │
                │        │           │  → login + XML show  │   │
                │        ▼           └──────────────────────┘   │
                │  found = {servers:[...], storage:[...], san_switches:[...]}       │
                │        │                                     │
                │        ▼                                     │
                │  برای هر سرور:                               │
                │    ensure_server_device()  →  NetBox device  │
                │    rf_collect_inventory() →  NetBox items    │
                │    sync_inventory()       →  diff by serial  │
                │                                              │
                │  برای هر ذخیره‌سازی:                          │
                │    ensure_storage_device() →  NetBox device  │
                │    storage_collect_inventory() → items      │
                │    sync_inventory()        →  diff by serial  │
                │                                              │
                │  علامت‌گذاری دستگاه‌های غیرقابل‌دسترسی به‌صورت آفلاین │
                └──────────────────────────────────────────────┘
                                    │
                                    ▼
                         ┌────────────────────┐
                         │   NetBox (DCIM)    │
                         │  devices + items   │
                         └────────────────────┘
```

### نکات کلیدی طراحی

- **تکرارپذیر (Idempotent)**: هر بار اجرا، وضعیت NetBox را با واقعیت تطبیق می‌دهد؛ اجرای مجدد کاملاً بی‌خطر است.
- **اسکن موازی**: بررسی IPها با thread pool انجام می‌شود (`SCAN_WORKERS`، پیش‌فرض ۲۰).
- **مقاوم در برابر خطا**: هر دستگاه در `try/except` جداگانه قرار دارد — خرابی یک میزبان، کل اجرا را متوقف نمی‌کند. محدودیت نرخ درخواست (rate-limit) در MSA با بازگشت نمایی (exponential backoff) و ورود مجدد مدیریت می‌شود.
- **انventory مبتنی بر سریال**: قطعات بر اساس شماره سریال تطبیق و به‌روزرسانی می‌شوند؛ قطعات حذف‌شده از دستگاه پاکسازی و موارد تکراری ادغام می‌گردند.
- **بدون نشت اطلاعات حساس در مخزن**: تمام اعتبارها، IPها و نگاشت سایت‌ها در فایل `.env` (که gitignore شده) قرار دارند و تنها `.env.example` در مخزن قرار می‌گیرد.

## فایل‌های مخزن

| فایل | کاربرد |
|------|--------|
| `sync_all_to_netbox.py` | نقطه ورود سبک — اعتبارسنجی پیکربندی و اجرای زمان‌بند (`python sync_all_to_netbox.py` دقیقاً مثل قبل کار می‌کند). |
| `netbox_sync/` | پکیج پیاده‌سازی: `config` (محیط/اعتبارها/لاگ)، `utils` (توابع نام‌گذاری و ابزارهای IP)، `netbox` (لایه API سِ NetBox: CRUD، ساخت/آفلاین دستگاه، همگام‌سازی inventory)، `collectors/` (sessionها و جمع‌آوری inventory سِ `redfish`، `msa`، `brocade`، `cisco`، `fortigate`)، `scanner` (بررسی موازی IP)، `sync` (هماهنگ‌کننده `run_sync`). |
| `netbox_sync/collectors/cisco.py` | کلکتور Cisco Catalyst — اتصال SSH با netmiko، پارسرهای CLI سِ IOS/IOS-XE، همگام‌سازی کابل‌های CDP/LLDP. |
| `netbox_sync/collectors/fortigate.py` | کلکتور FortiGate — session سِ REST API به‌علاوه SSH (کابل‌های LLDP، ترانسسیورهای SFP). |
| `netbox_sync/models.py` | نگاشت‌های نرمال‌سازی نام مدل سرور (`SERVER_MODEL_MAP`)، ذخیره‌سازی (`STORAGE_MODEL_MAP`) و سوئچ SAN (`SWITCH_MODEL_MAP`). رشته‌های سازنده (مانند `proliant dl360 gen10`) را به نام‌های متعارف نوع دستگاه در NetBox (مانند `HPE DL360 G10`) تبدیل می‌کند. |
| `.env.example` | قالب فایل `.env`. آن را به `.env` کپی کرده و مقادیر واقعی خود را وارد کنید. |
| `requirements.txt` | وابستگی‌های پایتون (`requests`, `pynetbox`, `python-dotenv`, `paramiko`, `netmiko`). |
| `requirements-dev.txt` | وابستگی‌های تست (pytest)؛ شامل `requirements.txt` نیز می‌شود. |
| `tests/` | مجموعه تست pytest برای پارسرهای CLI، توابع نام‌گذاری و منطق همگام‌سازی NetBox — کاملاً با fakeهای درون‌حافظه‌ای اجرا می‌شود و به سخت‌افزار نیاز ندارد. |
| `.gitignore` | فایل‌های `.env`، `__pycache__/`، venv و پوشه‌های کاری شخصی را نادیده می‌گیرد. |

## پیش‌نیازها

- پایتون ۳.۹ یا بالاتر
- یک نمونه **NetBox** (نسخه ۳.x) در دسترس، به‌همراه token سِ API
- دسترسی شبکه از ماشینی که اسکریپت روی آن اجرا می‌شود به:
  - IPهای iLO/BMC روی `REDFISH_PORT` (پیش‌فرض ۴۴۳)
  - IPهای ذخیره‌سازی MSA روی `STORAGE_PORT` (پیش‌فرض ۴۴۳)
- سرورهای HPE ProLiant دارای iLO 4 یا iLO 5 (پشتیبان Redfish)
- آرایه‌های ذخیره‌سازی HPE MSA (نسل‌های ۲۰۴۰ / ۲۰۴۲ / ۲۰۵۰ / ۲۰۵۲ / ۲۰۶۰ / ۲۰۶۲)
- سوئچ‌های Brocade / HPE B-Series (مجهز به Fabric OS، قابلیت SSH)

نصب وابستگی‌ها:
```bash
pip install -r requirements.txt
```

## پیکربندی (`.env`)

فایل `.env.example` را به `.env` کپی کرده و ویرایش کنید. **تمام مقادیر حساس باید در `.env` قرار گیرند** — این فایل هرگز در git قرار نمی‌گیرد.

| متغیر | الزامی | پیش‌فرض | توضیح |
|--------|:------:|---------|-------|
| `NETBOX_URL` | ✅ | — | آدرس پایه NetBox شما (مانند `https://netbox.example.com`). |
| `NETBOX_TOKEN` | ✅ | — | API token سِ NetBox (با دسترسی خواندن/نوشتن). |
| `NETBOX_VERIFY_TLS` | ❌ | `false` | بررسی گواهی TLS سِ NetBox. برای گواهی‌های self-signed روی `false` باقی بماند. |
| `REDFISH_USER` | ✅ | — | نام کاربری BMC (iLO) برای ورود به Redfish. |
| `REDFISH_PASS` | ✅ | — | رمز عبور BMC (iLO). |
| `REDFISH_PORT` | ❌ | `443` | پورت TCP سِ Redfish روی BMC. |
| `STORAGE_USER` | ✅ | — | نام کاربری API ذخیره‌سازی MSA. |
| `STORAGE_PASS` | ✅ | — | رمز عبور API ذخیره‌سازی MSA. |
| `STORAGE_PORT` | ❌ | `443` | پورت TCP سِ XML API اختصاصی MSA. |
| `STORAGE_AUTH_HASH` | ❌ | `sha256` | الگوریتم hash برای اعتبار MSA (`sha256` یا `md5`). در صورت شکست، گزینه جایگزین به‌طور خودکار امتحان می‌شود. |
| `BMC_RANGES` | ❌* | CIDR نمونه | بازه‌های CIDR جدا‌شده با کاما برای اسکن سرورها. |
| `STORAGE_RANGES` | ❌* | CIDR نمونه | بازه‌های CIDR جدا‌شده با کاما برای اسکن ذخیره‌سازی. IPهایی که قبلاً به‌عنوان سرور یافت شده‌اند نادیده گرفته می‌شوند. |
| `SITE_KEYWORD_MAP` | ❌ | — | جفت‌های `keyword:SiteName` جداشده با کاما — وقتی استفاده می‌شود که هیچ بازه‌ای در `SITE_IP_MAP` مطابقت نداشته باشد. دستگاهی که hostname آن شامل کلیدواژه (بدون حساسیت به حروف بزرگ/کوچک) باشد، به آن سایت اختصاص می‌یابد. مثال: `dc1:Datacenter1,hq:HQ`. |
| `SITE_IP_MAP` | ❌ | — | جفت‌های `cidr:SiteName` جداشده با کاما. دستگاهی که IP آن داخل CIDR باشد به آن سایت اختصاص می‌یابد؛ **طولانی‌ترین پیشوند برنده است**. **قبل از** `SITE_KEYWORD_MAP` بررسی می‌شود. مثال: `172.31.0.0/16:HQ,172.31.1.0/24:Branch`. |
| `SCAN_WORKERS` | ❌ | `20` | اندازه thread pool برای اسکن موازی IP. |
| `OFFLINE_THRESHOLD` | ❌ | `2` | تعداد اسکن‌های متوالی که دستگاه باید غایب باشد تا آفلاین علامت بخورد (ضد نوسان). |
| `LOG_LEVEL` | ❌ | `INFO` | میزان جزئیات لاگ: `DEBUG`، `INFO`، `WARN`، `ERROR`. |
| `DEFAULT_SITE_NAME` | ❌ | `Default` | نام سایت پیش‌فرض در صورت عدم تطابق هیچ کلیدواژه‌ای. |
| `DEFAULT_ROLE_NAME` | ❌ | `Server` | نقش دستگاه در NetBox برای سرورها. |
| `DEFAULT_STORAGE_ROLE` | ❌ | `Storage` | نقش دستگاه در NetBox برای ذخیره‌سازی. |
| `SWITCH_USER` | ✅ | -- | نام کاربری SSH برای سوئچ‌های Brocade SAN. |
| `SWITCH_PASS` | ✅ | -- | رمز عبور SSH برای سوئچ‌های Brocade SAN. |
| `SWITCH_PORT` | ❌ | `22` | پورت SSH برای CLI سوئچ SAN. |
| `SWITCH_STRICT_HOST_KEY` | ❌ | `false` | بررسی host key سِ SSH سوئیچ‌ها بر اساس `known_hosts` سیستم (محافظت در برابر MITM). |
| `SAN_RANGES` | ❌* | CIDR نمونه | بازه‌های CIDR جداشده با کاما برای اسکن سوئچ‌های SAN. IP‌هایی که قبلاً به‌عنوان سرور/ذخیره‌سازی یافت شده‌اند نادیده گرفته می‌شوند. |
| `DEFAULT_SWITCH_ROLE` | ❌ | `SAN Switch` | نقش دستگاه در NetBox برای سوئچ‌های SAN. |
| `CISCO_USER` | ❌* | — | نام کاربری SSH برای سوئیچ‌های سیسکو (فقط وقتی `CISCO_RANGES` تنظیم شده الزامی است). |
| `CISCO_PASS` | ❌* | — | رمز عبور SSH برای سوئیچ‌های سیسکو. |
| `CISCO_PORT` | ❌ | `22` | پورت SSH برای سوئیچ‌های سیسکو. |
| `CISCO_RANGES` | ❌ | *(خالی)* | بازه‌های CIDR جداشده با کاما برای سوئیچ‌های سیسکو. خالی = خانواده غیرفعال. |
| `DEFAULT_CISCO_ROLE` | ❌ | `Switch` | نقش دستگاه در NetBox برای سوئیچ‌های سیسکو. |
| `FORTIGATE_USER` | ❌* | — | نام کاربری ادمین FortiGateها (احراز **session auth** (POST /logincheck) برای REST API + SSH)؛ وقتی `FORTIGATE_RANGES` تنظیم شده الزامی است. |
| `FORTIGATE_PASS` | ❌* | — | رمز عبور ادمین FortiGateها. |
| `FORTIGATE_PORT` | ❌ | `443` | پورت REST API. |
| `FORTIGATE_SSH_PORT` | ❌ | `22` | پورت SSH برای FortiGateها. |
| `FORTIGATE_RANGES` | ❌ | *(خالی)* | بازه‌های CIDR جداشده با کاما برای FortiGateها. خالی = خانواده غیرفعال. |
| `DEFAULT_FORTIGATE_ROLE` | ❌ | `Firewall` | نقش دستگاه در NetBox برای FortiGateها. |
| `RUCKUS_USER` | ❌* | — | نام کاربری SSH برای کنترلرهای Ruckus (وقتی `RUCKUS_RANGES` تنظیم شده الزامی است). |
| `RUCKUS_PASS` | ❌* | — | رمز عبور SSH برای کنترلرهای Ruckus. |
| `RUCKUS_PORT` | ❌ | `22` | پورت SSH برای کنترلرهای Ruckus. |
| `RUCKUS_RANGES` | ❌ | *(خالی)* | بازه‌های CIDR جداشده با کاما برای کنترلرهای Ruckus (VIP و/یا آدرس واحدها). خالی = خانواده غیرفعال. |
| `RUCKUS_HA_MAP` | ❌ | — | جفت‌های HA: به‌صورت `vip:primary,secondary` برای هر جفت، جداشده با `;`. یک جفت را به یک دستگاه خوشه ادغام می‌کند. |
| `DEFAULT_RUCKUS_ROLE` | ❌ | `Wireless Controller` | نقش دستگاه در NetBox برای کنترلرها. |
| `DEFAULT_AP_ROLE` | ❌ | `Access Point` | نقش دستگاه در NetBox برای APها. |

> *پیش‌فرض‌های موجود در `netbox_sync/config.py` صرفاً CIDR‌های **نمونه/تست** هستند (`192.0.2.0/27` = TEST-NET). بازه‌های واقعی خود را در `.env` تنظیم کنید — یا برای غیرفعال‌کردن کامل یک خانواده، مقدار آن را **خالی** بگذارید (مثلاً `BMC_RANGES=`)؛ در این صورت نه اسکنی انجام می‌شود و نه علامت‌گذاری آفلاین برای آن خانواده.

### نمونه `.env`

```dotenv
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=your-netbox-api-token

REDFISH_USER=netbox
REDFISH_PASS=changeme
REDFISH_PORT=443

STORAGE_USER=netbox
STORAGE_PASS=changeme
STORAGE_PORT=443
STORAGE_AUTH_HASH=sha256

SWITCH_USER=netbox
SWITCH_PASS=changeme
SWITCH_PORT=22

BMC_RANGES=192.0.2.0/27,198.51.100.0/27
STORAGE_RANGES=192.0.2.16/32,198.51.100.16/32
SAN_RANGES=192.0.2.32/29,198.51.100.32/29

SITE_KEYWORD_MAP=dc1:Datacenter1,hq:HQ

SCAN_WORKERS=20
DEFAULT_SITE_NAME=Default
DEFAULT_ROLE_NAME=Server
DEFAULT_STORAGE_ROLE=Storage
DEFAULT_SWITCH_ROLE=SAN Switch
```

## پیش‌نیازهای NetBox

### ۱. نقش‌های inventory item

نقش‌های inventory item **بر اساس نام** شناسایی شده و در اولین استفاده **به‌صورت خودکار ساخته** می‌شوند (سپس کش می‌گردند)، بنابراین نیازی به تنظیم دستی نیست. اگر ترجیح می‌دهید آن‌ها را از قبل بسازید (`/dcim/inventory-item-roles/`)، نام‌ها باید دقیقاً مطابق این جدول باشند:

| نام نقش | کاربرد |
|--------|--------|
| HDD | هارددیسک |
| SSD | دیسک جامد (SSD) |
| CPU | پردازنده |
| Memory | ماژول RAM |
| NIC | کارت شبکه |
| PSU | منبع تغذیه |
| Controller | کنترلر RAID / ذخیره‌سازی |
| HBA | هاست باس آداپتور / FC |
| Battery | باتری Smart Storage |
| SAS Exp | اکسپندر SAS / FRU |
| SFP | ترانسسیور SFP (سوئیچ SAN) |

> اگر از نسخه‌ای قدیمی‌تر که از ID ثابت (۱ تا ۱۲) استفاده می‌کرد ارتقا می‌دهید: تا وقتی نقش‌های فعلی شما همین نام‌ها را دارند، شناسایی و مجدداً استفاده می‌شوند — هیچ چیز نمی‌شکند. ID نقش‌ها به ترتیب ساخت در دیتابیس بستگی دارد و دیگر هیچ‌جای کد به آن‌ها ارجاع داده نمی‌شود.

### ۲. فیلدهای سفارشی

اسکریپت **custom fields** را روی دستگاه‌ها می‌نویسد. این فیلدها **در ابتدای هر همگام‌سازی به‌طور خودکار ساخته می‌شوند** (نوع شیء `dcim | device`، نمایش `if-set`) — نیازی به تنظیم دستی نیست. فهرست کامل، برای مرجع:

**برای سرورها:**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `bmc_ip` | Text | BMC IP |
| `redfish_enabled` | Boolean | Redfish فعال |
| `redfish_model` | Text | مدل Redfish |
| `redfish_power_state` | Text | وضعیت تغذیه |
| `redfish_bios_version` | Text | نسخه BIOS |
| `redfish_cpu_model` | Text | مدل CPU |
| `redfish_cpu_sockets` | Integer | تعداد سوکت‌های CPU |
| `redfish_cpu_cores` | Integer | تعداد هسته‌های CPU |
| `redfish_cpu_threads` | Integer | تعداد threadهای CPU |
| `redfish_ram_gib` | Integer | RAM (GiB) |
| `redfish_disk_total_gib` | Integer | کل ظرفیت دیسک (GiB) |

**برای ذخیره‌سازی:**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `storage_ip` | Text | IP ذخیره‌سازی |
| `storage_enabled` | Boolean | ذخیره‌سازی فعال |
| `storage_health` | Text | وضعیت سلامت |
| `storage_firmware` | Text | نسخه فریم‌ور |
| `storage_model` | Text | مدل |
| `storage_disk_count` | Integer | تعداد دیسک‌ها |
| `storage_total_capacity_gib` | Integer | کل ظرفیت (GiB) |

**برای سوئیچ‌های SAN (HPE B-Series / Brocade):**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `san_switch_ip` | Text | IP سوئیچ SAN |
| `san_switch_enabled` | Boolean | سوئیچ SAN فعال |
| `san_switch_wwn` | Text | WWN سوئیچ |
| `san_switch_firmware` | Text | فریم‌ور (Fabric OS) |
| `san_switch_model` | Text | مدل |
| `san_switch_port_count` | Integer | تعداد پورت‌ها |

**برای سوئیچ‌های Cisco Catalyst:**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `cisco_ip` | Text | IP سوئیچ سیسکو |
| `cisco_enabled` | Boolean | سوئیچ سیسکو فعال |
| `cisco_firmware` | Text | نسخه IOS |
| `cisco_model` | Text | مدل |
| `cisco_port_count` | Integer | تعداد پورت‌ها |

**برای فایروال‌های FortiGate:**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `fortigate_ip` | Text | IP فایروال FortiGate |
| `fortigate_enabled` | Boolean | FortiGate فعال |
| `fortigate_firmware` | Text | نسخه FortiOS |
| `fortigate_model` | Text | مدل |
| `fortigate_port_count` | Integer | تعداد پورت‌ها |
| `fortigate_ha_group` | Text | نام گروه خوشه HA |
| `fortigate_ha_mode` | Text | حالت HA (a-p / a-a) |
| `fortigate_ha_peer` | Text | واحدهای peer سِ HA |
| `fortigate_ha_role` | Text | نقش واحد بررسی‌شده |

**برای کنسول‌های UniFi OS:**

| فیلد سفارشی | نوع | برچسب |
|--------------|------|-------|
| `unifi_ip` | Text | IP کنسول UniFi |
| `unifi_enabled` | Boolean | UniFi فعال |
| `unifi_version` | Text | نسخه UniFi OS |
| `unifi_ap_count` | Integer | تعداد APهای مدیریت‌شده |
| `unifi_sites` | Integer | تعداد سایت‌ها |
**NAT → IPAM:** ورودی‌های **VIP** در FortiGate به آدرس‌های IPAM برای IP خارجی (`extip`) تبدیل می‌شوند با فیلد بومی **`nat_inside`** در NetBox که به آدرس سرور داخلی نگاشت‌شده اشاره دارد؛ **poolهای IP** به آدرس‌های ساده IPAM برای محدوده SNAT تبدیل می‌شوند. هر VIP با port-forwarding همچنین به یک **NetBox Service** (پروتکل + پورت) روی دستگاه تبدیل می‌شود که به IP خارجی پیوند خورده و سرور نگاشت‌شده در توضیح آن ثبت می‌شود — بنابراین VIPهایی که یک `extip` مشترک دارند دقت کامل per-port را حفظ می‌کنند (VIPهای بدون پورت فقط با آدرس نمایش داده می‌شوند). آدرس‌ها/سرویس‌های NAT علامت‌دار (`netbox-sync: nat …`) که دیگر گزارش نشوند پس از هر اجرا حذف می‌شوند؛ ورودی‌های دستی هرگز دست نمی‌خورند.

**خوشه‌های HA:** یک جفت FortiGate در حالت HA (فعال-غیرفعال یا فعال-فعال) به **یک دستگاه** در NetBox تبدیل می‌شود که نام و سریال آن از واحد primary گرفته می‌شود — با هر سریال واحد قابل شناسایی است، بنابراین لیست‌کردن هر دو واحد هرگز دستگاه تکراری نمی‌سازد. واحدهای peer در فیلدهای `fortigate_ha_*` ثبت می‌شوند و primary IPv4 از واحد primary پیروی می‌کند (بررسی واحد secondary هرگز آن را تغییر نمی‌دهد).

> حلقه تشخیص آفلاین، دستگاه‌ها را با فیلتر `cf_redfish_enabled=True` / `cf_storage_enabled=True` / `cf_san_switch_enabled=True` / `cf_cisco_enabled=True` / `cf_fortigate_enabled=True` / `cf_unifi_enabled=True` فیلتر می‌کند (سینتکس فیلتر custom field در NetBox).

### ۳. نقش‌ها و سایت‌های دستگاه

نقش‌های `Server`، `Storage` و `SAN Switch`، سازندگان `HPE` / `Brocade` و سایت‌ها **به‌طور خودکار** ساخته می‌شوند اگر از قبل وجود نداشته باشند. البته می‌توانید آن‌ها را پیش از اجرا نیز دستی بسازید.

## اجرای برنامه

```bash
cp .env.example .env   # سپس با مقادیر واقعی ویرایش کنید
pip install -r requirements.txt
python sync_all_to_netbox.py
```

هر اجرا **یک همگام‌سازی کامل انجام می‌دهد و خارج می‌شود**: کد خروجی `0` در صورت موفقیت، `1` در صورت خطا، `130` با Ctrl+C. یک فایل قفل (`netbox-sync.lock` در ریشه مخزن) از اجرای هم‌زمان چند نمونه جلوگیری می‌کند؛ قفل قدیمی‌تر از ۲۴ ساعت به‌عنوان stale در نظر گرفته شده (بازیابی پس از crash) و جایگزین می‌شود.

```
[2026-07-29 00:00:01] [INFO] ============================================================
[2026-07-29 00:00:01] [INFO] Unified sync started (servers + storage + SAN + Cisco switches)
[2026-07-29 00:00:01] [INFO] ============================================================
[2026-07-29 00:00:01] [INFO] BMC ranges empty — skipping server scan.
[2026-07-29 00:00:01] [INFO] Scanning 1 IPs for Cisco switches (SSH) ...
[2026-07-29 00:00:02] [INFO]   + CISCO 192.0.2.65  C9200L-48T-4X  s/n=XXXXXXX
...
```

برای توقف یک همگام‌سازی در حال اجرا، `Ctrl+C` را فشار دهید (در حین اسکن فعال ممکن است تا حدود ۲۰ ثانیه طول بکشد تا بررسی‌های در حال انجام تمام شوند؛ بررسی‌های در صف بلافاصله لغو می‌شوند).

### زمان‌بندی با cron (پیشنهادی)

```cron
# دو بار در روز (۰۰:۰۰ و ۱۲:۰۰)، لاگ‌ها به فایل اضافه می‌شوند
0 0,12 * * * /opt/netbox-sync/.venv/bin/python /opt/netbox-sync/sync_all_to_netbox.py >> /var/log/netbox-sync.log 2>&1
```

systemd timer یا Windows Task Scheduler نیز به همین خوبی کار می‌کند — هر چیزی که این دستور را دوره‌ای اجرا کند. اسکریپت فایل `.env` خود را صرف‌نظر از دایرکتوری فعلی، کنار مخزن پیدا می‌کند.

### اجرای تست‌ها

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

این مجموعه، پارسرهای CLI سِ Brocade، پردازش XML سِ MSA، نام‌گذاری آیتم‌ها و منطق همگام‌سازی NetBox (پاکسازی موارد قدیمی/تکراری، به‌روزرسانی در برابر ساخت جدید) را با fakeهای درون‌حافظه‌ای تست می‌کند — بدون نیاز به سخت‌افزار یا نمونه NetBox.

## سخت‌افزارهای پشتیبانی‌شده

**سرورها (Redfish / iLO):**
- HPE ProLiant DL360 / DL380 / DL320، از Gen8 تا Gen11
- iLO 4 (Gen9) و iLO 5 (Gen10/Gen11)
- شامل مکانیزم جایگزین (fallback) برای HPE SmartStorage در Gen9 با iLO4 (که داده کامل Redfish ذخیره‌سازی را ارائه نمی‌دهد) و تولید سریال‌های ساختگی (pseudo-serial) برای HBAهایی که سریال عرضه نمی‌کنند.

**ذخیره‌سازی (XML API اختصاصی MSA):**
- HPE MSA 2040، 2042، 2050، 2052، 2060، 2062
- تفاوت نام فیلدها بین `show disks` (فریم‌ور جدیدتر) و `show disk-parameters` (فریم‌ور قدیمی‌تر) به‌طور خودکار مدیریت می‌شود.

**سوئیچ‌های SAN (Brocade / HPE B-Series، CLI از طریق SSH):**
- HPE SN6010B/C، SN6500B/C، SN6700B، SN8600C، SN8700C و معادل‌های Brocade 300/320/5100/5300/6505/6510/6520/6547/7800/7840/DCX-4S/SX6.
- اتصال از طریق SSH و اجرای `switchshow`، `version`، `nsshow`، `nscamshow`، `sfpshow`.

**سوئیچ‌های LAN (Cisco Catalyst، IOS / IOS-XE، SSH از طریق netmiko):**
- خانواده‌های Catalyst 2960X / 3650 / 3850 / 9200 / 9300 (هر دو گویش IOS کلاسیک و IOS-XE).
- اتصال از طریق SSH و اجرای `show version`، `show inventory`، `show interfaces status`، `show vlan brief`، `show interfaces trunk`، `show cdp neighbors detail` (با `show lldp neighbors detail` به‌عنوان جایگزین).
- خانواده سیسکو **اختیاری** است: فقط وقتی `CISCO_RANGES` تنظیم شود فعال می‌شود.

**کنترلرهای بی‌سیم (Ruckus ZoneDirector، SSH):**
- کنترلرهای خانواده ZD1200 از طریق shell تعاملی (ورود دو مرحله‌ای `login:`/`Password:` به‌علاوه `enable`) — `show sysinfo` (هویت)، `show ap all` (APها)، `show wlan all` (SSIDها).
- دستگاه کنترلر با فیلدهای سفارشی `wlc_*`؛ هر AP به دستگاهی با نقش `Access Point` تبدیل می‌شود (هویت بر اساس MAC — فیلد `wap_mac`) و به کنترلر (`wap_wlc`) و گروه (`wap_group`) خود پیوند می‌خورد؛ APهای ناپدیدشده آفلاین علامت می‌خورند و هرگز حذف نمی‌شوند.
- WLANها به **Wireless LANهای** بومی NetBox تبدیل می‌شوند (SSID + نوع احراز هویت + پیوند VLAN از گروه‌های سایت). **عبارات عبور (passphrase) هرگز همگام‌سازی نمی‌شوند.**
- **جفت‌های HA:** با `RUCKUS_HA_MAP=vip:primary,secondary` (به‌ازای هر جفت، جداشده با `;`) یک جفت به یک دستگاه خوشه ادغام می‌شود — هویت فقط از بررسی VIP/primary گرفته می‌شود و بررسی‌های secondary فقط زنده‌بودن را به‌روزرسانی می‌کنند؛ primary IPv4 همان VIP است.
- **اختیاری**: فقط وقتی `RUCKUS_RANGES` تنظیم شود فعال می‌شود.

**کنترلرهای بی‌سیم (Ubiquiti UniFi OS، API بر بستر HTTPS):**
- کنسول‌های UniFi OS (UDM / CloudKey / UniFi OS Server با Network Application نسخه ۱۰.x) از طریق API نشست‌محور قدیمی: `POST /api/login` (کوکی نشست) ← `/api/self/sites` و سپس به‌ازای هر سایت `/api/s/<site>/stat/device` (APها)، `/api/s/<site>/rest/wlanconf` (WLANها) و `/api/s/<site>/rest/networkconf` (پیوندهای VLAN). یک حساب ادمین **محلی** اختصاصی بسازید (حساب‌های ابری UI.com به‌خاطر MFA اتوماسیون را می‌شکنند).
- کنسول به یک **دستگاه** با فیلدهای سفارشی `unifi_*` تبدیل می‌شود (هویت = سریال uuid کنسول). هر AP از ماشین‌آلات مشترک AP استفاده می‌کند: نقش `Access Point`، هویت بر اساس MAC (`wap_mac`)، فیلد `wap_group` = نام سایت UniFi و `wap_wlc` = نام کنسول؛ سایت NetBox هر AP از **تفکیک استاندارد SITE_IP_MAP روی IP سِ AP** به دست می‌آید (مثل همه خانواده‌های دیگر)؛ APهای ناپدیدشده آفلاین علامت می‌خورند و هرگز حذف نمی‌شوند.
- WLANهای **همه سایت‌ها** به **Wireless LANهای** بومی NetBox تبدیل می‌شوند (گروه `UniFi <console>`)، تجمیع سراسری به‌ازای هر SSID؛ پیوند VLAN هر WLAN از روی network binding آن به‌ازای هر سایت حل می‌شود (تطبیق یکتا در گروه‌های VLAN علامت‌دار سایت، وگرنه در گروه UniFi آن سایت ساخته می‌شود). **عبارات عبور (passphrase) هرگز همگام‌سازی نمی‌شوند.**
- **اختیاری**: فقط وقتی `UNIFI_RANGES` تنظیم شود فعال می‌شود.

**فایروالها (FortiGate، REST API + SSH):**
- خانواده FortiGate 40F / 60F / 80F / 100F / 200F (FortiOS 6/7). کوئری‌های `/api/v2/monitor/system/status`، `/api/v2/monitor/system/interface`، `/api/v2/cmdb/system/interface` (VDOM سِ `root`).
- احراز هویت با **session auth** (POST /logincheck) و اعتبار ادمین (`FORTIGATE_USER`/`FORTIGATE_PASS`) — بدون نیاز به توکن API.
- SSH برای اجرای `diagnose lldp neighbor-summary` (کابل‌ها) و `diagnose sys transceiver list` (ترانسسیورهای SFP).
- رابط‌های aggregate (port-channel) از cmdb به‌عنوان رابط `lag` در NetBox وارد می‌شوند؛ پورت‌های عضو با `lag` و زیررابط‌های VLAN با `parent` به رابط LAG/والد خود پیوند می‌خورند.
- **اختیاری**: فقط وقتی `FORTIGATE_RANGES` تنظیم شود فعال می‌شود.
- زیررابط‌های VLAN **با VLANهای موجود سوئیچ‌ها تطبیق داده می‌شوند** نه اینکه تکراری ساخته شوند: vid موجود در دقیقاً یک دامنه broadcast استفاده مجدد می‌شود، VLANهای مخصوص FortiGate در یک گروه per-device ساخته می‌شوند، و موارد هم‌پوشان با جستجوی MAC زیررابط در جدول MAC سوئیچ‌ها ابهام‌زدایی می‌شوند (`fnsysctl ifconfig -a` روی FortiGate و `show mac address-table address <mac>` روی سوئیچ‌ها).

برای مشاهده نگاشت کامل نام مدل‌ها به `netbox_sync/models.py` مراجعه کنید. می‌توانید مدل‌های جدید را نیز در همان فایل اضافه کنید.

## آیتم‌های inventory جمع‌آوری‌شده

**از هر سرور (Redfish):**

| قطعه | منبع | فیلدهای کلیدی |
|----------|--------|------------|
| CPU | `/Systems/1/Processors` | مدل، تعداد هسته، thread، سریال |
| Memory | `/Systems/1/Memory` | ظرفیت، سرعت، نوع، پارت‌نامبر، سریال |
| دیسک | `/Storage/*/Drives` + fallback سِ SmartStorage | مدل، ظرفیت، MediaType، پروتکل، سریال |
| کنترلر | `StorageControllers` + SmartStorage | مدل، فریم‌ور، سریال |
| PSU | `/Chassis/*/Power/PowerSupplies` | مدل، توان (وات)، سریال |
| NIC | `/NetworkAdapters` + PCIe devices | نام، فریم‌ور، MAC، پارت‌نامبر، سریال |
| HBA | PCIe FRUها (Gen10) + pseudo-serial (Gen9 iLO4) | نام، فریم‌ور، سریال |
| باتری | Oem.Hpe.Battery (Gen9) + SmartStorageBattery (Gen10) | مدل، فریم‌ور، سریال |

**از هر ذخیره‌سازی (MSA):**

| قطعه | دستور `show` در MSA | فیلدهای کلیدی |
|----------|--------------------|------------|
| دیسک | `show disks` / `show disk-parameters` / `show disk-statistics` | مدل، اندازه، نوع، سریال، وضعیت سلامت |
| کنترلر | `show controllers` | controller-id، فریم‌ور، IP، سلامت، سریال |
| PSU | `show power-supplies` | مکان، سلامت، وضعیت، سریال |
| FRU / اکسپندر SAS | `show frus` / `show enclosure-fru` | نام، مکان، سلامت، سریال |

**از هر سوئیچ SAN (Brocade CLI):**

| قطعه | دستور CLI | فیلدهای کلیدی |
|----------|-------------|------------|
| هویت سوئیچ | `switchshow` | WWN سوئیچ، مدل، نام سوئیچ |
| فریم‌ور | `version` | نسخه Fabric OS |
| پورت‌های FC (رابط‌های NetBox) | `switchshow` | ایندکس، پورت، مدیا، سرعت، وضعیت، WWN متصل |
| Name server (دستگاه‌های لاگین‌شده) | `nsshow` / `nscamshow` | port id، port WWN، node WWN |
| ترانسسیورهای SFP | `sfpshow` | سازنده، پارت‌نامبر، سریال، دما |

## نحوه تطبیق دستگاه‌ها

هر دستگاه کشف‌شده عمدتاً از طریق **شماره سریال** با دستگاه موجود در NetBox تطبیق داده می‌شود. اگر سریال نامعتبر یا ناموجود باشد، جستجوی ثانویه بر اساس **نام + سایت + نقش** انجام می‌شود. این رویکرد از ایجاد دستگاه‌های تکراری بین اجراهای مختلف جلوگیری می‌کند.

در مورد ذخیره‌سازی، جستجوی ثانویه همچنین از تداخل با سروری هم‌نام جلوگیری می‌کند (با بررسی نبود فیلد سفارشی `bmc_ip`).

## کابل‌کشی CDP/LLDP (سیسکو)

برای هر سوئیچ سیسکو کشف‌شده، اسکریپت خروجی `show cdp neighbors detail` را می‌خواند (و در صورت خالی بودن، از `show lldp neighbors detail` استفاده می‌کند) و **کابل‌هایی** در NetBox بین رابط‌های سوئیچ و رابط‌های همسایه شناسایی‌شده ایجاد می‌کند:

- کابل فقط وقتی ساخته می‌شود که **هر دو سر لینک شناسایی شوند**: hostname همسایه (بدون پسوند دامنه) باید با یک دستگاه NetBox مطابقت کند **و** رابط راه‌دور روی آن وجود داشته باشد. در غیر این صورت با لاگ DEBUG رد می‌شود — به‌ویژه لینک‌های سیسکو↔سرور، چون کارت‌های شبکه سرور در این ابزار inventory item هستند، نه interface.
- کابل‌های ساخته‌شده توسط همگام‌سازی پیشوند `netbox-sync:` در description دارند. فقط کابل‌های **علامت‌دار** به‌روزرسانی یا حذف می‌شوند (موارد قدیمی وقتی همسایه دیگر گزارش نشود پاک می‌شوند). **کابل‌های دستی هرگز تغییر یا حذف نمی‌شوند.**

## همگام‌سازی VLAN (سیسکو)

VLANهای `show vlan brief` بر اساس **دامنه broadcast مشتق از توپولوژی CDP** در IPAM گروه‌بندی می‌شوند: سوئیچ‌هایی که یکدیگر را به‌عنوان همسایه CDP می‌بینند (یال‌های درون یک سایت) اجزای متصل را تشکیل می‌دهند و هر جزء به یک **VLAN group** با نام `BD1`، `BD2`… نگاشت می‌شود. کلید گروه (در description، `netbox-sync: vtp=<key>`) دامنه VTP یکی از اعضا را ترجیح می‌دهد (با حروف کوچک)، در غیر این صورت اولین hostname — پایدار بین اجراها. این روش سوئیچ‌های بدون دامنه VTP (transparent) را نیز درست مدیریت می‌کند: جزیره سوئیچ‌های متصل به جای یک گروه per-switch، یک گروه مشترک می‌گیرند. VLANهای با ID هم‌پوشان در یک سایت در گروه‌های اجزای مختلف کنار هم قرار می‌گیرند. اتصال VLAN رابط‌ها (untagged در access، native + tagged در trunk) مانند قبل انجام می‌شود. VLANهای علامت‌دار (`netbox-sync:`) که دیگر هیچ عضوی از گروه گزارش نکند و گروه‌های تکراری قدیمی (انواع حروف بزرگ/کوچک، fallbackهای رها شده per-switch) پس از هر اجرا حذف می‌شوند؛ VLANها/گروه‌های دستی هرگز تغییر یا حذف نمی‌شوند.

## پیشوندها و آدرس‌های IPAM

**پیشوندها (prefix)** از IP رابط‌های FortiGate استخراج می‌شوند (`ip + mask` در پیکربندی cmdb — ماسک واقعی، مثل `172.31.2.0/24` و `79.127.120.176/28`) و با پیوند سایت و VLAN در IPAM ساخته/به‌روزرسانی می‌شوند (علامت `netbox-sync:`). هر CIDR در `SITE_IP_MAP` نیز به‌عنوان پیشوند **container** والد همگام‌سازی می‌شود (علامت `netbox-sync: last seen parent <site>`) تا زیرشبکه‌های کشف‌شده در یک سلسله‌مراتب تمیز قرار گیرند؛ والدهایی که ورودی‌شان از نگاشت حذف شود پاک می‌شوند. **آدرس‌های gateway/host**: IPهای زیررابط FortiGate به زیررابط‌هایشان تخصیص می‌یابند؛ IPهای SVI سیسکو (`show ip interface brief`) داخل طولانی‌ترین پیشوند منطبق قرار می‌گیرند و به SVI خود تخصیص می‌یابند (در صورت نبود، به‌صورت رابط مجازی ساخته می‌شوند). پیشوندها و آدرس‌های علامت‌داری که دیگر گزارش نشوند پس از هر اجرا حذف می‌شوند؛ ورودی‌های دستی IPAM و آدرس‌های `netbox-sync: mgmt` هرگز دست نمی‌خورند.

## تشخیص آفلاین

پس از هر همگام‌سازی، اسکریپت تمام دستگاه‌هایی که `redfish_enabled=True` (سرورها)، `storage_enabled=True` (ذخیره‌سازی)، `san_switch_enabled=True` (سوئیچ‌های SAN) یا `cisco_enabled=True` (سوئیچ‌های سیسکو) دارند را از NetBox استعلام می‌کند. اگر IP ذخیره‌شده BMC/ذخیره‌سازی/SAN دستگاه در اسکن فعلی **دیده نشده باشد**، یک شمارنده غیبت افزایش می‌یابد؛ تنها پس از `OFFLINE_THRESHOLD` **غیبت متوالی** (پیش‌فرض ۲) دستگاه با `status=offline` و فلگ `*_enabled=false` علامت‌گذاری می‌شود — این کار از علامت‌گذاری اشتباه آفلاین به‌دلیل کندی موقتی جلوگیری می‌کند. دستگاه **حذف نمی‌شود** — اسکن موفق بعدی آن را به `active` بازمی‌گرداند و شمارنده را صفر می‌کند.
