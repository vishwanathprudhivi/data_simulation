# ADR-018: Stretch items (M14)

- **Status:** Accepted
- **Requirements:** FR-SCG-05/06, FR-POL-03/04, FR-DOM-09, FR-UI-09 (PPTX)

## Decisions
- **Goal seeking as a bounded sweep.** A compiled goal becomes an ordinary CRN
  experiment over evenly spaced candidate values, capped so that variants x
  replications stays within `max_runs`. `ExperimentSpec.goal` makes the runner add a
  `goal` section to `summary.json` (per-value CIs, which values meet the target for
  their whole CI, the extreme one), surfaced in `Findings.goal` and the narrative with
  a caveat that only the listed values were evaluated. This keeps goals inside the
  confirmation gate, job service, cache and UI. The bisection search
  (`simcore.experiment.goalseek`) remains for the Python API.
- **Learned policy loader.** `learned` scores (agent, task) pairs with a JSON linear
  model over a fixed, versioned feature set (`FEATURES`) or a `module:attr` callable
  and assigns greedily, so trained policies obey the same contract as built-ins and
  can be A/B-tested under CRN. `cross_entropy_search` trains such weights through the
  Evaluator (seeded, traced) and writes a model the policy loads.
- **Constrained online LLM policy.** `llm_dispatch` offers the reasoning client only
  the top-K candidate pairs as an enum-constrained structured output. Off-menu
  answers, errors, a disabled client or an exhausted `max_calls` budget fall back to
  `nearest_task`; the run report counts calls, acceptances and fallbacks. The engine
  hands its `ReasoningClient` to policies that declare `uses_reasoning`; with the
  default disabled client the policy is exactly `nearest_task`. Reproducible only with
  deterministic (mock/replay) clients.
- **AI-assisted onboarding.** `draft_domain` asks the reasoning client for a
  definition + questions (validated, one repair) and otherwise reads SOP-style step
  lists deterministically. Every guess becomes an open question; the draft is written
  for review (`simcore domain draft`, `POST /domains/draft`) and never published.
- **PPTX.** `GET /jobs/{id}/report?format=pptx` builds slides from `Findings` only
  (title, findings table, difference chart via vl-convert, narrative and caveats) with
  the trust indicators on every slide; optional extra `report` (python-pptx).
- **Not done:** `physics` mode (needs Isaac Sim + GPU), RL training against a live
  environment API beyond black-box search, ONNX policy loading.
