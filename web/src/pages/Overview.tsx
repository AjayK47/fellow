import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Bell,
  CheckCircle2,
  ListChecks,
  MessageSquareText,
  Plug,
  Wrench,
  X,
} from "lucide-react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, Json, ORG, fmt, reltime } from "../api";
import {
  PageHeader,
  Section,
  Card,
  Empty,
  Pill,
  StatCard,
  StatePill,
  useAsync,
  useVisibleInterval,
} from "../components";

type Dashboard = {
  runs: Json[];
  approvals: Json[];
  attention: Json[];
  interventions: Json[];
  jobs: { counts: Record<string, number>; dead_letters: Json[] };
};

type AuditEvents = { events: Json[]; count: number };
type ConnectorAccounts = { accounts: Json[] };

const REFRESH_MS = 20_000;
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
const DAYS = 9;

function dayKey(iso: unknown): string {
  const d = new Date(String(iso ?? ""));
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString().slice(0, 10);
}

function buildSeries(runs: Record<string, unknown>[], approvals: Record<string, unknown>[]) {
  const days: { key: string; label: string }[] = [];
  const today = new Date();
  for (let i = DAYS - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    days.push({ key, label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) });
  }
  const runCounts = new Map<string, number>();
  for (const r of runs) runCounts.set(dayKey(r.created_at), (runCounts.get(dayKey(r.created_at)) ?? 0) + 1);
  const approvalCounts = new Map<string, number>();
  for (const a of approvals)
    approvalCounts.set(dayKey(a.created_at), (approvalCounts.get(dayKey(a.created_at)) ?? 0) + 1);
  return days.map((d) => ({
    label: d.label,
    runs: runCounts.get(d.key) ?? 0,
    approvals: approvalCounts.get(d.key) ?? 0,
  }));
}

function eventTone(eventType: string): { icon: typeof Activity; tone: "good" | "bad" | "orange" | "muted" } {
  if (eventType.includes("failed") || eventType.includes("denied")) return { icon: AlertTriangle, tone: "bad" };
  if (eventType.includes("completed") || eventType.includes("approved")) return { icon: CheckCircle2, tone: "good" };
  if (eventType.includes("approval") || eventType.includes("waiting")) return { icon: Bell, tone: "orange" };
  if (eventType.includes("tool")) return { icon: Wrench, tone: "muted" };
  return { icon: MessageSquareText, tone: "muted" };
}

const TONE_BUBBLE: Record<string, string> = {
  good: "bg-good-soft text-good",
  bad: "bg-bad-soft text-bad",
  orange: "bg-orange-soft text-orange",
  muted: "bg-line/50 text-muted",
};

