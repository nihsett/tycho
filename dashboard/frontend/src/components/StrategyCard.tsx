import type { CardView } from "../lib/types";
import { entityLabel, scopeLabel } from "../lib/format";

interface Props {
  card: CardView;
  onOpenClaim: (claimId: string, version: number) => void;
}

export function StrategyCard({ card, onOpenClaim }: Props) {
  const rejected = card.status === "rejected";

  return (
    <article className="strategy-card" data-status={card.status}>
      <p className="card-label">{rejected ? "Challenged conclusion" : "Verified conclusion"}</p>
      <h3>{card.statement}</h3>

      <div className="tag-row">
        <span className="tag" data-tone={rejected ? "fail" : "pass"}>
          {rejected ? "Rejected" : "Passed verification"}
        </span>
        <span className="tag">Confidence: {card.confidence}</span>
        {card.entities.map((entity) => (
          <span key={entity} className="tag">
            {entityLabel(entity)}
          </span>
        ))}
      </div>

      <dl className="strategy-explanation">
        <dt>{rejected ? "Why it was challenged" : "Why Tycho believes it"}</dt>
        <dd>{rejected ? card.rejection_reasons.join(" ") : card.rationale}</dd>
        {!rejected ? (
          <>
            <dt>What would change our mind</dt>
            <dd>{card.falsifier}</dd>
          </>
        ) : null}
      </dl>

      {card.premises.length > 0 ? (
        <div className="premise-row">
          <span className="card-label">Evidence behind this conclusion</span>
          <div className="chips">
            {card.premises.map((premise) => (
              <button
                key={`${premise.claim_id}-${premise.claim_version}`}
                type="button"
                className="chip"
                data-canonical={premise.resolved}
                onClick={() => onOpenClaim(premise.claim_id, premise.claim_version)}
                aria-label={`View evidence for ${premise.entity ? entityLabel(premise.entity) : "this"} belief`}
              >
                View evidence · {premise.entity ? entityLabel(premise.entity) : "belief"}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <details className="technical-details">
        <summary>Technical details</summary>
        <dl className="kv compact-kv">
          <dt>Card ID</dt>
          <dd className="id">{card.card_id}</dd>
          <dt>Challenger</dt>
          <dd>{card.challenger_verdict ?? "not reached"}</dd>
          <dt>Scopes</dt>
          <dd>{card.scopes.map(scopeLabel).join(" · ") || "none recorded"}</dd>
          <dt>Source families</dt>
          <dd>{card.source_families.join(" · ") || "none recorded"}</dd>
          <dt>Premise pins</dt>
          <dd>
            {card.premises.length > 0
              ? card.premises.map((premise) => `${premise.claim_id}@v${premise.claim_version}`).join(", ")
              : "none recorded"}
          </dd>
          <dt>Competing explanation</dt>
          <dd>{card.competing_explanation}</dd>
        </dl>
      </details>
    </article>
  );
}
