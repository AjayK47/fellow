import { FormEvent, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  CircleAlert,
  KeyRound,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Server,
  Trash2,
} from "lucide-react";
import { api, ApiError, Json, ORG, reltime } from "../api";
import { Card, Empty, PageHeader, Pill, Section, useAsync } from "../components";

type MCPResponse = { servers: Json[]; count: number };
type Transport = "streamable_http" | "sse" | "stdio";
type AuthType = "none" | "api_key" | "oauth";

function parseLines(value: FormDataEntryValue | null): string[] {
  return String(value || "").split("\n").map((line) => line.trim()).filter(Boolean);
}

function parseEnvironment(value: FormDataEntryValue | null): Record<string, string> {
  const entries: Record<string, string> = {};
  for (const line of parseLines(value)) {
    const separator = line.indexOf("=");
    if (separator <= 0) throw new Error(`Invalid environment line: ${line}`);
    entries[line.slice(0, separator).trim()] = line.slice(separator + 1);
  }
  return entries;
}

function AddServer({ onSaved }: { onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [transport, setTransport] = useState<Transport>("streamable_http");
  const [authType, setAuthType] = useState<AuthType>("none");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSaving(true);
    setError(null);
    try {
      const created = await api.post<Record<string, unknown>>("/v1/mcp/servers", {
        organization_id: ORG,
        name: String(form.get("name") || ""),
        transport,
        url: transport === "stdio" ? null : String(form.get("url") || ""),
        command: transport === "stdio" ? String(form.get("command") || "") : null,
        args: transport === "stdio" ? parseLines(form.get("args")) : [],
        env: transport === "stdio" ? parseEnvironment(form.get("env")) : {},
        auth_type: transport === "stdio" ? "none" : authType,
        api_key_header: String(form.get("api_key_header") || "Authorization"),
        api_key: authType === "api_key" ? String(form.get("api_key") || "") : null,
        oauth_scope: authType === "oauth" ? String(form.get("oauth_scope") || "") : null,
      });
      const id = String(created.id);
      if (authType === "oauth") {
        const popup = window.open("about:blank", "docket-mcp-oauth", "popup,width=620,height=760");
        const result = await api.post<{ connected: boolean; authorization_url?: string }>(
          `/v1/mcp/servers/${id}:oauth-start`,
          { organization_id: ORG },
        );
        if (result.authorization_url && popup) popup.location.href = result.authorization_url;
        else if (result.authorization_url) window.location.href = result.authorization_url;
        else popup?.close();
      } else {
        await api.post(`/v1/mcp/servers/${id}:test`, { organization_id: ORG });
      }
      event.currentTarget.reset();
      setOpen(false);
      onSaved();
    } catch (exception) {
      setError(exception instanceof ApiError ? exception.message : String(exception));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <button className="flex w-full items-center justify-between px-5 py-4 text-left text-[13px] font-semibold" onClick={() => setOpen(!open)}>
        <span className="flex items-center gap-2"><Plus size={15} className="text-brand" /> Add MCP server</span>
        {open ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
      </button>
      {open && (
        <form onSubmit={submit} className="grid gap-4 border-t border-line p-5">
          <div className="grid gap-3 md:grid-cols-2">
            <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Name</span><input className="input" name="name" placeholder="Linear workspace" required /></label>
            <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Transport</span>
              <select className="select" value={transport} onChange={(e) => { setTransport(e.target.value as Transport); if (e.target.value === "stdio") setAuthType("none"); }}>
                <option value="streamable_http">Streamable HTTP</option><option value="sse">HTTP / SSE (legacy)</option><option value="stdio">Local stdio process</option>
              </select>
            </label>
          </div>
          {transport === "stdio" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Command</span><input className="input font-mono" name="command" placeholder="uvx" required /></label>
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Arguments — one per line</span><textarea className="input min-h-24 font-mono" name="args" placeholder={"my-mcp-server\n--stdio"} /></label>
              <label className="grid gap-1.5 text-[12px] md:col-span-2"><span className="font-medium">Environment — KEY=value, one per line</span><textarea className="input min-h-20 font-mono" name="env" placeholder="API_TOKEN=…" /></label>
            </div>
          ) : (
            <>
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Server URL</span><input className="input font-mono" name="url" type="url" placeholder="https://example.com/mcp" required /></label>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Authentication</span>
                  <select className="select" value={authType} onChange={(e) => setAuthType(e.target.value as AuthType)}>
                    <option value="none">None</option><option value="api_key">API key / bearer token</option><option value="oauth">OAuth</option>
                  </select>
                </label>
                {authType === "api_key" && <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Header</span><input className="input font-mono" name="api_key_header" defaultValue="Authorization" /></label>}
              </div>
              {authType === "api_key" && <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Header value</span><input className="input font-mono" name="api_key" type="password" placeholder="Bearer … or your API key" required /></label>}
              {authType === "oauth" && <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Scopes <span className="font-normal text-muted">(optional)</span></span><input className="input font-mono" name="oauth_scope" placeholder="read write offline_access" /></label>}
            </>
          )}
          <p className="m-0 rounded-lg bg-orange-soft px-3 py-2 text-[11px] text-ink/75">
            Discovered MCP tools are treated as write tools. They follow your organization mode and can be overridden individually on the Tools page.
          </p>
          <div className="flex items-center gap-3"><button className="btn" disabled={saving}>{saving ? "Connecting…" : "Add and test server"}</button>{error && <span className="text-[12px] text-bad">{error}</span>}</div>
        </form>
      )}
    </Card>
  );
}

export default function MCP() {
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data, error: loadError, loading } = useAsync<MCPResponse>(() => api.get("/v1/mcp/servers", { organization_id: ORG }), [revision]);

  useEffect(() => {
    const completed = (event: MessageEvent) => {
      if (event.origin === window.location.origin && event.data?.type === "docket:mcp-oauth-complete") setRevision((value) => value + 1);
    };
    window.addEventListener("message", completed);
    return () => window.removeEventListener("message", completed);
  }, []);

  async function action(id: string, operation: "test" | "enable" | "disable" | "delete") {
    if (operation === "delete" && !window.confirm("Delete this MCP server and unload all of its tools?")) return;
    setBusy(`${id}:${operation}`); setError(null);
    try {
      if (operation === "delete") await api.del(`/v1/mcp/servers/${id}?organization_id=${encodeURIComponent(ORG)}`);
      else await api.post(`/v1/mcp/servers/${id}:${operation}`, { organization_id: ORG });
      setRevision((value) => value + 1);
    } catch (exception) { setError(exception instanceof ApiError ? exception.message : String(exception)); }
    finally { setBusy(null); }
  }

  return (
    <>
      <PageHeader title="MCP servers" subtitle="Connect remote or local MCP servers. Docket discovers their tools, namespaces them, and applies the same readiness and execution policies as native tools." />
      <Section><AddServer onSaved={() => setRevision((value) => value + 1)} /></Section>
      <Section>
        <Card title="Connected servers" meta={String(data?.count ?? 0)}>
          {(loading || loadError) && <Empty>{loading ? "loading…" : `error: ${loadError}`}</Empty>}
          {error && <div className="flex items-center gap-2 border-b border-line bg-bad-soft px-4 py-3 text-[12px] text-bad"><CircleAlert size={14} />{error}</div>}
          {data?.servers.length === 0 && <Empty>no MCP servers configured</Empty>}
          <div className="divide-y divide-line">
            {data?.servers.map((raw) => {
              const server = raw as Record<string, any>;
              const id = String(server.id);
              const ready = server.status === "ready";
              const enabled = Boolean(server.enabled);
              return <div key={id} className="grid gap-4 px-5 py-4 lg:grid-cols-[minmax(220px,1fr)_minmax(260px,1.5fr)_auto] lg:items-center">
                <div className="flex min-w-0 items-start gap-3"><span className="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-brand-soft text-brand"><Server size={15} /></span><div className="min-w-0"><div className="truncate text-[13px] font-semibold">{server.name}</div><div className="mt-1 flex flex-wrap gap-1.5"><Pill tone={ready ? "good" : server.status === "error" ? "bad" : "orange"}>{String(server.status).replaceAll("_", " ")}</Pill><Pill tone="muted">{server.transport}</Pill><span className="inline-flex items-center gap-1 text-[11px] text-muted"><KeyRound size={11} />{server.auth?.type || "none"}</span></div></div></div>
                <div className="min-w-0"><div className="truncate font-mono text-[11px] text-muted">{server.url || [server.command, ...(server.args || [])].join(" ")}</div><div className="mt-1 text-[11px] text-muted">{server.tools?.length || 0} tools · tested {reltime(server.last_tested_at)}</div>{server.last_error && <div className="mt-1 truncate text-[11px] text-bad" title={server.last_error}>{server.last_error}</div>}</div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <button className="btn-ghost flex items-center gap-1.5" disabled={busy !== null} onClick={() => action(id, "test")}><RefreshCw size={12} className={busy === `${id}:test` ? "animate-spin" : ""} />Test</button>
                  <button className="btn-ghost flex items-center gap-1.5" disabled={busy !== null} onClick={() => action(id, enabled ? "disable" : "enable")}>{enabled ? <Pause size={12} /> : <Play size={12} />}{enabled ? "Disable" : "Enable"}</button>
                  <button className="btn-ghost text-bad" disabled={busy !== null} onClick={() => action(id, "delete")} aria-label="Delete server"><Trash2 size={13} /></button>
                </div>
              </div>;
            })}
          </div>
        </Card>
      </Section>
    </>
  );
}
