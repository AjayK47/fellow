import { api, Json, ORG, PRINCIPAL, fmt } from "../api";
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
    // decided_by must be a principal actually in the approval's own
    // allowed_approver_ids (server checks this and 403s otherwise) --
    // the literal string "console" never is, so every dashboard decision
    // used to fail closed regardless of who was signed in.
    await api.post(`/v1/approvals/${id}:decide`, { approved, decided_by: PRINCIPAL });
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Write actions that paused for human approval. Decide here; the Task resumes with the exact (or edited) arguments."
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
                <tr>
                  <th className="whitespace-nowrap">id</th>
                  <th className="whitespace-nowrap">task</th>
                  <th className="whitespace-nowrap">tool</th>
                  <th className="w-full">preview</th>
                  <th className="whitespace-nowrap">status</th>
                  <th className="whitespace-nowrap">action</th>
                </tr>
              </thead>
              <tbody>
                {data.approvals.map((a) => {
                  const ap = a as Record<string, unknown>;
                  const preview = (ap.preview as Record<string, unknown>) || {};
                  return (
                    <tr key={String(ap.id)}>
                      <td>{String(ap.id)}</td>
                      <td>{String(ap.task_id)}</td>
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
