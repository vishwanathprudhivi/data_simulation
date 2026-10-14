// One source of truth for the workspace: the selected scenario, the draft that the
// chat and the scenario editor share (FR-UI-03), jobs and pinned charts.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { CapabilityManifest, Job, ScenarioRef, ScenarioRow } from "./api";

export interface Draft {
  levers: Record<string, unknown>; // final lever values of the what-if
  replications: number;
  runLength: number | null; // seconds; null = scenario duration
  source: "chat" | "editor";
}

export interface PinnedChart {
  id: string;
  ref_id: string;
  job_id: string;
  chart: Record<string, any>;
  vega: Record<string, any>;
  title: string;
  note: string;
}

export type View = "editor" | "runs" | "compare" | "canvas" | "facility" | "live" | "explorer";

interface Store {
  scenarios: ScenarioRow[];
  scenario: ScenarioRow | null;
  scenarioRef: ScenarioRef | null;
  manifest: CapabilityManifest | null;
  baseLevers: Record<string, unknown>;
  draft: Draft | null;
  jobs: Job[];
  jobId: string | null;
  pinned: PinnedChart[];
  view: View;
  selectScenario(id: string): void;
  setDraft(d: Draft | null): void;
  setJobId(id: string | null): void;
  setView(v: View): void;
  pin(c: Omit<PinnedChart, "id">): void;
  updatePin(id: string, patch: Partial<PinnedChart>): void;
  unpin(id: string): void;
  setPinned(p: PinnedChart[]): void;
  refreshJobs(): Promise<void>;
}

const Ctx = createContext<Store | null>(null);

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [scenarios, setScenarios] = useState<ScenarioRow[]>([]);
  const [scenario, setScenario] = useState<ScenarioRow | null>(null);
  const [manifest, setManifest] = useState<CapabilityManifest | null>(null);
  const [baseLevers, setBaseLevers] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Draft | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [pinned, setPinned] = useState<PinnedChart[]>([]);
  const [view, setView] = useState<View>("editor");

  useEffect(() => {
    api.get<ScenarioRow[]>("/scenarios").then(setScenarios).catch(() => setScenarios([]));
  }, []);

  const scenarioRef = useMemo<ScenarioRef | null>(
    () => (scenario ? { scenario_id: scenario.id } : null),
    [scenario],
  );

  const selectScenario = useCallback(
    (id: string) => {
      const row = scenarios.find((s) => s.id === id) ?? null;
      setScenario(row);
      setDraft(null);
      if (!row) return;
      api.get<CapabilityManifest>(`/domains/${row.domain}`).then(setManifest);
      api
        .post<{ lever_values: Record<string, unknown> }>("/scenarios/validate", { scenario_id: id })
        .then((v) => setBaseLevers(v.lever_values));
    },
    [scenarios],
  );

  const refreshJobs = useCallback(async () => {
    setJobs(await api.get<Job[]>("/jobs"));
  }, []);

  useEffect(() => {
    refreshJobs();
    const t = setInterval(refreshJobs, 1500);
    return () => clearInterval(t);
  }, [refreshJobs]);

  const store: Store = {
    scenarios,
    scenario,
    scenarioRef,
    manifest,
    baseLevers,
    draft,
    jobs,
    jobId,
    pinned,
    view,
    selectScenario,
    setDraft,
    setJobId,
    setView,
    pin: (c) => setPinned((p) => [...p, { ...c, id: `pin_${Date.now()}_${p.length}` }]),
    updatePin: (id, patch) => setPinned((p) => p.map((c) => (c.id === id ? { ...c, ...patch } : c))),
    unpin: (id) => setPinned((p) => p.filter((c) => c.id !== id)),
    setPinned,
    refreshJobs,
  };
  return <Ctx.Provider value={store}>{children}</Ctx.Provider>;
}
