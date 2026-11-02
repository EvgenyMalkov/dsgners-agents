#!/usr/bin/env bash
# Обновить код с GitHub и перезапустить бота. От root: bash /home/agents/dsgners-agents/deploy/update.sh
set -euo pipefail
APP=/home/agents/dsgners-agents
sudo -u agents git -C "$APP" pull --ff-only
sudo -u agents "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
systemctl restart dsgners-bot
echo "Обновлено: $(sudo -u agents git -C "$APP" log -1 --format='%h %s')"
