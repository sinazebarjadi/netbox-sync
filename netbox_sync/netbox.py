"""NetBox API layer: connection, get-or-create helpers, device ensure /
mark-offline, and serial-keyed inventory reconciliation.

Inventory item roles are resolved by NAME via get_or_create_inventory_role().
Role IDs are DB-sequence-dependent and NOT portable between NetBox
instances, so nothing here may hardcode them.
"""
import re

import pynetbox

from netbox_sync.config import (NETBOX_URL, NETBOX_TOKEN, _env_bool,
                                SERVER_ROLE, STORAGE_ROLE, SWITCH_ROLE,
                                CISCO_ROLE, FORTIGATE_ROLE,
                                DEFAULT_MFR, OFFLINE_THRESHOLD, log)
from netbox_sync.models import (SERVER_MODEL_MAP, STORAGE_MODEL_MAP,
                                SWITCH_MODEL_MAP, CISCO_MODEL_MAP,
                                FORTIGATE_MODEL_MAP)
from netbox_sync.utils import (slugify, normalize_model, resolve_site,
                               _invalid_serial, _mgmt_prefixlen)

nb = None

def get_netbox():
    global nb
    if nb is not None:
        return nb
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    # TLS verification for NetBox is opt-in (NETBOX_VERIFY_TLS=true) since
    # many internal NetBox installs use self-signed certs.
    nb.http_session.verify = _env_bool("NETBOX_VERIFY_TLS", False)
    return nb

# ── CRUD helpers ─────────────────────────────────────────────────────────────
# Resolution caches: a sync run is short-lived and NetBox names/slugs are
# unique, so caching id lookups for the process lifetime is safe and avoids
# re-querying the same manufacturer/role/site/type once per device or item.
_MANUFACTURER_CACHE = {}
_ROLE_CACHE = {}
_SITE_CACHE = {}
_DEVICE_TYPE_CACHE = {}

def _get_or_create(endpoint, lookup, create):
    obj = endpoint.get(**lookup)
    if obj: return obj.id
    return endpoint.create(create).id

def get_or_create_manufacturer(name):
    if not name: return None
    name = name.strip()
    key = name.lower()
    if key in _MANUFACTURER_CACHE:
        return _MANUFACTURER_CACHE[key]
    mfr_id = _resolve_manufacturer(name)
    if mfr_id is not None:
        _MANUFACTURER_CACHE[key] = mfr_id
    return mfr_id

def _resolve_manufacturer(name):
    api = get_netbox()
    # Primary lookup by name (case-insensitive in NetBox)
    m = api.dcim.manufacturers.get(name=name)
    if m: return m.id
    # Secondary lookup by slug (handles pre-existing manufacturers with
    # different casing or a manually-set slug)
    slug = slugify(name)
    try:
        m = api.dcim.manufacturers.get(slug=slug)
        if m: return m.id
    except Exception: pass
    # Try to create; if slug collides, fall back to a suffixed slug
    for attempt in range(3):
        try:
            return api.dcim.manufacturers.create(
                {"name": name, "slug": slug if attempt == 0 else f"{slug}-{attempt+1}"}).id
        except Exception as e:
            if "already exists" in str(e) and attempt < 2:
                continue
            # Last resort: re-query by name (race conditions, etc.)
            m = api.dcim.manufacturers.get(name=name)
            if m: return m.id
            raise

def get_or_create_device_type(model, mfr_id, model_map=None):
    m = normalize_model(model, model_map) or model or "Unknown"
    key = (m.lower(), mfr_id)
    if key in _DEVICE_TYPE_CACHE:
        return _DEVICE_TYPE_CACHE[key]
    dt_id = _get_or_create(get_netbox().dcim.device_types, {"model": m},
                           {"model": m, "slug": slugify(m), "manufacturer": mfr_id})
    _DEVICE_TYPE_CACHE[key] = dt_id
    return dt_id

def get_or_create_role(name, color="9e9e9e"):
    key = name.lower()
    if key in _ROLE_CACHE:
        return _ROLE_CACHE[key]
    api = get_netbox()
    r = api.dcim.device_roles.get(name=name)
    if not r:
        r = api.dcim.device_roles.get(slug=slugify(name))
    if not r:
        r = api.dcim.device_roles.create(
            {"name": name, "slug": slugify(name), "color": color})
    _ROLE_CACHE[key] = r.id
    return r.id

