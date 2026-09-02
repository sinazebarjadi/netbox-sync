"""AssetExplorer -> NetBox sync.

Two jobs, and only these two:
1. Serial found in NetBox  -> sync the Asset Tag ONLY (never any other field;
   the discovery automation remains source of truth for existing devices).
2. Serial NOT in NetBox   -> create the device from AE data (offline / in-store
   inventory the discovery automation cannot reach).

Matching is case-insensitive (serials and reference objects). Every run is
idempotent: multiple runs never create duplicates or unexpected writes.
"""
import os
import time

from netbox_sync.collectors.assetexplorer import ae_fetch_assets
from netbox_sync.config import log
from netbox_sync.netbox import (get_netbox, get_or_create_device_type,
                                get_or_create_manufacturer, get_or_create_role,
                                get_or_create_site, ensure_custom_fields,
                                get_or_create_inventory_role)
from netbox_sync.utils import _invalid_serial

_SERIAL_SUFFIX_LEN = 9   # Hikvision hardware serial = trailing 9 chars of the
                         # long NVR-reported serial (e.g. ...AAWRFB0225316)

def _serial_suffix(s):
    """Trailing 9 alphanumeric chars, upper-cased — the physical hardware
    serial printed on the device / stored in ManageEngine."""
    s = "".join(c for c in str(s or "") if c.isalnum()).upper()
    return s[-_SERIAL_SUFFIX_LEN:] if len(s) >= _SERIAL_SUFFIX_LEN else ""


