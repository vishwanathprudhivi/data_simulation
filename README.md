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
| M6 Domain definitions, catalogs, Tier-0 process network | done |
| M7 Service layer (FastAPI, jobs, SSE, cache) | done |
| M8 Semantic layer & charts (DuckDB, Vega-Lite) | done |
| M9 Scenario compiler, Findings & narrative | done |
| M10 Conversational analyst (tools, grounding, golden set) | done |
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

# config-only (Tier 0) domain: packaging line defined entirely in domains/packaging_line/domain.yaml
.venv/bin/simcore domains show packaging_line
.venv/bin/simcore run examples/scenarios/packaging_line/baseline.yaml

# experiments: baseline + variants x replications with CRN, CIs and a report
.venv/bin/simcore experiment examples/experiments/mmc_servers.yaml --out runs --parallel 4
# -> runs/mmc_servers/{report.html,report.md,summary.json,results.parquet,...}
.venv/bin/simcore warmup examples/scenarios/toy_queue/mmc_baseline.yaml --experiment-dir runs/mmc_servers
.venv/bin/simcore fit samples.csv --column minutes --unit min --out dist.yaml
```

See [`docs/guides/experiments.md`](docs/guides/experiments.md).

Service (API at http://127.0.0.1:8000/api/v1/docs):

```bash
make api                         # in-process workers, examples seeded into the library
docker compose up                # API + Redis + CPU worker
```

Natural language -> draft scenario card (diff, assumptions, gaps, estimate); nothing
runs until you submit the returned `submit` body to `/experiments`:

```bash
curl -s localhost:8000/api/v1/compile -H 'content-type: application/json' -d '{
  "text": "add 5 jacks and compare to baseline",
  "scenario": {"example": "scenarios/warehouse/baseline.yaml"}}'
# after the job completes: GET /api/v1/jobs/<id>/findings  (Findings + narrative)
```

Chat with the analyst (same tools and permissions as the API):

```bash
S=$(curl -s localhost:8000/api/v1/chat/sessions -H 'content-type: application/json' \
  -d '{"scenario": {"example": "scenarios/warehouse/baseline.yaml"}}' | jq -r .session_id)
curl -s localhost:8000/api/v1/chat/sessions/$S/messages -H 'content-type: application/json' \
  -d '{"text": "add 5 jacks and compare to baseline"}' | jq -r .reply
# then: "run it", "is it done?", "why did on-time rate change in variant A?", "chart ..."
```

The compiler and analyst use a deterministic rule-based client by default; set
`SIMCORE_REASONING=anthropic` (and `ANTHROPIC_API_KEY`) to compile with Claude.

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
