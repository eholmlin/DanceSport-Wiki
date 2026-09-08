#!/bin/bash
# Double-click this file in Finder to push both today's heat-list updates
# and any new/updated results to the production site, in one go. Asks for
# the Neon connection string once, then runs both syncs back to back --
# replaces double-clicking "Push Heat Lists to Production.command" and
# "Push Results to Production.command" separately (per user request, to
# stop re-pasting the same connection string twice a day). Both steps
# still run even if one of them fails, since they touch different tables
# (scheduled_heat vs. comp_event/entry/round/result/mark) and a bad heat-
# list refresh shouldn't hold back an already-good results push, or vice
# versa.
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Push to Production")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was pushed."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi
export DSR_DATABASE_URL

echo "=== Heat lists ==="
.venv/bin/python scripts/sync_missing_scheduled_heat_deps.py
HEATLIST_STATUS=$?

echo ""
echo "=== Results ==="
CHOICE=$(osascript -e 'button returned of (display dialog "Dry Run just shows what would change. Push for Real actually syncs it." buttons {"Dry Run", "Push for Real"} default button "Dry Run" with title "Push Results to Production")' 2>/dev/null)

if [ "$CHOICE" = "Push for Real" ]; then
  .venv/bin/python scripts/sync_results_to_production.py
else
  .venv/bin/python scripts/sync_results_to_production.py --dry-run
fi
RESULTS_STATUS=$?

if [ "$HEATLIST_STATUS" -eq 0 ] && [ "$RESULTS_STATUS" -eq 0 ]; then
  osascript -e 'display notification "Heat lists and results are live on production." with title "Done"' >/dev/null 2>&1
else
  osascript -e 'display notification "Something went wrong -- see the Terminal window." with title "Push failed"' >/dev/null 2>&1
fi

echo ""
echo "Press Return to close this window."
read
