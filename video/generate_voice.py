#!/usr/bin/env python3
"""Generate ElevenLabs narration for the Tycho demo video."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_ROOT = "https://api.elevenlabs.io"
MODEL_ID = "eleven_multilingual_v2"
VOICE_ID = "iP95p4xoKVk53GoZ742B"  # Chris
OUTPUT_FORMAT = "mp3_44100_128"
SAMPLE_RATE = 48_000
TARGET_LUFS = -17.5
VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.75,
    "style": 0.05,
    "use_speaker_boost": True,
    "speed": 1.05,
}

STEMS: tuple[tuple[str, str], ...] = (
    # Section 1: The problem
    (
        "01-problem-hook",
        "Every coding agent starts cold. It doesn't know that a price doubled, "
        "that a deprecation was confirmed, or that a market pattern was rejected "
        "for lacking independent evidence.",
    ),
    (
        "02-problem-close",
        "Tycho is a fleet of four Gemini agents that maintains a living, "
        "evidenced theory of each competitor — autonomously.",
    ),
    # Section 2: How it works
    (
        "03-watch",
        "Sources are watched nightly by Cloud Scheduler and Cloud Run. "
        "Immutable observations land in BigQuery and Cloud Storage — "
        "append only, never rewritten.",
    ),
    (
        "04-differ",
        "A hash gate stops unchanged content before any model call. Changed "
        "pairs go to a Gemini 3.7 Flash semantic differ that produces a "
        "grounded Delta — every change backed by an exact source quote, "
        "validated by Python.",
    ),
    (
        "05-analyst",
        "An analyst agent converts meaningful Deltas into versioned claims "
        "under strict lifecycle rules. Evidence bars, supersession, demotion "
        "guards — all enforced in the Python tool layer, not the prompt.",
    ),
    (
        "06-council",
        "A weekly Strategy Council — three A D K agents on their own Agent "
        "Runtime — reads the claims, proposes cross-entity conclusions, and a "
        "Challenger argues against them from the same evidence. Python decides "
        "what survives.",
    ),
    # Section 3: Dashboard demo
    (
        "07-dashboard-intro",
        "This is the Intelligence Dashboard, the fleet's read surface. Four "
        "competitors are monitored: Claude Code, OpenAI Codex, Gemini CLI, "
        "and Pi.",
    ),
    (
        "08-dashboard-cards",
        "Each card shows verified facts and the latest meaningful change. "
        "Where no meaningful change has cleared the evidence bar, Tycho says "
        "so honestly.",
    ),
    (
        "09-dashboard-timeline",
        "The belief timeline shows how Tycho's beliefs changed over time. "
        "Each event has lifecycle colours and pinned claim versions. Let's "
        "look at one.",
    ),
    (
        "10-dashboard-provenance",
        "The provenance drawer traces the full chain: exact claim version, "
        "the canonical Delta with its grounded quote, the observation IDs, "
        "and the configured source URL. Every line in the brief traces back "
        "to the snapshot it came from.",
    ),
    (
        "11-dashboard-strategy",
        "The strategy brief shows conclusions that survived the council's "
        "challenge process. Tycho rejected both candidate conclusions for "
        "this period — one for lacking independent evidence, one for failing "
        "the entity diversity rule. Tycho publishes nothing rather than "
        "manufacture a conclusion.",
    ),
    (
        "12-dashboard-trigger",
        "We can trigger a strategy session from here. The same bounded "
        "workflow the weekly Scheduler runs. Duplicate triggers are safe at "
        "two independent layers.",
    ),
    # Section 4: GCP proof
    (
        "13-gcp-proof",
        "This is the production fleet running on Google Cloud right now. "
        "Three Cloud Run services, two managed Agent Runtimes in the Vertex "
        "AI Agent Registry, two Cloud Scheduler jobs, BigQuery, Firestore, "
        "and Cloud Storage. The last acquisition ran at 2 AM UTC today.",
    ),
    # Section 5: Close
    (
        "14-close",
        "Accumulate facts. Revise beliefs. Never confuse the two. Tycho.",
    ),
)

PAUSE_AFTER: dict[str, float] = {
    "02-problem-close": 1.5,
    "06-council": 1.5,
    "12-dashboard-trigger": 1.0,
    "13-gcp-proof": 1.5,
}


KEY_FILE = Path(__file__).resolve().parent / ".elevenlabs_key"


def api_key() -> str:
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    if KEY_FILE.exists():
        value = KEY_FILE.read_text().strip()
        if value:
            return value
    raise SystemExit(
        "ELEVENLABS_API_KEY is not set and video/.elevenlabs_key was not found"
    )


def synthesize(key: str, text: str) -> bytes:
    query = urllib.parse.urlencode({"output_format": OUTPUT_FORMAT})
    url = f"{API_ROOT}/v1/text-to-speech/{VOICE_ID}?{query}"
    body = json.dumps(
        {
            "text": text,
            "model_id": MODEL_ID,
            "language_code": "en",
            "voice_settings": VOICE_SETTINGS,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "xi-api-key": key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ElevenLabs returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"Could not reach ElevenLabs: {error.reason}") from error


def duration(path: Path) -> float:
    result = subprocess.run(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def run_ffmpeg(*args: str) -> None:
    subprocess.run(("ffmpeg", "-hide_banner", "-loglevel", "error", *args), check=True)


def main() -> None:
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)

    key: str | None = None
    stem_paths: list[Path] = []
    manifest: list[dict[str, object]] = []

    for name, text in STEMS:
        raw = out / f"{name}.mp3"
        if not raw.exists():
            if key is None:
                key = api_key()
            print(f"Generating {name}...", flush=True)
            raw.write_bytes(synthesize(key, text))
        else:
            print(f"Reusing {raw.name}", flush=True)

        wav = out / f"{name}.wav"
        run_ffmpeg(
            "-i",
            str(raw),
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav),
            "-y",
        )
        dur = duration(wav)
        stem_paths.append(wav)
        manifest.append({"name": name, "duration": round(dur, 3), "text": text})
        print(f"  {name}: {dur:.2f}s", flush=True)

    # Concatenate with pauses between sections
    concat_list = out / "concat.txt"
    silence = out / "silence.wav"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=mono",
        "-t",
        "0.6",
        "-c:a",
        "pcm_s16le",
        str(silence),
        "-y",
    )
    long_silence = out / "long_silence.wav"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=mono",
        "-t",
        "1.5",
        "-c:a",
        "pcm_s16le",
        str(long_silence),
        "-y",
    )

    lines: list[str] = []
    for i, (wav, (name, _)) in enumerate(zip(stem_paths, STEMS)):
        lines.append(f"file '{wav.name}'")
        pause = PAUSE_AFTER.get(name, 0.6)
        if i < len(stem_paths) - 1:
            if pause > 1.0:
                lines.append(f"file '{long_silence.name}'")
            else:
                lines.append(f"file '{silence.name}'")
    concat_list.write_text("\n".join(lines) + "\n")

    master = out / "tycho-narration.wav"
    run_ffmpeg(
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-af",
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(master),
        "-y",
    )
    total = duration(master)
    print(f"\nMaster: {master} ({total:.1f}s)")

    (out / "manifest.json").write_text(
        json.dumps({"stems": manifest, "master_duration": total}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
