import { ReactNode } from "react";
import { api, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, StatePill } from "../components";
import { useParams, Link } from "react-router-dom";

type SessionDetailResponse = {
  session: Record<string, unknown>;
  runs: Record<string, unknown>[];
  messages: Record<string, unknown>[];
};

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="border-b border-r border-line p-3.5 last:border-r-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-[13px] font-medium text-ink">{children}</div>
    </div>
  );
}

function Transcript({ messages }: { messages: Record<string, unknown>[] }) {
  if (messages.length === 0) return <Empty>no messages recorded</Empty>;
  return (
    <div className="grid gap-2 p-4">
      {messages.map((m) => {
        const role = String(m.role);
        const toolCalls = (m.tool_calls as Record<string, unknown>[]) || [];
        return (
          <div key={String(m.id)} className="rounded-lg border border-line p-3">
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <Pill tone={role === "assistant" ? "good" : role === "tool" ? "orange" : "muted"}>{role}</Pill>
              {m.name ? <span className="font-mono">{String(m.name)}</span> : null}
              {m.run_id ? (
                <Link to={`/runs/${m.run_id}`} className="font-mono text-brand hover:underline">
                  {fmt(m.run_id, 20)}
                </Link>
              ) : null}
              <span className="ml-auto">{reltime(String(m.created_at))}</span>
            </div>
            {m.content ? (
              <p className="mt-2 whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink">{fmt(m.content, 1200)}</p>
            ) : null}
            {toolCalls.length > 0 && (
              <div className="mt-2 grid gap-1">
                {toolCalls.map((call, i) => {
                  const fn = (call.function as Record<string, unknown>) || {};
                  return (
                    <div key={i} className="rounded bg-paper px-2 py-1 font-mono text-[11px] text-ink">
                      call: {String(fn.name)}({fmt(fn.arguments, 140)})
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { data, error, loading } = useAsync<SessionDetailResponse>(
    () => api.get(`/v1/sessions/${sessionId}`),
    [sessionId]
  );
  const session = (data?.session || {}) as Record<string, any>;
  const destination = session.destination || {};
  const runs = (data?.runs ?? []) as Record<string, unknown>[];
  const messages = (data?.messages ?? []) as Record<string, unknown>[];
  const participants = (session.participants as string[]) || [];

  return (
    <>
      <PageHeader
        title={fmt(destination.id, 60) || "Session"}
        subtitle={`id ${session.id} · conversation key ${session.conversation_key}`}
        actions={<Link to="/sessions" className="btn-ghost no-underline">← back</Link>}
      />
      {loading && !data && <Section><Card><Empty>loading…</Empty></Card></Section>}
      {error && <Section><Card><Empty>error: {error}</Empty></Card></Section>}
      {data && (
        <>
          <Section>
            <div className="card grid grid-cols-2 sm:grid-cols-3">
              <Field label="state"><StatePill state={String(session.state)} /></Field>
              <Field label="destination">{`${destination.type ?? "—"} · ${fmt(destination.id, 30)}`}</Field>
              <Field label="participants">{participants.length > 0 ? participants.join(", ") : "—"}</Field>
              <Field label="runs">{runs.length}</Field>
              <Field label="created">{fmt(session.created_at, 22)}</Field>
              <Field label="updated">{fmt(session.updated_at, 22)}</Field>
            </div>
          </Section>

          <Section>
            <Card title="Runs in this session" meta={String(runs.length)}>
              {runs.length === 0 && <Empty>no runs yet</Empty>}
              {runs.length > 0 && (
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-full">goal</th>
                      <th className="whitespace-nowrap">state</th>
                      <th className="whitespace-nowrap">trigger</th>
                      <th className="whitespace-nowrap">created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={String(r.id)}>
                        <td title={String(r.goal)}>
                          <Link to={`/runs/${r.id}`} className="text-ink hover:underline">
                            {fmt(r.goal, 90)}
                          </Link>
                        </td>
                        <td><StatePill state={String(r.state)} /></td>
                        <td>{String(r.trigger_kind)}</td>
                        <td>{fmt(r.created_at, 22)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          </Section>

          <Section>
            <Card title="Transcript" meta={String(messages.length)}>
              <Transcript messages={messages} />
            </Card>
          </Section>
        </>
      )}
    </>
  );
}
