#!/usr/bin/env bash
# Установка/переустановка на сервере. Запускать от root: bash /home/agents/dsgners-agents/deploy/install.sh
set -euo pipefail
APP=/home/agents/dsgners-agents
[ "$(id -u)" = 0 ] || { echo "Запусти от root"; exit 1; }

echo "== Пакеты системы"
apt-get update -y -q
apt-get install -y -q python3-venv fonts-inter

echo "== Python-окружение"
sudo -u agents python3 -m venv "$APP/.venv"
sudo -u agents "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u agents "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== Браузер для рендера картинок"
"$APP/.venv/bin/python" -m playwright install-deps chromium >/dev/null
sudo -u agents "$APP/.venv/bin/python" -m playwright install chromium

if [ ! -f "$APP/.env" ]; then
  sudo -u agents cp "$APP/.env.example" "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== Сервис бота"
cp "$APP/deploy/dsgners-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable dsgners-bot >/dev/null

echo "== Проверка рендера"
sudo -u agents bash -c "cd $APP && .venv/bin/python -m core.selftest"

cat <<MSG

Готово. Осталось:
  1) nano $APP/.env   — впиши ANTHROPIC_API_KEY и TELEGRAM_BOT_TOKEN, сохрани (Ctrl+O, Enter, Ctrl+X)
  2) systemctl restart dsgners-bot
  3) напиши боту /start — он покажет твой ID, впиши его в OWNER_IDS в .env и снова: systemctl restart dsgners-bot
Логи: journalctl -u dsgners-bot -f
MSG
