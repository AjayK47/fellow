import { useEffect, useMemo, useState } from "react";
import { Ban, CheckCircle2, Search, ShieldAlert } from "lucide-react";
import { api, ApiError, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill, Modal } from "../components";

type ToolPolicy = { enabled?: boolean; approval_required?: boolean; tool_name?: string };
type Tool = {
  name: string;
  description?: string;
  risk?: string;
  toolkits?: string[];
  capability_id?: string;
  approval_mode?: string;
  input_schema?: Json;
  policy?: ToolPolicy;
};
type ToolsResponse = { tools: Tool[]; count: number; composio_configured: boolean };

const RISK_TONE: Record<string, "good" | "orange" | "bad" | "muted"> = {
  read: "muted",
  write: "orange",
  destructive: "bad",
};

function policyState(tool: Tool): { label: string; tone: "good" | "orange" | "bad" } {
  if (tool.policy?.enabled === false) return { label: "Forbidden", tone: "bad" };
  if (tool.policy?.approval_required || tool.risk === "write" || tool.risk === "destructive")
    return { label: "Ask approval", tone: "orange" };
  return { label: "Allowed", tone: "good" };
}

function ToolDetailModal({
  tool,
  onClose,
  onPolicyChange,
}: {
  tool: Tool;
  onClose: () => void;
  onPolicyChange: (name: string, policy: ToolPolicy) => void;
}) {
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const writeLocked = tool.risk === "write" || tool.risk === "destructive";

  async function setPolicy(next: { enabled: boolean; approval_required?: boolean }) {
    setSaving(next.enabled ? (next.approval_required ? "approval" : "allow") : "forbid");
    setError(null);
    try {
      const result = await api.post<ToolPolicy>("/v1/tool-policies", {
        organization_id: ORG,
        tool_name: tool.name,
        enabled: next.enabled,
        approval_required: next.approval_required ?? false,
      });
      onPolicyChange(tool.name, result);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  const state = policyState(tool);

  return (
    <Modal title={tool.name} onClose={onClose}>
      <div className="grid gap-4">
        {tool.description && <p className="text-[13px] leading-relaxed text-muted">{tool.description}</p>}

        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={RISK_TONE[tool.risk ?? "read"]}>{tool.risk ?? "read"}</Pill>
          <Pill tone={state.tone}>{state.label}</Pill>
          {tool.toolkits && tool.toolkits.length > 0 && (
            <Pill tone="muted">{tool.toolkits.join(", ")}</Pill>
          )}
        </div>

        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            Organization policy
          </div>
          <div className="grid grid-cols-3 gap-2">
            <button
              className={`rounded-lg border px-2.5 py-2 text-[12px] font-medium transition-colors ${
                state.label === "Allowed" && !writeLocked
                  ? "border-good bg-good-soft text-good"
                  : "border-line bg-panel text-ink hover:bg-paper"
              } ${writeLocked ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}
              disabled={writeLocked || saving !== null}
              onClick={() => setPolicy({ enabled: true, approval_required: false })}
            >
              <CheckCircle2 size={14} className="mx-auto mb-1" />
              {saving === "allow" ? "Saving…" : "Allow"}
            </button>
            <button
              className={`cursor-pointer rounded-lg border px-2.5 py-2 text-[12px] font-medium transition-colors ${
                state.label === "Ask approval"
                  ? "border-orange bg-orange-soft text-orange"
                  : "border-line bg-panel text-ink hover:bg-paper"
              }`}
              disabled={saving !== null}
              onClick={() => setPolicy({ enabled: true, approval_required: true })}
            >
              <ShieldAlert size={14} className="mx-auto mb-1" />
              {saving === "approval" ? "Saving…" : "Ask approval"}
            </button>
            <button
              className={`cursor-pointer rounded-lg border px-2.5 py-2 text-[12px] font-medium transition-colors ${
                state.label === "Forbidden"
                  ? "border-bad bg-bad-soft text-bad"
                  : "border-line bg-panel text-ink hover:bg-paper"
              }`}
              disabled={saving !== null}
              onClick={() => setPolicy({ enabled: false })}
            >
              <Ban size={14} className="mx-auto mb-1" />
              {saving === "forbid" ? "Saving…" : "Forbid"}
            </button>
          </div>
          {writeLocked && (
            <p className="mt-2 text-[11px] text-muted">
              This is a {tool.risk}-risk tool — it already requires human approval on every call,
              regardless of this setting. Forbid still fully removes it from the model's tool list.
            </p>
          )}
          {error && <p className="mt-2 text-[11px] text-bad">error: {error}</p>}
        </div>

        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            Input schema
          </div>
          <pre className="max-h-[320px] overflow-auto rounded-lg border border-line bg-paper p-3 font-mono text-[11px] leading-relaxed text-ink">
            {JSON.stringify(tool.input_schema ?? {}, null, 2)}
          </pre>
        </div>
      </div>
    </Modal>
  );
}

export default function Tools() {
  const { data, error, loading } = useAsync<ToolsResponse>(
    () => api.get("/v1/tools", { organization_id: ORG }),
    []
  );
  const [tools, setTools] = useState<Tool[]>([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Tool | null>(null);

  useEffect(() => {
    if (data?.tools) setTools(data.tools);
  }, [data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tools;
    return tools.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        (t.toolkits ?? []).some((k) => k.toLowerCase().includes(q)) ||
        (t.capability_id ?? "").toLowerCase().includes(q)
    );
  }, [tools, query]);

  function applyPolicy(name: string, policy: ToolPolicy) {
    setTools((prev) => prev.map((t) => (t.name === name ? { ...t, policy } : t)));
    setSelected((prev) => (prev && prev.name === name ? { ...prev, policy } : prev));
  }

  return (
    <>
      <PageHeader
        title="Tools"
        subtitle="Every tool the agent can call, with per-tool allow / ask-approval / forbid policy. Write-risk tools require human approval on every call regardless of this setting."
        actions={
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
            <input
              className="input w-[240px] pl-9"
              placeholder="Search tools or toolkits…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        }
      />
      <Section>
        <Card title="Tools" meta={String(filtered.length)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && filtered.length === 0 && !loading && <Empty>no tools match “{query}”</Empty>}
          {filtered.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">name</th>
                  <th className="whitespace-nowrap">toolkit</th>
                  <th className="whitespace-nowrap">risk</th>
                  <th className="whitespace-nowrap">policy</th>
                  <th className="whitespace-nowrap">schema</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t) => {
                  const state = policyState(t);
                  return (
                    <tr key={t.name} className="cursor-pointer" onClick={() => setSelected(t)}>
                      <td className="font-medium">{t.name}</td>
                      <td className="text-muted">
                        {t.toolkits && t.toolkits.length > 0 ? t.toolkits.join(", ") : "built-in"}
                      </td>
                      <td><Pill tone={RISK_TONE[t.risk ?? "read"]}>{t.risk ?? "read"}</Pill></td>
                      <td><Pill tone={state.tone}>{state.label}</Pill></td>
                      <td>
                        <button
                          className="btn-ghost"
                          onClick={(e) => {
                            e.stopPropagation();
                            setSelected(t);
                          }}
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>

      {selected && (
        <ToolDetailModal
          tool={selected}
          onClose={() => setSelected(null)}
          onPolicyChange={applyPolicy}
        />
      )}
    </>
  );
}
