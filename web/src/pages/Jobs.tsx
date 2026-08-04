import { useState } from "react";
import { api, Json, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Jobs = { jobs: Json[]; count: number };

const STATUS_TONE: Record<string, "good" | "bad" | "orange" | "muted"> = {
  completed: "good",
  dead_letter: "bad",
  failed: "bad",
  leased: "orange",
  queued: "muted",
};

export default function Jobs() {
  const [tick, setTick] = useState(0);
  const [status, setStatus] = useState("");
  const { data, error, loading } = useAsync<Jobs>(
    () => api.get("/v1/harness/jobs", { organization_id: ORG, status: status || undefined }), [tick, status]
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
        actions={
          <div className="flex items-center gap-2">
            <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all statuses</option>
              <option value="queued">queued</option>
              <option value="leased">leased</option>
              <option value="completed">completed</option>
              <option value="dead_letter">dead letter</option>
            </select>
            <button className="btn" onClick={drain}>Drain now</button>
          </div>
        }
      />
      <Section>
        <Card title="Queue" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.jobs.length === 0 && <Empty>queue empty</Empty>}
          {data && data.jobs.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>id</th>
                  <th className="whitespace-nowrap">kind</th>
                  <th className="whitespace-nowrap">status</th>
                  <th className="whitespace-nowrap">attempts</th>
                  <th className="w-full">payload</th>
                  <th className="whitespace-nowrap">available</th>
                </tr>
              </thead>
              <tbody>
                {data.jobs.map((j) => {
                  const jo = j as Record<string, unknown>;
                  return (
                    <tr key={String(jo.id)}>
                      <td className="font-mono text-[12px]">{String(jo.id)}</td>
                      <td className="font-medium">{String(jo.kind)}</td>
                      <td><Pill tone={STATUS_TONE[String(jo.status)] || "muted"}>{String(jo.status)}</Pill></td>
                      <td className="text-muted">{String(jo.attempts)} / {String(jo.max_attempts)}</td>
                      <td className="text-muted">{fmt(jo.payload, 120)}</td>
                      <td className="text-muted">{reltime(String(jo.available_at))}</td>
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
