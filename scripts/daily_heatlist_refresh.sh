#!/bin/bash
# Runs scripts/bulk_load_ndca_heatlists.py against the local SQLite DB only
# -- see com.erikholmlin.dsr.heatlist-refresh.plist for the daily 6am
# schedule (launchd). Deliberately does NOT sync to production; that's a
# separate, manual step (scripts/migrate_sqlite_to_postgres.py --table
# scheduled_heat) so the production DB credential never has to live in a
# file a background job reads unattended.
set -euo pipefail
cd "/Users/erikholmlin/Dancesport Results Project"
exec .venv/bin/python scripts/bulk_load_ndca_heatlists.py