export default function Overview() {
  const [tick, setTick] = useState(0);
  const [lastRefreshed, setLastRefreshed] = useState(() => new Date());
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  useVisibleInterval(() => setTick((t) => t + 1), REFRESH_MS);

  const { data, error, loading } = useAsync<Dashboard>(
    () => api.get("/v1/harness/dashboard", { organization_id: ORG }),
    [tick]
  );
  const { data: audit } = useAsync<AuditEvents>(
    () => api.get("/v1/audit/events", { organization_id: ORG }),
    [tick]
  );
  const { data: connectors } = useAsync<ConnectorAccounts>(
    () => api.get("/v1/connector-accounts", { organization_id: ORG }),
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
  const approvalsAll = (data?.approvals ?? []) as Record<string, unknown>[];
  const pendingApprovals = approvalsAll.filter((a) => a.status === "pending");
  const deadLetters = (data?.jobs.dead_letters ?? []) as Record<string, unknown>[];
  const jobCounts = data?.jobs.counts ?? {};
  const queued = (jobCounts["queued"] ?? 0) + (jobCounts["leased"] ?? 0);
  const recentAudit = ((audit?.events ?? []) as Record<string, unknown>[])
    .filter((e) => !dismissed.has(String(e.id)))
    .slice(0, 8);
  const series = useMemo(() => buildSeries(runs, approvalsAll), [runs, approvalsAll]);
  const activeConnections = (connectors?.accounts ?? []).filter(
    (a) => (a as Record<string, unknown>).status === "active"
  ).length;

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="What Docket is doing right now — active runs, pending approvals, and recent activity."
        actions={
          <span className="text-[11px] font-medium text-muted">
            Refreshed {reltime(lastRefreshed.toISOString())}
          </span>
        }
      />

      {data && activeConnections === 0 && (
        <Section>
          <div className="flex items-center justify-between gap-4 rounded-xl2 bg-gradient-to-r from-brand-dark to-brand px-6 py-4 text-white shadow-card">
            <div className="flex items-center gap-3">
              <span className="flex h-9 w-9 flex-none items-center justify-center rounded-full bg-white/15">
                <Plug size={17} />
              </span>
              <div>
                <div className="text-[14px] font-semibold">Give Docket something to work with</div>
                <div className="mt-0.5 text-[12px] text-white/80">
                  Connect Slack or a Composio integration so retrieval and tools have real company context.
                </div>
              </div>
            </div>
            <Link
              to="/connections"
              className="flex-none rounded-lg bg-white px-3.5 py-2 text-[13px] font-medium text-brand-dark no-underline transition-opacity hover:opacity-90"
            >
              Connect an integration
            </Link>
          </div>
        </Section>
      )}

      {error && (
        <Section>
          <Card><Empty>error: {error}</Empty></Card>
        </Section>
      )}

      <Section>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            icon={<Activity size={17} />}
            label="Active runs"
            value={activeRuns.length}
            delta="in flight right now"
            deltaTone="muted"
          />
          <StatCard
            icon={<CheckCircle2 size={17} />}
            label="Pending approvals"
            value={pendingApprovals.length}
            delta={pendingApprovals.length ? "waiting on a decision" : "nothing waiting"}
            deltaTone={pendingApprovals.length ? "bad" : "muted"}
            tone={pendingApprovals.length ? "orange" : "brand"}
          />
          <StatCard
            icon={<ListChecks size={17} />}
            label="Jobs queued"
            value={queued}
            delta="in the durable queue"
            deltaTone="muted"
          />
          <StatCard
            icon={<AlertTriangle size={17} />}
            label="Dead letters"
            value={deadLetters.length}
            delta={deadLetters.length ? "needs a look" : "queue is healthy"}
            deltaTone={deadLetters.length ? "bad" : "good"}
            tone={deadLetters.length ? "bad" : "brand"}
          />
        </div>
      </Section>

      <div className="mt-5 grid gap-5 xl:grid-cols-[1fr_360px]">
        <div className="grid gap-5">
          <Card title="Activity overview" meta={`last ${DAYS} days`}>
            <div className="flex items-center gap-4 border-b border-line px-5 py-3 text-[11px] font-medium text-muted">
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-brand" /> Runs started
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-acid" /> Approvals requested
              </span>
            </div>
            <div className="h-[280px] p-4">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="#e5e8e1" vertical={false} />
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 11, fill: "#767d72" }}
                    axisLine={{ stroke: "#e5e8e1" }}
                    tickLine={false}
                  />
                  <YAxis
                    allowDecimals={false}
                    tick={{ fontSize: 11, fill: "#767d72" }}
                    axisLine={false}
                    tickLine={false}
                    width={28}
                  />
                  <Tooltip
                    contentStyle={{
                      borderRadius: 12,
                      border: "1px solid #e5e8e1",
                      fontSize: 12,
                      boxShadow: "0 8px 24px -8px rgba(20,26,21,0.2)",
                    }}
                  />
                  <Bar dataKey="runs" name="Runs" fill="#1c7a4d" radius={[6, 6, 0, 0]} barSize={22} />
                  <Line
                    type="monotone"
                    dataKey="approvals"
                    name="Approvals"
                    stroke="#7a9a17"
                    strokeWidth={2.5}
                    dot={{ r: 3, fill: "#7a9a17" }}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </Card>

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
                        <Link to={`/runs/${r.id}`} className="font-medium text-ink hover:text-brand hover:underline">
                          {String(r.id)}
                        </Link>
                      </td>
                      <td>{fmt(r.goal, 90)}</td>
                      <td>{String(r.trigger_kind)}</td>
                      <td><StatePill state={String(r.state)} /></td>
                      <td className="text-muted">{reltime(String(r.updated_at))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          {pendingApprovals.length > 0 && (
            <Card title="Pending approvals" meta={String(pendingApprovals.length)}>
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
                          <Link to={`/runs/${a.run_id}`} className="font-medium text-ink hover:text-brand hover:underline">
                            {String(a.run_id)}
                          </Link>
                        </td>
                        <td>{String(preview.action)}</td>
                        <td>{fmt(preview.description, 90)}</td>
                        <td>
                          <div className="flex gap-2">
                            <button className="btn" onClick={() => decide(String(a.id), true)}>Approve</button>
                            <button className="btn-ghost" onClick={() => decide(String(a.id), false)}>Deny</button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Card>
          )}
        </div>

        <div className="grid gap-5 self-start">
          <Card title="Notification centre" meta={String(recentAudit.length)}>
            <div className="grid gap-1 p-2">
              {recentAudit.length === 0 && <Empty>no activity yet</Empty>}
              {recentAudit.map((e) => {
                const { icon: Icon, tone } = eventTone(String(e.event_type ?? ""));
                return (
                  <div
                    key={String(e.id)}
                    className="group flex items-start gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-paper"
                  >
                    <span className={`mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full ${TONE_BUBBLE[tone]}`}>
                      <Icon size={14} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[12.5px] font-medium text-ink">
                        {fmt(e.event_type, 60)}
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted">
                        {fmt(e.principal_id, 30)} · {reltime(String(e.created_at))}
                      </div>
                    </div>
                    <button
                      className="mt-0.5 flex-none cursor-pointer rounded-md border-none bg-transparent p-1 text-muted opacity-0 transition-opacity hover:text-ink group-hover:opacity-100"
                      aria-label="Dismiss"
                      onClick={() => setDismissed((prev) => new Set(prev).add(String(e.id)))}
                    >
                      <X size={13} />
                    </button>
                  </div>
                );
              })}
            </div>
            <div className="border-t border-line p-2">
              <Link
                to="/audit"
                className="block rounded-lg px-3 py-2 text-center text-[12px] font-medium text-brand no-underline hover:bg-brand-soft"
              >
                View all
              </Link>
            </div>
          </Card>

          <Card title="Job queue" meta={queued + deadLetters.length > 0 ? "needs attention" : "quiet"}>
            <div className="grid grid-cols-2 gap-3 p-4">
              {Object.entries(jobCounts).length === 0 && <Empty>no jobs recorded</Empty>}
              {Object.entries(jobCounts).map(([status, count]) => (
                <div key={status} className="flex items-center justify-between border-b border-line pb-2">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-muted">{status}</span>
                  <Pill tone={status === "dead_letter" ? "bad" : status === "queued" ? "orange" : "muted"}>
                    {String(count)}
                  </Pill>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
