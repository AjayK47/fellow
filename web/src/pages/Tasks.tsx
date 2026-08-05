import { useState } from "react";
import { api, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, StatePill } from "../components";
import { Link } from "react-router-dom";

type Task = Record<string, any>;
type TaskList = { tasks: Task[]; count: number };

function workerTone(worker: string): "good" | "muted" | "orange" {
  return worker === "pi" ? "orange" : worker === "session_manager" ? "good" : "muted";
}

export default function Tasks() {
  const [state, setState] = useState("");
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useAsync<TaskList>(
    () => api.get("/v1/tasks", { organization_id: ORG, state: state || undefined, limit: 200 }),
    [state, refresh]
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
        actions={
          <select className="select" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">all states</option>
            <option value="queued">queued</option>
            <option value="running">running</option>
            <option value="waiting_for_children">waiting for children</option>
            <option value="waiting_for_approval">approval</option>
            <option value="waiting_for_human">human</option>
            <option value="completed">completed</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
        }
      />
      <Section>
        <Card title="Task tree roots and children" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.tasks.length === 0 && <Empty>no Tasks yet</Empty>}
          {data && data.tasks.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">prompt</th>
                  <th className="whitespace-nowrap">state</th>
                  <th className="whitespace-nowrap">worker</th>
                  <th className="whitespace-nowrap">parent</th>
                  <th className="whitespace-nowrap">updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.tasks.map((task) => (
                  <tr key={String(task.id)}>
                    <td title={String(task.prompt || task.goal)}>
                      <Link to={`/tasks/${task.id}`} className="text-ink hover:underline">
                        {fmt(task.prompt || task.goal, 90)}
                      </Link>
                    </td>
                    <td><StatePill state={String(task.state)} /></td>
                    <td><Pill tone={workerTone(String(task.worker_type))}>{String(task.worker_type)}</Pill></td>
                    <td className="font-mono text-[11px] text-muted">{fmt(task.parent_task_id, 18)}</td>
                    <td className="text-muted">{reltime(String(task.updated_at))}</td>
                    <td>
                      {(task.state === "failed" || task.state === "cancelled") && (
                        <button className="btn-ghost" onClick={() => retry(String(task.id))}>Retry</button>
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
