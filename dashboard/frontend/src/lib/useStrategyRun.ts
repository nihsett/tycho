/**
 * The Run Strategy Session hook.
 *
 * It POSTs the fixed trigger, opens the SSE stream, and keeps a de-duplicated,
 * ordered event list. Events arrive with a monotonic `seq`; a reconnect replays
 * from the last sequence number, so a dropped connection cannot lose or
 * duplicate a step.
 */
import { useCallback, useRef, useState } from "react";
import { api } from "./api";
import type { ActivityEvent, RunState, StreamClosed } from "./types";
import { isActivityKind } from "./types";

export interface StrategyRunStatus {
  runId: string | null;
  sessionId: string | null;
  state: RunState | null;
  duplicate: boolean;
  message: string | null;
  busy: boolean;
  events: ActivityEvent[];
  error: string | null;
}

const INITIAL: StrategyRunStatus = {
  runId: null,
  sessionId: null,
  state: null,
  duplicate: false,
  message: null,
  busy: false,
  events: [],
  error: null,
};

export interface StrategyRunHandle extends StrategyRunStatus {
  run: () => Promise<void>;
  ingest: (event: ActivityEvent) => void;
}

/** Merge one event into an ordered list, ignoring a replayed duplicate. */
export function mergeEvent(events: ActivityEvent[], incoming: ActivityEvent): ActivityEvent[] {
  if (incoming.event === "heartbeat") {
    const withoutHeartbeat = events.filter((event) => event.event !== "heartbeat");
    return [...withoutHeartbeat, incoming];
  }
  if (events.some((event) => event.event !== "heartbeat" && event.seq === incoming.seq)) {
    return events;
  }
  const merged = [...events.filter((event) => event.event !== "heartbeat"), incoming];
  merged.sort((left, right) => left.seq - right.seq);
  const heartbeat = events.find((event) => event.event === "heartbeat");
  return heartbeat && incoming.event !== "brief_completed" && incoming.event !== "run_failed"
    ? [...merged, heartbeat]
    : merged;
}

export function parseEvent(raw: string): ActivityEvent | null {
  try {
    const value = JSON.parse(raw) as ActivityEvent;
    if (typeof value.seq !== "number" || !isActivityKind(value.event)) return null;
    return value;
  } catch {
    return null;
  }
}

export function useStrategyRun(onComplete: (sessionId: string | null) => void): StrategyRunHandle {
  const [status, setStatus] = useState<StrategyRunStatus>(INITIAL);
  const sourceRef = useRef<EventSource | null>(null);

  const ingest = useCallback((event: ActivityEvent) => {
    setStatus((current) => ({ ...current, events: mergeEvent(current.events, event) }));
  }, []);

  const run = useCallback(async () => {
    setStatus({ ...INITIAL, busy: true, message: "Sending the fixed trigger…" });
    let trigger;
    try {
      trigger = await api.runStrategySession();
    } catch (error) {
      setStatus({
        ...INITIAL,
        state: "failed",
        error: error instanceof Error ? error.message : "The trigger was refused.",
        message: "The trigger was refused.",
      });
      return;
    }

    setStatus((current) => ({
      ...current,
      runId: trigger.run_id,
      sessionId: trigger.session_id,
      state: trigger.state,
      duplicate: trigger.duplicate,
      message: trigger.detail,
      busy: true,
    }));

    sourceRef.current?.close();
    const source = new EventSource(trigger.stream_path);
    sourceRef.current = source;

    const finish = (state: RunState, message: string, sessionId: string | null) => {
      source.close();
      sourceRef.current = null;
      setStatus((current) => ({
        ...current,
        busy: false,
        state,
        message,
        sessionId: sessionId ?? current.sessionId,
      }));
      onComplete(sessionId);
    };

    const onEvent = (message: MessageEvent<string>) => {
      const event = parseEvent(message.data);
      if (!event) return;
      setStatus((current) => ({
        ...current,
        events: mergeEvent(current.events, event),
        sessionId: event.session_id ?? current.sessionId,
      }));
      if (event.event === "brief_completed") {
        finish("completed", "Strategy session finished.", event.session_id);
      }
      if (event.event === "run_failed") {
        finish(
          "failed",
          `The session did not complete (${event.failure_class ?? "failure"}). The period stays retryable.`,
          event.session_id,
        );
      }
    };

    source.onmessage = onEvent;
    source.addEventListener("stream_closed", (raw) => {
      const message = raw as MessageEvent<string>;
      let closed: StreamClosed | null = null;
      try {
        closed = JSON.parse(message.data) as StreamClosed;
      } catch {
        closed = null;
      }
      finish(
        closed?.state === "completed" ? "completed" : (closed?.state ?? "failed"),
        closed?.duplicate
          ? "This period already had a session; the lease returned it without a model call."
          : "Strategy session finished.",
        closed?.session_id ?? null,
      );
    });
    for (const kind of [
      "run_started",
      "agent_started",
      "agent_completed",
      "card_rejected",
      "brief_completed",
      "run_failed",
      "heartbeat",
    ]) {
      source.addEventListener(kind, (raw) => onEvent(raw as MessageEvent<string>));
    }
    source.onerror = () => {
      // EventSource reconnects on its own with Last-Event-ID; only report a
      // hard failure once the browser has given up.
      if (source.readyState === EventSource.CLOSED) {
        setStatus((current) => ({
          ...current,
          busy: false,
          error: "The event stream closed unexpectedly.",
        }));
      }
    };
  }, [onComplete]);

  return { ...status, run, ingest };
}