def sync_assetexplorer():
    log("INFO", "=" * 60)
    log("INFO", "AssetExplorer sync started (asset-tag sync + missing devices)")
    log("INFO", "=" * 60)

    try:
        ensure_custom_fields()
    except Exception as e:
        log("ERROR", f"  custom-field bootstrap failed: {e}")

    api = get_netbox()
    assets, stats = ae_fetch_assets()

    # Serial -> device index, single fetch (no N+1). Keys are lower-cased so
    # case differences never split into a duplicate device.
    existing = {}
    by_name = {}
    by_suffix = {}   # trailing-9-char hardware serial -> devices
    for d in api.dcim.devices.filter():
        s = (d.serial or "").strip().lower()
        if s and s not in existing:
            existing[s] = d
            sfx = _serial_suffix(s)
            if sfx:
                by_suffix.setdefault(sfx, []).append(d)
        n = (d.name or "").strip().lower()
        if n:
            by_name.setdefault(n, []).append(d)
    log("INFO", f"  netbox: {len(existing)} devices with serials indexed")

    # AE name -> record index, for detecting stale serials (AE says serial X
    # belongs to name N, but NetBox has serial X on a different name).
    by_ae_name = {}
    for rec in assets:
        n = (rec.get("name") or "").strip().lower()
        if n and n not in by_ae_name:
            by_ae_name[n] = rec

    created = tags_synced = matched_skipped = name_matched = failures = 0
    suffix_matched = skipped_bad_serial = 0
    inv_items_created = inv_items_updated = inv_items_skipped = 0
    failure_lines = []

    for rec in assets:
        serial = rec["serial"]
        rec_name = (rec.get("name") or "").strip().lower()

        # ── primary: match by serial ─────────────────────────────────────
        cur = None
        if not _invalid_serial(serial):
            cur = existing.get(serial.lower())
            if cur is not None and cur.name.lower() != rec_name:
                # Names differ — could be a rename (fine) OR a stale serial
                # (dangerous: AE says this serial belongs to a different name).
                # Detect stale: AE has ANOTHER asset whose name matches the
                # NetBox device but whose serial differs -> the NetBox serial
                # is outdated, don't trust this match.
                nb_name = cur.name.lower()
                ae_for_nb_name = by_ae_name.get(nb_name)
                if (ae_for_nb_name is not None
                        and ae_for_nb_name["serial"].lower() != serial.lower()):
                    log("WARN", f"  serial {serial} is held by {cur.name!r} "
                                f"but AE assigns {ae_for_nb_name['serial']} to "
                                f"that name — stale serial, using name fallback")
                    cur = None

        # ── fallback 1: hardware-serial suffix (NVR long serial vs the
        #    short serial printed on the camera / stored in ME) ───────────
        if cur is None and not _invalid_serial(serial):
            sfx = _serial_suffix(serial)
            ae_len = len("".join(c for c in serial if c.isalnum()))
            cands = by_suffix.get(sfx, []) if sfx else []
            # Only when the AE serial is strictly shorter than the NetBox
            # serial (the NVR-prefix case). Same-length serials would have
            # matched exactly already; a suffix hit there is a stale serial.
            cands = [c for c in cands
                     if len("".join(ch for ch in (c.serial or "")
                                    if ch.isalnum())) > ae_len]
            if len(cands) == 1:
                cur = cands[0]
                suffix_matched += 1
                log("DEBUG", f"  suffix-matched: {serial} -> {cur.serial} "
                             f"(id={cur.id})")
            elif len(cands) > 1:
                # ambiguous: NEVER guess — no name fallback, no create
                skipped_bad_serial += 1
                log("WARN", f"  serial suffix {sfx} for {serial} is ambiguous "
                            f"({len(cands)} devices) — skipped")
                continue
        if cur is None and rec_name:
            cands = by_name.get(rec_name, [])
            if len(cands) == 1:
                cur = cands[0]
                name_matched += 1
                log("DEBUG", f"  name-matched: {rec['name']} -> id={cur.id}")
            elif len(cands) > 1:
                # prefer the device at the AE site; ambiguous otherwise
                site_name = (rec.get("site") or "").strip().lower()
                same_site = [d for d in cands
                             if (getattr(getattr(d, "site", None), "name", "")
                                 or "").lower() == site_name]
                if len(same_site) == 1:
                    cur = same_site[0]
                    name_matched += 1
                    log("DEBUG", f"  name+site-matched: {rec['name']} "
                                 f"-> id={cur.id}")
                else:
                    log("WARN", f"  name {rec['name']!r} is ambiguous "
                                f"({len(cands)} devices) — skipped")

        if cur is not None:
            # ── matched device -> enrich ONLY missing information ────────
            # The discovery automation is the absolute source of truth for
            # all devices it manages. ManageEngine NEVER overwrites any
            # existing value in NetBox. It only fills fields that are EMPTY:
            # 1. asset_tag: if NetBox has no asset tag and ME provides one
            # 2. ae_department: if NetBox has no department and ME has one
            update_payload = {"id": cur.id}
            changed = False

            ae_tag = (rec.get("asset_tag") or "").strip()
            nb_tag = (cur.asset_tag or "").strip()
            # ONLY fill if NetBox tag is currently empty (never overwrite an existing tag)
            if not nb_tag and ae_tag:
                update_payload["asset_tag"] = ae_tag
                changed = True

            # Department enrichment (only if empty in NetBox)
            ae_dept = (rec.get("department") or "").strip()
            nb_dept = str((cur.custom_fields or {}).get("ae_department") or "").strip()
            if not nb_dept and ae_dept:
                cf = dict(cur.custom_fields or {})
                cf["ae_department"] = ae_dept
                update_payload["custom_fields"] = cf
                changed = True

            if changed:
                try:
                    api.dcim.devices.update([update_payload])
                    if "asset_tag" in update_payload:
                        tags_synced += 1
                        log("INFO", f"  asset-tag populated: {rec.get('serial')} "
                                    f"{cur.name} -> {ae_tag}")
                except Exception as e:
                    if "asset tag already exists" in str(e):
                        matched_skipped += 1
                        log("WARN", f"  asset-tag {ae_tag} held by another "
                                    f"device — skipped for {rec.get('serial')}")
                    else:
                        failures += 1
                        failure_lines.append(
                            f"{rec.get('serial')}: update failed: {e}")
                        log("ERROR", f"  update failed for {rec.get('serial')}: {e}")
            else:
                matched_skipped += 1
            continue

        # ── component (Inventory Item) path ─────────────────────────────
        if rec.get("is_component"):
            try:
                rc = _ensure_inventory_item(api, rec, existing, by_name)
                if rc == "created":
                    inv_items_created += 1
                elif rc == "updated":
                    inv_items_updated += 1
                else:
                    inv_items_skipped += 1
            except Exception as e:
                failures += 1
                failure_lines.append(f"{serial}: inventory item failed: {e}")
                log("ERROR", f"  AE inventory item failed for {serial}: {e}")
            continue

        # ── no match at all -> create (serial required for safe identity) ─
        if _invalid_serial(serial):
            skipped_bad_serial += 1
            log("WARN", f"  skipped (no serial, no name match): "
                        f"{rec['name']} (ae_id={rec['ae_id']})")
            continue
        try:
            dev = _create_device(api, rec)
            existing[serial.lower()] = dev   # guard AE-internal dupes
            by_name.setdefault(rec_name, []).append(dev)
            created += 1
        except Exception as e:
            failures += 1
            failure_lines.append(f"{serial}: create failed: {e}")
            log("ERROR", f"  AE create failed for {serial} ({rec['name']}): {e}")

    log("INFO", "=" * 60)
    log("INFO", "AE SYNC SUMMARY")
    log("INFO", f"  ME assets processed:            {stats['fetched']}")
    log("INFO", f"  skipped (no/invalid product type): {stats['skipped_no_type']}")
    log("INFO", f"  skipped (no serial):            {stats['skipped_no_serial'] + skipped_bad_serial}")
    log("INFO", f"  serial matches (existing devices): {tags_synced + matched_skipped - name_matched - suffix_matched}")
    log("INFO", f"  hardware-suffix matches (camera serials): {suffix_matched}")
    log("INFO", f"  name matches (serial missing/unmatched): {name_matched}")
    log("INFO", f"  asset tags updated:             {tags_synced}")
    log("INFO", f"  existing devices skipped:       {matched_skipped}")
    log("INFO", f"  new devices created:            {created}")
    log("INFO", f"  inventory items created:        {inv_items_created}")
    log("INFO", f"  inventory items updated:        {inv_items_updated}")
    log("INFO", f"  inventory items skipped:        {inv_items_skipped}")
    log("INFO", f"  failures:                       {failures}")
    for line in failure_lines:
        log("WARN", f"    failed: {line}")
    log("INFO", "AssetExplorer sync complete")


