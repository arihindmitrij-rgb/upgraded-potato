# Политика секретов

Правило: **значения никогда** в git, в чат, в playbook, в экспорты workflow «на диске рядом с compose». В docs — только **имена** файлов и credential.

Контекст: [`../project-context.md`](../project-context.md), credentials: [`../n8n-credentials-status.md`](../n8n-credentials-status.md), hardening: [`../stack-hardening-progress.md`](../stack-hardening-progress.md).

## Где хранить

| Место | Что |
|-------|-----|
| **Project store** `secrets/` | API-ключи стека проекта (WB, Ozon, Hub basic, МойСклад, OpenRouter, TG logist, …) |
| **User store** | SSH / n8n-mcp / env миграции — не дублировать в Project и в чат |
| **VPS** `/opt/<app>/.env` | Боевые значения, `chmod 600`; не коммитить |
| **n8n Credentials** | Токены ботов и API, привязанные к workflow |

Не спрашивать ключ повторно, если файл уже есть в store.

## Имена файлов в Project `secrets/` (без значений)

Ориентир по наличию (список может расти):

- `wb-api-token.txt` — WRITE / общий legacy
- `wb-api-token-read.txt` — READ
- `wb-advert-token.txt` — опционально, категория «Продвижение» (когда выдадут)
- `ozon-api-key.txt`
- `moysklad-token.txt`
- `openrouter-api-key.txt`
- `hub-basic-auth-user.txt` / `hub-basic-auth-password.txt`
- `wb-logist-telegram-token.txt`

## Имена credentials в n8n (не значения)

| Имя в n8n | Тип (ориентир) | Назначение |
|-----------|----------------|------------|
| Виталик | `telegramApi` | бот «Первое детище» |
| Fitness account | `telegramApi` | личный тренер |
| WB Agent | `telegramApi` | ревизор / WB-бот |
| OpenAI account | `openAiApi` | LLM в WF |
| Google Sheets account | `googleSheetsOAuth2Api` | таблицы |
| WB API токен (Арихин Д) | `httpTemplatedCustomAuth` | JWT WB в HTTP-нодах |
| Header Auth account 3 | `httpHeaderAuth` | МойСклад Bearer |
| OpenRouter | `httpHeaderAuth` | Transcribe Voice (не inline в ноде) |

Полный статус/ID: [`../n8n-credentials-status.md`](../n8n-credentials-status.md).

## Что не класть в git / чат / HTML

- Любые токены, JWT, пароли, OAuth refresh, содержимое `.env`
- `creds.json`, сырые экспорты n8n с credential data рядом с `/opt/n8n`
- Скриншоты/логи с `Authorization: …` или полным JWT
- Desktop-ярлыки с зашитым паролем в URL (если появятся — только локально у пользователя)

## Что можно в git / playbook

- Имена env-переменных (`WB_API_KEY_STANDARD`, `WB_TOKEN`, `LLM_ENABLED`)
- Имена файлов secrets и credential
- Публичные URL, пути `/opt/…`, команды без подстановки секретов
- Политики (READ vs WRITE, analytics-only)

## Практика на VPS

1. Скопировать значение из store → `/opt/<app>/.env` (или n8n UI credential).
2. Проверить права `600`; не `chmod` в world-readable backup без нужды.
3. Бэкапы с секретами — в `/var/backups/…` с жёсткими правами (`0700` на офлайн-экспорты).
4. Ротация: сначала новый ключ в store + сервисы, потом отзыв старого (особенно если ключ светился в чате/ноде).
