# Playbook стека Дмитрия

Каркас для новых Cursor Projects и агентов вокруг одного VPS: n8n, Agent Hub, WB-инструменты. **Без секретов и токенов** — только имена, пути и правила.

Дата каркаса: 2026-11-25 (Europe/Moscow). Живые детали и smoke — в соседних docs; этот раздел — «как устроено и что нельзя ломать».

## Оглавление

| Файл | О чём |
|------|--------|
| [vps-stack.md](./vps-stack.md) | VPS, Caddy, n8n, Postgres, бэкапы, hard constraints |
| [agent-hub.md](./agent-hub.md) | Hub, auth, вкладки Work/Личное, паттерн CLI → `/opt` |
| [wb-tools.md](./wb-tools.md) | revisor / logist / ads: где код, кто прод, analytics-only для ads |
| [secrets-policy.md](./secrets-policy.md) | Где секреты, что не класть в git/чат |
| [how-to-export-to-git.md](./how-to-export-to-git.md) | Чеклист выноса в GitHub/Origin, когда будет URL |

## Как пользоваться в новом Project

1. **Скопируй или прилинкуй** эту папку `docs/playbooks/` в Context нового Project (или укажи агенту читать из этого store).
2. **Прочитай hard constraints** в [vps-stack.md](./vps-stack.md) до любых правок на сервере.
3. **Секреты не переноси** — только имена файлов из [secrets-policy.md](./secrets-policy.md); значения остаются в store / `/opt/*/.env`.
4. **Детали по одной теме** — открывай исходный doc (ссылки ниже), не копируй логи в playbook.
5. **Вынос в git** — только по [how-to-export-to-git.md](./how-to-export-to-git.md); playbook сам по себе безопасен для репо (без значений секретов).

## Роль Project

Долгоживущий координатор VPS-стека (n8n, боты, Hub, workflow), не разовые чаты. Контекст: [`../project-context.md`](../project-context.md). Предпочтения: [`../../preferences.md`](../../preferences.md).

## Ключевые URL (публичные, без паролей)

| Сервис | URL |
|--------|-----|
| n8n | https://147-45-137-104.sslip.io |
| Agent Hub | https://hub-147-45-137-104.sslip.io/ |
| Hub login | https://hub-147-45-137-104.sslip.io/login |

VPS: `147.45.137.104`. Домен пока sslip.io.

## Карта исходных docs (не дублировать)

| Тема | Doc |
|------|-----|
| Контекст / очередь | [`../project-context.md`](../project-context.md) |
| Hub вкладки | [`../hub-tabs-personal.md`](../hub-tabs-personal.md) |
| Hub login / Basic auth | [`../hub-login.md`](../hub-login.md) |
| Hub deploy | [`../agent-hub-deploy.md`](../agent-hub-deploy.md) |
| Dev vs Prod Hub | [`../hub-dev-prod.md`](../hub-dev-prod.md) |
| Hardening | [`../stack-hardening-progress.md`](../stack-hardening-progress.md) |
| Аудит рисков | [`../stack-efficiency-audit.md`](../stack-efficiency-audit.md) |
| n8n credentials (имена) | [`../n8n-credentials-status.md`](../n8n-credentials-status.md) |
| WB Ads план / VPS | [`../wb-ads-plan.md`](../wb-ads-plan.md), [`../wb-ads-vps.md`](../wb-ads-vps.md) |
| Revisor / Logist VPS | [`../wb-revisor-vps.md`](../wb-revisor-vps.md), [`../wb-logist-vps.md`](../wb-logist-vps.md) |

## Жёсткие правила (кратко)

- **Не ломать n8n** без явной нужды; не рестартить контейнер «на всякий».
- **Один Telegram webhook** на весь сервер (боты n8n). Отдельные боты — только long polling / свой токен.
- **Ревизор прод = только n8n**; Hub — витрина (read-only).
- **WB-Ads = только аналитика** (без write ставок/паузы).
- **Секреты** — в store / `.env` mode `600`, никогда в git и в чат.
