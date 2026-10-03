#!/usr/bin/env bash
# Daily job: pull, scrape tvtv.us, commit latest.json + last-run.json, push.
# Cron calls this; safe to run by hand too. Exit codes: 0 ok, 1 scrape
# failed, 3 blocked by Cloudflare, 4 git failure.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p logs
exec 9>logs/.run.lock
flock -n 9 || { echo "$(date '+%F %T') another run is in progress; exiting"; exit 0; }

echo "$(date '+%F %T') ---- run.sh start ----"
git pull --ff-only --quiet || { echo "git pull failed"; exit 4; }

PY="${PYTHON:-.venv/bin/python}"
"$PY" scrape.py
rc=$?
if [ $rc -ne 0 ]; then
    echo "$(date '+%F %T') scrape.py exited $rc; nothing committed"
    exit $rc
fi

git add latest.json last-run.json
if git diff --cached --quiet; then
    echo "$(date '+%F %T') no changes to commit"
else
    git commit --quiet -m "TV listings scrape $(TZ=America/Los_Angeles date +%F)" || exit 4
fi
git push --quiet origin HEAD || { echo "git push failed"; exit 4; }
echo "$(date '+%F %T') pushed $(git rev-parse --short HEAD)"
