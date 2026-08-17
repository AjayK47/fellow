import { ReactNode, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, StatePill } from "../components";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="border-b border-r border-line p-3.5"><div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div><div className="mt-1 text-[13px] font-medium text-ink">{children}</div></div>;
}

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useAsync<Record<string, any>>(
    () => api.get(`/v1/tasks/${taskId}`, { organization_id: ORG }),
    [taskId, refresh]
  );
  const task = data?.task || {};
  const children = (data?.children || []) as Record<string, any>[];
  const attempts = (data?.attempts || []) as Record<string, any>[];
  const plan = (data?.plan || null) as Record<string, any> | null;
  const artifacts = (data?.artifacts || []) as Record<string, any>[];

  async function cancel() {
    await api.post(`/v1/tasks/${taskId}:cancel`, undefined);
    setRefresh((value) => value + 1);
  }

  async function retry() {
    await api.post(`/v1/tasks/${taskId}:retry`, undefined);
    setRefresh((value) => value + 1);
  }

  return (
    <>
      <PageHeader
        title={fmt(task.prompt || task.goal, 70) || "Task"}
        subtitle={`id ${taskId}`}
        actions={<div className="flex gap-2"><Link to="/tasks" className="btn-ghost no-underline">← back</Link>{["queued", "running", "waiting"].includes(String(task.state)) && <button className="btn-ghost" onClick={cancel}>Cancel</button>}{["failed", "cancelled"].includes(String(task.state)) && <button className="btn" onClick={retry}>Retry</button>}</div>}
      />
      {loading && !data && <Section><Card><Empty>loading…</Empty></Card></Section>}
      {error && <Section><Card><Empty>error: {error}</Empty></Card></Section>}
      {data && (
        <>
          <Section><div className="card grid grid-cols-2 sm:grid-cols-4">
            <Field label="state"><StatePill state={String(task.state)} /></Field>
            <Field label="worker"><Pill tone={String(task.worker_type) === "pi" ? "orange" : "muted"}>{String(task.worker_type)}</Pill></Field>
            <Field label="parent">{fmt(task.parent_task_id, 24)}</Field>
            <Field label="root">{fmt(task.root_task_id, 24)}</Field>
            <Field label="attempts">{attempts.length}</Field>
            <Field label="heartbeat">{reltime(String(task.last_heartbeat_at))}</Field>
            <Field label="created">{fmt(task.created_at, 22)}</Field>
            <Field label="updated">{reltime(String(task.updated_at))}</Field>
          </div></Section>
          <Section><Card title="Prompt"><div className="whitespace-pre-wrap px-4 py-4 text-[13px] text-ink">{String(task.prompt || task.goal || "")}</div></Card></Section>
          <Section><Card title="Plan" meta={plan ? String((plan.items || []).length) : "0"}>{!plan ? <Empty>no durable plan</Empty> : <div className="grid gap-2 p-4">{(plan.items || []).map((item: Record<string, any>) => {
            const linkedChildren = children.filter((child) => ((child.metadata?.plan_step_ids || []) as string[]).includes(String(item.id)));
            return <div key={String(item.id)} className="rounded border border-line p-3 text-[12px]"><div className="flex items-center justify-between gap-3"><span className="font-medium text-ink">{String(item.title)}</span><StatePill state={String(item.status)} /></div>{(item.dependencies || []).length > 0 && <div className="mt-1 text-muted">depends on: {(item.dependencies || []).join(", ")}</div>}{item.blocker && <div className="mt-1 text-red-700">blocked: {String(item.blocker)}</div>}{(item.evidence || []).length > 0 && <div className="mt-1 text-muted">evidence: {(item.evidence || []).map((evidence: any) => fmt(evidence, 100)).join(" · ")}</div>}{linkedChildren.length > 0 && <div className="mt-2 flex flex-wrap gap-2">{linkedChildren.map((child) => <Link key={String(child.id)} to={`/tasks/${child.id}`} className="text-link">child {fmt(child.id, 18)}</Link>)}</div>}</div>;
          })}</div>}</Card></Section>
          <Section><Card title="Children" meta={String(children.length)}>{children.length === 0 ? <Empty>no child Tasks</Empty> : <div className="grid gap-1 p-4">{children.map((child) => <Link key={String(child.id)} to={`/tasks/${child.id}`} className="rounded border border-line p-2 text-[12px] hover:bg-paper"><StatePill state={String(child.state)} /> <span className="ml-2">{fmt(child.prompt || child.goal, 100)}</span></Link>)}</div>}</Card></Section>
          <Section><Card title="Artifacts" meta={String(artifacts.length)}>{artifacts.length === 0 ? <Empty>no Task artifacts</Empty> : <div className="grid gap-1 p-4">{artifacts.map((artifact) => <a key={String(artifact.id)} href={String(artifact.download_url || `/v1/artifacts/${artifact.id}/download?organization_id=${ORG}`)} className="rounded border border-line p-2 text-[12px] hover:bg-paper"><span className="font-medium text-ink">{String(artifact.title || artifact.id)}</span><span className="ml-2 text-muted">{String(artifact.artifact_type || "artifact")}</span></a>)}</div>}</Card></Section>
          <Section><Card title="Attempts" meta={String(attempts.length)}>{attempts.length === 0 ? <Empty>no attempts</Empty> : <div className="grid gap-2 p-4">{attempts.map((attempt) => <div key={String(attempt.id)} className="rounded border border-line p-3 text-[12px]"><div className="flex justify-between"><Pill tone={attempt.status === "completed" ? "good" : attempt.status === "running" ? "orange" : "bad"}>{String(attempt.status)}</Pill><span className="text-muted">attempt {String(attempt.attempt_number)}</span></div><div className="mt-2 text-muted">{fmt(attempt.error || attempt.output_tail, 300)}</div></div>)}</div>}</Card></Section>
          <Section><Card title="Result"><div className="whitespace-pre-wrap px-4 py-4 text-[13px] text-ink">{fmt(task.result || task.final_answer || task.error, 4000)}</div></Card></Section>
        </>
      )}
    </>
  );
}
