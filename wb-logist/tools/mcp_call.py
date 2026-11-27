#!/usr/bin/env python3
"""Мини-клиент n8n MCP (JSON-RPC over Streamable HTTP) для ручных вызовов.

Usage:
    python tools/mcp_call.py <tool_name> [json-args]

Токен берётся из dashboard/api-credentials.md, чтобы не дублировать секрет.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

MCP_URL = "https://sweedaddy.app.n8n.cloud/mcp-server/http"
CREDENTIALS = (
    Path.home()
    / ".cline"
    / "data"
    / "workspaces"
    / "chat"
    / "dashboard"
    / "api-credentials.md"
)


def load_token() -> str:
    text = CREDENTIALS.read_text(encoding="utf-8")
    match = re.search(r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9[A-Za-z0-9_\-\.]+", text)
    if not match:
        raise SystemExit("n8n MCP token not found in api-credentials.md")
    return match.group(0)


def rpc(token: str, method: str, params: dict, request_id: int) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    response = requests.post(
        MCP_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise SystemExit(f"HTTP {response.status_code}: {response.text[:500]}")

    response.encoding = "utf-8"

    last: dict | None = None
    for blob in _iter_json_objects(response.text):
        try:
            item = json.loads(blob, strict=False)
        except json.JSONDecodeError:
            continue
        if item.get("id") == request_id:
            return item
        last = item
    if last is not None:
        return last

    raw_path = Path(os.environ.get("TEMP", ".")) / "mcp_raw.txt"
    raw_path.write_text(response.text, encoding="utf-8")
    raise SystemExit(f"Cannot parse MCP response; raw saved to {raw_path}")


def _iter_json_objects(text: str):
    """Достаёт JSON-объекты из SSE-потока (в т.ч. если они на одной длинной строке)."""

    index = text.find("{")
    while index != -1:
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for position in range(index, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "\"":
                    in_string = False
                continue
            if char == "\"":
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position + 1
                    break
        if end == -1:
            return
        yield text[index:end]
        index = text.find("{", end)


def call_tool(token: str, name: str, arguments: dict) -> dict:
    rpc(token, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "cline", "version": "1.0.0"},
    }, 1)
    data = rpc(token, "tools/call", {"name": name, "arguments": arguments}, 2)
    result = data.get("result") or {}
    if result.get("isError"):
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    content = result.get("content") or []
    if content and content[0].get("text"):
        return json.loads(content[0]["text"])
    return result


def main() -> None:
    name = sys.argv[1]
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    if raw.startswith("@"):
        arguments = json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    else:
        arguments = json.loads(raw)
    result = call_tool(load_token(), name, arguments)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(text, encoding="utf-8")
        print(f"written: {sys.argv[3]} ({len(text)} chars)")
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)


if __name__ == "__main__":
    main()
