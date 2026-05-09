"""Twilio-only voice agent (no Deepgram, no ElevenLabs) + live dashboard.

Built-in <Say> + <Gather speech> for audio. Groq for the brain. Adds:
  - live dashboard (browser): http://localhost:8000/dashboard
  - customer recognition by caller phone number
  - hours of operation (auto-close outside hours)
  - per-call JSON log with full transcript and per-turn latency

Required in .env:
  - GROQ_API_KEY
  - PUBLIC_HOST (your ngrok host, e.g. abc123.ngrok-free.app)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time as time_mod
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import Response

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import Agent  # noqa: E402
from dashboard import bus, make_router  # noqa: E402
from runtime import (  # noqa: E402
    CallLog,
    closed_message,
    is_open_now,
    load_customers,
    lookup_customer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("simple-server")


PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.environ.get("PORT", "8000"))

if not GROQ_API_KEY:
    sys.exit("ERROR: GROQ_API_KEY missing in .env")
if not PUBLIC_HOST:
    sys.exit("ERROR: PUBLIC_HOST missing in .env (e.g. abc123.ngrok-free.app)")

MENU = json.loads((ROOT / "menu.json").read_text(encoding="utf-8"))
HOURS = MENU.get("hours", {})
CUSTOMERS = load_customers(ROOT / "customers.json")
ORDERS_DIR = ROOT / "orders"
LOGS_DIR = ROOT / "logs"

# Twilio's Mexican Spanish neural voice via Amazon Polly.
TTS_VOICE = "Polly.Mia-Neural"
TTS_LANG = "es-MX"
GATHER_LANG = "es-MX"


def _greeting(customer: dict | None) -> str:
    base = f"¡Hola! Bienvenido a {MENU['restaurant_name']}."
    if customer and customer.get("name"):
        return f"¡Hola {customer['name']}! Qué gusto saludarte. ¿Te traigo lo de siempre o algo distinto hoy?"
    return f"{base} ¿Qué te gustaría ordenar?"


# In-memory state per call.
class CallState:
    __slots__ = ("agent", "log", "started_monotonic")

    def __init__(self, agent: Agent, log: CallLog) -> None:
        self.agent = agent
        self.log = log
        self.started_monotonic = time_mod.monotonic()


calls: dict[str, CallState] = {}


app = FastAPI(title="ai-phone-simple")
app.include_router(make_router(orders_dir=ORDERS_DIR, logs_dir=LOGS_DIR))


def twiml(body: str) -> Response:
    payload = f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'
    return Response(content=payload, media_type="application/xml")


def say(text: str) -> str:
    return f'<Say voice="{TTS_VOICE}" language="{TTS_LANG}">{xml_escape(text)}</Say>'


def gather() -> str:
    return (
        f'<Gather input="speech" language="{GATHER_LANG}" speechTimeout="auto" '
        f'action="https://{PUBLIC_HOST}/turn" method="POST" />'
    )


def hangup() -> str:
    return "<Hangup/>"


def _end_call(call_sid: str, *, saved: bool) -> None:
    state = calls.pop(call_sid, None)
    if state is None:
        return
    duration = round(time_mod.monotonic() - state.started_monotonic, 1)
    order_id = state.agent.last_saved_order_id
    order_total = None
    if order_id:
        # Pull total from the saved file.
        try:
            order_path = ORDERS_DIR / f"{order_id}.json"
            order_total = json.loads(order_path.read_text())["total"]
        except Exception:
            pass
    state.log.finish(order_id, order_total)
    state.log.write(LOGS_DIR)
    bus.publish({
        "type": "call_ended",
        "call_sid": call_sid,
        "duration_s": duration,
        "saved": saved,
    })
    logger.info("call %s ended (saved=%s, duration=%ss)", call_sid[:8], saved, duration)


@app.post("/voice")
async def incoming_call(
    CallSid: str = Form(...),
    From: str = Form(""),
) -> Response:
    """First webhook fired when a call comes in."""
    # Closed-hours short-circuit.
    if not is_open_now(HOURS):
        msg = closed_message(HOURS, MENU["restaurant_name"])
        logger.info("call %s outside hours, declining", CallSid[:8])
        return twiml(say(msg) + hangup())

    customer = lookup_customer(CUSTOMERS, From)
    greeting = _greeting(customer)

    agent = Agent(api_key=GROQ_API_KEY, model=GROQ_MODEL, menu=MENU)
    # Pre-seed the greeting so the agent has it as context.
    agent.history.append({"role": "assistant", "content": greeting})

    log = CallLog(
        call_sid=CallSid,
        caller_phone=From,
        customer_name=(customer or {}).get("name"),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    log.add_turn("agent", greeting)

    calls[CallSid] = CallState(agent=agent, log=log)

    bus.publish({
        "type": "call_started",
        "call_sid": CallSid,
        "from": From,
        "customer_name": (customer or {}).get("name"),
        "ts": log.started_at,
    })
    bus.publish({"type": "agent_said", "call_sid": CallSid, "text": greeting})

    logger.info("call %s started, from=%s, known=%s", CallSid[:8], From, bool(customer))
    return twiml(say(greeting) + gather() + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>')


@app.post("/turn")
async def handle_turn(
    CallSid: str = Form(...),
    SpeechResult: str = Form(""),
) -> Response:
    state = calls.get(CallSid)
    if state is None:
        logger.warning("call %s not found, hanging up", CallSid[:8])
        return twiml(hangup())

    user_text = SpeechResult.strip()
    logger.info("call %s user: %r", CallSid[:8], user_text)

    if not user_text:
        prompt = "Disculpa, no te escuché. ¿Puedes repetir?"
        bus.publish({"type": "agent_said", "call_sid": CallSid, "text": prompt})
        state.log.add_turn("agent", prompt)
        return twiml(
            say(prompt)
            + gather()
            + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>'
        )

    state.log.add_turn("user", user_text)
    bus.publish({"type": "user_said", "call_sid": CallSid, "text": user_text})

    started = time_mod.monotonic()
    first_token_ms: float | None = None
    reply_parts: list[str] = []
    async for sentence in state.agent.turn(user_text):
        if first_token_ms is None:
            first_token_ms = (time_mod.monotonic() - started) * 1000
        reply_parts.append(sentence)
        bus.publish({"type": "agent_said", "call_sid": CallSid, "text": sentence})

    reply = " ".join(reply_parts).strip() or "Perdón, no entendí, ¿me lo repites?"
    state.log.add_turn("agent", reply, latency_ms=first_token_ms)
    logger.info(
        "call %s agent: %s (ttft=%sms)",
        CallSid[:8],
        reply,
        round(first_token_ms or 0, 1),
    )

    if state.agent.last_saved_order_id:
        # Read the order back so we can announce it on the dashboard.
        try:
            order = json.loads(
                (ORDERS_DIR / f"{state.agent.last_saved_order_id}.json").read_text()
            )
            bus.publish({"type": "order_saved", "call_sid": CallSid, "order": order})
        except Exception:
            logger.exception("could not load saved order")
        _end_call(CallSid, saved=True)
        return twiml(say(reply) + hangup())

    return twiml(
        say(reply) + gather() + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>'
    )


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup_endpoint(CallSid: str = Form("")) -> Response:
    if CallSid:
        _end_call(CallSid, saved=False)
    return twiml(hangup())


@app.post("/status")
async def call_status(
    CallSid: str = Form(""),
    CallStatus: str = Form(""),
) -> Response:
    """Optional Twilio status callback: cleans up state if caller hangs up early."""
    if CallStatus in {"completed", "failed", "busy", "no-answer", "canceled"}:
        if CallSid:
            _end_call(CallSid, saved=False)
    return Response(status_code=204)


@app.get("/")
async def healthcheck() -> dict:
    return {
        "status": "ok",
        "public_host": PUBLIC_HOST,
        "calls_in_progress": len(calls),
        "is_open": is_open_now(HOURS),
        "dashboard_url": f"http://localhost:{PORT}/dashboard",
    }


if __name__ == "__main__":
    import uvicorn

    logger.info("dashboard: http://localhost:%s/dashboard", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
