"""Streaming text-to-speech via ElevenLabs, emitting Twilio-ready audio."""
from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# 20ms of mu-law @ 8kHz = 160 bytes. This is Twilio's natural frame size.
TWILIO_FRAME_BYTES = 160


class ElevenLabsTTS:
    """Streams synthesised mu-law 8kHz audio chunks for Twilio Media Streams."""

    BASE_URL = "https://api.elevenlabs.io/v1"

    def __init__(
        self,
        api_key: str,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model: str = "eleven_flash_v2_5",
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mu-law 8kHz audio frames for the given text."""
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}/stream"
        params = {
            "output_format": "ulaw_8000",
            "optimize_streaming_latency": "3",
        }
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }
        body = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", url, params=params, headers=headers, json=body
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"ElevenLabs returned {response.status_code}: {detail}"
                    )
                # Re-chunk into Twilio-sized frames so the receiver paces correctly.
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    while len(buffer) >= TWILIO_FRAME_BYTES:
                        yield buffer[:TWILIO_FRAME_BYTES]
                        buffer = buffer[TWILIO_FRAME_BYTES:]
                if buffer:
                    yield buffer
