#!/bin/bash
# Shared runner for the daily local refreshes (see daily_heatlist_refresh.sh
# and daily_results_refresh.sh). Usage:
#
#   daily_refresh_runner.sh <job-name> <command> [args...]
#
# What it adds over just running the command from launchd at 6am:
#
#  - Once a day: a successful run writes today's date to
#    logs/.last-success-<job>. Any later trigger the same day exits at once,
#    so the job can be (and is) scheduled many times a day -- hourly from 6am
#    until evening, and at login -- and the first trigger that finds the Mac
#    awake and online does the work. This is the catch-up: a Mac that was
#    asleep or off at 6am (launchd does not fire a missed slot for a machine
#    that was powered off, and a sleeping one can wake with no network yet)
#    simply runs it at the next trigger instead of skipping the day.
#  - Waits for the network before starting (the 6am-wake failure was a DNS
#    error with the Wi-Fi not up yet).
#  - Retries a failed run, after a pause.
#  - Keeps the Mac from idle-sleeping mid-run (caffeinate).
#
# Set DSR_FORCE=1 to run even if today's marker exists (a manual re-run).
#
# Test/override knobs: DSR_MARKER_DIR, DSR_NETWORK_CHECK_URL,
# DSR_NETWORK_WAIT_SECONDS, DSR_RETRY_PAUSE_SECONDS, DSR_MAX_ATTEMPTS.
set -uo pipefail

JOB="${1:?usage: daily_refresh_runner.sh <job-name> <command> [args...]}"
shift
[ "$#" -ge 1 ] || { echo "usage: daily_refresh_runner.sh <job-name> <command> [args...]" >&2; exit 2; }

PROJECT="/Users/erikholmlin/Dancesport Results Project"
cd "$PROJECT" || exit 1

MARKER_DIR="${DSR_MARKER_DIR:-$PROJECT/logs}"
MARKER="$MARKER_DIR/.last-success-$JOB"
CHECK_URL="${DSR_NETWORK_CHECK_URL:-https://ndcapremier.com/}"
NETWORK_WAIT="${DSR_NETWORK_WAIT_SECONDS:-900}"
RETRY_PAUSE="${DSR_RETRY_PAUSE_SECONDS:-300}"
MAX_ATTEMPTS="${DSR_MAX_ATTEMPTS:-3}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $JOB: $*"; }

TODAY="$(date +%Y-%m-%d)"
if [ -z "${DSR_FORCE:-}" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
  log "already succeeded today, nothing to do"
  exit 0
fi

network_up() {
  # Any HTTP response at all (even an error page) means DNS and the network
  # work; curl reports 000 only when it could not connect.
  local code
  code="$(curl -s -o /dev/null --head --max-time 15 -w '%{http_code}' "$CHECK_URL" 2>/dev/null)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

waited=0
until network_up; do
  if [ "$waited" -ge "$NETWORK_WAIT" ]; then
    log "network still down after ${waited}s; giving up for now (the next scheduled trigger will retry)"
    exit 1
  fi
  [ "$waited" -eq 0 ] && log "waiting for the network..."
  sleep 20
  waited=$((waited + 20))
done
[ "$waited" -gt 0 ] && log "network is up after ${waited}s"

attempt=1
while true; do
  log "starting (attempt $attempt of $MAX_ATTEMPTS): $*"
  caffeinate -i "$@"
  status=$?
  if [ "$status" -eq 0 ]; then
    mkdir -p "$MARKER_DIR"
    echo "$TODAY" > "$MARKER"
    log "finished successfully"
    exit 0
  fi
  log "failed with exit status $status"
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    log "out of attempts; the next scheduled trigger will try again"
    exit "$status"
  fi
  attempt=$((attempt + 1))
  log "retrying in ${RETRY_PAUSE}s"
  sleep "$RETRY_PAUSE"
done
