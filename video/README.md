# Tycho demo video toolkit

The final narration, recording runbook, and synchronized floating cue card live here. Generated audio and rendered video are intentionally gitignored.

## Generate narration and captions

Prerequisites: FFmpeg and an ElevenLabs API key.

```bash
export ELEVENLABS_API_KEY='...'
uv run python video/generate_voice_v2.py
```

Alternatively, place the key in the gitignored file `video/.elevenlabs_key`.

Outputs:

- `video/out-v2/tycho-narration-v2.wav`
- `video/out-v2/narration-v2.srt`
- `video/out-v2/manifest-v2.json`
- individual cached MP3/WAV stems

The generator hashes each source stem, reuses unchanged audio, derives subtitle timings from the rendered files, normalizes the master, and refuses narration over 230 seconds.

## Record

Follow [`docs/devpost/demo-recording-runbook.md`](../docs/devpost/demo-recording-runbook.md). The public dashboard is:

```text
https://tycho-dashboard-u2s544lf5a-uc.a.run.app
```

The narration script and visual cues are in [`narration-v2.md`](narration-v2.md).

## Floating cue card

Open the cue card as a separate browser window:

```bash
google-chrome --app="file://$PWD/video/cue-card.html" --window-size=460,260
```

When the cue-card window is focused, `Ctrl+Shift+F8` starts its timer at the same time as the configured OBS `DEMO` hotkey. The card shows one visual action and the countdown to the next. Because OBS captures the main browser window rather than the display, the floating cue window is not recorded.

## Remux the final take

OBS records MKV so a crash cannot destroy the take. Convert it to MP4 without re-encoding:

```bash
ffmpeg -i TAKE.mkv -map 0 -c copy -movflags +faststart tycho-demo-final.mp4
```
