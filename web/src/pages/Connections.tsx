import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ExternalLink, Loader2, RefreshCw } from "lucide-react";
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

/** Composio hosts a stable logo per toolkit slug — falls back to a letter
 * mark if a slug has no logo or the request fails, rather than a broken image. */
function ToolkitLogo({ toolkit, label, bubble }: { toolkit: string; label: string; bubble: string }) {
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

const POLL_MS = 2500;

type ConnectState = "start" | "waiting" | "success" | "error";

function ConnectModal({
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
  const [state, setState] = useState<ConnectState>("start");
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  async function checkConnected(): Promise<boolean> {
    const result = await api.post<{ accounts: ConnectorAccount[] }>(
      "/v1/composio/connections:sync",
      { organization_id: ORG }
    );
    return (result.accounts || []).some(
      (a) => String(a.toolkit) === toolkit && String(a.status) === "active"
    );
  }

  function startPolling() {
    pollRef.current = setInterval(async () => {
      try {
        if (await checkConnected()) {
          if (pollRef.current) clearInterval(pollRef.current);
          setState("success");
          onConnected();
          setTimeout(onClose, 1200);
        }
      } catch {
        // transient sync errors while waiting are not fatal; keep polling
      }
    }, POLL_MS);
  }

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
      startPolling();
    } catch (e) {
      setState("error");
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <Modal title={`Sign in to ${label}`} onClose={onClose}>
      {state === "start" && (
        <>
          <p className="text-[13px] leading-relaxed text-muted">
            Opens {label} in a new tab so you can sign in and authorize Docket via
            Composio. Come back here once you've approved it — this page picks it up
            automatically.
          </p>
          <button className="btn mt-4 flex w-full items-center justify-center gap-2" onClick={connect}>
            Continue to {label} <ExternalLink size={14} />
          </button>
        </>
      )}
      {state === "waiting" && (
        <div className="flex items-center gap-3 text-[13px] text-muted">
          <Loader2 size={16} className="flex-none animate-spin text-brand" />
          Waiting for you to finish signing in to {label} in the other tab…
        </div>
      )}
      {state === "success" && (
        <div className="flex items-center gap-2 text-[13px] font-medium text-good">
          <CheckCircle2 size={16} /> {label} connected.
        </div>
      )}
      {state === "error" && (
        <>
          <p className="text-[13px] text-bad">Something went wrong: {error}</p>
          <button className="btn mt-4 w-full" onClick={connect}>
            Retry
          </button>
        </>
      )}
    </Modal>
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
          <button
            className="btn-ghost flex items-center gap-2"
            onClick={syncAndRefresh}
            disabled={syncing}
          >
            <RefreshCw size={14} className={syncing ? "animate-spin" : ""} />
            {syncing ? "Syncing…" : "Refresh"}
          </button>
        }
      />

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

      {activeToolkit && (
        <ConnectModal
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
  );
}
