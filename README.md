# dsgners-agents — ИИ-команда dsgners

Дизайнер (готов), SMM-щик и SEO-шник (следующие). Работают на VPS, задачи и согласование — через Telegram-бота.

```
team/            общий мозг: бренд, правила, дизайн-система — читают все агенты
agents/designer  роль, чек-лист, утверждённые правила, шаблоны визуалов, инструменты
core/            движок агентов, рендер HTML→PNG с автопроверкой, учёт расходов, правила из правок
bot.py           Telegram-бот: задача → агент → картинки → «✅ Ок / 🔁 Переделать»
deploy/          установка и обновление на сервере
```

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
```bash
bash /home/agents/dsgners-agents/deploy/update.sh
```
Логи: `journalctl -u dsgners-bot -f`

## Добавить нового агента
Папка `agents/<имя>/` с `ROLE.md`, `checklist.md`, `feedback.md`, `tools.py` (TOOLS + handle) — движок общий.
