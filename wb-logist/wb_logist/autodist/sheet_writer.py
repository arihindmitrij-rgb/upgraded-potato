"""Запись qty в боевой Google sheet «Баранов ВБ/ОЗОН …»."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..config import Config
from ..errors import WbLogistError
from ..sheets import SheetRef, find_freshest_baranov_sheet
from .config import AutodistConfig

LOG = logging.getLogger("wb_logist.autodist.sheet")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def _sheets_service(ad: AutodistConfig):
    """Google Sheets API client из token.json / OAuth client."""

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise WbLogistError(
            "GOOGLE_DEPS_MISSING",
            "Нужны пакеты google-auth, google-auth-oauthlib, google-api-python-client",
        ) from exc

    creds = None
    token_path = ad.google_token_path
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    if not creds or not creds.valid:
        raise WbLogistError(
            "GOOGLE_AUTH_MISSING",
            "Нет .cache/google_token.json. Один раз на этом ПК: "
            "python tools/google_oauth_bootstrap.py (откроется браузер Google).",
        )

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _today_sheet_name(mp: str, today: date | None = None) -> str:
    d = today or date.today()
    label = "ВБ" if mp == "wb" else "ОЗОН"
    return f"Баранов {label} {d.strftime('%d.%m.%Y')}"


def _list_sheets_api(service, spreadsheet_id: str) -> list[SheetRef]:
    """Вкладки через Sheets API (не зависит от публичного htmlview)."""

    meta = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(title,sheetId)",
        )
        .execute()
    )
    out: list[SheetRef] = []
    for sh in meta.get("sheets") or []:
        props = sh.get("properties") or {}
        title = (props.get("title") or "").strip()
        sheet_id = props.get("sheetId")
        if not title or sheet_id is None:
            continue
        out.append(SheetRef(name=title, gid=str(sheet_id)))
    return out


def _read_headers_api(service, spreadsheet_id: str, sheet_title: str) -> list[str]:
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_title}'!1:1",
            majorDimension="ROWS",
        )
        .execute()
    )
    row = (result.get("values") or [[]])[0]
    return [str(c).strip() for c in row]


def ensure_sheet_tab(
    service,
    spreadsheet_id: str,
    title: str,
    *,
    template_gid: str | None = None,
) -> int:
    """Создаёт вкладку, если нет; возвращает sheetId."""

    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sh in meta.get("sheets") or []:
        props = sh.get("properties") or {}
        if props.get("title") == title:
            return int(props["sheetId"])

    requests_body: list[dict[str, Any]] = []
    if template_gid:
        requests_body.append(
            {
                "duplicateSheet": {
                    "sourceSheetId": int(template_gid),
                    "insertSheetIndex": 0,
                    "newSheetName": title,
                }
            }
        )
    else:
        requests_body.append(
            {
                "addSheet": {
                    "properties": {"title": title, "index": 0}
                }
            }
        )
    result = (
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests_body})
        .execute()
    )
    replies = result.get("replies") or []
    if not replies:
        raise WbLogistError("SHEET_WRITE_FAILED", "Не удалось создать вкладку")
    reply = replies[0]
    if "duplicateSheet" in reply:
        return int(reply["duplicateSheet"]["properties"]["sheetId"])
    return int(reply["addSheet"]["properties"]["sheetId"])


def clear_and_write_values(
    service,
    spreadsheet_id: str,
    sheet_title: str,
    headers: list[str],
    rows: list[list[Any]],
) -> None:
    range_a1 = f"'{sheet_title}'!A1"
    # Очистить лист
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_title}'",
        body={},
    ).execute()
    values = [headers] + rows
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def write_wb_distribution(
    config: Config,
    ad: AutodistConfig,
    qty_by_sku: dict[str, int],
    *,
    dry_run: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Пишет лист «Баранов ВБ DD.MM.YYYY»: артикул + колонка склада Москва."""

    if not config.spreadsheet_id:
        raise WbLogistError("CONFIG_ERROR", "SPREADSHEET_ID не задан")

    # Native Sheet может быть private — list/headers только через OAuth API.
    service = _sheets_service(ad)
    sheets = _list_sheets_api(service, config.spreadsheet_id)
    freshest = find_freshest_baranov_sheet(sheets, "wb", today=today)
    headers = _read_headers_api(service, config.spreadsheet_id, freshest.name)
    if len(headers) < 2:
        headers = ["Артикул продавца", ad.wb_warehouse_label]
    else:
        # Сохраняем имя склада из freshest, если есть
        headers = [headers[0], headers[1] if len(headers) > 1 else ad.wb_warehouse_label]

    title = _today_sheet_name("wb", today)
    rows = [[sku, int(qty)] for sku, qty in sorted(qty_by_sku.items()) if int(qty) > 0]

    if dry_run or ad.dry_run:
        LOG.info("DRY-RUN WB sheet %s: %s строк", title, len(rows))
        return {
            "marketplace": "wb",
            "sheet": title,
            "rows": len(rows),
            "dry_run": True,
            "sample": rows[:5],
            "template": freshest.name,
        }

    ensure_sheet_tab(
        service, config.spreadsheet_id, title, template_gid=freshest.gid
    )
    # После duplicate шаблон может содержать старые qty — перезаписываем
    clear_and_write_values(service, config.spreadsheet_id, title, headers[:2], rows)
    LOG.info("Записан WB лист %s (%s строк)", title, len(rows))
    return {
        "marketplace": "wb",
        "sheet": title,
        "rows": len(rows),
        "dry_run": False,
        "total_qty": sum(r[1] for r in rows),
    }


