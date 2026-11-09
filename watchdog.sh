#!/bin/zsh
# Refresh picks every 20 min; restart overnight.sh if it died before finishing.
cd "$(dirname "$0")"
while [ ! -f data/OVERNIGHT_DONE ]; do
  sleep 1200
  ./.venv/bin/python cli.py picks >> data/watchdog.log 2>&1
  if ! pgrep -qf "overnight.sh"; then echo "$(date +%H:%M) restarting overnight.sh" >> data/watchdog.log; caffeinate -i ./overnight.sh >> data/overnight.log 2>&1 & fi
done
