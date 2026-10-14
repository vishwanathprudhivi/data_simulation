import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { ChatTurn, CompileOut, RenderOut, ToolCall } from "../api";
import { useStore } from "../store";
import { FindingsCard } from "./Comparison";
import { JobProgress } from "./RunMonitor";
import { VegaChart } from "./VegaChart";

/** Right-hand chat panel with inline cards (FR-UI-02). */
export function ChatPanel() {
  const s = useStore();
  const [session, setSession] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setSession(null);
    setTurns([]);
    if (!s.scenarioRef) return;
    api
      .post<{ session_id: string }>("/chat/sessions", { scenario: s.scenarioRef })
      .then((r) => setSession(r.session_id))
      .catch((e) => setError(String(e)));
  }, [s.scenarioRef]);

  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [turns, busy]);

  const send = async (msg: string) => {
    if (!session || !msg.trim()) return;
    setBusy(true);
    setError(null);
    setText("");
    try {
      const t = await api.post<ChatTurn>(`/chat/sessions/${session}/messages`, { text: msg });
      setTurns((x) => [...x, t]);
      if (t.state.job_id) s.setJobId(t.state.job_id);
      s.refreshJobs();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (action: string) => {
    const r = await api.post<{ job: { id: string } }>(`/chat/sessions/${session}/confirm`, { action_id: action });
    s.setJobId(r.job.id);
    s.refreshJobs();
    setTurns((x) => [...x, { user: `confirm ${action}`, reply: `Launched job ${r.job.id}.`, tool_calls: [], grounding: { ok: true, unreferenced: [] }, pending_actions: [], state: { job_id: r.job.id, variants: [], last_chart: null } }]);
  };

  return (
    <aside className="chat" data-testid="chat">
      <h2>Analyst</h2>
      <div className="messages">
        {!s.scenario && <p className="muted">Pick a scenario to start a conversation.</p>}
        {turns.map((t, i) => (
          <div key={i} className="turn">
            <div className="msg user">{t.user}</div>
            {t.tool_calls.map((c, j) => (
              <Card key={j} call={c} onConfirm={confirm} pending={t.pending_actions} />
            ))}
            <div className="msg assistant" data-testid="assistant-reply">
              {t.reply.split("\n").map((l, k) => (
                <p key={k}>{l}</p>
              ))}
              {!t.grounding.ok && <span className="badge warn">unverified numbers</span>}
            </div>
          </div>
        ))}
        {busy && <div className="msg assistant muted">…</div>}
        <div ref={end} />
      </div>
      {error && <p className="error">{error}</p>}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send(text);
        }}
      >
        <input
          data-testid="chat-input"
          value={text}
          disabled={!session || busy}
          placeholder={session ? "Ask a what-if, e.g. add 5 jacks and compare to baseline" : ""}
          onChange={(e) => setText(e.target.value)}
        />
        <button data-testid="chat-send" disabled={!session || busy}>
          Send
        </button>
      </form>
    </aside>
  );
}

function Card({ call, onConfirm, pending }: { call: ToolCall; onConfirm(a: string): void; pending: string[] }) {
  if (call.is_error || !call.output) return null;
  switch (call.name) {
    case "compile_scenario":
      return <DraftCard out={call.output as CompileOut} />;
    case "submit_experiment": {
      const o = call.output;
      if (o.status === "pending_confirmation")
        return (
          <div className="card" data-testid="pending-card">
            Needs confirmation ({o.reason}).{" "}
            {pending.includes(o.action_id) && <button onClick={() => onConfirm(o.action_id)}>Confirm run</button>}
          </div>
        );
      return <JobProgress jobId={o.job_id} compact />;
    }
    case "render_chart":
      return <ChartCard out={call.output as RenderOut} chart={call.input.chart as Record<string, any>} />;
    case "get_findings":
      return <FindingsCard data={call.output} />;
    default:
      return null;
  }
}

function DraftCard({ out }: { out: CompileOut }) {
  const s = useStore();
  if (!out.ok || !out.card || !out.card.variants.length) return null;
  const v = out.card.variants[0];
  const reps = Number(out.card.experiment?.replications ?? 5);
  const edit = () => {
    s.setDraft({ levers: { ...v.levers }, replications: reps, runLength: null, source: "chat" });
    s.setView("editor");
  };
  return (
    <div className="card draft" data-testid="draft-card">
      <b>Draft scenario</b> <span className="muted">(not run)</span>
      <ul>
        {out.card.variants.map((x) => (
          <li key={x.id}>
            {x.id}: {x.diff.map((d) => d.text).join("; ") || "no change"}
            {x.extrapolated && <span className="badge warn">extrapolated</span>}
          </li>
        ))}
      </ul>
      {out.confirmation && (
        <p className="muted">
          {out.confirmation.estimate.runs} runs · ~{out.confirmation.estimate.minutes.toFixed(1)} min · {out.confirmation.fidelity}
        </p>
      )}
      {out.card.variants.length === 1 && (
        <button data-testid="edit-draft" onClick={edit}>
          Edit in form
        </button>
      )}
    </div>
  );
}

function ChartCard({ out, chart }: { out: RenderOut; chart: Record<string, any> }) {
  const s = useStore();
  return (
    <div className="card" data-testid="chart-card">
      <VegaChart spec={out.vega_lite} />
      <button
        data-testid="pin-chat-chart"
        onClick={() =>
          s.pin({ ref_id: out.ref_id, job_id: String(out.query.provenance.job_id), chart, vega: out.vega_lite, title: chart.title || out.ref_id, note: "" })
        }
      >
        Pin to canvas
      </button>
    </div>
  );
}
