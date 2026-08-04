import { FormEvent, ReactNode, useState } from "react";
import { Check, ChevronDown, ChevronUp, Copy, ExternalLink, RotateCcw } from "lucide-react";
import { api, ApiError, Json, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, Modal } from "../components";

type Status = {
  environment_file?: string;
  model?: Record<string, unknown>;
  embedding?: Record<string, unknown>;
  slack?: Record<string, unknown>;
  tools?: Record<string, unknown>;
  organizations?: Json[];
};

function StatusGrid({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4 p-5 sm:grid-cols-3">
      {rows.map(([k, v]) => (
        <div key={k}>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{k}</div>
          <div className="mt-1 text-[13px] font-medium text-ink">{v}</div>
        </div>
      ))}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="grid gap-1.5 text-[12px]">
      <span className="font-medium text-ink">{label}</span>
      {children}
    </label>
  );
}

function SaveBanner({ result }: { result: { ok: boolean; message: string; restartRequired?: boolean } | null }) {
  if (!result) return null;
  return (
    <div
      className={`flex items-center gap-2 rounded-lg px-3 py-2 text-[12px] font-medium ${
        result.ok ? "bg-good-soft text-good" : "bg-bad-soft text-bad"
      }`}
    >
      {result.ok && <Check size={14} />}
      {result.message}
      {result.ok && result.restartRequired && (
        <span className="ml-1 flex items-center gap-1 text-good/80">
          <RotateCcw size={12} /> restart the gateway to pick this up
        </span>
      )}
    </div>
  );
}

function ConfigSection({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-line">
      <button
        className="flex w-full cursor-pointer items-center justify-between px-5 py-3 text-[12px] font-medium text-brand"
        onClick={() => setOpen((o) => !o)}
      >
        {label}
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>
      {open && <div className="grid gap-3 px-5 pb-5">{children}</div>}
    </div>
  );
}

