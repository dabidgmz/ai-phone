"""Helpers shared between the simple and production servers.

- Customer lookup by phone number.
- Restaurant hours of operation (returns "open" / "closed").
- Per-call log writer with full transcript and metrics.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Customer recognition
# ---------------------------------------------------------------------------

def load_customers(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to load %s", path)
        return {}


def lookup_customer(customers: dict, phone: str) -> Optional[dict]:
    """Return customer record for the given phone number, or None."""
    return customers.get(phone)


# ---------------------------------------------------------------------------
# Hours of operation
# ---------------------------------------------------------------------------

def is_open_now(hours: dict, now: Optional[datetime] = None) -> bool:
    """True if the restaurant is currently within `hours.open`-`hours.close`."""
    if not hours:
        return True
    try:
        tz = ZoneInfo(hours.get("timezone", "America/Mexico_City"))
    except Exception:
        tz = ZoneInfo("America/Mexico_City")
    now_local = (now or datetime.now(tz)).astimezone(tz)
    open_t = _parse_hm(hours.get("open", "00:00"))
    close_t = _parse_hm(hours.get("close", "23:59"))
    current = now_local.time()
    if open_t <= close_t:
        return open_t <= current < close_t
    # closing past midnight, e.g. open 18:00 - close 02:00
    return current >= open_t or current < close_t


def _parse_hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def closed_message(hours: dict, restaurant_name: str) -> str:
    open_at = hours.get("open", "9:00")
    return (
        f"Hola, gracias por llamar a {restaurant_name}. "
        f"En este momento estamos cerrados. Abrimos a las {open_at}. "
        f"Por favor llámanos más tarde, ¡que tengas buen día!"
    )


# ---------------------------------------------------------------------------
# Call log
# ---------------------------------------------------------------------------

@dataclass
class CallLog:
    call_sid: str
    caller_phone: str
    customer_name: Optional[str]
    started_at: str
    turns: list[dict] = field(default_factory=list)
    order_id: Optional[str] = None
    order_total: Optional[float] = None
    ended_at: Optional[str] = None
    duration_s: Optional[float] = None

    def add_turn(self, role: str, text: str, latency_ms: Optional[float] = None) -> None:
        entry = {
            "role": role,
            "text": text,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if latency_ms is not None:
            entry["latency_ms"] = round(latency_ms, 1)
        self.turns.append(entry)

    def finish(self, order_id: Optional[str], order_total: Optional[float]) -> None:
        self.order_id = order_id
        self.order_total = order_total
        end = datetime.now()
        start = datetime.fromisoformat(self.started_at)
        self.ended_at = end.isoformat(timespec="seconds")
        self.duration_s = round((end - start).total_seconds(), 1)

    def write(self, logs_dir: Path) -> Path:
        logs_dir.mkdir(exist_ok=True)
        path = logs_dir / f"{self.call_sid}.json"
        path.write_text(
            json.dumps(self.__dict__, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path
