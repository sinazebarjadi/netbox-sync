# NetBox HPE Sync

> **English** documentation below · مستندات **فارسی** در ادامه

A Python automation tool that automatically discovers HPE ProLiant servers (via Redfish/iLO) and HPE MSA storage arrays (via the XML API) on your network, then synchronizes their hardware inventory into [NetBox](https://github.com/netbox-community/netbox) DCIM. It creates, updates, and marks devices offline, and keeps per-component inventory (CPU, RAM, disks, PSUs, NICs, HBAs, controllers, batteries, FRUs) in sync — running on a daily scheduler.

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

1. **Scans IP ranges** you define (CIDR notation) for two kinds of devices:
   - **HPE ProLiant servers** — detected via the Redfish API on the iLO BMC.
   - **HPE MSA storage arrays** — detected via the MSA XML API.
2. **Creates or updates** a NetBox **device** for each discovered server/storage unit, including manufacturer, device type, role, site, serial, and custom fields (BMC IP, firmware, CPU/RAM/disk summaries, health…).
3. **Collects detailed hardware inventory** from each device (CPUs, RAM modules, disks, PSUs, NICs, HBAs, controllers, batteries, FRUs) and syncs each component as a NetBox **inventory item** keyed by serial number.
4. **Removes stale inventory items** that are no longer reported by the device.
5. **Marks devices offline** in NetBox when they stop responding to the scan.
6. **Runs automatically** on a schedule (default: 00:00 and 12:00 daily) plus an immediate run on startup.

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
                │  found = {servers:[...], storage:[...]}       │
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
| `sync_all_to_netbox.py` | Main automation script — scanner, collectors, NetBox sync, scheduler. |
| `models.py` | Server (`SERVER_MODEL_MAP`) and storage (`STORAGE_MODEL_MAP`) model-name normalization maps. Maps vendor strings (e.g. `proliant dl360 gen10`) to canonical NetBox device-type names (e.g. `HPE DL360 G10`). |
| `.env.example` | Template for your `.env` file. Copy to `.env` and fill in real values. |
| `requirements` | Python dependencies (`requests`, `pynetbox`, `schedule`, `python-dotenv`). |
| `.gitignore` | Ignores `.env`, `__pycache__/`, venvs, and any personal working folders. |

## Requirements

- Python 3.8+
- A reachable **NetBox** instance (v3.x) with an API token
- Network access from the host running this script to:
  - iLO/BMC IPs on `REDFISH_PORT` (default 443)
  - MSA storage IPs on `STORAGE_PORT` (default 443)
- HPE ProLiant servers with iLO 4 / iLO 5 (Redfish capable)
- HPE MSA storage arrays (2040 / 2042 / 2050 / 2052 / 2060 class)

Install dependencies:
```bash
pip install -r requirements
```

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit. **All sensitive values must live in `.env`** — never commit the real file.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `NETBOX_URL` | ✅ | — | Base URL of your NetBox instance (e.g. `https://netbox.example.com`). |
| `NETBOX_TOKEN` | ✅ | — | NetBox API token (read/write). |
| `REDFISH_USER` | ✅ | — | BMC (iLO) username for Redfish login. |
| `REDFISH_PASS` | ✅ | — | BMC (iLO) password. |
| `REDFISH_PORT` | ❌ | `443` | TCP port for Redfish on the BMC. |
| `STORAGE_USER` | ✅ | — | MSA storage API username. |
| `STORAGE_PASS` | ✅ | — | MSA storage API password. |
| `STORAGE_PORT` | ❌ | `443` | TCP port for the MSA XML API. |
| `STORAGE_AUTH_HASH` | ❌ | `sha256` | Hash algorithm for MSA credential hash (`sha256` or `md5`). Falls back automatically if one fails. |
| `BMC_RANGES` | ❌* | example CIDRs | Comma-separated CIDR ranges to scan for servers. |
| `STORAGE_RANGES` | ❌* | example CIDRs | Comma-separated CIDR ranges to scan for storage. IPs already found as servers are skipped. |
| `SITE_KEYWORD_MAP` | ❌ | — | Comma-separated `keyword:SiteName` pairs. A device whose hostname contains the keyword (case-insensitive) is assigned that site. e.g. `dc1:Datacenter1,hq:HQ`. |
| `SCAN_WORKERS` | ❌ | `20` | Thread-pool size for parallel IP scanning. |
| `DEFAULT_SITE_NAME` | ❌ | `Default` | Fallback site name when no keyword matches. |
| `DEFAULT_ROLE_NAME` | ❌ | `Server` | NetBox device role for servers. |
| `DEFAULT_STORAGE_ROLE` | ❌ | `Storage` | NetBox device role for storage arrays. |

> *The shipped defaults in `sync_all_to_netbox.py` are **documentation-only** placeholder CIDRs (`192.0.2.0/27` = TEST-NET). Set `BMC_RANGES` and `STORAGE_RANGES` in `.env` to your real ranges.

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

BMC_RANGES=192.0.2.0/27,198.51.100.0/27
STORAGE_RANGES=192.0.2.16/32,198.51.100.16/32

SITE_KEYWORD_MAP=dc1:Datacenter1,hq:HQ

SCAN_WORKERS=20
DEFAULT_SITE_NAME=Default
DEFAULT_ROLE_NAME=Server
DEFAULT_STORAGE_ROLE=Storage
```

## NetBox prerequisites

### 1. Inventory item roles

The script assigns inventory-item **roles** by **hardcoded ID**. Ensure these roles exist in NetBox (`/dcim/inventory-item-roles/`) with these IDs (create them in this order, or adjust the `ROLE_*` constants at the top of the script):

| ID | Role name | Used for |
|----|-----------|----------|
| 1  | HDD       | Hard disk drives |
| 2  | SSD       | Solid-state drives |
| 3  | CPU       | Processors |
| 4  | Memory    | RAM modules |
| 5  | NIC       | Network adapters |
| 6  | PSU       | Power supplies |
| 7  | Controller| RAID / storage controllers |
| 8  | HBA       | Host bus adapters / FC |
| 9  | Battery   | Smart storage batteries |
| 10 | SAS Exp   | SAS expanders / FRUs |

### 2. Custom fields

The script writes **custom fields** on devices. Create these in NetBox (`/extras/custom-fields/`, object type `dcim | device`):

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

> The offline-detection loop filters devices via `cf_redfish_enabled=True` / `cf_storage_enabled=True` (NetBox custom-field filter syntax).

### 3. Device roles & sites

`Server` and `Storage` device roles, `HPE` manufacturer, and sites are **auto-created** if missing. You may also pre-create them.

## Running

```bash
cp .env.example .env   # then edit with your real values
pip install -r requirements
python sync_all_to_netbox.py
```

The script:
1. Runs an **immediate** sync on startup.
2. Schedules daily runs at **00:00** and **12:00**.
3. Logs every action to stdout with timestamps and log levels (`INFO` / `WARN` / `ERROR`).

```
[2026-06-30 00:00:01] [INFO] Scheduler started — runs at 00:00 and 12:00 daily.
[2026-06-30 00:00:01] [INFO] Running initial unified sync now ...
[2026-06-30 00:00:01] [INFO] ============================================================
[2026-06-30 00:00:01] [INFO] Unified sync started (servers + storage)
[2026-06-30 00:00:02] [INFO] Scanning 62 IPs across 2 BMC ranges ...
[2026-06-30 00:00:15] [INFO]   + SERVER 192.0.2.5  HPE DL360 G10  s/n=XXXXXXX
...
```

Press `Ctrl+C` to stop the scheduler.

### Run as a service (optional)

For production, run under systemd, a Windows service, or a container so it survives reboots.

## Supported hardware

**Servers (Redfish / iLO):**
- HPE ProLiant DL360 / DL380 / DL320, Gen8 through Gen11
- iLO 4 (Gen9) and iLO 5 (Gen10/Gen11)
- Includes HPE SmartStorage fallback for Gen9 iLO4 (which lacks full Redfish storage data), with pseudo-serial generation for HBAs that don't expose serials.

**Storage (MSA XML API):**
- HPE MSA 2040, 2042, 2050, 2052, 2060
- Handles field-name differences between `show disks` (newer firmware) and `show disk-parameters` (older firmware).

See `models.py` for the full model alias maps. Add your own models there.

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

## How devices are matched

Each discovered device is matched to an existing NetBox device by **serial number** (primary). If the serial is invalid/missing, a secondary lookup by **name + site + role** is used. This prevents duplicate devices across runs.

For storage, the secondary lookup also avoids clashing with a server that has the same name (it checks the `bmc_ip` custom field is absent).

## Offline detection

After each sync, the script queries NetBox for all devices where `redfish_enabled=True` (servers) or `storage_enabled=True` (storage). If a device's stored BMC/storage IP was **not** seen in the current scan, it is marked `status=offline` and its `*_enabled` flag is set to `false`. It is **not** deleted — the next successful scan flips it back to `active`.

---

# فارسی

## این برنامه چه می‌کند

1. **بازه‌های IP** که شما تعریف کرده‌اید (به‌صورت CIDR) را برای دو نوع دستگاه اسکن می‌کند:
   - **سرورهای HPE ProLiant** — از طریق API سِ Redfish روی iLO/BMC.
   - **آرایه‌های ذخیره‌سازی HPE MSA** — از طریق XML API اختصاصی MSA.
2. برای هر سرور یا ذخیره‌سازی کشف‌شده، یک **دستگاه (device)** در NetBox **ایجاد یا به‌روزرسانی** می‌کند؛ اطلاعاتی نظیر سازنده، نوع دستگاه، نقش، سایت، شماره سریال و فیلدهای سفارشی (IP بورد BMC، نسخه فریم‌ور، خلاصه CPU/RAM/دیسک، وضعیت سلامت و …).
3. **انventory دقیق سخت‌افزاری** هر دستگاه (CPU، ماژول‌های RAM، دیسک‌ها، پاورها، کارت‌های شبکه، HBA، کنترلرها، باتری‌ها و FRU) را جمع‌آوری می‌کند و هر قطعه را به‌عنوان یک **inventory item** با کلید شماره سریال در NetBox همگام می‌سازد.
4. **آیتم‌های قدیمی inventory** که دیگر توسط دستگاه گزارش نمی‌شوند را حذف می‌کند.
5. **دستگاه‌هایی که دیگر پاسخگو نیستند** را در NetBox به‌صورت آفلاین (offline) علامت‌گذاری می‌کند.
6. **به‌صورت خودکار و بر اساس زمان‌بندی** اجرا می‌شود (پیش‌فرض: هر روز ساعت ۰۰:۰۰ و ۱۲:۰۰)، به‌علاوه یک اجرای بلافاصله پس از راه‌اندازی.

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
                │  found = {servers:[...], storage:[...]}       │
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
| `sync_all_to_netbox.py` | اسکریپت اصلی اتوماسیون — شامل اسکنر، collectorها، همگام‌سازی با NetBox و زمان‌بند. |
| `models.py` | نگاشت‌های نرمال‌سازی نام مدل سرور (`SERVER_MODEL_MAP`) و ذخیره‌سازی (`STORAGE_MODEL_MAP`). رشته‌های سازنده (مانند `proliant dl360 gen10`) را به نام‌های متعارف نوع دستگاه در NetBox (مانند `HPE DL360 G10`) تبدیل می‌کند. |
| `.env.example` | قالب فایل `.env`. آن را به `.env` کپی کرده و مقادیر واقعی خود را وارد کنید. |
| `requirements` | وابستگی‌های پایتون (`requests`, `pynetbox`, `schedule`, `python-dotenv`). |
| `.gitignore` | فایل‌های `.env`، `__pycache__/`، venv و پوشه‌های کاری شخصی را نادیده می‌گیرد. |

## پیش‌نیازها

- پایتون ۳.۸ یا بالاتر
- یک نمونه **NetBox** (نسخه ۳.x) در دسترس، به‌همراه token سِ API
- دسترسی شبکه از ماشینی که اسکریپت روی آن اجرا می‌شود به:
  - IPهای iLO/BMC روی `REDFISH_PORT` (پیش‌فرض ۴۴۳)
  - IPهای ذخیره‌سازی MSA روی `STORAGE_PORT` (پیش‌فرض ۴۴۳)
- سرورهای HPE ProLiant دارای iLO 4 یا iLO 5 (پشتیبان Redfish)
- آرایه‌های ذخیره‌سازی HPE MSA (نسل‌های ۲۰۴۰ / ۲۰۴۲ / ۲۰۵۰ / ۲۰۵۲ / ۲۰۶۰ / ۲۰۶۲)

نصب وابستگی‌ها:
```bash
pip install -r requirements
```

## پیکربندی (`.env`)

فایل `.env.example` را به `.env` کپی کرده و ویرایش کنید. **تمام مقادیر حساس باید در `.env` قرار گیرند** — این فایل هرگز در git قرار نمی‌گیرد.

| متغیر | الزامی | پیش‌فرض | توضیح |
|--------|:------:|---------|-------|
| `NETBOX_URL` | ✅ | — | آدرس پایه NetBox شما (مانند `https://netbox.example.com`). |
| `NETBOX_TOKEN` | ✅ | — | API token سِ NetBox (با دسترسی خواندن/نوشتن). |
| `REDFISH_USER` | ✅ | — | نام کاربری BMC (iLO) برای ورود به Redfish. |
| `REDFISH_PASS` | ✅ | — | رمز عبور BMC (iLO). |
| `REDFISH_PORT` | ❌ | `443` | پورت TCP سِ Redfish روی BMC. |
| `STORAGE_USER` | ✅ | — | نام کاربری API ذخیره‌سازی MSA. |
| `STORAGE_PASS` | ✅ | — | رمز عبور API ذخیره‌سازی MSA. |
| `STORAGE_PORT` | ❌ | `443` | پورت TCP سِ XML API اختصاصی MSA. |
| `STORAGE_AUTH_HASH` | ❌ | `sha256` | الگوریتم hash برای اعتبار MSA (`sha256` یا `md5`). در صورت شکست، گزینه جایگزین به‌طور خودکار امتحان می‌شود. |
| `BMC_RANGES` | ❌* | CIDR نمونه | بازه‌های CIDR جدا‌شده با کاما برای اسکن سرورها. |
| `STORAGE_RANGES` | ❌* | CIDR نمونه | بازه‌های CIDR جدا‌شده با کاما برای اسکن ذخیره‌سازی. IPهایی که قبلاً به‌عنوان سرور یافت شده‌اند نادیده گرفته می‌شوند. |
| `SITE_KEYWORD_MAP` | ❌ | — | جفت‌های `keyword:SiteName` جدا‌شده با کاما. دستگاهی که hostname آن شامل کلیدواژه (بدون حساسیت به حروف بزرگ/کوچک) باشد، به آن سایت اختصاص می‌یابد. مثال: `dc1:Datacenter1,hq:HQ`. |
| `SCAN_WORKERS` | ❌ | `20` | اندازه thread pool برای اسکن موازی IP. |
| `DEFAULT_SITE_NAME` | ❌ | `Default` | نام سایت پیش‌فرض در صورت عدم تطابق هیچ کلیدواژه‌ای. |
| `DEFAULT_ROLE_NAME` | ❌ | `Server` | نقش دستگاه در NetBox برای سرورها. |
| `DEFAULT_STORAGE_ROLE` | ❌ | `Storage` | نقش دستگاه در NetBox برای ذخیره‌سازی. |

> *پیش‌فرض‌های موجود در `sync_all_to_netbox.py` صرفاً CIDR‌های **نمونه/تست** هستند (`192.0.2.0/27` = TEST-NET). حتماً بازه‌های واقعی خود را در `.env` تنظیم کنید.

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

BMC_RANGES=192.0.2.0/27,198.51.100.0/27
STORAGE_RANGES=192.0.2.16/32,198.51.100.16/32

SITE_KEYWORD_MAP=dc1:Datacenter1,hq:HQ

SCAN_WORKERS=20
DEFAULT_SITE_NAME=Default
DEFAULT_ROLE_NAME=Server
DEFAULT_STORAGE_ROLE=Storage
```

## پیش‌نیازهای NetBox

### ۱. نقش‌های inventory item

اسکریپت نقش‌های inventory item را با **ID ثابت** اختصاص می‌دهد. مطمئن شوید این نقش‌ها در NetBox (`/dcim/inventory-item-roles/`) با همین IDها وجود داشته باشند (به‌ترتیب زیر ایجاد کنید، یا ثابت‌های `ROLE_*` را در ابتدای اسکریپت اصلاح کنید):

| ID | نام نقش | کاربرد |
|----|--------|--------|
| 1  | HDD | هارددیسک |
| 2  | SSD | دیسک جامد (SSD) |
| 3  | CPU | پردازنده |
| 4  | Memory | ماژول RAM |
| 5  | NIC | کارت شبکه |
| 6  | PSU | منبع تغذیه |
| 7  | Controller | کنترلر RAID / ذخیره‌سازی |
| 8  | HBA | هاست باس آداپتور / FC |
| 9  | Battery | باتری Smart Storage |
| 10 | SAS Exp | اکسپندر SAS / FRU |

### ۲. فیلدهای سفارشی

اسکریپت **custom fields** را روی دستگاه‌ها می‌نویسد. این فیلدها را در NetBox (`/extras/custom-fields/`، با نوع شیء `dcim | device`) ایجاد کنید:

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

> حلقه تشخیص آفلاین، دستگاه‌ها را با فیلتر `cf_redfish_enabled=True` / `cf_storage_enabled=True` فیلتر می‌کند (سینتکس فیلتر custom field در NetBox).

### ۳. نقش‌ها و سایت‌های دستگاه

نقش‌های `Server` و `Storage`، سازنده `HPE` و سایت‌ها **به‌طور خودکار** ساخته می‌شوند اگر از قبل وجود نداشته باشند. البته می‌توانید آن‌ها را پیش از اجرا نیز دستی بسازید.

## اجرای برنامه

```bash
cp .env.example .env   # سپس با مقادیر واقعی ویرایش کنید
pip install -r requirements
python sync_all_to_netbox.py
```

اسکریپت:
1. بلافاصله پس از راه‌اندازی، یک همگام‌سازی **اولیه** انجام می‌دهد.
2. اجرای روزانه را در **۰۰:۰۰** و **۱۲:۰۰** زمان‌بندی می‌کند.
3. هر اقدام را با timestamp و سطح لاگ (`INFO` / `WARN` / `ERROR`) در stdout ثبت می‌کند.

```
[2026-06-30 00:00:01] [INFO] Scheduler started — runs at 00:00 and 12:00 daily.
[2026-06-30 00:00:01] [INFO] Running initial unified sync now ...
[2026-06-30 00:00:01] [INFO] ============================================================
[2026-06-30 00:00:01] [INFO] Unified sync started (servers + storage)
[2026-06-30 00:00:02] [INFO] Scanning 62 IPs across 2 BMC ranges ...
[2026-06-30 00:00:15] [INFO]   + SERVER 192.0.2.5  HPE DL360 G10  s/n=XXXXXXX
...
```

برای توقف زمان‌بند، `Ctrl+C` را فشار دهید.

### اجرا به‌عنوان سرویس (اختیاری)

برای محیط عملیاتی، توصیه می‌شود اسکریپت را زیر systemd، به‌صورت سرویس ویندوز یا درون کانتینر اجرا کنید تا پس از ریبوت نیز فعال بماند.

## سخت‌افزارهای پشتیبانی‌شده

**سرورها (Redfish / iLO):**
- HPE ProLiant DL360 / DL380 / DL320، از Gen8 تا Gen11
- iLO 4 (Gen9) و iLO 5 (Gen10/Gen11)
- شامل مکانیزم جایگزین (fallback) برای HPE SmartStorage در Gen9 با iLO4 (که داده کامل Redfish ذخیره‌سازی را ارائه نمی‌دهد) و تولید سریال‌های ساختگی (pseudo-serial) برای HBAهایی که سریال عرضه نمی‌کنند.

**ذخیره‌سازی (XML API اختصاصی MSA):**
- HPE MSA 2040، 2042، 2050، 2052، 2060، 2062
- تفاوت نام فیلدها بین `show disks` (فریم‌ور جدیدتر) و `show disk-parameters` (فریم‌ور قدیمی‌تر) به‌طور خودکار مدیریت می‌شود.

برای مشاهده نگاشت کامل نام مدل‌ها به `models.py` مراجعه کنید. می‌توانید مدل‌های جدید را نیز در همان فایل اضافه کنید.

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

## نحوه تطبیق دستگاه‌ها

هر دستگاه کشف‌شده عمدتاً از طریق **شماره سریال** با دستگاه موجود در NetBox تطبیق داده می‌شود. اگر سریال نامعتبر یا ناموجود باشد، جستجوی ثانویه بر اساس **نام + سایت + نقش** انجام می‌شود. این رویکرد از ایجاد دستگاه‌های تکراری بین اجراهای مختلف جلوگیری می‌کند.

در مورد ذخیره‌سازی، جستجوی ثانویه همچنین از تداخل با سروری هم‌نام جلوگیری می‌کند (با بررسی نبود فیلد سفارشی `bmc_ip`).

## تشخیص آفلاین

پس از هر همگام‌سازی، اسکریپت تمام دستگاه‌هایی که `redfish_enabled=True` (سرورها) یا `storage_enabled=True` (ذخیره‌سازی) دارند را از NetBox استعلام می‌کند. اگر IP ذخیره‌شده BMC/ذخیره‌سازی دستگاه در اسکن فعلی **دیده نشده باشد**، وضعیت آن به `status=offline` و فلگ `*_enabled` آن به `false` تغییر می‌کند. دستگاه **حذف نمی‌شود** — اسکن موفق بعدی آن را مجدداً به `active` بازمی‌گرداند.