_INVENTORY_ROLE_CACHE = {}
def get_or_create_inventory_role(name, color="9e9e9e"):
    """Inventory-item-role IDs are NOT portable between NetBox instances —
    unlike device roles, they can't safely be hardcoded, so resolve by name
    (creating the role if it doesn't exist yet) and cache the result."""
    if name in _INVENTORY_ROLE_CACHE:
        return _INVENTORY_ROLE_CACHE[name]
    api = get_netbox()
    r = api.dcim.inventory_item_roles.get(name=name)
    if not r:
        r = api.dcim.inventory_item_roles.get(slug=slugify(name))
    if not r:
        r = api.dcim.inventory_item_roles.create(
            {"name": name, "slug": slugify(name), "color": color})
    _INVENTORY_ROLE_CACHE[name] = r.id
    return r.id

def get_or_create_site(name):
    key = name.lower()
    if key in _SITE_CACHE:
        return _SITE_CACHE[key]
    site_id = _get_or_create(get_netbox().dcim.sites, {"name": name},
                             {"name": name, "slug": slugify(name), "status": "active"})
    _SITE_CACHE[key] = site_id
    return site_id

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

def _sanitize_dns_name(hostname):
    h = re.sub(r'[^a-z0-9.-]', '', (hostname or "").lower())[:63]
    return h or None

MGMT_IFACE_NAME = "mgmt"

def _get_or_create_mgmt_iface(api, dev_id):
    iface = api.dcim.interfaces.get(device_id=dev_id, name=MGMT_IFACE_NAME)
    if iface:
        return iface
    return api.dcim.interfaces.create({
        "device": dev_id, "name": MGMT_IFACE_NAME, "type": "virtual",
        "enabled": True, "mgmt_only": True,
        "description": "netbox-sync: management interface",
    })

def ensure_primary_ip(dev_id, ip, hostname=None, iface_name=None):
    """Create/update the management IP in IPAM, assign it to the device's
    carrier interface, and set it as primary IPv4. NetBox REJECTS primary_ip4
    unless the IP is assigned to an interface on the same device. The carrier
    is iface_name when given (real SVI/subinterface), else the synthetic
    mgmt interface. Existing IPAM records are reused unchanged (any mask);
    an IP already assigned to ANOTHER device is left alone."""
    api = get_netbox()
    existing = list(api.ipam.ip_addresses.filter(address=str(ip)))
    if existing:
        ip_rec = existing[0]
    else:
        payload = {
            "address": f"{ip}/{_mgmt_prefixlen(ip)}",
            "status": "active",
            "description": "netbox-sync: mgmt",
        }
        dns = _sanitize_dns_name(hostname)
        if dns:
            payload["dns_name"] = dns
        ip_rec = api.ipam.ip_addresses.create(payload)
    ip_id = ip_rec.id

    assigned_iface = getattr(ip_rec, "assigned_object_id", None)
    if getattr(ip_rec, "assigned_object_type", None) == "dcim.interface" \
            and assigned_iface:
        iface = api.dcim.interfaces.get(id=assigned_iface)
        iface_dev = None
        if iface is not None:
            d = getattr(iface, "device", None)
            iface_dev = getattr(d, "id", None) if d is not None \
                        else getattr(iface, "device_id", None)
        if iface_dev != dev_id:
            log("WARN", f"  primary IPv4 {ip} is assigned to another device — "
                        f"leaving device id={dev_id} unchanged")
            return ip_id
    else:
        iface = None
        if iface_name:
            iface = api.dcim.interfaces.get(device_id=dev_id, name=iface_name)
            if iface is None:
                log("WARN", f"  carrier interface {iface_name} not found on "
                            f"device id={dev_id} — using synthetic mgmt")
        if iface is None:
            iface = _get_or_create_mgmt_iface(api, dev_id)
        api.ipam.ip_addresses.update([{
            "id": ip_id,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface.id,
        }])

    dev = api.dcim.devices.get(id=dev_id)
    current = getattr(getattr(dev, "primary_ip4", None), "id", None) if dev else None
    if current != ip_id:
        api.dcim.devices.update([{"id": dev_id, "primary_ip4": ip_id}])
    return ip_id

# ── device ensure / mark offline ─────────────────────────────────────────────
def _device_name(probe, prefix="server"):
    hn = probe.get("hostname") or f"{prefix}-{probe['ip'].replace('.', '-')}"
    return hn.strip()[:64]

def ensure_server_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "HPE")
    role_id = get_or_create_role(SERVER_ROLE)
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
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
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
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

