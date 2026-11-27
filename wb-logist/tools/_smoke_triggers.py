"""Smoke: матчинг фраз бота без Telegram."""

from __future__ import annotations

from wb_logist.bot import (
    HELP_TEXT,
    STATUS_TEXT,
    is_ozon_trigger,
    is_wb_trigger,
    normalize,
)

CASES = [
    ("делай вб", "wb"),
    ("Делай ВБ", "wb"),
    ("делай вб.", "wb"),
    ("делай вб!", "wb"),
    ("«делай вб»", "wb"),
    ("делай вb", "wb"),  # latin b
    ("делай распред", "wb"),
    ("погнали", "wb"),
    ("делай вб дубль", "wb"),
    ("делай озон", "ozon"),
    ("Делай Озон", "ozon"),
    ("делай распред озон", "ozon"),
    ("погнали озон", "ozon"),
    ("привет", None),
    ("что умеешь", None),
    ("делай", None),
]


def main() -> int:
    failed = 0
    for text, expect in CASES:
        wb = is_wb_trigger(text)
        oz = is_ozon_trigger(text)
        got = "ozon" if oz else ("wb" if wb else None)
        mark = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"{mark}\t{text!r}\t→ {got}\t(want {expect}) norm={normalize(text)!r}")

    for label, blob in (("HELP", HELP_TEXT), ("STATUS", STATUS_TEXT)):
        has_wb = "делай вб" in blob
        has_oz = "делай озон" in blob
        # welcome не должен начинать с «делай распред» как главной команды
        leads_with_raspred = "«делай распред» / «делай вб»" in blob
        ok = has_wb and has_oz and not leads_with_raspred
        print(f"{'OK' if ok else 'FAIL'}\t{label} mentions вб={has_wb} озон={has_oz} old_lead={leads_with_raspred}")
        if not ok:
            failed += 1

    print(f"\nfailed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
