## Inspiration

Every organization tracks its competitors; executives and sales/marketing teams rely on that information to make decisions every day. In many organizations this is done in an ad hoc way. The nature of the problem — watching an entity across many sources and their activities and building a detailed model of them — is naturally agentic and much more reliable when done by agents instead of human beings. 

Tycho is my attempt to build a living mental model of a business or a product that is fully evidenced — every belief can be traced back to its source — built and managed completely by autonomous agents.

The name comes from Tycho Brahe, the astronomer whose meticulous observations let Kepler derive the laws of planetary motion. Brahe did not theorize from intuition; he accumulated precise, immutable records and let the theory emerge from the evidence. Tycho does the same: accumulate facts, revise beliefs, never confuse the two.

## What it does

Tycho is four Gemini ADK agents that watch a market and maintain an evidenced picture of each entity in it. The fleet works in three layers:

**Watch and record.** Watchers run nightly on Cloud Scheduler. They fetch public sources, store the raw content in Cloud Storage, and log each observation in BigQuery. A hash check skips anything that hasn't changed — no model call wasted on unchanged content. When something does change, a Gemini 3.7 Flash call diffs the before and after and extracts what actually changed with exact quotes from the source. Python validates the result before it gets stored. Fetched content goes through a two-layer prompt-injection screen before any agent sees it: deterministic regex catches known patterns, then Gemma 4 classifies the content for subtler injection attempts that regex cannot catch.

**Believe and revise.** An analyst agent turns meaningful changes into versioned claims in Firestore. Claims are scoped to an ontology — pricing, capabilities, roadmap — and tagged with confidence and severity. A fact needs a primary source. An inference needs two independent sources. You don't edit a claim; you publish a replacement and retire the old one. These rules are in the Python tool layer, not in the prompt — the model can propose whatever it wants, but code enforces the bars.

**Reason and challenge.** Once a week, a Strategy Council — three ADK agents on their own managed Agent Runtime — reads the claim store and asks what materially changed across the market. A Strategist proposes cross-entity conclusions. Python checks every premise: exact claim versions, at least two entities, at least two independent source families (a vendor's GitHub releases and changelog count as one family, not two), and a language policy that rejects causation, intent, and market-leadership claims. A Challenger argues against each conclusion from the same evidence. A Brief Writer renders what survived into a cited brief. There is Python between every agent — nothing runs open-ended.

The Intelligence Dashboard is a private Cloud Run service that shows what changed, what Tycho believes, which conclusions survived challenge, and the full chain from any brief line back to the raw snapshot.

![Tycho's architecture: from source snapshots through grounded deltas to versioned claims, reasoned conclusions, and a cited brief](https://raw.githubusercontent.com/nihsett/tycho-assets/main/tycho-hero-1920x1080.png)

For this submission, Tycho watches the coding-agent market: Claude Code, Codex, Gemini CLI, and Pi, across GitHub releases and official changelogs. Four entities, eight source channels, real production data. The entity registry is a YAML file — no per-entity code. Swap the config and the same fleet watches a different market.

## How I built it

**Google AI:**
- Google ADK — four named agents (Analyst, Strategist, Challenger, Brief Writer)
- Gemini 3.7 Flash — semantic differ, analyst agent, and strategy council agents
- Gemma 4 — prompt-injection classifier that screens fetched content before any Gemini agent sees it
- Vertex AI Agent Runtime — two managed runtimes with Agent Identity, one for the analyst, one for the council

**Google Cloud:**
- Cloud Run — three private services (analyst dispatcher, strategy dispatcher, dashboard) + one acquisition job
- Cloud Scheduler — nightly acquisition, weekly strategy sessions
- BigQuery — immutable observations and Deltas
- Cloud Storage — write-once raw snapshots
- Firestore — versioned claims, sessions, leases
- Pub/Sub — analyst delivery with authenticated push
- Agent Registry — automatic registration of both runtimes
- Cloud Build + Artifact Registry — dashboard container images

**Other:**
- Python 3.13, FastAPI, Pydantic v2
- React 19, TypeScript 5.9, Vite 8
- OpenTelemetry — redacted trace export

I tested three models for the semantic differ — Gemma 4, Flash-Lite, and Flash 3.7. Ran them over 42 real observation pairs. 3.7 was the only one that correctly ignored nightly churn while still catching real deprecations. Gemma 4 found its home as the prompt-injection classifier — a fast, cheap gate that screens every fetched payload before any Gemini agent touches it.

The Strategy Council runs on its own Agent Runtime with its own service account and IAM — completely isolated from the analyst path. The deployment tooling won't even let you run a non-read-only command against analyst resources from the council's environment.

490 backend tests, 66 frontend tests, 14 end-to-end checks against production data.

## Challenges I ran into

- ADK task-mode agents return output as `finish_task` function-call arguments, not as text. The Strategy Council's first real run failed because I was only reading the text response.
- Found 13 cases where a GitHub release and the official changelog had byte-identical text. Two sources, one witness. Evidence rules now collapse duplicate quotes across source families.
- The semantic differ was letting unrelated OpenAI entries (ChatGPT plugins, cloud browser) leak into the Codex entity. Had to scope the prompt to require quoted content explicitly names the configured entity.

## Accomplishments that I'm proud of

- It runs in production right now. 100 observations, 56 Deltas, 11 active claims, 3 strategy sessions, 1 brief — all from real public sources with full provenance.
- Every governance rule is deterministic Python between agents. The model can't talk its way past a failed check.
- Complete provenance: any brief line traces back through claim versions, Deltas, observations, to the raw snapshot. The dashboard makes the whole chain navigable.

## What I learned

- Put governance in code, not in prompts. The model is useful inside its bounded role, but code decides what passes.
- Agents are cheap — a strategy session costs under $0.01. Making the output trustworthy is the expensive part: evidence rules, redacted traces, least-privilege IAM, 490 tests.

## What's next for Tycho

- More source types (RSS, search) and more entities. The YAML registry makes adding an entity a config change.
- An Auditor agent that reviews the claim store for stale evidence, contradictions, and gaps in coverage.
- A Repairman agent that automatically retries failed fetches, re-runs broken diffs, and generates operational claims from its own failures.
- Render the belief history timeline in the dashboard — the data model already supports full supersession chains.

## Try it out

- [GitHub repository](https://github.com/nihsett/tycho) (private during judging)
- [Architecture diagram](https://raw.githubusercontent.com/nihsett/tycho-assets/main/tycho-hero-1920x1080.png)
- Dashboard: private Cloud Run, accessible via `gcloud run services proxy tycho-dashboard --region us-central1`
