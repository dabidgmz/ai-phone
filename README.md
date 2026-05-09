<div align="center">

# ai-phone

**An AI receptionist that answers your restaurant's phone in Mexican Spanish, takes the order, and saves it as JSON ready for the POS.**

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)
![Twilio](https://img.shields.io/badge/Twilio-Media%20Streams-F22F46?style=flat-square&logo=twilio&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-Llama%203.3%2070B-FF6B35?style=flat-square)
![Deepgram](https://img.shields.io/badge/Deepgram-nova--2-13EF93?style=flat-square)
![ElevenLabs](https://img.shields.io/badge/ElevenLabs-Flash%20v2.5-000?style=flat-square)

</div>

---

## What it does

A real customer dials a phone number. The AI:

1. **Picks up on the first ring.**
2. **Greets** them in natural Mexican Spanish.
3. **Walks them through the menu**, suggests alternatives if something is missing.
4. **Confirms** items, total, pickup-vs-dine-in, customer name, and pickup time.
5. **Saves the order** as a structured JSON file, ready to drop into a POS.
6. **Hangs up cleanly.**

The MVP runs on your laptop and is reachable from a real phone through Twilio + ngrok. Once it feels right, the same code is meant to be deployed behind a stable URL and wired into a POS database directly.

---

## Quick start (5 minutes, no Deepgram or ElevenLabs needed)

You can place a real phone call to your AI **right now** using only Groq + Twilio + ngrok.

```
                  ┌──────────────┐
   Phone call ──> │   Twilio     │ ──► Built-in TTS (Polly Mia)
                  │ <Say>+<Gather│ <── Built-in STT (Spanish)
                  └──────┬───────┘
                         │ webhook
                         ▼
                  ┌──────────────┐
                  │  FastAPI     │  scripts/simple_server.py
                  │  /voice      │
                  │  /turn       │
                  └──────┬───────┘
                         │
                         ▼
                  ┌──────────────┐
                  │     Groq     │  Llama 3.3 70B + save_order
                  └──────┬───────┘
                         │
                         ▼
                    orders/*.json
```

### Step 1 — Install

```bash
cd /Users/davidabasa/Documents/ai-phone
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ngrok/ngrok/ngrok
```

### Step 2 — Get one API key

Create a free account at **<https://console.groq.com>**, generate an API key (`gsk_...`), and paste it into `.env`:

```bash
cp .env.example .env
# open .env and fill in:
#   GROQ_API_KEY=gsk_...
```

### Step 3 — Tunnel

In **terminal A**:

```bash
ngrok config add-authtoken <your-ngrok-token>   # one-time
ngrok http 8000
```

Copy the host (e.g. `abc-123.ngrok-free.app`) into `.env`:

```
PUBLIC_HOST=abc-123.ngrok-free.app
```

### Step 4 — Run the server

In **terminal B**:

```bash
source .venv/bin/activate
python scripts/simple_server.py
```

You should see:

```
INFO simple-server: Uvicorn running on http://0.0.0.0:8000
```

### Step 5 — Wire Twilio

In Twilio Console &rarr; **Phone Numbers** &rarr; **Active Numbers** &rarr; your number:

| Field | Value |
|---|---|
| **A call comes in** | `Webhook` |
| **URL** | `https://abc-123.ngrok-free.app/voice` |
| **Method** | `POST` |

Save.

### Step 6 — Call your number

Dial **+1 (315) 713-7382** from any phone. You should hear:

> *"¡Hola! Bienvenido al Restaurante Demo. ¿Qué te gustaría ordenar?"*

Pedí dos tacos al pastor, confirma para llevar, da tu nombre. Mira el JSON aparecer en `orders/`.

---

## Production stack

The Quick Start uses Twilio's built-in `<Say>` + `<Gather>`. It works, but the voice is robotic and turns are 2&ndash;4 s. The **production server** (`server.py`) replaces those with streaming services for sub-second turns and natural voice.

```
                       ┌──────────────────┐
   Phone call ─────────│      Twilio      │
                       │  Media Streams   │
                       └────────┬─────────┘
                                │ μ-law 8kHz WebSocket
                                ▼
                       ┌──────────────────┐
                       │  FastAPI server  │  server.py
                       │       /media     │
                       └────────┬─────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ┌─────────────┐         ┌─────────────┐         ┌─────────────┐
 │  Deepgram   │         │    Groq     │         │ ElevenLabs  │
 │   nova-2    │ ─text─► │ Llama 3.3   │ ─text─► │  Flash v2.5 │
 │ streaming   │         │   70B       │         │  streaming  │
 │   STT       │         │   + tools   │         │     TTS     │
 └─────────────┘         └──────┬──────┘         └─────────────┘
                                │
                                ▼
                          orders/*.json
```

Audio is **&micro;-law 8 kHz end-to-end** &mdash; Twilio Media Streams emits it natively, Deepgram accepts it as input, ElevenLabs can output it directly. No resampling, no extra CPU.

While the agent is talking, STT keeps listening. If Deepgram fires `speech_started`, the server sends `clear` to Twilio (drops the playback buffer) and cancels the in-flight LLM/TTS work &mdash; so callers can interrupt the agent like a human ("**barge-in**").

To run this version you need three more API keys (see [Full setup](#full-setup) below) and use `python server.py` instead of `scripts/simple_server.py`.

---

## Project layout

```
ai-phone/
├── server.py                ◄ Production: FastAPI + Twilio Media Streams + barge-in
├── agent.py                 ◄ Groq streaming agent + save_order tool
├── stt.py                   ◄ Deepgram WebSocket client (μ-law 8kHz)
├── tts.py                   ◄ ElevenLabs streaming HTTP client (μ-law 8kHz)
├── prompts.py               ◄ System prompt builder
├── config.py                ◄ Settings dataclass + env validation
├── menu.json                ◄ Editable menu — items, prices, currency
├── orders/                  ◄ Saved orders land here as <timestamp>.json
├── scripts/
│   ├── simple_server.py     ◄ Twilio-only fallback (no Deepgram/ElevenLabs)
│   ├── check_groq.py        ◄ End-to-end agent smoke test
│   └── check_deepgram.py    ◄ Verify Deepgram key with macOS `say`
├── requirements.txt
├── .env.example             ◄ Template — copy to .env and fill in
└── .gitignore
```

> Each module owns one responsibility. `server.py` is the only place that knows about the Twilio wire format. `agent.py` is the only place that knows about Groq.

---

## Why this stack

| Component | Choice | Why |
|---|---|---|
| **Telephony** | Twilio Media Streams | Bidirectional WebSocket with raw audio. Far lower latency than the older `<Gather>` TwiML loop. |
| **STT** | Deepgram `nova-2` | Native μ-law 8 kHz support, sub-300 ms partials, $200 free credit, built-in endpointing. |
| **LLM** | Groq Llama 3.3 70B | The fastest hosted inference on the market (~500 tok/s). Generous free tier. OpenAI-compatible tool use. |
| **TTS** | ElevenLabs Flash v2.5 | Natural Mexican Spanish voices and `ulaw_8000` direct output. ~200 ms first-byte latency. |
| **Server** | FastAPI + uvicorn | Native async, native WebSockets, simple lifespan for config. |
| **Tunnel (dev)** | ngrok | Public HTTPS URL pointing at `localhost:8000` so Twilio can reach the dev box. |

---

## Full setup

For the production server (`server.py`) you need four accounts. The Quick Start above only uses **Groq**.

### 1. Create accounts

| Service | URL | What you copy | Free tier |
|---|---|---|---|
| Twilio | <https://console.twilio.com> | Account SID, Auth Token, phone number | $15 trial |
| Groq | <https://console.groq.com> | API key (`gsk_...`) | Yes, generous |
| Deepgram | <https://console.deepgram.com> | API key | $200 credit |
| ElevenLabs | <https://elevenlabs.io> | API key + voice ID for Spanish | 10k chars/mo |

> **Never paste API keys into chat or commit them.** They go in `.env`, which is in `.gitignore`.

### 2. Fill `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in **every** value, including `PUBLIC_HOST` from ngrok.

### 3. Run

```bash
ngrok http 8000                            # terminal A
python server.py                           # terminal B (with venv active)
```

Point Twilio's webhook at `https://<ngrok-host>/voice` and call the number.

---

## Configuration

All knobs live in `.env`.

### Required

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | From Twilio Console |
| `TWILIO_AUTH_TOKEN` | From Twilio Console |
| `TWILIO_PHONE_NUMBER` | E.164 format, e.g. `+13157137382` |
| `GROQ_API_KEY` | From console.groq.com |
| `DEEPGRAM_API_KEY` | From console.deepgram.com (only for `server.py`) |
| `ELEVENLABS_API_KEY` | From elevenlabs.io (only for `server.py`) |
| `PUBLIC_HOST` | ngrok host without scheme, e.g. `abc-123.ngrok-free.app` |

### Optional (with sensible defaults)

| Variable | Default | Notes |
|---|---|---|
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Best at tool use on Groq. `llama-3.1-8b-instant` is faster but worse at tools. |
| `ELEVENLABS_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` | Pick another from <https://elevenlabs.io/app/voice-library> (filter by Spanish). |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | Lowest latency. `eleven_turbo_v2_5` is slightly higher quality. |
| `STT_LANGUAGE` | `es` | Try `es-419` or `es-MX` if recognition is off. |
| `STT_MODEL` | `nova-2` | Deepgram model. |
| `PORT` | `8000` | Local server port. |

---

## Customising the agent

### Menu

Edit `menu.json` and restart the server. The prompt is rebuilt from the menu on every connection.

```json
{
  "restaurant_name": "Tu Restaurante",
  "currency": "MXN",
  "categories": [
    {
      "name": "Tacos",
      "items": [
        { "id": "taco_pastor", "name": "Taco al pastor", "price": 25.0 }
      ]
    }
  ]
}
```

### Tone, language, rules

Edit `prompts.py` &rarr; `build_system_prompt`. Instructions are written in English (the model follows English instructions slightly better) but the agent is told to **respond in Mexican Spanish**.

### Voice

Pick any voice from the [ElevenLabs voice library](https://elevenlabs.io/app/voice-library) (filter by Spanish), copy its ID into `ELEVENLABS_VOICE_ID`, restart.

---

## Verification scripts

Located under `scripts/`. All assume `.venv` is active.

| Command | What it checks |
|---|---|
| `python scripts/check_groq.py` | End-to-end agent: greeting + ordering + `save_order` tool call. **Run this first.** |
| `python scripts/check_deepgram.py` | Generates Spanish audio with macOS `say` and posts to Deepgram. Verifies the key. |
| `python scripts/simple_server.py` | Twilio-only voice agent (no Deepgram/ElevenLabs). For the 5-min quick start. |

---

## Cost per minute (estimated)

| Component | Cost |
|---|---|
| Twilio inbound (US) | ~$0.013 |
| Deepgram nova-2 | ~$0.0043 |
| Groq Llama 3.3 70B | ~$0.001&ndash;0.003 _(free tier covers most testing)_ |
| ElevenLabs Flash v2.5 | ~$0.05&ndash;0.10 |
| **Total** | **~$0.07&ndash;0.12 / minute** |

> **Set a Twilio billing alert** (Console &rarr; Billing &rarr; Manage billing alerts) at **$10&ndash;20 USD** while testing. Same for ElevenLabs if it supports caps.

---

## Roadmap

- [ ] **POS integration** &mdash; replace `Agent._save_order()` (which writes JSON) with a call into the POS database, so orders show up on the kitchen tablet automatically.
- [ ] **Hang up cleanly** &mdash; call Twilio's REST API to end the call after `save_order`, so the customer doesn't sit on dead air.
- [ ] **Per-call metrics** &mdash; STT-to-LLM-to-TTS latency, sentence count, abandonment rate.
- [ ] **Robustness** &mdash; Deepgram occasionally fires `speech_started` on background noise; tighten the barge-in threshold.
- [ ] **Multilingual fallback** &mdash; detect English-speaking callers and switch.
- [ ] **Production deployment** &mdash; Docker image hosted on Fly/Render/Cloud Run with a stable domain instead of ngrok.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Missing required environment variable: X` | Copy `.env.example` to `.env`; fill **every** value, including `PUBLIC_HOST` from ngrok. |
| Twilio webhook fails immediately | URL must be HTTPS and reachable. Check `ngrok` is running and the URL in Twilio matches exactly. |
| Agent picks up but is silent | ElevenLabs key invalid, or `output_format=ulaw_8000` was rejected. Check server logs for `ElevenLabs returned 4xx`. |
| Agent doesn't understand you | Try `STT_LANGUAGE=es-419` or `es-MX`. Verify Twilio audio is reaching Deepgram in the logs. |
| High latency (>2 s per turn) | Confirm `llama-3.3-70b-versatile` and `eleven_flash_v2_5` are active. Use an ngrok region close to you. |
| Robotic / unnatural voice | You're using `simple_server.py` (Twilio Polly). Switch to `server.py` with ElevenLabs. |
| `Form data requires "python-multipart"` | `pip install -r requirements.txt` again to pick up the dep. |

---

## Security notes

- `.env` is in `.gitignore`. **Never paste secrets into chat, issues, or commits.**
- Use **API keys** (revocable per-key) rather than the master Auth Token in production. Twilio Console &rarr; Account &rarr; API keys & tokens &rarr; **Create**.
- **Rotate** any credential that has been pasted somewhere it shouldn't have been &mdash; the old one is dead the moment you create a replacement.
- Set spending caps on every paid service while testing.

---

<div align="center">

Built with care for restaurants that want to stop missing calls.

</div>
