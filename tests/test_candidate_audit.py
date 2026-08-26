"""Synthetic coverage for the read-only candidate audit.

Every fixture here is built in memory. The tests never touch BigQuery, GCS,
Firestore, Pub/Sub, Scheduler, or a model provider.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from infra.audit_semantic_candidate import (
    CandidateAuditError,
    audit_candidate,
    decode_delta_row,
    main,
    proposal_from_delta,
    quote_fingerprint,
)
from pipeline.cloud import canonical_delta_row_for_bigquery
from pipeline.semantic_differ import (
    GENERATED_BY,
    PROMPT_VERSION,
    SemanticDeltaProposal,
    build_comparison_bundle,
    construct_delta,
)
from schemas.claim import Claim
from schemas.delta import Delta
from schemas.observation import Observation, ObservationKind, ObservationStatus

BASE_TIME = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
POWERSHELL_QUOTE = "Added an optional `powershell` tool for Windows."
SHARED_QUOTE = "Sub-agents can now run in parallel."


def _release(tag: str, body: str, *, volatile: int = 1) -> dict:
    return {
        "id": volatile,
        "html_url": f"https://example.invalid/{volatile}",
        "tag_name": tag,
        "name": tag,
        "body": body,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-01T00:00:00Z",
    }


def _payload(releases: list[dict]) -> bytes:
    return json.dumps(releases).encode("utf-8")


def _page(sections: list[tuple[str, str]]) -> bytes:
    return json.dumps(
        {
            "title": "Changelog",
            "sections": [{"path": path, "content": content} for path, content in sections],
        }
    ).encode("utf-8")


def _observation(
    obs_id: str,
    payload: bytes,
    *,
    entity: str = "pi",
    source: str = "github_releases",
    fetched_at: datetime = BASE_TIME,
    status: ObservationStatus = ObservationStatus.OK,
) -> Observation:
    return Observation(
        obs_id=obs_id,
        entity=entity,
        source=source,
        kind=ObservationKind.STRUCTURED,
        fetched_at=fetched_at,
        content_ref=f"gs://tycho-raw/{entity}/{source}/{obs_id}.json",
        content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        adapter_ver="github@1",
        status=status,
    )


def _obs_id(suffix: str) -> str:
    return f"obs_01ARZ3NDEKTSV4RRFFQ69G5{suffix}"


def _claim_id(suffix: str) -> str:
    return f"clm_01ARZ3NDEKTSV4RRFFQ69G5{suffix}"


def _meaningful_proposal(quote: str, statement: str) -> SemanticDeltaProposal:
    return SemanticDeltaProposal.model_validate(
        {
            "status": "meaningful",
            "summary": statement,
            "reason": "The after snapshot states a durable user-facing change.",
            "changes": [
                {
                    "category": "capability",
                    "scope": "product/capabilities",
                    "statement": statement,
                    "before": "",
                    "after": quote,
                    "evidence_before": "",
                    "evidence_after": quote,
                }
            ],
        }
    )


def _noise_proposal() -> SemanticDeltaProposal:
    return SemanticDeltaProposal.model_validate(
        {
            "status": "noise",
            "summary": "No durable product change was published.",
            "reason": "Only release bookkeeping changed between the snapshots.",
            "changes": [],
        }
    )


def _build_delta(
    proposal: SemanticDeltaProposal,
    before: Observation,
    after: Observation,
    before_payload: bytes,
    after_payload: bytes,
    *,
    computed_at: datetime | None = None,
) -> Delta:
    bundle = build_comparison_bundle(
        after.entity,
        after.source,
        before_payload,
        after_payload,
        obs_before=before.obs_id,
        obs_after=after.obs_id,
    )
    return construct_delta(
        proposal,
        entity=after.entity,
        source=after.source,
        obs_before=before.obs_id,
        obs_after=after.obs_id,
        computed_at=computed_at or (after.fetched_at + timedelta(minutes=3)),
        generated_by=GENERATED_BY,
        prompt_version=PROMPT_VERSION,
        before_snapshot=bundle.before,
        after_snapshot=bundle.after,
    )


def _claim(
    claim_id: str,
    delta: Delta,
    *,
    status: str = "active",
    entity: str | None = None,
    source: str | None = None,
    superseded_by: str | None = None,
    supersedes: str | None = None,
) -> dict:
    return Claim.model_validate(
        {
            "claim_id": claim_id,
            "entity": entity or delta.entity,
            "scope": "product/capabilities",
            "class": "fact",
            "statement": "The product gained a durable capability on 2026-08-20.",
            "rationale": "A capability change affects the user's tooling decision.",
            "confidence": "confirmed",
            "severity": "notable",
            "evidence": [
                {
                    "delta_id": delta.delta_id,
                    "source": source or delta.source,
                    "note": "primary source release note",
                }
            ],
            "status": status,
            "superseded_by": superseded_by,
            "supersedes": supersedes,
            "version": 1,
            "created_at": BASE_TIME.isoformat(),
            "last_verified_at": BASE_TIME.isoformat(),
            "created_by": "gemini-analyst@1",
        }
    ).model_dump(mode="json", by_alias=True)


class FakeReader:
    """In-memory stand-in for the read-only cloud reader."""

    def __init__(
        self,
        *,
        deltas: list[Delta],
        observations: list[Observation],
        payloads: dict[str, bytes],
        claims: list[dict] | None = None,
        rows: list[dict] | None = None,
    ) -> None:
        self.candidate_table = "test-project.tycho.deltas_v2_candidate"
        self._rows = rows if rows is not None else [
            canonical_delta_row_for_bigquery(delta) for delta in deltas
        ]
        self._observations = observations
        self._payloads = payloads
        self._claims = claims or []
        self.payload_reads: list[str] = []

    def candidate_rows(self):
        return list(self._rows)

    def observation_documents(self):
        return [observation.model_dump(mode="json") for observation in self._observations]

    def raw_payload(self, content_ref: str) -> bytes:
        self.payload_reads.append(content_ref)
        try:
            return self._payloads[content_ref]
        except KeyError as exc:
            raise FileNotFoundError(content_ref) from exc

    def claim_documents(self):
        return list(self._claims)


def _clean_world():
    """One meaningful pair, one identical-noise pair, one changed-noise pair."""
    before_payload = _payload([_release("v1.0.0", "Initial release.")])
    after_payload = _payload(
        [
            _release("v1.1.0", POWERSHELL_QUOTE, volatile=2),
            _release("v1.0.0", "Initial release."),
        ]
    )
    # Same normalized snapshot; only volatile GitHub metadata differs.
    identical_payload = _payload(
        [
            _release("v1.1.0", POWERSHELL_QUOTE, volatile=99),
            _release("v1.0.0", "Initial release.", volatile=98),
        ]
    )
    changed_payload = _payload(
        [
            _release("v1.1.1", "Fixed a typo in the help output.", volatile=3),
            _release("v1.1.0", POWERSHELL_QUOTE, volatile=2),
        ]
    )

    before = _observation(_obs_id("AAA"), before_payload)
    after = _observation(_obs_id("AAB"), after_payload, fetched_at=BASE_TIME + timedelta(days=1))
    identical = _observation(
        _obs_id("AAC"), identical_payload, fetched_at=BASE_TIME + timedelta(days=2)
    )
    changed = _observation(
        _obs_id("AAD"), changed_payload, fetched_at=BASE_TIME + timedelta(days=3)
    )

    meaningful = _build_delta(
        _meaningful_proposal(POWERSHELL_QUOTE, "Pi added native PowerShell execution on Windows."),
        before,
        after,
        before_payload,
        after_payload,
    )
    identical_noise = _build_delta(
        _noise_proposal(), after, identical, after_payload, identical_payload
    )
    changed_noise = _build_delta(
        _noise_proposal(), identical, changed, identical_payload, changed_payload
    )

    observations = [before, after, identical, changed]
    payloads = {
        before.content_ref: before_payload,
        after.content_ref: after_payload,
        identical.content_ref: identical_payload,
        changed.content_ref: changed_payload,
    }
    return [meaningful, identical_noise, changed_noise], observations, payloads


def test_clean_candidate_audit_passes_with_bounded_counts():
    deltas, observations, payloads = _clean_world()
    reader = FakeReader(
        deltas=deltas,
        observations=observations,
        payloads=payloads,
        claims=[_claim(_claim_id("CA1"), deltas[0])],
    )

    report = audit_candidate(reader, project="test-project", dataset="tycho", table="candidate")

    assert report["ok"] is True
    assert report["hard_failures"]["total"] == 0
    assert report["counts"]["candidate_rows"] == 3
    assert report["counts"]["loaded_deltas"] == 3
    assert report["counts"]["meaningful"] == 1
    assert report["counts"]["noise"] == 2
    assert report["counts"]["meaningful_changes"] == 1
    assert report["counts"]["revalidated_deltas"] == 3
    assert report["counts"]["observation_pairs_hash_verified"] == 3
    assert report["counts"]["distinct_comparison_ids"] == 3
    assert report["read_only"] is True
    assert report["model_provider_calls"] == 0
    assert report["noise_pairs"]["normalized_identical"]["count"] == 1
    assert report["noise_pairs"]["normalized_changed"]["count"] == 1
    assert report["noise_pairs"]["normalized_identical"]["ids"] == [deltas[1].delta_id]
    assert report["noise_pairs"]["normalized_changed"]["ids"] == [deltas[2].delta_id]
    assert report["claims"]["active"] == 1
    assert report["claims"]["active_by_class"] == {"fact": 1}
    assert report["claims"]["active_resolving_to_meaningful_candidate"]["count"] == 1
    assert report["claims"]["active_with_valid_lifecycle_links"]["count"] == 1
    assert report["by_entity_source"] == [
        {"entity": "pi", "source": "github_releases", "rows": 3, "meaningful": 1, "noise": 2}
    ]


def test_audit_reads_each_raw_payload_once():
    deltas, observations, payloads = _clean_world()
    reader = FakeReader(deltas=deltas, observations=observations, payloads=payloads)

    audit_candidate(reader)

    assert len(reader.payload_reads) == len(set(reader.payload_reads)) == 4


def test_report_never_contains_source_or_claim_text():
    deltas, observations, payloads = _clean_world()
    reader = FakeReader(
        deltas=deltas,
        observations=observations,
        payloads=payloads,
        claims=[_claim(_claim_id("CA1"), deltas[0])],
    )

    text = json.dumps(audit_candidate(reader))

    for secret in (
        POWERSHELL_QUOTE,
        "Initial release.",
        "Pi added native PowerShell execution on Windows.",
        "Only release bookkeeping changed between the snapshots.",
        "The product gained a durable capability on 2026-08-20.",
        "A capability change affects the user's tooling decision.",
    ):
        assert secret not in text


def test_tampered_comparison_id_is_a_hard_failure():
    deltas, observations, payloads = _clean_world()
    rows = [canonical_delta_row_for_bigquery(delta) for delta in deltas]
    rows[0]["comparison_id"] = "sha256:" + "b" * 64
    reader = FakeReader(
        deltas=deltas, observations=observations, payloads=payloads, rows=rows
    )

    report = audit_candidate(reader)

    assert report["ok"] is False
    assert report["hard_failures"]["by_class"] == {"comparison_id_mismatch": 1}
    assert report["hard_failures"]["items"][0]["subject"] == deltas[0].delta_id


def test_rewritten_raw_payload_fails_the_hash_check():
    deltas, observations, payloads = _clean_world()
    tampered = dict(payloads)
    tampered[observations[1].content_ref] = _payload(
        [_release("v1.1.0", "Something else entirely.", volatile=2)]
    )
    reader = FakeReader(deltas=deltas, observations=observations, payloads=tampered)

    report = audit_candidate(reader)

    assert report["ok"] is False
    assert report["hard_failures"]["by_class"]["raw_payload_hash_mismatch"] >= 1


def test_missing_raw_payload_is_reported_without_a_traceback():
    deltas, observations, payloads = _clean_world()
    missing = {ref: value for ref, value in payloads.items() if ref != observations[0].content_ref}
    reader = FakeReader(deltas=deltas, observations=observations, payloads=missing)

    report = audit_candidate(reader)

    assert report["hard_failures"]["by_class"]["raw_payload_unavailable"] == 1
    assert report["hard_failures"]["items"][0]["detail"] == "FileNotFoundError"


def test_ungrounded_evidence_fails_the_rerun_of_current_validation():
    before_payload = _payload([_release("v1.0.0", "Initial release.")])
    after_payload = _payload([_release("v1.1.0", POWERSHELL_QUOTE, volatile=2)])
    before = _observation(_obs_id("BBA"), before_payload)
    after = _observation(_obs_id("BBB"), after_payload, fetched_at=BASE_TIME + timedelta(days=1))
    delta = _build_delta(
        _meaningful_proposal(POWERSHELL_QUOTE, "Pi added native PowerShell execution."),
        before,
        after,
        before_payload,
        after_payload,
    )
    row = canonical_delta_row_for_bigquery(delta)
    row["changes"][0]["evidence_after"]["quote"] = "Pi removed the Windows integration."
    row["changes"][0]["after"] = json.dumps("Pi removed the Windows integration.")
    reader = FakeReader(
        deltas=[delta],
        observations=[before, after],
        payloads={before.content_ref: before_payload, after.content_ref: after_payload},
        rows=[row],
    )

    report = audit_candidate(reader)

    assert report["ok"] is False
    assert report["hard_failures"]["by_class"] == {"grounding_failed": 1}
    assert report["counts"]["revalidated_deltas"] == 0


def test_evidence_after_already_present_in_obs_before_is_an_advisory():
    before_payload = _payload([_release("v1.0.0", POWERSHELL_QUOTE)])
    after_payload = _payload(
        [_release("v1.1.0", POWERSHELL_QUOTE, volatile=2), _release("v1.0.0", POWERSHELL_QUOTE)]
    )
    before = _observation(_obs_id("CCA"), before_payload)
    after = _observation(_obs_id("CCB"), after_payload, fetched_at=BASE_TIME + timedelta(days=1))
    delta = _build_delta(
        _meaningful_proposal(POWERSHELL_QUOTE, "Pi added native PowerShell execution."),
        before,
        after,
        before_payload,
        after_payload,
    )
    reader = FakeReader(
        deltas=[delta],
        observations=[before, after],
        payloads={before.content_ref: before_payload, after.content_ref: after_payload},
    )

    report = audit_candidate(reader)

    assert report["ok"] is True
    assert report["advisories"]["by_class"] == {"evidence_after_present_in_obs_before": 1}
    item = report["advisories"]["items"][0]
    assert item["subject"] == delta.delta_id
    assert item["change_index"] == 0
    assert item["quote_fingerprint"] == quote_fingerprint(POWERSHELL_QUOTE)
    assert POWERSHELL_QUOTE not in json.dumps(report)


def test_cross_source_mirrored_evidence_is_informational_only():
    releases_before = _payload([_release("v1.0.0", "Initial release.")])
    releases_after = _payload([_release("v1.1.0", SHARED_QUOTE, volatile=2)])
    page_before = _page([("Changelog/v1.0.0", "Initial release.")])
    page_after = _page([("Changelog/v1.1.0", SHARED_QUOTE)])

    gh_before = _observation(_obs_id("DDA"), releases_before)
    gh_after = _observation(
        _obs_id("DDB"), releases_after, fetched_at=BASE_TIME + timedelta(days=1)
    )
    web_before = _observation(
        _obs_id("DDC"), page_before, source="website_changelog", fetched_at=BASE_TIME
    )
    web_after = _observation(
        _obs_id("DDD"),
        page_after,
        source="website_changelog",
        fetched_at=BASE_TIME + timedelta(days=1),
    )

    statement = "Pi runs sub-agents in parallel."
    gh_delta = _build_delta(
        _meaningful_proposal(SHARED_QUOTE, statement),
        gh_before,
        gh_after,
        releases_before,
        releases_after,
    )
    web_delta = _build_delta(
        _meaningful_proposal(SHARED_QUOTE, statement),
        web_before,
        web_after,
        page_before,
        page_after,
    )
    reader = FakeReader(
        deltas=[gh_delta, web_delta],
        observations=[gh_before, gh_after, web_before, web_after],
        payloads={
            gh_before.content_ref: releases_before,
            gh_after.content_ref: releases_after,
            web_before.content_ref: page_before,
            web_after.content_ref: page_after,
        },
    )

    report = audit_candidate(reader)

    assert report["ok"] is True
    assert report["hard_failures"]["total"] == 0
    assert report["advisories"]["by_class"] == {"cross_source_mirrored_evidence": 1}
    mirrored = report["advisories"]["items"][0]
    assert mirrored["subject"] == "pi"
    assert mirrored["sources"] == ["github_releases", "website_changelog"]
    assert sorted(mirrored["delta_ids"]) == sorted([gh_delta.delta_id, web_delta.delta_id])
    assert SHARED_QUOTE not in json.dumps(report)


def test_claim_backed_by_noise_or_unknown_delta_fails():
    deltas, observations, payloads = _clean_world()
    unknown = deltas[0].model_copy(update={"delta_id": "dlt_01ARZ3NDEKTSV4RRFFQ69G5ZZZ"})
    reader = FakeReader(
        deltas=deltas,
        observations=observations,
        payloads=payloads,
        claims=[
            _claim(_claim_id("CB2"), deltas[1]),
            _claim(_claim_id("CC3"), unknown),
            _claim(_claim_id("CD4"), deltas[0], entity="codex"),
            _claim(_claim_id("CE5"), deltas[0], source="website_changelog"),
        ],
    )

    report = audit_candidate(reader)

    assert report["ok"] is False
    assert report["hard_failures"]["by_class"] == {
        "claim_evidence_not_meaningful": 1,
        "claim_evidence_unresolved": 1,
        "claim_entity_mismatch": 1,
        "claim_source_mismatch": 1,
    }
    assert report["claims"]["active"] == 4
    assert report["claims"]["active_resolving_to_meaningful_candidate"]["count"] == 0
    assert report["claims"]["active_with_valid_lifecycle_links"]["count"] == 4


def test_broken_supersession_link_is_a_hard_failure():
    deltas, observations, payloads = _clean_world()
    old = _claim(_claim_id("CF6"), deltas[0], status="active")
    new = _claim(_claim_id("CG7"), deltas[0], supersedes=old["claim_id"])
    reader = FakeReader(
        deltas=deltas,
        observations=observations,
        payloads=payloads,
        claims=[old, new],
    )

    report = audit_candidate(reader)

    assert report["hard_failures"]["by_class"] == {"claim_supersession_link_broken": 1}
    assert report["claims"]["active_with_valid_lifecycle_links"]["count"] == 1


def test_unloadable_rows_and_documents_are_classified_not_raised():
    deltas, observations, payloads = _clean_world()
    rows = [canonical_delta_row_for_bigquery(delta) for delta in deltas]
    rows[0]["triage"] = "pending"
    reader = FakeReader(
        deltas=deltas,
        observations=observations,
        payloads=payloads,
        rows=rows,
        claims=[{"claim_id": "not-a-ulid"}],
    )

    report = audit_candidate(reader)

    assert report["counts"]["loaded_deltas"] == 2
    assert report["hard_failures"]["by_class"] == {
        "claim_document_not_loadable": 1,
        "delta_row_not_loadable": 1,
    }
    detail = next(
        item["detail"]
        for item in report["hard_failures"]["items"]
        if item["class"] == "delta_row_not_loadable"
    )
    assert "pending" not in detail


def test_chronology_and_identity_problems_are_detected():
    deltas, observations, payloads = _clean_world()
    swapped = [
        observations[0].model_copy(update={"fetched_at": BASE_TIME + timedelta(days=5)}),
        observations[1].model_copy(update={"entity": "codex"}),
        observations[2].model_copy(update={"fetched_at": BASE_TIME + timedelta(days=10)}),
        observations[3].model_copy(update={"status": ObservationStatus.QUARANTINED}),
    ]
    reader = FakeReader(deltas=deltas, observations=swapped, payloads=payloads)

    report = audit_candidate(reader)

    classes = report["hard_failures"]["by_class"]
    assert classes["chronology_out_of_order"] >= 1
    assert classes["observation_identity_mismatch"] >= 1
    assert classes["observation_not_clean"] == 1
    assert classes["computed_at_precedes_observation"] >= 1


def test_missing_observation_stops_that_row_only():
    deltas, observations, payloads = _clean_world()
    reader = FakeReader(deltas=deltas, observations=observations[1:], payloads=payloads)

    report = audit_candidate(reader)

    assert report["hard_failures"]["by_class"] == {"observation_missing": 1}
    assert report["counts"]["revalidated_deltas"] == 2


def test_decode_round_trips_a_canonical_bigquery_row():
    deltas, _, _ = _clean_world()
    row = canonical_delta_row_for_bigquery(deltas[0])

    restored = decode_delta_row(row)

    assert restored == deltas[0]
    assert proposal_from_delta(restored).status == "meaningful"


def test_main_exits_nonzero_on_hard_failure_and_writes_only_the_report(tmp_path, monkeypatch, capsys):
    deltas, observations, payloads = _clean_world()
    rows = [canonical_delta_row_for_bigquery(delta) for delta in deltas]
    rows[0]["comparison_id"] = "sha256:" + "c" * 64

    def _reader(**_kwargs):
        return FakeReader(
            deltas=deltas, observations=observations, payloads=payloads, rows=rows
        )

    monkeypatch.setattr("infra.audit_semantic_candidate.CloudCandidateReader", _reader)
    output = tmp_path / "audit.json"

    code = main(["--project", "test-project", "--output", str(output)])

    assert code == 1
    written = json.loads(output.read_text())
    assert written["ok"] is False
    assert written["hard_failures"]["by_class"] == {"comparison_id_mismatch": 1}
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_main_exits_zero_on_a_clean_candidate(monkeypatch, capsys):
    deltas, observations, payloads = _clean_world()

    def _reader(**_kwargs):
        return FakeReader(deltas=deltas, observations=observations, payloads=payloads)

    monkeypatch.setattr("infra.audit_semantic_candidate.CloudCandidateReader", _reader)

    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_reader_failure_exits_with_the_setup_code(monkeypatch, capsys):
    def _reader(**_kwargs):
        raise CandidateAuditError("cannot read `tycho.deltas_v2_candidate`")

    monkeypatch.setattr("infra.audit_semantic_candidate.CloudCandidateReader", _reader)

    assert main([]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_unclassified_failure_class_is_refused(monkeypatch):
    deltas, observations, payloads = _clean_world()
    monkeypatch.setattr(
        "infra.audit_semantic_candidate.HARD_FAILURE_CLASSES", frozenset()
    )
    rows = [canonical_delta_row_for_bigquery(delta) for delta in deltas]
    rows[0]["comparison_id"] = "sha256:" + "d" * 64
    reader = FakeReader(
        deltas=deltas, observations=observations, payloads=payloads, rows=rows
    )

    with pytest.raises(CandidateAuditError, match="unclassified failure classes"):
        audit_candidate(reader)
