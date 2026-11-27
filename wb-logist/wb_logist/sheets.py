"""Чтение таблицы распределения из Google Sheets.

Важно: параметр `sheet=` у gviz/tq Google молча игнорирует и отдаёт первый лист,
поэтому лист всегда определяется через gid (из .env либо из htmlview по имени),
а результат проверяется по заголовкам колонок.

Для ИП Баранов по умолчанию берётся самый свежий лист
«Баранов ВБ …» / «Баранов ОЗОН …» по дате в имени.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from .config import Config, SHEETS_EXPORT_URL, SHEETS_HTMLVIEW_URL
from .errors import WbLogistError
from .models import CityQty, Distribution, DistributionRow, SkippedRow

HttpGet = Callable[[str], str]
Marketplace = Literal["wb", "ozon"]

USER_AGENT = "wb-logist/0.1 (+n8n)"

# ---------------------------------------------------------------------------
# Заголовки колонок
# ---------------------------------------------------------------------------

VENDOR_ALIASES = {
    "артикул продавца",
    "артикул",
    "артикул поставщика",
    "артикул поставщика/продавца",
    "артикул поставщика / продавца",
    "vendorcode",
    "vendor code",
    "vendor_code",
    "sku",
}

QUANTITY_ALIASES = {
    "количество",
    "кол-во",
    "колво",
    "кол-во шт",
    "количество шт",
    "количество, шт",
    "qty",
    "quantity",
    "шт",
}

# Колонки-служебные на листах Ozon / общих — не города
META_COLUMN_ALIASES = {
    "итого",
    "всего",
    "сумма",
    "актуальные остатки",
    "остатки",
    "комментарий",
    "примечание",
    "примечания",
}

# Чистое число: цифры, опционально пробелы-разделители тысяч, опционально дробная часть.
# «30 /10», «10 шт», «нет же?» — не число.
_PURE_NUMBER_RE = re.compile(
    r"^\s*-?\d+(?:[ \u00a0\u202f\u2009]\d{3})*(?:[.,]\d+)?\s*$"
)

# «Баранов ВБ 05.11.2026», «Баранов ОЗОН 26.10», «Баранов Озон 18.11.2026»
_BARANOV_SHEET_RE = re.compile(
    r"^Баранов\s+(?P<mp>ВБ|ОЗОН|Озон)\s+"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})"
    r"(?:\.(?P<year>\d{2,4}))?\s*$",
    re.IGNORECASE,
)


def normalize_header(value: str) -> str:
    """Приводит заголовок к сравнимому виду: регистр, ё, пробелы, двоеточия."""

    text = (value or "").replace("ё", "е").replace("Ё", "Е").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(":")


def normalize_sheet_name(value: str) -> str:
    """Имена вкладок сравниваем без пробелов и скобок: «…(Дима)» == «… (Дима)»."""

    text = (value or "").replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"\s+", "", text)
    text = text.replace("(", "").replace(")", "")
    return text.casefold()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_raw(url: str, timeout: float) -> str:
    """GET без проверки типа контента — нужен для htmlview (это HTML)."""

    import requests

    try:
        response = requests.get(
            url, timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
    except requests.RequestException as exc:  # сеть, DNS, таймаут
        raise WbLogistError(
            "SHEET_REQUEST_FAILED", f"Не удалось получить таблицу: {exc}"
        ) from exc

    if response.status_code != 200:
        raise WbLogistError(
            "SHEET_REQUEST_FAILED",
            f"Google вернул HTTP {response.status_code} для {url}",
        )

    return response.content.decode("utf-8-sig", errors="replace")


def http_get_text(url: str, timeout: float) -> str:
    """GET с проверкой, что нам отдали именно CSV, а не HTML-заглушку."""

    text = http_get_raw(url, timeout)
    if text.lstrip().startswith("<"):
        raise WbLogistError(
            "SHEET_NOT_PUBLIC",
            "Google отдал HTML вместо CSV: откройте доступ «всем, у кого есть ссылка».",
        )
    return text


# ---------------------------------------------------------------------------
# Список листов и gid
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetRef:
    name: str
    gid: str


@dataclass(frozen=True)
class DatedSheetRef:
    sheet: SheetRef
    marketplace: Marketplace
    sheet_date: date


_JS_ESCAPE_RE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)")
_SHEET_ENTRY_RE = re.compile(
    r'name:\s*"(?P<name>(?:[^"\\]|\\.)*)"\s*,\s*pageUrl:\s*"(?P<url>(?:[^"\\]|\\.)*)"'
)
_FALLBACK_ENTRY_RE = re.compile(
    r'name:\s*"(?P<name>(?:[^"\\]|\\.)*)"[^}]{0,500}?gid=(?P<gid>\d+)'
)


def _js_unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if chunk.startswith("x") and len(chunk) == 3:
            return chr(int(chunk[1:], 16))
        if chunk.startswith("u") and len(chunk) == 5:
            return chr(int(chunk[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(chunk, chunk)

    return _JS_ESCAPE_RE.sub(repl, value)


def list_sheets(spreadsheet_id: str, http_get: HttpGet) -> list[SheetRef]:
    """Возвращает вкладки книги: имя + gid (из htmlview)."""

    url = SHEETS_HTMLVIEW_URL.format(spreadsheet_id=spreadsheet_id)
    html = http_get(url)

    sheets: list[SheetRef] = []
    seen: set[str] = set()

    for match in _SHEET_ENTRY_RE.finditer(html):
        name = _js_unescape(match.group("name")).strip()
        gid_match = re.search(r"gid=(\d+)", _js_unescape(match.group("url")))
        if not name or gid_match is None:
            continue
        gid = gid_match.group(1)
        if gid in seen:
            continue
        seen.add(gid)
        sheets.append(SheetRef(name=name, gid=gid))

    if sheets:
        return sheets

    for match in _FALLBACK_ENTRY_RE.finditer(html):
        name = _js_unescape(match.group("name")).strip()
        gid = match.group("gid")
        if not name or gid in seen:
            continue
        seen.add(gid)
        sheets.append(SheetRef(name=name, gid=gid))

    return sheets


def resolve_sheet_gid(
    spreadsheet_id: str, sheet_name: str, http_get: HttpGet
) -> SheetRef:
    """Ищет gid по имени вкладки (точное совпадение, затем вхождение)."""

    sheets = list_sheets(spreadsheet_id, http_get)
    if not sheets:
        raise WbLogistError(
            "SHEET_LIST_UNAVAILABLE",
            "Не удалось получить список вкладок книги. Задайте SHEET_GID в .env.",
        )

    target = normalize_sheet_name(sheet_name)
    for sheet in sheets:
        if normalize_sheet_name(sheet.name) == target:
            return sheet
    for sheet in sheets:
        if target and target in normalize_sheet_name(sheet.name):
            return sheet

    available = ", ".join(f"«{s.name}» (gid={s.gid})" for s in sheets)
    raise WbLogistError(
        "SHEET_NOT_FOUND",
        f"Лист «{sheet_name}» не найден. Доступные листы: {available}",
    )


def parse_baranov_sheet_name(
    name: str, *, today: date | None = None
) -> DatedSheetRef | None:
    """Разбирает имя вкладки «Баранов ВБ|ОЗОН DD.MM[.YYYY]»."""

    match = _BARANOV_SHEET_RE.match((name or "").strip())
    if match is None:
        return None

    mp_raw = match.group("mp").casefold()
    marketplace: Marketplace = "ozon" if mp_raw.startswith("озон") else "wb"
    day = int(match.group("day"))
    month = int(match.group("month"))
    year_raw = match.group("year")
    ref_today = today or date.today()
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
    else:
        year = ref_today.year
        # Если дата «в будущем» относительно сегодня без года — прошлый год
        candidate = date(year, month, day)
        if candidate > ref_today:
            year -= 1

    try:
        sheet_date = date(year, month, day)
    except ValueError:
        return None

    return DatedSheetRef(
        sheet=SheetRef(name=name.strip(), gid=""),
        marketplace=marketplace,
        sheet_date=sheet_date,
    )


def find_freshest_baranov_sheet(
    sheets: list[SheetRef],
    marketplace: Marketplace,
    *,
    today: date | None = None,
) -> SheetRef:
    """Самый свежий лист «Баранов ВБ …» / «Баранов ОЗОН …» по дате в имени."""

    dated: list[DatedSheetRef] = []
    for sheet in sheets:
        parsed = parse_baranov_sheet_name(sheet.name, today=today)
        if parsed is None or parsed.marketplace != marketplace:
            continue
        dated.append(
            DatedSheetRef(
                sheet=sheet,
                marketplace=parsed.marketplace,
                sheet_date=parsed.sheet_date,
            )
        )

    if not dated:
        label = "ВБ" if marketplace == "wb" else "ОЗОН"
        available = ", ".join(f"«{s.name}»" for s in sheets) or "(пусто)"
        raise WbLogistError(
            "SHEET_NOT_FOUND",
            f"Нет листов «Баранов {label} …». Доступные: {available}",
        )

    dated.sort(key=lambda item: item.sheet_date, reverse=True)
    return dated[0].sheet


# ---------------------------------------------------------------------------
# Разбор CSV
# ---------------------------------------------------------------------------


def _sniff_delimiter(text: str) -> str:
    sample = text[:5000]
    counts = {",": sample.count(","), ";": sample.count(";"), "\t": sample.count("\t")}
    return max(counts, key=lambda key: counts[key])


def parse_quantity(raw: str) -> int | None:
    """Строгое количество: только чистое число.

    Берём: «50», «1 000», «5,0».
    Не берём: «30 /10», «10 шт», «нет же?», пусто.
    """

    text = (raw or "").replace("\xa0", " ").replace("\u202f", " ").replace("\u2009", " ")
    text = text.strip()
    if not text:
        return None
    if not _PURE_NUMBER_RE.match(text):
        return None

    normalized = text.replace(" ", "").replace(",", ".")
    try:
        value = float(normalized)
    except ValueError:
        return None

    if value < 0:
        raise WbLogistError(
            "SHEET_BAD_QUANTITY", f"Количество не может быть отрицательным: {raw!r}"
        )
    return int(round(value))


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return (row[index] or "").replace("\xa0", " ").strip()


def _find_layout(rows: list[list[str]]) -> tuple[dict[str, object], int]:
    """Ищет заголовок: артикул + «Количество» ИЛИ артикул + колонки складов/городов."""

    for index, row in enumerate(rows[:20]):
        vendor_index: int | None = None
        quantity_index: int | None = None
        city_columns: list[tuple[int, str]] = []

        for cell_index, raw in enumerate(row):
            header = normalize_header(raw)
            if not header:
                continue
            if vendor_index is None and header in VENDOR_ALIASES:
                vendor_index = cell_index
                continue
            if quantity_index is None and header in QUANTITY_ALIASES:
                quantity_index = cell_index
                continue
            if header in META_COLUMN_ALIASES or header in VENDOR_ALIASES:
                continue
            # Любая другая непустая колонка — склад/город
            city_columns.append((cell_index, (raw or "").replace("\xa0", " ").strip()))

        if vendor_index is None:
            continue

        if quantity_index is not None:
            return (
                {
                    "mode": "quantity",
                    "vendor_code": vendor_index,
                    "quantity": quantity_index,
                },
                index,
            )

        if city_columns:
            return (
                {
                    "mode": "warehouses",
                    "vendor_code": vendor_index,
                    "cities": tuple(city_columns),
                },
                index,
            )

    preview = rows[0][:8] if rows else []
    raise WbLogistError(
        "SHEET_HEADERS_MISMATCH",
        "В листе не найдены колонки «Артикул продавца» и «Количество» "
        f"(или колонки складов/городов). Первая строка: {preview}",
    )


def parse_distribution_csv(
    text: str, *, source: str = "", sheet_label: str = ""
) -> Distribution:
    """Превращает CSV в Distribution: агрегирует дубли, отбрасывает нули.

    Для листов со складами/городами суммирует чистые числа по колонкам
    (контракт ТЗ/черновика WB — одно «Количество» на артикул).
    Разбивка by_city сохраняется для Ozon.
    """

    if not text.strip():
        raise WbLogistError("SHEET_EMPTY", f"Лист пуст: {sheet_label or source}")

    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
    rows = [row for row in reader]
    layout, header_index = _find_layout(rows)
    mode = str(layout["mode"])

    aggregated: dict[str, int] = {}
    first_row: dict[str, int] = {}
    cities_acc: dict[str, dict[str, int]] = {}
    skipped: list[SkippedRow] = []

    for index in range(header_index + 1, len(rows)):
        raw = rows[index]
        if not any(_cell(raw, i) for i in range(len(raw))):
            continue

        vendor_code = _cell(raw, int(layout["vendor_code"]))
        # артефакты экспорта Google (\r в ячейке)
        vendor_code = re.sub(r"[\r\n]+", "", vendor_code).strip()
        if not vendor_code:
            continue

        row_cities: dict[str, int] = {}
        quantity: int | None = None

        if mode == "quantity":
            quantity = parse_quantity(_cell(raw, int(layout["quantity"])))
            if quantity is None:
                cell = _cell(raw, int(layout["quantity"]))
                reason = "пустое количество" if not cell else "нечистое количество"
                skipped.append(
                    SkippedRow(
                        vendor_code=vendor_code,
                        reason=reason,
                        source_row=index + 1,
                    )
                )
                continue
        else:
            for col_index, city_name in layout["cities"]:  # type: ignore[index]
                cell = _cell(raw, int(col_index))
                if not cell:
                    continue
                qty = parse_quantity(cell)
                if qty is None:
                    # текст / «30 /10» рядом с числом — не берём эту ячейку
                    continue
                if qty <= 0:
                    continue
                row_cities[city_name] = row_cities.get(city_name, 0) + qty
            quantity = sum(row_cities.values()) if row_cities else 0
            if quantity == 0:
                # все ячейки пустые/текст/нули
                any_text = any(
                    _cell(raw, int(col_index))
                    for col_index, _ in layout["cities"]  # type: ignore[index]
                )
                skipped.append(
                    SkippedRow(
                        vendor_code=vendor_code,
                        reason="нет чистых количеств по складам"
                        if any_text
                        else "количество 0",
                        source_row=index + 1,
                    )
                )
                continue

        if quantity == 0:
            skipped.append(
                SkippedRow(
                    vendor_code=vendor_code,
                    reason="количество 0",
                    source_row=index + 1,
                )
            )
            continue

        if vendor_code in aggregated:
            aggregated[vendor_code] += quantity
        else:
            aggregated[vendor_code] = quantity
            first_row[vendor_code] = index + 1
            cities_acc[vendor_code] = {}

        for city_name, qty in row_cities.items():
            cities_acc[vendor_code][city_name] = (
                cities_acc[vendor_code].get(city_name, 0) + qty
            )

    distribution_rows = tuple(
        DistributionRow(
            vendor_code=vendor_code,
            quantity=quantity,
            source_row=first_row[vendor_code],
            by_city=tuple(
                CityQty(city=city, quantity=qty)
                for city, qty in cities_acc.get(vendor_code, {}).items()
                if qty > 0
            ),
        )
        for vendor_code, quantity in aggregated.items()
    )

    if not distribution_rows:
        raise WbLogistError(
            "SHEET_EMPTY",
            f"В листе нет строк с положительным количеством: {sheet_label or source}",
        )

    return Distribution(
        rows=distribution_rows,
        skipped=tuple(skipped),
        sheet_label=sheet_label or source,
        source=source,
    )


# ---------------------------------------------------------------------------
# Публичная точка входа
# ---------------------------------------------------------------------------


def load_distribution(
    config: Config,
    *,
    csv_path: str | Path | None = None,
    gid: str | None = None,
    sheet_name: str | None = None,
    marketplace: Marketplace | None = None,
    auto_freshest: bool = True,
    http_get: HttpGet | None = None,
    today: date | None = None,
) -> Distribution:
    """Читает распределение из CSV-файла или Google Sheets.

    При auto_freshest + marketplace (wb/ozon) выбирает самый свежий
    «Баранов ВБ|ОЗОН …», если gid/имя листа не заданы явно аргументами.
    """

    if csv_path:
        path = Path(csv_path)
        if not path.is_file():
            raise WbLogistError("SHEET_CSV_NOT_FOUND", f"Файл не найден: {path}")
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return parse_distribution_csv(text, source=str(path), sheet_label=path.name)

    if not config.spreadsheet_id:
        raise WbLogistError(
            "CONFIG_ERROR",
            "SPREADSHEET_ID не задан ни в .env, ни в переменных окружения.",
        )

    if http_get is not None:
        csv_getter: HttpGet = http_get
        raw_getter: HttpGet = http_get
    else:
        csv_getter = lambda url: http_get_text(url, config.http_timeout)
        raw_getter = lambda url: http_get_raw(url, config.http_timeout)

    # Явный gid/имя из CLI имеют приоритет над авто-выбором
    chosen_gid = gid
    label = f"gid={chosen_gid}" if chosen_gid else ""

    try:
        if not chosen_gid and sheet_name:
            sheet = resolve_sheet_gid(
                config.spreadsheet_id, sheet_name, raw_getter
            )
            chosen_gid = sheet.gid
            label = f"{sheet.name} (gid={sheet.gid})"
        elif not chosen_gid and auto_freshest and marketplace:
            sheets = list_sheets(config.spreadsheet_id, raw_getter)
            sheet = find_freshest_baranov_sheet(
                sheets, marketplace, today=today
            )
            chosen_gid = sheet.gid
            label = f"{sheet.name} (gid={sheet.gid})"
        elif not chosen_gid:
            # legacy: SHEET_GID / SHEET_NAME из .env
            chosen_gid = config.sheet_gid
            label = f"gid={chosen_gid}" if chosen_gid else ""
            if not chosen_gid:
                sheet = resolve_sheet_gid(
                    config.spreadsheet_id, config.sheet_name, raw_getter
                )
                chosen_gid = sheet.gid
                label = f"{sheet.name} (gid={sheet.gid})"

        url = SHEETS_EXPORT_URL.format(
            spreadsheet_id=config.spreadsheet_id, gid=chosen_gid
        )
        text = csv_getter(url)
        return parse_distribution_csv(text, source=url, sheet_label=label)
    except WbLogistError as exc:
        if exc.code != "SHEET_REQUEST_FAILED":
            raise
        # Private native Sheet: public htmlview/export 401 → OAuth API.
        text, label = _export_sheet_csv_via_oauth(
            config.spreadsheet_id,
            gid=chosen_gid or gid,
            sheet_name=sheet_name,
            sheet_label=label,
            marketplace=marketplace,
            today=today,
        )
        return parse_distribution_csv(
            text, source="sheets-api", sheet_label=label
        )


def _export_sheet_csv_via_oauth(
    spreadsheet_id: str,
    *,
    gid: str | None,
    sheet_name: str | None = None,
    sheet_label: str,
    marketplace: Marketplace | None,
    today: date | None,
) -> tuple[str, str]:
    """CSV-текст вкладки через OAuth (для private Google Sheet)."""

    import csv
    import io

    from .autodist.config import load_autodist_config
    from .autodist.sheet_writer import _list_sheets_api, _sheets_service

    ad = load_autodist_config()
    service = _sheets_service(ad)
    sheets = _list_sheets_api(service, spreadsheet_id)
    if not sheets:
        raise WbLogistError("SHEET_NOT_FOUND", "В книге нет вкладок (Sheets API)")

    chosen: SheetRef | None = None
    if gid:
        for s in sheets:
            if s.gid == str(gid):
                chosen = s
                break
    if chosen is None and sheet_name:
        want = normalize_sheet_name(sheet_name)
        for s in sheets:
            if normalize_sheet_name(s.name) == want:
                chosen = s
                break
    if chosen is None and marketplace:
        chosen = find_freshest_baranov_sheet(sheets, marketplace, today=today)
    if chosen is None:
        chosen = sheets[0]

    label = sheet_label or f"{chosen.name} (gid={chosen.gid})"
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{chosen.name}'",
            majorDimension="ROWS",
        )
        .execute()
    )
    rows = result.get("values") or []
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue(), label
