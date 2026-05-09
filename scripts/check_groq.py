"""End-to-end test of the Groq-backed agent (no phone, no audio).

Simulates a 3-turn conversation by typing user messages and printing
each sentence the agent streams back. Useful to verify:
  - your GROQ_API_KEY works
  - tool calling works (save_order is invoked at the end)
  - the model speaks natural Mexican Spanish
  - the order JSON is well-formed

Run:
    source .venv/bin/activate
    python scripts/check_groq.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agent import Agent  # noqa: E402


CONVERSATION = [
    "__GREETING__",
    "Hola, quiero dos tacos al pastor, uno de pollo y un agua de horchata",
    "Sí confirmo, es para llevar, mi nombre es Maria, lo paso a recoger en 20 minutos",
]


async def main() -> None:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        sys.exit("ERROR: GROQ_API_KEY is empty in .env")

    menu = json.loads((ROOT / "menu.json").read_text(encoding="utf-8"))
    agent = Agent(
        api_key=api_key,
        model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        menu=menu,
    )

    for i, user_msg in enumerate(CONVERSATION, start=1):
        label = "(call answered)" if user_msg == "__GREETING__" else user_msg
        print(f"\n--- TURN {i} ---")
        print(f"USER  > {label}")
        async for sentence in agent.turn(user_msg):
            print(f"AGENT > {sentence}")

    print("\n--- RESULT ---")
    if agent.last_saved_order_id:
        path = ROOT / "orders" / f"{agent.last_saved_order_id}.json"
        print(f"Order saved: {path}")
        print(path.read_text(encoding="utf-8"))
    else:
        print("No order was saved (the model didn't call save_order)")


if __name__ == "__main__":
    asyncio.run(main())
