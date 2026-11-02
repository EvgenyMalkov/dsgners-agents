#!/usr/bin/env bash
# Запускается таймером раз в минуту (от root): если на GitHub есть новое — обновляет и перезапускает.
# Не перезапускает, пока какой-то агент выполняет задачу.
set -uo pipefail
APP=/home/agents/dsgners-agents
LOG=/var/log/dsgners-autoupdate.log
G="sudo -u agents git -C $APP"

$G fetch -q origin main 2>>"$LOG" || exit 0
[ "$($G rev-parse HEAD)" = "$($G rev-parse origin/main)" ] && exit 0

DB="$APP/data/office.db"
if [ -f "$DB" ]; then
  BUSY=$(sudo -u agents "$APP/.venv/bin/python" -c "import sqlite3;print(sqlite3.connect('$DB').execute(\"select count(*) from jobs where status in ('queued','running')\").fetchone()[0])" 2>/dev/null || echo 0)
  [ "${BUSY:-0}" != "0" ] && exit 0
fi

echo "$(date '+%F %T') обновляю до $($G log -1 --format='%h %s' origin/main)" >> "$LOG"
bash "$APP/deploy/update.sh" >> "$LOG" 2>&1
