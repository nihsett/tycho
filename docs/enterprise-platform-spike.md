# Gemini Enterprise Agent Platform spike

Status: **passing, non-production, live resources untouched**.

## What was proved

| Capability | Evidence |
|---|---|
| Agent Runtime | `Tycho Platform Probe` is deployed in `us-central1` as reasoning engine `8123514612671840256`; two remote invocations returned the expected three-event ADK tool flow. |
| Agent Registry | Agent Runtime automatically registered `Tycho Platform Probe` as `agentregistry-00000000-0000-0000-f78b-5b25b5318bbc`. |
| Agent Identity | The runtime has its own managed identity: `agents.global.proj-548847028907.system.id.goog/resources/aiplatform/projects/548847028907/locations/us-central1/reasoningEngines/8123514612671840256`. |
| Observability | OpenTelemetry trace `6df87613b2397a8802335346b07aaa8d` contains seven linked spans: workflow, agent, two `call_llm`, two Gemini generation, and `execute_tool platform_probe`. |
| Gemini requirement | Both remote calls used `gemini-3.5-flash-lite` through Vertex/enterprise authentication. |
| Least privilege | The probe has no Tycho data tools. Its only explicit project grant is `roles/telemetry.tracesWriter`; model, session, and basic runtime access come from managed Agent Identity defaults. |

The second successful session ID is `915818350427242496`. Its final response was:

```text
status: ready
component: agent-runtime
version: tycho-platform-spike@1
```

## Resources created

- Agent Runtime: `projects/548847028907/locations/us-central1/reasoningEngines/8123514612671840256`
- Automatic Registry entry: `projects/gen-lang-client-0110801105/locations/us-central1/agents/agentregistry-00000000-0000-0000-f78b-5b25b5318bbc`
- Staging bucket: `gs://gen-lang-client-0110801105-tycho-agent-staging`
- Enabled APIs: Agent Identity, Agent Registry, App Hub, Monitoring, Resource Manager, Service Usage, and Storage. Existing Agent Platform, Logging, Telemetry, and Trace APIs remain enabled.

The runtime is capped at one instance and scales to zero. The live `tycho-analyst` Cloud Run revision, Pub/Sub push endpoint, and `tycho-nightly` scheduler were checked after the spike and remain unchanged.

## Reproduce or verify

```bash
GOOGLE_CLOUD_QUOTA_PROJECT=gen-lang-client-0110801105 \
GOOGLE_CLOUD_PROJECT=gen-lang-client-0110801105 \
uv run python -m infra.platform_spike verify
```

Deployment metadata is kept in the gitignored `data/platform_spike.json`. The deploy command refuses to create a duplicate while that state exists.

## Migration consequences

1. Package the real analyst as an `AdkApp` and include all local modules in `extra_packages`; object deployment does not infer local package dependencies.
2. Give the analyst identity only the exact Firestore, BigQuery, and alert permissions its tools require.
3. Keep Firestore claims as authoritative memory. Do not enable Memory Bank retrieval for factual analysis until a safe operational-memory use exists.
4. Standard Agent Runtime traces include complete model request and response payloads in `gcp.vertex.agent.*` span labels even with `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`. Before sending real deltas, choose a redaction/content-capture policy and verify it against Cloud Trace.
5. Pub/Sub can continue targeting the existing Cloud Run service until a thin dispatcher to Agent Runtime is deliberately cut over.

## Deployment lessons

- `GOOGLE_CLOUD_PROJECT` is reserved in Agent Runtime deployment environment variables.
- Local packages referenced by a pickled `AdkApp` must be listed in `extra_packages`.
- Agent Identity needs an explicit `roles/telemetry.tracesWriter` grant for persisted OTLP traces.
- Agent Runtime registration in Agent Registry is automatic and synchronized with runtime lifecycle.
