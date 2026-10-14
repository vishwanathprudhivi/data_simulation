import type { Findings } from "../api";

/** Fidelity, replications, confidence and envelope on every result (FR-UI-12). */
export function TrustBar({ findings }: { findings: Findings }) {
  const extrap = findings.comparisons?.some((c) => c.extrapolated);
  return (
    <div className="trust" data-testid="trust-bar">
      <span className="badge">mode: {findings.fidelity?.screen ?? "logical"}</span>
      {findings.fidelity?.promoted?.length ? (
        <span className="badge">promoted to {findings.fidelity.promoted_mode}</span>
      ) : null}
      <span className="badge">n = {findings.n_replications} replications</span>
      <span className="badge">{Math.round(findings.confidence * 100)}% confidence</span>
      <span className={`badge ${extrap ? "warn" : "ok"}`}>
        {extrap ? "extrapolated (outside envelope)" : "within calibrated envelope"}
      </span>
    </div>
  );
}
