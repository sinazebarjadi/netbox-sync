<div align="center">

# 🛰️ NetBox Infrastructure Sync

**One Python tool that scans your entire infrastructure and keeps NetBox as the always-accurate source of truth.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![NetBox](https://img.shields.io/badge/netbox-4.x-blueviolet)
![Tests](https://img.shields.io/badge/tests-288%20passing-brightgreen)
![Device families](https://img.shields.io/badge/device%20families-10%2BME-orange)

Servers · Storage · SAN & LAN switches · Firewalls · Wireless · NVRs & Hard Drives · Cameras · ManageEngine AssetExplorer inventory enrichment — devices, interfaces, VLANs, IPAM, cables, and hardware inventory.

🇬🇧 **English below** · مستندات **فارسی** در انتها 🇮🇷

</div>

---

## 📚 Table of Contents

- [Overview](#-overview)
- [Architecture & Workflow](#-architecture--workflow)
- [What gets scanned (Discovery)](#-what-gets-scanned-discovery)
- [ManageEngine AssetExplorer Sync](#-manageengine-assetexplorer-sync)
- [What gets created in NetBox](#-what-gets-created-in-netbox)
- [Getting started](#-getting-started)
- [Configuration reference](#-configuration-reference)
- [Running & scheduling (Automated Pipeline)](#%EF%B8%8F-running--scheduling-automated-pipeline)
- [Tests](#-tests)
- [Safety guarantees](#%EF%B8%8F-safety-guarantees)
- [Project layout](#%EF%B8%8F-project-layout)
- [مستندات فارسی](#-مستندات-فارسی)

---

## 🎯 Overview

Point the tool at your IP ranges (CIDR notation) and it identifies every device by its vendor-specific API or CLI, then reconciles NetBox to match reality — creating, updating, and (after repeated misses) marking devices offline. A secondary ManageEngine sync enriches Asset Tags and imports offline/in-store equipment.

| | |
|---|---|
| 🔌 **Ten live device families** | Native protocols: Redfish, XML API, SSH CLI, REST, digest ISAPI/CGI/LAPI |
| 🧩 **Component-level inventory** | CPUs, RAM, disks, PSUs, NICs, HBAs, SFPs, FC ports, NVR hard drives |
| 🏢 **ManageEngine Integration** | Asset Tag sync on serial match; imports offline & warehouse inventory |
| 🕸️ **Topology-aware** | CDP/LLDP inter-switch cables, camera↔switch cables from MAC tables, broadcast-domain VLAN groups |
| 🛡️ **Careful by design** | Never overwrites automation data from ME, never deletes devices on flap, manages marker-owned objects only |

---

## 🔄 Architecture & Workflow

```mermaid
flowchart TD
    subgraph S1["1. Live Infrastructure Discovery (sync_all_to_netbox.py)"]
        A[IP Ranges Scan] --> B[Direct Device APIs / SSH]
        B --> C[Active Devices · Interfaces · VLANs · Cables · Components · NVR HDDs]
        C --> D[(NetBox DCIM & IPAM)]
    end

    subgraph S2["2. ManageEngine AssetExplorer Enrichment (sync_assetexplorer.py)"]
        E[AssetExplorer API] --> F{Match NetBox by Serial / Name / Suffix?}
        F -- "Found in NetBox" --> G["Enrich Asset Tag ONLY<br/>(Never overwrite live data)"]
        F -- "Not in NetBox" --> H["Create Offline / In-Store Device<br/>(Status: Inventory, with Department)"]
        F -- "Component / Disk / Module" --> I["Attach as Inventory Item to Parent<br/>or Warehouse-Stock Container"]
        G --> D
        H --> D
        I --> D
    end

    S1 -->|"On exit 0 (Sequential)"| S2
```

---

## 🔍 What gets scanned (Discovery)

Every family is **opt-in**: set its `*_RANGES` in `.env`; leave empty to disable it entirely.

| Family | Devices | Protocol | Key endpoints / commands |
|---|---|---|---|
| 🖥️ **Servers** | HPE ProLiant DL/ML, Gen8–Gen11 | Redfish (iLO 4/5) | `/redfish/v1/Systems`, `/Chassis`, `/Storage` + SmartStorage fallback (Gen9) |
| 💾 **Storage** | HPE MSA 2040/2050/2060 | XML API (HTTPS, legacy-TLS capable) | `show disks`, `show disk-parameters`, `show controllers`, `show power-supplies`, `show frus` |
| 🧵 **SAN switches** | Brocade / HPE B-Series | SSH CLI (Fabric OS) | `switchshow`, `version`, `nsshow`, `nscamshow`, `sfpshow` |
| 🌐 **LAN switches** | Cisco Catalyst (IOS / IOS-XE) | SSH via netmiko | `show version`, `show inventory`, `show interfaces status`, `show vlan brief`, `show interfaces trunk`, `show cdp/lldp neighbors detail`, `show mac address-table`, `show ip interface brief`, `show vtp status` |
| 🔥 **Firewalls** | FortiGate (FortiOS 6/7) | REST session + SSH extras | `POST /logincheck`, `/monitor/system/*`, `/cmdb/system/interface`, HA monitors, VIPs/IP pools; SSH: `diagnose lldp neighbor-summary`, `diagnose sys transceiver list` |
| 📡 **Wireless (Ruckus)** | ZoneDirector ZD1200-class | SSH interactive shell | `show sysinfo`, `show ap all`, `show ap <mac>` (AP serials), `show wlan all` |
| 📶 **Wireless (Ubiquiti)** | UniFi OS consoles (UDM/CloudKey/Server) | HTTPS session API | `POST /api/login`, `/api/self/sites`, per-site `stat/device`, `rest/wlanconf`, `rest/networkconf` |
| 🎥 **NVRs (Hikvision)** | DS-96xx/77xx NVRs | HTTP digest (ISAPI) | `/ISAPI/System/deviceInfo`, `ContentMgmt/InputProxy/channels(+status)`, `ContentMgmt/Storage/hdd` (NVR disks), per-channel proxied `deviceInfo` |
| 🎥 **NVRs (Dahua)** | NVR4X/NVR6xx-class | HTTP digest (CGI) | `magicBox.cgi` (system info), `configManager.cgi RemoteDevice` + `ChannelTitle` |
| 🎥 **NVRs (Uniview)** | NVR30x-class | HTTP digest (LAPI) | `/LAPI/V1.0/System/DeviceInfo`, `Channels/System/ChannelDetailInfos`, `DeviceInfos` |

---

## 🏢 ManageEngine AssetExplorer Sync

ManageEngine AssetExplorer (`sync_assetexplorer.py`) runs as a secondary data source:

1. **Source of Truth Protection**: The live automation is the absolute source of truth for online devices. ManageEngine **never** overwrites device names, serials, interfaces, IPAM, or live hardware attributes.
2. **Asset Tag Enrichment**: For existing devices in NetBox, matches by Serial Number (with Name and Camera Suffix fallback) and enriches **only the Asset Tag**.
3. **Offline & In-Store Stock**: Assets in ME that do not exist in NetBox are created with status `Inventory` (or `Decommissioning` for Expired) along with their **Department** and **Location**.
4. **Inventory Item Sync**: Components (`Power Module`, `HARD-Hardware`, `HARD-CCTV`, `NM Module`) are matched to their parent devices and synced as NetBox Inventory Items (or placed in a `Warehouse-Stock-<Site>` container device if unattached).

---

## 📦 What gets created in NetBox

### Devices

| Source | NetBox role | Identity | Notes |
|---|---|---|---|
| Server | `Server` | serial | BMC IP, model, BIOS, CPU/RAM/disk summaries, power state |
| Storage | `Storage` | serial | model, firmware, health, disk/capacity summaries |
| SAN switch | `SAN Switch` | serial / WWN | model, Fabric OS, port count |
| Cisco switch | `Switch` | serial | IOS version, port count |
| FortiGate | `Firewall` | serial (cluster = primary) | HA role/peers; HA pair merges into **one** device |
| Ruckus ZD | `Wireless Controller` | serial | HA pairs merge via `RUCKUS_HA_MAP` |
| UniFi console | `Wireless Controller` | console UUID | per-site AP/WLAN aggregation |
| APs (Ruckus + UniFi) | `Access Point` | **MAC** (`wap_mac`) / Serial | Serial collected via `show ap <mac>` (Ruckus) or derived from MAC (UniFi) |
| NVRs (3 vendors) | `NVR` | serial | `nvr_*` fields; installed physical HDDs synced as inventory items |
| **Cameras** | `Camera` | **serial** | `cam_*` fields, `cam_nvr` parent link, real MAC when available |
| ME In-Store Assets | Respective role | serial | Status `Inventory`, Department custom field, ME Asset ID |

### Custom fields

All 69 `*_ip` / `*_enabled` / `ae_*` / model / firmware / count fields are **auto-created at sync start** (`dcim.device`, `ui_visible=if-set`) and normalized at the end of every run.

---

## 🚀 Getting started

```bash
# 1. Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env     # fill in your values
```

---

## 🧾 Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NETBOX_URL` / `NETBOX_TOKEN` | ✅ | — | NetBox API endpoint and token |
| `NETBOX_VERIFY_TLS` | ❌ | `true` | Set `false` for self-signed NetBox certs |
| **Live Discovery** | | | |
| `REDFISH_USER` / `REDFISH_PASS` | ✅ | — | iLO credentials |
| `STORAGE_USER` / `STORAGE_PASS` | ✅ | — | MSA credentials |
| `SWITCH_USER` / `SWITCH_PASS` | ✅ | — | Brocade SSH |
| `CISCO_USER` / `CISCO_PASS` | when ranged | — | SSH credentials |
| `FORTIGATE_USER` / `FORTIGATE_PASS` | when ranged | — | FortiGate admin |
| `RUCKUS_USER` / `RUCKUS_PASS` | when ranged | — | Ruckus SSH |
| `UNIFI_USER` / `UNIFI_PASS` | when ranged | — | UniFi local admin |
| `HIKVISION_USER` / `HIKVISION_PASS` | when ranged | — | Hikvision ISAPI digest |
| `DAHUA_USER` / `DAHUA_PASS` | when ranged | — | Dahua CGI digest |
| `UNV_USER` / `UNV_PASS` | when ranged | — | Uniview LAPI digest |
| **ManageEngine AssetExplorer** | | | |
| `AE_URL` | when using ME | — | e.g. `https://172.31.5.155` |
| `AE_API_KEY` | when using ME | — | Technician API key |
| **Scan ranges (core)** | | | |
| `BMC_RANGES` / `STORAGE_RANGES` / `SAN_RANGES` | ✅ | — | CIDR networks |
| `CISCO_RANGES` / `FORTIGATE_RANGES` / `RUCKUS_RANGES` / `UNIFI_RANGES` | ❌ | *(off)* | CIDR networks |
| `HIKVISION_RANGES` / `DAHUA_RANGES` / `UNV_RANGES` | ❌ | *(off)* | CIDR networks |

---

## ⏱️ Running & scheduling (Automated Pipeline)

### Manual runs

```bash
# Run live discovery only:
python sync_all_to_netbox.py

# Run ManageEngine sync only:
python sync_assetexplorer.py

# Wipe all devices & cables cleanly (for fresh sync):
python wipe_all_devices.py
```

### Automated daily pipeline (`run_daily_sync.sh`)

The included `run_daily_sync.sh` script coordinates the complete pipeline:
1. Executes `sync_all_to_netbox.py` and logs to `logs/main-YYYY-MM-DD.log`.
2. On success (exit code 0), immediately executes `sync_assetexplorer.py` and logs to `logs/ae-YYYY-MM-DD.log`.
3. If discovery fails, ManageEngine sync is safely skipped.

Schedule it in cron:

```cron
# Run daily pipeline at midnight
0 0 * * * /home/sina/netbox-redfish/run_daily_sync.sh

# Rotate logs older than 30 days
0 1 * * * find /home/sina/netbox-redfish/logs -name "*.log" -mtime +30 -delete
```

---

## ✅ Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

288 tests, all offline — fixtures captured from real devices and APIs, plus NetBox reconciliation against in-memory fakes.

---

## 🛡️ Safety guarantees

- **Source of truth separation**: Live automation data is never overwritten by ManageEngine.
- **Never deletes devices**: Offline devices are marked offline / inventory, never deleted.
- **Never touches manual data**: Only objects managed by the sync markers are updated.
- **Strict idempotency**: Repeated runs perform zero unnecessary writes and produce zero duplicates.
- **Collision & stale serial protection**: Ambiguous matches or cross-named serials fall back to safe name matching with clear warnings.

---

## 🗂️ Project layout

```
netbox_sync/
├── main.py                  # Entry: config validation, lockfile, exit codes
├── config.py                # .env loading, ranges, validation, logging
├── scanner.py               # Thread-pooled probing of all device families
├── report.py                # Failure categorization and scan summary generator
├── sync.py                  # Orchestration: live reconciliation, sweeps, cabling, HDDs
├── assetexplorer_sync.py    # ManageEngine AssetExplorer sync (tags, offline stock, items)
├── netbox.py                # Device/inventory/custom-field helpers
├── ipam.py                  # Prefixes, host IPs, NAT + services
├── models.py                # Vendor model alias maps
├── utils.py                 # Range expansion, port checks, naming helpers
└── collectors/
    ├── redfish.py           # HPE servers
    ├── msa.py               # HPE MSA storage
    ├── brocade.py           # SAN switches
    ├── cisco.py             # Catalyst switches
    ├── fortigate.py         # Firewalls
    ├── ruckus.py            # Ruckus ZD & APs (serial collection)
    ├── unifi.py             # UniFi consoles & APs
    ├── hikvision.py         # Hikvision NVRs, HDDs & cameras
    ├── dahua.py             # Dahua NVRs & cameras
    ├── unv.py               # Uniview NVRs & cameras
    └── assetexplorer.py     # ManageEngine API collector & normalizer

sync_all_to_netbox.py        # CLI entry point for live discovery
sync_assetexplorer.py        # CLI entry point for ManageEngine sync
run_daily_sync.sh            # Production sequential daily wrapper
wipe_all_devices.py          # Fast inventory cleanup utility
```

---
---

<div dir="rtl">

# 📘 مستندات فارسی و راهنمای جامع پروژه

این پروژه یک راهکار خودکار، مطمئن و مقیاس‌پذیر برای کشف، پایش و نگهداری موجودی سخت‌افزاری و توپولوژی زیرساخت شبکه در سامانه **NetBox** است. این ابزار از ترکیب «اسکن زنده تجهیزات شبکه» و «همگام‌سازی با سامانه مدیریت اموال ManageEngine AssetExplorer» استفاده می‌کند تا همواره پایگاه داده‌ای دقیق، به‌روز و بدون ثبت‌های تکراری در اختیار مدیران زیرساخت قرار دهد.

---

## 🏗️ معماری کلان و تفکیک وظایف (Separation of Concerns)

سیستم بر پایه یک زنجیره اجرای دومرحله‌ای با مرجعیت داده مشخص طراحی شده است:

```mermaid
flowchart TD
    subgraph S1["مرحله اول: کشف و پایش زنده زیرساخت (sync_all_to_netbox.py)"]
        A[اسکن رنج‌های IP با ThreadPool] --> B[اتصال مستقیم به API / SSH / CLI تجهیزات]
        B --> C[استخراج مشخصات، اینترفیس‌ها، VLANها، قطعات، کابل‌ها و هاردها]
        C --> D[(پایگاه داده NetBox)]
    end

    subgraph S2["مرحله دوم: غنی‌سازی با ManageEngine AssetExplorer (sync_assetexplorer.py)"]
        E[دریافت اطلاعات اموال از API AssetExplorer] --> F{بررسی وجود سریال در NetBox}
        F -- "یافت شد (دستگاه فعال/کشف‌شده)" --> G["فقط به‌روزرسانی برچسب اموال (Asset Tag)<br/>(عدم تغییر داده‌های کشف‌شده زنده)"]
        F -- "یافت نشد (تجهیزات انبار/خاموش)" --> H["ثبت دستگاه جدید با وضعیت Inventory<br/>(همراه با فیلد دپارتمان و موقعیت)"]
        F -- "قطعات (پاور، هارد، ماژول)" --> I["اتصال به دستگاه مرجع یا مخزن انبار به عنوان Inventory Item"]
        G --> D
        H --> D
        I --> D
    end

    S1 -->|"در صورت اتمام موفقیت‌آمیز (Exit Code 0)"| S2
```

### ۱. مرحله اول: اسکن زنده و پایش زیرساخت (`sync_all_to_netbox.py`)
این بخش مرجع اصلی و قطعی (Source of Truth) برای کلیه تجهیزات آنلاین و در حال کار است و اطلاعات زیر را مستقیماً از سخت‌افزار استخراج می‌کند:
- **سرورهای HPE (نسل ۸ تا ۱۱)**: اتصال از طریق رابط Redfish iLO، استخراج مدل، سریال، پردازنده‌ها، رم‌ها، دیسک‌های فیزیکی، کنترلرهای RAID و کارت‌های شبکه.
- **استوریج‌های HPE MSA (سری‌های ۲۰۴۰، ۲۰۵۰ و ۲۰۶۰)**: اتصال از طریق XML API و پشتیبانی از نسخه‌های قدیمی TLS، استخراج سلامت کنترلرها، وضعیت دیسک‌ها، انکلوژرها و پاورها.
- **سوییچ‌های SAN Brocade**: اتصال SSH، استخراج مشخصات فابریک، وضعیت پورت‌های فیبر (FC)، سرعت لینک‌ها و WWNهای متصل.
- **سوییچ‌های Cisco Catalyst (IOS / IOS-XE)**: استخراج پورت‌ها، وضعیت ترانک و اکسس، جدول‌های MAC، همسایگی‌های CDP/LLDP برای کابل‌کشی خودکار، و ساخت گروه‌های VLAN بر اساس دامنه‌های انتشار (Broadcast Domains).
- **فایروال‌های FortiGate**: استخراج اینترفیس‌ها، VLANها، وضعیت کلاستر HA، نگاشت‌های NAT (شامل VIP و IP Pool) و سرویس‌های پورت فوروارد.
- **کنترلرها و اکسس‌پوینت‌های Ruckus**: استخراج مشخصات ZoneDirector، ثبت اکسس‌پوینت‌ها با شناسه MAC و استخراج سریال فیزیکی هر AP از طریق دستور اختصاصی `show ap <mac>`.
- **کنسول‌ها و اکسس‌پوینت‌های UniFi**: اتصال به API و استخراج مشخصات APها و تولید سریال نامبر استاندارد بر پایه MAC آدرس (بدون دونقطه) جهت انطباق با ManageEngine.
- **دستگاه‌های NVR و دوربین‌ها (Hikvision، Dahua، Uniview)**: کشف NVRها، ثبت دوربین‌های متصل به همراه MAC و کانال، کابل‌کشی خودکار دوربین به پورت سوییچ از روی جدول MAC، و کشف دیسک‌های فیزیکی نصب‌شده روی NVR به عنوان Inventory Item.

### ۲. مرحله دوم: غنی‌سازی اطلاعات از ManageEngine AssetExplorer (`sync_assetexplorer.py`)
این بخش به عنوان منبع تکمیلی اموال و تجهیزات انبار عمل می‌کند:
- **حفظ جامعیت اطلاعات کشف‌شده زنده**: اطلاعات کشف‌شده از طریق اتوماسیون زنده (نام دستگاه، سریال، مشخصات اینترفیس‌ها، IPAM و وضعیت سخت‌افزاری) به هیچ وجه توسط ManageEngine بازنویسی نمی‌شود.
- **غنی‌سازی برچسب اموال (Asset Tag)**: در صورتی که دستگاهی در نت‌باکس وجود داشته باشد، سیستم سریال آن را در ME جستجو کرده و تنها فیلد `Asset Tag` را به‌روزرسانی می‌کند.
- **ورود تجهیزات آفلاین و انبار**: دارایی‌هایی که در نت‌باکس وجود ندارند (مانند دستگاه‌های خاموش یا موجود در انبار) با وضعیت `Inventory` (یا `Decommissioning` برای موارد منقضی) به همراه فیلد اختصاصی دپارتمان (`ae_department`) در نت‌باکس ایجاد می‌شوند.
- **مدیریت قطعات (Inventory Items)**: اقلامی نظیر `Power Module`، `HARD-Hardware`، `HARD-CCTV` و `NM Module` در صورت انتساب به دستگاه والد، به عنوان Inventory Item به همان دستگاه متصل شده و در صورت عدم انتساب، در رکورد انبار (`Warehouse-Stock-<Site>`) ثبت می‌شوند.

---

## 🛡️ اصول و تضمین‌های ایمنی داده‌ها

1. **اصل عطف به گذشته و عدم تکرار (Idempotency)**:
   اجرای مکرر اتوماسیون هیچ‌گونه رکورد تکراری در دستگاه‌ها، قطعات یا اینترفیس‌ها ایجاد نمی‌کند. مقایسه تمام فیلدها پیش از اعمال تغییر انجام می‌شود و در صورت عدم تغییر، هیچ درخواستی به دیتابیس ارسال نمی‌شود.

2. **انطباق غیرحساس به حروف بزرگ و کوچک (Case-Insensitive Matching)**:
   کلیه مقایسه‌ها در خصوص سریال‌نامبرها، نام دستگاه‌ها، تولیدکنندگان (مانند `Ruckus` و `RUCKUS`)، مدل‌ها و سایت‌ها به صورت یکسان‌سازی‌شده ارزیابی می‌شوند تا از دوگانگی داده‌ها جلوگیری شود.

3. **حفاظت در برابر نوسانات شبکه (Anti-Flapping)**:
   قطع شدن موقت دسترسی به یک دستگاه موجب حذف آن نمی‌شود. وضعیت دستگاه تنها پس از تعدادی اسکن متوالی ناموفق (پیش‌فرض ۲ بار) به حالت آفلاین تغییر می‌یابد و به محض برقراری ارتباط مجدداً فعال می‌گردد.

4. **پشتیبانی از نام‌های چندزبانه**:
   تولید نام‌های یکتا (Slug) در سامانه‌های با کاراکترهای غیرلاتین (فارسی و عربی) به صورت خودکار از الگوریتم هش یکتا استفاده می‌کند تا از خطای `slug may not be blank` در نت‌باکس جلوگیری شود.

---

## ⚙️ راه‌اندازی و اتوماسیون شبانه (Cron Job)

یک خط لوله کامل از طریق اسکریپت `run_daily_sync.sh` توسعه داده شده است که وظیفه مدیریت اجرای ترتیبی و تولید فایل‌های لاگ مجزا را بر عهده دارد.

### ۱. تست و اجرای دستی

```bash
# رفتن به دایرکتوری پروژه و فعال‌سازی محیط مجازی
cd /home/sina/netbox-redfish
source .venv/bin/activate

# اجرای مرحله اسکن زنده به تنهایی:
python -u sync_all_to_netbox.py

# اجرای مرحله همگام‌سازی ManageEngine به تنهایی:
python -u sync_assetexplorer.py

# اجرای کل فرآیند ترتیبی مشابه زمان‌بندی خودکار:
./run_daily_sync.sh
```

### ۲. تنظیم زمان‌بندی روزانه (اجرا در ساعت ۰۰:۰۰ بامداد)

فایل کرون‌تب را باز کنید:
```bash
crontab -e
```

خطوط زیر را در انتهای آن درج و ذخیره نمایید:
```cron
# اجرای خودکار خط لوله نت‌باکس در ساعت ۰۰:۰۰ بامداد هر شب
0 0 * * * /home/sina/netbox-redfish/run_daily_sync.sh

# پاکسازی خودکار فایل‌های لاگ قدیمی‌تر از ۳۰ روز
0 1 * * * find /home/sina/netbox-redfish/logs -name "*.log" -mtime +30 -delete
```

### ۳. بررسی سلامت و مشاهده نتایج اجرا

پس از اجرای شبانه، خلاصه وضعیت هر دو مرحله در مسیر `logs/` در دسترس است:

```bash
# مشاهده گزارش نهایی اسکن زنده (تعداد دستگاه‌های شناسایی‌شده و دلایل خطای احتمالی):
grep -A 25 "SCAN SUMMARY" /home/sina/netbox-redfish/logs/main-$(date +%F).log

# مشاهده گزارش نهایی غنی‌سازی ManageEngine (تعداد تگ‌های ست‌شده و خطاهای تطبیق):
grep -A 20 "AE SYNC SUMMARY" /home/sina/netbox-redfish/logs/ae-$(date +%F).log
```

---

## 🧪 آزمون‌های خودکار و تضمین کیفیت (Unit Tests)

این پروژه دارای **۲۸۸ تست واحد کاملاً آفلاین** است که عملکرد کلیه پارسرها، سشن‌های اختصاصی، مدیریت کابل‌ها، استخراج سریال‌ها و انطباق بدون شبکه را در برابر مدل‌های شبیه‌سازی‌شده نت‌باکس اعتبارسنجی می‌کند:

```bash
python -m pytest tests/
```

</div>
