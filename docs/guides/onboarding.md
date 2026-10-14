# From SOP to validated baseline: onboarding a domain

This guide takes a new operation from a process description and a few data exports
to a **Validation Report** and a published, calibrated domain, with no Python
(Tier 0). The worked example is the packaging line in
[`examples/onboarding/packaging_line`](../../examples/onboarding/packaging_line);
every command below runs as written from the repository root. Budget: under one
working day once the exports exist (AC-DOM-01 target).

## 0. Pick a tier

| Tier | You write | Use when |
|---|---|---|
| 0 | `domain.yaml` only | the operation is a network of stations, buffers, operators, routings, arrivals, shifts, breakdowns |
| 1 | `domain.yaml` + small hooks | you need a custom dispatch rule, KPI or event |
| 2 | a plug-in | you need custom entity types and movement (see `domains/warehouse`) |

## 1. Scaffold the workspace

```bash
simcore domain init "Packaging line" --tier 0 --dir work/
```

This creates `domain.yaml`, `mapping.yaml`, `scenarios/baseline.yaml`, `data/` and a
`CHECKLIST.md`.

## 2. Describe the process (from the SOP)

Fill in `domain.yaml` from the SOP or a process walk: stations (with nominal cycle
times as a first guess), buffers, which stations need an operator, routings per
product, breakdown behaviour, the **metrics** people report and the **levers** they
want to change. The example's draft is
[`domain.yaml`](../../examples/onboarding/packaging_line/domain.yaml): its service
times are rough triangular guesses; the backtest replaces them with fitted ones.

Metrics, levers and dimensions declared here appear automatically in the API, the
chat assistant and the UI (define once, surface everywhere).

## 3. Map the data

Export what the line records and describe it in `mapping.yaml`: for each file its
role (`kind`), the column for each engine field, the timestamp format and the time
zone of naive timestamps.

| kind | engine fields | feeds |
|---|---|---|
| `station_events` | station, start, end, [server, setup, items] | service-time fits, observed utilization, parallel servers, batch sizes |
| `arrivals` | time, [sku] | interarrival fits and per-period arrival rates |
| `downtime` | station, start, end | MTTR / MTBF fits |
| `observed_kpis` | period, start, end, + lever and KPI columns | the backtest periods |
| `samples` | value | any other distribution |

The example maps four MES exports (`data/station_log.csv`, `tray_releases.csv`,
`downtime.csv`, `shifts.csv`; synthetic, produced by `generate_data.py`). The
`backtest` section names the base scenario, the periods file, which period columns
are lever values (`operators`), where arrival rates go in the scenario and which
KPIs to compare (`line_output` vs "Trays packed").

## 4. Validate definitions and data

```bash
simcore domain validate examples/onboarding/packaging_line
```

You get the **data-quality report**: missing columns (with the available headers),
empty values, unparseable timestamps, ends before starts, stations unknown to the
domain, duplicates, outliers, time-zone assumptions and unit plausibility (a median
service time 60x the domain's default usually means a minutes/seconds mix-up).
Errors must be fixed; warnings are listed in the report.

## 5. Backtest

```bash
simcore domain backtest examples/onboarding/packaging_line --out work/report
```

1. **Auto input modeling** fits service times, repair times and interarrival times
   (MLE, ranked by AIC; the empirical distribution when every parametric fit is
   rejected by KS at 1%) and writes a *draft* `domain.fitted.yaml` with a bumped
   patch version.
2. **Replay**: each observed period is simulated from an empty system with its own
   length, staffing and arrival rates, `replications` times with common random
   numbers.
3. **Compare**: per KPI, mean absolute error, bias and coverage of the observed value
   by the 95% prediction interval; plus a KS test of standardized residuals.
4. **Rate**: PASS when error <= 10% and coverage >= 80%, WARN up to 20% / 50%
   (tolerances are configurable in `mapping.yaml`), FAIL otherwise or when a
   structural check fails.
5. **Envelope**: the observed range of every period lever (here operators 2 to 5)
   becomes the calibrated envelope; requests outside it are flagged "extrapolated"
   in the UI, chat and findings.

`work/report/validation_report.md` is the human-readable report;
`validation_report.json` is the same for tools.

### When it fails

[`examples/onboarding/packaging_line_misspecified`](../../examples/onboarding/packaging_line_misspecified)
is the same line with one SOP misreading (one case packer instead of two, no
batching). Its report fails and says exactly what to change:

```
[fail] line_output: mean absolute error 37%, coverage 0%
[fail] station case_packer: capacity 1 in domain.yaml, 2 parallel servers in the data
    action: set `capacity: 2` for station case_packer in domain.yaml
[fail] station case_packer: batch_size 1 in domain.yaml, 2 items per service in the data
    action: set `batch_size: 2` for station case_packer in domain.yaml
[warn] station case_packer: simulated utilization 99% vs observed 39%
```

Fix, re-run validate and backtest, repeat.

## 6. Review and publish

Read the report and the fitted draft. Publishing is a deliberate human step; it is
refused while the latest report is FAIL or the version already exists:

```bash
simcore domain publish examples/onboarding/packaging_line --fitted \
  --report work/report --to /srv/simcore/domains
export SIMCORE_DOMAIN_PATH=/srv/simcore/domains     # the API and CLI now load it
```

Each published version is kept with its Validation Report
(`<to>/<id>/versions/<version>/`). Scenarios record the domain version they were
built for; a major version change is a breaking change for existing scenarios.

## 7. Facility (when movement matters)

For layouts, export nodes and edges (and optionally zones and locations) as CSV and
build the facility manifest plus a minimal USD scene:

```bash
simcore domain ingest-facility layout/ --id site_a --out facilities/site_a.yaml --usd facilities/site_a.usda
```

## 8. Ask questions

Start the API (`make api`) and the UI (`make frontend`), pick the baseline and ask the
analyst: "what if we run with 4 operators?". Results outside the calibrated envelope
are marked extrapolated; capability gaps are logged for the domain owners
(`GET /api/v1/gaps`).
