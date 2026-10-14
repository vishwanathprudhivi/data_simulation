import { useEffect, useState } from "react";
import { api, ApiError, download } from "../api";
import type { RenderOut } from "../api";
import { useStore } from "../store";
import { VegaChart } from "./VegaChart";

/** Chart builder over the catalogs + pinned, annotated charts saved as analyses (FR-UI-04). */
export function ResultsCanvas() {
  const s = useStore();
  const [templates, setTemplates] = useState<Record<string, string>>({});
  const [template, setTemplate] = useState("bar_ci");
  const [metric, setMetric] = useState("");
  const [dimension, setDimension] = useState("variant");
  const [preview, setPreview] = useState<{ out: RenderOut; chart: Record<string, any> } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [analyses, setAnalyses] = useState<{ id: string; name: string }[]>([]);
  const [name, setName] = useState("");

  useEffect(() => {
    api.get<Record<string, string>>("/charts/templates").then(setTemplates);
    api.get<{ id: string; name: string }[]>("/analyses").then(setAnalyses);
  }, []);
  const metrics = s.manifest?.metrics ?? [];
  const m = metrics.find((x) => x.id === metric);
  const dims = m ? (m.allowed_dimensions?.length ? m.allowed_dimensions : ["variant"]) : ["variant"];

  const render = async () => {
    setError(null);
    const chart = {
      template,
      title: `${m?.display_name ?? metric} by ${dimension}`,
      query: { metrics: [metric], dimensions: Array.from(new Set(["variant", dimension])) },
    };
    try {
      const out = await api.post<RenderOut>("/charts/render", { job_id: s.jobId, chart });
      setPreview({ out, chart });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const save = async () => {
    const payload = { charts: s.pinned.map(({ chart, job_id, title, note, ref_id }) => ({ chart, job_id, title, note, ref_id })) };
    await api.post("/analyses", { name: name || "analysis", payload });
    setAnalyses(await api.get("/analyses"));
  };

  const load = async (id: string) => {
    const a = await api.get<{ payload: { charts: any[] } }>(`/analyses/${id}`);
    const pins = [];
    for (const c of a.payload.charts ?? []) {
      const out = await api.post<RenderOut>("/charts/render", { job_id: c.job_id, chart: c.chart });
      pins.push({ ...c, id: `pin_${pins.length}`, vega: out.vega_lite });
    }
    s.setPinned(pins);
  };

  const exportChart = async (job: string, chart: Record<string, any>, format: string) => {
    const blob = await api.blob("/charts/export", { job_id: job, chart, format });
    download(blob, `chart.${format}`);
  };

  return (
    <div className="canvas" data-testid="results-canvas">
      <h2>Results canvas</h2>
      {s.jobId ? (
        <div className="builder row">
          <select data-testid="chart-template" value={template} onChange={(e) => setTemplate(e.target.value)}>
            {Object.keys(templates).map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <select data-testid="chart-metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
            <option value="">metric…</option>
            {metrics.map((x) => (
              <option key={x.id} value={x.id}>
                {x.display_name}
              </option>
            ))}
          </select>
          <select data-testid="chart-dimension" value={dimension} onChange={(e) => setDimension(e.target.value)}>
            {dims.map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
          <button data-testid="chart-render" disabled={!metric} onClick={render}>
            Render
          </button>
        </div>
      ) : (
        <p className="muted">Select a completed run to build charts.</p>
      )}
      {error && <p className="error">{error}</p>}
      {preview && (
        <div className="card">
          <VegaChart spec={preview.out.vega_lite} testId="chart-preview" />
          <button
            data-testid="pin-chart"
            onClick={() =>
              s.pin({ ref_id: preview.out.ref_id, job_id: s.jobId!, chart: preview.chart, vega: preview.out.vega_lite, title: preview.chart.title, note: "" })
            }
          >
            Pin
          </button>
        </div>
      )}
      <div className="pins" data-testid="pinned">
        {s.pinned.map((p) => (
          <div key={p.id} className="card pin" data-testid="pinned-chart">
            <input value={p.title} onChange={(e) => s.updatePin(p.id, { title: e.target.value })} />
            <VegaChart spec={p.vega} />
            <textarea placeholder="annotation" value={p.note} onChange={(e) => s.updatePin(p.id, { note: e.target.value })} />
            <div className="row">
              <small className="muted">{p.ref_id}</small>
              {["png", "svg", "csv"].map((f) => (
                <button key={f} onClick={() => exportChart(p.job_id, p.chart, f)}>
                  {f.toUpperCase()}
                </button>
              ))}
              <button onClick={() => s.unpin(p.id)}>Remove</button>
            </div>
          </div>
        ))}
      </div>
      <div className="row">
        <input data-testid="analysis-name" placeholder="analysis name" value={name} onChange={(e) => setName(e.target.value)} />
        <button data-testid="save-analysis" disabled={!s.pinned.length} onClick={save}>
          Save as analysis
        </button>
        <select onChange={(e) => e.target.value && load(e.target.value)} value="">
          <option value="">open analysis…</option>
          {analyses.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
