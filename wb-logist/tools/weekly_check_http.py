#!/usr/bin/env python3
"""Localhost HTTP bridge: n8n -> weekly_check (read-only, no sheet write)."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HOST = os.environ.get("WEEKLY_CHECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEEKLY_CHECK_PORT", "8791"))
TOKEN = os.environ.get("WEEKLY_CHECK_HTTP_TOKEN", "").strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"status":"error","error":"unauthorized"}\n')

    def _check_auth(self) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("X-Weekly-Check-Token", "")
        return got == TOKEN

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in ("/weekly-check", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._check_auth():
            self._unauthorized()
            return
        if self.path.startswith("/health"):
            body = b'{"status":"ok","service":"weekly_check_http"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            from wb_logist.autodist.weekly_check import run_weekly_check

            payload = run_weekly_check()
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except Exception as exc:  # noqa: BLE001
            err = json.dumps(
                {"status": "error", "error": str(exc), "error_code": "WEEKLY_CHECK_FAILED"},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_POST(self) -> None:
        self.do_GET()


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("weekly_check_http on http://%s:%s/weekly-check\n" % (HOST, PORT))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())