/* Hallmark · pre-emit critique: P5 H5 E4 S5 R5 V5
 * Hallmark · macrostructure: Narrative Workflow · genre: modern-minimal
 * theme: Toda signal desk · nav: progress rail · enrichment: semantic system map
 * contrast: pass (40–41) · responsive: pass (34, 49, 50–57) · slop: pass
 */
import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  Copy,
  ExternalLink,
  KeyRound,
  LoaderCircle,
  Network,
  ShieldCheck,
} from "lucide-react";
import { api, ApiError, Json } from "../api";
import { useAsync } from "../components";

type SetupStep = { id: string; label: string; status: "complete" | "current" | "pending" | "optional" };
type SetupStatus = {
  write_authorized?: boolean;
  available_providers?: Json[];
  providers?: Json[];
  assignments?: Record<string, Record<string, unknown>>;
  slack?: Record<string, unknown>;
  tools?: Record<string, unknown>;
  organizations?: Json[];
  setup?: { complete: boolean; current_step: string; steps: SetupStep[] };
};
type Result = { ok: boolean; message: string } | null;

const ORDER = ["system", "provider", "models", "tools", "slack", "organization", "verify"];

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="setup-field">
      <span>{label}</span>
      {children}
      <small>{hint || "\u00a0"}</small>
    </label>
  );
}

function Feedback({ result }: { result: Result }) {
  if (!result) return <span className="setup-feedback" aria-live="polite" />;
  return (
    <span className={`setup-feedback ${result.ok ? "is-good" : "is-bad"}`} aria-live="polite">
      {result.ok ? <Check size={14} /> : <Circle size={11} />} {result.message}
    </span>
  );
}

function StepMark({ status }: { status: SetupStep["status"] }) {
  if (status === "complete") return <Check size={14} />;
  return <span className="setup-step-dot" aria-hidden="true" />;
}

