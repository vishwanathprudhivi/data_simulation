import { api } from "./api";
import type { CapabilityManifest, Estimate, Job, ScenarioRef } from "./api";
import type { Draft } from "./store";

/** The experiment for a single what-if vs the baseline, from lever values (CRN, explicit). */
export function experimentFor(
  manifest: CapabilityManifest,
  base: Record<string, unknown>,
  draft: Draft,
): Record<string, unknown> {
  const overrides: Record<string, unknown> = {};
  for (const lv of manifest.levers) {
    const v = draft.levers[lv.id];
    if (v !== undefined && JSON.stringify(v) !== JSON.stringify(base[lv.id])) overrides[lv.path] = v;
  }
  const exp: Record<string, unknown> = {
    id: `ui_${Date.now().toString(36)}`,
    design: "explicit",
    include_baseline: true,
    crn: true,
    replications: draft.replications,
    variants: [{ id: "what_if", name: "What-if (editor)", overrides }],
  };
  if (draft.runLength) exp.run_length = draft.runLength;
  return exp;
}

export interface Prepared {
  body: { experiment: Record<string, unknown>; scenario: ScenarioRef; mode: string };
  estimate: Estimate;
  needsConfirmation: boolean;
  token: string;
}

export async function prepare(
  experiment: Record<string, unknown>,
  scenario: ScenarioRef,
  mode = "logical",
): Promise<Prepared> {
  const body = { experiment, scenario, mode };
  const est = await api.post<{
    estimate: Estimate;
    needs_confirmation: boolean;
    confirmation_token: string;
  }>("/experiments/estimate", body);
  return {
    body,
    estimate: est.estimate,
    needsConfirmation: est.needs_confirmation,
    token: est.confirmation_token,
  };
}

export async function launch(p: Prepared): Promise<Job> {
  return api.post<Job>("/experiments", { ...p.body, confirmation_token: p.token });
}
