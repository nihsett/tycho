import type { TimelineResponse } from "../lib/types";
import { entityLabel, formatTimestamp, scopeLabel, sourceLabel } from "../lib/format";

interface Props {
  timeline: TimelineResponse | null;
  loading: boolean;
  error: string | null;
  entities: string[];
  scopes: string[];
  entity: string;
  scope: string | null;
  showArchive: boolean;
  onEntityChange: (entity: string) => void;
  onScopeChange: (scope: string | null) => void;
  onArchiveChange: (showArchive: boolean) => void;
  onOpenClaim: (claimId: string, version: number) => void;
  onLoadMore: () => void;
}

const KIND_TEXT: Record<string, string> = {
  created: "added",
  verified: "verified",
  disputed: "disputed",
  superseded: "replaced",
  retired: "retired",
};

const FRIENDLY_NOTE: Record<string, string> = {
  created: "Added to Tycho’s current beliefs",
  verified: "Still supported by a new observation",
  disputed: "A conflicting signal was recorded",
  superseded: "A newer belief replaced this one",
  retired: "Moved to history",
};

export function BeliefTimeline({
  timeline,
  loading,
  error,
  entities,
  scopes,
  entity,
  scope,
  showArchive,
  onEntityChange,
  onScopeChange,
  onArchiveChange,
  onOpenClaim,
  onLoadMore,
}: Props) {
  const visibleEvents =
    timeline?.events.filter(
      (event) => showArchive || (event.kind !== "retired" && event.claim.status !== "retired"),
    ) ?? [];
  const hiddenCount = (timeline?.events.length ?? 0) - visibleEvents.length;

  return (
    <section className="panel history-panel" aria-labelledby="belief-timeline-heading">
      <div className="section-heading-row">
        <div>
          <p className="eyebrow">Evidence and history</p>
          <h2 id="belief-timeline-heading">How Tycho’s beliefs changed</h2>
          <p className="panel-note">
            Recent belief changes stay here. Open any evidence chip to inspect the
            exact claim and its supporting source.
          </p>
        </div>
        <div className="history-mode">
          <label htmlFor="timeline-history">View</label>
          <select
            id="timeline-history"
            value={showArchive ? "archive" : "current"}
            onChange={(event) => onArchiveChange(event.target.value === "archive")}
          >
            <option value="current">Active and recent</option>
            <option value="archive">Include retired history</option>
          </select>
        </div>
      </div>

      <div className="filters">
        <label htmlFor="timeline-entity">Competitor</label>
        <select
          id="timeline-entity"
          value={entity}
          onChange={(event) => onEntityChange(event.target.value)}
        >
          {entities.map((key) => (
            <option key={key} value={key}>
              {entityLabel(key)}
            </option>
          ))}
        </select>

        <details className="technical-details timeline-technical">
          <summary>Technical details</summary>
          <div className="technical-filter">
            <label htmlFor="timeline-scope">Ontology scope</label>
            <select
              id="timeline-scope"
              value={scope ?? ""}
              onChange={(event) => onScopeChange(event.target.value || null)}
            >
              <option value="">Every scope</option>
              {scopes.map((key) => (
                <option key={key} value={key}>
                  {scopeLabel(key)}
                </option>
              ))}
            </select>
          </div>
          <p>
            Claim IDs, versions, ontology scopes, and Delta IDs are preserved in
            the provenance drawer for each event.
          </p>
        </details>
      </div>

      {error ? (
        <p className="error" role="status">
          The belief history could not be loaded: {error}
        </p>
      ) : null}

      {!error && loading && !timeline ? (
        <p className="loading" role="status">
          Loading recent belief history…
        </p>
      ) : null}

      {!error && timeline && visibleEvents.length === 0 ? (
        <p className="empty">
          {hiddenCount > 0
            ? "Retired migration records are hidden. Choose Include retired history to inspect them."
            : "Nothing has entered or left Tycho’s current belief set for this filter. Tycho is waiting for the next meaningful canonical change."}
        </p>
      ) : null}

      {visibleEvents.length > 0 ? (
        <>
          <ol className="timeline">
            {visibleEvents.map((event) => (
              <li key={event.event_id} data-kind={event.kind}>
                <div className="event-head">
                  <span className="kind">{KIND_TEXT[event.kind] ?? event.kind}</span>
                  <span className="event-time">{formatTimestamp(event.at)}</span>
                  {event.claim.stale ? (
                    <span className="badge" data-tone="stale">
                      stale
                    </span>
                  ) : null}
                </div>

                <p className="event-statement">
                  <button
                    type="button"
                    className="claim-link"
                    onClick={() => onOpenClaim(event.claim.claim_id, event.claim.version)}
                  >
                    {event.claim.statement}
                  </button>
                </p>
                <p className="event-note">{FRIENDLY_NOTE[event.kind] ?? event.note}</p>

                {event.replacement ? (
                  <div className="replacement">
                    <span className="arrow">replaced by</span>
                    <p className="event-statement">
                      <button
                        type="button"
                        className="claim-link"
                        onClick={() =>
                          onOpenClaim(event.replacement!.claim_id, event.replacement!.version)
                        }
                      >
                        {event.replacement.statement}
                      </button>
                    </p>
                  </div>
                ) : null}

                {event.evidence.length > 0 ? (
                  <div className="chips">
                    {event.evidence.map((chip) => (
                      <button
                        key={chip.delta_id}
                        type="button"
                        className="chip"
                        data-canonical={chip.canonical}
                        onClick={() => onOpenClaim(event.claim.claim_id, event.claim.version)}
                        aria-label={`View evidence for ${sourceLabel(chip.source)}`}
                      >
                        View evidence · {sourceLabel(chip.source)}
                      </button>
                    ))}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>

          <p className="panel-note history-count">
            Showing {visibleEvents.length} {showArchive ? "history" : "current/recent"} entr{visibleEvents.length === 1 ? "y" : "ies"}.
            {hiddenCount > 0 && !showArchive
              ? ` ${hiddenCount} retired ${hiddenCount === 1 ? "record is" : "records are"} hidden.`
              : ""}
          </p>
          {timeline?.next_offset !== null ? (
            <button type="button" className="ghost" onClick={onLoadMore} disabled={loading}>
              {loading ? "Loading…" : "Load more history"}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
