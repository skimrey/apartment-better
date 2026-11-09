#!/bin/zsh
# Overnight loop. Chrome: Apartments.com details (restarted if it dies). P40, one request at a time:
# judge newly detailed buildings, re-rank picks (sets photo priority), describe the next batch of photos.
cd "$(dirname "$0")"
PY=./.venv/bin/python
DB=data/apartment_better.db
while true; do
  left=$(sqlite3 $DB "select count(*) from listings where source='apartments' and detail_at is null")
  if [ "$left" -gt 0 ] && ! pgrep -qf "cli.py details"; then
    caffeinate -i $PY -u cli.py details --only apartments >> data/rest.out 2>&1 &
  fi
  $PY -u cli.py floors
  $PY -u cli.py character
  $PY -u cli.py picks
  $PY -u cli.py classify --images 300
  todo=$(sqlite3 $DB "select count(*) from images where status in ('new','fetched','working') or (status='error' and attempts<3)")
  if [ "$left" -eq 0 ] && [ "$todo" -eq 0 ]; then break; fi
  sleep 60
done
$PY -u cli.py character --redo-unknown
$PY -u cli.py picks
touch data/OVERNIGHT_DONE
