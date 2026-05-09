"""Quick smoke test for the Deepgram API key.

1. Generates a Spanish audio sample with macOS `say` (voice: Paulina).
2. Posts the audio to Deepgram's REST endpoint.
3. Prints the transcript and confidence so you can confirm it understood you.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("DEEPGRAM_API_KEY", "").strip()
if not API_KEY:
    sys.exit("ERROR: DEEPGRAM_API_KEY is empty in .env")

PHRASE = "Hola, quiero dos tacos al pastor y un agua de horchata para llevar."
AUDIO_FILE = "/tmp/dg_test.aiff"

print(f'1) Generating audio with `say`:\n   "{PHRASE}"')
try:
    subprocess.run(
        ["say", "-v", "Paulina", "-o", AUDIO_FILE, PHRASE],
        check=True,
    )
except FileNotFoundError:
    sys.exit("ERROR: `say` not found (this script is macOS-only)")

print(f"2) Sending {os.path.getsize(AUDIO_FILE)} bytes to Deepgram...")
with open(AUDIO_FILE, "rb") as f:
    audio = f.read()

response = httpx.post(
    "https://api.deepgram.com/v1/listen",
    params={"model": "nova-2", "language": "es", "smart_format": "true"},
    headers={"Authorization": f"Token {API_KEY}"},
    content=audio,
    timeout=30,
)

if response.status_code != 200:
    sys.exit(f"ERROR {response.status_code}: {response.text}")

result = response.json()
alt = result["results"]["channels"][0]["alternatives"][0]
print()
print(f"  Transcript: {alt['transcript']}")
print(f"  Confidence: {alt['confidence']:.1%}")
print()
print("If the transcript matches the phrase, your key is good.")
