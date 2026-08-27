import type { OverviewResponse } from "../lib/types";
import { CompetitorCard } from "./CompetitorCard";

interface Props {
  overview: OverviewResponse | null;
  error: string | null;
  selected: string | null;
  onSelect: (entity: string) => void;
  onOpenClaim: (claimId: string, version: number) => void;
}

export function CompetitorGrid({ overview, error, selected, onSelect, onOpenClaim }: Props) {
  if (error) {
    return (
      <p className="error" role="status">
        The competitor overview could not be loaded: {error}
      </p>
    );
  }
  if (!overview) {
    return (
      <p className="loading" role="status">
        Loading competitors…
      </p>
    );
  }
  if (overview.entities.length === 0) {
    return (
      <p className="empty">
        No competitor is configured. Tycho watches the entities listed in tycho.yaml.
      </p>
    );
  }
  return (
    <div className="competitor-grid">
      {overview.entities.map((card) => (
        <CompetitorCard
          key={card.entity}
          card={card}
          selected={selected === card.entity}
          onSelect={onSelect}
          onOpenClaim={onOpenClaim}
        />
      ))}
    </div>
  );
}
