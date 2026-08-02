import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

export default function Channels() {
  const { data: inst, error: e1, loading: l1 } = useAsync<{ installations: Json[]; count: number }>(
    () => api.get("/v1/slack/installations", { organization_id: ORG }), []
  );
  const { data: pols, error: e2, loading: l2 } = useAsync<{ channel_policies: Json[]; count: number }>(
    () => api.get("/v1/channel-policies", { organization_id: ORG }), []
  );
  return (
    <>
      <PageHeader
        title="Channels"
        subtitle="Docket is proactive by default in every observed Slack channel. Use a channel policy to opt out or enable free response."
      />
      <Section>
        <Card title="Installations" meta={String(inst?.installations?.length ?? 0)}>
          {l1 && <Empty>loading…</Empty>}
          {e1 && <Empty>error: {e1}</Empty>}
          {inst && (
            <table className="table">
              <thead><tr><th>workspace</th><th>team id</th><th>bot</th><th>org</th></tr></thead>
              <tbody>
                {(inst.installations || []).map((a) => {
                  const ao = a as Record<string, unknown>;
                  return (
                    <tr key={String(ao.workspace_id)}>
                      <td>{String(ao.workspace_name || ao.workspace_id)}</td>
                      <td>{String(ao.workspace_id)}</td>
                      <td>{String(ao.bot_user_id)}</td>
                      <td>{String(ao.organization_id)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
      <Section>
        <Card title="Channel policies" meta={String(pols?.count ?? 0)}>
          {l2 && <Empty>loading…</Empty>}
          {e2 && <Empty>error: {e2}</Empty>}
          {pols && (
            <table className="table">
              <thead><tr><th>channel</th><th>enabled</th><th>observe</th><th>free response</th><th>proactivity</th><th>scope</th></tr></thead>
              <tbody>
                {(pols.channel_policies || []).map((p) => {
                  const po = p as Record<string, unknown>;
                  return (
                    <tr key={String(po.channel_id)}>
                      <td>{String(po.channel_id)}</td>
                      <td>{po.enabled ? <Pill tone="good">on</Pill> : <Pill tone="bad">off</Pill>}</td>
                      <td>{po.observe ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{po.free_response ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{po.proactive ? <Pill tone="orange">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{fmt(po.scope_id, 40)}</td>
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
