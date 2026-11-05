# Общие функции для скриптов деплоя (подключается через source)
APP=/home/agents/dsgners-agents
G="sudo -u agents git -C $APP"
PY="sudo -u agents $APP/.venv/bin/python"
STATE=$APP/data/deploy
mkdir -p "$STATE"; chown -R agents:agents "$APP/data"
envget(){ grep "^$1=" "$APP/.env" 2>/dev/null | head -1 | cut -d= -f2-; }

notify(){  # сообщение владельцам в Telegram
  local tok ids; tok=$(envget TELEGRAM_BOT_TOKEN); ids=$(envget OWNER_IDS)
  [ -z "$tok" ] && return 0
  for id in ${ids//,/ }; do
    curl -s -m 10 "https://api.telegram.org/bot$tok/sendMessage" --data-urlencode "chat_id=$id" --data-urlencode "text=$1" >/dev/null || true
  done
}

deps(){ sudo -u agents "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"; sysdeps; }
sysdeps(){  # системные программы, которые понадобились позже (ffmpeg — для роликов дизайнера)
  command -v ffmpeg >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg >/dev/null 2>&1 || true
}

install_units(){
  cp "$APP/deploy/dsgners-bot.service" "$APP/deploy/dsgners-autoupdate.service" "$APP/deploy/dsgners-autoupdate.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now dsgners-autoupdate.timer >/dev/null
}

healthy(){  # бот запущен и офис отвечает
  local port; port=$(envget OFFICE_PORT); port=${port:-8080}
  sleep 12
  systemctl is-active -q dsgners-bot || return 1
  [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/state")" = "401" ]
}