def ensure_san_switch_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Brocade")
    role_id = get_or_create_role(SWITCH_ROLE, "f44336")
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, SWITCH_MODEL_MAP)
    name = _device_name(probe, prefix="san")
    api = get_netbox()
    dev = find_device(serial, role_name=SWITCH_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found san switch by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "san_switch_ip":      probe["ip"],
            "san_switch_enabled": True,
            "san_switch_wwn":     probe.get("wwn"),
            "san_switch_firmware": probe.get("firmware"),
            "san_switch_model":   probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  SAN switch updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  SAN switch created: {name} (id={new.id})")
    return new.id

def ensure_cisco_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Cisco")
    role_id = get_or_create_role(CISCO_ROLE, "009688")
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, CISCO_MODEL_MAP)
    name = _device_name(probe, prefix="cisco")
    api = get_netbox()
    dev = find_device(serial, role_name=CISCO_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found cisco switch by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "cisco_ip":       probe["ip"],
            "cisco_enabled":  True,
            "cisco_firmware": probe.get("firmware"),
            "cisco_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  Cisco switch updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  Cisco switch created: {name} (id={new.id})")
    return new.id

def ensure_fortigate_device(probe, ha=None):
    """Ensure the NetBox device for a FortiGate. When the unit belongs to an
    HA cluster, the device represents the CLUSTER: named and serialized after
    the primary unit, resolvable by ANY unit serial, peers recorded in custom
    fields instead of as separate devices."""
    ha = ha or {}
    serial = (probe.get("serial") or "").strip()
    clustered = bool(ha.get("clustered") and ha.get("primary_hostname"))
    if clustered:
        eff_serial = (ha.get("primary_serial") or serial).strip()
        name = ha["primary_hostname"][:64]
        find_serials = [eff_serial] + [u.get("serial") for u in ha.get("units", [])
                                       if u.get("serial") and u.get("serial") != eff_serial]
    else:
        eff_serial = serial
        name = _device_name(probe, prefix="fortigate")
        find_serials = [eff_serial] if eff_serial else []
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Fortinet")
    role_id = get_or_create_role(FORTIGATE_ROLE, "c62828")
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, FORTIGATE_MODEL_MAP)
    api = get_netbox()
    dev = None
    for s in find_serials:
        if _invalid_serial(s):
            continue
        dev = find_device(s, role_name=FORTIGATE_ROLE)
        if dev:
            break
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found fortigate by name+site: {name} (id={dev.id})")
    cf = {
        "fortigate_ip":       probe["ip"],
        "fortigate_enabled":  True,
        "fortigate_firmware": probe.get("firmware"),
        "fortigate_model":    probe.get("model"),
    }
    if clustered:
        cf.update({
            "fortigate_ha_group": ha.get("group_name"),
            "fortigate_ha_mode":  ha.get("mode"),
            "fortigate_ha_peer":  "; ".join(
                f"{u['hostname']} ({u['serial']})"
                for u in ha.get("units", []) if not u.get("is_primary")),
            "fortigate_ha_role":  "primary" if serial == eff_serial else "secondary",
        })
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": cf,
        **({"serial": eff_serial} if not _invalid_serial(eff_serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  FortiGate updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  FortiGate created: {name} (id={new.id})")
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

def mark_san_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"san_switch_enabled": False},
        }])
        log("WARN", f"  SAN switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark SAN switch offline {dev_name}: {e}")

def mark_cisco_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"cisco_enabled": False},
        }])
        log("WARN", f"  Cisco switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark Cisco switch offline {dev_name}: {e}")

def mark_fortigate_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"fortigate_enabled": False},
        }])
        log("WARN", f"  FortiGate marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark FortiGate offline {dev_name}: {e}")


def ensure_ap_device(ap, wlc_name, role_name=None, manufacturer="Ruckus",
                     site_name=None):
    """Ensure a NetBox device for an access point (Ruckus or UniFi). APs have
    no reliable serial — identity is the MAC (wap_mac custom field).
    site_name overrides keyword-based site resolution (no current caller passes
    it — AP sites resolve via SITE_IP_MAP / SITE_KEYWORD_MAP)."""
    from netbox_sync.config import AP_ROLE
    mac = ap["mac"]
    role = role_name or AP_ROLE
    mfr_id = get_or_create_manufacturer(manufacturer)
    role_id = get_or_create_role(role, "00acc1")
    site_name = site_name or resolve_site(ap.get("name") or "",
                                          ap.get("ip") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(ap.get("model") or f"{manufacturer} AP",
                                         mfr_id)
    name = (ap.get("name") or mac)[:64]
    api = get_netbox()
    dev = next(iter(api.dcim.devices.filter(cf_wap_mac=mac)), None)
    if dev is None:
        # adopt by name+site+role only when the candidate has no wap_mac of
        # its own — a device with a DIFFERENT wap_mac is a different AP
        cands = [d for d in api.dcim.devices.filter(name=name, site_id=site_id,
                                                    role_id=role_id)
                 if not (d.custom_fields or {}).get("wap_mac")]
        dev = cands[0] if cands else None
        if dev:
            log("INFO", f"  Found AP by name+site: {name} (id={dev.id})")
    # NetBox enforces device-name uniqueness per site. AP names are not
    # unique across controller sites (two "F1"s whose sites resolve to one
    # NetBox site) — disambiguate with a stable MAC-based suffix when the
    # plain name is held by any other device. Recomputed every run, so the
    # name reverts to plain once the clash disappears.
    clash = any(d.id != (dev.id if dev else None)
                and (d.custom_fields or {}).get("wap_mac") != mac
                for d in api.dcim.devices.filter(name=name, site_id=site_id))
    if clash:
        name = f"{name} ({mac.replace(':', '')[-4:]})"
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "wap_mac":    mac,
            "wap_enabled": True,
            "wap_group":  ap.get("group"),
            "wap_wlc":    wlc_name,
        },
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  AP created: {name} (id={new.id})")
    return new.id


