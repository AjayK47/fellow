import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, StatePill, Pill } from "../components";
import { Link } from "react-router-dom";
import { useState } from "react";

type RunList = { runs: Json[]; count: number };

export default function Runs() {
  const [state, setState] = useState("");
  const { data, error, loading } = useAsync<RunList>(
    () => api.get("/v1/agent/runs", { organization_id: ORG, state: state || undefined, limit: 50 }),
    [state]
  );
  return (
    <>
      <PageHeader
        title="Runs"
        subtitle="Agent runs across reactive and proactive triggers. Drill in to see the full transcript: turns, tools offered vs. called, tokens, specialists, artifacts."
        actions={
          <select className="select" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">all states</option>
            <option value="completed">completed</option>
            <option value="running">running</option>
            <option value="waiting_for_approval">approval</option>
            <option value="waiting_for_human">human</option>
            <option value="failed">failed</option>
          </select>
        }
      />
      <Section>
        <Card title="Recent runs" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.runs.length === 0 && <Empty>no runs</Empty>}
          {data && data.runs.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">goal</th>
                  <th className="whitespace-nowrap">state</th>
                  <th className="whitespace-nowrap">trigger</th>
                  <th className="whitespace-nowrap">iter</th>
                  <th className="whitespace-nowrap">tools</th>
                  <th className="whitespace-nowrap">specialist</th>
                  <th className="whitespace-nowrap">created</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((r) => {
                  const run = r as Record<string, unknown>;
                  const meta = (run.metadata as Record<string, unknown>) || {};
                  return (
                    <tr key={String(run.id)}>
                      <td title={String(run.goal)}>
                        <Link to={`/runs/${run.id}`} className="text-ink hover:underline">
                          {fmt(run.goal, 90)}
                        </Link>
                      </td>
                      <td>
                        <StatePill state={String(run.state)} />
                      </td>
                      <td>{String(run.trigger_kind)}</td>
                      <td>{String(run.iteration_count)}</td>
                      <td>{String(run.tool_call_count)}</td>
                      <td>
                        {meta.specialist_worker ? (
                          <Pill tone="muted">worker</Pill>
                        ) : (
                          <Pill tone="good">main</Pill>
                        )}
                      </td>
                      <td>{fmt(run.created_at, 22)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
    </>
  );
}