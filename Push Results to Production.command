#!/bin/bash
# Double-click this file in Finder to sync competition results (and any
# new dancers/partnerships they reference) to production. Asks for the
# Neon connection string once, then a "Dry Run" / "Push for Real" choice
# -- Dry Run prints what would change without writing anything, safe to
# run any time; Push for Real does the actual sync (see
# scripts/sync_results_to_production.py -- upserts only, never deletes
# or truncates anything).
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Push Results to Production")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was pushed."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi
export DSR_DATABASE_URL

CHOICE=$(osascript -e 'button returned of (display dialog "Dry Run just shows what would change. Push for Real actually syncs it." buttons {"Dry Run", "Push for Real"} default button "Dry Run" with title "Push Results to Production")' 2>/dev/null)

if [ "$CHOICE" = "Push for Real" ]; then
  .venv/bin/python scripts/sync_results_to_production.py
else
  .venv/bin/python scripts/sync_results_to_production.py --dry-run
fi
STATUS=$?

if [ $STATUS -eq 0 ]; then
  osascript -e 'display notification "Finished -- see the Terminal window for details." with title "Done"' >/dev/null 2>&1
else
  osascript -e 'display notification "Something went wrong -- see the Terminal window." with title "Push failed"' >/dev/null 2>&1
fi

echo ""
echo "Press Return to close this window."
read
