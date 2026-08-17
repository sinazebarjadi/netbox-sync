# NetBox Infrastructure Sync

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![NetBox](https://img.shields.io/badge/netbox-4.x-blueviolet)
![Tests](https://img.shields.io/badge/tests-224%20passing-brightgreen)

> One Python tool scans your entire infrastructure and keeps **NetBox** as the always-accurate source of truth: servers, storage, SAN & LAN switches, firewalls, wireless, NVRs and every camera — devices, interfaces, VLANs, IPAM, cables, and hardware inventory.
>
> 🇬🇧 **English below** · مستندات **فارسی** در انتها 🇮🇷

---

## 📚 Table of Contents

- [Overview](#-overview)
- [What gets scanned](#-what-gets-scanned)
- [What gets created in NetBox](#-what-gets-created-in-netbox)
- [How it works](#%EF%B8%8F-how-it-works)
- [Getting started](#-getting-started)
- [Configuration reference](#-configuration-reference)
- [Running & scheduling](#-running--scheduling)
- [Tests](#-tests)
- [Safety guarantees](#-safety-guarantees)
- [Project layout](#-project-layout)
- [مستندات فارسی](#-مستندات-فارسی)

---

## 🎯 Overview

The tool scans the IP ranges you configure (CIDR notation), identifies each device by its vendor-specific API/CLI, and reconciles NetBox to match reality — creating, updating, and (after repeated misses) marking devices offline. Run it from cron / systemd timer / Task Scheduler; each run is idempotent and safe to repeat.

**Highlights**

- 🔌 **Ten device families** — each with its own native protocol (Redfish, XML API, SSH CLI, REST, digest ISAPI/CGI/LAPI)
- 🧩 **Component-level inventory** — CPUs, RAM, disks, PSUs, NICs, HBAs, SFPs, FC ports… matched by serial
- 🕸️ **Topology-aware** — CDP/LLDP inter-switch cables, camera↔switch cables from MAC tables, VLAN groups derived from CDP broadcast domains
- 🛡️ **Careful by design** — never deletes devices, never touches manual records, marker-owned objects only, offline threshold against flapping

---

## 🔍 What gets scanned

| Family | Devices | Protocol | Key endpoints / commands |
|---|---|---|---|
| 🖥️ **Servers** | HPE ProLiant DL/ML, Gen8–Gen11 | Redfish (iLO 4/5) | `/redfish/v1/Systems`, `/Chassis`, `/Storage` + SmartStorage fallback (Gen9) |
| 💾 **Storage** | HPE MSA 2040/2050/2060 | XML API (HTTPS, legacy-TLS capable) | `show disks`, `show disk-parameters`, `show controllers`, `show power-supplies`, `show frus` |
| 🧵 **SAN switches** | Brocade / HPE B-Series | SSH CLI (Fabric OS) | `switchshow`, `version`, `nsshow`, `nscamshow`, `sfpshow` |
| 🌐 **LAN switches** | Cisco Catalyst (IOS / IOS-XE) | SSH via netmiko | `show version`, `show inventory`, `show interfaces status`, `show vlan brief`, `show interfaces trunk`, `show cdp/lldp neighbors detail`, `show mac address-table`, `show ip interface brief`, `show vtp status` |
| 🔥 **Firewalls** | FortiGate (FortiOS 6/7) | REST session + SSH extras | `POST /logincheck`, `/monitor/system/status`, `/monitor/system/interface`, `/cmdb/system/interface`, HA monitors, VIPs/IP pools; SSH: `diagnose lldp neighbor-summary`, `diagnose sys transceiver list` |
| 📡 **Wireless (Ruckus)** | ZoneDirector ZD1200-class | SSH interactive shell | `show sysinfo`, `show ap all`, `show wlan all` |
| 📶 **Wireless (Ubiquiti)** | UniFi OS consoles (UDM/CloudKey/Server) | HTTPS session API | `POST /api/login`, `/api/self/sites`, per-site `stat/device`, `rest/wlanconf`, `rest/networkconf` |
| 🎥 **NVRs (Hikvision)** | DS-96xx/77xx NVRs | HTTP digest (ISAPI) | `/ISAPI/System/deviceInfo`, `ContentMgmt/InputProxy/channels(+status)`, per-channel proxied `deviceInfo` |
| 🎥 **NVRs (Dahua)** | NVR6xx-class | HTTP digest (CGI) | `magicBox.cgi getSystemInfo/getDeviceClass/getSoftwareVersion/getMachineName`, `configManager.cgi RemoteDevice`, `ChannelTitle` |
| 🎥 **NVRs (Uniview)** | NVR30x-class | HTTP digest (LAPI) | `/LAPI/V1.0/System/DeviceInfo`, `Channels/System/ChannelDetailInfos`, `Channels/System/DeviceInfos` |

Every family is **opt-in**: set its `*_RANGES` in `.env`; leave empty to disable entirely (no scanning, no offline marking).

---

## 📦 What gets created in NetBox

### Devices

| Source | NetBox device | Identity | Notes |
|---|---|---|---|
| Server | `Server` role | serial | BMC IP, model, BIOS, CPU/RAM/disk summaries, power state |
| Storage | `Storage` role | serial | model, firmware, health, disk/capacity summaries |
| SAN switch | `SAN Switch` role | serial / WWN | model, Fabric OS, port count |
| Cisco switch | `Switch` role | serial | IOS version, port count |
| FortiGate | `Firewall` role | serial (cluster = primary) | HA role/peers; HA pair merges into **one** device |
| Ruckus ZD | `Wireless Controller` | serial | HA pairs merge via `RUCKUS_HA_MAP` |
| UniFi console | `Wireless Controller` | console UUID | per-site AP/WLAN aggregation |
| APs (Ruckus + UniFi) | `Access Point` | **MAC** (`wap_mac`) | group/controller links; name-clash disambiguation per site |
| NVRs (3 vendors) | `NVR` role | serial | `nvr_*` fields; offline sweeps are per-vendor (manufacturer-scoped) |
| **Cameras** | `Camera` role | **serial** | `cam_*` fields, `cam_nvr` parent link, real MAC when available |

### Interfaces, VLANs, IPAM, cables

- **Cisco**: every switchport as an interface (type mapped from speed/SFP, enabled state, description); SVIs as virtual interfaces; access/trunk VLAN linkage (untagged/tagged/native); VLANs grouped into **CDP-derived broadcast-domain groups** (`BD1`, `BD2`…); CDP/LLDP **cables** between resolved interfaces.
- **Brocade**: FC ports as interfaces with connected WWNs.
- **FortiGate**: physical/VLAN/aggregate interfaces (lag/parent links), interface IPs → IPAM prefixes + gateway addresses, **VLAN binding resolved against the switches' existing VLANs** (unique-match reuse, MAC-table disambiguation).
- **IPAM**: discovered prefixes nested under container parents from `SITE_IP_MAP`; management/primary IPs on the *real* carrier interface when identifiable.
- **NAT**: FortiGate VIPs → IPAM addresses with native `nat_inside` links; IP pools → SNAT ranges; port-forwarded VIPs → NetBox **Services** (protocol+port).
- **Camera → switch cables**: when Cisco is enabled, a camera with a known MAC is cabled to the switch port it's learned on (`netbox-sync: mac-table …`). Keep-on-absence: cables are never deleted when a MAC ages out of the tables — only moved on positive evidence.

### Custom fields

All 68 `*_ip` / `*_enabled` / model / firmware / count fields are **auto-created at sync start** (`dcim.device`, `ui_visible=if-set` — hidden until populated) and re-normalized at the end of every run. No manual NetBox setup required.

---

## ⚙️ How it works

```mermaid
flowchart LR
    subgraph cfg[".env (gitignored)"]
        A[Credentials + ranges + site maps]
    end
    subgraph run["run_sync()"]
        B[scan_all<br/>thread-pooled probes] --> C[per-family collectors<br/>API / SSH / digest]
        C --> D[ensure devices<br/>serial-first matching]
        D --> E[interfaces · VLANs · IPAM<br/>cables · inventory items]
        E --> F[offline sweeps<br/>threshold-guarded]
    end
    cfg --> run
    E --> G[(NetBox DCIM + IPAM)]
    F --> G
```

**Matching:** devices are matched by serial first, then name+site+role — no duplicates across runs. Cameras and APs use serial / MAC identity respectively.

**Offline detection:** a device is marked offline only after `OFFLINE_THRESHOLD` (default 2) consecutive missed scans; the next successful scan flips it back to active. NVR families sweep per-vendor; camera sweeps never fire on unverifiable data (channel presence + serial evidence required).

**Sites:** `SITE_IP_MAP` (longest-prefix CIDR→site) first, then `SITE_KEYWORD_MAP` on the name, else the default site.

---

## 🚀 Getting started

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in your values (see reference below)
python sync_all_to_netbox.py
```

Exit codes: `0` ok · `1` error · `130` Ctrl+C. A `netbox-sync.lock` file prevents overlapping runs (24 h stale-lock recovery).

---

## 🧾 Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NETBOX_URL` / `NETBOX_TOKEN` | ✅ | — | NetBox API endpoint and token |
| `NETBOX_VERIFY_TLS` | ❌ | `true` | Set `false` for self-signed NetBox certs |
| **Servers** | | | |
| `REDFISH_USER` / `REDFISH_PASS` | ✅ | — | iLO credentials |
| `REDFISH_PORT` | ❌ | `443` | |
| **Storage (MSA)** | | | |
| `STORAGE_USER` / `STORAGE_PASS` | ✅ | — | MSA credentials |
| `STORAGE_PORT` / `STORAGE_AUTH_HASH` | ❌ | `443` / `sha256` | hash = `md5` for very old firmware |
| **SAN switches** | | | |
| `SWITCH_USER` / `SWITCH_PASS` | ✅ | — | Brocade SSH |
| `SWITCH_PORT` / `SWITCH_STRICT_HOST_KEY` | ❌ | `22` / `false` | |
| **Cisco** | | | |
| `CISCO_USER` / `CISCO_PASS` | when ranged | — | SSH credentials |
| `CISCO_PORT` / `CISCO_RANGES` | ❌ | `22` / *(off)* | enables switches **and** camera cabling |
| `DEFAULT_CISCO_ROLE` | ❌ | `Switch` | |
| **FortiGate** | | | |
| `FORTIGATE_USER` / `FORTIGATE_PASS` | when ranged | — | admin (session auth via `/logincheck`) |
| `FORTIGATE_PORT` / `FORTIGATE_SSH_PORT` | ❌ | `443` / `22` | |
| **Ruckus** | | | |
| `RUCKUS_USER` / `RUCKUS_PASS` / `RUCKUS_PORT` | when ranged | — / — / `22` | SSH credentials |
| `RUCKUS_RANGES` / `RUCKUS_HA_MAP` | ❌ | *(off)* / — | HA pairs: `vip:primary,secondary;…` |
| `DEFAULT_RUCKUS_ROLE` / `DEFAULT_AP_ROLE` | ❌ | `Wireless Controller` / `Access Point` | |
| **UniFi** | | | |
| `UNIFI_USER` / `UNIFI_PASS` / `UNIFI_PORT` | when ranged | — / — / `8443` | dedicated **local** admin |
| `UNIFI_RANGES` / `DEFAULT_UNIFI_ROLE` | ❌ | *(off)* / `Wireless Controller` | |
| **NVRs** | | | |
| `HIKVISION_USER/PASS/PORT` | when ranged | — / — / `80` | ISAPI digest |
| `DAHUA_USER/PASS/PORT` | when ranged | — / — / `80` | CGI digest |
| `UNV_USER/PASS/PORT` | when ranged | — / — / `80` | LAPI digest |
| `*_RANGES` (each) | ❌ | *(off)* | per-family CIDR lists |
| `DEFAULT_*_ROLE` | ❌ | `NVR` | per NVR vendor |
| `DEFAULT_HIKVISION_CAMERA_ROLE` | ❌ | `Camera` | shared by all NVR vendors |
| **Scan ranges (core)** | | | |
| `BMC_RANGES` / `STORAGE_RANGES` / `SAN_RANGES` | ✅ | TEST-NET placeholders | replace with your networks |
| **Sites** | | | |
| `SITE_IP_MAP` | ❌ | — | `cidr:Site,…` — longest prefix wins, checked first |
| `SITE_KEYWORD_MAP` | ❌ | — | `keyword:Site,…` matched against device names |
| **Behavior** | | | |
| `SCAN_WORKERS` | ❌ | `20` | probe thread pool |
| `OFFLINE_THRESHOLD` | ❌ | `2` | consecutive misses before offline |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG`/`INFO`/`WARN`/`ERROR` |
| `DEFAULT_SITE_NAME` / `DEFAULT_ROLE_NAME` / … | ❌ | — | fallback site/roles |

---

## ⏱️ Running & scheduling

One run = one full reconciliation. Schedule it:

```cron
# twice daily, appended log
0 0,12 * * * /opt/netbox-sync/.venv/bin/python /opt/netbox-sync/sync_all_to_netbox.py >> /var/log/netbox-sync.log 2>&1
```

Systemd timers and Windows Task Scheduler work too. Ctrl+C aborts responsively (in-flight probes finish, pending ones are cancelled).

---

## ✅ Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

224 tests, all offline — parser fixtures captured from real devices, plus NetBox reconciliation against in-memory fakes (no hardware needed).

---

## 🛡️ Safety guarantees

- **Never deletes devices** — offline ≠ deleted; NVR/camera sweeps only mark offline.
- **Never touches manual data** — only objects marked `netbox-sync:` (cables, VLANs, prefixes, NAT records) are managed.
- **Serial/MAC identity** — renames, re-IPs and site moves are adopted, not duplicated.
- **Name-collision safe** — cameras colliding with an existing same-site device get a deterministic `-cam<channel>` suffix; NVRs with generic factory names get IP-qualified names.
- **No credential leaks** — secrets only in gitignored `.env`; `.env.example` is the template.

---

## 🗂️ Project layout

```
netbox_sync/
├── main.py                  # entry: config validation, lockfile, exit codes
├── config.py                # .env loading, ranges, validation, logging
├── scanner.py               # thread-pooled probing of all families
├── sync.py                  # orchestration: reconcile NetBox, sweeps, cabling
├── netbox.py                # device/inventory/custom-field helpers
├── ipam.py                  # prefixes, host IPs, NAT + services
├── models.py                # vendor model alias maps
├── utils.py                 # range expansion, port checks, misc
└── collectors/
    ├── redfish.py           # HPE servers
    ├── msa.py               # HPE MSA storage (legacy-TLS aware)
    ├── brocade.py           # SAN switches
    ├── cisco.py             # Catalyst: interfaces/VLANs/CDP + MAC tables
    ├── fortigate.py         # firewalls: session auth, VLAN/NAT, LLDP
    ├── ruckus.py            # ZoneDirector + APs + WLANs
    ├── unifi.py             # UniFi OS consoles + APs + WLANs
    ├── hikvision.py         # Hikvision NVRs + cameras (ISAPI)
    ├── dahua.py             # Dahua NVRs + cameras (CGI)
    └── unv.py               # Uniview NVRs + cameras (LAPI)
```

---
---

# 📘 مستندات فارسی

## این ابزار چه کار می‌کند

یک ابزار Python که کل زیرساخت شما را اسکن می‌کند و **NetBox** را همیشه به‌روز نگه می‌دارد: سرورها، استوریج‌ها، سوئیچ‌های SAN و LAN، فایروال‌ها، بی‌سیم، NVRها و تمام دوربین‌ها — دستگاه‌ها، اینترفیس‌ها، VLANها، IPAM، کابل‌ها و موجودی سخت‌افزاری.

**نکات برجسته**

- 🔌 **ده خانواده دستگاه** — هرکدام با پروتکل بومی خودشان (Redfish، XML API، SSH CLI، REST، digest ISAPI/CGI/LAPI)
- 🧩 **موجودی سطح قطعه** — CPU، RAM، دیسک، PSU، NIC، HBA، SFP، پورت‌های FC… با تطبیق بر اساس سریال
- 🕸️ **آگاه از توپولوژی** — کابل‌های بین سوئیچی CDP/LLDP، کابل‌های دوربین↔سوئیچ از جدول MAC، گروه‌های VLAN مشتق از دامنه‌های broadcast
- 🛡️ **احتیاطی طراحی شده** — هرگز دستگاهی را حذف نمی‌کند، رکوردهای دستی را دست نمی‌زند، فقط اشیاء marker-owned را مدیریت می‌کند

## چه چیزهایی اسکن می‌شود

| خانواده | دستگاه‌ها | پروتکل | نقاط پایانی / دستورات کلیدی |
|---|---|---|---|
| 🖥️ **سرورها** | HPE ProLiant DL/ML، Gen8 تا Gen11 | Redfish (iLO 4/5) | `/redfish/v1/Systems`، `/Chassis`، `/Storage` + جایگزین SmartStorage |
| 💾 **استوریج** | HPE MSA 2040/2050/2060 | XML API | `show disks`، `show controllers`، `show power-supplies`، `show frus` |
| 🧵 **سوئیچ‌های SAN** | Brocade / HPE B-Series | SSH CLI | `switchshow`، `version`، `nsshow`، `nscamshow`، `sfpshow` |
| 🌐 **سوئیچ‌های LAN** | Cisco Catalyst (IOS / IOS-XE) | SSH (netmiko) | `show version`، `show interfaces status`، `show vlan brief`، `show interfaces trunk`، `show cdp/lldp neighbors`، `show mac address-table`، `show ip interface brief` |
| 🔥 **فایروال‌ها** | FortiGate (FortiOS 6/7) | REST + SSH | `POST /logincheck`، `monitor/system/*`، `cmdb/system/interface`، HA، VIP/IP pool |
| 📡 **بی‌سیم (Ruckus)** | ZoneDirector | SSH | `show sysinfo`، `show ap all`، `show wlan all` |
| 📶 **بی‌سیم (Ubiquiti)** | کنسول‌های UniFi OS | HTTPS session | `api/login`، سایت‌ها، APها، WLANها، networkconf |
| 🎥 **NVR (Hikvision)** | سری DS-96xx/77xx | digest (ISAPI) | `System/deviceInfo`، `InputProxy/channels(+status)`، `deviceInfo` هر کانال |
| 🎥 **NVR (Dahua)** | سری NVR6xx | digest (CGI) | `magicBox.cgi`، `RemoteDevice`، `ChannelTitle` |
| 🎥 **NVR (Uniview)** | سری NVR30x | digest (LAPI) | `System/DeviceInfo`، `ChannelDetailInfos`، `DeviceInfos` |

هر خانواده **اختیاری** است: `*_RANGES` مربوطه را در `.env` تنظیم کنید؛ خالی = کاملاً غیرفعال.

## چه چیزهایی در NetBox ساخته می‌شود

- **دستگاه‌ها** برای همه خانواده‌ها (تطبیق با سریال، سپس نام+سایت+نقش) — دوربین‌ها دستگاه مستقل با نقش `Camera` (هویت = سریال، لینک والد در `cam_nvr`) و APها با هویت MAC
- **اینترفیس‌ها و VLANها** — پورت‌های سوئیچ (نوع/وضعیت)، SVIها، لینک access/trunk، گروه‌های VLAN مشتق از توپولوژی CDP (`BD1`، `BD2`…)
- **کابل‌ها** — بین سوئیچ‌ها از CDP/LLDP؛ دوربین↔سوئیچ از جدول MAC سوئیچ (فقط کابل‌های marker-owned مدیریت می‌شوند؛ کابل دوربین هرگز به‌خاطر aging جدول MAC حذف نمی‌شود)
- **IPAM** — پرفیکس‌های کشف‌شده زیر پرفیکس‌های container از `SITE_IP_MAP`؛ IPهای مدیریت روی اینترفیس واقعی؛ NAT فورتی‌گیت (VIPها با `nat_inside`، IP poolها، Serviceها برای port-forwardها)
- **فیلدهای سفارشی** — هر ۶۸ فیلد در ابتدای سینک **به‌طور خودکار ساخته می‌شوند** (`ui_visible=if-set`) و در پایان هر اجرا نرمال می‌شوند

## تشخیص آفلاین

دستگاه فقط پس از `OFFLINE_THRESHOLD` (پیش‌فرض: ۲) اسکن متوالی ناموفق آفلاین علامت می‌خورد؛ اسکن موفق بعدی آن را فعال برمی‌گرداند. جاروی NVRها به‌ازای هر وندور جداست؛ جاروی دوربین‌ها بدون شواهد (حضور کانال + سریال) هرگز اجرا نمی‌شود.

## نصب و اجرا

```bash
pip install -r requirements.txt
cp .env.example .env     # مقادیر واقعی را پر کنید
python sync_all_to_netbox.py
```

کدهای خروجی: `0` موفق · `1` خطا · `130` Ctrl+C. فایل قفل `netbox-sync.lock` از اجرای هم‌زمان جلوگیری می‌کند. زمان‌بندی با cron / systemd timer / Task Scheduler.

## تست‌ها

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

۲۲۴ تست، همه آفلاین — فیکسچرهای گرفته‌شده از دستگاه‌های واقعی + شبیه‌سازی درون‌حافظه‌ای NetBox.

## تضمین‌های ایمنی

- **هرگز دستگاه حذف نمی‌شود** — آفلاین ≠ حذف
- **داده‌های دستی دست‌نخورده می‌مانند** — فقط اشیاء با marker `netbox-sync:` مدیریت می‌شوند
- **هویت سریال/MAC** — تغییر نام/آی‌پی/سایت adopt می‌شود، duplicate نمی‌شود
- **تصادم نام امن است** — دوربین‌ها پسوند قطعی `-cam<کانال>` می‌گیرند؛ NVRهای با نام کارخانه‌ای عمومی نام مبتنی بر IP می‌گیرند
- **بدون نشت رمز** — اسرار فقط در `.env` (gitignored)
