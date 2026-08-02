import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";
import { useState } from "react";

type Approvals = { approvals: Json[]; count: number };

export default function Approvals() {
  const [status, setStatus] = useState("");
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync<Approvals>(
    () => api.get("/v1/approvals", { organization_id: ORG, status: status || undefined }),
    [status, tick]
  );
  async function decide(id: string, approved: boolean) {
    await api.post(`/v1/approvals/${id}:decide`, { approved, decided_by: "console" });
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Write actions that paused for human approval. Decide here; the run resumes with the exact (or edited) arguments."
        actions={
          <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all</option>
            <option value="pending">pending</option>
            <option value="approved">approved</option>
            <option value="denied">denied</option>
          </select>
        }
      />
      <Section>
        <Card title="Approval queue" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.approvals.length === 0 && <Empty>no approvals</Empty>}
          {data && data.approvals.length > 0 && (
            <table className="table">
              <thead>
                <tr><th>id</th><th>run</th><th>tool</th><th>preview</th><th>status</th><th>action</th></tr>
              </thead>
              <tbody>
                {data.approvals.map((a) => {
                  const ap = a as Record<string, unknown>;
                  const preview = (ap.preview as Record<string, unknown>) || {};
                  return (
                    <tr key={String(ap.id)}>
                      <td>{String(ap.id)}</td>
                      <td>{String(ap.run_id)}</td>
                      <td>{String(preview.action)}</td>
                      <td>{fmt(preview.description, 100)}</td>
                      <td>
                        <Pill tone={ap.status === "approved" ? "good" : ap.status === "denied" ? "bad" : "orange"}>
                          {String(ap.status)}
                        </Pill>
                      </td>
                      <td>
                        {ap.status === "pending" && (
                          <div className="flex gap-2">
                            <button className="btn" onClick={() => decide(String(ap.id), true)}>Approve</button>
                            <button className="btn btn-ghost" onClick={() => decide(String(ap.id), false)}>Deny</button>
                          </div>
                        )}
                      </td>
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