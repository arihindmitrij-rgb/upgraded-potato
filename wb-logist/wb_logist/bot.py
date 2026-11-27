"""Логика бота: команды, запуск пайплайна и ответы в Telegram."""

from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT, load_config
from .errors import WbLogistError
from .telegram import TelegramClient

LOG = logging.getLogger("wb_logist.bot")

TRIGGER_WB = (
    "делай распред",
    "делай распределение",
    "делай вб",
    "погнали",
)
TRIGGER_OZON = ("делай озон", "делай распред озон", "озон распред", "погнали озон")

HELP_TEXT = (
    "🤖 <b>Агент-Логист WB / Ozon</b>\n\n"
    "• «считай распред» — посчитать qty и записать в Google sheet\n"
    "• «считай распред, добавь art1, art2» — то же + новинки без истории\n"
    "• «делай вб» — свежий лист «Баранов ВБ …», ТЗ и черновик FBW\n"
    "• «делай озон» — свежий «Баранов ОЗОН …», ТЗ + заявка FBO "
    "(если задан OZON_DROP_OFF_WAREHOUSE_ID)\n"
    "• «статус» — проверить, что бот на связи\n"
    "• «помощь» — эта подсказка\n\n"
    "Алиасы WB: «делай распред», «погнали».\n"
    "WB: слот в кабинете. Ozon: слот бронирует бот (сегодня+2, вечер)."
)

STATUS_TEXT = (
    "🟢 На связи. «считай распред» — пересчёт листа; "
    "«делай вб» / «делай озон» — черновики.\n"
    "Файлы: REPORT_DIR (WB) / ТЗ Ozon."
)

STALE_TEXT = (
    "Я был офлайн, когда ты это писал — команда устарела. "
    "Повтори, пожалуйста: «статус», «считай распред», «делай вб» или «делай озон»."
)


