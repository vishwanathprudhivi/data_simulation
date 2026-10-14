# ADR-016: Domain onboarding kit (M12)

- **Status:** Accepted
- **Requirements:** FR-DOM-04..11, FR-SCG-10, AC-DOM-01/02

## Decisions
- **Declarative mapping.** `mapping.yaml` (`DataMappingSpec`, schema `data_mapping`)
  maps CSV headers to engine fields per source role (station events, arrivals,
  downtime, observed KPIs per period, samples), with strptime formats, IANA time
  zones for naive timestamps and units for duration columns. Files are parsed with
  the standard `csv` module only (size and row limits); nothing in them is evaluated.
- **DQ before modeling.** Mapping produces a data-quality report (errors block the
  gate; warnings and info are reported) including a unit-plausibility check against
  the domain's own default distributions.
- **Fit, then replay history.** Inputs are fitted with the existing MLE/AIC fitter
  (empirical fallback when KS rejects every candidate at 1%, at least 30 samples)
  and written into a draft definition with a bumped patch version. Each observed
  period is a variant (its length, lever values, arrival rates; empty start), run
  with CRN; comparison uses replication means and 95% prediction intervals.
- **Actionable failures over a single score.** Besides KPI error/coverage, the
  backtest compares what the data reveals about structure with the definition:
  parallel servers per station vs `capacity`, items per service vs `batch_size`,
  observed vs simulated utilization per station. These produce specific fixes.
- **Envelope from data.** The calibrated envelope is the observed range of each
  period lever, set only when the backtest does not fail.
- **Publishing is human.** `simcore domain publish` requires a non-failing report for
  the exact version (or the fitted draft), refuses to overwrite a published version,
  and keeps each version with its report on the domain search path.
- **Attachments.** `/attachments` stores an uploaded CSV, proposes a mapping from its
  headers and sample values, and `/attachments/{id}/apply` validates the mapping
  through the same layer, returning scenario overrides (arrival rates) or a fitted
  distribution only when the data-quality report is clean.
- **Facility from CSV.** nodes/edges(/zones/locations) CSVs -> validated manifest;
  a minimal `.usda` (plain text, no USD library) gives a starting 3D scene.
- **Not yet:** AI-assisted drafting of `domain.yaml` from SOPs (FR-DOM-09, S), chat
  tools for attachments, backtests of Tier 1/2 domains with fitted inputs (they run
  with the installed plug-in's own inputs).
