#!/usr/bin/env python3
"""Воркер Telegram-бота: long polling, запуск пайплайна, ответ в личку.

Запуск: python bot_worker.py [--env PATH] [--check] [--once] [--test-chat ID] [--verbose]
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from wb_logist.bot import STALE_TEXT, handle_message
from wb_logist.config import PROJECT_ROOT, load_config
from wb_logist.errors import WbLogistError
from wb_logist.heartbeat import write_heartbeat
from wb_logist.telegram import TelegramClient

LOCK_PORT = 45911
LOG_FILE = PROJECT_ROOT / "logs" / "bot.log"
HEARTBEAT_EVERY_SEC = 30


def setup_logging(verbose: bool) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if verbose:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def acquire_lock() -> socket.socket | None:
    """Не даёт запустить второй воркер (два polling = конфликт).

    На Windows SO_REUSEADDR позволяет двум процессам занять один порт —
    поэтому reuse не ставим; bind без него = эксклюзивный lock.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows: без REUSEADDR второй bind падает — это и нужно
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram-воркер Агент-Логист WB")
    parser.add_argument("--env", type=Path, default=None, help="путь к .env")
    parser.add_argument("--check", action="store_true", help="проверить токен и выйти")
    parser.add_argument("--once", action="store_true", help="обработать пачку и выйти")
    parser.add_argument("--test-chat", default=None, help="отправить тестовое сообщение в chat_id")
    parser.add_argument("--verbose", action="store_true", help="логировать в консоль")
    parser.add_argument(
        "--connect-attempts",
        type=int,
        default=0,
        help="сколько раз пытаться подключиться к Telegram (0 — пока не получится)",
    )
    return parser


def connect_with_retry(config, attempts: int = 0):
    """Ждёт сеть после входа в Windows: при логине интернет часто ещё не поднят."""

    delay = 10
    trial = 0
    while True:
        trial += 1
        try:
            client = TelegramClient(config.telegram_bot_token or "", timeout=90)
            me = client.get_me()
            logging.info("Подключились к Telegram: @%s", me.get("username"))
            return client, me
        except WbLogistError as exc:
            logging.error("Telegram недоступен (попытка %s): %s", trial, exc)
            try:
                write_heartbeat(status="starting", detail=f"telegram wait: {exc.code}")
            except OSError:
                pass
            if attempts and trial >= attempts:
                return None, None
            time.sleep(delay)
            delay = min(delay * 2, 60)


def start_heartbeat_loop(stop_event: threading.Event, username: str = "") -> threading.Thread:
    """Фоновый heartbeat для Agent Hub (без Cursor)."""

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                write_heartbeat(
                    status="ok",
                    detail=f"polling @{username}" if username else "polling",
                    extra={"bot": username or None},
                )
            except OSError as exc:
                logging.warning("heartbeat write failed: %s", exc)
            stop_event.wait(HEARTBEAT_EVERY_SEC)

    thread = threading.Thread(target=_loop, name="heartbeat", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    setup_logging(False)
    args = build_parser().parse_args(argv)
    if args.verbose:
        setup_logging(True)

    try:
        config = load_config(args.env)
    except WbLogistError as exc:
        logging.error("Ошибка конфигурации: %s", exc)
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 1

    # --check / --test-chat не держат polling — lock не нужен
    need_lock = not args.check and not args.test_chat
    lock = None
    if need_lock:
        lock = acquire_lock()
        if lock is None:
            logging.warning("Второй воркер не нужен: уже запущен. Выхожу.")
            return 0

    client, me = connect_with_retry(config, attempts=args.connect_attempts)
    if client is None or me is None:
        logging.error("Подключиться к Telegram не удалось — выхожу")
        if lock is not None:
            lock.close()
        return 1

    print(f"Бот: @{me.get('username')} (id {me.get('id')})")
    if args.check:
        return 0

    if args.test_chat:
        client.send_message(
            int(args.test_chat),
            "🟢 Бот на связи. Напиши «статус», «делай вб» или «делай озон».",
        )
        print("Тестовое сообщение отправлено.")
        return 0

    started = time.time()
    offset: int | None = None
    stale_replies = 0
    stale_window = 6 * 3600
    username = str(me.get("username") or "")
    logging.info("Воркер запущен, бот @%s", username)
    print("Воркер запущен. Ctrl+C — остановить.")

    stop_hb = threading.Event()
    start_heartbeat_loop(stop_hb, username=username)
    write_heartbeat(status="ok", detail=f"started @{username}", extra={"bot": username})

    try:
        while True:
            try:
                updates = client.get_updates(offset, timeout=50)
            except WbLogistError as exc:
                logging.error("getUpdates: %s", exc)
                try:
                    write_heartbeat(
                        status="degraded",
                        detail=f"getUpdates: {exc.code}",
                        extra={"bot": username},
                    )
                except OSError:
                    pass
                time.sleep(5)
                continue

            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                text = message.get("text") or ""
                sent_at = int(message.get("date") or 0)

                if not chat_id:
                    continue
                if sent_at and sent_at < started - 10:
                    logging.info("Старое сообщение (пропущено): %s", text[:80])
                    fresh_enough = (time.time() - sent_at) < stale_window
                    if fresh_enough and stale_replies < 3:
                        stale_replies += 1
                        try:
                            client.send_message(int(chat_id), STALE_TEXT)
                        except WbLogistError:
                            pass
                    continue

                logging.info("Команда от %s: %s", chat_id, text[:120])
                try:
                    handle_message(client, int(chat_id), text)
                except WbLogistError as exc:
                    logging.error("Ошибка обработки: %s", exc)
                    try:
                        client.send_message(int(chat_id), f"❌ {exc.code}: {exc.message}")
                    except WbLogistError:
                        pass

            if args.once:
                return 0
    except KeyboardInterrupt:
        logging.info("Остановлен вручную")
        print("Остановлен.")
        return 0
    finally:
        stop_hb.set()
        try:
            write_heartbeat(status="stopped", detail="worker exit")
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
