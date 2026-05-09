"""FastAPI server that bridges Twilio Media Streams to Deepgram, Claude, and ElevenLabs."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from agent import Agent
from config import Settings, load_settings
from stt import DeepgramSTT
from tts import ElevenLabsTTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ai-phone")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load settings and menu once at startup; fail fast on bad config."""
    app.state.settings = load_settings()
    app.state.menu = json.loads(
        (Path(__file__).parent / "menu.json").read_text(encoding="utf-8")
    )
    logger.info("startup ok — public_host=%s", app.state.settings.public_host)
    yield


app = FastAPI(title="ai-phone", lifespan=lifespan)


@app.get("/")
async def healthcheck() -> dict:
    return {
        "status": "ok",
        "service": "ai-phone",
        "public_host": app.state.settings.public_host,
    }


@app.api_route("/voice", methods=["GET", "POST"])
async def voice() -> Response:
    """Twilio webhook for incoming calls.

    Returns TwiML that opens a bidirectional Media Stream to /media.
    """
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="wss://{app.state.settings.public_host}/media" />'
        "</Connect>"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media")
async def media(ws: WebSocket) -> None:
    """Handle one Twilio Media Streams session for the duration of a call."""
    await ws.accept()
    logger.info("websocket accepted")

    settings: Settings = ws.app.state.settings
    menu: dict = ws.app.state.menu

    stt = DeepgramSTT(
        api_key=settings.deepgram_api_key,
        language=settings.stt_language,
        model=settings.stt_model,
    )
    tts = ElevenLabsTTS(
        api_key=settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model,
    )
    agent = Agent(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        menu=menu,
    )

    stream_sid: Optional[str] = None
    out_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    speaking = False
    response_task: Optional[asyncio.Task] = None

    async def send_audio_loop() -> None:
        """Forward audio chunks from out_queue to Twilio as base64 media events."""
        while True:
            chunk = await out_queue.get()
            if chunk is None:
                return
            if not chunk or stream_sid is None:
                continue
            try:
                await ws.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                })
            except Exception:
                logger.exception("failed to send audio frame")
                return

    async def respond(user_text: str) -> None:
        """Run one agent turn and stream the spoken response."""
        nonlocal speaking
        speaking = True
        try:
            async for sentence in agent.turn(user_text):
                logger.info("agent: %s", sentence)
                async for audio_chunk in tts.synthesize(sentence):
                    await out_queue.put(audio_chunk)
        except asyncio.CancelledError:
            logger.info("agent response cancelled")
            raise
        except Exception:
            logger.exception("agent response failed")
        finally:
            speaking = False

    def drain_out_queue() -> None:
        while not out_queue.empty():
            try:
                out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def handle_stt_events() -> None:
        """Translate Deepgram events into agent turns and barge-in handling."""
        nonlocal response_task, speaking
        async for evt in stt.events():
            kind = evt["type"]
            if kind == "speech_started" and speaking:
                logger.info("barge-in detected, cancelling agent response")
                if response_task and not response_task.done():
                    response_task.cancel()
                drain_out_queue()
                if stream_sid:
                    try:
                        await ws.send_json({"event": "clear", "streamSid": stream_sid})
                    except Exception:
                        pass
                speaking = False
            elif kind == "transcript" and evt.get("speech_final"):
                text = evt["text"].strip()
                if not text:
                    continue
                logger.info("user: %s", text)
                if response_task and not response_task.done():
                    response_task.cancel()
                response_task = asyncio.create_task(respond(text))

    sender_task = asyncio.create_task(send_audio_loop())
    stt_task: Optional[asyncio.Task] = None

    try:
        await stt.connect()
        stt_task = asyncio.create_task(handle_stt_events())

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")
            if event == "connected":
                logger.info("call connected")
            elif event == "start":
                stream_sid = msg["start"]["streamSid"]
                call_sid = msg["start"].get("callSid")
                logger.info("call start streamSid=%s callSid=%s", stream_sid, call_sid)
                response_task = asyncio.create_task(respond("__GREETING__"))
            elif event == "media":
                audio = base64.b64decode(msg["media"]["payload"])
                await stt.send_audio(audio)
            elif event == "stop":
                logger.info("call stop")
                break
    except WebSocketDisconnect:
        logger.info("websocket disconnected")
    except Exception:
        logger.exception("websocket handler failed")
    finally:
        for task in (response_task, stt_task):
            if task and not task.done():
                task.cancel()
        await stt.close()
        await out_queue.put(None)
        try:
            await sender_task
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    # Pre-load settings here so misconfig fails before the server boots.
    boot_settings = load_settings()
    uvicorn.run(app, host="0.0.0.0", port=boot_settings.port)
