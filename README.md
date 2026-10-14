# simcore

A reusable, domain-agnostic simulation engine for NVIDIA Omniverse / Isaac Sim,
with a statistically rigorous experiment layer and (in later milestones) a
service layer, semantic layer, conversational analyst, web frontend and domain
onboarding kit.

- Requirements: [`docs/BRD.md`](docs/BRD.md) · Working rules: [`CLAUDE.md`](CLAUDE.md)
- Progress: [`docs/CHANGELOG.md`](docs/CHANGELOG.md) · Decisions: [`docs/decisions/`](docs/decisions/)

## Status

| Milestone | State |
|-----------|-------|
| M0 Foundations | done |
| M1 Logical core | done |
| M2 Experiment layer | done |
| M3 Facility + interaction (warehouse domain) | done |
| M4 Omniverse adapter | code done; Isaac/GPU verification pending |
| M5 Dispatch & policies (Redis, plans, solver) | done |
| M4 – M14 | in progress |

## Quick start

```bash
make setup                      # venv + dev install
make lint test                  # ruff, mypy, import-linter; tests (no GPU/Redis)

.venv/bin/simcore validate examples/scenarios/toy_queue/mmc_baseline.yaml
.venv/bin/simcore run examples/scenarios/toy_queue/mmc_baseline.yaml --out runs
# -> runs/adhoc/mmc_baseline/rep_000/{events.parquet,kpis.json,meta.json}
.venv/bin/simcore run examples/scenarios/toy_queue/mmc_baseline.yaml --set fleet.server.count=4
.venv/bin/simcore domains list

# warehouse reference domain (geometry-derived congestion)
.venv/bin/simcore validate examples/facilities/broken.yaml          # shows actionable errors
.venv/bin/simcore run examples/scenarios/warehouse/narrow_aisles.yaml
.venv/bin/simcore experiment examples/experiments/warehouse_fleet_sweep.yaml --out runs

# experiments: baseline + variants x replications with CRN, CIs and a report
.venv/bin/simcore experiment examples/experiments/mmc_servers.yaml --out runs --parallel 4
# -> runs/mmc_servers/{report.html,report.md,summary.json,results.parquet,...}
.venv/bin/simcore warmup examples/scenarios/toy_queue/mmc_baseline.yaml --experiment-dir runs/mmc_servers
.venv/bin/simcore fit samples.csv --column minutes --unit min --out dist.yaml
```

See [`docs/guides/experiments.md`](docs/guides/experiments.md).

From Python:

```python
from simcore.api import run_scenario
result = run_scenario("examples/scenarios/toy_queue/mmc_baseline.yaml", seed=1)
result.kpis["wait_time_mean"]
```

## Architecture in one paragraph

Three configuration layers — **facility** (`facility.yaml`), **behaviors**
(`behaviors.yaml`), **scenario** (`scenario.yaml`) — are validated into spec
models (`simcore.specs`, JSON Schemas in `schemas/`). The domain-agnostic core
(`simcore.core`) provides the clock, `(time, priority, seq)` scheduler, typed
event bus, entity registry, named RNG streams, task queue, policies and event
log; it runs against an abstract `WorldBackend` (`logical` in pure Python;
`kinematic`/`physics` via Isaac Sim in `simcore.adapters.omniverse`, the only
place allowed to import `omni`/`isaacsim`/`pxr`). Domains (`domains/*`) are
plug-ins discovered through the `simcore.domains` entry point; the core never
imports them.

## Layout

```
src/simcore/   core/ specs/ experiment/ adapters/{logical,omniverse}/ cli/ api/
domains/       toy_queue/ warehouse/ process_network/ packaging_line/
schemas/       generated JSON Schemas (make schemas)
examples/      facilities/ scenarios/ experiments/
tests/         unit/ validation/ integration/
services/ frontend/   (later milestones)
```

## License

Apache-2.0
