import type { StrategySessionResponse } from "../lib/types";
import { bytes, countWord, formatTimestamp, isoWeekLabel, periodLabel } from "../lib/format";
import { Markdown } from "./Markdown";
import { StrategyCard } from "./StrategyCard";

interface Props {
  data: StrategySessionResponse | null;
  loading: boolean;
  error: string | null;
  onOpenClaim: (claimId: string, version: number) => void;
}

function withoutStoredTitle(source: string): string {
  return source.replace(/^# [^\n]*(?:\n|$)/, "").trim();
}

export function StrategyBrief({ data, loading, error, onOpenClaim }: Props) {
  if (error) {
    return (
      <section className="panel strategy-panel" aria-labelledby="strategy-heading">
        <p className="eyebrow">Strategy brief</p>
        <h2 id="strategy-heading">What Tycho believes now</h2>
        <p className="error" role="status">
          The strategy brief could not be loaded: {error}
        </p>
      </section>
    );
  }
  if (loading && !data) {
    return (
      <section className="panel strategy-panel" aria-labelledby="strategy-heading">
        <p className="eyebrow">Strategy brief</p>
        <h2 id="strategy-heading">What Tycho believes now</h2>
        <p className="loading" role="status">
          Loading the latest governed brief…
        </p>
      </section>
    );
  }
  if (!data || !data.session) {
    return (
      <section className="panel strategy-panel" aria-labelledby="strategy-heading">
        <p className="eyebrow">Strategy brief</p>
        <h2 id="strategy-heading">What Tycho believes now</h2>
        <p className="empty">
          {data?.waiting_for ??
            "No strategy session has completed yet. Refresh strategy brief checks the existing governed evidence."}
        </p>
      </section>
    );
  }

  const { session, brief, passed_cards: passed, rejected_cards: rejected } = data;
  const period = brief ?? session;

  return (
    <section className="panel strategy-panel" aria-labelledby="strategy-heading">
      <div className="section-heading-row compact-heading">
        <div>
          <p className="eyebrow">Strategy brief</p>
          <h2 id="strategy-heading">What Tycho believes now</h2>
        </div>
        <div className="brief-period" aria-label="Brief period">
          <strong>{periodLabel(period.period_from, period.period_to)}</strong>
          <span>{isoWeekLabel(period.period_to)}</span>
        </div>
      </div>
      <p className="panel-note">
        The latest governed brief, based on evidence available during this period.
      </p>

      {brief ? (
        <Markdown source={withoutStoredTitle(brief.rendered_md)} onOpenClaim={onOpenClaim} />
      ) : (
        <p className="empty">This session stored no brief.</p>
      )}

      {passed.length > 0 ? (
        <div className="conclusion-group">
          <h3>Conclusions that survived verification</h3>
          {passed.map((card) => (
            <StrategyCard key={card.card_id} card={card} onOpenClaim={onOpenClaim} />
          ))}
        </div>
      ) : (
        <p className="empty conclusion-empty">
          No conclusion survived validation for this period. Tycho leaves the brief
          empty instead of manufacturing a pattern.
        </p>
      )}

      <details className="rejected-cards">
        <summary>
          Why Tycho rejected {countWord(rejected.length)} possible conclusion
          {rejected.length === 1 ? "" : "s"}.
        </summary>
        {rejected.length === 0 ? (
          <p className="empty">Nothing was rejected in this session.</p>
        ) : (
          rejected.map((card) => (
            <StrategyCard key={card.card_id} card={card} onOpenClaim={onOpenClaim} />
          ))
        )}
      </details>

      <details className="technical-details brief-technical">
        <summary>Technical details</summary>
        <dl className="kv compact-kv">
          <dt>Session ID</dt>
          <dd className="id">{session.session_id}</dd>
          <dt>Brief ID</dt>
          <dd className="id">{brief?.brief_id ?? "none"}</dd>
          <dt>Strategy version</dt>
          <dd>{session.strategy_version}</dd>
          <dt>Manifest hash</dt>
          <dd className="id">{session.manifest_hash}</dd>
          <dt>Proposed / passed / rejected</dt>
          <dd>
            {session.metrics.cards_proposed} / {session.metrics.cards_passed} / {session.metrics.cards_rejected}
          </dd>
          <dt>Claim versions pinned</dt>
          <dd>{session.metrics.manifest_entries}</dd>
          <dt>Context size</dt>
          <dd>{bytes(session.metrics.input_bytes)}</dd>
          <dt>Model tokens</dt>
          <dd>
            {session.metrics.input_tokens} input · {session.metrics.output_tokens} output · {session.metrics.total_tokens} total
          </dd>
          <dt>Agent versions</dt>
          <dd>{Object.entries(session.agent_versions).map(([key, value]) => `${key}: ${value}`).join(" · ")}</dd>
          <dt>Created</dt>
          <dd>{formatTimestamp(session.created_at)}</dd>
        </dl>
      </details>
    </section>
  );
}