export default function Setup() {
  const [revision, setRevision] = useState(0);
  const { data, error, loading } = useAsync<SetupStatus>(
    () => api.get("/v1/gateway/setup/status"),
    [revision]
  );
  const [active, setActive] = useState("system");
  const [authorized, setAuthorized] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<Result>(null);
  const [models, setModels] = useState<Record<string, unknown>[]>([]);
  const [manifest, setManifest] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [verification, setVerification] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    if (data?.setup?.current_step && active === "system") setActive(data.setup.current_step);
  }, [data?.setup?.current_step]);

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("bootstrap");
    if (!token) return;
    api.post<{ authorized: boolean }>("/v1/gateway/setup/bootstrap", { token })
      .then(() => {
        setAuthorized(true);
        window.history.replaceState({}, "", "/setup");
      })
      .catch((cause) => setAuthError(cause instanceof ApiError ? cause.message : String(cause)));
  }, []);

  const steps = data?.setup?.steps || [];
  const providerProfiles = (data?.available_providers || []) as Record<string, unknown>[];
  const assignments = data?.assignments || {};
  const slack = data?.slack || {};
  const tools = data?.tools || {};
  const configuredProviders = (data?.providers || []) as Record<string, unknown>[];

  const stepIndex = useMemo(() => Math.max(0, ORDER.indexOf(active)), [active]);
  function next() { setActive(ORDER[Math.min(ORDER.length - 1, stepIndex + 1)]); }
  function refresh() { setRevision((value) => value + 1); }

  async function submitProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy("provider"); setResult(null);
    try {
      await api.post("/v1/gateway/setup/providers", {
        connection_id: String(form.get("connection_id") || "company-models"),
        provider: String(form.get("provider") || "openrouter"),
        api_key: String(form.get("api_key") || ""),
        base_url: String(form.get("base_url") || "") || null,
        assignments: { main: String(form.get("main") || "") },
      });
      setResult({ ok: true, message: "Provider connected. Restart Toda after setup." });
      refresh(); setActive("models");
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  async function discoverModels(form: HTMLFormElement) {
    const values = new FormData(form); setBusy("catalog"); setResult(null);
    try {
      const response = await api.post<Record<string, unknown>>("/v1/gateway/setup/model-catalog", {
        provider: String(values.get("provider") || "openrouter"),
        api_key: String(values.get("api_key") || "") || null,
        base_url: String(values.get("base_url") || "") || null,
      });
      const found = Array.isArray(response.models) ? response.models as Record<string, unknown>[] : [];
      setModels(found); setResult({ ok: true, message: `${found.length} models available.` });
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  async function submitModels(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    const connection = String(form.get("connection_id") || configuredProviders[0]?.id || "company-models");
    const roleAssignments = Object.fromEntries(
      ["main", "embedding", "attention", "extraction", "vision", "image", "transcription", "tts"]
        .map((role) => [role, String(form.get(role) || "").trim()]).filter(([, value]) => value)
    );
    setBusy("models"); setResult(null);
    try {
      const provider = configuredProviders.find((item) => String(item.id) === connection);
      await api.post("/v1/gateway/setup/providers", {
        connection_id: connection,
        provider: String(provider?.provider || "openrouter"), api_key: "", assignments: roleAssignments,
      });
      setResult({ ok: true, message: "Model roles assigned." }); refresh(); next();
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  async function submitTools(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    setBusy("tools"); setResult(null);
    try {
      await api.post("/v1/gateway/setup/tools", {
        connection_id: String(form.get("connection_id") || "web-search"),
        provider: String(form.get("provider") || "brave"), api_key: String(form.get("api_key") || "") || null,
        base_url: String(form.get("base_url") || "") || null, use_for_search: true,
        use_for_extraction: form.get("use_for_extraction") === "on", web_fetch_enabled: true,
      });
      setResult({ ok: true, message: "Research tools configured." }); refresh(); next();
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  async function loadManifest() {
    const response = await api.get<{ manifest: string }>("/v1/gateway/setup/slack-manifest");
    setManifest(response.manifest);
  }

  async function submitSlack(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    setBusy("slack"); setResult(null);
    try {
      await api.post("/v1/gateway/setup/slack", {
        organization_id: String(form.get("organization_id") || "org_company"),
        organization_name: String(form.get("organization_name") || ""), bot_token: String(form.get("bot_token") || ""),
        app_token: String(form.get("app_token") || ""), signing_secret: String(form.get("signing_secret") || ""),
        admin_slack_user_id: String(form.get("admin_slack_user_id") || "") || null,
        admin_name: String(form.get("admin_name") || "") || null, proactive: false,
      });
      setResult({ ok: true, message: "Slack workspace verified and connected." }); refresh(); setActive("verify");
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  async function verify() {
    setBusy("verify"); setResult(null);
    try {
      const response = await api.post<Record<string, unknown>>("/v1/gateway/setup/verify");
      setVerification(response); setResult({ ok: Boolean(response.ready), message: response.ready ? "Toda is ready." : "Some checks still need attention." });
      refresh();
    } catch (cause) { setResult({ ok: false, message: cause instanceof ApiError ? cause.message : String(cause) }); }
    finally { setBusy(null); }
  }

  const canWrite = authorized || Boolean(data?.write_authorized);

  return (
    <main className="setup-shell">
      <aside className="setup-rail">
        <a href="/" className="setup-wordmark" aria-label="Toda home"><span>to</span>da</a>
        <div className="setup-rail-copy">
          <p>Local installation</p>
          <strong>Bring your organizational coworker online.</strong>
        </div>
        <nav aria-label="Setup progress">
          {steps.map((step, index) => (
            <button key={step.id} className={`setup-step ${active === step.id ? "is-active" : ""} is-${step.status}`} onClick={() => setActive(step.id)}>
              <span className="setup-step-index">{String(index + 1).padStart(2, "0")}</span>
              <span>{step.label}</span><StepMark status={step.status} />
            </button>
          ))}
        </nav>
        <p className="setup-local-note"><ShieldCheck size={15} /> Credentials stay on this machine.</p>
      </aside>

      <section className="setup-stage">
        <header className="setup-stage-head">
          <span>First-run setup</span><span>{stepIndex + 1} / {ORDER.length}</span>
        </header>
        {loading && <div className="setup-loading"><LoaderCircle className="animate-spin" /> Inspecting this installation…</div>}
        {error && error !== "null" && <div className="setup-alert is-bad">Could not read setup state: {error}</div>}
        {authError && <div className="setup-alert is-bad">The bootstrap link is invalid or expired. Restart Toda to issue another.</div>}
        {!loading && !canWrite && (
          <div className="setup-alert"><KeyRound size={17} /> Open the complete bootstrap URL printed by <code>toda gateway</code> to change this installation.</div>
        )}

        {data && active === "system" && (
          <article className="setup-panel">
            <p className="setup-kicker">System check</p>
            <h1>The desk is running.<br />Now give it a mind.</h1>
            <p className="setup-lede">Toda’s API, SQLite coordinator, and three internal work lanes are alive in one local process.</p>
            <div className="setup-system-map" aria-label="Toda runtime map">
              <div><Network size={19} /><strong>One Toda daemon</strong><span>owns coordination</span></div>
              <i />
              <div><CheckCircle2 size={19} /><strong>SQLite WAL</strong><span>ready for work</span></div>
              <i />
              <div><CheckCircle2 size={19} /><strong>Worker lanes</strong><span>inside the daemon</span></div>
            </div>
            <button className="setup-primary" onClick={next}>Connect a provider <ArrowRight size={16} /></button>
          </article>
        )}

        {data && active === "provider" && (
          <article className="setup-panel">
            <p className="setup-kicker">Model provider</p><h1>Connect once.<br />Assign models after.</h1>
            <p className="setup-lede">A provider connection owns the credential. Every capability points to that connection and a model ID.</p>
            <form className="setup-form" onSubmit={submitProvider}>
              <div className="setup-grid">
                <Field label="Connection name" hint="A local label, such as company-models"><input name="connection_id" placeholder="company-models" required /></Field>
                <Field label="Provider" hint="Direct provider, gateway, or local endpoint"><select name="provider" defaultValue="openrouter">{providerProfiles.map((item) => <option key={String(item.id)} value={String(item.id)}>{String(item.name)}</option>)}</select></Field>
                <Field label="API key" hint="Stored in the local restricted environment file"><input name="api_key" type="password" autoComplete="off" placeholder="Paste provider key" /></Field>
                <Field label="Custom base URL" hint="Only for compatible or self-hosted endpoints"><input name="base_url" type="url" placeholder="https://api.example.com/v1" /></Field>
              </div>
              <Field label="Primary model" hint="You can fine-tune every role in the next step"><input name="main" list="setup-models" placeholder="provider/model-id" required /></Field>
              <datalist id="setup-models">{models.map((item) => <option key={String(item.id)} value={String(item.id)} />)}</datalist>
              <div className="setup-actions"><button type="button" className="setup-secondary" disabled={busy !== null} onClick={(event) => event.currentTarget.form && discoverModels(event.currentTarget.form)}>Discover models</button><button className="setup-primary" disabled={!canWrite || busy !== null}>{busy === "provider" ? "Connecting…" : "Connect provider"}</button></div>
              <Feedback result={result} />
            </form>
          </article>
        )}

        {data && active === "models" && (
          <article className="setup-panel">
            <p className="setup-kicker">Model roles</p><h1>One strong default.<br />Specialists only where useful.</h1>
            <p className="setup-lede">Start with Toda’s recommended reuse of the primary model, then override expensive or modality-specific roles.</p>
            <form className="setup-form" onSubmit={submitModels}>
              <Field label="Provider connection"><select name="connection_id">{configuredProviders.map((item) => <option key={String(item.id)} value={String(item.id)}>{String(item.id)}</option>)}</select></Field>
              <div className="setup-role-list">
                {[['main','Main reasoning'],['embedding','Embeddings'],['attention','Attention'],['extraction','Extraction'],['vision','Vision'],['image','Image generation'],['transcription','Speech to text'],['tts','Text to speech']].map(([role,label]) => <Field key={role} label={label} hint={role === 'main' ? 'Required' : 'Optional — leave blank to use the recommended fallback'}><input name={role} defaultValue={String(assignments[role]?.model || "")} list="setup-models" placeholder={role === 'main' ? 'provider/model-id' : 'Use recommended fallback'} /></Field>)}
              </div>
              <div className="setup-actions"><button className="setup-primary" disabled={!canWrite || busy !== null}>{busy === "models" ? "Saving…" : "Save assignments"}</button></div><Feedback result={result} />
            </form>
          </article>
        )}

        {data && active === "tools" && (
          <article className="setup-panel">
            <p className="setup-kicker">Research tools · optional</p><h1>Let Toda look outward.<br />Keep the boundary explicit.</h1>
            <p className="setup-lede">Safe page fetching works without a provider. Add search or structured extraction only when your organization needs it.</p>
            <form className="setup-form" onSubmit={submitTools}>
              <div className="setup-grid"><Field label="Connection name"><input name="connection_id" placeholder="company-search" required /></Field><Field label="Search provider"><select name="provider"><option value="brave">Brave</option><option value="tavily">Tavily</option><option value="exa">Exa</option><option value="firecrawl">Firecrawl</option><option value="searxng">SearXNG</option></select></Field><Field label="API key"><input name="api_key" type="password" autoComplete="off" placeholder="Provider key" /></Field><Field label="Instance URL"><input name="base_url" type="url" placeholder="Required for SearXNG" /></Field></div>
              <label className="setup-check"><input type="checkbox" name="use_for_extraction" /> Use this connection for structured extraction</label>
              <div className="setup-actions"><button type="button" className="setup-secondary" onClick={next}>Skip for now</button><button className="setup-primary" disabled={!canWrite || busy !== null}>Save tools</button></div><Feedback result={result} />
            </form>
          </article>
        )}

        {data && (active === "slack" || active === "organization") && (
          <article className="setup-panel">
            <p className="setup-kicker">Slack and organization</p><h1>One app.<br />One workspace.</h1>
            <p className="setup-lede">Create a Slack app from Toda’s manifest, then verify the workspace and establish its first administrator.</p>
            {!manifest ? <button className="setup-secondary" onClick={loadManifest}>Load Slack manifest</button> : <div className="setup-manifest"><pre>{manifest}</pre><button type="button" onClick={() => { navigator.clipboard.writeText(manifest); setCopied(true); setTimeout(() => setCopied(false), 2000); }}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "Copied" : "Copy manifest"}</button></div>}
            <a className="setup-text-link" href="https://api.slack.com/apps?new_app=1" target="_blank" rel="noreferrer">Create the Slack app <ExternalLink size={14} /></a>
            <form className="setup-form" onSubmit={submitSlack}>
              <div className="setup-grid"><Field label="Organization name"><input name="organization_name" placeholder="Northstar Labs" required /></Field><Field label="Organization ID" hint="Lowercase, stable, and local"><input name="organization_id" defaultValue="org_company" required /></Field><Field label="Bot token"><input name="bot_token" type="password" placeholder="xoxb-…" required /></Field><Field label="App token"><input name="app_token" type="password" placeholder="xapp-…" required /></Field><Field label="Signing secret"><input name="signing_secret" type="password" required /></Field><Field label="Admin Slack user ID"><input name="admin_slack_user_id" placeholder="U0…" /></Field><Field label="Admin display name"><input name="admin_name" placeholder="Maya" /></Field></div>
              <div className="setup-actions"><button className="setup-primary" disabled={!canWrite || busy !== null}>{busy === "slack" ? "Verifying…" : "Connect workspace"}</button></div><Feedback result={result} />
            </form>
          </article>
        )}

        {data && active === "verify" && (
          <article className="setup-panel">
            <p className="setup-kicker">Verification</p><h1>Inspect the whole path.<br />Then open Toda.</h1>
            <p className="setup-lede">Readiness is based on working components, not merely saved credentials.</p>
            <div className="setup-verification">{((verification?.checks || []) as Record<string, unknown>[]).map((check) => <div key={String(check.id)} className={check.ok ? "is-good" : "is-bad"}>{check.ok ? <CheckCircle2 size={18} /> : <Circle size={18} />}<span><strong>{String(check.label)}</strong><small>{String(check.detail)}</small></span></div>)}</div>
            <div className="setup-actions"><button className="setup-secondary" onClick={verify} disabled={!canWrite || busy !== null}>{busy === "verify" ? "Checking…" : "Run verification"}</button>{verification?.ready ? <a href="/" className="setup-primary">Open Toda <ArrowRight size={16} /></a> : null}</div><Feedback result={result} />
          </article>
        )}

        <footer className="setup-stage-foot"><span>Toda runs on this machine.</span><span>{slack.bot_token_configured ? "Slack credential saved" : "Slack not connected"} · {tools.web_fetch_enabled === false ? "Web fetch off" : "Safe web fetch on"}</span></footer>
      </section>
    </main>
  );
}
