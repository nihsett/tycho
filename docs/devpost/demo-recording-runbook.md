# Tycho Devpost demo — recording runbook

> Target: 3:46–3:48 including the OBS lead-in. Hard maximum: 4:00.
> Record on Monday, 31 August 2026 IST.

## The story this recording proves

Tycho did the work before the dashboard was opened.

At 06:00 UTC on 31 August, the real `tycho-strategy-weekly` Cloud Scheduler job invoked the private Strategy dispatcher. The managed Strategy Council Runtime completed at 06:03 UTC:

- period: 24–30 August 2026, ISO week 35;
- session: `sts_01M1B6KFJA79NF52PWF3QAVVZX`;
- 23 exact claim versions in the manifest;
- one real Gemini call;
- one conclusion proposed and rejected;
- zero conclusions passed;
- empty brief: `brf_2026w35-f3qavvzx`;
- trace: `81c8cffde8b178b8fdd6b6203210bb8a`, seven spans, no governed-prose leak.

This is better than manufacturing a fresh run for the camera. The live button demonstrates a second production property: a duplicate request returns that session through the durable lease and makes no second model call.

## What not to claim

- The deployed demo currently watches GitHub releases and official changelogs. Social media, jobs, reviews, RSS, and search explain the wider problem; do not say they are live source adapters.
- The semantic differ is Gemini 3.7 Flash. The 31 August Strategy Council session used `gemini-3.5-flash-lite`; say “Gemini” for that run.
- Firestore is Tycho's authoritative claim ledger, not Vertex AI Memory Bank.
- Do not call the private Cloud Run dispatchers Agent Gateway.
- Do not call the quarantine screen Model Armor.

Use `docs/diagrams/architecture-v2.png` for the architecture shot. It communicates the complete fleet design, while the narration and live console shots focus on the verified production path.

## Final run of show

| Time | Screen | Action |
|---|---|---|
| 0:00–0:23 | Dashboard top | Hold on the title and ontology tree. Point once from Entity to its governed branches. |
| 0:23–0:43 | Ontology + weekly facts | Trace Product → Capabilities/Roadmap and Claim ID → Version → Evidence → Supersession, then move to the metrics. |
| 0:44–1:08 | Strategy brief + activity | Scroll once to the brief and activity panels. Point to the 06:02 UTC activity. |
| 1:08–1:21 | Rejected conclusion | Expand “Why Tycho rejected one possible conclusion.” Keep all three reasons visible. |
| 1:22–1:38 | Dashboard header | Return to top and click **Refresh strategy brief** once. Do not cut. Wait for the lease message. |
| 1:40–2:00 | Provenance drawer | Open **View evidence** on Claude Code, then walk top-to-bottom through claim version, Delta, quote, and Observation IDs. |
| 2:01–2:40 | `architecture-v2.png` tab | Move left-to-right: acquisition and Gemma screen, Analyst Runtime, Strategy Council. |
| 2:40–3:01 | Agent Registry / Runtime | Show Tycho Analyst and Tycho Strategy Council; open the Council identity if already prepared. |
| 3:02–3:31 | Cloud Run → Scheduler → Trace | Show the public dashboard, private dispatchers, the 06:00 Scheduler attempt, then trace `81c8…`. |
| 3:32–3:46 | Dashboard empty brief | Return to the brief for the closing line. Stop immediately after “Tycho.” |

The dashboard currently reports:

- 8 watchers;
- 132 Observations;
- 79 canonical Deltas: 25 meaningful and 54 noise;
- 23 active verified facts;
- latest Strategy Council result: 0 of 1 cards passed.

These counts should remain stable until the next nightly acquisition at 02:00 UTC. Recheck them immediately before recording and use the fallback wording “real production observations, Deltas, and claims” if any count differs.

## Browser preparation

The dashboard is publicly readable at:

```text
https://tycho-dashboard-u2s544lf5a-uc.a.run.app
```

No proxy or Google credential is required for the dashboard tab. Prepare one clean Chrome window. Use full-screen mode and 125% zoom. Open these tabs in this exact order:

1. `https://tycho-dashboard-u2s544lf5a-uc.a.run.app`
2. `file:///path/to/tycho/docs/diagrams/architecture-v2.png`
3. Google Cloud Console → Agent Registry
4. Google Cloud Console → Cloud Run, region `us-central1`
5. Google Cloud Console → Cloud Scheduler, region `us-central1`
6. Google Cloud Console → Trace Explorer, trace ID `81c8cffde8b178b8fdd6b6203210bb8a`

Before opening OBS:

- authenticate every Google Cloud tab;
- dismiss tours, cookie banners, and side panels;
- filter Agent Registry to the Analyst and Strategy Council, not the old platform probe;
- filter Cloud Run to `tycho-dashboard`, `tycho-analyst-dispatcher`, `tycho-strategy-dispatcher`, and `tycho-acquire`;
- make the Scheduler columns for job, schedule, state, and last attempt visible;
- open the seven-span Runtime trace and collapse irrelevant metadata;
- put the dashboard back at the top with no drawer open;
- enable desktop Do Not Disturb;
- close personal tabs and hide the bookmarks bar;
- never put `.env`, API keys, the ElevenLabs key, or terminal history on screen.

