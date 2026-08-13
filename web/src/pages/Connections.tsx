import { FormEvent, ReactNode, useEffect, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, ExternalLink, Loader2, RefreshCw, Search } from "lucide-react";
import { api, ApiError, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, Modal } from "../components";

type ConnectorAccount = Record<string, unknown>;

const CURATED_INTEGRATIONS: { toolkit: string; label: string; blurb: string; bubble: string }[] = [
  { toolkit: "github", label: "GitHub", blurb: "Issues, pull requests, commits, and reviews.", bubble: "bg-ink text-white" },
  { toolkit: "linear", label: "Linear", blurb: "Issues, projects, and cycles.", bubble: "bg-[#5E6AD2] text-white" },
  { toolkit: "notion", label: "Notion", blurb: "Pages and databases.", bubble: "bg-ink text-white" },
  { toolkit: "googlecalendar", label: "Google Calendar", blurb: "Meetings and scheduling.", bubble: "bg-[#1a73e8] text-white" },
];

const BROWSE_PAGE_SIZE = 8;

/** Composio hosts a stable logo per toolkit slug — falls back to a letter
 * mark if a slug has no logo or the request fails, rather than a broken image. */
function ToolkitLogo({ toolkit, label, bubble = "bg-ink text-white" }: { toolkit: string; label: string; bubble?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className={`flex h-11 w-11 items-center justify-center rounded-xl text-[15px] font-semibold ${bubble}`}>
        {label.slice(0, 1)}
      </span>
    );
  }
  return (
    <span className="flex h-11 w-11 items-center justify-center overflow-hidden rounded-xl border border-line bg-white p-2">
      <img
        src={`https://logos.composio.dev/api/${toolkit}`}
        alt={`${label} logo`}
        className="h-full w-full object-contain"
        onError={() => setFailed(true)}
      />
    </span>
  );
}

type ToolkitSummary = {
  slug: string;
  name: string;
  description: string | null;
  logo: string | null;
  auth_schemes: string[];
  no_auth: boolean;
  categories: string[];
};

type AuthSchemeField = {
  name: string;
  displayName?: string;
  description?: string;
  is_secret?: boolean;
  required?: boolean;
};

type ToolkitAuthScheme = {
  mode: string;
  name: string;
  required_fields: AuthSchemeField[];
  optional_fields: AuthSchemeField[];
};

type ToolkitDetail = {
  slug: string;
  name: string;
  logo: string | null;
  description: string | null;
  no_auth: boolean;
  auth_schemes: ToolkitAuthScheme[];
};

function schemeLabel(mode: string): string {
  switch (mode) {
    case "OAUTH2":
    case "OAUTH1":
      return "Sign in";
    case "API_KEY":
      return "API key";
    case "BEARER_TOKEN":
      return "Bearer token";
    case "BASIC":
      return "Username & password";
    default:
      return mode.replace(/_/g, " ").toLowerCase();
  }
}

const POLL_MS = 2500;

function LoadingRow({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-3 text-[13px] text-muted">
      <Loader2 size={16} className="flex-none animate-spin text-brand" /> {text}
    </div>
  );
}

/** The OAuth2 sub-flow: open Composio's hosted sign-in in a new tab, then
 * poll connections:sync until the account shows up active. */
