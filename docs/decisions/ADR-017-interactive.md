# ADR-017: Interactive / demo mode (M13)

- **Status:** Accepted
- **Requirements:** FR-IFC-05, FR-SCN-06, AC-IFC-02; FR-UI-07 (3D streaming) deferred

## Decisions
- **One engine, incremental runs.** `InteractiveSession` drives the normal `Engine`
  with repeated `run(until=t)`. Wall time only decides how far to advance per tick
  (`speed` = sim seconds per wall second); the simulation never reads it.
- **Beats.** `ScenarioSpec.beats` gained `speed` and `inject` (a list of
  `InjectSpec`: a disruption without a start time). Beats act only in interactive
  sessions (narrate, pause, change speed, inject); batch runs ignore them.
- **Live injection = scheduled disruption at now.** Operator and beat injections
  schedule `disruption.begin` at the current sim-time, exactly like scheduled
  disruptions, and are recorded with their time. `recorded_scenario()` turns them
  into scheduled disruptions; a batch run of it reproduces the live session
  bit-for-bit (tested).
- **HUD.** Running KPIs over `[0, now]` from the normal KPI registry (domain metrics
  first, then core ones), cached per sim-time.
- **Surfaces.** `simcore interactive` (headless HUD, `--inject`, `--record`,
  `--realtime`), `/interactive/sessions` (control, state with `advance`, SSE stream
  while playing, recorded scenario) and the UI "Live" tab (play/pause/step/speed,
  inject form from the domain's disruption types, HUD formatted from the catalog,
  captions, live floor plan).
- **Isaac / 3D.** `--mode kinematic` runs the same session on the Omniverse backend
  where Isaac Sim is installed; streaming the viewport into the web UI (Omniverse
  WebRTC) is not implemented and could not be verified without a GPU host.