def mark_ap_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"wap_enabled": False},
        }])
        log("WARN", f"  AP marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark AP offline {dev_name}: {e}")


def mark_ruckus_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"wlc_enabled": False},
        }])
        log("WARN", f"  ZD marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark ZD offline {dev_name}: {e}")


def ensure_unifi_console(probe, ap_count=0, site_count=0):
    """Ensure the NetBox device for a UniFi OS console. Identity: the console
    uuid (serial field), then unifi_ip, then name+site+role."""
    from netbox_sync.config import UNIFI_ROLE
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer("Ubiquiti")
    role_id = get_or_create_role(UNIFI_ROLE, "8e44ad")
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model")
                                         or "UniFi OS Console", mfr_id)
    name = (probe.get("hostname")
            or f"unifi-{probe['ip'].replace('.', '-')}")[:64]
    api = get_netbox()
    dev = None
    if not _invalid_serial(serial):
        dev = find_device(serial, role_name=UNIFI_ROLE)
    if dev is None:
        dev = next(iter(api.dcim.devices.filter(cf_unifi_ip=probe["ip"])), None)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id,
                                             role_id=role_id))
        dev = cands[0] if cands else None
        if dev:
            log("INFO", f"  Found UniFi console by name+site: {name} (id={dev.id})")
    cf = {"unifi_ip": probe["ip"], "unifi_enabled": True,
          "unifi_version": probe.get("firmware"),
          "unifi_ap_count": ap_count, "unifi_sites": site_count}
    payload = {"name": name, "status": "active", "site": site_id,
               "device_type": dtype_id, "role": role_id,
               "custom_fields": cf,
               **({"serial": serial} if not _invalid_serial(serial) else {})}
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  UniFi console updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  UniFi console created: {name} (id={new.id})")
    return new.id


def mark_unifi_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"unifi_enabled": False},
        }])
        log("WARN", f"  UniFi console marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark UniFi console offline {dev_name}: {e}")


def _ensure_nvr_device(probe, manufacturer, role_name):
    """Ensure the NetBox device for an NVR of any vendor. Match by serial
    first, then name+site+role. The nvr_* custom fields are vendor-neutral and
    shared by the Hikvision/Dahua/Uniview families (the offline sweeps
    disambiguate per vendor by manufacturer)."""
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(manufacturer)
    role_id = get_or_create_role(role_name, "7b1fa2")
    site_name = resolve_site(probe.get("hostname") or "",
                             probe.get("reported_ip") or probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model") or "NVR", mfr_id)
    name = (probe.get("hostname") or f"nvr-{probe['ip'].replace('.', '-')}")[:64]
    api = get_netbox()
    dev = None
    if not _invalid_serial(serial):
        dev = find_device(serial, role_name=role_name)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id,
                                             role_id=role_id))
        dev = cands[0] if cands else None
        if dev:
            log("INFO", f"  Found NVR by name+site: {name} (id={dev.id})")
    cf = {"nvr_ip": probe["ip"], "nvr_enabled": True,
          "nvr_model": probe.get("model"),
          "nvr_firmware": probe.get("firmware")}
    payload = {"name": name, "status": "active", "site": site_id,
               "device_type": dtype_id, "role": role_id,
               "custom_fields": cf,
               **({"serial": serial} if not _invalid_serial(serial) else {})}
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  NVR updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  NVR created: {name} (id={new.id})")
    return new.id


def ensure_hikvision_device(probe):
    from netbox_sync.config import HIKVISION_ROLE
    return _ensure_nvr_device(probe, "Hikvision", HIKVISION_ROLE)


def ensure_dahua_device(probe):
    from netbox_sync.config import DAHUA_ROLE
    return _ensure_nvr_device(probe, "Dahua", DAHUA_ROLE)


def ensure_unv_device(probe):
    from netbox_sync.config import UNV_ROLE
    return _ensure_nvr_device(probe, "Uniview", UNV_ROLE)