function SlackManifestModal({ onClose }: { onClose: () => void }) {
  const { data, error, loading } = useAsync<{ manifest: string }>(
    () => api.get("/v1/gateway/setup/slack-manifest"),
    []
  );
  const [copied, setCopied] = useState(false);
  return (
    <Modal title="Slack app manifest" onClose={onClose}>
      <p className="text-[12px] text-muted">
        Paste this when creating the Slack app from a manifest, then come back and connect the
        bot/app tokens it generates.
      </p>
      {loading && <Empty>loading…</Empty>}
      {error && <Empty>error: {error}</Empty>}
      {data && (
        <>
          <pre className="mt-3 max-h-[320px] overflow-auto rounded-lg border border-line bg-paper p-3 font-mono text-[11px] leading-relaxed text-ink">
            {data.manifest}
          </pre>
          <button
            className="btn-ghost mt-3 flex w-full items-center justify-center gap-2"
            onClick={() => {
              navigator.clipboard.writeText(data.manifest);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? "Copied" : "Copy manifest"}
          </button>
        </>
      )}
    </Modal>
  );
}

export default function Setup() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync<Status>(
    () => api.get("/v1/gateway/setup/status"),
    [tick]
  );
  const [showManifest, setShowManifest] = useState(false);

  const [modelResult, setModelResult] = useState<{ ok: boolean; message: string; restartRequired?: boolean } | null>(null);
  const [toolsResult, setToolsResult] = useState<{ ok: boolean; message: string; restartRequired?: boolean } | null>(null);
  const [slackResult, setSlackResult] = useState<{ ok: boolean; message: string; restartRequired?: boolean } | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [savingTools, setSavingTools] = useState(false);
  const [savingSlack, setSavingSlack] = useState(false);

  const slack = data?.slack || {};
  const model = data?.model || {};
  const embedding = data?.embedding || {};
  const tools = data?.tools || {};
  const organizations = (data?.organizations ?? []) as Record<string, unknown>[];

  async function saveModel(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSavingModel(true);
    setModelResult(null);
    try {
      await api.post("/v1/gateway/setup/openrouter", {
        api_key: String(form.get("api_key") || ""),
        chat_model: String(form.get("chat_model") || "openai/gpt-5.6-luna"),
        attention_model: String(form.get("attention_model") || "") || null,
        extraction_model: String(form.get("extraction_model") || "") || null,
        embedding_api_key: String(form.get("embedding_api_key") || "") || null,
        embedding_model: String(form.get("embedding_model") || "qwen/qwen3-embedding-8b"),
      });
      setModelResult({ ok: true, message: "Saved to .env", restartRequired: true });
      setTick((t) => t + 1);
    } catch (e) {
      setModelResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSavingModel(false);
    }
  }

  async function saveTools(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSavingTools(true);
    setToolsResult(null);
    try {
      await api.post("/v1/gateway/setup/tools", {
        brave_search_api_key: String(form.get("brave_search_api_key") || "") || null,
        firecrawl_api_key: String(form.get("firecrawl_api_key") || "") || null,
        web_fetch_enabled: form.get("web_fetch_enabled") === "on",
      });
      setToolsResult({ ok: true, message: "Saved to .env", restartRequired: true });
      setTick((t) => t + 1);
    } catch (e) {
      setToolsResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSavingTools(false);
    }
  }

  async function saveSlack(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSavingSlack(true);
    setSlackResult(null);
    try {
      await api.post("/v1/gateway/setup/slack", {
        organization_id: String(form.get("organization_id") || ""),
        organization_name: String(form.get("organization_name") || ""),
        bot_token: String(form.get("bot_token") || ""),
        app_token: String(form.get("app_token") || ""),
        signing_secret: String(form.get("signing_secret") || ""),
        admin_slack_user_id: String(form.get("admin_slack_user_id") || "") || null,
        admin_name: String(form.get("admin_name") || "") || null,
        channel_id: String(form.get("channel_id") || "") || null,
        proactive: form.get("proactive") === "on",
      });
      setSlackResult({ ok: true, message: "Connected", restartRequired: true });
      setTick((t) => t + 1);
    } catch (e) {
      setSlackResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSavingSlack(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Setup"
        subtitle="Local gateway onboarding: inference provider, Slack app, and web research tools. Every write here lands only in the local, gitignored .env — never in the database."
      />

      {loading && !data && (
        <Section><Card><Empty>loading…</Empty></Card></Section>
      )}
      {error && (
        <Section><Card><Empty>error: {error}</Empty></Card></Section>
      )}

      {data && (
        <>
          <Section>
            <Card title="Inference (OpenRouter)">
              <StatusGrid
                rows={[
                  ["configured", model.configured ? <Pill tone="good">yes</Pill> : <Pill tone="bad">no</Pill>],
                  ["provider", String(model.model_provider)],
                  ["chat model", String(model.chat_model || "—")],
                  ["embedding configured", embedding.configured ? <Pill tone="good">yes</Pill> : <Pill tone="bad">no</Pill>],
                  ["embedding model", String(embedding.embedding_model || "—")],
                ]}
              />
              <ConfigSection label="Update inference configuration">
                <form className="grid gap-3" onSubmit={saveModel}>
                  <Field label="OpenRouter API key (required)">
                    <input className="input" name="api_key" type="password" placeholder="sk-or-…" required />
                  </Field>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Chat model">
                      <input className="input" name="chat_model" defaultValue={String(model.chat_model || "openai/gpt-5.6-luna")} />
                    </Field>
                    <Field label="Embedding model">
                      <input className="input" name="embedding_model" defaultValue={String(embedding.embedding_model || "qwen/qwen3-embedding-8b")} />
                    </Field>
                    <Field label="Attention model (optional)">
                      <input className="input" name="attention_model" placeholder="defaults to chat model" />
                    </Field>
                    <Field label="Extraction model (optional)">
                      <input className="input" name="extraction_model" placeholder="defaults to chat model" />
                    </Field>
                  </div>
                  <Field label="Separate embedding API key (optional)">
                    <input className="input" name="embedding_api_key" type="password" placeholder="defaults to the key above" />
                  </Field>
                  <div className="flex items-center gap-3">
                    <button className="btn" type="submit" disabled={savingModel}>
                      {savingModel ? "Saving…" : "Save"}
                    </button>
                    <SaveBanner result={modelResult} />
                  </div>
                </form>
              </ConfigSection>
            </Card>
          </Section>

          <Section>
            <Card title="Web tools">
              <StatusGrid
                rows={[
                  ["brave search", tools.brave_search_configured ? <Pill tone="good">set</Pill> : <Pill tone="muted">off</Pill>],
                  ["firecrawl", tools.firecrawl_configured ? <Pill tone="good">set</Pill> : <Pill tone="muted">off</Pill>],
                  ["web fetch", tools.web_fetch_enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>],
                ]}
              />
              <ConfigSection label="Update web tools">
                <form className="grid gap-3" onSubmit={saveTools}>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Brave Search API key (optional)">
                      <input className="input" name="brave_search_api_key" type="password" placeholder="leave blank to keep current" />
                    </Field>
                    <Field label="Firecrawl API key (optional)">
                      <input className="input" name="firecrawl_api_key" type="password" placeholder="leave blank to keep current" />
                    </Field>
                  </div>
                  <label className="flex items-center gap-2 text-[12px] font-medium text-ink">
                    <input
                      type="checkbox"
                      name="web_fetch_enabled"
                      defaultChecked={tools.web_fetch_enabled !== false}
                      className="h-4 w-4 rounded border-line accent-brand"
                    />
                    Enable safe, read-only web_fetch
                  </label>
                  <div className="flex items-center gap-3">
                    <button className="btn" type="submit" disabled={savingTools}>
                      {savingTools ? "Saving…" : "Save"}
                    </button>
                    <SaveBanner result={toolsResult} />
                  </div>
                </form>
              </ConfigSection>
            </Card>
          </Section>

          <Section>
            <Card title="Slack">
              <StatusGrid
                rows={[
                  ["bot token", slack.bot_token_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
                  ["app token", slack.app_token_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
                  ["signing secret", slack.signing_secret_configured ? <Pill tone="good">set</Pill> : <Pill tone="bad">missing</Pill>],
                  ["installations", fmt((slack.installations as Json[])?.length ?? 0)],
                ]}
              />
              <div className="flex flex-wrap gap-2 border-t border-line px-5 py-4">
                <button className="btn-ghost flex items-center gap-2" onClick={() => setShowManifest(true)}>
                  View app manifest
                </button>
                <a
                  href="/setup"
                  target="_blank"
                  rel="noreferrer"
                  className="btn-ghost flex items-center gap-2 no-underline"
                >
                  Open full onboarding wizard <ExternalLink size={13} />
                </a>
              </div>
              <p className="px-5 pb-2 text-[11px] text-muted">
                Haven't created the Slack app yet? Start from the manifest above or the full
                wizard — it walks through app creation before you have tokens to paste in below.
              </p>
              <ConfigSection label="Connect or update Slack">
                <form className="grid gap-3" onSubmit={saveSlack}>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Organization ID">
                      <input className="input" name="organization_id" defaultValue="org_acme" required />
                    </Field>
                    <Field label="Organization name">
                      <input className="input" name="organization_name" defaultValue="Acme Corp" required />
                    </Field>
                  </div>
                  <Field label="Bot token (xoxb-…)">
                    <input className="input" name="bot_token" type="password" placeholder="xoxb-…" required />
                  </Field>
                  <Field label="App token (xapp-…)">
                    <input className="input" name="app_token" type="password" placeholder="xapp-…" required />
                  </Field>
                  <Field label="Signing secret">
                    <input className="input" name="signing_secret" type="password" required />
                  </Field>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Admin Slack user ID (optional)">
                      <input className="input" name="admin_slack_user_id" placeholder="U0…" />
                    </Field>
                    <Field label="Admin display name (optional)">
                      <input className="input" name="admin_name" placeholder="Ajay" />
                    </Field>
                  </div>
                  <Field label="First channel to observe (optional)">
                    <input className="input" name="channel_id" placeholder="C0…" />
                  </Field>
                  <label className="flex items-center gap-2 text-[12px] font-medium text-ink">
                    <input
                      type="checkbox"
                      name="proactive"
                      defaultChecked
                      className="h-4 w-4 rounded border-line accent-brand"
                    />
                    Proactive by default in that channel
                  </label>
                  <p className="text-[11px] text-muted">
                    Validated live against Slack's <code className="rounded bg-paper px-1 py-0.5 font-mono">auth.test</code> before
                    anything is saved — an invalid token fails here, not silently at runtime.
                  </p>
                  <div className="flex items-center gap-3">
                    <button className="btn" type="submit" disabled={savingSlack}>
                      {savingSlack ? "Connecting…" : "Connect"}
                    </button>
                    <SaveBanner result={slackResult} />
                  </div>
                </form>
              </ConfigSection>
            </Card>
          </Section>

          {organizations.length > 0 && (
            <Section>
              <Card title="Organizations" meta={String(organizations.length)}>
                <table className="table">
                  <thead><tr><th>id</th><th>name</th></tr></thead>
                  <tbody>
                    {organizations.map((org) => (
                      <tr key={String(org.id)}>
                        <td className="font-mono text-[12px]">{String(org.id)}</td>
                        <td className="font-medium">{fmt(org.name, 60)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </Section>
          )}
        </>
      )}

      {showManifest && <SlackManifestModal onClose={() => setShowManifest(false)} />}
    </>
  );
}
