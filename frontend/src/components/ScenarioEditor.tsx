import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { LeverDef } from "../api";
import { experimentFor, launch, prepare } from "../runs";
import type { Prepared } from "../runs";
import { useStore } from "../store";

/** Form generated from the lever catalog + raw JSON view + diff (FR-UI-03). */
export function ScenarioEditor() {
  const s = useStore();
  const [raw, setRaw] = useState(false);
  const [scenarioJson, setScenarioJson] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Prepared | null>(null);
  const draft = s.draft ?? { levers: { ...s.baseLevers }, replications: 5, runLength: null, source: "editor" as const };
  const levers = s.manifest?.levers ?? [];

  const diff = useMemo(
    () =>
      levers.filter(
        (lv) =>
          draft.levers[lv.id] !== undefined &&
          JSON.stringify(draft.levers[lv.id]) !== JSON.stringify(s.baseLevers[lv.id]),
      ),
    [levers, draft.levers, s.baseLevers],
  );

  useEffect(() => {
    if (!s.scenarioRef || !raw) return;
    api
      .post<{ scenario: unknown }>("/scenarios/validate", { ...s.scenarioRef, levers: draft.levers })
      .then((v) => setScenarioJson(JSON.stringify(v.scenario, null, 2)))
      .catch((e) => setScenarioJson(String(e)));
  }, [raw, s.scenarioRef, draft.levers]);

  if (!s.scenario || !s.manifest) return <p className="muted">Select a scenario on the left.</p>;

  const set = (lv: LeverDef, value: unknown) =>
    s.setDraft({ ...draft, levers: { ...draft.levers, [lv.id]: value }, source: "editor" });

  const run = async () => {
    setError(null);
    try {
      const p = await prepare(experimentFor(s.manifest!, s.baseLevers, draft), s.scenarioRef!);
      if (p.needsConfirmation) {
        setPending(p);
        return;
      }
      await go(p);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };
  const go = async (p: Prepared) => {
    const job = await launch(p);
    setPending(null);
    s.setJobId(job.id);
    await s.refreshJobs();
    s.setView("runs");
  };

  return (
    <div className="editor" data-testid="scenario-editor">
      <div className="row">
        <h2>
          {s.scenario.name} <small className="muted">({s.manifest.domain} v{s.manifest.domain_version})</small>
        </h2>
        <label className="toggle">
          <input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} /> raw JSON
        </label>
      </div>
      {raw ? (
        <pre className="raw" data-testid="raw-json">{scenarioJson}</pre>
      ) : (
        <form className="levers" onSubmit={(e) => e.preventDefault()}>
          {levers.map((lv) => (
            <LeverField key={lv.id} lever={lv} value={draft.levers[lv.id]} base={s.baseLevers[lv.id]} onChange={(v) => set(lv, v)} />
          ))}
        </form>
      )}
      <h3>Diff vs baseline</h3>
      {diff.length ? (
        <ul data-testid="editor-diff">
          {diff.map((lv) => (
            <li key={lv.id}>
              {lv.display_name}: {String(s.baseLevers[lv.id])} → <b>{String(draft.levers[lv.id])}</b>
              {outside(lv.id, draft.levers[lv.id], s.manifest!.envelope ?? []) && <span className="badge warn">extrapolated</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No changes yet.</p>
      )}
      <div className="row">
        <label>
          Replications{" "}
          <input
            type="number"
            min={1}
            data-testid="replications"
            value={draft.replications}
            onChange={(e) => s.setDraft({ ...draft, replications: Number(e.target.value) })}
          />
        </label>
        <label>
          Run length (s){" "}
          <input
            type="number"
            min={60}
            data-testid="run-length"
            placeholder="scenario"
            value={draft.runLength ?? ""}
            onChange={(e) => s.setDraft({ ...draft, runLength: e.target.value ? Number(e.target.value) : null })}
          />
        </label>
        <button data-testid="run" disabled={!diff.length} onClick={run}>
          Run vs baseline
        </button>
      </div>
      {pending && (
        <div className="confirm" data-testid="confirm-gate">
          This run needs confirmation: {pending.estimate.variants} variants × {pending.estimate.replications} replications, about{" "}
          {pending.estimate.minutes.toFixed(1)} min ({pending.estimate.mode}).{" "}
          <button onClick={() => go(pending)}>Confirm and run</button> <button onClick={() => setPending(null)}>Cancel</button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function outside(lever: string, v: unknown, envelope: { lever: string; min?: number | null; max?: number | null }[]) {
  const b = envelope.find((e) => e.lever === lever);
  if (!b || typeof v !== "number") return false;
  return (b.min != null && v < b.min) || (b.max != null && v > b.max);
}

function LeverField({ lever, value, base, onChange }: { lever: LeverDef; value: unknown; base: unknown; onChange(v: unknown): void }) {
  const id = `lever-${lever.id}`;
  let input;
  if (lever.type === "enum") {
    input = (
      <select id={id} data-testid={id} value={String(value ?? base ?? "")} onChange={(e) => onChange(e.target.value)}>
        {(lever.choices ?? []).map((c) => (
          <option key={String(c)} value={String(c)}>
            {String(c)}
          </option>
        ))}
      </select>
    );
  } else if (lever.type === "bool") {
    input = <input id={id} data-testid={id} type="checkbox" checked={Boolean(value ?? base)} onChange={(e) => onChange(e.target.checked)} />;
  } else if (lever.type === "distribution") {
    input = <code>{JSON.stringify(value ?? base)}</code>;
  } else {
    input = (
      <input
        id={id}
        data-testid={id}
        type="number"
        min={lever.min ?? undefined}
        max={lever.max ?? undefined}
        step={lever.type === "int" ? 1 : lever.step ?? "any"}
        value={value === undefined || value === null ? "" : Number(value)}
        onChange={(e) => onChange(lever.type === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value))}
      />
    );
  }
  return (
    <div className="field">
      <label htmlFor={id} title={lever.description ?? ""}>
        {lever.display_name}
        {lever.unit && lever.unit !== "1" ? ` (${lever.unit})` : ""}
      </label>
      {input}
      <small className="muted">baseline {String(base)}</small>
    </div>
  );
}
