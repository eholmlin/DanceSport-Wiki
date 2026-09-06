#!/bin/bash
# Double-click this file in Finder to push today's local heat-list updates
# to the production site. Asks for the Neon connection string once (paste
# into the popup box), then does everything else automatically -- including
# fixing any new dancers/partnerships the sync would otherwise fail on (see
# scripts/sync_missing_scheduled_heat_deps.py for why that step exists).
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Push Heat Lists to Production")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was pushed."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi

export DSR_DATABASE_URL
.venv/bin/python scripts/sync_missing_scheduled_heat_deps.py
STATUS=$?

if [ $STATUS -eq 0 ]; then
  osascript -e 'display notification "Heat lists are live on production." with title "Done"' >/dev/null 2>&1
else
  osascript -e 'display notification "Something went wrong -- see the Terminal window." with title "Push failed"' >/dev/null 2>&1
fi

echo ""
echo "Press Return to close this window."
read
