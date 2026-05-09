"""Minimal Twilio-only voice agent (no Deepgram, no ElevenLabs).

Uses Twilio's built-in <Say> (Polly Mia Neural for Mexican Spanish) and
<Gather input="speech"> instead of the full Media Streams pipeline. The
brain is the same Groq-powered agent. Latency and voice quality are lower
than the production server, but this version only needs:

  - GROQ_API_KEY     (already in .env)
  - PUBLIC_HOST      (your ngrok host, e.g. abc123.ngrok-free.app)

Run:
    source .venv/bin/activate
    python scripts/simple_server.py

Then point your Twilio number's "A call comes in" webhook to:
    https://<PUBLIC_HOST>/voice
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import Response

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import Agent  # noqa: E402

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

# Twilio's Mexican Spanish neural voice via Amazon Polly.
TTS_VOICE = "Polly.Mia-Neural"
TTS_LANG = "es-MX"
GATHER_LANG = "es-MX"
GREETING = "¡Hola! Bienvenido al Restaurante Demo. ¿Qué te gustaría ordenar?"

# In-memory map: CallSid -> Agent. Fine for one-server demos; lose on restart.
agents: dict[str, Agent] = {}


app = FastAPI(title="ai-phone-simple")


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


@app.post("/voice")
async def incoming_call(CallSid: str = Form(...)) -> Response:
    """First webhook fired when a call comes in. Greets and starts gathering."""
    agent = Agent(api_key=GROQ_API_KEY, model=GROQ_MODEL, menu=MENU)
    # Pre-seed the greeting so the agent doesn't repeat it on the first turn.
    agent.history.append({"role": "assistant", "content": GREETING})
    agents[CallSid] = agent
    logger.info("call %s started", CallSid[:8])
    return twiml(say(GREETING) + gather() + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>')


@app.post("/turn")
async def handle_turn(
    CallSid: str = Form(...),
    SpeechResult: str = Form(""),
) -> Response:
    """Webhook fired after each gather. Runs one agent turn and replies."""
    agent = agents.get(CallSid)
    if agent is None:
        logger.warning("call %s not found, hanging up", CallSid[:8])
        return twiml(hangup())

    user_text = SpeechResult.strip()
    logger.info("call %s user: %r", CallSid[:8], user_text)

    if not user_text:
        return twiml(
            say("Disculpa, no te escuché. ¿Puedes repetir?")
            + gather()
            + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>'
        )

    reply_parts: list[str] = []
    async for sentence in agent.turn(user_text):
        reply_parts.append(sentence)
    reply = " ".join(reply_parts).strip() or "Perdón, no entendí, ¿me lo repites?"
    logger.info("call %s agent: %s", CallSid[:8], reply)

    # End the call once the order is saved.
    if agent.last_saved_order_id:
        agents.pop(CallSid, None)
        logger.info(
            "call %s order saved %s — hanging up",
            CallSid[:8],
            agent.last_saved_order_id,
        )
        return twiml(say(reply) + hangup())

    return twiml(
        say(reply) + gather() + f'<Redirect>https://{PUBLIC_HOST}/hangup</Redirect>'
    )


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup_endpoint() -> Response:
    return twiml(hangup())


@app.get("/")
async def healthcheck() -> dict:
    return {"status": "ok", "public_host": PUBLIC_HOST, "calls_in_progress": len(agents)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
