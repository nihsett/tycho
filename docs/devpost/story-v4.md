## Inspiration

Competitive intelligence is not a one-time search. Teams must watch the same companies over time, separate real changes from noise, and revise old beliefs when new evidence arrives. Most teams do this through scattered alerts, Slack threads, and slide decks. The reasoning quickly disappears.

**Tycho is an autonomous competitive-intelligence fleet with evidence-based memory.** It keeps the observations, the beliefs built from them, and the exact chain between the two. Coding agents are the live test market, not the limit of the product.

The name comes from Tycho Brahe. His careful observations gave Kepler the evidence needed to explain planetary motion. Tycho follows the same rule: **accumulate facts, revise beliefs, never confuse the two.**

## What it does

Tycho is a production fleet of **four Gemini agents built with Google ADK** and deployed on the **Gemini Enterprise Agent Platform**. It runs without a chat prompt. Sources are watched nightly, beliefs are updated as evidence arrives, and a separate council reasons across the market every week.

For this submission, Tycho monitors **Claude Code, OpenAI Codex, Gemini CLI, and Pi** across eight official sources: each product's GitHub releases and changelog. The market, sources, ontology, and schedules live in `tycho.yaml`; there is no competitor-specific application code.

The system has four steps:

1. **Watch and record.** Cloud Scheduler starts a Cloud Run acquisition job. Raw payloads are written once to Cloud Storage. Immutable Observations are appended to BigQuery. A hash gate stops unchanged content before any model call.

2. **Interpret the change.** A deterministic screen and **Gemma 4** classifier quarantine prompt-injection attempts. Clean before-and-after pairs go to **Gemini 3.7 Flash on Vertex AI**. Gemini returns structured changes with exact source quotes. Python checks every quote, ID, enum, bound, and policy rule before storing a canonical Delta.

3. **Believe and revise.** Pub/Sub delivers meaningful Deltas to the managed **Tycho Analyst Agent Runtime**. The Analyst can use only five governed lifecycle tools: create, supersede, adjust confidence, reverify, or take no action. Claims in Firestore are versioned and superseded, never silently overwritten.

4. **Reason and challenge.** A second managed Runtime hosts the weekly **Strategy Council**. A Strategist proposes conclusions from exact pinned claim versions. Python requires multiple entities and independent source families. A Challenger tries to reject what survives. A Brief Writer can cite only validated claims. If nothing clears the rules, Tycho publishes an explicit empty brief.

The public read-only dashboard shows current beliefs, claim history, rejected conclusions, agent activity, and full provenance. A user can follow a claim version to its canonical Delta, grounded quote, and before-and-after Observation IDs.

![Tycho architecture: immutable observations become grounded Deltas, versioned claims, challenged conclusions, and cited briefs](https://raw.githubusercontent.com/nihsett/tycho/main/docs/diagrams/architecture-v2.png)

**The main difference from a normal monitor:** Tycho does not replace yesterday's summary. It preserves observations, versions beliefs, records contradictions, and refuses unsupported conclusions.

This is my **Fortified Enterprise Fleet** entry: two isolated managed Runtimes, narrow identities, shared governed memory, adversarial reasoning, and code-enforced guardrails.

## How I built it

I split the system into two planes:

- **Evidence plane:** Cloud Storage and BigQuery hold write-once payloads, immutable Observations, and validated Deltas.
- **Belief plane:** Firestore holds exact claim versions, supersession history, sessions, briefs, transactions, and durable leases.

**Firestore—not Vertex AI Memory Bank—is the authoritative memory.** Tycho needs exact versions and transactional lifecycle rules, not conversational recall. The models propose changes; Python decides whether those changes are allowed.

The Google stack is visible in the deployed system:

- **Google ADK:** Analyst, Strategist, Challenger, and Brief Writer
- **Gemini on Vertex AI:** semantic change interpretation and agent reasoning
- **Gemma 4:** semantic prompt-injection screening
- **Two managed Agent Runtimes**, automatically cataloged in **Agent Registry**
- Separate managed **Agent Identities** and least-privilege service accounts
- **Cloud Run, Cloud Scheduler, Pub/Sub, BigQuery, Cloud Storage, Firestore, Cloud Build, and Artifact Registry**
- **Cloud Trace and OpenTelemetry** for structural traces and model usage

The agents cannot write directly to storage. The Analyst acts only through validated tools. The Strategy Council cannot browse, read raw snapshots, or modify claims. Traces keep IDs, timing, token usage, and workflow structure, while prompts, model responses, claims, and source quotes are removed before export.

Durable leases make retries safe. Repeating the same Delta or weekly period returns the existing result instead of creating duplicate state or another model call.

## Challenges I ran into

Three production issues shaped the design. A real quote could still describe the wrong product, so entity relevance became a hard validation rule. Two vendor-controlled URLs could repeat the same evidence, so mirrored channels now count as one source family. ADK task-mode output arrived through `finish_task` arguments instead of final text, so the Runtime invoker had to handle that provider contract explicitly. Each fix became a regression test.

## Accomplishments that I'm proud of

Tycho is running on Google Cloud against real public data:

- **132 immutable Observations**
- **79 canonical Deltas:** 25 meaningful and 54 noise
- **23 active evidenced claims**
- **Four competitors and eight official source channels**

The latest autonomous weekly session started from Cloud Scheduler before the dashboard was opened. It pinned all 23 active claim versions and made one Gemini call. The Strategist proposed one conclusion. Python rejected it because the evidence did not satisfy the cross-entity rules. Tycho wrote an empty brief instead of manufacturing a market pattern.

Repeating the same period returned that completed session through its Firestore lease. **No second Gemini call was made.**

That result is the feature I care about most. The goal is not to make an agent always say something. The goal is to know when it has earned the right to say it.

The repository has **488 passing backend tests, 68 frontend tests, 14 live end-to-end checks, and green GitHub Actions CI**. The public dashboard and production verifiers read the real Google Cloud stores, not fixtures prepared for the demo.

## What I learned

A model output should be treated as a proposal, not authority. Reliable autonomy comes from combining model judgment with narrow identities, durable state, deterministic validation, and explicit failure paths.

## What's next for Tycho

Next I would add an Auditor for stale or contradictory beliefs and a Repairer for failed sources and model runs. I also want to add more source adapters and test the same fleet in another market. Firestore will remain the governed source of truth while semantic retrieval can support non-authoritative operational recall.

## Try it out

- [Live Intelligence Dashboard](https://tycho-dashboard-u2s544lf5a-uc.a.run.app)
- [GitHub repository](https://github.com/nihsett/tycho)
- [Architecture diagram](https://raw.githubusercontent.com/nihsett/tycho/main/docs/diagrams/architecture-v2.png)
