import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CompetitorGrid } from "./CompetitorGrid";
import { BeliefTimeline } from "./BeliefTimeline";
import { StrategyBrief } from "./StrategyBrief";
import { AgentActivityTimeline } from "./AgentActivityTimeline";
import { FleetHealthBar } from "./FleetHealthBar";
import { Markdown } from "./Markdown";
import { StrategyVerdict } from "./StrategyVerdict";
import {
  CLAIM_A,
  CLAIM_OLD,
  activityEvent,
  emptyTimeline,
  entityCard,
  health,
  overview,
  strategySession,
  timeline,
} from "../test/fixtures";

const noop = () => {};

describe("competitor overview", () => {
  it("renders one card per entity with its latest change and counts", () => {
    render(
      <CompetitorGrid
        overview={overview([entityCard(), entityCard({ entity: "codex", name: "OpenAI Codex" })])}
        error={null}
        selected="claude_code"
        onSelect={noop}
        onOpenClaim={noop}
      />,
    );
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /View evidence for/ })).toHaveLength(2);
    expect(
      screen.getAllByText(/Claude Code enables sandboxed shell execution by default/)[0],
    ).toBeInTheDocument();
    expect(screen.getAllByText("4")[0]).toBeInTheDocument();
  });

  it("marks the selected card without turning the whole card into a button", () => {
    render(
      <CompetitorGrid
        overview={overview()}
        error={null}
        selected="claude_code"
        onSelect={noop}
        onOpenClaim={noop}
      />,
    );
    expect(screen.getAllByRole("article")[0]).toHaveAttribute("data-selected", "true");
    expect(screen.getAllByRole("article")[0].querySelector("button")?.getAttribute("aria-label")).toMatch(
      /View evidence for Claude Code/,
    );
  });

  it("explains what an entity with no claims is waiting for", () => {
    render(
      <CompetitorGrid
        overview={overview([
          entityCard({
            entity: "pi",
            name: "Pi",
            latest_change: null,
            notable_claim: null,
            active_claim_count: 0,
            active_fact_count: 0,
            waiting_for: "Watching, but nothing has cleared the evidence bar yet.",
          }),
        ])}
        error={null}
        selected={null}
        onSelect={noop}
        onOpenClaim={noop}
      />,
    );
    expect(screen.getByText(/No meaningful change has cleared the evidence bar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /View evidence for Pi when a claim is available/ })).toBeDisabled();
  });

  it("shows a bounded error state rather than an empty page", () => {
    render(
      <CompetitorGrid
        overview={null}
        error="That resource does not exist."
        selected={null}
        onSelect={noop}
        onOpenClaim={noop}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/could not be loaded/);
  });

  it("opens the drawer from the notable claim", async () => {
    const onOpenClaim = vi.fn();
    render(
      <CompetitorGrid
        overview={overview()}
        error={null}
        selected={null}
        onSelect={noop}
        onOpenClaim={onOpenClaim}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /View evidence for Claude Code/ }));
    expect(onOpenClaim).toHaveBeenCalledWith(CLAIM_A, 1);
  });
});

