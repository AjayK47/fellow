import { api, Json, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty } from "../components";
import { useState } from "react";

type Audit = { events: Json[]; count: number };

export default function Audit() {
  const [type, setType] = useState("");
  const { data, error, loading } = useAsync<Audit>(
    () => api.get("/v1/audit/events", { organization_id: ORG, event_type: type || undefined }),
    [type]
  );
  return (
    <>
      <PageHeader
        title="Audit"
        subtitle="Append-only event log: every route, attention decision, run, tool call, delivery, and admin action."
        actions={
          <select className="select" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">all types</option>
            <option value="event.routed">event.routed</option>
            <option value="attention.evaluated">attention.evaluated</option>
            <option value="agent.run.completed">agent.run.completed</option>
            <option value="agent.run.failed">agent.run.failed</option>
            <option value="agent.delivery">agent.delivery</option>
            <option value="agent.specialist.completed">agent.specialist.completed</option>
            <option value="slack.event.accepted">slack.event.accepted</option>
          </select>
        }
      />
      <Section>
        <Card title="Events" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.events.length === 0 && <Empty>no events</Empty>}
          {data && (
            <table className="table">
              <thead>
                <tr>
                  <th className="whitespace-nowrap">type</th>
                  <th className="whitespace-nowrap">at</th>
                  <th className="whitespace-nowrap">principal</th>
                  <th className="w-full">payload</th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((e, i) => {
                  const eo = e as Record<string, unknown>;
                  return (
                    <tr key={i}>
                      <td>{String(eo.event_type)}</td>
                      <td>{reltime(String(eo.created_at))}</td>
                      <td>{String(eo.principal_id || eo.actor_id || "—")}</td>
                      <td>{fmt(eo.payload || eo, 220)}</td>
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