def _ensure_inventory_item(api, rec, existing_devices, by_name):
    """Sync a component (Power Module, HARD-Hardware, HARD-CCTV, NM Module)
    as a NetBox Inventory Item attached to its parent device (via used_by_asset)
    or to the single HQ Warehouse-Stock container if unattached."""
    serial = rec["serial"]
    parent = None

    # Try matching parent by used_by serial first, then by used_by name
    used_serial = (rec.get("used_by_serial") or "").strip().lower()
    used_name = (rec.get("used_by_name") or "").strip().lower()

    if used_serial and not _invalid_serial(used_serial):
        parent = existing_devices.get(used_serial)
    if parent is None and used_name:
        cands = by_name.get(used_name, [])
        if len(cands) == 1:
            parent = cands[0]

    # Rule 1: Warehouse Stock exists ONLY for the HQ site. All unattached items go to HQ.
    if parent is None:
        site_id = get_or_create_site("HQ")
        warehouse_name = "Warehouse-Stock-HQ"
        mfr_id = get_or_create_manufacturer("Generic")
        dtype_id = get_or_create_device_type("Warehouse Inventory", mfr_id)
        role_id = get_or_create_role("Inventory")
        
        cands = list(api.dcim.devices.filter(name=warehouse_name, site_id=site_id))
        if cands:
            parent = cands[0]
        else:
            parent = api.dcim.devices.create({
                "name": warehouse_name, "site": site_id,
                "device_type": dtype_id, "role": role_id, "status": "inventory",
                "comments": "Container for offline/spare inventory items from AssetExplorer"
            })

    # Rule 2: Check if an inventory item with this serial already exists on this device
    cands = list(api.dcim.inventory_items.filter(device_id=parent.id, serial=serial))
    role_id = get_or_create_inventory_role(rec.get("component_role") or "Other")
    mfr_id = get_or_create_manufacturer(rec.get("manufacturer") or "Unknown")

    # Build descriptive name including capacity if present (e.g. "Disk 960GB" or item name)
    cap = rec.get("capacity")
    name = rec["name"]
    if cap and cap not in name:
        name = f"{name} ({cap})"

    desc_parts = []
    if rec.get("description"):
        desc_parts.append(rec["description"])
    if cap:
        desc_parts.append(f"Capacity: {cap}")
    if rec.get("department"):
        desc_parts.append(f"Department: {rec['department']}")
    if rec.get("asset_tag"):
        desc_parts.append(f"Asset Tag: {rec['asset_tag']}")
    description = " | ".join(desc_parts)

    payload = {
        "device":       parent.id,
        "name":         name[:64],
        "serial":       serial,
        "part_id":      (rec.get("part_number") or "")[:50] or None,
        "role":         role_id,
        "manufacturer": mfr_id,
        "description":  description[:200],
    }
    if not payload["part_id"]:
        payload.pop("part_id", None)

    if cands:
        existing_item = cands[0]
        # Idempotency: only update if something actually changed
        changed = False
        update_payload = {"id": existing_item.id}
        for k, v in payload.items():
            if k == "device":
                continue
            cur_val = getattr(existing_item, k, None)
            # Normalize None and ""
            if (cur_val or "") == (v or ""):
                continue
            update_payload[k] = v
            changed = True
        if changed:
            api.dcim.inventory_items.update([update_payload])
            log("DEBUG", f"  inventory item updated: {serial} on {parent.name}")
            return "updated"
        else:
            log("DEBUG", f"  inventory item unchanged: {serial} on {parent.name}")
            return "skipped"
    else:
        api.dcim.inventory_items.create(payload)
        log("INFO", f"  inventory item created: {name} ({serial}) on {parent.name}")
        return "created"


