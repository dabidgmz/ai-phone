"""Conversation agent backed by Claude with a save_order tool."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from anthropic import AsyncAnthropic

from prompts import build_system_prompt

logger = logging.getLogger(__name__)


SAVE_ORDER_TOOL = {
    "name": "save_order",
    "description": (
        "Persist a confirmed order. Call this ONLY after the customer has "
        "explicitly confirmed all items, the mode (pickup or dine_in), their "
        "name, and the total."
    ),
    "input_schema": {
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
}


# Match a sentence ending in punctuation followed by whitespace/end-of-string.
SENTENCE_RE = re.compile(r".+?[.!?¿¡\n]+(?:\s|$)", re.DOTALL)


class Agent:
    """Stateful conversation agent for one phone call."""

    def __init__(self, api_key: str, model: str, menu: dict) -> None:
        self.client = AsyncAnthropic(api_key=api_key)
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

        # Loop to handle tool-use round-trips: model calls tool -> we run it ->
        # we feed the result back -> model produces a follow-up message.
        while True:
            buffer = ""
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=400,
                system=self.system,
                tools=[SAVE_ORDER_TOOL],
                messages=self.history,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        buffer += event.delta.text
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

                final = await stream.get_final_message()

            # Flush any trailing text that didn't end with punctuation.
            tail = buffer.strip()
            if tail:
                yield tail

            # Persist the assistant turn (text + tool_use blocks) for context.
            self.history.append({
                "role": "assistant",
                "content": [block.model_dump() for block in final.content],
            })

            # Run any tool calls the model emitted.
            tool_results = []
            for block in final.content:
                if block.type == "tool_use" and block.name == "save_order":
                    logger.info("save_order called: %s", json.dumps(block.input))
                    result = self._save_order(block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            if not tool_results:
                break

            self.history.append({"role": "user", "content": tool_results})
            # Loop again so the model produces a follow-up message after the tool call.

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