def _mark_nvr_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"nvr_enabled": False},
        }])
        log("WARN", f"  NVR marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark NVR offline {dev_name}: {e}")


def mark_hikvision_offline(dev_id, dev_name):
    _mark_nvr_offline(dev_id, dev_name)


def mark_dahua_offline(dev_id, dev_name):
    _mark_nvr_offline(dev_id, dev_name)


def mark_unv_offline(dev_id, dev_name):
    _mark_nvr_offline(dev_id, dev_name)


def ensure_camera_device(cam, nvr_name, role_name=None, manufacturer="Hikvision"):
    """Ensure a NetBox device for a camera behind any NVR vendor (Hikvision,
    Dahua, Uniview). Cameras are real devices (not inventory items); identity
    is the camera serial. The parent NVR is recorded in the cam_nvr custom
    field. cam_mac is set only when the collector supplies a real MAC —
    Dahua's ONVIF-registered cameras usually have none."""
    from netbox_sync.config import HIKVISION_CAMERA_ROLE
    serial = (cam.get("serial") or "").strip()
    role = role_name or HIKVISION_CAMERA_ROLE
    mfr_id = get_or_create_manufacturer(manufacturer or "Hikvision")
    role_id = get_or_create_role(role, "4a90d9")
    site_name = resolve_site(cam.get("name") or "", cam.get("ip") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(cam.get("model") or "Camera", mfr_id)
    name = (cam.get("name") or f"camera-ch{cam.get('channel')}")[:64]
    # NetBox enforces device-name uniqueness per site: a camera title can
    # collide with an unrelated device (e.g. a UniFi AP named "GF"). Try the
    # plain title first; on collision use a deterministic '<title>-cam<ch>'.
    ch = cam.get("channel")
    names = [name]
    if ch is not None:
        suffix = f"-cam{ch}"
        names.append(f"{name[:64 - len(suffix)]}{suffix}")
    api = get_netbox()
    dev = None
    if not _invalid_serial(serial):
        dev = find_device(serial, role_name=role)
    if dev is None:
        for cand in names:
            # adopt by name+site+role only when the candidate has no
            # cam_serial of its own — a device with a DIFFERENT serial is
            # another camera, not this one
            cands = [d for d in api.dcim.devices.filter(
                         name=cand, site_id=site_id, role_id=role_id)
                     if not (d.custom_fields or {}).get("cam_serial")]
            if cands:
                dev = cands[0]
                log("INFO", f"  Found camera by name+site: {cand} (id={dev.id})")
                break
    # Final name: first candidate not held by a DIFFERENT device (any role).
    # Applies to updates too — a serial-matched camera must NOT be renamed
    # into a colliding plain title (that 400'd, and the failed ensure then
    # kept the serial out of seen_camera_serials, so the sweep offlined a
    # healthy camera).
    own_id = dev.id if dev else None
    final = None
    for cand in names:
        if not any(d.id != own_id
                   for d in api.dcim.devices.filter(name=cand, site_id=site_id)):
            final = cand
            break
    if final is None:
        tail = serial[-4:] if serial else (f"ch{ch}" if ch is not None else "x")
        cand = f"{name[:63 - len(tail)]}-{tail}"
        if not any(d.id != own_id
                   for d in api.dcim.devices.filter(name=cand, site_id=site_id)):
            final = cand
    if final is None:
        log("WARN", f"  all camera name candidates taken at site: {names}")
        final = name
    name = final
    cf = {"cam_ip": cam.get("ip"), "cam_enabled": bool(cam.get("online")),
          "cam_nvr": nvr_name, "cam_model": cam.get("model")}
    if serial:
        cf["cam_serial"] = serial
    try:
        cf["cam_channel"] = int(ch)
    except (TypeError, ValueError):
        pass
    if cam.get("mac"):
        cf["cam_mac"] = cam["mac"]
    payload = {"name": name, "status": "active", "site": site_id,
               "device_type": dtype_id, "role": role_id,
               "custom_fields": cf,
               **({"serial": serial} if not _invalid_serial(serial) else {})}
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  camera created: {name} (id={new.id})")
    return new.id


CAMERA_IFACE_NAME = "eth0"

def ensure_camera_interface(dev_id, online=True):
    """Get-or-create the camera's single LAN interface — the cable
    termination point for camera<->switch cabling. Only `enabled` is
    refreshed on existing interfaces."""
    api = get_netbox()
    existing = api.dcim.interfaces.get(device_id=dev_id, name=CAMERA_IFACE_NAME)
    if existing:
        if bool(getattr(existing, "enabled", True)) != bool(online):
            api.dcim.interfaces.update([{"id": existing.id,
                                         "enabled": bool(online)}])
        return existing.id
    new = api.dcim.interfaces.create({
        "device": dev_id, "name": CAMERA_IFACE_NAME, "type": "1000base-t",
        "enabled": bool(online), "mgmt_only": False,
        "description": "netbox-sync: camera LAN"})
    log("INFO", f"  camera interface created: {CAMERA_IFACE_NAME} "
                f"(device id={dev_id})")
    return new.id


def mark_camera_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"cam_enabled": False},
        }])
        log("WARN", f"  camera marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark camera offline {dev_name}: {e}")