describe("belief timeline", () => {
  const base = {
    loading: false,
    error: null,
    entities: ["claude_code", "codex"],
    scopes: ["pricing", "product/capabilities"],
    entity: "codex",
    scope: null,
    onEntityChange: noop,
    onScopeChange: noop,
    showArchive: false,
    onArchiveChange: noop,
    onOpenClaim: noop,
    onLoadMore: noop,
  };

  it("shows the old and the replacement statement together", () => {
    render(<BeliefTimeline {...base} timeline={timeline()} />);
    const superseded = screen.getByText(/Codex team plan is \$30 per seat/).closest("li");
    expect(superseded).not.toBeNull();
    expect(within(superseded as HTMLElement).getByText(/replaced by/i)).toBeInTheDocument();
    expect(
      within(superseded as HTMLElement).getByText(/\$45 per seat per month/),
    ).toBeInTheDocument();
  });

  it("keeps claim IDs and versions out of the default history view", () => {
    render(<BeliefTimeline {...base} timeline={timeline()} />);
    expect(screen.queryByText(new RegExp(CLAIM_OLD))).toBeNull();
    expect(screen.getByText(/Claim IDs, versions, ontology scopes, and Delta IDs/)).toBeInTheDocument();
  });

  it("colours each lifecycle state, not each agent", () => {
    const { container } = render(<BeliefTimeline {...base} timeline={timeline()} />);
    const kinds = Array.from(container.querySelectorAll("li[data-kind]")).map((node) =>
      node.getAttribute("data-kind"),
    );
    expect(new Set(kinds)).toEqual(new Set(["created", "superseded"]));
  });

  it("reveals retired migration records only through the archive filter", async () => {
    const { rerender } = render(<BeliefTimeline {...base} timeline={timeline()} />);
    expect(screen.queryByText(/interactive agents dashboard/)).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("View"), "archive");
    rerender(<BeliefTimeline {...base} timeline={timeline()} showArchive />);
    expect(screen.getByText(/interactive agents dashboard/)).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Include retired history" })).toBeInTheDocument();
  });

  it("explains an empty timeline instead of showing a blank list", () => {
    render(<BeliefTimeline {...base} entity="pi" timeline={emptyTimeline()} />);
    expect(screen.getByText(/waiting for the next meaningful canonical change/)).toBeInTheDocument();
  });

  it("offers entity and scope filters only", () => {
    render(<BeliefTimeline {...base} timeline={timeline()} />);
    expect(screen.getByLabelText("Competitor")).toBeInTheDocument();
    expect(screen.getByLabelText("Ontology scope")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("opens the provenance drawer from an evidence chip", async () => {
    const onOpenClaim = vi.fn();
    render(<BeliefTimeline {...base} timeline={timeline()} onOpenClaim={onOpenClaim} />);
    await userEvent.click(screen.getByRole("button", { name: /View evidence for website changelog/ }));
    expect(onOpenClaim).toHaveBeenCalledWith(CLAIM_A, 1);
  });
});

describe("strategy brief", () => {
  it("renders passed cards with confidence as a word", () => {
    render(
      <StrategyBrief data={strategySession()} loading={false} error={null} onOpenClaim={noop} />,
    );
    expect(screen.getByText(/Workspace execution isolation is a standard control/)).toBeInTheDocument();
    expect(screen.getByText("Confidence: likely")).toBeInTheDocument();
    expect(screen.getByText("August 17–23, 2026")).toBeInTheDocument();
    expect(screen.getByText("ISO week 34, 2026")).toBeInTheDocument();
    expect(screen.queryByText("2026-W35")).toBeNull();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("keeps rejected cards behind a collapsed section with their reasons", async () => {
    render(
      <StrategyBrief data={strategySession()} loading={false} error={null} onOpenClaim={noop} />,
    );
    const summary = screen.getByText("Why Tycho rejected one possible conclusion.");
    await userEvent.click(summary);
    expect(screen.getByText(/conclusion asserts unsupported causation/)).toBeInTheDocument();
  });

  it("shows an honest empty brief when nothing survived", () => {
    const data = strategySession();
    data.passed_cards = [];
    data.waiting_for = "No conclusion survived validation for this period.";
    if (data.brief) data.brief.empty = true;
    render(<StrategyBrief data={data} loading={false} error={null} onOpenClaim={noop} />);
    expect(screen.getByText(/leaves the brief empty instead of manufacturing a pattern/)).toBeInTheDocument();
  });

  it("explains the empty state when no session exists", () => {
    render(
      <StrategyBrief
        data={{
          session: null,
          brief: null,
          passed_cards: [],
          rejected_cards: [],
          waiting_for: "No strategy session has completed yet.",
        }}
        loading={false}
        error={null}
        onOpenClaim={noop}
      />,
    );
    expect(screen.getByText("No strategy session has completed yet.")).toBeInTheDocument();
  });

  it("opens a premise chip as provenance", async () => {
    const onOpenClaim = vi.fn();
    render(
      <StrategyBrief
        data={strategySession()}
        loading={false}
        error={null}
        onOpenClaim={onOpenClaim}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /View evidence for Claude Code belief/ }));
    expect(onOpenClaim).toHaveBeenCalledWith(CLAIM_A, 1);
  });
});

describe("strategy verdict", () => {
  it("states plainly when no market-wide pattern survives verification", () => {
    const data = strategySession();
    data.passed_cards = [];
    render(<StrategyVerdict data={data} loading={false} error={null} />);
    expect(
      screen.getByText("No market-wide pattern passed verification for August 17–23."),
    ).toBeInTheDocument();
    expect(screen.getByText("Tycho rejected weak conclusions rather than manufacturing one.")).toBeInTheDocument();
  });
});

describe("agent activity", () => {
  it("names the workflow agents without crowding status and time", () => {
    render(
      <AgentActivityTimeline
        events={[
          activityEvent("run_started", 0),
          activityEvent("agent_completed", 1, { agent: "tycho_strategist", card_count: 2 }),
          activityEvent("agent_completed", 2, { agent: "tycho_challenger", state: "rejected" }),
          activityEvent("card_rejected", 3, {
            reason_classes: ["entity_diversity", "conclusion_language"],
            reason_count: 2,
          }),
          activityEvent("brief_completed", 4, { passed_count: 0, rejected_count: 2 }),
        ]}
        loading={false}
        error={null}
        live
        note="Live structural events."
      />,
    );
    expect(screen.getByText("Strategist")).toBeInTheDocument();
    expect(screen.getByText("candidate conclusions drafted")).toBeInTheDocument();
    expect(screen.getByText("candidate rejected by evidence rules")).toBeInTheDocument();
    expect(screen.getByText("brief finalized")).toBeInTheDocument();
  });

  it("never renders a prompt, response, or quote field", () => {
    const { container } = render(
      <AgentActivityTimeline
        events={[activityEvent("agent_completed", 1, { agent: "tycho_strategist", card_count: 1 })]}
        loading={false}
        error={null}
        live={false}
        note="Reconstructed from the stored session record."
      />,
    );
    const text = container.textContent ?? "";
    for (const forbidden of ["prompt", "response", "quote", "rendered_md", "statement"]) {
      expect(text.toLowerCase()).not.toContain(forbidden);
    }
  });

  it("explains the empty state before any run", () => {
    render(
      <AgentActivityTimeline events={[]} loading={false} error={null} live={false} note="n" />,
    );
    expect(screen.getByText(/No agent run to show yet/)).toBeInTheDocument();
  });

  it("collapses repeated heartbeats to the most recent one", () => {
    render(
      <AgentActivityTimeline
        events={[
          activityEvent("run_started", 0),
          activityEvent("heartbeat", 1),
          activityEvent("heartbeat", 2),
        ]}
        loading={false}
        error={null}
        live
        note="n"
      />,
    );
    expect(screen.getAllByText("waiting for the council")).toHaveLength(1);
  });
});

describe("fleet health", () => {
  it("shows one pill per component with its state", () => {
    render(<FleetHealthBar health={health()} error={null} />);
    const list = screen.getByLabelText("Fleet health");
    expect(within(list).getByText("Acquisition watchers")).toBeInTheDocument();
    expect(within(list).getByText("Strategy Council")).toBeInTheDocument();
  });
});

describe("markdown sanitisation", () => {
  it("never renders raw HTML from brief prose", () => {
    const { container } = render(
      <Markdown
        source={'## Heading\n\n<img src=x onerror="alert(1)"> <script>alert(2)</script> text'}
        onOpenClaim={noop}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    // The markup survives only as literal text: no element, no attribute.
    const tags = Array.from(container.querySelectorAll("*")).map((node) => node.tagName);
    expect(new Set(tags)).toEqual(new Set(["DIV", "H2", "P", "SPAN"]));
    for (const node of Array.from(container.querySelectorAll("h2, p, span"))) {
      expect(node.getAttributeNames()).toEqual([]);
    }
    expect(container.textContent).toContain('img src=x onerror="alert(1)"');
  });

  it("renders a pinned claim citation as a provenance button", async () => {
    const onOpenClaim = vi.fn();
    render(
      <Markdown
        source={`Two vendors shipped [${CLAIM_A}@v1](/claims/${CLAIM_A}?version=1).`}
        onOpenClaim={onOpenClaim}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "View evidence" }));
    expect(onOpenClaim).toHaveBeenCalledWith(CLAIM_A, 1);
  });

  it("flattens an external link instead of rendering an anchor", () => {
    const { container } = render(
      <Markdown source="See [the vendor blog](https://example.com/post)." onOpenClaim={noop} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("the vendor blog");
    expect(container.textContent).not.toContain("https://example.com");
  });
});
