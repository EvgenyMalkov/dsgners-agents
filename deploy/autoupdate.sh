#!/usr/bin/env bash
# Таймер раз в минуту (от root): откат по запросу из бота, иначе — обновление, если есть новое.
# Не трогает сервер, пока агент выполняет задачу.
set -uo pipefail
source /home/agents/dsgners-agents/deploy/_lib.sh
LOG=/var/log/dsgners-autoupdate.log

busy(){
  local db="$APP/data/office.db"
  [ -f "$db" ] || return 1
  local n; n=$($PY -c "import sqlite3;print(sqlite3.connect('$db').execute(\"select count(*) from jobs where status in ('queued','running')\").fetchone()[0])" 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

if [ -f "$STATE/rollback_request" ]; then
  busy && exit 0
  rm -f "$STATE/rollback_request"
  echo "$(date '+%F %T') откат по запросу" >> "$LOG"
  bash "$APP/deploy/rollback.sh" >> "$LOG" 2>&1
  exit 0
fi

$G fetch -q origin main 2>>"$LOG" || exit 0
NEW=$($G rev-parse origin/main)
[ "$NEW" = "$($G rev-parse HEAD)" ] && exit 0
[ "$NEW" = "$(cat "$STATE/bad" 2>/dev/null)" ] && exit 0
busy && exit 0

echo "$(date '+%F %T') обновляю до $($G log -1 --format='%h %s' "$NEW")" >> "$LOG"
bash "$APP/deploy/update.sh" >> "$LOG" 2>&1
