#!/usr/bin/env python3
"""Generate the production-state narration and timed captions for the Tycho demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import textwrap
from pathlib import Path

from generate_voice import (
    SAMPLE_RATE,
    TARGET_LUFS,
    api_key,
    duration,
    run_ffmpeg,
    synthesize,
)


STEMS: tuple[tuple[str, str], ...] = (
    (
        "01-problem",
        "Competitive intelligence is scattered across pricing pages, product "
        "updates, social media, hiring, reviews, and news. Teams rebuild that "
        "picture by hand. Tycho is a fleet of Gemini agents that watches evidence "
        "over time, revises what it believes, and refuses conclusions it cannot "
        "support. Coding agents are the live test market.",
    ),
    (
        "02-production-state",
        "This live Intelligence Dashboard is backed by production data on Google "
        "Cloud. Each entity has a durable, versioned belief tree across eight "
        "ontology branches. Four products and eight official sources have produced "
        "132 Observations, 79 canonical Deltas, and 23 active verified facts.",
    ),
    (
        "03-autonomous-session",
        "At six U T C on August thirty-first, before I opened this page, Cloud "
        "Scheduler started the weekly Strategy Council. It assembled 23 exact "
        "claim versions and made one Gemini call. The Strategist proposed one "
        "market conclusion. Python rejected it for using one entity, one source "
        "family, and unsupported causation. Tycho wrote an empty brief instead.",
    ),
    (
        "04-rejection-result",
        "That is not a failed demo. The goal is not to force an agent to speak. "
        "It is to know when it has earned that right. The rejected card remains "
        "visible here, together with every reason it failed.",
    ),
    (
        "05-duplicate-safe",
        "I will request the same period again. The browser cannot send a prompt, "
        "choose dates, or weaken the rules. The private dispatcher derives the "
        "period, and Firestore returns the completed session through its durable "
        "lease. No second Gemini call is made.",
    ),
    (
        "06-provenance",
        "Now look at one belief. The provenance drawer resolves an exact claim "
        "version to the canonical Delta that created it, the grounded source "
        "quote, and the before-and-after Observation I D's. The dashboard never "
        "fetches the raw snapshot, and the browser holds no Google credential. "
        "It receives only the evidence it is allowed to read.",
    ),
    (
        "07-architecture",
        "Here is the system behind that screen. Cloud Scheduler starts a Cloud "
        "Run job. A Gemma classifier screens fetched content before agents see it. "
        "Payloads go to Cloud Storage and Observations to BigQuery. A hash gate "
        "stops unchanged content. Changed pairs go to Gemini 3.7 Flash, and Python "
        "validates every quote before a Delta is stored. Pub Sub delivers meaningful "
        "Deltas to the Analyst Runtime, which updates versioned claims in Firestore "
        "through five governed tools. A separate Strategy Council Runtime drives "
        "the Strategist, Challenger, and Brief Writer, with Python gates between "
        "them.",
    ),
    (
        "08-managed-agents",
        "The two managed applications are cataloged in Agent Registry and run "
        "under separate Agent Identities. They have no Cloud Storage or Pub Sub "
        "role. The dashboard has a third, read-only identity. Firestore is the "
        "authoritative claim ledger because Tycho needs exact versions and "
        "transactions, not conversational memory.",
    ),
    (
        "09-cloud-proof",
        "This is the live Google Cloud project. Cloud Run shows the public dashboard "
        "and both private dispatchers. Cloud Scheduler shows nightly acquisition at "
        "two U T C and the weekly council at six. The August thirty-first "
        "attempt created the session shown in the dashboard. Its Cloud Trace has "
        "seven spans: the Council, the Strategist, one Gemini generation, and "
        "finish task. The production verifier found no prompt, claim, or source "
        "text in the exported trace.",
    ),
    (
        "10-close",
        "Tycho had already watched the market, assembled the evidence, challenged "
        "the conclusion, and decided not to publish before I opened the page. "
        "Accumulate facts. Revise beliefs. Never confuse the two. Tycho.",
    ),
)

NO_COUNT_TEXT = (
    "This live Intelligence Dashboard is backed by production data on Google "
    "Cloud. Each entity has a durable, versioned belief tree across eight ontology "
    "branches. These are real production Observations, canonical Deltas, and active "
    "verified facts, not fixtures prepared for this recording."
)

PAUSE_AFTER: dict[str, float] = {
    "01-problem": 0.6,
    "02-production-state": 0.6,
    "02-production-state-no-counts": 0.6,
    "03-autonomous-session": 0.6,
    "04-rejection-result": 1.0,
    "05-duplicate-safe": 2.0,
    "06-provenance": 1.0,
    "07-architecture": 0.6,
    "08-managed-agents": 0.6,
    "09-cloud-proof": 0.8,
}

MAX_MASTER_SECONDS = 230.0


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-counts",
        action="store_true",
        help="use the count-free production-state stem",
    )
    return parser.parse_args()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole_seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def caption_text(text: str) -> str:
    display_text = (
        text.replace("U T C", "UTC")
        .replace("I D's", "IDs")
        .replace("Pub Sub", "Pub/Sub")
    )
    return "\n".join(textwrap.wrap(display_text, width=72))


def make_silence(out: Path, seconds: float) -> Path:
    token = str(seconds).replace(".", "p")
    path = out / f"silence-{token}.wav"
    if not path.exists():
        run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-t",
            str(seconds),
            "-c:a",
            "pcm_s16le",
            str(path),
            "-y",
        )
    return path


def main() -> None:
    args = arguments()
    out = Path(__file__).resolve().parent / "out-v2"
    out.mkdir(exist_ok=True)

    stems = list(STEMS)
    if args.no_counts:
        stems[1] = ("02-production-state-no-counts", NO_COUNT_TEXT)

    key: str | None = None
    rendered: list[tuple[str, str, Path, float]] = []

    for name, text in stems:
        raw = out / f"{name}.mp3"
        metadata = out / f"{name}.source.json"
        digest = content_hash(text)
        current_digest = ""
        if metadata.exists():
            try:
                current_digest = json.loads(metadata.read_text()).get("sha256", "")
            except (json.JSONDecodeError, OSError):
                pass

        if not raw.exists() or current_digest != digest:
            if key is None:
                key = api_key()
            print(f"Generating {name}...", flush=True)
            raw.write_bytes(synthesize(key, text))
            metadata.write_text(
                json.dumps({"sha256": digest, "text": text}, indent=2) + "\n"
            )
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
        stem_duration = duration(wav)
        rendered.append((name, text, wav, stem_duration))
        print(f"  {name}: {stem_duration:.2f}s", flush=True)

    concat_lines: list[str] = []
    manifest_stems: list[dict[str, object]] = []
    subtitle_blocks: list[str] = []
    cursor = 0.0

    for index, (name, text, wav, stem_duration) in enumerate(rendered, start=1):
        start = cursor
        end = start + stem_duration
        pause = PAUSE_AFTER.get(name, 0.0) if index < len(rendered) else 0.0

        concat_lines.append(f"file '{wav.name}'")
        if pause:
            silence = make_silence(out, pause)
            concat_lines.append(f"file '{silence.name}'")

        manifest_stems.append(
            {
                "name": name,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(stem_duration, 3),
                "pause_after": pause,
                "text": text,
            }
        )
        subtitle_blocks.append(
            f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{caption_text(text)}\n"
        )
        cursor = end + pause

    concat_list = out / "concat-v2.txt"
    concat_list.write_text("\n".join(concat_lines) + "\n")

    master = out / "tycho-narration-v2.wav"
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

    captions = out / "narration-v2.srt"
    captions.write_text("\n".join(subtitle_blocks))
    (out / "manifest-v2.json").write_text(
        json.dumps(
            {
                "variant": "no-counts" if args.no_counts else "production-counts",
                "stems": manifest_stems,
                "master_duration": round(total, 3),
            },
            indent=2,
        )
        + "\n"
    )

    print(f"\nMaster: {master} ({total:.1f}s)")
    print(f"Captions: {captions}")
    if total > MAX_MASTER_SECONDS:
        raise SystemExit(
            f"Narration is {total:.1f}s; tighten it below {MAX_MASTER_SECONDS:.0f}s "
            "before recording."
        )


if __name__ == "__main__":
    main()
