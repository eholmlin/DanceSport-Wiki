#!/bin/bash
# Runs scripts/refresh_ndca.py against the local SQLite DB only -- see
# com.erikholmlin.dsr.results-refresh.plist for the daily 6:15am schedule
# (launchd, right after the 6am heat-list refresh). Deliberately does NOT
# sync to production; that's a separate, manual step (the "Push Heat Lists
# to Production.command" launcher, or migrate_sqlite_to_postgres.py
# directly) so the production DB credential never has to live in a file a
# background job reads unattended.
set -euo pipefail
cd "/Users/erikholmlin/Dancesport Results Project"
exec .venv/bin/python scripts/refresh_ndca.py