def ensure_custom_fields_if_set():
    """Normalize every custom field's UI visibility to 'if-set' (hidden until
    the field carries a value, keeping device pages clean). Runs at the end of
    each sync so custom fields added later are normalized automatically."""
    api = get_netbox()
    updates = []
    for cf in api.extras.custom_fields.all():
        vis = getattr(cf, "ui_visible", None)
        value = vis.get("value") if isinstance(vis, dict) else vis
        # pynetbox returns choice fields as label strings ("If set") — normalize
        normalized = str(value or "").strip().lower().replace(" ", "-")
        if normalized != "if-set":
            updates.append({"id": cf.id, "ui_visible": "if-set"})
    if updates:
        api.extras.custom_fields.update(updates)
        log("INFO", f"  custom fields: set ui_visible=if-set on "
                    f"{len(updates)} field(s)")
    else:
        log("DEBUG", "  custom fields: all already ui_visible=if-set")


# ── Custom-field registry ────────────────────────────────────────────────────
# Every custom field the tool writes/filters, on dcim.device. Source of truth
# for automatic creation — keep in sync with the README tables.
CUSTOM_FIELDS = [
    # (name, type, label) — servers (Redfish)
    ("bmc_ip",                    "text",    "BMC IP"),
    ("redfish_enabled",           "boolean", "Redfish enabled"),
    ("redfish_model",             "text",    "Redfish model"),
    ("redfish_power_state",       "text",    "Power state"),
    ("redfish_bios_version",      "text",    "BIOS version"),
    ("redfish_cpu_model",         "text",    "CPU model"),
    ("redfish_cpu_sockets",       "integer", "CPU sockets"),
    ("redfish_cpu_cores",         "integer", "CPU cores"),
    ("redfish_cpu_threads",       "integer", "CPU threads"),
    ("redfish_ram_gib",           "integer", "RAM (GiB)"),
    ("redfish_disk_total_gib",    "integer", "Total disk (GiB)"),
    # storage (MSA)
    ("storage_ip",                "text",    "Storage IP"),
    ("storage_enabled",           "boolean", "Storage enabled"),
    ("storage_health",            "text",    "Health"),
    ("storage_firmware",          "text",    "Firmware"),
    ("storage_model",             "text",    "Model"),
    ("storage_disk_count",        "integer", "Disk count"),
    ("storage_total_capacity_gib", "integer", "Total capacity (GiB)"),
    # SAN switches (Brocade)
    ("san_switch_ip",             "text",    "SAN switch IP"),
    ("san_switch_enabled",        "boolean", "SAN switch enabled"),
    ("san_switch_wwn",            "text",    "Switch WWN"),
    ("san_switch_firmware",       "text",    "Firmware (Fabric OS)"),
    ("san_switch_model",          "text",    "Model"),
    ("san_switch_port_count",     "integer", "Port count"),
    # Cisco Catalyst
    ("cisco_ip",                  "text",    "Cisco switch IP"),
    ("cisco_enabled",             "boolean", "Cisco switch enabled"),
    ("cisco_firmware",            "text",    "IOS version"),
    ("cisco_model",               "text",    "Model"),
    ("cisco_port_count",          "integer", "Port count"),
    # FortiGate
    ("fortigate_ip",              "text",    "FortiGate IP"),
    ("fortigate_enabled",         "boolean", "FortiGate enabled"),
    ("fortigate_firmware",        "text",    "FortiOS version"),
    ("fortigate_model",           "text",    "Model"),
    ("fortigate_port_count",      "integer", "Port count"),
    ("fortigate_ha_group",        "text",    "HA cluster group name"),
    ("fortigate_ha_mode",         "text",    "HA mode (a-p / a-a)"),
    ("fortigate_ha_peer",         "text",    "HA peer units"),
    ("fortigate_ha_role",         "text",    "Role of the probed unit"),
    # Ruckus ZoneDirector
    ("wlc_ip",                    "text",    "Controller IP"),
    ("wlc_enabled",               "boolean", "Controller enabled"),
    ("wlc_model",                 "text",    "Model"),
    ("wlc_firmware",              "text",    "Firmware"),
    ("wlc_ap_count",              "integer", "AP count"),
    ("wlc_ha_role",               "text",    "HA role"),
    ("wlc_vip",                   "text",    "HA virtual IP"),
    # access points (shared Ruckus/UniFi)
    ("wap_mac",                   "text",    "AP MAC"),
    ("wap_enabled",               "boolean", "AP enabled"),
    ("wap_group",                 "text",    "AP group / site"),
    ("wap_wlc",                   "text",    "Controller name"),
    # NVRs (Hikvision / Dahua / Uniview) + cameras
    ("nvr_ip",                    "text",    "NVR IP"),
    ("nvr_enabled",               "boolean", "NVR enabled"),
    ("nvr_model",                 "text",    "Model"),
    ("nvr_firmware",              "text",    "Firmware version"),
    ("nvr_camera_count",          "integer", "Number of attached cameras"),
    ("cam_ip",                    "text",    "Camera IP"),
    ("cam_mac",                   "text",    "Camera MAC (if known)"),
    ("cam_enabled",               "boolean", "Camera enabled (online)"),
    ("cam_nvr",                   "text",    "Parent NVR name"),
    ("cam_channel",               "integer", "NVR channel number"),
    ("cam_model",                 "text",    "Model"),
    ("cam_serial",                "text",    "Camera serial"),
    # UniFi OS consoles
    ("unifi_ip",                  "text",    "UniFi console IP"),
    ("unifi_enabled",             "boolean", "UniFi enabled"),
    ("unifi_version",             "text",    "UniFi OS version"),
    ("unifi_ap_count",            "integer", "Number of managed APs"),
    ("unifi_sites",               "integer", "Number of sites"),
]


