import type { HealthResponse } from "../lib/types";

const STATE_LABEL: Record<string, string> = {
  ok: "healthy",
  stale: "needs attention",
  idle: "quiet",
  failed: "failed",
  unknown: "not reported",
};

interface Props {
  health: HealthResponse | null;
  error: string | null;
}

export function FleetHealthBar({ health, error }: Props) {
  if (error) {
    return (
      <p className="error" role="status">
        Fleet health is unavailable: {error}
      </p>
    );
  }
  if (!health) {
    return (
      <p className="loading" role="status">
        Reading fleet health…
      </p>
    );
  }
  return (
    <div className="fleet-health-wrap">
      <p className="card-label">Fleet health</p>
      <ul className="fleet-health" aria-label="Fleet health">
        {health.components.map((component) => (
          <li key={component.key} className="health-pill" data-state={component.state}>
            <span className="dot" aria-hidden="true" />
            <span className="label">{component.name}</span>
            <span className="meta">{STATE_LABEL[component.state] ?? component.state}</span>
            <span className="sr-only">{component.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
