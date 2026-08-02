import { api, Json, fmt, ORG } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, StatePill } from "../components";
import { useParams, Link } from "react-router-dom";
import { useState } from "react";

type GraphView = {
  run: Record<string, unknown>; nodes: Json[]; edges: Json[];
  skill_refs?: Json[]; capability_refs?: Json[]; workspace?: Json | null;
  plan?: Json | null; specialist_tasks?: Json[]; handoffs?: Json[]; artifacts?: Json[];
  messages?: Json[]; turns?: Json[]; tool_calls?: Json[];
};

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [tab, setTab] = useState<"turns" | "messages" | "graph">("turns");
  const { data, error, loading } = useAsync<GraphView>(
    () => api.get(`/v1/agent/runs/${runId}`),
    [runId]
  );
  const run = (data?.run || {}) as any;
  const meta = run.metadata || {};
  const slack = meta.slack || {};
  const usage = run.usage || {};
  const children = data?.nodes?.filter((n: any) => n.entity_type === "agent_run" && n.id !== run.id);
  return (
    <>
      <PageHeader
        title={fmt(run.goal, 60) || "Run"}
        subtitle={`id ${run.id} · trace ${run.trace_id} · session ${run.session_id}`}
        actions={<Link to="/runs" className="btn btn-ghost">← back</Link>}
      />
      <Section>
        <div className="grid grid-cols-2 gap-0 border border-ink">
          <Field label="state"><StatePill state={String(run.state)} /></Field>
          <Field label="trigger">{String(run.trigger_kind)}</Field>
          <Field label="principal">{String(run.envelope?.principal_id)}</Field>
          <Field label="parent run">{run.parent_run_id || "— main agent"}</Field>
          <Field label="iterations">{`${run.iteration_count} / ${run.max_iterations}`}</Field>
          <Field label="tool calls">{`${run.tool_call_count} / ${run.max_tool_calls}`}</Field>
          <Field label="tokens">{`prompt ${usage.prompt_tokens||"-"} · completion ${usage.completion_tokens||"-"} · total ${usage.total_tokens||"-"}`}</Field>
          <Field label="cost">{fmt(usage.cost) || "—"}</Field>
          {(meta.specialist_worker) && (
            <Field label="specialist">
              {`role ${meta.worker_role} · depth ${meta.worker_depth}`}
            </Field>
          )}
          {slack.channel_id && <Field label="slack">{`channel ${slack.channel_id} · thread ${slack.thread_id||"—"}`}</Field>}
          <Field label="created">{fmt(run.created_at, 22)}</Field>
          <Field label="updated">{fmt(run.updated_at, 22)}</Field>
        </div>
      </Section>
      {children && children.length > 0 && (
        <Section>
          <Card title="Specialist workers" meta={String(children.length)}>
            <table className="table">
              <thead><tr><th>id</th><th>goal</th><th>state</th></tr></thead>
              <tbody>
                {children.map((n) => {
                  const nd = n as Record<string, unknown>;
                  return (
                    <tr key={String(nd.id)}>
                      <td><Link to={`/runs/${nd.id}`} className="text-ink hover:underline">{String(nd.id)}</Link></td>
                      <td>{fmt((nd.attributes as any)?.final_answer || nd.name, 90)}</td>
                      <td>{String((nd.attributes as any)?.state)}</td>
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
          <Card title="Resolved skills" meta={String(data?.skill_refs?.length ?? 0)}>
            {(data?.skill_refs?.length ?? 0) === 0 && <Empty>none recorded</Empty>}
            {data?.skill_refs && data.skill_refs.length > 0 && (
              <pre className="max-h-56 overflow-auto p-4 font-mono text-[11px]">{JSON.stringify(data.skill_refs, null, 2)}</pre>
            )}
          </Card>
          <Card title="Resolved capabilities" meta={String(data?.capability_refs?.length ?? 0)}>
            {(data?.capability_refs?.length ?? 0) === 0 && <Empty>none recorded</Empty>}
            {data?.capability_refs && data.capability_refs.length > 0 && (
              <pre className="max-h-56 overflow-auto p-4 font-mono text-[11px]">{JSON.stringify(data.capability_refs, null, 2)}</pre>
            )}
          </Card>
          <Card title="Workspace" meta={data?.workspace ? "active" : "none"}>
            <pre className="max-h-40 overflow-auto p-4 font-mono text-[11px]">{JSON.stringify(data?.workspace || {}, null, 2)}</pre>
          </Card>
          <Card title="Artifacts and handoffs" meta={`${data?.artifacts?.length ?? 0} artifacts · ${data?.handoffs?.length ?? 0} handoffs`}>
            <pre className="max-h-40 overflow-auto p-4 font-mono text-[11px]">{JSON.stringify({ artifacts: data?.artifacts || [], handoffs: data?.handoffs || [] }, null, 2)}</pre>
          </Card>
        </div>
      </Section>
      <Section>
        <div className="flex gap-2 border-b border-ink">
          {(["turns", "messages", "graph"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 font-mono text-[12px] uppercase tracking-wider ${
                tab === t ? "bg-ink text-white" : "text-muted"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="mt-3">
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {tab === "graph" && data && (
            <Card title="Execution graph">
              <pre className="overflow-auto p-4 font-mono text-[11px]">{JSON.stringify({ nodes: data.nodes, edges: data.edges }, null, 2)}</pre>
            </Card>
          )}
        </div>
      </Section>
    </>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-r border-ink p-3 last:border-r-0">
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1.5 font-mono text-[13px]">{children}</div>
    </div>
  );
}