def ensure_custom_fields():
    """Create every missing custom field from CUSTOM_FIELDS (dcim.device,
    ui_visible=if-set), then normalize visibility on all. MUST run before any
    cf_* filter is used in a sync: NetBox SILENTLY IGNORES filters on
    nonexistent custom fields (the filter matches every device) — on a fresh
    NetBox that made the camera sweep mark the whole fleet offline and AP
    MAC lookups adopt random devices."""
    api = get_netbox()
    existing = {cf.name for cf in api.extras.custom_fields.all()}
    created = 0
    for name, typ, label in CUSTOM_FIELDS:
        if name in existing:
            continue
        api.extras.custom_fields.create({
            "name": name, "label": label, "type": typ,
            "object_types": ["dcim.device"], "ui_visible": "if-set",
            "description": "netbox-sync: managed field"})
        created += 1
    if created:
        log("INFO", f"  custom fields: created {created} missing field(s)")
    ensure_custom_fields_if_set()


_WLAN_AUTH_MAP = {"open": "open", "wpa": "wpa-personal",
                  "wpa2": "wpa-personal", "wpa3": "wpa-personal",
                  "802.1x": "wpa-enterprise", "8021x": "wpa-enterprise",
                  "dot1x": "wpa-enterprise"}


def ensure_wireless_lan_group(name):
    api = get_netbox()
    g = api.wireless.wireless_lan_groups.get(name=name)
    if g:
        return g.id
    return api.wireless.wireless_lan_groups.create(
        {"name": name, "slug": slugify(name),
         "description": f"netbox-sync: {name}"}).id


def sync_wireless_lans(wlc_name, wlans, vid_map, group_prefix="ZD"):
    """Sync controller WLANs as NetBox Wireless LANs (ssid/auth/vlan).
    Passphrases are never written. Returns the set of SSIDs seen (for
    sweeping). group_prefix names the Wireless LAN group ('ZD' for Ruckus,
    'UniFi' for UniFi consoles)."""
    api = get_netbox()
    group_id = ensure_wireless_lan_group(f"{group_prefix} {wlc_name}")
    seen = set()
    for w in wlans:
        ssid = (w.get("ssid") or w.get("name") or "").strip()[:32]
        if not ssid:
            continue
        seen.add(ssid)
        payload = {
            "ssid": ssid,
            "group": group_id,
            "auth_type": _WLAN_AUTH_MAP.get((w.get("auth") or "").lower(), "open"),
            "status": "active",
            "description": f"netbox-sync: {wlc_name} {w.get('name')}"[:200],
        }
        if w.get("vlan_id") and w["vlan_id"] in vid_map:
            payload["vlan"] = vid_map[w["vlan_id"]]
        existing = next(iter(api.wireless.wireless_lans.filter(ssid=ssid)), None)
        if existing:
            api.wireless.wireless_lans.update([{"id": existing.id, **payload}])
        else:
            try:
                api.wireless.wireless_lans.create(payload)
            except Exception as exc:
                log("WARN", f"  wlan {ssid}: create failed: {exc}")
    return seen


