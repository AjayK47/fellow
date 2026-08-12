import { FormEvent, ReactNode, useState } from "react";
import { Check, ChevronDown, ChevronUp, Copy, ExternalLink, RotateCcw } from "lucide-react";
import { api, ApiError, Json, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, Modal } from "../components";

type Status = {
  environment_file?: string;
  available_providers?: Json[];
  providers?: Json[];
  assignments?: Record<string, Record<string, unknown>>;
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

function OrganizationRow({
  org,
  onSaved,
}: {
  org: Record<string, unknown>;
  onSaved: () => void;
}) {
  const [about, setAbout] = useState(String(org.about || ""));
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  async function save() {
    setSaving(true);
    setResult(null);
    try {
      await api.patch(`/v1/organizations/${org.id}`, { about });
      setResult({ ok: true, message: "Saved" });
      onSaved();
    } catch (e) {
      setResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <tr>
      <td className="font-mono text-[12px] align-top">{String(org.id)}</td>
      <td className="font-medium align-top">{fmt(org.name, 60)}</td>
      <td className="align-top">
        <textarea
          className="input min-h-[60px] w-full"
          placeholder="What this company does — briefly, for the agent's own grounding."
          value={about}
          onChange={(e) => setAbout(e.target.value)}
        />
        <div className="mt-1.5 flex items-center gap-2">
          <button className="btn-ghost" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          {result && (
            <span className={`text-[11px] ${result.ok ? "text-good" : "text-bad"}`}>
              {result.message}
            </span>
          )}
        </div>
      </td>
    </tr>
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
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelOptions, setModelOptions] = useState<Record<string, unknown>[]>([]);
  const [savingTools, setSavingTools] = useState(false);
  const [savingSlack, setSavingSlack] = useState(false);

  const slack = data?.slack || {};
  const providers = (data?.providers ?? []) as Record<string, unknown>[];
  const assignments = (data?.assignments ?? {}) as Record<string, Record<string, unknown>>;
  const availableProviders = (data?.available_providers ?? []) as Record<string, unknown>[];
  const tools = data?.tools || {};
  const organizations = (data?.organizations ?? []) as Record<string, unknown>[];

  async function saveModel(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSavingModel(true);
    setModelResult(null);
    try {
      const assignments = Object.fromEntries(
        ["main", "attention", "extraction", "embedding", "vision", "image", "transcription", "tts"]
          .map((role) => [role, String(form.get(role) || "").trim()])
          .filter(([, model]) => model)
      );
      await api.post("/v1/gateway/setup/providers", {
        connection_id: String(form.get("connection_id") || ""),
        provider: String(form.get("provider") || "openrouter"),
        api_key: String(form.get("api_key") || ""),
        base_url: String(form.get("base_url") || "") || null,
        assignments,
      });
      setModelResult({ ok: true, message: "Saved to .env", restartRequired: true });
      setTick((t) => t + 1);
    } catch (e) {
      setModelResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setSavingModel(false);
    }
  }

  async function loadModels(form: HTMLFormElement) {
    const data = new FormData(form);
    setLoadingModels(true);
    setModelResult(null);
    try {
      const result = await api.post("/v1/gateway/setup/model-catalog", {
        provider: String(data.get("provider") || "openrouter"),
        api_key: String(data.get("api_key") || "") || null,
        base_url: String(data.get("base_url") || "") || null,
      }) as Record<string, unknown>;
      const models = Array.isArray(result.models) ? result.models as Record<string, unknown>[] : [];
      setModelOptions(models);
      setModelResult({ ok: true, message: `Loaded ${models.length} models` });
    } catch (e) {
      setModelResult({ ok: false, message: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setLoadingModels(false);
    }
  }

  async function saveTools(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSavingTools(true);
    setToolsResult(null);
    try {
      await api.post("/v1/gateway/setup/tools", {
        connection_id: String(form.get("connection_id") || ""),
        provider: String(form.get("provider") || "brave"),
        api_key: String(form.get("api_key") || "") || null,
        base_url: String(form.get("base_url") || "") || null,
        use_for_search: form.get("use_for_search") === "on",
        use_for_extraction: form.get("use_for_extraction") === "on",
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
            <Card title="Model providers">
              <StatusGrid
                rows={[
                  ["connections", providers.length ? <Pill tone="good">{providers.length}</Pill> : <Pill tone="bad">none</Pill>],
                  ["main", String(assignments.main?.model || "deterministic offline")],
                  ["embedding", String(assignments.embedding?.model || "deterministic offline")],
                ]}
              />
              <ConfigSection label="Add provider and assign model roles">
                <form className="grid gap-3" onSubmit={saveModel}>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Connection name"><input className="input" name="connection_id" placeholder="openrouter-company" required /></Field>
                    <Field label="Provider"><select className="input" name="provider" defaultValue="openrouter">{availableProviders.map((item) => <option key={String(item.id)} value={String(item.id)}>{String(item.name)}</option>)}</select></Field>
                    <Field label="Provider API key"><input className="input" name="api_key" type="password" placeholder="not required for local providers" /></Field>
                    <Field label="Custom base URL"><input className="input" name="base_url" placeholder="optional" /></Field>
                    <Field label="Main model"><input className="input" name="main" list="provider-models" placeholder="provider model id" /></Field>
                    <Field label="Embedding model"><input className="input" name="embedding" list="provider-models" placeholder="optional" /></Field>
                    <Field label="Attention model"><input className="input" name="attention" list="provider-models" placeholder="defaults to main" /></Field>
                    <Field label="Extraction model"><input className="input" name="extraction" list="provider-models" placeholder="defaults to main" /></Field>
                    <Field label="Vision model"><input className="input" name="vision" list="provider-models" placeholder="optional" /></Field>
                    <Field label="Image model"><input className="input" name="image" list="provider-models" placeholder="optional" /></Field>
                    <Field label="Speech-to-text model"><input className="input" name="transcription" list="provider-models" placeholder="optional" /></Field>
                    <Field label="Text-to-speech model"><input className="input" name="tts" list="provider-models" placeholder="optional" /></Field>
                  </div>
                  <datalist id="provider-models">{modelOptions.map((item) => <option key={String(item.id)} value={String(item.id)}>{String(item.name || item.description || "")}</option>)}</datalist>
                  <div className="flex items-center gap-3">
                    <button className="btn-secondary" type="button" disabled={loadingModels} onClick={(event) => event.currentTarget.form && loadModels(event.currentTarget.form)}>{loadingModels ? "Loading…" : "Load models"}</button>
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
                  ["search", tools.search_connection ? <Pill tone="good">{String(tools.search_connection)}</Pill> : <Pill tone="muted">off</Pill>],
                  ["extraction", tools.extract_connection ? <Pill tone="good">{String(tools.extract_connection)}</Pill> : <Pill tone="muted">direct fetch</Pill>],
                  ["web fetch", tools.web_fetch_enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>],
                ]}
              />
              <ConfigSection label="Update web tools">
                <form className="grid gap-3" onSubmit={saveTools}>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="Connection name"><input className="input" name="connection_id" placeholder="brave-company" required /></Field>
                    <Field label="Provider"><select className="input" name="provider"><option value="brave">Brave</option><option value="tavily">Tavily</option><option value="exa">Exa</option><option value="firecrawl">Firecrawl</option><option value="searxng">SearXNG</option></select></Field>
                    <Field label="API key"><input className="input" name="api_key" type="password" placeholder="not required for SearXNG" /></Field>
                    <Field label="Custom / instance URL"><input className="input" name="base_url" placeholder="required for SearXNG" /></Field>
                  </div>
                  <label className="flex items-center gap-2 text-[12px] font-medium text-ink"><input type="checkbox" name="use_for_search" defaultChecked className="h-4 w-4 rounded border-line accent-brand" />Use for search</label>
                  <label className="flex items-center gap-2 text-[12px] font-medium text-ink"><input type="checkbox" name="use_for_extraction" className="h-4 w-4 rounded border-line accent-brand" />Use for extraction (Firecrawl only)</label>
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
                  <thead><tr><th>id</th><th>name</th><th className="w-full">about</th></tr></thead>
                  <tbody>
                    {organizations.map((org) => (
                      <OrganizationRow
                        key={String(org.id)}
                        org={org}
                        onSaved={() => setTick((t) => t + 1)}
                      />
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
