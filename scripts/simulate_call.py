"""Simulate a Twilio inbound call against scripts/simple_server.py.

Walks through a full 3-turn conversation by POSTing the same form-encoded
payloads Twilio would send. Useful to demo the live dashboard without
needing a real phone, ngrok, or Twilio credits.

Run (in two terminals):

    # A — server
    python scripts/simple_server.py

    # B — simulator
    python scripts/simulate_call.py

Then open http://localhost:8000/dashboard and watch the transcripts and
order appear live.
"""
from __future__ import annotations

import asyncio
import os
import random
import string
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

PORT = int(os.environ.get("PORT", "8000"))
BASE = f"http://localhost:{PORT}"

# +528711419810 is pre-registered as "David" in customers.json,
# so the agent should greet by name.
CALLER_PHONE = "+528711419810"

CONVERSATION = [
    "Hola, quiero dos tacos al pastor, uno de pollo y un agua de horchata",
    "Sí confirmo, es para llevar, mi nombre es Maria, lo paso a recoger en 20 minutos",
]


def make_call_sid() -> str:
    return "CA" + "".join(random.choices(string.hexdigits.lower(), k=32))


async def main() -> None:
    call_sid = make_call_sid()
    print(f"call_sid: {call_sid}")
    print(f"from:     {CALLER_PHONE}")
    print()
    print(f"Open the dashboard now: {BASE}/dashboard")
    print("Pausing 3 s so you can switch tabs...")
    await asyncio.sleep(3)

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Inbound call webhook — agent greets.
        print("\n[1/3] POST /voice (call answered)")
        r = await client.post(
            f"{BASE}/voice",
            data={"CallSid": call_sid, "From": CALLER_PHONE},
        )
        r.raise_for_status()
        await asyncio.sleep(3)

        # 2. Each user turn — agent responds and (eventually) saves the order.
        for idx, user_text in enumerate(CONVERSATION, start=1):
            print(f"\n[{idx + 1}/3] POST /turn")
            print(f"        user: {user_text}")
            r = await client.post(
                f"{BASE}/turn",
                data={"CallSid": call_sid, "SpeechResult": user_text},
            )
            r.raise_for_status()
            if "<Hangup/>" in r.text:
                print("        --> server hung up (order saved)")
                break
            await asyncio.sleep(3)

    print("\nDone. Check the dashboard.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.ConnectError:
        sys.exit(
            "ERROR: could not connect to the server. "
            "Start it first with `python scripts/simple_server.py`."
        )
    except KeyboardInterrupt:
        sys.exit(0)
