import type { StrategySessionResponse } from "../lib/types";
import { countWord, periodLabel } from "../lib/format";

interface Props {
  data: StrategySessionResponse | null;
  loading: boolean;
  error: string | null;
}

export function StrategyVerdict({ data, loading, error }: Props) {
  const passed = data?.passed_cards.length ?? 0;
  const rejected = data?.rejected_cards.length ?? 0;
  const hasSession = data?.session !== null && data?.session !== undefined;

  let title = "Strategy verdict is loading.";
  let explanation = "Tycho is checking the latest governed strategy record.";
  if (error) {
    title = "Strategy verdict is unavailable.";
    explanation = "The governed strategy record could not be read right now.";
  } else if (!loading && !hasSession) {
    title = "No strategy conclusion yet.";
    explanation = data?.waiting_for ?? "Tycho is waiting for its first completed strategy brief.";
  } else if (!loading && passed === 0) {
    const period = data?.brief ?? data?.session;
    const label = period
      ? periodLabel(period.period_from, period.period_to)
      : "the latest period";
    title = `No market-wide pattern passed verification for ${label}.`;
    explanation = "Tycho rejected weak conclusions rather than manufacturing one.";
  } else if (!loading) {
    const count = countWord(passed);
    const displayCount = count.charAt(0).toUpperCase() + count.slice(1);
    title = `${displayCount} market-wide pattern${passed === 1 ? "" : "s"} passed verification this week.`;
    explanation = `Tycho published ${passed} conclusion${passed === 1 ? "" : "s"} after challenging ${rejected} proposed conclusion${rejected === 1 ? "" : "s"}.`;
  }

  return (
    <section className="strategy-verdict" aria-labelledby="strategy-verdict-heading">
      <div className="verdict-mark" aria-hidden="true">
        ✓
      </div>
      <div>
        <p className="eyebrow">Strategy verdict</p>
        <h2 id="strategy-verdict-heading">{title}</h2>
        <p>{explanation}</p>
      </div>
    </section>
  );
}
