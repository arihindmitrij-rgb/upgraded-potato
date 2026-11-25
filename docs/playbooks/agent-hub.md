# Agent Hub

Витрина статусов и кнопок Run для CLI-агентов на VPS. **Не** дублирует execution движок n8n для ревизора/тренера.

Детали: [`../agent-hub-deploy.md`](../agent-hub-deploy.md), [`../hub-tabs-personal.md`](../hub-tabs-personal.md), [`../hub-login.md`](../hub-login.md), [`../hub-dev-prod.md`](../hub-dev-prod.md).

## Prod vs Dev

| | Prod (VPS) | Dev (ПК) |
|---|------------|----------|
| URL | https://hub-147-45-137-104.sslip.io/ | `http://127.0.0.1:<port>` |
| Код | `/opt/agent-hub` | локальный clone |
| Роль | боевой контур рядом с n8n | песочница UI |
| Правило | без лишних рестартов n8n | можно ломать только локально |

**Источник правды:** VPS. Локальный Hub не держать постоянно «как второй прод».

## Auth

- User: `dmitry` (пароль только в store / у пользователя — не в docs).
- Caddy: `basic_auth` на `/api*`; страница `/login` + soft-gate в UI.
- Сессия: `sessionStorage.hubBasicAuth`; `fetch` с заголовком `Authorization: Basic …` (не deprecated `xhr.open` user/pass — ломает Chrome).
- n8n-хост **без** этого auth.

Smoke (пароль не печатать):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://hub-147-45-137-104.sslip.io/api/health   # 401
curl -sS -u 'dmitry:…' -o /dev/null -w '%{http_code}\n' https://hub-147-45-137-104.sslip.io/api/health  # 200
```

## Вкладки Work / Личное

Поле `meta.hubTab` в `agent.json`: `work` | `personal`. UI фильтрует; `localStorage.hubTab`.

| Вкладка | Карточки (ориентир) | Поведение |
|---------|---------------------|-----------|
| **Work** | Ревизор, WB Логист, WB-Ads | revisor/ads: read-only или analytics Run; logist — Run |
| **Личное** | Тренер | метрики из n8n Data Tables `fitness_*`, **без** Run; ссылка на WF |

Шапка: алерты (упавшие executions). Блок n8n: bot executions с VPS Postgres (`local-postgres`), не cloud MCP как единственный источник. Обогащение: `/opt/agent-hub/lib/hub_enrich.py`.

Hub **не** запускает n8n workflow. Тренер / ревизор: `readOnly` / `disableRun`.

## Паттерн: CLI + `/opt` + карточка Hub

Образец — Логист / Ads (не дублировать в n8n):

1. Код инструмента → `/opt/<name>` (venv, CLI, `output/`).
2. Секреты → `/opt/<name>/.env` (600) из store; в репо не класть.
3. Карточка → `/opt/agent-hub/agents/<id>/`:
   - `agent.json`: `projectPath`, `pythonExe`, `hubTab`, флаги `readOnly` / `asyncRun` / `planned`
   - `state.json` — метрики для UI
   - артефакты через `/api/artifact/<id>/…`
4. Run: Hub вызывает CLI (или read-only stub). n8n **не** трогаем.
5. Отдельный Telegram у инструмента — только long polling + **новый** токен бота (не webhook n8n).

### Кто Run с Hub

| id | Run с Hub | Прод-логика |
|----|-----------|-------------|
| `revisor` | нет (read-only mirror) | n8n WF «Агент-Ревизор WB» |
| `wb-logist` | да | `/opt/wb-logist` |
| `wb-ads` | да (analytics CLI) | `/opt/wb-ads` |
| `trainer` | нет | n8n «Личный тренер» |

## Runtime

- systemd: `agent-hub` (`systemctl status|restart agent-hub`)
- PowerShell 7 (`pwsh`) + `hub-server.ps1`
- Порт `8787` наружу закрыт; снаружи только HTTPS через Caddy

```bash
systemctl status agent-hub
journalctl -u agent-hub -f
curl -sS -u 'dmitry:…' https://hub-147-45-137-104.sslip.io/api/health
```
