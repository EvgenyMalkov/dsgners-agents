"""Вход в аккаунт PR/sales-менеджера — один раз, на сервере, от пользователя agents:

    sudo -u agents /home/agents/dsgners-agents/.venv/bin/python -m core.pr.login
    systemctl restart dsgners-bot

Спросит номер телефона, код из Telegram и облачный пароль (если стоит). Код и пароль никому не пересылай.
Сессия ляжет в data/<TG_SESSION>.session — это полный доступ к аккаунту, файл не попадает в GitHub.
"""
import os
import sys

from core import config
from core.pr.sender import API_HASH, API_ID, SESSION


def main() -> int:
    if not API_ID or not API_HASH:
        print("В .env нет TG_API_ID или TG_API_HASH.")
        return 1
    from telethon.sync import TelegramClient
    print("Вход в аккаунт менеджера. Номер — в международном формате, например +79991234567.")
    with TelegramClient(str(SESSION), API_ID, API_HASH, device_model="dsgners-agents", system_version="VPS") as c:
        me = c.get_me()
        print(f"Вошли как: {me.first_name or ''} @{me.username or '—'}")
    path = SESSION.with_suffix(".session")
    os.chmod(path, 0o600)
    print(f"Сессия: {path}\nТеперь перезапусти бота: systemctl restart dsgners-bot")
    if not config.OWNER_IDS:
        print("Внимание: в .env пустой OWNER_IDS — ответы авторов некому пересылать.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
