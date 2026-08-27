import type { RunState } from "../lib/types";

interface Props {
  onRun: () => void;
  state: RunState | null;
  busy: boolean;
  message: string | null;
  tone: "neutral" | "failed";
}

export function RunStrategyButton({ onRun, state, busy, message, tone }: Props) {
  return (
    <div className="run-strategy">
      <button type="button" className="primary" onClick={onRun} disabled={busy}>
        {busy ? "Refreshing strategy brief…" : "Refresh strategy brief"}
      </button>
      <p className="run-note" data-tone={tone} role="status">
        {message ??
          "Rechecks existing governed evidence for the current strategy period. It does not collect new sources or change the evidence rules."}
      </p>
      {state ? <span className="sr-only">Refresh state: {state}</span> : null}
    </div>
  );
}
