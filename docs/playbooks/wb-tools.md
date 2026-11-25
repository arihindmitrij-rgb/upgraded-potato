# WB-инструменты

Три контура: Ревизор, Логист, Ads. Не смешивать API и токены. Обзоры/smoke — в исходных docs, здесь только карта.

## Сводка

| Инструмент | Hub id | Код на VPS | Прод | Токен (роль) | Docs |
|------------|--------|------------|------|--------------|------|
| Ревизор | `revisor` | `/opt/wb-revisor` (зеркало/fixture) | **только n8n** WF | READ | [`../wb-revisor-vps.md`](../wb-revisor-vps.md), [`../wb-revisor-overview.md`](../wb-revisor-overview.md) |
| Логист | `wb-logist` | `/opt/wb-logist` | Hub Run + CLI; бот long poll | WRITE | [`../wb-logist-vps.md`](../wb-logist-vps.md) |
| Ads | `wb-ads` | `/opt/wb-ads` | Hub Run → CLI отчёт | READ / Advert | [`../wb-ads-vps.md`](../wb-ads-vps.md), [`../wb-ads-plan.md`](../wb-ads-plan.md) |

Локальный Windows-черновик ревизора: `C:\Users\User\.cline\data\workspaces\chat\wb-revisor` — **на VPS как «второй прод» не класть** (уже есть `/opt` + n8n). Исходники в store: `internal/wb-*-src/`.

## Ревизор

- Source of truth: n8n **«Агент-Ревизор WB»** (`89BaXCFtcZS0au0J`, active).
- Hub: `readOnly` / `disableRun` / «Только просмотр»; `POST /api/run/revisor` → ошибка read-only.
- `/opt/wb-revisor`: fixture/ручные прогоны, `LLM_ENABLED=0` по умолчанию; `.env` ← READ-токен.
- Не дублировать live/schedule с Hub и n8n одновременно.

## Логист

- CLI: `python wb_logist.py` (в т.ч. `--dry-run`); отчёты в `output/reports`.
- Hub Run разрешён; паттерн-образец для Ads.
- Telegram: сервис `wb-logist-bot` (long polling); webhook у бота пустой — конфликта с n8n нет.
- `.env` ← WRITE-токен WB (+ отдельный TG-токен бота из store).

## WB-Ads (analytics-only)

**Решения (зафиксировано):**

- Только **аналитика** — без write ставок/паузы.
- Telegram-дайджест **не нужен** (если появится — отдельный бот/long poll, не webhook n8n).
- Период выбираемый: пресеты (`last-week`, `last-7d`, `this-week`, `yesterday`) + `begin`/`end`.
- Пороги алертов: ДРР > 20%, spend без orders > 300₽, баланс < 5000₽.
- Карточка: display **WB-Ads**, id `wb-ads`, вкладка Work. Не путать со stub `wb`.

**CLI:**

```bash
cd /opt/wb-ads && source .venv/bin/activate
python wb_ads.py ping
python wb_ads.py run --preset last-week -v
python wb_ads.py run --begin YYYY-MM-DD --end YYYY-MM-DD
```

Артефакты: `output/wb-ads-dashboard.html`, `report.csv`, `data.json`, `report.md`.

**Токен:** сейчас READ в `.env` как `WB_TOKEN` достаточен для Advert read. Отдельный `wb-advert-token.txt` (категория «Продвижение») — когда выдадут; CLI подхватит из store/`/opt/wb-ads/secrets/`.

**Не делать:** отдельный ads-engine в n8n; write Advert API; второй webhook на боте n8n.

## Разделение токенов

| Куда | Роль | Файл в store (имя) |
|------|------|---------------------|
| `/opt/wb-revisor/.env` | READ | `wb-api-token-read.txt` |
| `/opt/wb-logist/.env` | WRITE | `wb-api-token.txt` |
| `/opt/wb-ads/.env` | READ / Advert | READ; опционально `wb-advert-token.txt` |

Политика на VPS: `TOKEN_POLICY.txt` у ревизора. Подробности hardening: [`../stack-hardening-progress.md`](../stack-hardening-progress.md).
