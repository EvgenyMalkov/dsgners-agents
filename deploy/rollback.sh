#!/usr/bin/env bash
# Вернуть предыдущую версию. От root или через /rollback в боте.
set -uo pipefail
source /home/agents/dsgners-agents/deploy/_lib.sh

[ -s "$STATE/history" ] || { notify "Откатывать некуда — это первая установленная версия."; exit 1; }
CUR=$($G rev-parse HEAD)
TARGET=$(tail -1 "$STATE/history")
sed -i '$d' "$STATE/history"
# текущую версию помечаем как плохую, чтобы автообновление её не вернуло
echo "$CUR" > "$STATE/bad"
$G reset -q --hard "$TARGET"; deps; install_units; systemctl restart dsgners-bot
if healthy; then
  notify "↩️ Откатил на $($G log -1 --format='%h %s' HEAD). Следующее обновление поставится, когда я залью новую версию."
else
  notify "⚠️ Откатил, но бот не отвечает — посмотри: journalctl -u dsgners-bot -n 50"
fi
