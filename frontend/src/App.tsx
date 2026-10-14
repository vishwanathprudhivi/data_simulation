import { ChatPanel } from "./components/ChatPanel";
import { Comparison } from "./components/Comparison";
import { DomainExplorer } from "./components/DomainExplorer";
import { FacilityViewer } from "./components/FacilityViewer";
import { LiveView } from "./components/LiveView";
import { ResultsCanvas } from "./components/ResultsCanvas";
import { RunMonitor } from "./components/RunMonitor";
import { ScenarioEditor } from "./components/ScenarioEditor";
import { useStore } from "./store";
import type { View } from "./store";

const VIEWS: [View, string][] = [
  ["editor", "Scenario"],
  ["runs", "Runs"],
  ["compare", "Comparison"],
  ["canvas", "Canvas"],
  ["facility", "Facility"],
  ["live", "Live"],
  ["explorer", "Domain"],
];

/** Workspace layout: navigation, central canvas, chat panel (FR-UI-01). */
export function App() {
  const s = useStore();
  return (
    <div className="app">
      <nav className="nav">
        <h1>simcore</h1>
        <h3>Scenarios</h3>
        <ul data-testid="scenario-list">
          {s.scenarios.map((sc) => (
            <li key={sc.id}>
              <button className={s.scenario?.id === sc.id ? "active" : ""} data-testid={`scenario-${sc.id}`} onClick={() => s.selectScenario(sc.id)}>
                {sc.name} {sc.pinned ? <span className="badge">baseline</span> : null}
                <small className="muted"> {sc.domain}</small>
              </button>
            </li>
          ))}
        </ul>
        <h3>Experiments</h3>
        <ul>
          {s.jobs.slice(0, 12).map((j) => (
            <li key={j.id}>
              <button className={s.jobId === j.id ? "active" : ""} onClick={() => (s.setJobId(j.id), s.setView(j.status === "completed" ? "compare" : "runs"))}>
                {j.id} <small className="muted">{j.status}</small>
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <main className="main">
        <div className="tabs">
          {VIEWS.map(([v, label]) => (
            <button key={v} data-testid={`tab-${v}`} className={s.view === v ? "active" : ""} onClick={() => s.setView(v)}>
              {label}
            </button>
          ))}
        </div>
        <section className="view">
          {s.view === "editor" && <ScenarioEditor />}
          {s.view === "runs" && <RunMonitor />}
          {s.view === "compare" && <Comparison />}
          {s.view === "canvas" && <ResultsCanvas />}
          {s.view === "facility" && <FacilityViewer />}
          {s.view === "live" && <LiveView />}
          {s.view === "explorer" && <DomainExplorer />}
        </section>
      </main>
      <ChatPanel />
    </div>
  );
}
