import { api, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, StatePill } from "../components";
import { Link } from "react-router-dom";

type SessionRow = {
  session: Record<string, unknown>;
  run_count: number;
  latest_run: Record<string, unknown> | null;
  message_count: number;
  last_message_preview: Record<string, unknown> | null;
};

type SessionList = { sessions: SessionRow[]; count: number };

export default function Sessions() {
  const { data, error, loading } = useAsync<SessionList>(
    () => api.get("/v1/sessions", { organization_id: ORG, limit: 100 }),
    []
  );
  return (
    <>
      <PageHeader
        title="Sessions"
        subtitle="One conversation key — a Slack thread, DM, or channel — is one session. Every run inside it shares the same transcript; there's no separate 'new session' action, a session starts implicitly the first time someone messages Docket there."
      />
      <Section>
        <Card title="Conversations" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.sessions.length === 0 && <Empty>no sessions yet</Empty>}
          {data && data.sessions.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">preview</th>
                  <th className="whitespace-nowrap">destination</th>
                  <th className="whitespace-nowrap">state</th>
                  <th className="whitespace-nowrap">runs</th>
                  <th className="whitespace-nowrap">messages</th>
                  <th className="whitespace-nowrap">last active</th>
                </tr>
              </thead>
              <tbody>
                {data.sessions.map((row) => {
                  const session = row.session;
                  const destination = (session.destination as Record<string, unknown>) || {};
                  const preview =
                    (row.latest_run?.goal as string) ||
                    (row.last_message_preview?.content as string) ||
                    "(no messages yet)";
                  return (
                    <tr key={String(session.id)}>
                      <td title={preview}>
                        <Link to={`/sessions/${session.id}`} className="text-ink hover:underline">
                          {fmt(preview, 90)}
                        </Link>
                      </td>
                      <td>
                        <Pill tone="muted">{String(destination.type ?? "—")}</Pill>{" "}
                        <span className="font-mono text-[11px] text-muted">{fmt(destination.id, 24)}</span>
                      </td>
                      <td>
                        <StatePill state={String(session.state)} />
                      </td>
                      <td>{row.run_count}</td>
                      <td>{row.message_count}</td>
                      <td>{reltime(String(session.updated_at))}</td>
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
