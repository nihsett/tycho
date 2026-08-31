## Inspiration

Every company tracks its competitors, but the useful signals are scattered across pricing pages, product updates, social media, job listings, reviews, communities, and news. Most teams follow them ad hoc, and the reasoning behind a conclusion disappears into Slack threads and slide decks.

This is an ongoing job, not a search query. You have to watch the same entities over time, separate real shifts from daily noise, and revise earlier beliefs when the evidence changes. Developer tools are Tycho's demonstration market, but the problem exists in every industry.

Tycho uses autonomous agents to maintain an evidenced model of a company or product. Every belief traces back to a source, and when the evidence is weak, Tycho is allowed to say nothing.

The name comes from Tycho Brahe, whose meticulous observations gave Kepler the evidence needed to derive the laws of planetary motion. Tycho follows the same rule: **accumulate facts, revise beliefs, never confuse the two.**

## What it does

Tycho is a production fleet of four Gemini agents built with Google ADK and deployed on the Gemini Enterprise Agent Platform. It runs without a chat prompt: sources are watched nightly, beliefs are revised as evidence arrives, and a separate council reasons across the market every week.

For this submission, Tycho monitors Claude Code, OpenAI Codex, Gemini CLI, and Pi across eight official channels: each product's GitHub releases and changelog. Entity names, aliases, sources, ontology, staleness rules, and schedules live in `tycho.yaml`. There is no competitor-specific application code; changing the registry changes the market the same fleet watches.

The system has three layers.

### Watch and record — evidence before interpretation

Cloud Scheduler starts the `tycho-acquire` Cloud Run job every night. Generic adapters fetch each configured source. The complete payload is stored once in Cloud Storage, while an immutable Observation row—identity, source, timestamp, hash, status, and content reference—lands in BigQuery.

Fetched text is treated as adversarial data. A deterministic screen catches known prompt-injection patterns, while Gemma 4 adds a semantic screen for subtler instructions. Flagged content is preserved as a quarantined Observation but goes no further. A hash gate then stops unchanged sources before any semantic model call.

When a clean source changes, Gemini 3.7 Flash compares one bounded before-and-after pair through Vertex AI. It has no tools and must return strict JSON. Every proposed change needs an exact quote from the source. Python verifies the schema, quote grounding, Observation identities, entity relevance, enums, bounds, and policy before a canonical `Delta@2` can enter BigQuery.

A malformed response or invented quote creates no Delta and leaves the pair retryable. Valid noise is retained for audit, but only meaningful Deltas are published to Pub/Sub. Gemini decides what a change means; code decides whether Tycho may rely on it.

### Believe and revise — models propose, tools decide

Pub/Sub sends each meaningful Delta by authenticated push to a private Cloud Run dispatcher. The dispatcher validates the canonical Delta, then passes only its `delta_id` to the first managed Agent Runtime: **Tycho Analyst**.

The Runtime is automatically cataloged in Agent Registry and runs under its own managed Agent Identity. A deterministic ADK `BaseAgent` wrapper loads the exact Delta from BigQuery and invokes the Gemini Analyst. There is no outer model deciding how to route the request.

The Analyst sees the Delta, entity context, and relevant active claims from Firestore. It must choose exactly one of five tools: create a claim, supersede one, adjust confidence, bump its verification clock, or take no action. Those tools—not the prompt—enforce the evidence rules.

A fact needs the entity's own primary source. A normal inference needs independent sources. Claims about intent or future action are clamped to speculative confidence. A new third-party contradiction cannot erase a confirmed critical claim; Tycho creates a separate dispute and waits for stronger evidence.

Claims are never edited in place. A correction publishes its replacement before retiring the old version, so a crash can create a visible duplicate but cannot silently remove the current belief. Firestore holds the active claim graph, supersession history, analyst leases, and delivery state.

I deliberately use Firestore—not Vertex AI Memory Bank—as the authoritative memory. Tycho does not need conversational recall here. It needs exact claim versions, evidence references, transactions, and lifecycle invariants. Agent Registry and Agent Runtime manage the agents; Firestore remains the governed source of truth they share.

### Reason and challenge — conclusions are earned

