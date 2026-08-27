import type { ActivityEvent } from "../lib/types";
import { formatTimestamp } from "../lib/format";

interface Props {
  events: ActivityEvent[];
  loading: boolean;
  error: string | null;
  live: boolean;
  note: string;
}

const AGENT_LABEL: Record<string, string> = {
  strategy_context: "Context builder",
  tycho_strategist: "Strategist",
  tycho_challenger: "Challenger",
  tycho_brief_writer: "Brief Writer",
  tycho_strategy_council: "Strategy Council",
};

function describe(event: ActivityEvent): string {
  switch (event.event) {
    case "run_started":
      return "refresh started";
    case "agent_started":
      return "working";
    case "agent_completed":
      if (event.agent === "strategy_context") {
        return "evidence context assembled";
      }
      if (event.agent === "tycho_strategist") {
        return "candidate conclusions drafted";
      }
      if (event.agent === "tycho_challenger") {
        return event.state === "rejected" ? "challenge recorded" : "challenge completed";
      }
      return "brief finalized";
    case "card_rejected":
      return "candidate rejected by evidence rules";
    case "brief_completed":
      return "brief finalized";
    case "run_failed":
      return "refresh failed";
    case "heartbeat":
      return "waiting for the council";
    default:
      return "";
  }
}

export function AgentActivityTimeline({ events, loading, error, live, note }: Props) {
  const visible = events.filter(
    (event, index) => event.event !== "heartbeat" || index === events.length - 1,
  );
  return (
    <section className="panel" aria-labelledby="activity-heading">
      <h2 id="activity-heading">Agent activity</h2>
      <p className="panel-note">{note}</p>

      {error ? (
        <p className="error" role="status">
          Agent activity is unavailable: {error}
        </p>
      ) : null}

      {!error && loading && visible.length === 0 ? (
        <p className="loading" role="status">
          Loading agent activity…
        </p>
      ) : null}

      {!error && !loading && visible.length === 0 ? (
        <p className="empty">
          No agent run to show yet. Run Strategy Session starts the fixed
          workflow and its progress appears here.
        </p>
      ) : null}

      {visible.length > 0 ? (
        <ol className="activity" aria-live={live ? "polite" : "off"}>
          {visible.map((event) => (
            <li key={`${event.run_id ?? event.session_id ?? "s"}-${event.seq}`} data-event={event.event}>
              <span className="agent">
                {event.agent ? (AGENT_LABEL[event.agent] ?? event.agent) : "Council"}
              </span>
              <span className="state">{event.event.replace(/_/g, " ")}</span>
              <span>{describe(event)}</span>
              <span className="counts">{formatTimestamp(event.at)}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
