import { useCallback, useEffect, useState } from "react";
import { api } from "./lib/api";
import { useStrategyRun } from "./lib/useStrategyRun";
import type {
  ActivityEvent,
  HealthResponse,
  MetaResponse,
  OverviewResponse,
  ProvenanceResponse,
  StrategySessionResponse,
  TimelineResponse,
} from "./lib/types";
import { AgentActivityTimeline } from "./components/AgentActivityTimeline";
import { BeliefTimeline } from "./components/BeliefTimeline";
import { CompetitorGrid } from "./components/CompetitorGrid";
import { FleetHealthBar } from "./components/FleetHealthBar";
import { ProvenanceDrawer } from "./components/ProvenanceDrawer";
import { RunStrategyButton } from "./components/RunStrategyButton";
import { StrategyBrief } from "./components/StrategyBrief";
import { StrategyVerdict } from "./components/StrategyVerdict";
import { WeeklyOverview } from "./components/WeeklyOverview";

const PROMISE = "Competitive intelligence that shows why its beliefs changed.";
const TIMELINE_PAGE = 25;

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Unavailable.";
}

export function App() {
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);

  const [entity, setEntity] = useState<string>("claude_code");
  const [scope, setScope] = useState<string | null>(null);
  const [showArchive, setShowArchive] = useState(false);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(true);
  const [timelineError, setTimelineError] = useState<string | null>(null);

  const [strategy, setStrategy] = useState<StrategySessionResponse | null>(null);
  const [strategyLoading, setStrategyLoading] = useState(true);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const [storedActivity, setStoredActivity] = useState<ActivityEvent[]>([]);
  const [activityError, setActivityError] = useState<string | null>(null);

  const [drawer, setDrawer] = useState<{ claimId: string; version: number } | null>(null);
  const [provenance, setProvenance] = useState<ProvenanceResponse | null>(null);
  const [provenanceLoading, setProvenanceLoading] = useState(false);
  const [provenanceError, setProvenanceError] = useState<string | null>(null);

  const loadStrategy = useCallback(async () => {
    setStrategyLoading(true);
    try {
      const data = await api.latestSession();
      setStrategy(data);
      setStrategyError(null);
      if (data.session) {
        try {
          const activity = await api.sessionEvents(data.session.session_id);
          setStoredActivity(activity.events);
          setActivityError(null);
        } catch (error) {
          setActivityError(message(error));
        }
      }
    } catch (error) {
      setStrategyError(message(error));
    } finally {
      setStrategyLoading(false);
    }
  }, []);

  const strategyRun = useStrategyRun(
    useCallback(() => {
      void loadStrategy();
    }, [loadStrategy]),
  );

  useEffect(() => {
    void (async () => {
      try {
        setMeta(await api.meta());
      } catch {
        setMeta(null);
      }
      try {
        setHealth(await api.health());
      } catch (error) {
        setHealthError(message(error));
      }
      try {
        setOverview(await api.overview());
      } catch (error) {
        setOverviewError(message(error));
      }
      void loadStrategy();
    })();
  }, [loadStrategy]);

  const loadTimeline = useCallback(
    async (key: string, branch: string | null, offset: number) => {
      setTimelineLoading(true);
      try {
        const page = await api.timeline(key, {
          scope: branch,
          limit: TIMELINE_PAGE,
          offset,
        });
        setTimeline((current) =>
          offset > 0 && current
            ? { ...page, events: [...current.events, ...page.events] }
            : page,
        );
        setTimelineError(null);
      } catch (error) {
        setTimelineError(message(error));
      } finally {
        setTimelineLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadTimeline(entity, scope, 0);
  }, [entity, scope, loadTimeline]);

  const openClaim = useCallback(async (claimId: string, version: number) => {
    setDrawer({ claimId, version });
    setProvenance(null);
    setProvenanceError(null);
    setProvenanceLoading(true);
    try {
      setProvenance(await api.provenance(claimId, version));
    } catch (error) {
      setProvenanceError(message(error));
    } finally {
      setProvenanceLoading(false);
    }
  }, []);

  const liveEvents = strategyRun.events;
  const activityEvents = liveEvents.length > 0 ? liveEvents : storedActivity;
  const activityNote =
    liveEvents.length > 0
      ? "Live progress from this refresh. Only safe workflow events are shown."
      : "The latest completed refresh, shown as safe workflow events only.";

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="masthead" role="banner">
        <div className="brand">
          <h1>Tycho</h1>
          <p className="promise">{PROMISE}</p>
        </div>
        <RunStrategyButton
          onRun={() => {
            void strategyRun.run();
          }}
          state={strategyRun.state}
          busy={strategyRun.busy}
          message={strategyRun.error ?? strategyRun.message}
          tone={strategyRun.state === "failed" || strategyRun.error ? "failed" : "neutral"}
        />
      </header>

      <main id="main">
        <WeeklyOverview
          overview={overview}
          strategy={strategy}
          loading={!overview}
          error={overviewError}
        />

        <StrategyVerdict data={strategy} loading={strategyLoading} error={strategyError} />

        <section className="workflow" aria-labelledby="workflow-heading">
          <div className="workflow-heading">
            <p className="eyebrow">How Tycho works</p>
            <h2 id="workflow-heading">From signal to belief</h2>
          </div>
          <ol className="workflow-steps">
            <li>
              <span className="step-number">1</span>
              <strong>Sources watched</strong>
              <span>Public release and changelog signals</span>
            </li>
            <li>
              <span className="step-number">2</span>
              <strong>Changes verified</strong>
              <span>Meaningful evidence is checked</span>
            </li>
            <li>
              <span className="step-number">3</span>
              <strong>Beliefs updated</strong>
              <span>Claims change only when supported</span>
            </li>
            <li>
              <span className="step-number">4</span>
              <strong>Conclusions challenged</strong>
              <span>Weak cross-market patterns are rejected</span>
            </li>
          </ol>
        </section>

        <section className="panel competitors-panel" aria-labelledby="competitors-heading">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Market changes</p>
              <h2 id="competitors-heading">What changed across watched competitors</h2>
              <p className="panel-note">
                The latest meaningful change and verified facts for each watched competitor.
                Select View evidence to inspect the supporting claim.
              </p>
            </div>
            <FleetHealthBar health={health} error={healthError} />
          </div>
          <CompetitorGrid
            overview={overview}
            error={overviewError}
            selected={entity}
            onSelect={(key) => {
              setEntity(key);
              setScope(null);
            }}
            onOpenClaim={openClaim}
          />
        </section>

        <div className="columns">
          <StrategyBrief
            data={strategy}
            loading={strategyLoading}
            error={strategyError}
            onOpenClaim={openClaim}
          />
          <AgentActivityTimeline
            events={activityEvents}
            loading={strategyLoading && storedActivity.length === 0}
            error={activityError}
            live={liveEvents.length > 0}
            note={activityNote}
          />
        </div>

        <BeliefTimeline
          timeline={timeline}
          loading={timelineLoading}
          error={timelineError}
          entities={meta?.entities ?? []}
          scopes={meta?.scopes ?? []}
          entity={entity}
          scope={scope}
          showArchive={showArchive}
          onEntityChange={(key) => setEntity(key)}
          onScopeChange={(branch) => setScope(branch)}
          onArchiveChange={setShowArchive}
          onOpenClaim={openClaim}
          onLoadMore={() => {
            if (timeline?.next_offset != null) {
              void loadTimeline(entity, scope, timeline.next_offset);
            }
          }}
        />

        <footer className="legend">
          <span>
            <span className="swatch" style={{ background: "var(--created)" }} />
            added
          </span>
          <span>
            <span className="swatch" style={{ background: "var(--verified)" }} />
            verified
          </span>
          <span>
            <span className="swatch" style={{ background: "var(--disputed)" }} />
            disputed
          </span>
          <span>
            <span className="swatch" style={{ background: "var(--superseded)" }} />
            replaced
          </span>
        </footer>
      </main>

      <ProvenanceDrawer
        open={drawer !== null}
        loading={provenanceLoading}
        error={provenanceError}
        data={provenance}
        requested={drawer}
        onClose={() => setDrawer(null)}
        onOpenClaim={openClaim}
      />
    </div>
  );
}
