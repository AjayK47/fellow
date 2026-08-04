import { ReactNode, useState } from "react";
import { api, Json, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, StatePill, Pill } from "../components";
import { useParams, Link } from "react-router-dom";
import { ChevronDown, ChevronUp } from "lucide-react";

type RunDetailResponse = {
  run: Record<string, unknown>; nodes: Json[]; edges: Json[];
  skill_refs?: Json[]; capability_refs?: Json[]; workspace?: Json | null;
  plan?: Json | null; specialist_tasks?: Json[]; handoffs?: Json[]; artifacts?: Json[];
  messages?: Json[]; turns?: Json[]; tool_calls?: Json[];
};

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="border-b border-r border-line p-3.5 last:border-r-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-[13px] font-medium text-ink">{children}</div>
    </div>
  );
}

function MessagesPanel({ messages }: { messages: Record<string, unknown>[] }) {
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

function TurnRow({ turn }: { turn: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const request = (turn.request as Record<string, unknown>) || {};
  const response = (turn.response as Record<string, unknown>) || {};
  const toolCalls = (response.tool_calls as unknown[]) || [];
  const usage = (response.usage as Record<string, unknown>) || {};
  return (
    <div className="border-b border-line last:border-b-0">
      <button
        className="flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="font-mono text-[11px] text-muted">turn {String(turn.turn_number)}</span>
        <span className="text-[12px] text-ink">{fmt(response.content, 90) || "(tool call, no text)"}</span>
        <span className="ml-auto flex items-center gap-2 text-[11px] text-muted">
          {toolCalls.length > 0 && <Pill tone="orange">{toolCalls.length} tool call{toolCalls.length === 1 ? "" : "s"}</Pill>}
          {usage.total_tokens ? `${usage.total_tokens} tok` : ""}
          {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </span>
      </button>
      {open && (
        <div className="grid gap-3 bg-paper px-4 py-3 sm:grid-cols-2">
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Offered</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {((request.tools as string[]) || []).map((t) => <Pill key={t} tone="muted">{t}</Pill>)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Model</div>
            <div className="mt-1 text-[12px] text-ink">{fmt(request.model, 60)} · {String(response.finish_reason ?? "")}</div>
          </div>
          {toolCalls.length > 0 && (
            <div className="sm:col-span-2">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Tool calls</div>
              <pre className="mt-1 max-h-[200px] overflow-auto rounded-lg border border-line bg-panel p-2 font-mono text-[11px]">
                {JSON.stringify(toolCalls, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [tab, setTab] = useState<"messages" | "turns" | "graph">("messages");
  const { data, error, loading } = useAsync<RunDetailResponse>(
    () => api.get(`/v1/agent/runs/${runId}`),
    [runId]
  );
  const run = (data?.run || {}) as Record<string, any>;
  const meta = run.metadata || {};
  const slack = meta.slack || {};
  const usage = run.usage || {};
  const children = data?.nodes?.filter((n) => {
    const no = n as Record<string, unknown>;
    return no.entity_type === "agent_run" && no.id !== run.id;
  }) as Record<string, unknown>[] | undefined;
  const skillRefs = (data?.skill_refs ?? []) as Record<string, unknown>[];
  const capabilityRefs = (data?.capability_refs ?? []) as Record<string, unknown>[];
  const artifacts = (data?.artifacts ?? []) as Record<string, unknown>[];
  const handoffs = (data?.handoffs ?? []) as Record<string, unknown>[];
  const messages = (data?.messages ?? []) as Record<string, unknown>[];
  const turns = (data?.turns ?? []) as Record<string, unknown>[];
  const nodes = (data?.nodes ?? []) as Record<string, unknown>[];
  const edges = (data?.edges ?? []) as Record<string, unknown>[];

  return (
    <>
      <PageHeader
        title={fmt(run.goal, 60) || "Run"}
        subtitle={`id ${run.id} · trace ${run.trace_id} · session ${run.session_id}`}
        actions={<Link to="/runs" className="btn-ghost no-underline">← back</Link>}
      />
      {loading && !data && <Section><Card><Empty>loading…</Empty></Card></Section>}
      {error && <Section><Card><Empty>error: {error}</Empty></Card></Section>}
      {data && (
        <>
          <Section>
            <div className="card grid grid-cols-2 sm:grid-cols-3">
              <Field label="state"><StatePill state={String(run.state)} /></Field>
              <Field label="trigger">{String(run.trigger_kind)}</Field>
              <Field label="principal">{String(run.envelope?.principal_id)}</Field>
              <Field label="parent run">{run.parent_run_id ? (
                <Link to={`/runs/${run.parent_run_id}`} className="text-brand hover:underline">{fmt(run.parent_run_id, 24)}</Link>
              ) : "— main agent"}</Field>
              <Field label="iterations">{`${run.iteration_count} / ${run.max_iterations}`}</Field>
              <Field label="tool calls">{`${run.tool_call_count} / ${run.max_tool_calls}`}</Field>
              <Field label="tokens">{`${usage.total_tokens ?? "—"} total (${usage.prompt_tokens ?? "-"} prompt / ${usage.completion_tokens ?? "-"} completion)`}</Field>
              <Field label="cost">{usage.cost ? `$${Number(usage.cost).toFixed(4)}` : "—"}</Field>
              {meta.specialist_worker && (
                <Field label="specialist">{`role ${meta.worker_role} · depth ${meta.worker_depth}`}</Field>
              )}
              {slack.channel_id && <Field label="slack">{`channel ${slack.channel_id} · thread ${slack.thread_id || "—"}`}</Field>}
              <Field label="created">{fmt(run.created_at, 22)}</Field>
              <Field label="updated">{fmt(run.updated_at, 22)}</Field>
            </div>
          </Section>

          {children && children.length > 0 && (
            <Section>
              <Card title="Specialist workers" meta={String(children.length)}>
                <table className="table">
                  <thead>
                    <tr>
                      <th className="whitespace-nowrap">id</th>
                      <th className="w-full">goal</th>
                      <th className="whitespace-nowrap">state</th>
                    </tr>
                  </thead>
                  <tbody>
                    {children.map((n) => {
                      const attrs = (n.attributes as Record<string, unknown>) || {};
                      return (
                        <tr key={String(n.id)}>
                          <td><Link to={`/runs/${n.id}`} className="text-brand hover:underline">{String(n.id)}</Link></td>
                          <td>{fmt(attrs.final_answer || n.name, 90)}</td>
                          <td>{String(attrs.state)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </Card>
            </Section>
          )}

          <Section>
            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Resolved skills" meta={String(skillRefs.length)}>
                {skillRefs.length === 0 && <Empty>none recorded</Empty>}
                {skillRefs.length > 0 && (
                  <div className="grid gap-1.5 p-4">
                    {skillRefs.map((s, i) => (
                      <div key={i} className="flex items-center justify-between rounded-lg bg-paper px-3 py-2 text-[12px]">
                        <span className="font-medium text-ink">{fmt(s.name, 40)}</span>
                        <span className="text-muted">v{String(s.version)} · {String(s.mode)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
              <Card title="Resolved capabilities" meta={String(capabilityRefs.length)}>
                {capabilityRefs.length === 0 && <Empty>none recorded</Empty>}
                {capabilityRefs.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 p-4">
                    {capabilityRefs.map((c, i) => (
                      <Pill key={i} tone={c.risk === "write" ? "orange" : "muted"}>{String(c.name)}</Pill>
                    ))}
                  </div>
                )}
              </Card>
              <Card title="Workspace" meta={data.workspace ? "active" : "none"}>
                {!data.workspace && <Empty>no workspace for this run</Empty>}
                {data.workspace ? (
                  <div className="p-4 font-mono text-[11px] text-muted">
                    {fmt((data.workspace as Record<string, unknown>).root_path ?? data.workspace, 200)}
                  </div>
                ) : null}
              </Card>
              <Card title="Artifacts and handoffs" meta={`${artifacts.length} · ${handoffs.length}`}>
                {artifacts.length === 0 && handoffs.length === 0 && <Empty>none recorded</Empty>}
                {(artifacts.length > 0 || handoffs.length > 0) && (
                  <div className="grid gap-1.5 p-4">
                    {artifacts.map((a, i) => (
                      <div key={`a${i}`} className="flex items-center justify-between rounded-lg bg-paper px-3 py-2 text-[12px]">
                        <span className="text-ink">{fmt(a.title, 60)}</span>
                        <Pill tone="muted">{String(a.artifact_type)}</Pill>
                      </div>
                    ))}
                    {handoffs.map((h, i) => (
                      <div key={`h${i}`} className="flex items-center justify-between rounded-lg bg-orange-soft px-3 py-2 text-[12px]">
                        <span className="text-ink">{fmt(h.summary, 60)}</span>
                        <Pill tone="orange">{String(h.status)}</Pill>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </div>
          </Section>

          <Section>
            <Card>
              <div className="flex gap-1 border-b border-line px-2 pt-2">
                {(["messages", "turns", "graph"] as const).map((t) => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={`rounded-t-lg px-4 py-2 text-[12px] font-medium capitalize transition-colors ${
                      tab === t ? "bg-brand-soft text-brand" : "text-muted hover:text-ink"
                    }`}
                  >
                    {t} {t === "messages" ? `(${messages.length})` : t === "turns" ? `(${turns.length})` : `(${nodes.length})`}
                  </button>
                ))}
              </div>
              {tab === "messages" && <MessagesPanel messages={messages} />}
              {tab === "turns" && (
                turns.length === 0 ? <Empty>no turns recorded</Empty> : (
                  <div>{turns.map((t, i) => <TurnRow key={i} turn={t} />)}</div>
                )
              )}
              {tab === "graph" && (
                nodes.length === 0 ? <Empty>no related entities</Empty> : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th className="w-[45%]">from</th>
                        <th className="whitespace-nowrap">type</th>
                        <th className="w-[45%]">to</th>
                      </tr>
                    </thead>
                    <tbody>
                      {edges.map((e, i) => (
                        <tr key={i}>
                          <td className="font-mono text-[12px]">{String(e.from_id)}</td>
                          <td><Pill tone="muted">{String(e.edge_type)}</Pill></td>
                          <td className="font-mono text-[12px]">{String(e.to_id)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )
              )}
            </Card>
          </Section>
        </>
      )}
    </>
  );
}