def write_ozon_distribution(
    config: Config,
    ad: AutodistConfig,
    by_sku_city: dict[str, dict[str, int]],
    *,
    dry_run: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Пишет «Баранов ОЗОН DD.MM.YYYY» с разбивкой по кластерам-колонкам."""

    if not config.spreadsheet_id:
        raise WbLogistError("CONFIG_ERROR", "SPREADSHEET_ID не задан")

    service = _sheets_service(ad)
    sheets = _list_sheets_api(service, config.spreadsheet_id)
    freshest = find_freshest_baranov_sheet(sheets, "ozon", today=today)
    headers = _read_headers_api(service, config.spreadsheet_id, freshest.name)
    if not headers:
        headers = ["Артикул продавца", "Итого"]

    # Нормализуем имена колонок
    header_index = {h: i for i, h in enumerate(headers)}
    city_cols = [
        h
        for h in headers[1:]
        if h
        and h.casefold()
        not in {"итого", "всего", "сумма", "актуальные остатки", "остатки", "комментарий"}
    ]

    title = _today_sheet_name("ozon", today)
    rows: list[list[Any]] = []
    for sku, cities in sorted(by_sku_city.items()):
        total = sum(int(v) for v in cities.values() if int(v) > 0)
        if total <= 0:
            continue
        row = [""] * len(headers)
        row[0] = sku
        if "Итого" in header_index:
            row[header_index["Итого"]] = total
        elif "итого" in {h.casefold(): h for h in headers}:
            # already handled
            pass
        for city, qty in cities.items():
            if int(qty) <= 0:
                continue
            # точное имя или casefold match
            idx = header_index.get(city)
            if idx is None:
                for h, i in header_index.items():
                    if h.casefold() == city.casefold():
                        idx = i
                        break
            if idx is None:
                # кластер не из шаблона — пропускаем колонку (оставим в итоге)
                continue
            row[idx] = int(qty)
        rows.append(row)

    if dry_run or ad.dry_run:
        LOG.info("DRY-RUN Ozon sheet %s: %s строк", title, len(rows))
        return {
            "marketplace": "ozon",
            "sheet": title,
            "rows": len(rows),
            "dry_run": True,
            "sample": rows[:3],
            "template": freshest.name,
            "city_cols": city_cols[:8],
        }

    ensure_sheet_tab(
        service, config.spreadsheet_id, title, template_gid=freshest.gid
    )
    clear_and_write_values(service, config.spreadsheet_id, title, headers, rows)
    LOG.info("Записан Ozon лист %s (%s строк)", title, len(rows))
    return {
        "marketplace": "ozon",
        "sheet": title,
        "rows": len(rows),
        "dry_run": False,
        "total_qty": sum(
            sum(int(v) for v in cities.values()) for cities in by_sku_city.values()
        ),
    }
