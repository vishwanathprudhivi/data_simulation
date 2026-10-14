import { useEffect, useState } from "react";
import { API, api, formatValue } from "../api";
import type { Findings, MetricDef } from "../api";
import { useStore } from "../store";
import { TrustBar } from "./TrustBar";

interface FindingsOut {
  findings: Findings;
  narrative: { text: string };
}

/** Baseline vs variants with deltas, CIs and significance badges (FR-UI-08) + exports (FR-UI-09). */
export function Comparison() {
  const s = useStore();
  const [data, setData] = useState<FindingsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    if (!s.jobId) return;
    api
      .get<FindingsOut>(`/jobs/${s.jobId}/findings`)
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [s.jobId]);
  if (!s.jobId) return <p className="muted">Select a completed run.</p>;
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">loading…</p>;
  return (
    <div data-testid="comparison">
      <h2>Comparison · {s.jobId}</h2>
      <FindingsCard data={data} />
      <div className="row exports">
        <a data-testid="export-report" href={`${API}/jobs/${s.jobId}/report?format=html`} download={`${s.jobId}_report.html`}>
          Report (HTML)
        </a>
        <a href={`${API}/jobs/${s.jobId}/report?format=md`} download={`${s.jobId}_report.md`}>
          Report (Markdown)
        </a>
        <a href={`${API}/jobs/${s.jobId}/report?format=pptx`} download={`${s.jobId}.pptx`}>
          Slides (PPTX)
        </a>
        {s.scenarioRef && <ScenarioYamlLink />}
      </div>
    </div>
  );
}

function ScenarioYamlLink() {
  const s = useStore();
  const go = async () => {
    const levers = s.draft?.levers;
    const blob = await api.blob("/scenarios/export", { ...s.scenarioRef, ...(levers ? { levers } : {}) });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${s.scenario?.id ?? "scenario"}.yaml`;
    a.click();
  };
  return (
    <a href="#" onClick={(e) => (e.preventDefault(), go())}>
      Scenario YAML
    </a>
  );
}

export function FindingsCard({ data }: { data: FindingsOut }) {
  const s = useStore();
  const catalog: Record<string, MetricDef> = Object.fromEntries((s.manifest?.metrics ?? []).map((m) => [m.id, m]));
  const f = data.findings;
  return (
    <div className="card findings" data-testid="findings-card">
      <TrustBar findings={f} />
      <table>
        <thead>
          <tr>
            <th>Variant</th>
            <th>Metric</th>
            <th>Baseline</th>
            <th>Variant</th>
            <th>Δ (CI)</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(f.comparisons ?? []).map((c) => {
            const m = catalog[c.metric];
            const d = c.paired_diff;
            return (
              <tr key={c.ref_id} data-ref={c.ref_id}>
                <td>{c.variant}</td>
                <td title={m?.description ?? ""}>{m?.display_name ?? c.metric}</td>
                <td>{formatValue(c.baseline?.mean, m)}</td>
                <td>{formatValue(c.variant_estimate?.mean, m)}</td>
                <td>
                  {formatValue(d.mean, m)} ({formatValue(d.ci[0], m)} … {formatValue(d.ci[1], m)})
                </td>
                <td>
                  <span className={`badge ${c.significant ? "ok" : ""}`}>{c.significant ? "significant" : "n.s."}</span>
                  {c.extrapolated && <span className="badge warn">extrapolated</span>}
                  <span className="badge">{c.fidelity ?? "logical"}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {(f.caveats ?? []).map((c, i) => (
        <p key={i} className="caveat">
          ⚠ {c}
        </p>
      ))}
    </div>
  );
}
