import { Link } from "react-router-dom";
import { MessageSquare } from "lucide-react";
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
  const installations = inst?.installations ?? [];
  return (
    <>
      <PageHeader
        title="Channels"
        subtitle="Docket is proactive by default in every observed Slack channel. Use a channel policy to opt out or enable free response."
      />
      <Section>
        <Card title="Installations" meta={String(installations.length)}>
          {l1 && <Empty>loading…</Empty>}
          {e1 && <Empty>error: {e1}</Empty>}
          {inst && installations.length === 0 && (
            <div className="flex flex-col items-center gap-3 px-4 py-14 text-center">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-brand-soft text-brand">
                <MessageSquare size={20} />
              </span>
              <div>
                <div className="text-[13px] font-medium text-ink">No Slack workspace registered</div>
                <p className="mx-auto mt-1 max-w-[420px] text-[12px] text-muted">
                  If Slack was configured by hand in <code className="rounded bg-paper px-1 py-0.5 font-mono text-[11px]">.env</code> rather
                  than through onboarding, the bot can still be live-connected while Docket has no
                  record of the workspace — org resolution silently falls back to the default
                  organization instead of a real mapping. Connect it properly on the{" "}
                  <Link to="/setup" className="font-medium text-brand hover:underline">Setup</Link> page.
                </p>
              </div>
            </div>
          )}
          {installations.length > 0 && (
            <table className="table">
              <thead><tr><th>workspace</th><th>team id</th><th>bot</th><th>org</th></tr></thead>
              <tbody>
                {installations.map((a) => {
                  const ao = a as Record<string, unknown>;
                  return (
                    <tr key={String(ao.workspace_id)}>
                      <td className="font-medium">{String(ao.workspace_name || ao.workspace_id)}</td>
                      <td className="font-mono text-[12px] text-muted">{String(ao.workspace_id)}</td>
                      <td className="font-mono text-[12px] text-muted">{String(ao.bot_user_id)}</td>
                      <td className="text-muted">{String(ao.organization_id)}</td>
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
          {pols && pols.channel_policies.length === 0 && (
            <Empty>No overrides — every channel stays proactive by default until one is set.</Empty>
          )}
          {pols && pols.channel_policies.length > 0 && (
            <table className="table">
              <thead><tr><th>channel</th><th>enabled</th><th>observe</th><th>free response</th><th>proactivity</th><th>scope</th></tr></thead>
              <tbody>
                {(pols.channel_policies || []).map((p) => {
                  const po = p as Record<string, unknown>;
                  return (
                    <tr key={String(po.channel_id)}>
                      <td className="font-mono text-[12px]">{String(po.channel_id)}</td>
                      <td>{po.enabled ? <Pill tone="good">on</Pill> : <Pill tone="bad">off</Pill>}</td>
                      <td>{po.observe ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{po.free_response ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{po.proactive ? <Pill tone="orange">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td className="text-muted">{fmt(po.scope_id, 40)}</td>
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
