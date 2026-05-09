# ai-phone

An AI phone receptionist that answers calls to a Mexican restaurant, takes orders by voice in Spanish, and saves them as structured JSON ready to drop into a POS system.

## Goal

Replace the manual "someone has to pick up the phone" step in a small restaurant. The agent should:

1. Answer every incoming call within one ring.
2. Greet the customer in natural Mexican Spanish.
3. Walk them through the menu, take their order, suggest alternatives if they ask for something off-menu.
4. Confirm items, total, pickup-vs-dine-in, and the customer's name.
5. Save a clean order record (`orders/<timestamp>.json`) the kitchen / POS can act on.
6. Hand off cleanly — no infinite loops, no robotic dead-air.

The MVP runs locally on your laptop and is reachable from a real phone number through Twilio + ngrok. When the flow feels right, the same code is meant to be deployed behind a stable URL and wired into the POS database directly.

## How a call flows

```
 Caller dials +1 (315) 713-7382
   ↓
 Twilio (telephony)
   ↓ TwiML <Connect><Stream>
 FastAPI WebSocket  (server.py)
   ↓ μ-law audio frames (8 kHz)
 Deepgram streaming STT  (stt.py)              ← speech → text in real time
   ↓ final transcript
 Claude with save_order tool  (agent.py)       ← decides what to say + when to save
   ↓ token stream, split per sentence
 ElevenLabs streaming TTS  (tts.py)            ← text → μ-law audio
   ↓ μ-law frames
 FastAPI WebSocket
   ↓
 Twilio → caller hears the agent
```

Audio is **μ-law 8 kHz end-to-end**. Twilio Media Streams emits this format natively, Deepgram accepts it as input, and ElevenLabs can output it directly — no resampling, no extra CPU.

While the agent is talking, the STT keeps listening. If Deepgram fires `speech_started`, the server sends `clear` to Twilio (drops the playback buffer) and cancels the in-flight LLM/TTS work — so callers can interrupt the agent like they would a human ("barge-in").

## Why this stack

| Component | Choice | Why |
|---|---|---|
| Telephony | **Twilio Media Streams** | Bidirectional WebSocket with raw audio. Far lower latency than the older `<Gather>` TwiML loop. |
| STT | **Deepgram nova-2** | First-class μ-law 8 kHz support, sub-300 ms partials, free $200 credit. |
| LLM | **Claude Haiku 4.5** | Cheap, fast, follows tool-use instructions reliably. Easy to swap to Sonnet 4.6 for harder dialogues. |
| TTS | **ElevenLabs Flash v2.5** | Natural Spanish voices and `ulaw_8000` output, ~200 ms first-byte latency. |
| Server | **FastAPI + uvicorn** | Native async + WebSockets, simple lifespan for config. |
| Tunnel (dev) | **ngrok** | Public HTTPS URL pointing at `localhost:8000` so Twilio can reach the dev box. |

## Project layout

```
ai-phone/
├── server.py        # FastAPI app: /voice webhook + /media WebSocket + barge-in
├── agent.py         # Claude streaming agent + save_order tool
├── stt.py           # Deepgram WebSocket client (mu-law 8kHz)
├── tts.py           # ElevenLabs streaming HTTP client (mu-law 8kHz)
├── prompts.py       # System-prompt builder (English instructions, Spanish output)
├── config.py        # Settings dataclass + env-var validation
├── menu.json        # Editable menu — items, prices, currency
├── orders/          # Saved orders end up here as <timestamp>.json
├── requirements.txt
├── .env.example     # Template; copy to .env and fill in
└── .gitignore
```

Each module has one responsibility. `server.py` is the only place that knows about Twilio's wire protocol. `agent.py` is the only place that knows about Claude.

## Setup

### 1. Get API keys

Create accounts and grab API keys for:

| Service | URL | What you need |
|---|---|---|
| Twilio | https://console.twilio.com | Account SID, Auth Token, phone number |
| Deepgram | https://console.deepgram.com | API key (ships with $200 free credit) |
| Anthropic | https://console.anthropic.com | API key (top up ~$5 to start) |
| ElevenLabs | https://elevenlabs.io | API key + voice ID for Spanish |

> **Never paste API keys into chat or commit them.** They go in `.env`, which is in `.gitignore`.

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in every value. Leave `PUBLIC_HOST` blank for now — you'll set it after starting ngrok.

### 3. Install

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Expose your local server

In a second terminal:

```bash
brew install ngrok/ngrok/ngrok            # one-time
ngrok config add-authtoken <token>        # from ngrok.com
ngrok http 8000
```

Copy the `https://<subdomain>.ngrok-free.app` host (no scheme, no path) into `.env` as `PUBLIC_HOST`.

