# dsgners-agents — ИИ-команда dsgners

Графический дизайнер (готов); веб-дизайнер, SMM-щик и SEO-шник (следующие). Работают на VPS, задачи и согласование — через Telegram-бота.

```
team/            общий мозг: бренд, правила, дизайн-система — читают все агенты
agents/designer  графический дизайнер: роль, чек-лист, утверждённые правила, шаблоны визуалов, инструменты
core/            движок агентов, рендер HTML→PNG с автопроверкой, учёт расходов, правила из правок
bot.py           главный процесс: Telegram-бот + ИИ-офис
web/office.html  ИИ-офис: сцена, лента, история задач, знания агента, задачи
core/web.py      API офиса; core/runner.py — запуск задач; core/knowledge.py — знания
deploy/          установка и обновление на сервере
```

## ИИ-офис
Ссылка выдаётся в конце установки и по команде `/office` в боте. Вкладки у агента:
- **Лента** — что делает прямо сейчас, по шагам;
- **История** — все задачи с картинками, «Принять / Переделать», цепочки переделок;
- **Знания** — роль, чек-лист, правила (правятся прямо там), референсы «хорошо/плохо», документы .md/.txt/.pdf.

Правки из офиса хранятся в `data/` на сервере и главнее версий из репозитория.

## Как дизайнер учится
«🔁 Переделать» → пишешь, что не так → это сохраняется в `data/feedback/designer.md`
и подмешивается во все следующие задачи. Удачные правила раз в неделю переносим
в `agents/designer/feedback.md` (в git). Посмотреть правила: `/rules` в боте.

## Установка на сервер (один раз, от root)
```bash
# 1. ключ доступа сервера к репозиторию
sudo -u agents mkdir -p /home/agents/.ssh
sudo -u agents ssh-keygen -t ed25519 -N "" -f /home/agents/.ssh/id_ed25519 -C dsgners-vps
cat /home/agents/.ssh/id_ed25519.pub
#    → GitHub: репо → Settings → Deploy keys → Add deploy key, вставить, Write не нужен

# 2. скачать код и установить
sudo -u agents GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:EvgenyMalkov/dsgners-agents.git /home/agents/dsgners-agents
bash /home/agents/dsgners-agents/deploy/install.sh
```

## Обновление
Автоматически: сервер раз в минуту проверяет GitHub и сам обновляется (не во время задач).
Журнал: `tail /var/log/dsgners-autoupdate.log`. Вручную, если нужно срочно:
```bash
bash /home/agents/dsgners-agents/deploy/update.sh
```
Логи: `journalctl -u dsgners-bot -f`

## Добавить нового агента
Папка `agents/<имя>/` с `ROLE.md`, `checklist.md`, `feedback.md`, `tools.py` (TOOLS + handle) — движок общий.
