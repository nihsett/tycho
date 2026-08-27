import type { EntityCard } from "../lib/types";
import { entityLabel } from "../lib/format";

interface Props {
  card: EntityCard;
  selected: boolean;
  onSelect: (entity: string) => void;
  onOpenClaim: (claimId: string, version: number) => void;
}

export function CompetitorCard({ card, selected, onSelect, onOpenClaim }: Props) {
  const viewEvidence = () => {
    onSelect(card.entity);
    if (card.notable_claim) {
      onOpenClaim(card.notable_claim.claim_id, card.notable_claim.version);
    }
  };

  return (
    <article className="competitor-card" data-selected={selected}>
      <div className="competitor-card-heading">
        <h3>{card.name}</h3>
        <div className="status-badges" aria-label={`${card.name} status`}>
          {card.stale ? (
            <span className="badge" data-tone="stale">
              stale
            </span>
          ) : null}
          {card.disputed ? (
            <span className="badge" data-tone="disputed">
              disputed
            </span>
          ) : null}
          {!card.stale && !card.disputed ? (
            <span className="badge" data-tone="quiet">
              monitored
            </span>
          ) : null}
        </div>
      </div>

      <div className="change-block">
        <p className="card-label">Latest meaningful change</p>
        <p className="change">
          {card.latest_change
            ? card.latest_change.statement
            : "No meaningful change has cleared the evidence bar yet."}
        </p>
      </div>

      <div className="competitor-card-footer">
        <div className="fact-count">
          <strong>{card.active_fact_count}</strong>
          <span>verified facts</span>
        </div>
        <button
          type="button"
          className="evidence-action"
          onClick={viewEvidence}
          disabled={!card.notable_claim}
          aria-label={
            card.notable_claim
              ? `View evidence for ${card.name}`
              : `View evidence for ${entityLabel(card.entity)} when a claim is available`
          }
        >
          View evidence
          <span aria-hidden="true"> ↗</span>
        </button>
      </div>
    </article>
  );
}
