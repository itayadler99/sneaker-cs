#!/bin/bash
# launchd entry for the outcome sentinel. Hourly; silent unless something is wrong.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a
/usr/bin/python3 route-b/sentinel.py >> route-b/sentinel.log 2>&1
