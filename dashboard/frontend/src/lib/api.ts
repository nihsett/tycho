/**
 * The single typed API client.
 *
 * The browser holds no credential: every request is same-origin to the
 * dashboard service, which authenticates to Google Cloud with its own Cloud Run
 * service account. There is no endpoint here that accepts free-form text.
 */
import type {
  ActivityResponse,
  HealthResponse,
  MetaResponse,
  OverviewResponse,
  ProvenanceResponse,
  StrategySessionResponse,
  TimelineResponse,
  TriggerResponse,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // A non-JSON error body is never rendered; the status is enough.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  meta: (): Promise<MetaResponse> => request<MetaResponse>("/api/meta"),
  health: (): Promise<HealthResponse> => request<HealthResponse>("/api/health"),
  overview: (): Promise<OverviewResponse> => request<OverviewResponse>("/api/overview"),

  timeline: (
    entity: string,
    options: { scope?: string | null; limit?: number; offset?: number } = {},
  ): Promise<TimelineResponse> => {
    const params = new URLSearchParams();
    if (options.scope) params.set("scope", options.scope);
    params.set("limit", String(options.limit ?? 50));
    params.set("offset", String(options.offset ?? 0));
    return request<TimelineResponse>(
      `/api/entities/${encodeURIComponent(entity)}/timeline?${params.toString()}`,
    );
  },

  provenance: (claimId: string, version: number): Promise<ProvenanceResponse> =>
    request<ProvenanceResponse>(
      `/api/claims/${encodeURIComponent(claimId)}/versions/${version}/provenance`,
    ),

  latestSession: (): Promise<StrategySessionResponse> =>
    request<StrategySessionResponse>("/api/strategy/sessions/latest"),

  session: (sessionId: string): Promise<StrategySessionResponse> =>
    request<StrategySessionResponse>(
      `/api/strategy/sessions/${encodeURIComponent(sessionId)}`,
    ),

  sessionEvents: (sessionId: string): Promise<ActivityResponse> =>
    request<ActivityResponse>(
      `/api/strategy/sessions/${encodeURIComponent(sessionId)}/events`,
    ),

  /** Start the fixed bounded strategy workflow. There is no request body. */
  runStrategySession: (): Promise<TriggerResponse> =>
    request<TriggerResponse>("/api/strategy/sessions", { method: "POST" }),
};

export type Api = typeof api;
