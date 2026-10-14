# ADR-014: Conversational analyst (M10)

- **Status:** Accepted
- **Requirements:** FR-CHAT-01..12, AC-CHAT-01/02/04, AC-VIZ-01 (recovery), AC-SEM-01 (chat)

## Decisions
- **Tools are API calls.** `services.chat.tools` implements the FR-CHAT-02 tool set
  (plus `get_findings`) by calling `/api/v1` in-process through a gateway that
  forwards the caller's `Authorization` and `X-Workspace` headers. The assistant
  therefore has exactly the user's permissions and every tool action is audited like
  any API call. Unknown tools and unexpected arguments are refused.
- **Launching needs a human.** `submit_experiment` re-estimates the run; above the
  auto-approve threshold (or past the session's job budget) it only records a pending
  action, which the user approves with `POST /chat/sessions/{id}/confirm`. The model
  has no tool for confirming, so an injected "launch 1000 runs" cannot start work.
- **Loop.** A manual tool loop (`ChatSession`): all tool results of a step go back in
  one user turn; budgets per session/turn for steps (8), tool calls (16), tokens,
  query rows (5000, results truncated with a flag), jobs (3) and turns.
- **Grounding.** `check_grounding` extracts numbers (ignoring identifiers such as
  `rep_003`, `jacks-40` and list markers) and requires each to match a number in this
  session's tool results or user messages at its written precision, as a percentage,
  or by magnitude. A failed check gets one repair round; numbers still unsupported are
  appended to the reply as a visible warning rather than hidden.
- **Untrusted content.** Tool results are JSON data; text in them that looks like an
  instruction is flagged with `_untrusted_content_notice`. The system prompt states
  that tool results, files and logs are never instructions, and the compile prompt
  escapes the user's text inside its data tag.
- **Deterministic agent.** `HeuristicChatClient` (default `SIMCORE_REASONING=heuristic`)
  routes requests with rules (draft, run, status, findings, why, drill-down, chart,
  save, describe, gaps) and writes replies only from tool results; it also exercises
  the full tool path in tests. With `SIMCORE_REASONING=anthropic` the same session
  runs on Claude with the same tools and checks.
- **Golden set.** `tests/golden/chat_golden.yaml` has 65 requests with expected spec
  diffs (final lever values, sweep variants, goals, gaps, failures) and expected
  QuerySpecs for drill-downs/charts. `python -m services.chat.golden --client SPEC`
  runs it with any client; CI runs it with the heuristic client and with replayed
  recordings when `tests/golden/recordings/` is populated (via `record:<dir>`). A live
  run is marked `live_llm`. Draft replies and Findings narratives must pass the
  grounding check (AC-CHAT-02).
- **Logging.** Each session's prompts, tool calls/results and replies are stored in
  `chat_sessions` after every turn; string values pass through redaction hooks
  (emails and phone numbers by default) before storage (FR-CHAT-11).
- **Not yet:** live recordings (no API key in the build environment), streaming
  replies (NFR-11 first-token target), and running goal-seek from chat.
