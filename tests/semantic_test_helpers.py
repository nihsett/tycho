"""Deterministic semantic provider used by local tests; never used in production."""

from datetime import UTC, datetime

from pipeline.semantic_differ import (
    SemanticDeltaProposal,
    SemanticModelResult,
    construct_delta,
    searchable_text,
)
from schemas.delta import ChangeCategory, ChangeScope


class FakeSemanticDiffer:
    model = "gemini-3.7-flash"

    def compare_bundle(
        self,
        bundle,
        *,
        obs_before,
        obs_after,
        computed_at=None,
        generated_by,
        prompt_version,
    ):
        before_fields = set(searchable_text(bundle.before))
        quote = next(
            (field for field in searchable_text(bundle.after) if field not in before_fields),
            next(iter(searchable_text(bundle.after))),
        )
        proposal = SemanticDeltaProposal(
            status="meaningful",
            summary="A durable semantic product change was observed.",
            reason="The test provider supplies one grounded durable change.",
            changes=[
                {
                    "category": ChangeCategory.CAPABILITY,
                    "scope": ChangeScope.PRODUCT_CAPABILITIES,
                    "statement": "The product gained a durable user-facing capability.",
                    "before": "",
                    "after": quote,
                    "evidence_before": "",
                    "evidence_after": quote,
                }
            ],
        )
        delta = construct_delta(
            proposal,
            entity=bundle.entity,
            source=bundle.source,
            obs_before=obs_before,
            obs_after=obs_after,
            computed_at=computed_at or datetime.now(UTC),
            generated_by=generated_by,
            prompt_version=prompt_version,
            before_snapshot=bundle.before,
            after_snapshot=bundle.after,
        )
        return SemanticModelResult(
            proposal=proposal,
            delta=delta,
            usage={
                "input_tokens": bundle.estimated_input_tokens,
                "output_tokens": 20,
                "thinking_tokens": 0,
                "total_tokens": bundle.estimated_input_tokens + 20,
                "estimated_cost_usd": 0.0,
            },
            latency_ms=0,
            input_bytes=bundle.input_bytes,
            estimated_input_tokens=bundle.estimated_input_tokens,
        )
