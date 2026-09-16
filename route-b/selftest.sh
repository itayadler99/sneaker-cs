#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a
/usr/bin/python3 route-b/selftest.py >> route-b/selftest.log 2>&1