def normalize(text: str) -> str:
    # точки/лишняя пунктуация не мешают («делай вб.» == «делай вб»)
    # латинская b в «вб» → кириллическая «б»
    text = (text or "").replace("B", "б").replace("b", "б")
    text = re.sub(r"[.!?…,;:]+", " ", text)
    text = re.sub(r"[«»\"'`]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def is_ozon_trigger(text: str) -> bool:
    normalized = normalize(text)
    return any(phrase in normalized for phrase in TRIGGER_OZON)


def is_wb_trigger(text: str) -> bool:
    if is_ozon_trigger(text):
        return False
    normalized = normalize(text)
    return any(phrase in normalized for phrase in TRIGGER_WB)


def is_trigger(text: str) -> bool:
    return is_wb_trigger(text) or is_ozon_trigger(text)


def run_pipeline(
    project_root: Path = PROJECT_ROOT,
    extra_args: list[str] | None = None,
) -> dict:
    """Запускает wb_logist.py и возвращает его JSON-ответ."""

    command = [
        sys.executable,
        str(project_root / "wb_logist.py"),
        *(extra_args or []),
    ]
    LOG.info("Запуск пайплайна: %s", " ".join(command))

    try:
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except subprocess.TimeoutExpired as exc:
        raise WbLogistError(
            "PIPELINE_TIMEOUT", "Скрипт работал больше 30 минут — прервал."
        ) from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        tail = (completed.stderr or "").strip()[-600:]
        raise WbLogistError(
            "PIPELINE_NO_OUTPUT",
            f"Скрипт завершился с кодом {completed.returncode} и ничего не вернул. {tail}",
        )

    try:
        return json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise WbLogistError("PIPELINE_BAD_JSON", stdout[-600:]) from exc


def success_caption(payload: dict) -> str:
    marketplace = payload.get("marketplace") or "wb"
    if marketplace == "ozon":
        return ozon_success_caption(payload)

    supply_id = payload.get("supply_id")
    total = payload.get("total_items", 0)
    reused = bool(payload.get("reused"))
    sheet = payload.get("sheet") or ""

    if reused:
        header = "♻️ Такой черновик уже есть — второй не создавал."
    else:
        header = "Шеф, готово! 🎉"

    lines = [header, ""]
    if sheet:
        lines.append(f"📄 Лист: {html.escape(str(sheet))}")
    if supply_id:
        lines.append(
            f"📦 Черновик WB: <code>{html.escape(str(supply_id))}</code>"
        )
    else:
        lines.append("📄 ТЗ собрано (черновик не создавался).")
    lines.append(f"🔢 Всего штук: <b>{total}</b>")
    lines.append("📎 Файл для ФФ — ниже.")
    lines.append("")
    if reused:
        lines.append("Нужен именно новый черновик — напиши «делай вб дубль».")
    else:
        lines.append("Слот не трогал: дату и коэффициент выбери в кабинете WB.")
    return "\n".join(lines)


def ozon_success_caption(payload: dict) -> str:
    """Краткий отчёт в Telegram после Ozon (заявка / dry-run)."""

    order_id = payload.get("order_id")
    order_url = payload.get("order_url")
    draft_id = payload.get("supply_id")
    sheet = payload.get("sheet") or ""
    positions = payload.get("positions", 0)
    total = payload.get("total_items", 0)
    dry_run = bool(payload.get("dry_run"))
    timeslot = payload.get("timeslot") or {}
    drop_name = payload.get("drop_off_name") or "Кавказский хаб"
    drop_id = payload.get("drop_off_id")
    adjustments = payload.get("adjustments") or []

    lines = ["Шеф, Ozon готово! 🎉", ""]
    if sheet:
        lines.append(f"📄 Лист: {html.escape(str(sheet))}")
    if order_id:
        lines.append(f"📦 Заявка: <code>{html.escape(str(order_id))}</code>")
        if order_url:
            lines.append(f"🔗 {html.escape(str(order_url))}")
    elif draft_id:
        lines.append(f"📝 Draft: <code>{html.escape(str(draft_id))}</code>")
    elif dry_run:
        lines.append("📄 ТЗ собрано (заявка не создавалась — dry-run / нет DROP_OFF).")

    if timeslot.get("start_msk") or timeslot.get("from_in_timezone"):
        slot = timeslot.get("start_msk") or timeslot.get("from_in_timezone")
        lines.append(f"🕒 Слот: {html.escape(str(slot))}")
    if drop_id or drop_name:
        drop_line = html.escape(str(drop_name))
        if drop_id:
            drop_line += f" (<code>{html.escape(str(drop_id))}</code>)"
        lines.append(f"🏭 Drop-off: {drop_line}")

    lines.append(f"🔢 Итого: <b>{positions}</b> поз. / <b>{total}</b> шт.")

    skipped = [a for a in adjustments if a.get("kind") == "skipped_city"]
    reduced = [a for a in adjustments if a.get("kind") == "reduced_qty"]
    skipped_sku = [a for a in adjustments if a.get("kind") == "skipped_sku"]
    if skipped or reduced or skipped_sku:
        lines.append("")
        lines.append("✂️ Убрали / урезали:")
        for block in (skipped + skipped_sku + reduced)[:12]:
            detail = html.escape(str(block.get("detail") or ""))
            qty = block.get("quantity") or 0
            tail = f" (−{qty} шт)" if qty else ""
            lines.append(f"• {detail}{tail}")
        rest = len(skipped) + len(reduced) + len(skipped_sku) - 12
        if rest > 0:
            lines.append(f"• …и ещё {rest}")

    lines.append("")
    lines.append("📎 Excel для ФФ — ниже.")
    return "\n".join(lines)


def _error_text(exc: WbLogistError) -> str:
    return (
        "❌ Не получилось.\n"
        f"Код: <b>{html.escape(exc.code)}</b>\n"
        f"{html.escape(exc.message)}"
    )


def handle_autodist(client: TelegramClient, chat_id: int, text: str) -> None:
    """Команда «считай распред» (+ опционально «добавь …»)."""

    from .autodist.pipeline import format_telegram_summary, parse_add_skus, run_autodist

    add_skus = parse_add_skus(text)
    dry = "dry" in normalize(text)
    client.send_typing(chat_id)
    client.send_message(
        chat_id,
        "⏳ Считаю распределение (WB/Ozon/МойСклад)…"
        + (f" +добавь: {', '.join(add_skus)}" if add_skus else "")
        + (" [dry-run]" if dry else ""),
    )
    try:
        payload = run_autodist(add_skus=add_skus, dry_run=dry if dry else None)
        if payload.get("status") != "success":
            raise WbLogistError(
                str(payload.get("error_code") or "AUTODIST_ERROR"),
                str(payload.get("error") or "неизвестная ошибка"),
            )
        client.send_message(chat_id, format_telegram_summary(payload))
        LOG.info(
            "Autodist ok dry=%s ship=%s",
            payload.get("dry_run"),
            (payload.get("allocation") or {}).get("total_ship"),
        )
    except WbLogistError as exc:
        LOG.error("Autodist error: %s", exc)
        client.send_message(chat_id, _error_text(exc))


def handle_message(client: TelegramClient, chat_id: int, text: str) -> None:
    """Обрабатывает одно сообщение пользователя."""

    from .autodist.pipeline import is_autodist_command

    normalized = normalize(text)

    if normalized in {"/start", "старт", "привет"}:
        client.send_message(chat_id, HELP_TEXT)
        return
    if normalized in {"статус", "/статус", "/status", "status"}:
        client.send_message(chat_id, STATUS_TEXT)
        return
    if normalized in {"помощь", "/help", "/помощь", "help"}:
        client.send_message(chat_id, HELP_TEXT)
        return

    if is_autodist_command(text):
        handle_autodist(client, chat_id, text)
        return

    ozon = is_ozon_trigger(text)
    wb = is_wb_trigger(text)
    if not ozon and not wb:
        client.send_message(
            chat_id,
            "Не понял. Напиши «считай распред», «делай вб» или «делай озон». "
            "«помощь» — подсказка.",
        )
        return

    marketplace = "ozon" if ozon else "wb"
    client.send_typing(chat_id)
    if marketplace == "ozon":
        client.send_message(
            chat_id,
            "⏳ Принял Ozon: лист «Баранов ОЗОН …», ТЗ, заявка FBO "
            "(недоступное уберу)…",
        )
    else:
        client.send_message(
            chat_id,
            "⏳ Принял, работаю: свежий «Баранов ВБ …», баркоды, "
            "ТЗ и черновик поставки… (~10–30 секунд)",
        )

    try:
        force_duplicate = "дубл" in normalized
        extra = ["--marketplace", marketplace]
        if force_duplicate and marketplace == "wb":
            extra.append("--allow-duplicate")
        # Ozon live, если DROP_OFF в .env; иначе пайплайн сам уйдёт в dry-run
        if marketplace == "ozon":
            cfg = load_config()
            if not cfg.ozon_drop_off_warehouse_id or "dry" in normalized:
                extra.append("--dry-run")
        payload = run_pipeline(extra_args=extra)
        if payload.get("status") != "success":
            raise WbLogistError(
                str(payload.get("error_code") or "PIPELINE_ERROR"),
                str(payload.get("error") or "неизвестная ошибка"),
            )
        excel_path = Path(str(payload.get("excel_path") or ""))
        client.send_document(chat_id, excel_path, success_caption(payload))
        LOG.info(
            "Успех %s: order=%s draft=%s файл %s",
            marketplace,
            payload.get("order_id"),
            payload.get("supply_id"),
            excel_path,
        )
    except WbLogistError as exc:
        LOG.error("Ошибка пайплайна: %s", exc)
        client.send_message(chat_id, _error_text(exc))