### 5. Point Twilio at your server

Twilio Console → **Phone Numbers** → **Active Numbers** → click your number:

- **A call comes in** → `Webhook`
- **URL** → `https://<your-ngrok-host>/voice`
- **Method** → `POST`
- Save.

### 6. Run

```bash
source .venv/bin/activate
python server.py
```

You should see:
```
INFO ai-phone: startup ok — public_host=<your-ngrok-host>
```

### 7. Call it

Dial your Twilio number from any phone. The agent should pick up, greet you, and start taking your order.

Confirmed orders land in `orders/<YYYYMMDD_HHMMSS>.json`, e.g.:

```json
{
  "items": [
    {"id": "taco_pastor", "name": "Taco al pastor", "qty": 3, "unit_price": 25.0}
  ],
  "mode": "pickup",
  "customer_name": "Juan",
  "total": 75.0,
  "pickup_time": "en 30 minutos",
  "order_id": "20260509_142233",
  "created_at": "2026-05-09T14:22:33.123456"
}
```

## Customising the agent

### Menu

Edit `menu.json` and restart the server. The prompt is rebuilt from the menu every connection, so the agent always knows what's available.

```json
{
  "restaurant_name": "Tu Restaurante",
  "currency": "MXN",
  "categories": [
    { "name": "Tacos", "items": [{ "id": "taco_pastor", "name": "Taco al pastor", "price": 25.0 }] }
  ]
}
```

### Tone, language, rules

Edit `prompts.py` → `build_system_prompt`. Instructions are in English (Claude follows English instructions slightly better), but the agent is told to **respond in Mexican Spanish**.

### Voice

Pick any voice from https://elevenlabs.io/app/voice-library (filter by Spanish), copy its ID into `ELEVENLABS_VOICE_ID` in `.env`, restart.

### Models

`.env` knobs:
- `ANTHROPIC_MODEL` — default `claude-haiku-4-5`. Use `claude-sonnet-4-6` if the agent struggles with edge cases.
- `ELEVENLABS_MODEL` — default `eleven_flash_v2_5` (lowest latency). `eleven_turbo_v2_5` is slightly higher quality, slightly slower.
- `STT_LANGUAGE` — default `es`. Try `es-419` or `es-MX` if recognition is off.

## Cost per minute (rough)

| Component | Cost |
|---|---|
| Twilio inbound (US) | ~$0.013 |
| Deepgram nova-2 | ~$0.0043 |
| Claude Haiku 4.5 | ~$0.005–0.01 |
| ElevenLabs Flash v2.5 | ~$0.05–0.10 |
| **Total** | **~$0.08–0.13 / minute** |

> Set a billing alert in Twilio (Console → Billing → Manage billing alerts) at $10–20 USD while testing. Same for the LLM/TTS providers if they support spend caps.

## Roadmap

- [ ] **POS integration** — replace `Agent._save_order()` (which writes JSON) with a call into the POS Comida database, so orders show up on the kitchen tablet automatically.
- [ ] **Hang up cleanly** — call Twilio's REST API to end the call after `save_order`, so the customer doesn't sit on dead air.
- [ ] **Per-call metrics** — STT-to-LLM-to-TTS latency, sentence count, abandonment rate.
- [ ] **Robustness** — Deepgram occasionally fires `speech_started` on background noise; tighten the barge-in threshold.
- [ ] **Multilingual fallback** — detect English speakers and switch.
- [ ] **Production deployment** — Docker image, hosted on Fly/Render/Cloud Run with a stable domain instead of ngrok.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Missing required environment variable: X` | Copy `.env.example` to `.env`; fill **every** value, including `PUBLIC_HOST` from ngrok. |
| Twilio webhook fails immediately | URL must be HTTPS and reachable. Check `ngrok` is running and the URL in Twilio matches. |
| Agent picks up but is silent | ElevenLabs key invalid, or `output_format=ulaw_8000` was rejected. Check server logs for `ElevenLabs returned 4xx`. |
| Agent doesn't understand you | Try `STT_LANGUAGE=es-419` or `es-MX`. Check Twilio audio actually reaches Deepgram in the logs. |
| High latency | Confirm `claude-haiku-4-5` and `eleven_flash_v2_5` are active. Use an ngrok region close to you. |
| Robotic / unnatural voice | Switch `ELEVENLABS_VOICE_ID` to a Spanish voice from the ElevenLabs voice library. |

## Security notes

- `.env` is in `.gitignore`. Don't paste secrets into chat, issues, or commits.
- Use **API keys** (revocable) rather than the master Auth Token in production. Twilio Console → Account → API keys & tokens → Create.
- Rotate any credential that has ever been pasted somewhere it shouldn't have been — the old one is dead the moment you create a replacement.
