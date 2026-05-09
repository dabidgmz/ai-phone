"""Conversation agent backed by Groq with a save_order tool."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from groq import AsyncGroq

from prompts import build_system_prompt

logger = logging.getLogger(__name__)


SAVE_ORDER_TOOL = {
    "type": "function",
    "function": {
        "name": "save_order",
        "description": (
            "Persist a confirmed order. Call this ONLY after the customer has "
            "explicitly confirmed all items, the mode (pickup or dine_in), their "
            "name, and the total."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Menu item id from the system prompt",
                            },
                            "name": {"type": "string"},
                            "qty": {"type": "integer", "minimum": 1},
                            "unit_price": {"type": "number"},
                            "notes": {"type": "string"},
                        },
                        "required": ["id", "name", "qty", "unit_price"],
                    },
                },
                "mode": {"type": "string", "enum": ["pickup", "dine_in"]},
                "customer_name": {"type": "string"},
                "customer_phone": {"type": "string"},
                "total": {"type": "number"},
                "pickup_time": {
                    "type": "string",
                    "description": "If pickup, estimated time (e.g. '14:30' or 'in 30 minutes')",
                },
                "notes": {"type": "string"},
            },
            "required": ["items", "mode", "customer_name", "total"],
        },
    },
}


# Match a sentence ending in terminal punctuation followed by whitespace/end.
# Note: Spanish '¿' and '¡' open a sentence — they must NOT split it.
SENTENCE_RE = re.compile(r".+?[.!?\n]+(?:\s|$)", re.DOTALL)


class Agent:
    """Stateful conversation agent for one phone call."""

    def __init__(self, api_key: str, model: str, menu: dict) -> None:
        self.client = AsyncGroq(api_key=api_key)
        self.model = model
        self.system = build_system_prompt(menu)
        self.history: list[dict] = []
        self.last_saved_order_id: Optional[str] = None

    async def turn(self, user_text: str) -> AsyncIterator[str]:
        """Run one conversation turn, yielding sentences for streaming TTS."""
        if user_text == "__GREETING__":
            self.history.append({
                "role": "user",
                "content": (
                    "[The customer just answered the phone. Greet them briefly "
                    "and ask what they'd like to order.]"
                ),
            })
        else:
            self.history.append({"role": "user", "content": user_text})

        # Loop to handle tool-use round-trips: the model emits a tool_call,
        # we run it, append a tool message, then call the model again so it
        # can produce a follow-up reply for the caller.
        while True:
            buffer = ""
            full_content = ""
            tool_calls_acc: dict[int, dict] = {}

            messages = [{"role": "system", "content": self.system}] + self.history

            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=[SAVE_ORDER_TOOL],
                tool_choice="auto",
                stream=True,
                max_tokens=400,
                temperature=0.5,
            )

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    buffer += delta.content
                    full_content += delta.content
                    # Yield each complete sentence so TTS can start speaking
                    # before the model has finished generating.
                    while True:
                        match = SENTENCE_RE.match(buffer)
                        if not match:
                            break
                        sentence = match.group(0).strip()
                        if sentence:
                            yield sentence
                        buffer = buffer[match.end():]

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_calls_acc.setdefault(idx, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments

            # Flush any trailing text that didn't end with punctuation.
            tail = buffer.strip()
            if tail:
                yield tail

            tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

            assistant_msg: dict = {
                "role": "assistant",
                "content": full_content if full_content else None,
            }
            if tool_calls_list:
                assistant_msg["tool_calls"] = tool_calls_list
            self.history.append(assistant_msg)

            if not tool_calls_list:
                break

            # Run each tool call and append a `tool` message with its result.
            for tc in tool_calls_list:
                if tc["function"]["name"] == "save_order":
                    raw_args = tc["function"]["arguments"]
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        logger.error("save_order arguments not valid JSON: %r", raw_args)
                        result = {"ok": False, "error": "invalid_arguments"}
                    else:
                        logger.info("save_order called: %s", json.dumps(args))
                        result = self._save_order(args)
                else:
                    result = {"ok": False, "error": f"unknown_tool:{tc['function']['name']}"}

                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })
            # Loop again so the model produces a follow-up message.

    def _save_order(self, payload: dict) -> dict:
        order_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        record = dict(payload)
        record["order_id"] = order_id
        record["created_at"] = datetime.now().isoformat()
        out_dir = Path(__file__).parent / "orders"
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"{order_id}.json"
        path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.last_saved_order_id = order_id
        logger.info("order saved: %s", path)
        return {"ok": True, "order_id": order_id}
