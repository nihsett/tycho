import type { OverviewResponse, StrategySessionResponse } from "../lib/types";
import { isoWeek, periodLabel } from "../lib/format";

interface Props {
  overview: OverviewResponse | null;
  strategy: StrategySessionResponse | null;
  loading: boolean;
  error: string | null;
}

function periodText(strategy: StrategySessionResponse | null): string {
  const period = strategy?.brief ?? strategy?.session;
  if (!period) return "No completed brief yet";
  return `Latest strategy brief: ${periodLabel(period.period_from, period.period_to)} · ISO week ${isoWeek(period.period_to).week}.`;
}

export function WeeklyOverview({ overview, strategy, loading, error }: Props) {
  return (
    <section className="weekly-overview" aria-labelledby="weekly-overview-heading">
      <div className="weekly-overview-copy">
        <p className="eyebrow">Weekly intelligence</p>
        <h2 id="weekly-overview-heading">Current coding-agent intelligence.</h2>
        <p>
          A concise read on the changes Tycho is watching, what cleared its evidence
          bar, and what still needs proof.
        </p>
        <p className="period-stamp">{periodText(strategy)}</p>
      </div>

      {error ? (
        <p className="error" role="status">
          The weekly facts could not be loaded: {error}
        </p>
      ) : loading || !overview ? (
        <p className="loading" role="status">
          Reading this week&apos;s governed facts…
        </p>
      ) : (
        <dl className="weekly-facts">
          <div className="weekly-fact">
            <dd>{overview.entities.length}</dd>
            <dt>competitors watched</dt>
          </div>
          <div className="weekly-fact">
            <dd>{overview.entities.filter((card) => card.latest_change !== null).length}</dd>
            <dt>competitors with recorded changes</dt>
          </div>
          <div className="weekly-fact">
            <dd>
              {overview.entities.reduce((total, card) => total + card.active_fact_count, 0)}
            </dd>
            <dt>active verified facts</dt>
          </div>
          <div className="weekly-fact">
            <dd>{strategy?.brief?.stats_new ?? "—"}</dd>
            <dt>claims added during the latest brief period</dt>
          </div>
        </dl>
      )}
    </section>
  );
}
