#!/bin/bash
# Double-click this file in Finder to check that the production database
# has exactly the same number of competitions' events, entries, rounds,
# results, marks and hesitation flags as the local database, competition by
# competition (scripts/verify_production_sync.py). Read-only -- it changes
# nothing. Asks for the Neon connection string once.
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Verify Production")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was checked."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi
export DSR_DATABASE_URL

.venv/bin/python scripts/verify_production_sync.py

echo ""
echo "Press Return to close this window."
read
