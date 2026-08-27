import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "./App";
import {
  activityEvent,
  entityCard,
  health,
  overview,
  provenance,
  strategySession,
  timeline,
} from "./test/fixtures";

const META = {
  entities: ["claude_code", "codex", "gemini_cli", "pi"],
  scopes: ["pricing", "product/capabilities"],
  service: "tycho-dashboard",
  revision: "tycho-dashboard-00001-abc",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function route(url: string): Response {
  if (url.startsWith("/api/meta")) return json(META);
  if (url.startsWith("/api/health")) return json(health());
  if (url.startsWith("/api/overview")) {
    return json(
      overview([
        entityCard(),
        entityCard({ entity: "codex", name: "OpenAI Codex" }),
        entityCard({
          entity: "gemini_cli",
          name: "Gemini CLI",
          latest_change: null,
          notable_claim: null,
          active_claim_count: 0,
          active_fact_count: 0,
          waiting_for: "Watching, but nothing has cleared the evidence bar yet.",
        }),
        entityCard({ entity: "pi", name: "Pi" }),
      ]),
    );
  }
  if (url.includes("/timeline")) return json(timeline());
  if (url.includes("/provenance")) return json(provenance());
  if (url.endsWith("/events")) {
    return json({
      session_id: "sts_01M0ZGG793KQK3BTCVPAP3DH2D",
      events: [
        activityEvent("run_started", 0),
        activityEvent("agent_completed", 1, { agent: "tycho_strategist", card_count: 2 }),
      ],
      derived_from: "persisted strategy session record",
    });
  }
  if (url.includes("/strategy/sessions/latest")) return json(strategySession());
  return json({ error: "not_found", detail: "That resource does not exist." }, 404);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => route(String(input))),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("the dashboard page", () => {
  it("leads with the weekly overview, verdict, health, and refresh action", async () => {
    render(<App />);
    expect(screen.getByRole("heading", { level: 1, name: "Tycho" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Current coding-agent intelligence." })).toBeInTheDocument();
    expect(
      screen.getByText("Competitive intelligence that shows why its beliefs changed."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh strategy brief" })).toBeInTheDocument();
    expect(screen.getByText("Sources watched")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Fleet health")).toBeInTheDocument());
  });

  it("shows all four simplified competitor cards, including the quiet one", async () => {
    render(<App />);
    await waitFor(() =>
      expect(document.querySelectorAll(".competitor-card")).toHaveLength(4),
    );
    expect(screen.getByText(/No meaningful change has cleared the evidence bar/)).toBeInTheDocument();
    expect(screen.getAllByText("verified facts")).toHaveLength(4);
  });

  it("renders the brief, activity, and lower evidence history together", async () => {
    render(<App />);
    await waitFor(() =>
      expect(screen.getAllByRole("heading", { name: /What Tycho believes/ }).length).toBeGreaterThan(0),
    );
    expect(
      await screen.findByRole("heading", { name: "How Tycho’s beliefs changed" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent activity" })).toBeInTheDocument();
    expect(await screen.findByText("Strategist")).toBeInTheDocument();
    expect(
      screen.getByText("Latest strategy brief: August 17–23, 2026 · ISO week 34."),
    ).toBeInTheDocument();
    expect(screen.getByText("ISO week 34, 2026")).toBeInTheDocument();
    expect(screen.queryByText("2026-W35")).toBeNull();
  });

  it("opens the provenance drawer from a claim and closes it with Escape", async () => {
    render(<App />);
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: /Codex team plan is \$45 per seat per month/ }),
      ).not.toHaveLength(0),
    );
    await userEvent.click(
      screen.getAllByRole("button", { name: /Codex team plan is \$45 per seat per month/ })[0],
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Provenance/)).toBeInTheDocument();
    await waitFor(() =>
      expect(within(dialog).getByText("https://github.com/anthropics/claude-code")).toBeInTheDocument(),
    );
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("opens the drawer from a brief citation", async () => {
    render(<App />);
    const citation = await screen.findByRole("button", { name: "View evidence" });
    await userEvent.click(citation);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("exposes landmarks and a skip link for keyboard users", async () => {
    render(<App />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    await waitFor(() => expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThan(2));
  });

  it("has no free-form text input anywhere on the page", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("Fleet health")).toBeInTheDocument());
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(document.querySelectorAll("input, textarea")).toHaveLength(0);
  });

  it("uses a responsive grid rather than a fixed-width layout", async () => {
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".competitor-grid")).not.toBeNull());
    expect(container.querySelector(".columns")).not.toBeNull();
    expect(container.querySelector('[style*="width: 1200px"]')).toBeNull();
  });

  it("keeps every failing panel isolated and explained", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/timeline")) {
          return json({ error: "not_found", detail: "That resource does not exist." }, 404);
        }
        return route(url);
      }),
    );
    render(<App />);
    await waitFor(() =>
      expect(screen.getByText(/The belief history could not be loaded/)).toBeInTheDocument(),
    );
    expect(
      await screen.findByRole("heading", { name: "What Tycho believes now" }),
    ).toBeInTheDocument();
  });
});
