// Thin typed client for /api/v1. Spec payloads use the generated types; the
// envelopes below (jobs, cards, chat turns) are API responses, not spec contracts.
import type { CapabilityManifest } from "./generated/capability_manifest";
import type { Findings } from "./generated/findings";

export const API = "/api/v1";

export type LeverDef = CapabilityManifest["levers"][number];
export type MetricDef = CapabilityManifest["metrics"][number];
export type DimensionDef = CapabilityManifest["dimensions"][number];
export type { CapabilityManifest, Findings };

export interface ScenarioRef {
  scenario_id?: string;
  version?: number;
  example?: string;
  levers?: Record<string, unknown>;
  overrides?: Record<string, unknown>;
}

export interface ScenarioRow {
  id: string;
  name: string;
  domain: string;
  version: number;
  pinned: boolean | number;
  tags: string[];
}

export interface Estimate {
  variants: number;
  replications: number;
  runs: number;
  minutes: number;
  mode: string;
}

export interface Job {
  id: string;
  status: string;
  progress_completed: number | null;
  progress_planned: number | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  estimate: Estimate | null;
  cached: boolean;
  error: string | null;
}

export interface DiffLine {
  lever: string;
  display_name: string;
  baseline: unknown;
  value: unknown;
  unit: string;
  text: string;
}

export interface DraftVariant {
  id: string;
  diff: DiffLine[];
  levers: Record<string, unknown>;
  extrapolated: boolean;
  outside_envelope: Record<string, { value: number; min: number | null; max: number | null }>;
}

export interface CompileOut {
  ok: boolean;
  domain?: string;
  card?: {
    intent: string;
    summary: string;
    runnable: boolean;
    variants: DraftVariant[];
    experiment: Record<string, unknown> | null;
    assumptions: string[];
    ambiguities: string[];
    capability_gaps: { request: string; reason: string; nearest_lever: string | null }[];
  };
  messages?: string[];
  confirmation?: {
    estimate: Estimate;
    fidelity: string;
    needs_confirmation: boolean;
    confirmation_token: string;
  };
  submit?: { experiment: Record<string, unknown>; scenario: ScenarioRef; mode: string };
  issues?: string[];
}

export interface ToolCall {
  name: string;
  input: Record<string, unknown>;
  is_error: boolean;
  output?: any;
}

export interface ChatTurn {
  user: string;
  reply: string;
  tool_calls: ToolCall[];
  grounding: { ok: boolean; unreferenced: string[] };
  pending_actions: string[];
  state: { job_id: string | null; variants: string[]; last_chart: string | null };
}

export interface QueryResult {
  ref_id: string;
  columns: string[];
  rows: Record<string, any>[];
  provenance: Record<string, any>;
  warnings: string[];
}

export interface RenderOut {
  ref_id: string;
  vega_lite: Record<string, any>;
  query: QueryResult;
}

export interface ReplayData {
  duration: number;
  nodes: Record<string, number[]>;
  edges: { id: string; source: string; target: string; zone: string | null }[];
  zones: { id: string; nodes: string[] }[];
  agents: Record<
    string,
    { spawn: number[] | null; segments: [number, number, number, number, number, number][] }
  >;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: any,
  ) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data);
  return data as T;
}

export const api = {
  get: <T>(path: string) => req<T>("GET", path),
  post: <T>(path: string, body: unknown) => req<T>("POST", path, body),
  async blob(path: string, body: unknown): Promise<Blob> {
    const r = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new ApiError(r.status, await r.text());
    return r.blob();
  },
};

export function download(blob: Blob, name: string): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Format a value with the catalog's number format (FR-UI-11: nothing hard-coded). */
export function formatValue(v: unknown, m?: MetricDef | null): string {
  if (v === null || v === undefined) return "–";
  if (typeof v !== "number") return String(v);
  const f = m?.format ?? { kind: "number", decimals: 2 };
  const d = f.decimals ?? 2;
  switch (f.kind) {
    case "percent":
      return `${(v * 100).toFixed(d)}%`;
    case "integer":
      return Math.round(v).toLocaleString();
    case "duration":
      return v >= 3600 ? `${(v / 3600).toFixed(1)} h` : v >= 60 ? `${(v / 60).toFixed(1)} min` : `${v.toFixed(d)} s`;
    default:
      return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : v.toFixed(d);
  }
}

/** Position of an agent at time t: same rule as simcore.core.backend.TrajectoryStore. */
export function positionAt(agent: ReplayData["agents"][string], t: number): number[] | null {
  const segs = agent.segments;
  if (!segs.length) return agent.spawn;
  let lo = 0;
  let hi = segs.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid][1] < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo >= segs.length) {
    const s = segs[segs.length - 1];
    return [s[4], s[5]];
  }
  const [t0, t1, x0, y0, x1, y1] = segs[lo];
  if (t < t0) {
    if (lo === 0) return [x0, y0];
    const p = segs[lo - 1];
    return [p[4], p[5]];
  }
  if (t1 <= t0) return [x1, y1];
  const f = Math.min(Math.max((t - t0) / (t1 - t0), 0), 1);
  return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f];
}