Once a week, a second Cloud Scheduler job calls a separate private Cloud Run dispatcher. The dashboard can invoke the same endpoint. Neither caller can send a prompt, question, date range, or evidence policy. The request only names `previous_complete_week`; the dispatcher derives the UTC period from its own clock. Every trigger in the same week therefore resolves to the same durable lease.

The request enters the second managed Agent Runtime: **Tycho Strategy Council**. It has a different Agent Identity, service account, deployment path, schedule, and Agent Registry entry from the Analyst. Its deployment tooling is structurally unable to modify the Analyst Runtime, subscription, acquisition job, nightly schedule, or Delta topic.

A deterministic ADK `BaseAgent` drives three specialized Gemini agents:

1. The **Strategist** proposes at most three present-state conclusions from a bounded manifest of exact claim versions.
2. Python checks every premise before a Challenger sees it: canonical evidence, exact versions, at least two entities, at least two independent source families, staleness, confidence ceilings, and language that does not overclaim causation, intent, leadership, or the future.
3. The **Challenger** argues against each surviving card using the same pinned evidence. It can reject a card; it cannot revive one that failed a hard rule.
4. The **Brief Writer** sees only cards that survived both stages and may cite only their pinned claim versions. Python validates those citations before publishing the brief.

All three are strict structured-output ADK agents. They have no tools, cannot search the web, cannot read raw Cloud Storage snapshots, and cannot mutate claims. Python runs between every role. This is a governed workflow, not a free-running `SequentialAgent`.

The public read-only Intelligence Dashboard on Cloud Run is the fleet's read surface. It shows watcher health, current beliefs, belief history, Strategy Council sessions, rejected cards, and the provenance behind every brief citation. A provenance drawer resolves an exact claim version to its canonical Delta, grounded quote, before-and-after Observation IDs, and configured source.

The browser holds no Google credential and never talks directly to BigQuery, Firestore, Cloud Storage, Pub/Sub, or an Agent Runtime. The dashboard has its own service account with read-only data roles and one additional permission: invoke the strategy dispatcher.

