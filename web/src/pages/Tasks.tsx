import { useState } from "react";
import { api, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, StatePill } from "../components";
import { Link } from "react-router-dom";

type Task = Record<string, any>;
type RunList = { runs: Task[]; count: number };

function workerTone(worker: string): "good" | "muted" | "orange" {
  return worker === "pi" ? "orange" : worker === "session_manager" ? "good" : "muted";
}

export default function Tasks() {
  const [state, setState] = useState("");
  const [incompleteOnly, setIncompleteOnly] = useState(false);
  const [highCost, setHighCost] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useAsync<RunList>(
    () => api.get("/v1/harness/runs", {
      organization_id: ORG,
      state: state || undefined,
      incomplete_only: incompleteOnly ? "true" : undefined,
      high_cost_usd: highCost ? 1 : undefined,
      limit: 200,
    }),
    [state, incompleteOnly, highCost, refresh]
  );

  async function retry(taskId: string) {
    await api.post(`/v1/tasks/${taskId}:retry`, undefined);
    setRefresh((value) => value + 1);
  }

  return (
    <>
      <PageHeader
        title="Tasks"
        subtitle="Every unit of work is a Task. Session Managers own root Tasks; general and Pi workers run as child Tasks."
        actions={<div className="flex flex-wrap items-center gap-2">
          <select className="select" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">all states</option><option value="queued">queued</option><option value="running">running</option><option value="waiting">waiting</option><option value="completed">completed</option><option value="failed">failed</option><option value="cancelled">cancelled</option>
          </select>
          <label className="flex items-center gap-1.5 text-[11px] text-muted"><input type="checkbox" checked={incompleteOnly} onChange={(e) => setIncompleteOnly(e.target.checked)} /> incomplete</label>
          <label className="flex items-center gap-1.5 text-[11px] text-muted"><input type="checkbox" checked={highCost} onChange={(e) => setHighCost(e.target.checked)} /> cost ≥ $1</label>
        </div>}
      />
      <Section>
        <Card title="Logical Task runs" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.runs.length === 0 && <Empty>no matching runs</Empty>}
          {data && data.runs.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">prompt</th>
                  <th className="whitespace-nowrap">state</th>
                  <th className="whitespace-nowrap">model</th>
                  <th className="whitespace-nowrap">cost</th>
                  <th className="whitespace-nowrap">complete</th>
                  <th className="whitespace-nowrap">updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.runs.map((task) => (
                  <tr key={String(task.task_id)}>
                    <td title={String(task.prompt || task.goal)}>
                      <Link to={`/tasks/${task.task_id}`} className="text-ink hover:underline">
                        {fmt(task.prompt || task.goal, 90)}
                      </Link>
                    </td>
                    <td><StatePill state={String(task.state)} /></td>
                    <td className="font-mono text-[11px] text-muted">{fmt((task.providers_models || []).map((item: Task) => `${item.provider}/${item.model}`).join(", "), 38)}</td>
                    <td className="text-[11px]">{task.metrics?.cost?.amount == null ? <span className="text-muted">unavailable</span> : `${task.metrics.cost.amount} ${task.metrics.cost.source || ""}`}</td>
                    <td><Pill tone={task.completeness?.status === "complete" ? "good" : task.completeness?.status === "pending" ? "orange" : "bad"}>{String(task.completeness?.status || "unknown")}</Pill></td>
                    <td className="text-muted">{reltime(String(task.updated_at))}</td>
                    <td>
                      {(task.state === "failed" || task.state === "cancelled") && (
                        <button className="btn-ghost" onClick={() => retry(String(task.task_id))}>Retry</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
    </>
  );
}
