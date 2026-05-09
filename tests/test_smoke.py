"""Smoke tests: project imports cleanly and core helpers behave."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent


def test_core_modules_import():
    import agent  # noqa: F401
    import config  # noqa: F401
    import dashboard  # noqa: F401
    import prompts  # noqa: F401
    import runtime  # noqa: F401
    import server  # noqa: F401
    import stt  # noqa: F401
    import tts  # noqa: F401


def test_config_loads_with_env_vars():
    from config import load_settings

    s = load_settings()
    assert s.twilio_account_sid == "AC_test"
    assert s.groq_model == "llama-3.3-70b-versatile"
    assert s.public_host == "test.ngrok-free.app"


def test_menu_is_valid_json():
    menu = json.loads((ROOT / "menu.json").read_text(encoding="utf-8"))
    assert "restaurant_name" in menu
    assert "categories" in menu and len(menu["categories"]) > 0
    for cat in menu["categories"]:
        for item in cat["items"]:
            assert {"id", "name", "price"} <= set(item.keys())


def test_build_system_prompt_includes_menu_items():
    from prompts import build_system_prompt

    menu = json.loads((ROOT / "menu.json").read_text(encoding="utf-8"))
    prompt = build_system_prompt(menu)
    assert menu["restaurant_name"] in prompt
    for cat in menu["categories"]:
        for item in cat["items"]:
            assert item["name"] in prompt


def test_hours_open_at_noon_closed_at_3am():
    from runtime import is_open_now

    hours = {"timezone": "America/Mexico_City", "open": "09:00", "close": "22:00"}
    tz = ZoneInfo("America/Mexico_City")
    assert is_open_now(hours, datetime(2026, 5, 9, 12, 0, tzinfo=tz)) is True
    assert is_open_now(hours, datetime(2026, 5, 9, 3, 0, tzinfo=tz)) is False


def test_customer_lookup():
    from runtime import load_customers, lookup_customer

    customers = load_customers(ROOT / "customers.json")
    assert lookup_customer(customers, "+528711419810") is not None
    assert lookup_customer(customers, "+10000000000") is None


def test_calllog_writes_json(tmp_path):
    from runtime import CallLog

    log = CallLog(
        call_sid="CA_test",
        caller_phone="+1",
        customer_name="Test",
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    log.add_turn("user", "hola")
    log.add_turn("agent", "hi", latency_ms=234.5)
    log.finish(order_id=None, order_total=None)

    path = log.write(tmp_path)
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["call_sid"] == "CA_test"
    assert len(record["turns"]) == 2
    assert record["turns"][1]["latency_ms"] == 234.5


def test_dashboard_event_bus_pubsub():
    import asyncio
    from dashboard import EventBus

    async def run():
        bus = EventBus()
        q = bus.subscribe()
        bus.publish({"type": "hello"})
        evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt == {"type": "hello"}

    asyncio.run(run())


def test_voice_endpoint_returns_twiml():
    from fastapi.testclient import TestClient

    import server

    with TestClient(server.app) as client:
        r = client.post("/voice")
    assert r.status_code == 200
    assert "<Response>" in r.text
    assert '<Stream url="wss://test.ngrok-free.app/media"' in r.text
