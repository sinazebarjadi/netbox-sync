#!/usr/bin/env python3
"""AssetExplorer -> NetBox sync entry point (offline/inventory assets).

Run daily alongside the main discovery sync:
    python sync_assetexplorer.py
"""
import sys

from netbox_sync.assetexplorer_sync import main

if __name__ == "__main__":
    sys.exit(main())