def sweep_wireless_lans(wlc_name, seen_ssids):
    """Delete marker-owned Wireless LANs of the controller not seen this run."""
    api = get_netbox()
    for wl in list(api.wireless.wireless_lans.filter()):
        desc = getattr(wl, "description", None) or ""
        if not desc.startswith(f"netbox-sync: {wlc_name}"):
            continue
        if wl.ssid not in seen_ssids:
            try:
                wl.delete()
                log("INFO", f"  wireless lan {wl.ssid} deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete wireless lan {wl.ssid}: {exc}")


def ensure_ruckus_device(probe, role, vip):
    """Ensure the NetBox device for a Ruckus ZoneDirector. For HA pairs the
    device represents the CLUSTER (matched by wlc_vip); secondary-unit probes
    only update liveness/role, never cluster identity."""
    from netbox_sync.config import RUCKUS_ROLE
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer("Ruckus")
    role_id = get_or_create_role(RUCKUS_ROLE, "8e44ad")
    site_name = resolve_site(probe.get("hostname") or "",
                             probe.get("reported_ip") or probe["ip"])
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model") or "ZoneDirector",
                                         mfr_id)
    name = (probe.get("hostname") or f"ruckus-{probe['ip'].replace('.', '-')}")[:64]
    api = get_netbox()
    dev = None
    if vip:
        dev = next(iter(api.dcim.devices.filter(cf_wlc_vip=vip)), None)
    if dev is None and not _invalid_serial(serial):
        dev = find_device(serial, role_name=RUCKUS_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id,
                                             role_id=role_id))
        dev = cands[0] if cands else None
        if dev:
            log("INFO", f"  Found ZD by name+site: {name} (id={dev.id})")
    cf = {"wlc_ip": probe["ip"], "wlc_enabled": True,
          "wlc_model": probe.get("model"),
          "wlc_firmware": probe.get("firmware"),
          "wlc_ha_role": role}
    if vip:
        cf["wlc_vip"] = vip
    payload = {"name": name, "status": "active", "site": site_id,
               "device_type": dtype_id, "role": role_id,
               "custom_fields": cf,
               **({"serial": serial} if not _invalid_serial(serial) else {})}
    if dev:
        if role == "secondary":
            # never overwrite cluster identity from a secondary-unit probe
            api.dcim.devices.update([{"id": dev.id, "status": "active",
                                      "custom_fields": cf}])
        else:
            api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  ZD updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  ZD created: {name} (id={new.id})")
    return new.id

# ── Consecutive-failure tracking (prevents flapping) ─────────────────────────
# A device must fail to appear in the scan for OFFLINE_THRESHOLD consecutive
# runs before being marked offline. The counter persists across scheduled runs
# in process memory and resets to 0 the moment the device is seen again.
_scan_fail_counts = {}   # {ip: consecutive_miss_count}

def _check_offline(ip, live_ips, dev_id, dev_name, mark_fn, label):
    """Shared logic: increment miss counter if absent, mark offline only
    when the threshold is reached. Reset counter when the device is present."""
    if ip in live_ips:
        if ip in _scan_fail_counts:
            _scan_fail_counts.pop(ip, None)
        return
    misses = _scan_fail_counts.get(ip, 0) + 1
    _scan_fail_counts[ip] = misses
    if misses >= OFFLINE_THRESHOLD:
        mark_fn(dev_id, dev_name)
        _scan_fail_counts.pop(ip, None)   # reset after marking
    else:
        log("INFO", f"  {label} {dev_name} not seen this run "
            f"(miss {misses}/{OFFLINE_THRESHOLD}) -- keeping active")

# ── inventory sync (shared) ──────────────────────────────────────────────────
def sync_inventory(dev_id, new_inventory):
    api = get_netbox()
    # Single fetch — group existing items by serial
    by_serial = {}
    for item in api.dcim.inventory_items.filter(device_id=dev_id):
        s = str(item.serial or "").strip()
        if s: by_serial.setdefault(s, []).append(item)

    # Delete duplicate entries for the same serial outright; the canonical
    # item is recreated below from the freshly collected inventory.
    for s, items in by_serial.items():
        if len(items) > 1:
            for item in items: item.delete()
            by_serial[s] = []

    # Delete items the device no longer reports
    new_serials = set(new_inventory.keys())
    for s, items in by_serial.items():
        if items and s not in new_serials:
            for item in items: item.delete()
            by_serial[s] = []

    # What remains are live single items whose serial is still reported
    live = {s: items[0] for s, items in by_serial.items() if items}

    # Bulk write: one HTTP call per operation regardless of item count
    updates, creates = [], []
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
        if serial in live:
            updates.append({"id": live[serial].id, **payload})
        else:
            creates.append(payload)
    if updates:
        api.dcim.inventory_items.update(updates)
    if creates:
        api.dcim.inventory_items.create(creates)
