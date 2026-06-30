"""Device model normalization maps for the NetBox sync tool.

HPE ProLiant server and HPE MSA storage model-name aliases live here so the
sync script can stay product-agnostic. Import via:

    from models import SERVER_MODEL_MAP, STORAGE_MODEL_MAP

Keys are the raw vendor strings (lowercased) as returned by Redfish
(servers) or the MSA XML API (storage); values are the canonical NetBox
device-type model names.
"""

# ── HPE ProLiant servers ─────────────────────────────────────────────────────
# Keys: raw Redfish "Model" string, lowercased.
SERVER_MODEL_MAP = {
    "proliant dl360 gen8":       "HPE DL360 G8",
    "proliant dl360p gen8":      "HPE DL360 G8",
    "proliant dl380 gen8":       "HPE DL380 G8",
    "proliant dl380p gen8":      "HPE DL380 G8",
    "proliant dl360 gen9":       "HPE DL360 G9",
    "proliant dl380 gen9":       "HPE DL380 G9",
    "proliant dl360 gen10":      "HPE DL360 G10",
    "proliant dl380 gen10":      "HPE DL380 G10",
    "proliant dl360 gen10 plus": "HPE DL360 G10+",
    "proliant dl380 gen10 plus": "HPE DL380 G10+",
    "proliant dl320 gen11":      "HPE DL320 G11",
    "proliant dl360 gen11":      "HPE DL360 G11",
    "proliant dl380 gen11":      "HPE DL380 G11",
}

# ── HPE MSA storage arrays ───────────────────────────────────────────────────
# Keys: raw MSA "product-id" string, lowercased.
# Note: MSA 2040-class firmware reports disk fields via "show disk-parameters"
# while newer MSA 2060-class reports them via "show disks". The storage
# collector tries both commands, so this map does not drive that logic.
STORAGE_MODEL_MAP = {
    "msa 2040 san":  "HPE MSA 2040",
    "msa 2040":      "HPE MSA 2040",
    "msa 2042 san":  "HPE MSA 2042",
    "msa 2050 san":  "HPE MSA 2050",
    "msa 2052 san":  "HPE MSA 2052",
}
