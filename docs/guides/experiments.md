# Experiment guide

An experiment runs a base scenario plus alternatives, each for N replications,
and reports KPI means with confidence intervals and comparisons against the
baseline.

## 1. Write `experiment.yaml`

```yaml
schema_version: "1.0"
id: more_servers
base_scenario: ../scenarios/toy_queue/mmc_baseline.yaml   # relative to this file
variants:                       # named alternatives (scenario override paths)
  - id: four_servers
    overrides: {fleet.server.count: 4}
factors:                        # optional: full-factorial grid
  - {name: servers, path: fleet.server.count, levels: [5, 6]}
replications: 10
master_seed: 2024
crn: true                       # common random numbers (keep this on to compare)
confidence: 0.95
kpis: [wait_time_mean, utilization, throughput]   # empty = all
# warmup: 5 min                 # overrides the scenario's warm-up
# run_length: 8 h               # overrides the scenario's duration
# adaptive: {metric: wait_time_mean, target_half_width: 0.05, max_replications: 50}
```

Override paths address scenario fields; list items are picked by their key:
`fleet.<agent_type>.count`, `demand.<stream>.interarrival.mean`,
`disruptions.<id>.at`, `params.<name>`.

## 2. Run it

```bash
simcore validate examples/experiments/mmc_servers.yaml
simcore experiment examples/experiments/mmc_servers.yaml --out runs --parallel 4
open runs/mmc_servers/report.html
```

## 3. Check the warm-up

```bash
simcore warmup examples/scenarios/toy_queue/mmc_baseline.yaml --bucket 50 \
    --replications 10 --experiment-dir runs/mmc_servers
```

This adds a Welch plot and an MSER suggestion to the report. If the suggestion is
well above the configured warm-up, raise `warmup` and re-run.

## 4. Read the results

- A difference is only significant when its interval **excludes zero**.
- With CRN the comparison is paired; the report also shows the unpaired width on
  the same data so you can see how much CRN helped.
- Fewer than 5 successful replications, failures, or zero variance are flagged
  as warnings in the report.

## 5. Fit input distributions from data

```bash
simcore fit service_times.csv --column minutes --unit min --out service.yaml
```

Paste the resulting spec into `behaviors.yaml` (`service_times`) or a scenario's
demand (`interarrival`).

## From Python

```python
from simcore.api import Evaluator, run_experiment
result = run_experiment("examples/experiments/mmc_servers.yaml", parallelism=4)
ev = Evaluator("examples/scenarios/toy_queue/mmc_baseline.yaml", parallelism=4)
ev.evaluate({"fleet.server.count": 4}, replications=10, seed=1).kpis["wait_time_mean"]
```
