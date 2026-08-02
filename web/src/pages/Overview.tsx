import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Json, ORG, fmt, reltime } from "../api";
import { PageHeader, Section, Card, Empty, Pill, StatePill, useAsync } from "../components";

type Dashboard = {
  runs: Json[];
  approvals: Json[];
  attention: Json[];
  interventions: Json[];
  jobs: { counts: Record<string, number>; dead_letters: Json[] };
};

type AuditEvents = { events: Json[]; count: number };

const REFRESH_MS = 8000;
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);

function StatCard({ label, value, tone }: { label: string; value: number; tone?: "bad" | "orange" }) {
  const toneClass = tone === "bad" ? "text-bad" : tone === "orange" ? "text-orange" : "text-ink";
  return (
    <div className="card px-5 py-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">{label}</div>
      <div className={`mt-1 text-[32px] leading-none tracking-tight ${toneClass}`}>{value}</div>
    </div>
  );
}

export default function Overview() {
  const [tick, setTick] = useState(0);
  const [lastRefreshed, setLastRefreshed] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  const { data, error, loading } = useAsync<Dashboard>(
    () => api.get("/v1/harness/dashboard", { organization_id: ORG }),
    [tick]
  );
  const { data: audit } = useAsync<AuditEvents>(
    () => api.get("/v1/audit/events", { organization_id: ORG }),
    [tick]
  );

  useEffect(() => {
    if (data) setLastRefreshed(new Date());
  }, [data]);

  async function decide(id: string, approved: boolean) {
    await api.post(`/v1/approvals/${id}:decide`, { approved, decided_by: "console" });
    setTick((t) => t + 1);
  }

  const runs = (data?.runs ?? []) as Record<string, unknown>[];
  const activeRuns = runs.filter((r) => !TERMINAL_STATES.has(String(r.state)));
  const approvals = (data?.approvals ?? []) as Record<string, unknown>[];
  const pendingApprovals = approvals.filter((a) => a.status === "pending");
  const deadLetters = (data?.jobs.dead_letters ?? []) as Record<string, unknown>[];
  const jobCounts = data?.jobs.counts ?? {};
  const queued = (jobCounts["queued"] ?? 0) + (jobCounts["leased"] ?? 0);
  const recentAudit = ((audit?.events ?? []) as Record<string, unknown>[]).slice(0, 15);

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="What Docket is doing right now — active runs, pending approvals, the job queue, and recent activity in one place."
        actions={
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
            refreshed {reltime(lastRefreshed.toISOString())}
          </span>
        }
      />

      <Section>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Active runs" value={activeRuns.length} />
          <StatCard label="Pending approvals" value={pendingApprovals.length} tone={pendingApprovals.length ? "orange" : undefined} />
          <StatCard label="Jobs queued" value={queued} />
          <StatCard label="Dead letters" value={deadLetters.length} tone={deadLetters.length ? "bad" : undefined} />
        </div>
      </Section>

      {error && (
        <Section>
          <Card><Empty>error: {error}</Empty></Card>
        </Section>
      )}

      <Section>
        <Card title="Active runs" meta={String(activeRuns.length)}>
          {loading && !data && <Empty>loading…</Empty>}
          {data && activeRuns.length === 0 && <Empty>nothing in flight</Empty>}
          {activeRuns.length > 0 && (
            <table className="table">
              <thead>
                <tr><th>run</th><th>goal</th><th>trigger</th><th>state</th><th>updated</th></tr>
              </thead>
              <tbody>
                {activeRuns.slice(0, 10).map((r) => (
                  <tr key={String(r.id)}>
                    <td>
                      <Link to={`/runs/${r.id}`} className="text-ink hover:underline">
                        {String(r.id)}
                      </Link>
                    </td>
                    <td>{fmt(r.goal, 90)}</td>
                    <td>{String(r.trigger_kind)}</td>
                    <td><StatePill state={String(r.state)} /></td>
                    <td>{reltime(String(r.updated_at))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </Section>

      <Section>
        <Card title="Pending approvals" meta={String(pendingApprovals.length)}>
          {data && pendingApprovals.length === 0 && <Empty>nothing waiting</Empty>}
          {pendingApprovals.length > 0 && (
            <table className="table">
              <thead>
                <tr><th>run</th><th>tool</th><th>preview</th><th>action</th></tr>
              </thead>
              <tbody>
                {pendingApprovals.slice(0, 10).map((a) => {
                  const preview = (a.preview as Record<string, unknown>) || {};
                  return (
                    <tr key={String(a.id)}>
                      <td>
                        <Link to={`/runs/${a.run_id}`} className="text-ink hover:underline">
                          {String(a.run_id)}
                        </Link>
                      </td>
                      <td>{String(preview.action)}</td>
                      <td>{fmt(preview.description, 90)}</td>
                      <td>
                        <div className="flex gap-2">
                          <button className="btn" onClick={() => decide(String(a.id), true)}>Approve</button>
                          <button className="btn btn-ghost" onClick={() => decide(String(a.id), false)}>Deny</button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Recent audit events" meta={String(recentAudit.length)}>
          {recentAudit.length === 0 && <Empty>no activity yet</Empty>}
          {recentAudit.length > 0 && (
            <table className="table">
              <thead><tr><th>type</th><th>principal</th><th>when</th></tr></thead>
              <tbody>
                {recentAudit.map((e) => (
                  <tr key={String(e.id)}>
                    <td>{String(e.event_type)}</td>
                    <td>{fmt(e.principal_id, 40)}</td>
                    <td>{reltime(String(e.created_at))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Job queue" meta={queued + deadLetters.length > 0 ? "attention" : "quiet"}>
          <div className="grid grid-cols-2 gap-3 p-4">
            {Object.entries(jobCounts).length === 0 && <Empty>no jobs recorded</Empty>}
            {Object.entries(jobCounts).map(([status, count]) => (
              <div key={status} className="flex items-center justify-between border-b border-line pb-2">
                <span className="font-mono text-[11px] uppercase tracking-wider text-muted">{status}</span>
                <Pill tone={status === "dead_letter" ? "bad" : status === "queued" ? "orange" : "muted"}>
                  {String(count)}
                </Pill>
              </div>
            ))}
          </div>
          {deadLetters.length > 0 && (
            <div className="border-t border-ink px-4 py-3">
              <div className="font-mono text-[10px] uppercase tracking-wider text-bad">
                {deadLetters.length} dead-lettered — needs a look
              </div>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
