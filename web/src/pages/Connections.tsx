import { useEffect, useRef, useState } from "react";
import { api, ApiError, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, Modal } from "../components";

type ConnectorAccount = Record<string, unknown>;

const CURATED_INTEGRATIONS: { toolkit: string; label: string; blurb: string }[] = [
  { toolkit: "github", label: "GitHub", blurb: "Issues, pull requests, commits, and reviews." },
  { toolkit: "linear", label: "Linear", blurb: "Issues, projects, and cycles." },
  { toolkit: "notion", label: "Notion", blurb: "Pages and databases." },
  { toolkit: "googlecalendar", label: "Google Calendar", blurb: "Meetings and scheduling." },
];

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
    <Modal title={`Connect ${label}`} onClose={onClose}>
      {state === "start" && (
        <>
          <p className="font-mono text-[12px] text-muted">
            Opens {label} in a new tab to authorize Docket via Composio. Come back
            here once you've approved it — this page will pick it up automatically.
          </p>
          <button className="btn mt-4 w-full" onClick={connect}>
            Continue to {label}
          </button>
        </>
      )}
      {state === "waiting" && (
        <p className="font-mono text-[12px] text-muted">
          Waiting for you to finish connecting {label} in the other tab…
        </p>
      )}
      {state === "success" && (
        <p className="font-mono text-[12px] text-acid">{label} connected.</p>
      )}
      {state === "error" && (
        <>
          <p className="font-mono text-[12px] text-bad">error: {error}</p>
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
  const [activeToolkit, setActiveToolkit] = useState<{ toolkit: string; label: string } | null>(
    null
  );

  const { data: accts, error: e1, loading: l1 } = useAsync<{ connector_accounts: ConnectorAccount[] }>(
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

  const accounts = accts?.connector_accounts || [];
  const connectedToolkits = new Set(
    accounts.filter((a) => String(a.status) === "active").map((a) => String(a.toolkit))
  );

  return (
    <>
      <PageHeader
        title="Connections"
        subtitle="Connect a tool via Composio to start ingesting it. Tenant resolution is server-side from the connected-account registry."
      />

      <Section>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {CURATED_INTEGRATIONS.map((it) => {
            const connected = connectedToolkits.has(it.toolkit);
            return (
              <div key={it.toolkit} className="card flex flex-col justify-between p-4">
                <div>
                  <div className="font-mono text-[13px] uppercase tracking-wide">{it.label}</div>
                  <p className="mt-1 font-mono text-[11px] text-muted">{it.blurb}</p>
                </div>
                <div className="mt-4 flex items-center justify-between">
                  <Pill tone={connected ? "good" : "muted"}>
                    {connected ? "connected" : "not connected"}
                  </Pill>
                  <button
                    className="btn"
                    onClick={() => setActiveToolkit({ toolkit: it.toolkit, label: it.label })}
                  >
                    {connected ? "Reconnect" : "Connect"}
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
          {accts && (
            <table className="table">
              <thead><tr><th>id</th><th>provider</th><th>org</th><th>principal</th><th>scope</th></tr></thead>
              <tbody>
                {accounts.map((a) => (
                  <tr key={String(a.id)}>
                    <td>{String(a.id)}</td>
                    <td>{String(a.provider ?? a.toolkit)}</td>
                    <td>{String(a.organization_id)}</td>
                    <td>{String(a.principal_id)}</td>
                    <td>{String(a.scope_id)}</td>
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
              <thead><tr><th>name</th><th>mode</th><th>auth schema</th></tr></thead>
              <tbody>
                {(auths.auth_configs || []).map((a, i) => {
                  const ao = a as Record<string, unknown>;
                  return (
                    <tr key={i}>
                      <td>{String(ao.name)}</td>
                      <td>{String(ao.mode)}</td>
                      <td>{fmt(ao.auth_scheme, 100)}</td>
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
              <thead><tr><th>id</th><th>type</th><th>status</th><th>account</th></tr></thead>
              <tbody>
                {(triggers.triggers || []).map((t) => {
                  const to = t as Record<string, unknown>;
                  return (
                    <tr key={String(to.id)}>
                      <td>{String(to.id)}</td>
                      <td>{String(to.trigger_type)}</td>
                      <td>{to.enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{String(to.connected_account_id)}</td>
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
