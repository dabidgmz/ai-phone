"""Centralised configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str

    deepgram_api_key: str
    anthropic_api_key: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model: str

    anthropic_model: str
    stt_language: str
    stt_model: str

    public_host: str
    port: int


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def load_settings() -> Settings:
    return Settings(
        twilio_account_sid=_required("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_required("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=_required("TWILIO_PHONE_NUMBER"),
        deepgram_api_key=_required("DEEPGRAM_API_KEY"),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        elevenlabs_api_key=_required("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=os.environ.get(
            "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
        ),
        elevenlabs_model=os.environ.get(
            "ELEVENLABS_MODEL", "eleven_flash_v2_5"
        ),
        anthropic_model=os.environ.get(
            "ANTHROPIC_MODEL", "claude-haiku-4-5"
        ),
        stt_language=os.environ.get("STT_LANGUAGE", "es"),
        stt_model=os.environ.get("STT_MODEL", "nova-2"),
        public_host=_required("PUBLIC_HOST"),
        port=int(os.environ.get("PORT", "8000")),
    )
