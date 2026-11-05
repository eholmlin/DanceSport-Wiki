#!/bin/bash
# Runs scripts/bulk_load_ndca_heatlists.py against the local SQLite DB only
# -- see launchd/com.erikholmlin.dsr.heatlist-refresh.plist for the schedule
# (6am, then hourly until 9pm and at login, so a missed or failed run is
# caught up; daily_refresh_runner.sh makes the extra triggers no-ops once the
# day's run has succeeded, and handles waiting for the network and retries).
# Deliberately does NOT sync to production; that's a separate, manual step
# (the "Push to Production.command" launcher) so the production DB credential
# never has to live in a file a background job reads unattended.
set -euo pipefail
exec "/Users/erikholmlin/Dancesport Results Project/scripts/daily_refresh_runner.sh" heatlist \
  "/Users/erikholmlin/Dancesport Results Project/.venv/bin/python" \
  "/Users/erikholmlin/Dancesport Results Project/scripts/bulk_load_ndca_heatlists.py"