function OAuthConnectBody({
  toolkit,
  label,
  onConnected,
  onDone,
}: {
  toolkit: string;
  label: string;
  onConnected: () => void;
  onDone: () => void;
}) {
  const [state, setState] = useState<"start" | "waiting" | "success" | "error">("start");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (state !== "waiting") return;
    const id = setInterval(async () => {
      try {
        const result = await api.post<{ accounts: ConnectorAccount[] }>(
          "/v1/composio/connections:sync",
          { organization_id: ORG }
        );
        const connected = (result.accounts || []).some(
          (a) => String(a.toolkit) === toolkit && String(a.status) === "active"
        );
        if (connected) {
          clearInterval(id);
          setState("success");
          onConnected();
          setTimeout(onDone, 1200);
        }
      } catch {
        // transient sync errors while waiting are not fatal; keep polling
      }
    }, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  async function connect() {
    setError(null);
    try {
      const result = await api.post<{ redirect_url: string | null }>(
        "/v1/composio/connect",
        { organization_id: ORG, toolkit }
      );
      if (!result.redirect_url) throw new Error("Composio did not return a connect link");
      window.open(result.redirect_url, "_blank", "noopener,noreferrer");
      setState("waiting");
    } catch (e) {
      setState("error");
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (state === "start") {
    return (
      <>
        <p className="text-[13px] leading-relaxed text-muted">
          Opens {label} in a new tab so you can sign in and authorize Toda via
          Composio. Come back here once you've approved it — this page picks it up
          automatically.
        </p>
        <button className="btn mt-4 flex w-full items-center justify-center gap-2" onClick={connect}>
          Continue to {label} <ExternalLink size={14} />
        </button>
      </>
    );
  }
  if (state === "waiting") {
    return <LoadingRow text={`Waiting for you to finish signing in to ${label} in the other tab…`} />;
  }
  if (state === "success") {
    return (
      <div className="flex items-center gap-2 text-[13px] font-medium text-good">
        <CheckCircle2 size={16} /> {label} connected.
      </div>
    );
  }
  return (
    <>
      <p className="text-[13px] text-bad">Something went wrong: {error}</p>
      <button className="btn mt-4 w-full" onClick={connect}>
        Retry
      </button>
    </>
  );
}

/** The non-OAuth sub-flow: renders a form from the scheme's own declared
 * fields (API key, bearer token, basic auth, ...) and connects synchronously
 * — no redirect, no polling, Composio activates it in the same request. */
function CredentialConnectBody({
  toolkit,
  label,
  scheme,
  onConnected,
  onDone,
}: {
  toolkit: string;
  label: string;
  scheme: ToolkitAuthScheme;
  onConnected: () => void;
  onDone: () => void;
}) {
  const fields = [...scheme.required_fields, ...scheme.optional_fields];
  const [values, setValues] = useState<Record<string, string>>({});
  const [state, setState] = useState<"form" | "submitting" | "success" | "error">("form");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setState("submitting");
    setError(null);
    try {
      await api.post("/v1/composio/connections:credentials", {
        organization_id: ORG,
        toolkit,
        auth_scheme: scheme.mode,
        credentials: values,
      });
      setState("success");
      onConnected();
      setTimeout(onDone, 1000);
    } catch (e) {
      setState("error");
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (state === "success") {
    return (
      <div className="flex items-center gap-2 text-[13px] font-medium text-good">
        <CheckCircle2 size={16} /> {label} connected.
      </div>
    );
  }

  return (
    <form className="grid gap-3" onSubmit={submit}>
      {fields.length === 0 && (
        <p className="text-[13px] text-muted">
          {label} doesn't need any additional details for {schemeLabel(scheme.mode).toLowerCase()} — continue to connect.
        </p>
      )}
      {fields.map((field) => (
        <label key={field.name} className="grid gap-1.5 text-[12px]">
          <span className="font-medium text-ink">
            {field.displayName || field.name}
            {field.required !== false && <span className="text-bad"> *</span>}
          </span>
          <input
            className="input"
            type={field.is_secret ? "password" : "text"}
            required={field.required !== false}
            value={values[field.name] || ""}
            onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
          />
          {field.description && <span className="text-[11px] text-muted">{field.description}</span>}
        </label>
      ))}
      {state === "error" && <p className="text-[13px] text-bad">Something went wrong: {error}</p>}
      <button className="btn mt-1 w-full" type="submit" disabled={state === "submitting"}>
        {state === "submitting" ? "Connecting…" : `Connect ${label}`}
      </button>
    </form>
  );
}

function SchemePicker({ schemes, onPick }: { schemes: ToolkitAuthScheme[]; onPick: (s: ToolkitAuthScheme) => void }) {
  return (
    <>
      <p className="mb-3 text-[13px] text-muted">This tool supports more than one way to connect:</p>
      <div className="grid gap-2">
        {schemes.map((s) => (
          <button key={s.mode} className="btn-ghost justify-start" onClick={() => onPick(s)}>
            {schemeLabel(s.mode)}
          </button>
        ))}
      </div>
    </>
  );
}

/** Figures out whether a toolkit is OAuth-only, credential-only, or offers
 * both (fetched fresh per open, not assumed) and drives the right sub-flow —
 * one modal handles every toolkit, curated or browsed. */
function ConnectFlowModal({
  toolkit,
  label,
  onClose,
  onConnected,
}: {
  toolkit: string;
  label: string;
  onClose: () => void;
  onConnected: () => void;
}) {
  const [detail, setDetail] = useState<ToolkitDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [scheme, setScheme] = useState<ToolkitAuthScheme | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<ToolkitDetail>(`/v1/composio/toolkits/${toolkit}`)
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        if (d.auth_schemes.length === 1) setScheme(d.auth_schemes[0]);
      })
      .catch((e) => alive && setDetailError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [toolkit]);

  let body: ReactNode;
  if (detailError) {
    body = <p className="text-[13px] text-bad">Could not load sign-in options: {detailError}</p>;
  } else if (!detail) {
    body = <LoadingRow text="Checking sign-in options…" />;
  } else if (detail.no_auth) {
    body = <p className="text-[13px] text-muted">{label} doesn't require signing in — its tools are available directly.</p>;
  } else if (detail.auth_schemes.length === 0) {
    body = <p className="text-[13px] text-muted">{label} doesn't expose a supported sign-in method yet.</p>;
  } else if (!scheme) {
    body = <SchemePicker schemes={detail.auth_schemes} onPick={setScheme} />;
  } else if (scheme.mode === "OAUTH2" || scheme.mode === "OAUTH1") {
    body = <OAuthConnectBody toolkit={toolkit} label={label} onConnected={onConnected} onDone={onClose} />;
  } else {
    body = (
      <CredentialConnectBody
        toolkit={toolkit}
        label={label}
        scheme={scheme}
        onConnected={onConnected}
        onDone={onClose}
      />
    );
  }

  return (
    <Modal title={`Sign in to ${label}`} onClose={onClose}>
      {body}
    </Modal>
  );
}

/** Search-driven browse over Composio's full toolkit catalog (1000+ entries)
 * — cursor-paginated a handful at a time rather than fetching or rendering
 * all of it. */
function BrowseToolkits({
  connectedToolkits,
  onPick,
}: {
  connectedToolkits: Set<string>;
  onPick: (toolkit: string, label: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);

  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(id);
  }, [query]);

  useEffect(() => {
    setCursorStack([null]);
  }, [debouncedQuery]);

  const cursor = cursorStack[cursorStack.length - 1] ?? null;
  const { data, error, loading } = useAsync<{ items: ToolkitSummary[]; next_cursor: string | null; total_items: number }>(
    () =>
      api.get("/v1/composio/toolkits", {
        search: debouncedQuery || undefined,
        cursor: cursor || undefined,
        limit: BROWSE_PAGE_SIZE,
      }),
    [debouncedQuery, cursor]
  );

  const items = data?.items || [];

  return (
    <Card title="All integrations" meta={data ? `${data.total_items} available` : undefined}>
      <div className="border-b border-line p-4">
        <div className="relative">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <input
            className="input pl-8"
            placeholder="Search Composio's catalog — Slack, Stripe, Salesforce, HubSpot…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>
      {loading && <Empty>loading…</Empty>}
      {error && <Empty>error: {error}</Empty>}
      {data && items.length === 0 && <Empty>No integrations match "{debouncedQuery}".</Empty>}
      {items.length > 0 && (
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
          {items.map((it) => {
            const connected = connectedToolkits.has(it.slug);
            return (
              <div key={it.slug} className="card flex flex-col justify-between p-4">
                <div className="flex items-start justify-between">
                  <ToolkitLogo toolkit={it.slug} label={it.name} />
                  {connected && (
                    <span className="flex items-center gap-1 text-good">
                      <CheckCircle2 size={16} />
                    </span>
                  )}
                </div>
                <div className="mt-3">
                  <div className="text-[13px] font-semibold text-ink">{it.name}</div>
                  <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-muted">
                    {it.description || "—"}
                  </p>
                </div>
                <div className="mt-3 flex items-center justify-between gap-2">
                  <Pill tone={connected ? "good" : "muted"}>{connected ? "Connected" : "Not connected"}</Pill>
                  <button
                    className={connected ? "btn-ghost" : "btn"}
                    disabled={it.no_auth}
                    onClick={() => onPick(it.slug, it.name)}
                  >
                    {it.no_auth ? "No auth needed" : connected ? "Reconnect" : "Connect"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="flex items-center justify-between border-t border-line px-4 py-3">
        <button
          className="btn-ghost flex items-center gap-1 disabled:opacity-40"
          disabled={cursorStack.length <= 1 || loading}
          onClick={() => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))}
        >
          <ChevronLeft size={14} /> Previous
        </button>
        <button
          className="btn-ghost flex items-center gap-1 disabled:opacity-40"
          disabled={!data?.next_cursor || loading}
          onClick={() => data?.next_cursor && setCursorStack((s) => [...s, data.next_cursor])}
        >
          Next <ChevronRight size={14} />
        </button>
      </div>
    </Card>
  );
}

/** Shown in place of the catalog when no Composio key is configured.
 *
 * Composio is the only thing standing between this page and every integration
 * on it, so an unconfigured install used to render the whole catalog as the
 * bare string "error: Composio API key is not configured" with nothing to do
 * about it. Credentials live in config.json and are never written at runtime,
 * so this reports what to set rather than offering a form. */
function ConnectComposio() {
  return (
    <Card>
      <div className="p-6">
        <h2 className="text-[15px] font-semibold text-ink">Connect Composio</h2>
        <p className="mt-1.5 max-w-[62ch] text-[13px] leading-relaxed text-muted">
          Composio brokers the OAuth for every tool on this page. Set its API key in
          this machine's <code className="font-mono text-[12px]">config.json</code> and
          restart the gateway; the catalog, connections, and triggers then become
          available.
        </p>
        <pre className="mt-4 max-w-[520px] overflow-x-auto rounded-lg border border-line bg-paper p-4 text-[12px] leading-relaxed text-ink">
{`"composio": {
  "api_key": "your-composio-api-key",
  "webhook_secret": null
}`}
        </pre>
        <div className="mt-4">
          <a
            className="inline-flex items-center gap-1 text-[12.5px] font-medium text-brand no-underline hover:underline"
            href="https://app.composio.dev/developers" target="_blank" rel="noreferrer"
          >
            Get an API key <ExternalLink size={12} />
          </a>
        </div>
      </div>
    </Card>
  );
}

export default function Connections() {
  const [tick, setTick] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [activeToolkit, setActiveToolkit] = useState<{ toolkit: string; label: string } | null>(
    null
  );

  async function syncAndRefresh() {
    setSyncing(true);
    try {
      // A completed Composio connection only reaches this table via the
      // connect modal's own polling loop. If that closed before catching
      // the status flip — or the connection was approved outside this
      // tab entirely — this table goes stale with nothing to invalidate
      // it. Re-syncing here is what makes "Refresh" actually reconcile
      // with Composio instead of just re-reading the same stale rows.
      await api.post("/v1/composio/connections:sync", { organization_id: ORG });
    } catch {
      // sync failures surface via the existing error states below on reload
    } finally {
      setSyncing(false);
      setTick((t) => t + 1);
    }
  }

  const { data: composio } = useAsync<{ api_key_configured: boolean }>(
    () => api.get("/v1/composio/status"),
    [tick]
  );
  const composioReady = composio?.api_key_configured !== false;

  const { data: accts, error: e1, loading: l1 } = useAsync<{ accounts: ConnectorAccount[] }>(
    () => api.get("/v1/connector-accounts", { organization_id: ORG }),
    [tick]
  );
  const { data: auths, error: e2, loading: l2 } = useAsync<{ auth_configs: Json[] }>(
    () => api.get<{ auth_configs: Json[] }>("/v1/composio/auth-configs").catch(() => ({ auth_configs: [] as Json[] })),
    [tick]
  );
  const { data: triggers, error: e3, loading: l3 } = useAsync<{ triggers: Json[]; count: number }>(
    () => api.get("/v1/composio/triggers", { organization_id: ORG }),
    [tick]
  );

  const accounts = accts?.accounts || [];
  const connectedToolkits = new Set(
    accounts.filter((a) => String(a.status) === "active").map((a) => String(a.toolkit))
  );

  return (
    <>
      <PageHeader
        title="Integrations"
        subtitle="Sign in to a tool via Composio to start ingesting it. Tenant resolution happens server-side from the connected-account registry — nothing here trusts a client-supplied org id."
        actions={
          composioReady ? (
            <button
              className="btn-ghost flex items-center gap-2"
              onClick={syncAndRefresh}
              disabled={syncing}
            >
              <RefreshCw size={14} className={syncing ? "animate-spin" : ""} />
              {syncing ? "Syncing…" : "Refresh"}
            </button>
          ) : null
        }
      />

      {!composioReady && (
        <Section>
          <ConnectComposio />
        </Section>
      )}

      {composioReady && (
        <>
        <Section>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {CURATED_INTEGRATIONS.map((it) => {
              const connected = connectedToolkits.has(it.toolkit);
              return (
                <div key={it.toolkit} className="card flex flex-col justify-between p-5">
                  <div className="flex items-start justify-between">
                    <ToolkitLogo toolkit={it.toolkit} label={it.label} bubble={it.bubble} />
                    {connected && (
                      <span className="flex items-center gap-1 text-good">
                        <CheckCircle2 size={16} />
                      </span>
                    )}
                  </div>
                  <div className="mt-4">
                    <div className="text-[14px] font-semibold text-ink">{it.label}</div>
                    <p className="mt-1 text-[12px] leading-relaxed text-muted">{it.blurb}</p>
                  </div>
                  <div className="mt-4 flex items-center justify-between gap-2">
                    <Pill tone={connected ? "good" : "muted"}>
                      {connected ? "Connected" : "Not connected"}
                    </Pill>
                    <button
                      className={connected ? "btn-ghost" : "btn"}
                      onClick={() => setActiveToolkit({ toolkit: it.toolkit, label: it.label })}
                    >
                      {connected ? "Reconnect" : "Sign in"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </Section>

        <Section>
          <BrowseToolkits
            connectedToolkits={connectedToolkits}
            onPick={(toolkit, label) => setActiveToolkit({ toolkit, label })}
          />
        </Section>

        {activeToolkit && (
          <ConnectFlowModal
            toolkit={activeToolkit.toolkit}
            label={activeToolkit.label}
            onClose={() => setActiveToolkit(null)}
            onConnected={() => setTick((t) => t + 1)}
          />
        )}

        <Section>
          <Card title="Connected accounts" meta={String(accounts.length)}>
            {l1 && <Empty>loading…</Empty>}
            {e1 && <Empty>error: {e1}</Empty>}
            {accts && accounts.length === 0 && !l1 && (
              <Empty>No accounts yet — sign in to an integration above to connect one.</Empty>
            )}
            {accounts.length > 0 && (
              <table className="table">
                <thead>
                  <tr>
                    <th className="w-full">id</th>
                    <th className="whitespace-nowrap">provider</th>
                    <th className="whitespace-nowrap">org</th>
                    <th className="whitespace-nowrap">principal</th>
                    <th className="whitespace-nowrap">scope</th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((a) => (
                    <tr key={String(a.id)}>
                      <td className="font-mono text-[12px]">{String(a.id)}</td>
                      <td>{String(a.provider ?? a.toolkit)}</td>
                      <td className="text-muted">{String(a.organization_id)}</td>
                      <td className="text-muted">{String(a.principal_id)}</td>
                      <td className="text-muted">{String(a.scope_id)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </Section>
        <Section>
          <Card title="Auth configurations" meta={String(auths?.auth_configs?.length ?? 0)}>
            {l2 && <Empty>loading…</Empty>}
            {e2 && <Empty>error: {e2}</Empty>}
            {auths && (
              <table className="table">
                <thead>
                  <tr>
                    <th className="whitespace-nowrap">name</th>
                    <th className="whitespace-nowrap">mode</th>
                    <th className="w-full">auth schema</th>
                  </tr>
                </thead>
                <tbody>
                  {(auths.auth_configs || []).map((a, i) => {
                    const ao = a as Record<string, unknown>;
                    return (
                      <tr key={i}>
                        <td className="font-medium">{String(ao.name)}</td>
                        <td className="text-muted">{String(ao.mode)}</td>
                        <td className="text-muted">{fmt(ao.auth_scheme, 100)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>
        </Section>
        <Section>
          <Card title="Triggers" meta={String(triggers?.count ?? 0)}>
            {l3 && <Empty>loading…</Empty>}
            {e3 && <Empty>error: {e3}</Empty>}
            {triggers && (
              <table className="table">
                <thead>
                  <tr>
                    <th className="w-full">id</th>
                    <th className="whitespace-nowrap">type</th>
                    <th className="whitespace-nowrap">status</th>
                    <th className="whitespace-nowrap">account</th>
                  </tr>
                </thead>
                <tbody>
                  {(triggers.triggers || []).map((t) => {
                    const to = t as Record<string, unknown>;
                    return (
                      <tr key={String(to.id)}>
                        <td className="font-mono text-[12px]">{String(to.id)}</td>
                        <td>{String(to.trigger_type)}</td>
                        <td>{to.enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                        <td className="text-muted">{String(to.connected_account_id)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>
        </Section>
        </>
      )}
    </>
  );
}