![Tycho's architecture: immutable observations become grounded Deltas, versioned claims, challenged conclusions, and cited briefs](https://raw.githubusercontent.com/nihsett/tycho/main/docs/diagrams/architecture-v2.png)

A conventional monitor asks, “What does this page say now?” Tycho keeps a longer-lived record:

| | Scraper or summarizer | Tycho |
|---|---|---|
| Source handling | Reads the latest page | Preserves immutable before-and-after Observations |
| Change detection | Text diff or fresh summary | Grounded semantic Delta with exact quotes |
| Memory | Replaces an old summary | Versions and supersedes evidenced claims |
| Contradiction | Quietly changes the answer | Records a dispute until stronger evidence resolves it |
| Strategy | Produces a plausible synthesis | Requires independent premises and adversarial challenge |
| No defensible answer | Usually still returns prose | Publishes an explicit empty brief |

That combination—cataloged agents, long-lived governed state, narrow identities, and autonomous work against production data—is why Tycho belongs in the **Fortified Enterprise Fleet** category.

## How I built it

I designed Tycho outward from one distinction: **facts accumulate; beliefs revise.**

Cloud Storage and BigQuery form the evidence plane. Raw snapshots, Observations, and canonical Deltas are append-only. Firestore forms the belief plane. Claims can change, but only through versioned lifecycle operations that preserve their history.

That split constrains every agent. Gemini can interpret a source change and propose a belief update, but it cannot rewrite the evidence. The Strategy Council can reason over governed claims, but it cannot browse for more convenient premises. New evidence must enter through acquisition first.

I treated Google Cloud as part of the safety model, not merely the place the containers happen to run. The Analyst and Strategy Council live in separate managed Agent Runtimes, automatically appear in Agent Registry, and receive separate Agent Identities. Private Cloud Run dispatchers reduce requests to bounded contracts before they reach either Runtime. Cloud Scheduler supplies authenticated time-based triggers. Pub/Sub gives the Analyst retryable delivery. Firestore transactions and leases make repeated or concurrent execution idempotent.

Deployment follows the same philosophy. Each Runtime, dispatcher, identity, IAM binding, and Scheduler job is recorded as soon as it exists, then read back from the Google Cloud API. A mismatch fails closed instead of being silently overwritten. The Strategy Council deployment was interrupted during its Scheduler step; the resumed deployment reused all eight resources already created and applied only the missing step.

Model selection was empirical. I first gave Gemma 4, Gemini 3.5 Flash-Lite, and Gemini 3.7 Flash the same four real source transitions. Gemma was inexpensive but did not reliably follow the schema and missed a clear Codex deprecation. Flash-Lite grounded its quotes but promoted routine nightly fixes into durable intelligence. Gemini 3.7 was the only one that both caught the deprecation and rejected the churn.

Before changing production authority, I ran 3.7 over 42 historical production Observation pairs. It classified 12 as meaningful and 29 as noise. Python rejected one response because its `evidence_before` quote did not occur in the earlier Observation. No Delta was written; a targeted retry of that immutable pair passed. That rejection mattered more than a perfect-looking report because it proved the model was not the final authority.

The repository now has 488 passing backend tests, 68 frontend tests, and 14 read-only end-to-end checks against the deployed service and real production data. They cover grounding, model schemas, claim lifecycle, transaction races, duplicate delivery, bounded dispatchers, exact IAM, deployment resume, trace redaction, canonical-only dashboard queries, and end-to-end provenance.

## Technical Details

- **Agents and models:** Google ADK powers the Analyst, Strategist, Challenger, and Brief Writer. Gemini 3.7 Flash powers the semantic differ and is the current code default; exact model IDs are persisted per run, and the 31 August Council session recorded Gemini 3.5 Flash-Lite. Gemma 4 supplements the deterministic quarantine screen.

- **Managed agent platform:** Two managed Agent Runtimes, two automatic Agent Registry entries, and two separate managed Agent Identities. Both runtimes scale from zero to one instance.

- **Google Cloud:** One Cloud Run acquisition job; private Analyst and Strategy dispatchers; a public read-only Dashboard service; two Cloud Scheduler jobs; BigQuery; Cloud Storage; Firestore; Pub/Sub; Cloud Build; and Artifact Registry.

- **Least privilege:** Runtime identities hold only the project roles needed for Firestore, BigQuery, and telemetry, with no Cloud Storage or Pub/Sub role. The dashboard identity is read-only on data. Deployment readback treats an unexpected or missing role as a failure.

- **Durable execution:** Analyst leases key on Delta and analyst version. Strategy leases key on period and strategy version. A duplicate returns the existing result without another model call; a failed or expired attempt remains retryable.

- **Atomic state:** Claim replacement is publish-before-retire. Strategy session start creates its lease and readable running record together. The final brief, terminal session, and lease release commit in one Firestore transaction.

- **Observability:** OpenTelemetry preserves the Runtime span graph, model name, token counts, latency, IDs, and structural outcomes. A redacting span processor removes prompts, responses, claims, and source text before Cloud Trace export; logs contain IDs, states, and counts only.

## Challenges I ran into

The hardest problems were not API calls. They were deciding what the system was allowed to believe.

The first Analyst calibration exposed a flaw in my own specification. I had written rules that made a third-party contradiction awkward to represent honestly: either the Analyst demoted a strong claim using weak evidence or ignored the contradiction. I changed the claim model instead of tuning the prompt around the problem. Weak contradictory evidence now becomes a separate speculative dispute; the established claim remains active; later primary-source evidence can resolve it. The model had exposed a product-design bug.

The first full semantic replay found a different precision problem. OpenAI's changelog mixes Codex entries with ChatGPT plugins, cloud-browser features, and other sibling products. Gemini's quotes were real, but some were evidence about the wrong entity. I tightened the contract so a retained quote must explicitly identify Codex or one of its components. A targeted replay of all five Codex webpage pairs kept the Codex changes and removed the sibling-product contamination.

Evidence independence was subtler than counting URLs. The production audit found 13 Claude Code cases where GitHub Releases and the official changelog carried the same evidence text. Two channels were acting as one witness. Tycho now groups a vendor's mirrored channels into one source family and collapses identical evidence before allowing it to support a cross-source conclusion.

The first two production Strategy Council triggers failed. ADK task-mode agents deliver structured output in the arguments of a `finish_task` function call; my invoker was reading only final text. Both failures became durable failed sessions. The shared period lease stayed retryable, no brief was written, no claim changed, and only a sanitized error class reached Firestore and Cloud Logging. I fixed the invoker, added regression tests, updated the existing Runtime, and the next Scheduler trigger completed.

Production found smaller assumptions too. Cloud Trace pagination can silently omit matching traces. A report can be labeled with the wrong ISO week if its exclusive Monday endpoint is treated as part of the period. Cloud Scheduler normalizes an endpoint with a trailing slash and uses different header flags for create and update. Each discovery became a correction and a regression test.

## Accomplishments that I'm proud of

Tycho is running on Google Cloud against real public data: 132 Observations, 79 canonical Deltas, and 23 active evidenced claims across four competitors and eight source channels. The latest autonomous weekly session pinned all 23 active claim versions, proposed one card, rejected it, and published an empty brief. The dashboard and production verifiers read those stores directly; the submission is not driven by fixtures or a prerecorded happy path.

The result I am proudest of is an empty brief.

In the first completed production session, the Strategist proposed two market conclusions. Python rejected one for unsupported causation. It rejected the other for unsupported causation, using only one entity, and relying on one source family after mirrored evidence was collapsed. I did not lower the threshold to improve the demo. Tycho published the honest result: no defensible cross-entity pattern survived.

That is the product working. The goal is not to ensure that an agent always has something to say. It is to know when the agent has earned the right to say it.

I then triggered the same period again. Firestore returned the existing session and brief through the shared lease, and the Agent Runtime trace contained no model-generation span. The duplicate was not merely cleaned up afterwards; it made no Gemini call.

The provenance and security claims are similarly inspectable. Both managed Runtimes are visible in Agent Registry. Their Agent Identities have narrow, verified IAM. The dashboard has a separate read-only identity. Runtime traces preserve structure and usage while removing governed prose before export. Any brief citation resolves to the exact claim version and grounded source change behind it.

## What I learned

Autonomy does not require giving a model broad freedom. Tycho works unattended because each agent has one bounded responsibility, durable state, a narrow identity, and a clear failure path. The agents provide judgment; Google Cloud provides the scheduling, isolation, delivery, transactions, and audit trail that make that judgment operational.

Inference was the inexpensive part. The first production strategy session cost less than one cent. The costly work was making its output trustworthy: defining evidence independence, separating facts from beliefs, preserving exact versions, redacting traces, proving IAM, designing retries, and writing the tests that keep both models and deployment tools inside their boundaries.

A failure is useful when it is first-class. The two broken Strategy Council sessions revealed the real ADK provider contract, remained visible for audit, changed no governed data, and became retryable after the fix. Hiding them would have made the submission look cleaner and the system less credible.

Most importantly, evidence quantity is not evidence quality. Two URLs can be one witness. An exact quote can describe the wrong product. A Challenger's approval is not additional evidence. Tycho encodes those distinctions in Python rather than hoping every prompt remembers them.

## What's next for Tycho

The next agents should improve the fleet itself. An **Auditor** can inspect the claim graph for stale evidence, contradictions, broken provenance, and gaps in coverage. A **Repairer** can retry failed fetches and semantic generations, detect source-format drift, and turn repeated operational failures into governed source claims.

I also want to add RSS, search, and more entity registries, then test Tycho outside the coding-agent market. The architecture is designed to be domain-neutral, but a YAML registry is not proof of generality; another market will expose different evidence and staleness rules.

As the fleet grows, Agent Gateway and Model Armor are the natural next managed boundaries. I would keep Firestore claims as the exact authoritative ledger while evaluating Vertex AI Memory Bank for non-authoritative operational recall—places where semantic retrieval is useful but a paraphrase cannot alter a governed belief.

The larger goal is an institutional agent fleet that can work for weeks without a person watching it and still answer two questions about every conclusion: **Why do you believe this? What would make you change your mind?**

## Try it out

- [GitHub repository](https://github.com/nihsett/tycho)
- [Architecture diagram](https://raw.githubusercontent.com/nihsett/tycho/main/docs/diagrams/architecture-v2.png)
- [Live Intelligence Dashboard](https://tycho-dashboard-u2s544lf5a-uc.a.run.app)
