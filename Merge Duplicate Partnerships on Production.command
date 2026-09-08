#!/bin/bash
# Double-click this file in Finder to merge duplicate partnerships (a
# couple split across two Partnership rows because their leader/follower
# order was recorded differently across sources -- see
# scripts/merge_duplicate_partnerships.py) on the production database.
# Asks for the Neon connection string once, then a "Dry Run" / "Merge for
# Real" choice -- Dry Run just lists what would be merged, safe to run any
# time; Merge for Real moves entries/scheduled heats onto the surviving
# partnership and deletes the redundant one. Safe to run more than once --
# it finds nothing left to merge once it's already clean.
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Merge Duplicate Partnerships")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was changed."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi
export DSR_DATABASE_URL

CHOICE=$(osascript -e 'button returned of (display dialog "Dry Run just lists what would be merged. Merge for Real actually does it." buttons {"Dry Run", "Merge for Real"} default button "Dry Run" with title "Merge Duplicate Partnerships")' 2>/dev/null)

if [ "$CHOICE" = "Merge for Real" ]; then
  .venv/bin/python scripts/merge_duplicate_partnerships.py
else
  .venv/bin/python scripts/merge_duplicate_partnerships.py --dry-run
fi
STATUS=$?

if [ $STATUS -eq 0 ]; then
  osascript -e 'display notification "Finished -- see the Terminal window for details." with title "Done"' >/dev/null 2>&1
else
  osascript -e 'display notification "Something went wrong -- see the Terminal window." with title "Merge failed"' >/dev/null 2>&1
fi

echo ""
echo "Press Return to close this window."
read