def _payload(rec):
    cf = {"ae_asset_id":  str(rec["ae_id"]),
          "ae_department": rec.get("department"),
          "ae_location":   rec.get("location")}
    return {
        "name": rec["name"][:64],
        "serial": rec["serial"],
        "status": rec["status"],
        "asset_tag": rec.get("asset_tag"),
        "comments": rec.get("description") or "",
        "device_type": get_or_create_device_type(rec.get("model"), _mfr_id(rec),
                                                 _model_map(rec)),
        "role": get_or_create_role(rec["role"]),
        "site": get_or_create_site(rec["site"]) if rec.get("site") else None,
        "custom_fields": cf,
    }


def _mfr_id(rec):
    return get_or_create_manufacturer(rec.get("manufacturer") or "Unknown")


def _model_map(rec):
    return {}   # AE product names are used verbatim (no alias map)


def _create_device(api, rec):
    p = _payload(rec)
    if p["site"] is None:
        p.pop("site")
    try:
        dev = api.dcim.devices.create(p)
    except Exception as e:
        msg = str(e)
        if "asset tag already exists" in msg:
            # Tag is held by another (stale) device — create without it rather
            # than dropping the asset entirely.
            log("WARN", f"  asset_tag {p.get('asset_tag')} already used — "
                        f"creating {rec['name']} without it")
            p.pop("asset_tag", None)
            try:
                dev = api.dcim.devices.create(p)
            except Exception as e2:
                dev = _adopt_name_collision(api, rec, p, e2)
                if dev is None:
                    raise
        elif "must be unique per site" in msg:
            dev = _adopt_name_collision(api, rec, p, e)
            if dev is None:
                raise
        else:
            raise
    log("INFO", f"  AE device created: {rec['name']} "
                f"(serial={rec['serial']}, role={rec['role']}, id={dev.id})")
    return dev


def _adopt_name_collision(api, rec, payload, orig_exc):
    """Create failed with 'name must be unique per site': a device with that
    name already exists at the site. If it carries NO serial (discovered with
    a blank serial), adopt it — fill in the AE serial + asset tag. If it has
    a different serial, it's a genuinely different device -> skip (None)."""
    try:
        site_id = payload.get("site")
        role_id = payload.get("role")
        cands = list(api.dcim.devices.filter(name=payload["name"],
                                             site_id=site_id,
                                             role_id=role_id))
        if len(cands) != 1:
            return None
        dev = cands[0]
        if not _invalid_serial(getattr(dev, "serial", "")):
            return None     # different real serial — never hijack
        api.dcim.devices.update([{"id": dev.id, "serial": rec["serial"],
                                  "asset_tag": rec.get("asset_tag")}])
        log("INFO", f"  AE adopted blank-serial device: {rec['name']} "
                    f"(id={dev.id}, serial={rec['serial']})")
        return dev
    except Exception:
        log("ERROR", f"  name-collision adoption failed for "
                     f"{rec['name']}: {orig_exc}")
        return None


def main():
    try:
        from netbox_sync.config import _validate_config
        _validate_config()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1
    if not (os.getenv("AE_URL") and os.getenv("AE_API_KEY")):
        log("ERROR", "AE_URL / AE_API_KEY not set — nothing to do.")
        return 1
    try:
        sync_assetexplorer()
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
        return 130
    except Exception:
        import traceback; traceback.print_exc()
        return 1
    return 0
