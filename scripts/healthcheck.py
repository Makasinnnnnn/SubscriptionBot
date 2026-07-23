#!/usr/bin/env python3
"""Check production health and notify the operations chat on state changes."""

from __future__ import annotations

import json
import os
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(os.environ.get("PROPGATE_ROOT", "/opt/propgate"))
ENV_FILE = ROOT / ".env.production"
STATE_FILE = Path(os.environ.get("PROPGATE_MONITOR_STATE", "/var/lib/propgate-monitor/status"))
EVENTS_FILE = Path(os.environ.get("PROPGATE_MONITOR_EVENTS", "/var/lib/propgate-monitor/events.jsonl"))
BASE_URL = os.environ.get("MONITOR_BASE_URL", "https://propgate.ru").rstrip("/")


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def check(path: str) -> str:
    request = Request(f"{BASE_URL}{path}", headers={"User-Agent": "PropGateMonitor/1.0"})
    with urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return response.read(4096).decode("utf-8", errors="replace")


def parse_chat_ids(raw_value: str) -> list[int]:
    if not raw_value:
        return []
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = [item.strip() for item in raw_value.split(",") if item.strip()]
    return [int(item) for item in value]


def notify(token: str, chat_ids: list[int], text: str) -> None:
    if not token or not chat_ids:
        return
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                if response.status != 200:
                    print(f"Telegram notification returned HTTP {response.status}", file=sys.stderr)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"Telegram notification failed: {type(exc).__name__}", file=sys.stderr)


def load_previous_state() -> str:
    try:
        return STATE_FILE.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "unknown"


def save_state(value: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(f"{value}\n", encoding="ascii")


def append_event(status: str, details: str) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "time": datetime.now(UTC).isoformat(),
        "status": status,
        "details": details[:500],
    }
    with EVENTS_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=True) + "\n")


def main() -> int:
    env = read_env()
    previous = load_previous_state()
    details = ""
    current = "up"
    try:
        check("/health")
        check("/health/db")
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        current = "down"
        details = f"{type(exc).__name__}: {exc}"

    if current != previous:
        if current == "down":
            text = f"PROP GATE / ALERT\nProduction health check failed.\n{details[:500]}"
        elif previous == "down":
            text = "PROP GATE / RECOVERY\nProduction health checks are healthy again."
        else:
            text = "PROP GATE / SYSTEM\nProduction health monitoring started successfully."
        notify(
            env.get("OPS_BOT_TOKEN", ""),
            parse_chat_ids(env.get("OPS_CHAT_IDS", "")),
            text,
        )
        append_event(current, details or text)

    save_state(current)
    if current == "down":
        print(details, file=sys.stderr)
        return 1
    print("Production health checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
