#!/usr/bin/env bash
# Keep the DRY-TESTING backtest dashboard running at 0.0.0.0:8099.
# DRY / RESEARCH ONLY — never trades live capital.
#
# This wrapper auto-restarts the server if it ever exits, so it "keeps on running"
# even without pm2. For a managed setup use pm2 instead:
#   pm2 start run_dashboard.sh --name bt-dashboard
#
# Usage:  ./run_dashboard.sh [--port 8099] [--interval 600] [--seeds 5]
set -u
cd "$(dirname "$0")"
PY=/home/chengyou1368/polymarket-bot/kalshi/.venv-cli-trader/bin/python
LOG=/tmp/bt_dashboard.log
while true; do
  echo "[$(date -u +%H:%M:%S)] starting dashboard_server.py $*" | tee -a "$LOG"
  "$PY" dashboard_server.py "$@" >> "$LOG" 2>&1
  code=$?
  echo "[$(date -u +%H:%M:%S)] exited (code $code) — restarting in 5s" | tee -a "$LOG"
  sleep 5
done
