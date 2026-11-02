#!/usr/bin/env bash
# Обновить до новой версии с GitHub с проверкой и автооткатом. От root:
#   bash /home/agents/dsgners-agents/deploy/update.sh
set -uo pipefail
source /home/agents/dsgners-agents/deploy/_lib.sh

$G fetch -q origin main || { echo "Нет связи с GitHub"; exit 1; }
TARGET=$($G rev-parse origin/main)
PREV=$($G rev-parse HEAD)
[ "$PREV" = "$TARGET" ] && { echo "Уже последняя версия"; exit 0; }
MSG=$($G log -1 --format='%h %s' "$TARGET")

# первая установка системных частей (офис, https, таймер)
if [ ! -f /etc/caddy/Caddyfile ] || [ -z "$(envget OFFICE_TOKEN)" ]; then
  $G reset -q --hard "$TARGET"; bash "$APP/deploy/install.sh"; exit $?
fi

back(){  # вернуть прежнюю версию
  echo "$TARGET" > "$STATE/bad"
  $G reset -q --hard "$PREV"; deps; install_units; systemctl restart dsgners-bot
  notify "⚠️ Обновление «$MSG» $1 — оставил прежнюю версию, всё работает как раньше."
  echo "Откат: $1"; exit 1
}

echo "Проверяю $MSG"
$G reset -q --hard "$TARGET"
deps || back "не поставило зависимости"
(cd "$APP" && $PY -m core.selftest) || back "не прошло проверку"

install_units
systemctl restart dsgners-bot
healthy || back "не запустилось"

echo "$PREV" >> "$STATE/history"
rm -f "$STATE/bad"
notify "✅ Обновлено: $MSG"
echo "Обновлено: $MSG"
echo "Офис: https://$(cat "$APP/.office_domain" 2>/dev/null)/?token=$(envget OFFICE_TOKEN)"
