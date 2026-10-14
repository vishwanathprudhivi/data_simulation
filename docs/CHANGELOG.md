# Changelog

## M2 – Experiment layer (2026-10-14)

**Exit gate:** AC-EXP-03, AC-EXP-04, AC-IFC-01 (logical) — all passing.

Measured for AC-EXP-03 (baseline vs 4% lower arrival rate, 10 replications;
independent-seed half-width ÷ CRN paired half-width): wait_time_mean 2.8x,
throughput 5.8x, utilization 4.6x. With CRN the wait-time difference is detected
(CI excludes 0); with independent seeds it is not. Gate threshold: > 2x.

Requirements implemented:
- FR-EXP-02 CRN seed mapping; FR-EXP-03 named variants + full-factorial factors
  (explicit design; Latin hypercube raises a clear error, stretch);
  FR-EXP-04 Student-t CIs; FR-EXP-05 paired comparisons with `ci_excludes_zero`
  (Welch for independent seeds); FR-EXP-06 fixed warm-up plus Welch diagnostic
  plot and MSER suggestion (`simcore warmup`); FR-EXP-07 adaptive replication;
  FR-EXP-08 fitting (exponential, lognormal, gamma, Weibull, normal; AIC ranking,
  KS/AD, Q-Q; `simcore fit`); FR-EXP-09 Parquet results with provenance;
  FR-EXP-10 warnings for low n, degenerate variance, failures, unknown KPIs.
- FR-IFC-01 `simcore experiment | fit | report | warmup`; FR-IFC-02 `simcore.api`
  exports `Engine`, `Experiment`, `Evaluator`; FR-IFC-03 Evaluator with
  memory/disk cache; FR-IFC-04 process pool with per-replication timeout;
  FR-IFC-06 Markdown + HTML reports.
- NFR-08 failed replications are retried/recorded and never abort an experiment.

Decisions: ADR-006. Guide: `docs/guides/experiments.md`.

Deferrals: fidelity promotion (needs M4; warns), Latin hypercube (stretch),
plan inputs to the Evaluator (M5 plan follower).

Known issues: service times are drawn when a task starts, so CRN synchronisation
of service draws relies on FIFO start order being the same across alternatives;
attaching draws to task identity would be stronger (revisit with M3 policies).

## M1 – Logical core (2026-10-14)

**Exit gate:** AC-ENG-01, AC-ENG-02, AC-ENG-03, AC-ENG-04, AC-EXP-01, AC-EXP-02 — all passing
(`make test`, `make lint`).

Requirements implemented (tests are named after them):
- FR-ENG-01 lifecycle with order enforcement; FR-ENG-02 event-driven and fixed-step
  advance, real-time factor in metadata; FR-ENG-03 typed deterministic bus and
  `(time, priority, seq)` scheduler; FR-ENG-04 entity registry; FR-ENG-05 plug-in
  discovery/registry; FR-ENG-06 RNG manager; FR-ENG-08 actionable config errors;
  FR-ENG-09 `WorldBackend` + `logical` backend (kinematic/physics: M4).
- FR-SCN-01/02/03/05 scenario layer: stochastic/replay/list demand, core
  disruptions (resource_unavailable, demand_surge, speed_change handled in core;
  location/capacity types are published for domains/M3), `extends`.
  FR-SCN-04 condition triggers (queue_length/released/completed).
- FR-DSP-01/02/03/04/06: task model, `TaskQueue` + `InMemoryQueue` with
  conformance suite, deterministic ordering, pull/push dispatch, no Redis needed.
- FR-AGT-01/02/04/05: behavior specs, `Policy` contract with fifo / nearest_task /
  zone_affinity, per-agent speed multipliers, heterogeneous fleets.
- FR-POL-01/05 uniform observation/action contract with JSON schemas;
  `ReasoningClient` (disabled by default, deterministic mock) — AC-POL-01 holds:
  the test suite blocks all network access.
- FR-OBS-01/02/03/06: Parquet (+ JSONL) event log, KPI registry, core KPIs
  (throughput, cycle/wait time percentiles, queue length, utilization, state
  breakdown, due-time adherence), run metadata. FR-OBS-04 partial: `simcore replay`
  summarizes a log and agent states at given times (viewport replay: M4/M11).
- FR-IFC-01 partial: `simcore validate | run | replay | schemas export | domains list`
  (`experiment`, `fit`, `report` arrive in M2). FR-IFC-02 partial: `simcore.api`.
- `toy_queue` domain (M/M/c + Erlang C helper) and warehouse placeholder (ADR-004).

Assumptions / decisions: ADR-003 (RNG + determinism), ADR-004 (reference scenario
until M3), ADR-005 (config conventions).

Deferrals: FR-ENG-07 checkpoint/restore (S), FR-AGT-03 state-machine export (S),
FR-AGT-07 shift schedules (S, schema exists), FR-SCN-06 beats (schema only; M13).

Known issues: a `demand_surge` does not reschedule an already-scheduled next
arrival (ADR-003).

## M0 – Foundations (2026-10-14)

**Exit gate:** CI green; schemas validate sample configs — passing
(`tests/unit/test_specs.py`).

- Repository reset: previous contents removed (only README existed); LICENSE kept.
- Scaffold per BRD Section 11; tooling: ruff, mypy (`--strict` on `core/` and
  `specs/`), pytest (+cov), import-linter, pre-commit, GitHub Actions (Python 3.10, 3.12).
- Versioned spec contracts as Pydantic v2 models with generated JSON Schemas in
  `schemas/`: facility, behaviors, scenario, experiment (the four config
  documents) plus DomainDefinition, Lever/Metric/DimensionCatalog, QuerySpec,
  ChartSpec, Findings. Migration hook registry (`register_migration`).
- Plug-in loader skeleton (entry points + `module:attr`).
- ADR-001 (Isaac Sim pin, provisional), ADR-002 (layout, discovery, generated schemas).
