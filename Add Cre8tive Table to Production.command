#!/bin/bash
# Double-click this file in Finder to add the one new table the Cre8tive
# Score data needs (mark_hesitation) to the production database. Do this
# once, before the next "Push to Production.command". Asks for the Neon
# connection string, then a "Dry Run" / "Add Table for Real" choice. Safe to
# run more than once -- it only creates the table if it is missing and never
# changes or removes any existing data.
cd "/Users/erikholmlin/Dancesport Results Project"

DSR_DATABASE_URL=$(osascript -e 'text returned of (display dialog "Paste the Neon connection string (starts with postgresql://):" default answer "" with title "Add Cre8tive Table")' 2>/dev/null)

if [ -z "$DSR_DATABASE_URL" ]; then
  echo "Cancelled -- nothing was changed."
  echo ""
  echo "Press Return to close this window."
  read
  exit 1
fi
export DSR_DATABASE_URL

CHOICE=$(osascript -e 'button returned of (display dialog "Dry Run just reports what it would do. Add Table for Real creates the new, empty table." buttons {"Dry Run", "Add Table for Real"} default button "Dry Run" with title "Add Cre8tive Table")' 2>/dev/null)

if [ "$CHOICE" = "Add Table for Real" ]; then
  .venv/bin/python scripts/add_mark_hesitation_to_production.py
else
  .venv/bin/python scripts/add_mark_hesitation_to_production.py --dry-run
fi

echo ""
echo "Press Return to close this window."
read
