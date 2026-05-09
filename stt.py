"""Async client for Deepgram's streaming speech-to-text WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import websockets

logger = logging.getLogger(__name__)


class DeepgramSTT:
    """Streaming STT configured for Twilio Media Streams audio (mu-law 8kHz)."""

    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(
        self,
        api_key: str,
        language: str = "es",
        model: str = "nova-2",
    ) -> None:
        self.api_key = api_key
        self.language = language
        self.model = model
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        params = {
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "model": self.model,
            "language": self.language,
            "interim_results": "true",
            "utterance_end_ms": "1000",
            "vad_events": "true",
            "endpointing": "300",
            "smart_format": "true",
        }
        url = f"{self.URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        self.ws = await websockets.connect(
            url,
            extra_headers={"Authorization": f"Token {self.api_key}"},
        )
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            assert self.ws is not None
            async for raw in self.ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "Results":
                    alt = msg["channel"]["alternatives"][0]
                    text = alt.get("transcript", "")
                    if text:
                        await self._queue.put({
                            "type": "transcript",
                            "text": text,
                            "is_final": msg.get("is_final", False),
                            "speech_final": msg.get("speech_final", False),
                        })
                elif kind == "SpeechStarted":
                    await self._queue.put({"type": "speech_started"})
                elif kind == "UtteranceEnd":
                    await self._queue.put({"type": "utterance_end"})
        except websockets.ConnectionClosed:
            logger.debug("Deepgram connection closed")
        except Exception:
            logger.exception("Deepgram reader failed")
        finally:
            await self._queue.put(None)

    async def send_audio(self, mulaw_bytes: bytes) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.send(mulaw_bytes)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                break
            yield evt

    async def close(self) -> None:
        if self.ws and not self.ws.closed:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            await self.ws.close()
        if self._reader_task:
            self._reader_task.cancel()
