#!/usr/bin/env python3
"""Wipe cables first, then wipe devices in small fast parallel batches."""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import dotenv

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    dotenv.load_dotenv(env_path)
else:
    dotenv.load_dotenv()

import requests
import urllib3

urllib3.disable_warnings()

NETBOX_URL = os.getenv("NETBOX_URL")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")

def delete_single(url, headers):
    try:
        r = requests.delete(url, headers=headers, verify=False, timeout=10)
        return r.status_code in (204, 404)
    except Exception:
        return False

def wipe_endpoint(api, endpoint_name, headers, workers=20):
    print(f"\n--- Wiping {endpoint_name} ---")
    url = f"{api}/{endpoint_name}/?limit=1000&brief=1"
    ids = []
    while url:
        r = requests.get(url, headers=headers, verify=False, timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        ids.extend([d["id"] for d in data.get("results", [])])
        url = data.get("next")

    total = len(ids)
    print(f"Found {total} {endpoint_name}.")
    if total == 0:
        return

    # Delete in parallel
    deleted = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(delete_single, f"{api}/{endpoint_name}/{item_id}/", headers) for item_id in ids]
        for f in as_completed(futures):
            if f.result():
                deleted += 1
            if deleted % 50 == 0 or deleted == total:
                print(f"  {endpoint_name} deleted: {deleted}/{total}...", end="\r", flush=True)
    print(f"\n  Finished {endpoint_name}: {deleted}/{total} deleted.")

def main():
    if not NETBOX_URL or not NETBOX_TOKEN:
        print("ERROR: NETBOX_URL and NETBOX_TOKEN required.")
        return 1

    headers = {
        "Authorization": f"Token {NETBOX_TOKEN}",
        "Content-Type": "application/json",
    }
    api = f"{NETBOX_URL.rstrip('/')}/api"

    # Step 1: Wipe cables first (cables cause the database lock/hang on device delete!)
    wipe_endpoint(api, "dcim/cables", headers, workers=20)

    # Step 2: Wipe devices in parallel
    wipe_endpoint(api, "dcim/devices", headers, workers=20)

    print("\nAll devices and cables wiped cleanly!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