## Launch and configure OBS

OBS Studio 32.1.2 is already installed as the user-level Flathub application `com.obsproject.Studio`. Launch it with:

```bash
flatpak run com.obsproject.Studio
```

The Flatpak already has host-filesystem, X11, PipeWire, and PulseAudio access, so Display Capture and the narration file should work without an override. The monitor is 2560×1440 at 59.95 Hz and the machine has an NVIDIA GTX 1050 Ti. Use:

### Video

- Base canvas: `2560x1440`
- Output resolution: `2560x1440`
- FPS: `30`
- Downscale filter: Lanczos, although no scaling should occur

### Recording

- Output mode: Advanced
- Recording format: `MKV`
- Encoder: NVIDIA NVENC H.264
- Rate control: `CQP`
- CQ level: `18`
- Keyframe interval: `2`
- Preset: `P5 / Quality`
- Multipass: two passes, quarter resolution
- Profile: High
- Look-ahead: Off
- Psycho Visual Tuning: On
- B-frames: 2
- Recording directory: `~/Videos/tycho-demo/`

MKV is deliberate: a crash does not destroy the recording. Remux the selected take to MP4 afterwards.

### Scenes and audio

Create two scenes using the same dedicated Chrome **Window Capture** source:

1. **STANDBY** — Window Capture only.
2. **DEMO** — the same Window Capture plus a Media Source pointing to `video/out-v2/tycho-narration-v2.wav`.

Media Source settings:

- Local file: On
- Loop: Off
- Restart playback when source becomes active: On
- Close file when inactive: On

In Advanced Audio Properties, set the narration Media Source to **Monitor and Output** so it goes both to the recording and to headphones. Disable Desktop Audio and disable the microphone. This prevents notifications or room noise from entering the final video.

Recording sequence:

1. Wear headphones.
2. Start on the STANDBY scene with Chrome full-screen.
3. Start recording and wait one second.
4. Switch to DEMO. The narration begins automatically.
5. Follow the cues in `video/narration-v2.md`.
6. Do not change OBS scenes during the live dashboard refresh.
7. Stop recording immediately after the final line.

Do one silent rehearsal first. Clicking **Refresh strategy brief** during rehearsal is safe: it should return the existing session through the lease and make no model call.

## Generate narration and captions

From the repository root:

```bash
uv run python video/generate_voice_v2.py
```

Outputs:

- `video/out-v2/tycho-narration-v2.wav`
- `video/out-v2/narration-v2.srt`
- `video/out-v2/manifest-v2.json`

The generator reuses the existing ElevenLabs voice configuration and key lookup. It refuses a master longer than 3:50.

## After recording

In OBS, use **File → Remux Recordings** to create an MP4. Or:

```bash
ffmpeg -i ~/Videos/tycho-demo/TAKE.mkv \
  -c copy video/out-v2/tycho-demo-v2.mp4
```

Check duration:

```bash
ffprobe -v error \
  -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 \
  video/out-v2/tycho-demo-v2.mp4
```

Reject any take over 230 seconds. Watch the entire selected take once at normal speed and once muted. Confirm:

- the refresh sequence has no cut;
- the lease-return message is readable;
- the rejected reasons are readable;
- the claim version, grounded quote, and Observation IDs are readable;
- the Cloud Scheduler last attempt is `2026-08-31T06:00:01Z`;
- the trace shows one `generate_content gemini-3.5-flash-lite` span;
- no secret or personal notification appears;
- the closing line finishes before 3:50.

Upload the MP4 publicly to YouTube or Vimeo. Upload `video/out-v2/narration-v2.srt` as English captions; do not rely only on automatic captions.

## When to do this

The submission deadline is 17:00 PT on 31 August, which is 05:30 IST on 1 September. Do not use the final hours.

Recommended schedule for Monday, 31 August IST:

1. **Now–13:00:** install OBS, generate narration, prepare all browser tabs.
2. **13:00–13:30:** one silent rehearsal and one recorded rehearsal.
3. **13:30–15:00:** record three complete takes; keep the best continuous take.
4. **15:00–17:00:** remux, review, upload captions, and verify YouTube playback at 1440p.
5. **By 20:00:** put the public video URL into the Devpost draft.
6. **By 22:00:** finish the submission, leaving more than seven hours of buffer.

Do not wait for another fresh Strategy Council period. The 06:00 UTC run is the autonomous action the demo should prove.

## Fallbacks

- **Refresh returns too quickly:** good. Hold on the “lease returned it without a model call” message for three seconds.
- **Dashboard cache briefly shows the older verdict:** reload once before recording and wait 60 seconds. Do not reload during the take.
- **Console is slow:** hold the current page; do not cut the live dashboard segment. Architecture and console shots may be trimmed only before final export if the live segment remains continuous.
- **Narration and clicks drift:** stop and record another full take rather than splicing the refresh sequence.
- **A count changes:** do not rerecord narration for a number. Use the fallback no-count stem described in `video/narration-v2.md`.
