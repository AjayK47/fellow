import { api, Json, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Status = {
  environment_file?: string;
  model?: Record<string, unknown>;
  embedding?: Record<string, unknown>;
  slack?: Record<string, unknown>;
  tools?: Record<string, unknown>;
  organizations?: Json[];
};

export default function Setup() {
  const { data, error, loading } = useAsync<Status>(() => api.get("/v1/gateway/setup/status"), []);
  const slack = data?.slack || {};
  const model = data?.model || {};
  const embedding = data?.embedding || {};
  const tools = data?.tools || {};
  return (
    <>
      <PageHeader
        title="Setup"
        subtitle="Local gateway onboarding: inference provider, Slack app, web research tools. Writes only the local ignored .env."
      />
      <Section>
        <Card title="Chat model">
          <StatusGrid rows={[
            ["configured", model.configured ? <Pill tone="good">yes</Pill> : <Pill tone="muted">no</Pill>],
            ["model provider", String(model.model_provider)],
            ["chat model", String(model.chat_model)],
          ]} />
        </Card>
      </Section>
      <Section>
        <Card title="Embeddings">
          <StatusGrid rows={[
            ["configured", embedding.configured ? <Pill tone="good">yes</Pill> : <Pill tone="muted">no</Pill>],
            ["embedding model", String(embedding.embedding_model)],
          ]} />
        </Card>
      </Section>
      <Section>
        <Card title="Slack">
          <StatusGrid rows={[
            ["bot token", slack.bot_token_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
            ["app token", slack.app_token_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
            ["signing secret", slack.signing_secret_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
            ["installations", fmt((slack.installations as Json[])?.length ?? 0)],
          ]} />
        </Card>
      </Section>
      <Section>
        <Card title="Tools">
          <StatusGrid rows={[
            ["brave search", tools.brave_search_configured ? <Pill tone="good">set</Pill> : <Pill tone="muted">off</Pill>],
            ["firecrawl", tools.firecrawl_configured ? <Pill tone="good">set</Pill> : <Pill tone="muted">off</Pill>],
            ["web fetch", tools.web_fetch_enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>],
          ]} />
        </Card>
      </Section>
      {data?.environment_file && (
        <Section>
          <Card title="Environment file" meta={data.environment_file}>
            <pre className="overflow-auto p-4 font-mono text-[11px]">{fmt(data, 4000)}</pre>
          </Card>
        </Section>
      )}
      {loading && !data && <Empty>loading…</Empty>}
      {error && <Empty>error: {error}</Empty>}
    </>
  );
}

function StatusGrid({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <div className="grid grid-cols-2 gap-0">
      {rows.map(([k, v]) => (
        <div key={k} className="border-b border-r border-ink p-3 last:border-r-0">
          <div className="font-mono text-[10px] uppercase tracking-wider text-muted">{k}</div>
          <div className="mt-1.5 font-mono text-[13px]">{v}</div>
        </div>
      ))}
    </div>
  );
}