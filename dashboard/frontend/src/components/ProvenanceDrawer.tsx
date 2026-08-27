import { useCallback, useEffect, useRef } from "react";
import type { ProvenanceResponse } from "../lib/types";
import { formatTimestamp, scopeLabel, sourceLabel } from "../lib/format";

interface Props {
  open: boolean;
  loading: boolean;
  error: string | null;
  data: ProvenanceResponse | null;
  requested: { claimId: string; version: number } | null;
  onClose: () => void;
  onOpenClaim: (claimId: string, version: number) => void;
}

const FOCUSABLE =
  'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * The right-side provenance drawer.
 *
 * Grounded quotes are untrusted source text: they are rendered as text nodes
 * inside a blockquote, never as markup, and the raw GCS payload behind them is
 * never fetched. Observation IDs and the configured source URL are the whole
 * chain the dashboard needs.
 */
export function ProvenanceDrawer({
  open,
  loading,
  error,
  data,
  requested,
  onClose,
  onOpenClaim,
}: Props) {
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => {
      restoreRef.current?.focus?.();
    };
  }, [open]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = drawerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (!nodes || nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  if (!open) return null;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} data-testid="drawer-backdrop" />
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provenance-heading"
        ref={drawerRef}
        onKeyDown={onKeyDown}
      >
        <header>
          <div>
            <h2 id="provenance-heading">Provenance</h2>
            <p className="panel-note" style={{ margin: "4px 0 0" }}>
              <span className="id">
                {requested?.claimId ?? "…"} · v{requested?.version ?? "?"}
              </span>
            </p>
          </div>
          <button type="button" className="ghost" onClick={onClose} ref={closeRef}>
            Close
          </button>
        </header>

        <div className="body">
          {loading ? <p className="loading">Reading the claim and its canonical Deltas…</p> : null}
          {error ? <p className="error">{error}</p> : null}

          {data ? (
            <>
              <section>
                <h3>Current claim</h3>
                <p>{data.claim.statement}</p>
                <dl className="kv">
                  <dt>Claim</dt>
                  <dd className="id">
                    {data.claim.claim_id} · v{data.current_version}
                  </dd>
                  <dt>Requested version</dt>
                  <dd>
                    v{data.requested_version} {data.exact_version ? "(exact)" : "(reconstructed)"}
                  </dd>
                  <dt>Entity / scope</dt>
                  <dd>
                    {data.claim.entity} · {scopeLabel(data.claim.scope)}
                  </dd>
                  <dt>Class</dt>
                  <dd>{data.claim.claim_class}</dd>
                  <dt>Confidence</dt>
                  <dd>
                    {data.claim.confidence} · severity {data.claim.severity}
                  </dd>
                  <dt>Status</dt>
                  <dd>
                    {data.claim.status}
                    {data.claim.stale ? " · stale" : ""}
                  </dd>
                  <dt>Created</dt>
                  <dd>
                    {formatTimestamp(data.created_at)} by {data.created_by}
                  </dd>
                  <dt>Last verified</dt>
                  <dd>
                    {formatTimestamp(data.last_verified_at)} (stale after{" "}
                    {data.staleness_days} days)
                  </dd>
                </dl>
                <p className="panel-note" style={{ marginTop: 8 }}>
                  {data.rationale}
                </p>
                {data.reconstruction_note ? (
                  <p className="empty">{data.reconstruction_note}</p>
                ) : null}
              </section>

              <section>
                <h3>Lifecycle</h3>
                {data.lifecycle.supersedes ||
                data.lifecycle.superseded_by ||
                data.lifecycle.disputes ||
                data.lifecycle.disputed_by.length > 0 ? (
                  <dl className="kv">
                    {data.lifecycle.supersedes ? (
                      <>
                        <dt>Supersedes</dt>
                        <dd>
                          <button
                            type="button"
                            className="claim-link"
                            onClick={() => onOpenClaim(data.lifecycle.supersedes!, 1)}
                          >
                            {data.lifecycle.supersedes}
                          </button>
                        </dd>
                      </>
                    ) : null}
                    {data.lifecycle.superseded_by ? (
                      <>
                        <dt>Superseded by</dt>
                        <dd>
                          <button
                            type="button"
                            className="claim-link"
                            onClick={() => onOpenClaim(data.lifecycle.superseded_by!, 1)}
                          >
                            {data.lifecycle.superseded_by}
                          </button>
                        </dd>
                      </>
                    ) : null}
                    {data.lifecycle.disputes ? (
                      <>
                        <dt>Disputes</dt>
                        <dd className="id">{data.lifecycle.disputes}</dd>
                      </>
                    ) : null}
                    {data.lifecycle.disputed_by.length > 0 ? (
                      <>
                        <dt>Disputed by</dt>
                        <dd>
                          {data.lifecycle.disputed_by.map((id) => (
                            <button
                              key={id}
                              type="button"
                              className="claim-link"
                              onClick={() => onOpenClaim(id, 1)}
                            >
                              {id}
                            </button>
                          ))}
                        </dd>
                      </>
                    ) : null}
                  </dl>
                ) : (
                  <p className="panel-note">
                    No supersession or dispute links: this claim has stood since it
                    was created.
                  </p>
                )}
                {data.history.length > 0 ? (
                  <ul className="reasons">
                    {data.history.map((entry, index) => (
                      <li key={index}>
                        {formatTimestamp(entry.at)} — {entry.action ?? entry.event ?? "event"}
                        {entry.actor ? ` by ${entry.actor}` : ""}
                        {entry.reason ? `: ${entry.reason}` : ""}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>

              <section>
                <h3>Evidence ({data.evidence.length} canonical Deltas)</h3>
                {data.evidence.length === 0 ? (
                  <p className="empty">This claim cites no evidence.</p>
                ) : null}
                {data.evidence.map((delta) => (
                  <div className="evidence-block" key={delta.delta_id}>
                    <dl className="kv">
                      <dt>Delta</dt>
                      <dd className="id">{delta.delta_id}</dd>
                      <dt>Source</dt>
                      <dd>
                        {sourceLabel(delta.source)} · family {delta.source_family}
                      </dd>
                      <dt>Observed</dt>
                      <dd>{formatTimestamp(delta.computed_at)}</dd>
                      <dt>Triage</dt>
                      <dd>
                        {delta.triage}
                        {delta.admissible ? "" : " · not admissible"}
                      </dd>
                      <dt>Generated by</dt>
                      <dd>
                        {delta.generated_by} · {delta.prompt_version}
                      </dd>
                      {delta.source_ref ? (
                        <>
                          <dt>{delta.source_ref.kind}</dt>
                          <dd className="id">{delta.source_ref.target}</dd>
                        </>
                      ) : null}
                    </dl>

                    {delta.defect ? <p className="error">{delta.defect}</p> : null}

                    {delta.changes.map((change, index) => (
                      <div key={index} style={{ marginTop: 10 }}>
                        <p style={{ margin: 0 }}>
                          <strong>{change.category ?? "change"}</strong> ·{" "}
                          {scopeLabel(change.scope)}
                        </p>
                        <p style={{ margin: "2px 0 0" }}>{change.statement}</p>
                        {change.quote_before ? (
                          <blockquote className="quote">
                            before: {change.quote_before}
                          </blockquote>
                        ) : null}
                        {change.quote_after ? (
                          <blockquote className="quote">{change.quote_after}</blockquote>
                        ) : null}
                      </div>
                    ))}

                    <p className="panel-note" style={{ marginTop: 10, marginBottom: 0 }}>
                      Observations:{" "}
                      {delta.observations.map((observation) => (
                        <span key={observation.obs_id} className="id">
                          {observation.role} {observation.obs_id}
                          {observation.fetched_at
                            ? ` (${formatTimestamp(observation.fetched_at)})`
                            : " (not resolved)"}{" "}
                        </span>
                      ))}
                    </p>
                  </div>
                ))}
              </section>
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}
