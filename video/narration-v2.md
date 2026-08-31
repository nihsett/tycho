# Tycho demo narration v2

Target: 3:46. Read with direct, measured energy—not trailer voice.

The headings and visual cues are not spoken. Each spoken block is one ElevenLabs stem.

## 0:00–0:23 — Dashboard and ontology

**Visual:** Hold on the Tycho title and the above-the-fold ontology tree. During the final sentence, point once from Entity to the governed branches.

> Competitive intelligence is scattered across pricing pages, product updates, social media, hiring, reviews, and news. Teams rebuild that picture by hand. Tycho is a fleet of Gemini agents that watches evidence over time, revises what it believes, and refuses conclusions it cannot support. Coding agents are the live test market.

## 0:23–0:43 — Durable production state

**Visual:** Trace Entity → Product → Capabilities/Roadmap, then the Claim ID → Version → Evidence → Supersession chain. Move down to the weekly facts. Do not click yet.

> This live Intelligence Dashboard is backed by production data on Google Cloud. Each entity has a durable, versioned belief tree across eight ontology branches. Four products and eight official sources have produced 132 Observations, 79 canonical Deltas, and 23 active verified facts.

**Fallback if a count changes:**

> This live Intelligence Dashboard is backed by production data on Google Cloud. Each entity has a durable, versioned belief tree across eight ontology branches. These are real production Observations, canonical Deltas, and active verified facts—not fixtures prepared for this recording.

## 0:44–1:08 — Autonomous session

**Visual:** Scroll once to **What Tycho believes now** and **Agent activity**. Point at the 06:02 UTC run events.

> At six UTC on August thirty-first, before I opened this page, Cloud Scheduler started the weekly Strategy Council. It assembled 23 exact claim versions and made one Gemini call. The Strategist proposed one market conclusion. Python rejected it for using one entity, one source family, and unsupported causation. Tycho wrote an empty brief instead.

## 1:08–1:21 — Rejection is the result

**Visual:** Expand **Why Tycho rejected one possible conclusion**. Point at the three rejection reasons and then at the zero-card brief.

> That is not a failed demo. The goal is not to force an agent to speak. It is to know when it has earned that right. The rejected card remains visible here, together with every reason it failed.

## 1:22–1:38 — Continuous live duplicate-safe action

**Visual:** Return to the header. Click **Refresh strategy brief** exactly once. Keep the cursor still while the activity updates. Hold on: “This period already had a session; the lease returned it without a model call.” Do not cut this segment.

> I will request the same period again. The browser cannot send a prompt, choose dates, or weaken the rules. The private dispatcher derives the period, and Firestore returns the completed session through its durable lease. No second Gemini call is made.

## 1:40–2:00 — Exact provenance

**Visual:** Open **View evidence** for Claude Code. Move down the drawer: claim version, canonical Delta, grounded quote, then before-and-after Observation IDs.

> Now look at one belief. The provenance drawer resolves an exact claim version to the canonical Delta that created it, the grounded source quote, and the before-and-after Observation IDs. The dashboard never fetches the raw snapshot, and the browser holds no Google credential. It receives only the evidence it is allowed to read.

## 2:01–2:40 — Architecture

**Visual:** Switch to `docs/diagrams/architecture-v2.png`. Trace left-to-right through acquisition and the Gemma screen, the Analyst Runtime, and the Strategy Council Runtime.

> Here is the system behind that screen. Cloud Scheduler starts a Cloud Run job. A Gemma classifier screens fetched content before agents see it. Payloads go to Cloud Storage and Observations to BigQuery. A hash gate stops unchanged content. Changed pairs go to Gemini 3.7 Flash, and Python validates every quote before a Delta is stored. Pub/Sub delivers meaningful Deltas to the Analyst Runtime, which updates versioned claims in Firestore through five governed tools. A separate Strategy Council Runtime drives the Strategist, Challenger, and Brief Writer, with Python gates between them.

## 2:40–3:01 — Managed agents and identity

**Visual:** Show the Analyst and Strategy Council in Agent Registry. If prepared, open the Strategy Council Runtime identity panel.

> The two managed applications are cataloged in Agent Registry and run under separate Agent Identities. They have no Cloud Storage or Pub/Sub role. The dashboard has a third, read-only identity. Firestore is the authoritative claim ledger because Tycho needs exact versions and transactions, not conversational memory.

## 3:02–3:31 — Correlated Google Cloud proof

**Visual:** Show Cloud Run, then the Scheduler row with the 06:00 last attempt, then trace `81c8cffde8b178b8fdd6b6203210bb8a`. Point at the seven spans and the single Gemini generation.

> This is the live Google Cloud project. Cloud Run shows the public dashboard and both private dispatchers. Cloud Scheduler shows nightly acquisition at two UTC and the weekly council at six. The August thirty-first attempt created the session shown in the dashboard. Its Cloud Trace has seven spans: the Council, the Strategist, one Gemini generation, and finish task. The production verifier found no prompt, claim, or source text in the exported trace.

## 3:32–3:46 — Close

**Visual:** Return to the empty brief. No cursor movement after the first sentence.

> Tycho had already watched the market, assembled the evidence, challenged the conclusion, and decided not to publish before I opened the page. Accumulate facts. Revise beliefs. Never confuse the two. Tycho.
