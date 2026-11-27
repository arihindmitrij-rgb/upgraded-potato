#!/usr/bin/env python3
"""Агент-Логист WB/Ozon: черновик поставки + ТЗ для фулфилмента.

Контракт для n8n:
  stdout — ровно одна JSON-строка;
  stderr — логи и диагностика;
  exit code 0 — успех, 1 — ошибка.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from wb_logist.config import PROJECT_ROOT, load_config
from wb_logist.errors import WbLogistError
from wb_logist.excel import build_report
from wb_logist.ozon_api import (
    OzonClient,
    quiet_smoke,
    resolve_ozon_rows,
)
from wb_logist.ozon_supply import create_fbo_supply
from wb_logist.sheets import load_distribution
from wb_logist.wb_content import build_index, load_cards, resolve_rows
from wb_logist.wb_supplies import SuppliesClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb_logist.py",
        description="Агент-Логист: распределение Баранов → ТЗ + черновик WB/Ozon.",
    )
    parser.add_argument("--env", type=Path, default=None, help="путь к .env")
    parser.add_argument(
        "--sheet-csv",
        type=Path,
        default=None,
        help="локальный CSV вместо Google Sheets",
    )
    parser.add_argument("--gid", default=None, help="gid листа (явный, без авто-выбора)")
    parser.add_argument("--sheet", default=None, help="имя листа (явное, без авто-выбора)")
    parser.add_argument("--spreadsheet-id", default=None, help="ID Google-таблицы")
    parser.add_argument("--out-dir", type=Path, default=None, help="куда писать xlsx и манифест")
    parser.add_argument(
        "--marketplace",
        choices=("wb", "ozon"),
        default="wb",
        help="wb — свежий «Баранов ВБ …»; ozon — «Баранов ОЗОН …»",
    )
    parser.add_argument(
        "--legacy-sheet",
        action="store_true",
        help="не брать свежий Баранов-лист: использовать SHEET_GID/SHEET_NAME из .env",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="игнорировать кэш карточек и заново выгрузить их из Content API",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="создать черновик WB, даже если такой уже есть за 24 часа",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="собрать ТЗ, но не создавать черновик в кабинете",
    )
    parser.add_argument(
        "--ozon-smoke",
        action="store_true",
        help="только тихий smoke-test Ozon API и выход",
    )
    parser.add_argument("--verbose", action="store_true", help="печатать подробности в stderr")
    return parser


def _force_utf8() -> None:
    """n8n читает байты: фиксируем UTF-8 независимо от кодовой страницы Windows."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def write_manifest(config, distribution, items, draft, excel_path, *, marketplace: str) -> Path:
    """Слепок прогона: что прочитали, что сопоставили, что создали."""

    stamp = datetime.now().strftime("%d%m%Y_%H%M%S")
    if draft is None or getattr(draft, "dry_run", False) or not getattr(draft, "draft_id", None):
        suffix = "dryrun"
    else:
        suffix = getattr(draft, "draft_id", None)
    path = config.output_dir / f"manifest_{marketplace}_{stamp}_{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    draft_id = getattr(draft, "draft_id", None) if draft is not None else None
    chunks = getattr(draft, "chunks", 0) if draft is not None else 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "marketplace": marketplace,
        "sheet": distribution.sheet_label,
        "excel_path": str(excel_path),
        "draft_id": draft_id,
        "chunks": chunks,
        "total_items": sum(item.quantity for item in items),
        "items": [
            {
                "vendor_code": item.vendor_code,
                "barcode": item.barcode,
                "size": item.size,
                "title": item.title,
                "quantity": item.quantity,
                "nm_id": item.nm_id,
                "by_city": [
                    {"city": c.city, "quantity": c.quantity}
                    for c in next(
                        (
                            row.by_city
                            for row in distribution.rows
                            if row.vendor_code == item.vendor_code
                        ),
                        (),
                    )
                ],
            }
            for item in items
        ],
        "skipped": [
            {
                "vendor_code": skip.vendor_code,
                "reason": skip.reason,
                "row": skip.source_row,
            }
            for skip in distribution.skipped
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path.resolve()


def _ozon_report_config(config):
    template = config.ozon_template_path
    if template is None:
        desktop = Path(r"C:\Users\User\Desktop\ИП Баранов Озон.xlsx")
        bundled = PROJECT_ROOT / "templates" / "ozon_tz_template.xlsx"
        if desktop.is_file():
            template = desktop
        elif bundled.is_file():
            template = bundled
    report_dir = config.ozon_report_dir or Path(r"C:\Users\User\Desktop\ТЗ Ozon")
    return replace(
        config,
        report_prefix=config.ozon_report_prefix,
        destination_marker=config.ozon_destination_marker,
        report_dir=report_dir,
    ), template


def run_wb(args, config) -> dict:
    distribution = load_distribution(
        config,
        csv_path=args.sheet_csv,
        gid=args.gid,
        sheet_name=args.sheet,
        marketplace=None if args.legacy_sheet else "wb",
        auto_freshest=not args.legacy_sheet and not args.gid and not args.sheet,
    )
    if args.verbose:
        _log(f"Лист: {distribution.sheet_label}")
        for row in distribution.rows:
            _log(f"  {row.vendor_code}\t{row.quantity}")

    cards = load_cards(config, refresh=args.refresh_cache)
    index = build_index(cards)
    items, barcode_errors = resolve_rows(distribution.rows, index)
    if barcode_errors:
        shown = "; ".join(barcode_errors[:10])
        tail = (
            f" (и ещё {len(barcode_errors) - 10})"
            if len(barcode_errors) > 10
            else ""
        )
        raise WbLogistError(
            "BARCODE_RESOLVE_FAILED",
            f"Не удалось определить баркоды: {shown}{tail}",
        )
    excel_path = build_report(items, config, args.out_dir)
    draft = (
        None
        if args.dry_run
        else SuppliesClient(config).create_supply(
            items, allow_duplicate=args.allow_duplicate
        )
    )
    manifest_path = write_manifest(
        config, distribution, items, draft, excel_path, marketplace="wb"
    )
    total_items = sum(item.quantity for item in items)

    if draft is not None and draft.reused:
        message = (
            f"Точно такой черновик уже есть: {draft.draft_id} "
            f"({draft.total_items} шт) — второй не создавал. "
            "Для принудительного дубля: --allow-duplicate."
        )
    elif draft is not None:
        message = (
            f"Черновик поставки создан, товары добавлены: {draft.total_items} шт. "
            "Слот не бронировал — дату и коэффициент выберите в кабинете WB."
        )
    else:
        message = (
            f"ТЗ для ФФ сформировано: {len(items)} позиций, {total_items} шт. "
            "Черновик не создавался (dry-run)."
        )

    return {
        "status": "success",
        "marketplace": "wb",
        "stage": "supply" if draft is not None else "excel",
        "supply_id": draft.draft_id if draft is not None else None,
        "sheet": distribution.sheet_label,
        "positions": len(items),
        "total_items": total_items,
        "excel_path": str(excel_path),
        "manifest_path": str(manifest_path),
        "skipped": [
            {
                "vendor_code": skip.vendor_code,
                "reason": skip.reason,
                "row": skip.source_row,
            }
            for skip in distribution.skipped
        ],
        "dry_run": draft is None,
        "reused": bool(draft.reused) if draft is not None else False,
        "message": message,
    }


def run_ozon(args, config) -> dict:
    # Тихий smoke один раз — не блокирует пайплайн
    smoke = quiet_smoke(config)
    if args.verbose or not smoke.configured:
        _log(f"Ozon smoke: {smoke.message}")

    distribution = load_distribution(
        config,
        csv_path=args.sheet_csv,
        gid=args.gid,
        sheet_name=args.sheet,
        marketplace=None if args.legacy_sheet else "ozon",
        auto_freshest=not args.legacy_sheet and not args.gid and not args.sheet,
    )
    if args.verbose:
        _log(f"Лист: {distribution.sheet_label}")
        for row in distribution.rows:
            cities = ", ".join(f"{c.city}:{c.quantity}" for c in row.by_city) or "—"
            _log(f"  {row.vendor_code}\t{row.quantity}\t[{cities}]")

    products: dict = {}
    resolve_errors: list[str] = []
    supply = None

    if smoke.configured and smoke.ok:
        client = OzonClient(config)
        products, missing = client.resolve_products(
            [row.vendor_code for row in distribution.rows]
        )
        resolve_errors.extend(f"нет в Ozon: {m}" for m in missing)
        items, item_errors = resolve_ozon_rows(distribution.rows, products)
        resolve_errors.extend(item_errors)
        want_live = not args.dry_run
        supply = create_fbo_supply(
            client,
            list(distribution.rows),
            products,
            dry_run=not want_live or not config.ozon_drop_off_warehouse_id,
        )
        resolve_errors.extend(supply.errors)
        if want_live and not config.ozon_drop_off_warehouse_id:
            resolve_errors.append("Live не создавался: задайте OZON_DROP_OFF_WAREHOUSE_ID")
    else:
        items, _ = resolve_ozon_rows(distribution.rows, {})

    report_config, template = _ozon_report_config(config)
    excel_path = build_report(
        items,
        report_config,
        args.out_dir,
        template_path=template,
    )
    # лёгкий объект для манифеста
    class _DraftProxy:
        def __init__(self, supply_obj):
            self.draft_id = supply_obj.order_id or supply_obj.draft_id if supply_obj else None
            self.dry_run = True if supply_obj is None else supply_obj.dry_run
            self.chunks = 0

    manifest_path = write_manifest(
        config,
        distribution,
        items,
        _DraftProxy(supply) if supply is not None else None,
        excel_path,
        marketplace="ozon",
    )
    total_items = (
        supply.total_items
        if supply is not None and not supply.dry_run
        else sum(item.quantity for item in items)
    )
    positions = (
        supply.positions
        if supply is not None and not supply.dry_run
        else len(items)
    )
    cities_n = len({c.city for row in distribution.rows for c in row.by_city})
    adjustments = [
        {"kind": a.kind, "detail": a.detail, "quantity": a.quantity}
        for a in (supply.adjustments if supply else [])
    ]
    message = (
        supply.message
        if supply is not None
        else f"Ozon: ТЗ {positions} поз. / {total_items} шт."
    )
    if adjustments:
        message += f" Убрано/урезано: {len(adjustments)}."

    return {
        "status": "success",
        "marketplace": "ozon",
        "stage": "order" if supply and supply.order_id else ("excel" if not supply or supply.dry_run else "draft"),
        "supply_id": supply.draft_id if supply else None,
        "order_id": supply.order_id if supply else None,
        "order_url": supply.order_url if supply else None,
        "sheet": distribution.sheet_label,
        "positions": positions,
        "total_items": total_items,
        "cities": cities_n,
        "clusters": supply.clusters if supply else 0,
        "excel_path": str(excel_path),
        "manifest_path": str(manifest_path),
        "timeslot": supply.timeslot if supply else None,
        "drop_off_id": supply.drop_off_id if supply else None,
        "drop_off_name": supply.drop_off_name if supply else None,
        "adjustments": adjustments,
        "ozon_smoke": {
            "ok": smoke.ok,
            "configured": smoke.configured,
            "message": smoke.message,
        },
        "skipped": [
            {
                "vendor_code": skip.vendor_code,
                "reason": skip.reason,
                "row": skip.source_row,
            }
            for skip in distribution.skipped
        ],
        "resolve_errors": resolve_errors[:20],
        "dry_run": bool(args.dry_run or not supply or supply.dry_run),
        "reused": False,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)

    try:
        config = load_config(
            args.env,
            {
                "SPREADSHEET_ID": args.spreadsheet_id,
                "OUTPUT_DIR": args.out_dir,
            },
        )
    except WbLogistError as exc:
        _log(f"Ошибка: {exc}")
        _emit({"status": "error", "error": exc.message, "error_code": exc.code})
        return 1

    if args.ozon_smoke:
        smoke = quiet_smoke(config)
        _emit(
            {
                "status": "success" if smoke.ok or not smoke.configured else "error",
                "stage": "ozon_smoke",
                "ozon_smoke": {
                    "ok": smoke.ok,
                    "configured": smoke.configured,
                    "message": smoke.message,
                },
                "message": smoke.message,
            }
        )
        # пустые ключи — не ошибка процесса
        return 0 if smoke.ok or not smoke.configured else 1

    try:
        payload = (
            run_ozon(args, config)
            if args.marketplace == "ozon"
            else run_wb(args, config)
        )
    except WbLogistError as exc:
        _log(f"Ошибка: {exc}")
        _emit({"status": "error", "error": exc.message, "error_code": exc.code})
        return 1
    except Exception as exc:  # noqa: BLE001 — n8n должен получить JSON, а не трейсбек
        import traceback

        traceback.print_exc(file=sys.stderr)
        _emit(
            {
                "status": "error",
                "error": f"Внутренняя ошибка: {exc}",
                "error_code": "INTERNAL_ERROR",
            }
        )
        return 1

    if args.verbose:
        _log(payload.get("message", ""))

    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
