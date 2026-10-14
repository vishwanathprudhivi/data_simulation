# ADR-013: Scenario compiler, Findings and the reasoning-client boundary (M9)

- **Status:** Accepted
- **Requirements:** FR-SCG-02..07, FR-SCG-09, FR-SCG-11, FR-CHAT-04, FR-CHAT-09,
  AC-SCG-01/02, AC-CHAT-03/05

## Decisions
- **Split the compile step in two.** The reasoning client produces only a
  `ScenarioEdit` (schema `scenario_edit`): lever ids + `set|add|multiply` changes,
  named variants, a sweep, or a goal, plus assumptions, ambiguities and unsupported
  requests. `simcore.compiler.compile_edit` is deterministic and does everything else:
  catalog validation, relative-to-absolute resolution, the diff, envelope flags, the
  `ExperimentSpec` (explicit design, CRN, baseline included, default 10 replications
  stated as an assumption). Compile never runs anything; the card carries the
  estimate and a confirmation token for the existing gate (FR-SCG-07).
- **Repair loop.** Pydantic or catalog errors go back to the client as
  `<validation_errors>` for at most 2 repairs (3 attempts), then the compile fails
  with the issues listed. Catalog bounds always apply, whatever the text says.
- **Prompt-injection posture.** User text, catalogs and baseline values are wrapped
  in data tags (`<request>` is escaped so it cannot close its tag); the system prompt
  says the tagged content is data. The client never sees scenario files or results.
- **Clients.** `services.chat.llm`: `AnthropicReasoningClient` (official SDK,
  `claude-opus-5-5`, structured outputs via `output_config.format` with the SDK's
  `transform_schema`, server-side refusal fallbacks, refusal -> `ReasoningRefusal`),
  `RecordingReasoningClient` / `ReplayReasoningClient` keyed by a request hash, and
  `HeuristicCompilerClient`, a deterministic grammar over catalog ids, display names
  and the new `synonyms` field. The service default is `heuristic` so the product
  works with no model configured; `SIMCORE_REASONING=anthropic` switches to Claude.
  Tests use mock/heuristic/replay clients only; no live recordings are committed yet
  (no API key in the build environment).
- **Capability gaps.** Unsupported requests are not errors: they are listed on the
  card with the nearest supported lever and logged to `capability_gaps` with the
  utterance and domain version (FR-SCG-03, FR-DOM-12).
- **Envelope.** `envelope_check` runs on each variant's full lever values (and the
  baseline). `/jobs/{id}/findings` recomputes the flags from the variants' overrides,
  so jobs submitted without the compiler are flagged too.
- **Findings + narrative.** `synthesize_findings` reads `summary.json` only.
  Comparisons carry `ref_id = f_<hash(experiment, variant, metric, kind)>` and their
  fidelity mode; drivers are non-headline metrics ranked by |paired mean| / CI half
  width (> 1 only). The narrative is a template with `{{ref:field}}` placeholders
  rendered from Findings values: "improves/worsens" only when the CI excludes zero
  and the metric has a direction, otherwise "no statistically significant change";
  replication count, confidence and mode are always stated; extrapolated variants and
  caveats are spelled out.
- **Goal seeking.** Bisection over sorted candidates assuming monotonicity; a
  candidate meets the target only if its whole CI does; hard cap on replications;
  every evaluation is in the trace; the Evaluator cache makes re-checks free.
- **Tiered fidelity.** After screening, the top-K by `rank_metric`/`rank_direction`
  (plus explicit `promote` ids) and the reference are re-run in `promoted_mode` with
  the same CRN seeds; results are kept separately under `summary.fidelity` and each
  Findings comparison is labeled with its mode. An unavailable backend (no Isaac)
  produces a warning, never a silent fallback.
