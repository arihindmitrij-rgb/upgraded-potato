# Как вынести стек в GitHub / Origin

Чеклист на момент, когда появится URL репозитория (сейчас playbook живёт в Project Context). Цель: код и docs без секретов, воспроизводимый деплой на `/opt`.

## Когда делать

- Есть URL пустого (или целевого) репо на GitHub / Cursor Origin.
- Понятно, **что** выносим: monorepo (`agent-hub` + `wb-*`) или отдельные репо на инструмент.
- Prod на VPS уже стабилен — git = страховка и откат, не «переезд ради переезда».

## Что класть в репо

- Исходники без `.venv`, `.env`, `output/`, `out/`, `__pycache__`, крупных raw JSON со статистикой.
- `README` + этот playbook (или симлинк/копия `docs/playbooks/`).
- Примеры env: `.env.example` с **пустыми** ключами / комментариями имён.
- `agent.json` шаблоны с placeholder-путями Linux (`/opt/...`), без токенов.
- Unit-файлы systemd / Caddy-фрагменты **без** паролей (htpasswd — генерить на сервере).

Ориентир упаковки: каталоги `internal/wb-*-src/`, `internal/agent-hub-src/` в store — уже «почти пакет без секретов».

## Что не класть (чеклист перед push)

- [ ] Нет файлов `secrets/`, `.env`, `*.pem`, `creds.json`
- [ ] Нет JWT/Bearer в workflow JSON, HTML, markdown, скриншотах
- [ ] Нет пароля Hub Basic auth и содержимого `hub-basic-auth-*.txt`
- [ ] Нет SSH-ключей и n8n MCP-токенов из user store
- [ ] В истории git нет случайно закоммиченных секретов (`git log -p` / scan); если были — ротация ключей, не только `git rm`
- [ ] `.gitignore`: `.env`, `.venv/`, `output/`, `out/`, `secrets/`, `*.xlsx` с боевыми выгрузками по вкусу

## Порядок выноса

1. **Выбрать структуру репо** (mono vs multi) и ветку `main`.
2. **Собрать дерево** из `/opt/<name>` или `internal/*-src` → рабочая копия без мусора.
3. **Добавить** `.env.example`, README «как задеплоить на VPS», ссылку на playbook.
4. **Локальный scan** на паттерны токенов (`sk-`, `eyJ`, `Bearer `, `api.telegram.org/bot`).
5. **Создать remote** (URL от пользователя) → `git remote add` → первый push.
6. **Не менять** prod автоматически из CI, пока нет явного pipeline; деплой вручную по [`../hub-dev-prod.md`](../hub-dev-prod.md):
   - пакет без venv/env → `/opt/<name>`
   - venv + `.env` из store
   - поправить `projectPath` / `pythonExe` в Hub `agent.json`
   - smoke; **n8n не рестартить** без нужды; webhook не трогать

## После появления URL

Записать в [`../project-context.md`](../project-context.md) (или сюда): URL репо, что именно в нём (hub / revisor / logist / ads), ветка деплоя. Обновить этот файл одной строкой «Remote: …».

**Remote:** https://github.com/arihindmitrij-rgb/upgraded-potato.git — в репо пока только `docs/playbooks/` (каркас без секретов); исходники hub / revisor / logist / ads — следующий вынос.

## Связанные правила

- Секреты: [secrets-policy.md](./secrets-policy.md)
- Hard constraints VPS: [vps-stack.md](./vps-stack.md)
- Паттерн агентов: [agent-hub.md](./agent-hub.md)
