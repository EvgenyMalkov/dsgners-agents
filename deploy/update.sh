#!/usr/bin/env bash
# Обновить код с GitHub и перезапустить. От root: bash /home/agents/dsgners-agents/deploy/update.sh
set -euo pipefail
APP=/home/agents/dsgners-agents
sudo -u agents git -C "$APP" pull --ff-only
# новые системные части (офис, https) ставятся установщиком — он безопасен для повторного запуска
if [ ! -f /etc/caddy/Caddyfile ] || ! grep -q '^OFFICE_TOKEN=.\+' "$APP/.env"; then
  bash "$APP/deploy/install.sh"; exit 0
fi
sudo -u agents "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
cp "$APP/deploy/dsgners-bot.service" /etc/systemd/system/ && systemctl daemon-reload
systemctl restart dsgners-bot
echo "Обновлено: $(sudo -u agents git -C "$APP" log -1 --format='%h %s')"
echo "Офис: https://$(cat $APP/.office_domain)/?token=$(grep '^OFFICE_TOKEN=' $APP/.env | cut -d= -f2)"
