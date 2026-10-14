import { useEffect, useMemo, useState } from "react";
import { api, positionAt } from "../api";
import type { QueryResult, ReplayData } from "../api";
import { useStore } from "../store";

/** 2D floor plan with layers and a time scrubber replaying the event log (FR-UI-06, AC-UI-02). */
export function FacilityViewer() {
  const s = useStore();
  const [variant, setVariant] = useState("baseline");
  const [rep, setRep] = useState(0);
  const [data, setData] = useState<ReplayData | null>(null);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [layers, setLayers] = useState({ graph: true, zones: true, heat: false });
  const [heat, setHeat] = useState<Record<string, number>>({});
  const [variants, setVariants] = useState<string[]>(["baseline"]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!s.jobId) return;
    api.get<{ variants: { id: string }[] }>(`/jobs/${s.jobId}/summary`).then((r) => setVariants(r.variants.map((v) => v.id)));
  }, [s.jobId]);

  useEffect(() => {
    setData(null);
    setError(null);
    if (!s.jobId) return;
    api
      .get<ReplayData>(`/jobs/${s.jobId}/variants/${variant}/replications/${rep}/replay`)
      .then((d) => (setData(d), setT(0)))
      .catch((e) => setError(String(e)));
  }, [s.jobId, variant, rep]);

  useEffect(() => {
    if (!layers.heat || !s.jobId) return;
    api
      .post<QueryResult>("/query", { job_id: s.jobId, query: { metrics: ["blocking_time"], dimensions: ["location"], scope: { variants: [variant] } } })
      .then((r) => setHeat(Object.fromEntries(r.rows.map((x) => [x.location, x.blocking_time]))))
      .catch(() => setHeat({}));
  }, [layers.heat, s.jobId, variant]);

  useEffect(() => {
    if (!playing || !data) return;
    const id = setInterval(() => setT((x) => (x + data.duration / 200 > data.duration ? (setPlaying(false), data.duration) : x + data.duration / 200)), 50);
    return () => clearInterval(id);
  }, [playing, data]);

  const box = useMemo(() => {
    if (!data) return null;
    const pts = Object.values(data.nodes);
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const pad = Math.max(x1 - x0, y1 - y0) * 0.05 + 1;
    return { x: x0 - pad, y: y0 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad };
  }, [data]);

  if (!s.jobId) return <p className="muted">Select a completed run.</p>;
  if (error) return <p className="error">{error}</p>;
  if (!data || !box) return <p className="muted">loading replay…</p>;
  const r = Math.max(box.w, box.h) / 150;
  const maxHeat = Math.max(1, ...Object.values(heat));
  return (
    <div className="facility" data-testid="facility-viewer">
      <div className="row">
        <select data-testid="replay-variant" value={variant} onChange={(e) => setVariant(e.target.value)}>
          {variants.map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
        <label>
          rep <input type="number" min={0} value={rep} onChange={(e) => setRep(Number(e.target.value))} style={{ width: 60 }} />
        </label>
        {(["graph", "zones", "heat"] as const).map((k) => (
          <label key={k}>
            <input type="checkbox" checked={layers[k]} onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })} /> {k === "heat" ? "blocking heatmap" : k}
          </label>
        ))}
      </div>
      <svg viewBox={`${box.x} ${-(box.y + box.h)} ${box.w} ${box.h}`} className="plan" data-testid="floorplan">
        <g transform="scale(1,-1)">
          {layers.zones &&
            data.zones.map((z, i) => {
              const ps = z.nodes.map((n) => data.nodes[n]).filter(Boolean);
              const xs = ps.map((p) => p[0]);
              const ys = ps.map((p) => p[1]);
              return (
                <rect key={z.id} x={Math.min(...xs) - r} y={Math.min(...ys) - r} width={Math.max(...xs) - Math.min(...xs) + 2 * r} height={Math.max(...ys) - Math.min(...ys) + 2 * r} className={`zone z${i % 6}`}>
                  <title>{z.id}</title>
                </rect>
              );
            })}
          {layers.graph &&
            data.edges.map((e) => {
              const a = data.nodes[e.source];
              const b = data.nodes[e.target];
              return <line key={e.id} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} className="edge" strokeWidth={r / 3} />;
            })}
          {layers.heat &&
            Object.entries(heat).map(([loc, v]) =>
              data.nodes[loc] ? <circle key={loc} cx={data.nodes[loc][0]} cy={data.nodes[loc][1]} r={r * (1 + 3 * (v / maxHeat))} className="heat" opacity={0.2 + 0.6 * (v / maxHeat)} /> : null,
            )}
          {Object.entries(data.agents).map(([id, a]) => {
            const p = positionAt(a, t);
            if (!p) return null;
            return (
              <circle key={id} cx={p[0]} cy={p[1]} r={r} className="agent" data-agent={id} data-x={p[0].toFixed(4)} data-y={p[1].toFixed(4)}>
                <title>{id}</title>
              </circle>
            );
          })}
        </g>
      </svg>
      <div className="row">
        <button onClick={() => setPlaying(!playing)}>{playing ? "Pause" : "Play"}</button>
        <input type="range" min={0} max={data.duration} step={data.duration / 1000} value={t} data-testid="scrubber" onChange={(e) => setT(Number(e.target.value))} style={{ flex: 1 }} />
        <input type="number" data-testid="scrub-time" value={Number(t.toFixed(2))} onChange={(e) => setT(Number(e.target.value))} style={{ width: 90 }} /> s
      </div>
    </div>
  );
}
