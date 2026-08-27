import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mergeEvent, parseEvent, useStrategyRun } from "./useStrategyRun";
import type { ActivityEvent, TriggerResponse } from "./types";
import { activityEvent } from "../test/fixtures";

/** A controllable EventSource stand-in with real reconnect semantics. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  readonly url: string;
  readyState = FakeEventSource.OPEN;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, handler: (event: MessageEvent<string>) => void): void {
    const existing = this.listeners.get(kind) ?? [];
    this.listeners.set(kind, [...existing, handler]);
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(kind: string, payload: unknown): void {
    const event = new MessageEvent(kind, { data: JSON.stringify(payload) });
    for (const handler of this.listeners.get(kind) ?? []) handler(event);
  }

  fail(): void {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
}

const TRIGGER: TriggerResponse = {
  run_id: "run_0123456789abcdef",
  state: "dispatching",
  duplicate: false,
  session_id: null,
  brief_id: null,
  period_from: "2026-08-17T00:00:00Z",
  period_to: "2026-08-24T00:00:00Z",
  stream_path: "/api/strategy/sessions/run_0123456789abcdef/stream",
  detail: "Trigger accepted.",
};

function Harness({ onComplete }: { onComplete: (id: string | null) => void }) {
  const run = useStrategyRun(onComplete);
  return (
    <div>
      <button type="button" onClick={() => void run.run()}>
        run
      </button>
      <p data-testid="state">{run.state ?? "idle"}</p>
      <p data-testid="busy">{String(run.busy)}</p>
      <p data-testid="message">{run.error ?? run.message ?? ""}</p>
      <ol data-testid="events">
        {run.events.map((event) => (
          <li key={`${event.event}-${event.seq}`}>{`${event.seq}:${event.event}`}</li>
        ))}
      </ol>
    </div>
  );
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(TRIGGER), { status: 202 })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function start() {
  const onComplete = vi.fn();
  render(<Harness onComplete={onComplete} />);
  await userEvent.click(screen.getByRole("button", { name: "run" }));
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  return { source: FakeEventSource.instances[0], onComplete };
}

describe("merge semantics", () => {
  it("keeps events ordered by sequence number", () => {
    const merged = [
      activityEvent("agent_completed", 2),
      activityEvent("run_started", 0),
      activityEvent("agent_completed", 1),
    ].reduce<ActivityEvent[]>((events, event) => mergeEvent(events, event), []);
    expect(merged.map((event) => event.seq)).toEqual([0, 1, 2]);
  });

  it("ignores a replayed duplicate", () => {
    const first = mergeEvent([], activityEvent("run_started", 0));
    const again = mergeEvent(first, activityEvent("run_started", 0));
    expect(again).toHaveLength(1);
  });

  it("keeps only the newest heartbeat", () => {
    let events = mergeEvent([], activityEvent("run_started", 0));
    events = mergeEvent(events, activityEvent("heartbeat", 1));
    events = mergeEvent(events, activityEvent("heartbeat", 1, { at: "2026-08-27T10:01:00Z" }));
    expect(events.filter((event) => event.event === "heartbeat")).toHaveLength(1);
  });

  it("drops the heartbeat once the run finishes", () => {
    let events = mergeEvent([], activityEvent("heartbeat", 1));
    events = mergeEvent(events, activityEvent("brief_completed", 2));
    expect(events.some((event) => event.event === "heartbeat")).toBe(false);
  });

  it("refuses an event that is not in the closed enum", () => {
    expect(parseEvent(JSON.stringify({ seq: 0, event: "exfiltrate" }))).toBeNull();
    expect(parseEvent("not json")).toBeNull();
    expect(parseEvent(JSON.stringify(activityEvent("run_started", 0)))).not.toBeNull();
  });
});

describe("the live run", () => {
  it("streams named events in order and completes", async () => {
    const { source, onComplete } = await start();
    act(() => {
      source.emit("run_started", activityEvent("run_started", 0, { derived: false }));
      source.emit(
        "agent_completed",
        activityEvent("agent_completed", 1, { agent: "tycho_strategist", card_count: 2 }),
      );
      source.emit(
        "brief_completed",
        activityEvent("brief_completed", 2, { passed_count: 0, rejected_count: 2 }),
      );
    });
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("completed"));
    expect(
      Array.from(screen.getByTestId("events").querySelectorAll("li")).map(
        (node) => node.textContent,
      ),
    ).toEqual(["0:run_started", "1:agent_completed", "2:brief_completed"]);
    expect(screen.getByTestId("busy")).toHaveTextContent("false");
    expect(source.readyState).toBe(FakeEventSource.CLOSED);
    expect(onComplete).toHaveBeenCalled();
  });

  it("ignores a duplicate event replayed after a reconnect", async () => {
    const { source } = await start();
    act(() => {
      source.emit("run_started", activityEvent("run_started", 0));
      source.emit("agent_completed", activityEvent("agent_completed", 1));
      // The browser reconnects and the server replays from the beginning.
      source.emit("run_started", activityEvent("run_started", 0));
      source.emit("agent_completed", activityEvent("agent_completed", 1));
    });
    await waitFor(() =>
      expect(screen.getByTestId("events").querySelectorAll("li")).toHaveLength(2),
    );
  });

  it("reports a failed run with its failure class and keeps the period retryable", async () => {
    const { source } = await start();
    act(() => {
      source.emit("run_started", activityEvent("run_started", 0));
      source.emit(
        "run_failed",
        activityEvent("run_failed", 1, { failure_class: "strategist:StrategyModelError" }),
      );
    });
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("failed"));
    expect(screen.getByTestId("message")).toHaveTextContent("strategist:StrategyModelError");
    expect(screen.getByTestId("message")).toHaveTextContent("retryable");
  });

  it("reports a duplicate trigger as a lease hit with no model call", async () => {
    const { source } = await start();
    act(() => {
      source.emit("stream_closed", {
        state: "completed",
        session_id: "sts_01M0ZGG793KQK3BTCVPAP3DH2D",
        brief_id: "brf_2026w35-pap3dh2d",
        duplicate: true,
      });
    });
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("completed"));
    expect(screen.getByTestId("message")).toHaveTextContent("without a model call");
  });

  it("stays quiet while the browser is reconnecting and reports a hard failure", async () => {
    const { source } = await start();
    act(() => {
      source.readyState = FakeEventSource.CONNECTING;
      source.onerror?.();
    });
    expect(screen.getByTestId("message")).not.toHaveTextContent("closed unexpectedly");
    act(() => source.fail());
    await waitFor(() =>
      expect(screen.getByTestId("message")).toHaveTextContent("closed unexpectedly"),
    );
  });

  it("surfaces a refused trigger without starting a stream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "cross_origin", detail: "Refused." }), {
            status: 403,
          }),
      ),
    );
    render(<Harness onComplete={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "run" }));
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("failed"));
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByTestId("message")).toHaveTextContent("Refused.");
  });
});
