import { api, Json, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Jobs = { jobs: Json[]; count: number };

const STATE_TONE: Record<string, "good" | "bad" | "orange" | "muted"> = {
  completed: "good",
  failed: "bad",
  dead: "bad",
  leased: "orange",
  pending: "muted",
  queued: "muted",
};

export default function Jobs() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync<Jobs>(
    () => api.get("/v1/harness/jobs", { organization_id: ORG }), [tick]
  );
  async function drain() {
    await api.post("/v1/harness/worker:drain", {});
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Jobs"
        subtitle="Durable leased job queue with retries, exponential backoff, and dead letters. Drain to process any due jobs immediately."
        actions={<button className="btn" onClick={drain}>drain now</button>}
      />
      <Section>
        <Card title="Queue" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.jobs.length === 0 && <Empty>queue empty</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>kind</th><th>state</th><th>attempts</th><th>payload</th><th>available</th></tr></thead>
              <tbody>
                {data.jobs.map((j) => {
                  const jo = j as Record<string, unknown>;
                  return (
                    <tr key={String(jo.id)}>
                      <td>{String(jo.id)}</td>
                      <td>{String(jo.kind)}</td>
                      <td><Pill tone={STATE_TONE[String(jo.state)] || "muted"}>{String(jo.state)}</Pill></td>
                      <td>{String(jo.attempts)}</td>
                      <td>{fmt(jo.payload, 120)}</td>
                      <td>{reltime(String(jo.available_at))}</td>
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

import { useState } from "react";