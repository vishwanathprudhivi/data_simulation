import { useEffect, useMemo, useRef, useState } from "react";
import { API, api, ApiError, formatValue } from "../api";
import type { MetricDef } from "../api";
import { useStore } from "../store";

interface LiveState {
  t: number;
  duration: number;
  playing: boolean;
  speed: number;
  finished: boolean;
  hud: Record<string, number>;
  captions: { t: number; beat: string; text: string }[];
  injections: { t: number; id: string; type: string; source: string }[];
  active_disruptions: string[];
  next_beat: string | null;
  positions: Record<string, number[]>;
}

interface Facility {
  nodes: Record<string, number[]>;
  edges: { id: string; source: string; target: string }[];
}

/** Interactive/demo mode: play the scenario with beats, inject disruptions live, KPI HUD (M13). */
export function LiveView() {
  const s = useStore();
  const [id, setId] = useState<string | null>(null);
  const [state, setState] = useState<LiveState | null>(null);
  const [facility, setFacility] = useState<Facility | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [inj, setInj] = useState({ id: "", type: "", target: "", duration: 900, count: "" });
  const es = useRef<EventSource | null>(null);

  const catalog: Record<string, MetricDef> = useMemo(
    () => Object.fromEntries((s.manifest?.metrics ?? []).map((m) => [m.formula, m])),
    [s.manifest],
  );
  const types = s.manifest?.disruption_types ?? [];

  useEffect(() => () => es.current?.close(), []);
  useEffect(() => {
    es.current?.close();
    setId(null);
    setState(null);
  }, [s.scenarioRef]);

  const start = async () => {
    setError(null);
    try {
      const r = await api.post<{ id: string; state: LiveState; facility: Facility | null }>("/interactive/sessions", {
        scenario: s.scenarioRef,
        speed: 300,
      });
      setId(r.id);
      setState({ ...r.state, positions: {} });
      setFacility(r.facility);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const control = async (body: Record<string, unknown>) => {
    if (!id) return;
    setError(null);
    try {
      const st = await api.post<LiveState>(`/interactive/sessions/${id}/control`, body);
      setState(st);
      if (body.action === "play") stream();
      if (body.action === "pause") es.current?.close();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const stream = () => {
    es.current?.close();
    const src = new EventSource(`${API}/interactive/sessions/${id}/stream`);
    src.addEventListener("state", (e) => {
      const st = JSON.parse((e as MessageEvent).data) as LiveState;
      setState(st);
      if (!st.playing) src.close();
    });
    src.addEventListener("done", () => src.close());
    src.onerror = () => src.close();
    es.current = src;
  };

  const box = useMemo(() => {
    if (!facility) return null;
    const pts = Object.values(facility.nodes);
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const pad = 2;
    return { x: Math.min(...xs) - pad, y: Math.min(...ys) - pad, w: Math.max(...xs) - Math.min(...xs) + 2 * pad, h: Math.max(...ys) - Math.min(...ys) + 2 * pad };
  }, [facility]);

  if (!s.scenarioRef) return <p className="muted">Select a scenario to run it live.</p>;
  return (
    <div className="live" data-testid="live-view">
      <h2>Live · {s.scenario?.name}</h2>
      {!id ? (
        <button data-testid="live-start" onClick={start}>
          Start live session
        </button>
      ) : (
        <>
          <div className="row">
            <button data-testid="live-play" onClick={() => control({ action: state?.playing ? "pause" : "play" })} disabled={state?.finished}>
              {state?.playing ? "Pause" : "Play"}
            </button>
            <button data-testid="live-step" onClick={() => control({ action: "step", seconds: 600 })} disabled={state?.finished}>
              Step 10 min
            </button>
            <label>
              speed ×
              <input type="number" data-testid="live-speed" min={1} value={state?.speed ?? 300} onChange={(e) => control({ action: "speed", speed: Number(e.target.value) })} style={{ width: 80 }} />
            </label>
            <span data-testid="live-clock">
              t = {Math.round(state?.t ?? 0)} s / {state?.duration} s
            </span>
            {state?.next_beat && <span className="muted">next beat: {state.next_beat}</span>}
          </div>
          <div className="bar">
            <div style={{ width: `${(100 * (state?.t ?? 0)) / (state?.duration || 1)}%` }} />
          </div>
          <div className="hud" data-testid="kpi-hud">
            {Object.entries(state?.hud ?? {}).map(([k, v]) => (
              <div key={k} className="card hud-item" data-kpi={k}>
                <small className="muted">{catalog[k]?.display_name ?? k}</small>
                <b>{formatValue(v, catalog[k])}</b>
              </div>
            ))}
          </div>
          <div className="row">
            {(state?.active_disruptions ?? []).map((d) => (
              <span key={d} className="badge warn" data-testid="active-disruption">
                {d}
              </span>
            ))}
          </div>
          <form
            className="row inject"
            onSubmit={(e) => {
              e.preventDefault();
              control({
                action: "inject",
                inject: {
                  id: inj.id || `live_${Date.now().toString(36)}`,
                  type: inj.type || types[0],
                  target: inj.target || null,
                  duration: inj.duration,
                  params: inj.count ? { count: Number(inj.count) } : {},
                },
              });
            }}
          >
            <b>Inject</b>
            <select data-testid="inject-type" value={inj.type} onChange={(e) => setInj({ ...inj, type: e.target.value })}>
              <option value="">type…</option>
              {types.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
            <input data-testid="inject-target" placeholder="target (e.g. zone:aisle_02)" value={inj.target} onChange={(e) => setInj({ ...inj, target: e.target.value })} />
            <input type="number" data-testid="inject-duration" value={inj.duration} onChange={(e) => setInj({ ...inj, duration: Number(e.target.value) })} style={{ width: 90 }} /> s
            <input data-testid="inject-count" placeholder="count" value={inj.count} onChange={(e) => setInj({ ...inj, count: e.target.value })} style={{ width: 60 }} />
            <button data-testid="inject-go" disabled={state?.finished}>
              Inject now
            </button>
          </form>
          <div className="captions" data-testid="captions">
            {(state?.captions ?? []).map((c, i) => (
              <p key={i}>
                <span className="muted">[{Math.round(c.t)} s]</span> <b>{c.beat}</b>: {c.text}
              </p>
            ))}
          </div>
          {facility && box && (
            <svg viewBox={`${box.x} ${-(box.y + box.h)} ${box.w} ${box.h}`} className="plan" data-testid="live-plan">
              <g transform="scale(1,-1)">
                {facility.edges.map((e) => {
                  const a = facility.nodes[e.source];
                  const b = facility.nodes[e.target];
                  return <line key={e.id} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} className="edge" strokeWidth={0.3} />;
                })}
                {Object.entries(state?.positions ?? {}).map(([agent, p]) => (
                  <circle key={agent} cx={p[0]} cy={p[1]} r={0.8} className="agent" data-agent={agent} />
                ))}
              </g>
            </svg>
          )}
        </>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
