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

# ME udf key + value written for in-scope assets (4-digit tag, scoped types).
_VENTURE_FIELD = "udf_sline_901"   # same key the collector reads back
_VENTURE_VALUE = "Central"

# Exact Venture assignment rules enforced during enrichment. Earlier rules win.
# Rule 2 is deliberately overwrite-capable: for the 8-digit CAB tags the user
# requested, ME is authoritative for Venture only. Rule 1 preserves the old
# fill-empty-only behaviour for all other Venture traffic.
_VENTURE_RULES = (
    {"prefix": "3531", "length": 8, "value": "CAB", "overwrite": True},
    {"prefix": "", "length": 4, "value": "Central", "overwrite": False},
)


def _venture_assignment(rec):
    """Return (value, overwrite) for an AE record's Venture rule, else None."""
    tag = (rec.get("asset_tag") or "").strip()
    if not tag.isdigit():
        return None
    for rule in _VENTURE_RULES:
        if len(tag) == rule["length"] and tag.startswith(rule["prefix"]):
            return rule["value"], rule["overwrite"]
    return None


def _is_venture_write_target(rec):
    """True when the ME asset is in the Venture scope: exact-4-digit asset tag,
    has an ME id to PUT against, and is one of the three scoped product types.
    The scoped ME product types map uniquely onto these NetBox roles
    (AE_TYPE_TO_ROLE: Server->Server, CCTV->Camera, Storage Device->Storage),
    so we scope on rec['role']."""
    tag = (rec.get("asset_tag") or "").strip()
    if not tag.isdigit():
        return False
    if not rec.get("ae_id"):
        return False
    return _venture_assignment(rec) is not None and rec.get("role") in {
        "Server", "Camera", "Storage"}

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
    # When multiple devices share a serial (ME-created duplicate + the real
    # automation-discovered device), prefer the one WITHOUT ae_asset_id —
    # the real device — so the Asset Tag lands on it, not on the ME duplicate.
    existing = {}
    by_name = {}
    by_suffix = {}   # trailing-9-char hardware serial -> devices
    def _is_automation_owned(dev):
        """A device the main automation created/discovered: identified by
        sync evidence (cam/BMC/storage/switch/HA custom fields) rather than
        the AE tag alone. AE also stamps ae_asset_id on enriched automation
        records, so the presence of that marker alone does NOT make a device
        ME-created — use the automation's own fingerprints instead."""
        cf = getattr(dev, "custom_fields", None) or {}
        return bool(
            cf.get("cam_serial") or cf.get("cam_nvr")
            or cf.get("bmc_ip") or cf.get("storage_ip")
            or cf.get("cisco_ip") or cf.get("san_switch_wwn")
            or cf.get("fortigate_ip") or cf.get("fortiweb_ip")
            or cf.get("nvr_ip") or cf.get("cam_ip")
        )

    for d in api.dcim.devices.filter():
        s = (d.serial or "").strip().lower()
        if s:
            # Prefer the automation-owned record over an ME-created bare
            # duplicate sharing the same serial. Automation ownership is
            # detected via sync evidence (cam_serial/cam_nvr/BMC IP/etc),
            # because AE also backfills ae_asset_id onto enriched automation
            # records, making that marker alone unreliable for arbitration.
            if s not in existing:
                existing[s] = d
            elif _is_automation_owned(existing[s]):
                # Existing device is automation-owned — keep it
                pass
            elif _is_automation_owned(d):
                # This one is automation-owned — prefer it over the placeholder
                existing[s] = d
            elif not (existing[s].custom_fields or {}).get("ae_asset_id"):
                # Neither shows automation evidence; keep the first record
                # (matches prior behaviour)
                pass
            else:
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
    # Names are NOT unique across sites (every branch has a camera called
    # 'Salon'), so keep ALL records per name — the stale-serial check must
    # only consider same-site candidates, never a same-named asset elsewhere.
    by_ae_name = {}
    for rec in assets:
        n = (rec.get("name") or "").strip().lower()
        if n:
            by_ae_name.setdefault(n, []).append(rec)

    # ── Layer 1: detect duplicate serials / asset tags inside ME itself ──
    # When ME has two assets with the same serial, matching by serial is
    # ambiguous — we must not guess which record's tag to apply.
    from collections import Counter, defaultdict
    me_serial_groups = defaultdict(list)
    for r in assets:
        me_serial_groups[r["serial"].lower()].append(r)
    # A serial is a *conflict* only when distinct assets (different names or
    # different tags) share it. Identical repeats of the same record are
    # harmless and processed normally.
    me_dup_serials = set()
    for s, recs in me_serial_groups.items():
        names = {(r.get("name") or "").strip().lower() for r in recs}
        tags = {(r.get("asset_tag") or "").strip().lower() for r in recs}
        if len(names) > 1 or len(tags) > 1:
            me_dup_serials.add(s)
    if me_dup_serials:
        for s in sorted(me_dup_serials):
            recs = me_serial_groups[s]
            names = ", ".join(f"{r['name']!r}(id={r['ae_id']},tag={r.get('asset_tag') or '-'})"
                              for r in recs)
            log("WARN", f"  duplicate serial in ME: {s} -> {names}")
    me_tag_counts = Counter((r.get("asset_tag") or "").strip().lower()
                            for r in assets if (r.get("asset_tag") or "").strip())
    me_dup_tags = {t for t, c in me_tag_counts.items() if c > 1}
    if me_dup_tags:
        for t in sorted(me_dup_tags):
            recs = [r for r in assets
                    if (r.get("asset_tag") or "").strip().lower() == t]
            names = ", ".join(f"{r['name']!r}(id={r['ae_id']})" for r in recs)
            log("WARN", f"  duplicate asset tag in ME: {t} -> {names}")

    created = tags_synced = matched_skipped = name_matched = failures = 0
    suffix_matched = skipped_bad_serial = skipped_me_dup = 0
    inv_items_created = inv_items_updated = inv_items_skipped = 0
    failure_lines = []

    for rec in assets:
        serial = rec["serial"]
        rec_name = (rec.get("name") or "").strip().lower()

        # ── Layer 2: duplicate serial in ME — never guess which record wins ──
        # Allow the record whose NAME matches the NetBox device; skip others.
        if not _invalid_serial(serial) and serial.lower() in me_dup_serials:
            candidates = [r for r in assets if r["serial"].lower() == serial.lower()]
            nb_dev = existing.get(serial.lower())
            nb_name = (nb_dev.name or "").lower() if nb_dev else None
            if nb_name and rec_name != nb_name:
                skipped_me_dup += 1
                log("WARN", f"  duplicate serial in ME: {serial} — skipping "
                            f"{rec['name']!r} (NetBox device is {nb_dev.name!r})")
                continue
            if nb_name is None:
                # serial not in NetBox at all and ME has two records for it
                skipped_me_dup += 1
                log("WARN", f"  duplicate serial in ME: {serial} — no NetBox "
                            f"device to disambiguate; skipping {rec['name']!r}")
                continue

        # ── primary: match by serial ─────────────────────────────────────
        cur = None
        if not _invalid_serial(serial):
            cur = existing.get(serial.lower())
        if cur is not None and cur.name.lower() != rec_name:
            # Names differ — could be a rename (fine) OR a stale serial
            # (dangerous: AE says this serial belongs to a different name).
            #Detect stale: AE has ANOTHER asset whose name matches the
            # NetBox device but whose serial differs -> the NetBox serial
            # is outdated, don't trust this match.
            # But first: check if the names are actually the same device
            # (case-insensitive match handles AE uppercase vs NetBox mixed case)
            if cur.name.lower() == rec_name.lower():
                pass  # same name, just different case — proceed with update
            elif not (cur.custom_fields or {}).get("ae_asset_id"):
                # Automation-created record (no AE provenance marker): the
                # serial is the source of truth here. AE assigning the same
                # serial to a differently-named asset does NOT invalidate the
                # match — unless the AE batch itself proves the NetBox serial is
                # stale: another AE asset carries THIS record's NetBox name AT
                # THE SAME SITE with a different serial. Names repeat across
                # sites (e.g. every site has a camera called 'Salon'), so a
                # same-name hit elsewhere must not veto the match.
                #
                # EXCEPTION: when the current record is a live, NVR-discovered
                # camera (has cam_serial), even a same-site ME name collision
                # does not invalidate it. The NVR-reported serial on the live
                # device is ground truth for that physical camera; ME naming is
                # administrative metadata and may be stale on EITHER side. ME
                # must never veto an automation identity — it may only fill
                # missing fields below.
                if (cur.custom_fields or {}).get("cam_serial"):
                    pass  # live automation camera: keep the serial match
                else:
                    nb_name = cur.name.lower()
                    cur_site = (getattr(getattr(cur, "site", None), "name", "")
                                or "").strip().lower()
                    rec_site = (rec.get("site") or "").strip().lower()
                    candidates = by_ae_name.get(nb_name) or []
                    stale = next(
                        (c for c in candidates
                         if str(c.get("serial") or "").lower() != serial.lower()
                         and str(c.get("site") or "").strip().lower() in (cur_site, rec_site, "")),
                        None,
                    )
                    if stale is not None:
                        log("WARN", f"  serial {serial} is held by {cur.name!r} "
                                    f"but AE assigns {stale['serial']} to "
                                    f"that name — stale serial, using name fallback")
                        cur = None
                # otherwise: keep the serial match (main automation is source
                # of truth), updating only empty fields below.
            else:
                # ME-tagged record holding the serial under a different name.
                # Default: same stale-serial guard, site-scoped. BUT if this
                # record is automation-owned (live cam_serial), ME's naming
                # claim cannot invalidate the physical identity — keep it.
                if (cur.custom_fields or {}).get("cam_serial"):
                    pass  # live automation camera — ME naming is metadata only
                else:
                    nb_name = cur.name.lower()
                    cur_site = (getattr(getattr(cur, "site", None), "name", "")
                                or "").strip().lower()
                    candidates = by_ae_name.get(nb_name) or []
                    stale = next(
                        (c for c in candidates
                         if str(c.get("serial") or "").lower() != serial.lower()
                         and str(c.get("site") or "").strip().lower() in (cur_site, "")),
                        None,
                    )
                    if stale is not None:
                        log("WARN", f"  serial {serial} is held by {cur.name!r} "
                                    f"but AE assigns {stale['serial']} to "
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
            # Guard: never treat a placeholder device (no cam_serial / no
            # cam_nvr — an AE-created bare record that failed enrichment) as a
            # name-match. Such devices are duplicates waiting to be cleaned up,
            # not the authoritative record to enrich.
            real_cands = [c for c in cands
                          if (c.custom_fields or {}).get("cam_serial")
                          or (c.custom_fields or {}).get("cam_nvr")
                          or not (c.custom_fields or {}).get("ae_asset_id")]
            cands = real_cands
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
            elif len(cands) == 0:
                # Only placeholder devices matched by name (AE-created bare
                # records that failed enrichment). Skip — creating would just
                # mint another duplicate.
                skipped_bad_serial += 1
                log("WARN", f"  name {rec['name']!r} matched only placeholder "
                            f"devices (no cam evidence) — skipping")

        if cur is not None:
            # ── matched device -> enrich ONLY missing information ────────
            # The discovery automation is the absolute source of truth for
            # all devices it manages. ManageEngine NEVER overwrites any
            # existing value in NetBox. It only fills fields that are EMPTY:
            # 1. asset_tag: if NetBox has no asset tag and ME provides one
            # 2. ae_department: if NetBox has no department and ME has one
            # 3. serial: if NetBox has no serial and ME provides one
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

            # Venture enrichment follows the explicit tag assignment rules.
            # Ordinary assets are fill-empty-only; the 3531nnnn rule is the one
            # scoped overwrite, because the user declared those tags authoritative
            # for Venture=CAB. Everything else in custom_fields stays intact.
            rule = _venture_assignment(rec)
            nb_venture = str((cur.custom_fields or {}).get("venture") or "").strip()
            if rule:
                venture_value, overwrite = rule
                if nb_venture != venture_value and (overwrite or not nb_venture):
                    cf = dict(update_payload.get("custom_fields")
                              or cur.custom_fields or {})
                    cf["venture"] = venture_value
                    update_payload["custom_fields"] = cf
                    changed = True

            # Serial fallback: fill NetBox serial from ME ONLY when empty.
            # The discovery automation (Redfish/iLO) is the primary source —
            # if it later provides a serial, it will overwrite this value
            # on the next run (source of truth). ME is the fallback.
            nb_serial = (cur.serial or "").strip()
            ae_serial = (rec.get("serial") or "").strip()
            if not nb_serial and ae_serial and not _invalid_serial(ae_serial):
                update_payload["serial"] = ae_serial
                changed = True

            if changed:
                try:
                    api.dcim.devices.update([update_payload])
                    if "asset_tag" in update_payload:
                        tags_synced += 1
                        log("INFO", f"  asset-tag populated: {rec.get('serial')} "
                                    f"{cur.name} -> {ae_tag}")
                    if "serial" in update_payload:
                        log("INFO", f"  serial populated (ME fallback): "
                                    f"{cur.name} -> {ae_serial}")
                except Exception as e:
                    if "asset tag already exists" in str(e):
                        retry_payload = {"id": update_payload["id"]}
                        if update_payload.get("custom_fields"):
                            retry_payload["custom_fields"] = update_payload["custom_fields"]
                        api.dcim.devices.update([retry_payload])
                        matched_skipped += 1
                        log("WARN", f"  asset-tag {ae_tag} held by another "
                                    f"device — updated custom_fields only for "
                                    f"{rec.get('serial')}")
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

        # ── no match at all — CAB rule is explicitly enrichment-only. ─────
        # Exact 3531nnnn tags may be shared by multiple ME records. Creating a
        # new device under such a tag cannot be made deterministic without a
        # matching serial; skip rather than mint a duplicate/placeholder.
        rule = _venture_assignment(rec)
        if rule and rule[0] == "CAB" and rule[1]:
            skipped_bad_serial += 1
            log("WARN", f"  CAB-target {rec.get('asset_tag')} has no serial-matched "
                        f"device in NetBox — skipping create for {rec['name']!r}")
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
    log("INFO", f"  skipped (duplicate serial in ME): {skipped_me_dup}")
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
    or to the single HQ Warehouse-Stock container if unattached.

    HDD fallback chain:
      1. NVR-direct discovery (Hikvision ISAPI) — primary, always tried first
      2. ManageEngine HARD-CCTV with used_by_asset -> NVR — fallback for
         NVRs where direct discovery fails (Dahua 501, Uniview 599)
    """
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

    # Rule 2: Check if an inventory item with this serial already exists ANYWHERE
    # in NetBox (not just on this device). One physical item = one serial = one
    # inventory item across the entire inventory.
    # 
    # Logic:
    #   - Serial on a DIFFERENT real device (not warehouse, not this device)
    #     -> skip (main automation owns it there)
    #   - Serial on this SAME device -> update in place
    #   - Serial ONLY on warehouse containers -> MOVE it to the real device
    #     (delete the warehouse copy, create on the device)
    cands = list(api.dcim.inventory_items.filter(serial=serial))
    warehouse_copies = []
    same_device_copy = None
    for c in cands:
        dev = getattr(c, "device", None)
        dev_name = dev.name if dev else ""
        dev_id = dev.id if dev else None
        if dev_id == parent.id:
            same_device_copy = c
        elif dev_name and "Warehouse-Stock" in dev_name:
            warehouse_copies.append(c)
        elif dev_name:
            # On a DIFFERENT real device — skip (main automation owns it there)
            log("DEBUG", f"  inventory item {serial} already exists on "
                        f"{dev_name} (id={c.id}) — skipping ME import")
            return "skipped"

    if same_device_copy:
        # Update in place below
        pass
    elif warehouse_copies:
        # The item is being installed on a real device — remove warehouse copies
        for wc in warehouse_copies:
            try:
                wc.delete()
                log("INFO", f"  inventory item {serial} moved from "
                            f"{getattr(wc.device, 'name', '?')} to {parent.name}")
            except Exception as e:
                log("WARN", f"  failed to delete warehouse copy {wc.id}: {e}")
        cands = []   # will create fresh on the real device
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

    if same_device_copy:
        existing_item = same_device_copy
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
          "ae_location":   rec.get("location"),
          "venture":       rec.get("venture")}
    # Use Part Number (P/N) for device_type when available (more specific than
    # product.name, e.g. "C9200L-48P-4X-E V02" vs "C9200L").
    device_type_model = rec.get("part_number") or rec.get("model")
    return {
        "name": rec["name"][:64],
        "serial": rec["serial"],
        "status": rec["status"],
        "asset_tag": rec.get("asset_tag"),
        "comments": rec.get("description") or "",
        "device_type": get_or_create_device_type(device_type_model, _mfr_id(rec),
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
