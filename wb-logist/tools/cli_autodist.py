#!/usr/bin/env python3
"""CLI: python tools/cli_autodist.py [--dry-run] [--add sku1,sku2]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_logist.autodist.pipeline import run_autodist
from wb_logist.errors import WbLogistError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Автораспределение Баранов")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--add", default="", help="SKU через запятую")
    args = p.parse_args(argv)
    add = [s.strip() for s in args.add.split(",") if s.strip()]
    try:
        payload = run_autodist(add_skus=add, dry_run=args.dry_run)
    except WbLogistError as exc:
        print(json.dumps({"status": "error", "error_code": exc.code, "error": exc.message}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
