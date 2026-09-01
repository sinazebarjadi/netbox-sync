#!/usr/bin/env bash
# Daily NetBox pipeline: main discovery sync, then AssetExplorer sync.
#
# Runs sequentially: the AssetExplorer sync starts ONLY if the main sync
# exited 0. All output goes to dated logs under ./logs next to this script.
#
# Cron example (midnight daily):
#   0 0 * * * /home/sina/netbox-redfish/run_daily_sync.sh
#
# Optional log rotation (keep 30 days):
#   0 1 * * * find /home/sina/netbox-redfish/logs -name "*.log" -mtime +30 -delete
set -u

# Repo root = directory containing this script (works from any checkout)
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="$BASE/logs"
mkdir -p "$LOGDIR"

STAMP="$(date +%Y-%m-%d)"
MAIN_LOG="$LOGDIR/main-$STAMP.log"
AE_LOG="$LOGDIR/ae-$STAMP.log"
LOCKFILE="/tmp/netbox-daily.lock"

# Prevent overlap if yesterday's run is somehow still going
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] another daily run is in progress — exiting" >> "$MAIN_LOG"
    exit 1
fi

cd "$BASE" || exit 1
# Activate the virtualenv if present
if [ -f "$BASE/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$BASE/.venv/bin/activate"
fi

echo "[$(date '+%F %T')] === main discovery sync started ===" | tee -a "$MAIN_LOG"
python -u sync_all_to_netbox.py >> "$MAIN_LOG" 2>&1
MAIN_RC=$?
echo "[$(date '+%F %T')] === main discovery sync finished (exit=$MAIN_RC) ===" | tee -a "$MAIN_LOG"

if [ "$MAIN_RC" -eq 0 ]; then
    echo "[$(date '+%F %T')] === AssetExplorer sync started ===" | tee -a "$AE_LOG"
    python -u sync_assetexplorer.py >> "$AE_LOG" 2>&1
    AE_RC=$?
    echo "[$(date '+%F %T')] === AssetExplorer sync finished (exit=$AE_RC) ===" | tee -a "$AE_LOG"
    exit "$AE_RC"
else
    echo "[$(date '+%F %T')] main sync failed (exit=$MAIN_RC) — AssetExplorer sync SKIPPED" | tee -a "$AE_LOG"
    exit "$MAIN_RC"
fi
