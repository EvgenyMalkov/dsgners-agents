#!/usr/bin/env bash
# Установка/переустановка на сервере. Запускать от root: bash /home/agents/dsgners-agents/deploy/install.sh
# Можно указать свой домен: OFFICE_DOMAIN=agents.dsgners.ru bash .../install.sh
set -euo pipefail
APP=/home/agents/dsgners-agents
ENVF="$APP/.env"
[ "$(id -u)" = 0 ] || { echo "Запусти от root"; exit 1; }

echo "== Пакеты системы"
apt-get update -y -q
apt-get install -y -q python3-venv fonts-inter caddy

echo "== Python-окружение"
sudo -u agents python3 -m venv "$APP/.venv"
sudo -u agents "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u agents "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== Браузер для рендера картинок"
"$APP/.venv/bin/python" -m playwright install-deps chromium >/dev/null
sudo -u agents "$APP/.venv/bin/python" -m playwright install chromium

echo "== Настройки"
if [ ! -f "$ENVF" ]; then sudo -u agents cp "$APP/.env.example" "$ENVF"; fi
chmod 600 "$ENVF"; chown agents:agents "$ENVF"
if ! grep -q '^OFFICE_TOKEN=.\+' "$ENVF"; then
  TOKEN=$(openssl rand -hex 16)
  if grep -q '^OFFICE_TOKEN=' "$ENVF"; then sed -i "s/^OFFICE_TOKEN=.*/OFFICE_TOKEN=$TOKEN/" "$ENVF"
  else printf '\nOFFICE_TOKEN=%s\nOFFICE_PORT=8080\n' "$TOKEN" >> "$ENVF"; fi
fi
TOKEN=$(grep '^OFFICE_TOKEN=' "$ENVF" | cut -d= -f2)

echo "== HTTPS для офиса"
IP=$(curl -4 -s https://api.ipify.org || hostname -I | awk '{print $1}')
DOMAIN=${OFFICE_DOMAIN:-$(cat "$APP/.office_domain" 2>/dev/null || echo "${IP//./-}.sslip.io")}
echo "$DOMAIN" > "$APP/.office_domain"; chown agents:agents "$APP/.office_domain"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
CADDY
ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
systemctl enable caddy >/dev/null; systemctl restart caddy

echo "== Сервис"
cp "$APP/deploy/dsgners-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable dsgners-bot >/dev/null
systemctl restart dsgners-bot

echo "== Автообновление с GitHub"
cp "$APP/deploy/dsgners-autoupdate.service" "$APP/deploy/dsgners-autoupdate.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dsgners-autoupdate.timer >/dev/null

echo "== Проверка рендера"
sudo -u agents bash -c "cd $APP && .venv/bin/python -m core.selftest"

cat <<MSG

==================================================================
ИИ-офис: https://$DOMAIN/?token=$TOKEN
(сохрани ссылку в закладки; первый раз сертификат выпускается ~30 сек)
==================================================================
Если ещё не вписал ключи:
  nano $ENVF   — ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, OWNER_IDS
  systemctl restart dsgners-bot
Логи: journalctl -u dsgners-bot -f
MSG
