# VPS-стек

Практическая карта боевого сервера. Детали hardening и бэкапов: [`../stack-hardening-progress.md`](../stack-hardening-progress.md). Аудит рисков: [`../stack-efficiency-audit.md`](../stack-efficiency-audit.md).

## Хост и сервисы

| Что | Где |
|-----|-----|
| VPS | `147.45.137.104` |
| n8n UI | https://147-45-137-104.sslip.io → `/opt/n8n` (Docker + Caddy) |
| Agent Hub | https://hub-147-45-137-104.sslip.io/ → `/opt/agent-hub`, systemd `agent-hub`, listen `:8787` (снаружи UFW deny) |
| Postgres n8n | контейнер рядом с n8n; Hub читает executions/fitness **read-only** (`local-postgres`) |
| Инструменты | `/opt/wb-revisor`, `/opt/wb-logist`, `/opt/wb-ads` |
| Бэкапы | `/var/backups/agent-stack/` (cron ~03:15 UTC, 14 дней); офлайн-экспорты n8n → `/var/backups/n8n-offline-exports/` (`0700`) |

В `/opt/n8n` держать минимум: `.env`, `Caddyfile`, `docker-compose.yml`. Массовые экспорты/creds JSON — не рядом с compose.

## Caddy

- Отдельный хост для Hub (`hub-…sslip.io`) → `host.docker.internal:8787`. **Не** вешать Hub path на тот же хост, что n8n (безопаснее для webhook/UI n8n).
- Basic auth на Hub: только `/api*` (user `dmitry`); UI и `/login` публичные. См. [`../hub-login.md`](../hub-login.md).
- Хост n8n — **без** Basic auth (пока так решено).

## n8n

- Источник правды для Telegram-ботов (Виталик, тренер, WB Agent) и workflow «Агент-Ревизор WB».
- Credentials: имена и статус — [`../n8n-credentials-status.md`](../n8n-credentials-status.md) (значения не копировать).
- Активные WF (ориентир): Первое детище, Личный тренер, Агент-Ревизор WB, ФБС Стукач, Парсер Штрафов.
- OpenRouter для голоса — через credential **OpenRouter**, не inline в ноде.

## Hard constraints (обязательно)

1. **Не ломать n8n** — без лишних рестартов контейнера; правки credentials/WF через UI/import, не «почистить volume».
2. **Один Telegram webhook на весь сервер** — webhook только у ботов n8n. Другие боты (например logist) — **long polling**, свой токен, webhook пустой.
3. **Не трогать webhook** при работах над Hub/Caddy/opt-CLI, если задача этого не требует явно.
4. **Windows self-hosted worker** — только файлы на ПК; VPS/SSH — через облачный агент миграции.
5. **Домен** — sslip.io до своего домена; сертификаты/имена менять осознанно.

## Бэкап и операционка

- Скрипт: `/usr/local/sbin/backup-agent-stack.sh` → `/var/backups/agent-stack/`.
- Бэкапить: Postgres n8n, sqlite/артефакты инструментов, конфиги `/opt/*/`.
- Раз в месяц — чистить старые executions/логи/артефакты (диск маленький).
- После изменений на VPS — smoke: n8n 200, Hub health (с auth), `docker` Up, webhook не трогали.

## Кто что правит

| Действие | Где |
|----------|-----|
| Workflow / TG credentials | только n8n на VPS |
| UI Hub / Run CLI-агентов | `/opt/agent-hub` + `/opt/wb-*` |
| Секреты на диск | `/opt/*/.env` (600) из Project/user store |
| Эксперименты UI | ПК Hub (dev), не путать с prod — [`../hub-dev-prod.md`](../hub-dev-prod.md) |
