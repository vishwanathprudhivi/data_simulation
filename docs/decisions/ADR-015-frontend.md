# ADR-015: Frontend SPA (M11)

- **Status:** Accepted
- **Requirements:** FR-UI-01..12, FR-API-08, AC-UI-01/02

## Decisions
- **Stack.** React 19 + TypeScript + Vite, Vega-Lite via vega-embed, SSE for job
  progress, Playwright for e2e (BRD 7.10). No UI framework: a small CSS file keeps the
  bundle and the dependency surface small.
- **Types from schemas.** `npm run gen:types` compiles every `schemas/*.schema.json`
  into `src/generated/<name>.ts` with `json-schema-to-typescript` (one module per
  schema so same-named nested types such as `Comparison` do not collide). CI checks
  the generated files are current. API envelopes (jobs, cards, chat turns) are typed
  by hand in `src/api.ts`; they are responses, not spec contracts.
- **One source of truth for the draft.** The chat's draft card and the scenario
  editor share one `Draft` (lever values + replications + run length) in the store.
  "Edit in form" loads the chat draft; the editor's Run builds the same explicit,
  CRN experiment the compiler builds (variant overrides at the levers' paths) and goes
  through `/experiments/estimate` and the confirmation gate.
- **Catalog-driven.** Lever forms are generated from the lever catalog (type, bounds,
  choices, units); chart builders list catalog metrics and their allowed dimensions;
  values are formatted with each metric's `format`. Trust indicators (mode,
  replications, confidence, envelope) are shown on every findings card.
- **Replay.** `GET /jobs/{id}/variants/{v}/replications/{r}/replay` returns the
  facility graph, zones and piecewise-linear trajectories rebuilt from the event log;
  the viewer interpolates with the same rule as `TrajectoryStore.position`.
  `/snapshot?t=` computes positions server-side; AC-UI-02 compares the two at sampled
  timestamps in the browser, and a Python test cross-checks an independent
  implementation.
- **Serving.** The API mounts the built SPA (`SIMCORE_FRONTEND`) after all API routes,
  so one origin serves both (Docker image builds it in a Node stage).
- **Gaps.** Chat replies are not streamed yet (FR-UI-02 asks for streaming); the 3D
  Omniverse viewer (FR-UI-07, S) and PPTX export (stretch) are not implemented;
  partial results during a run show progress only.
