import { useEffect, useMemo, useState } from "react";
import {
  Ban,
  Check,
  ChevronRight,
  CircleAlert,
  Search,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import { api, ApiError, Json, ORG } from "../api";
import { Card, Empty, PageHeader, Pill, Section, useAsync } from "../components";

type PolicyMode = "supervised" | "autonomous" | "full_access" | "restricted";
type Execution = "direct" | "ai_review" | "ask" | "deny";
type ExecutionOverride = "inherit" | "ai_review" | "ask" | "deny";
type EffectivePolicy = {
  override: ExecutionOverride;
  access: "enabled" | "forbidden";
  execution: Execution;
  source: string;
};
type Tool = {
  name: string;
  description?: string;
  risk?: "read" | "write" | "destructive";
  toolkits?: string[];
  capability_id?: string;
  approval_required?: boolean;
  input_schema?: Json;
  runtime_ready: boolean;
  setup_needed?: string[];
  policy: EffectivePolicy;
};
type ToolsResponse = {
  tools: Tool[];
  count: number;
  ready_count: number;
  setup_needed_count: number;
  policy_mode: PolicyMode;
  composio_configured: boolean;
};

const MODE_COPY: Record<PolicyMode, { label: string; description: string }> = {
  supervised: {
    label: "Supervised",
    description: "Reads run directly. Every write asks a human.",
  },
  autonomous: {
    label: "Autonomous",
    description: "Reads run directly. Writes go through AI review.",
  },
  full_access: {
    label: "Full access",
    description: "Reads and ordinary writes run directly.",
  },
  restricted: {
    label: "Restricted",
    description: "Every read and write asks a human.",
  },
};

const EXECUTION_COPY: Record<Execution, { label: string; tone: "good" | "orange" | "bad" | "muted" }> = {
  direct: { label: "Direct", tone: "good" },
  ai_review: { label: "AI review", tone: "orange" },
  ask: { label: "Ask human", tone: "orange" },
  deny: { label: "Not loaded", tone: "bad" },
};

const OVERRIDE_OPTIONS: Array<{
  value: ExecutionOverride;
  label: string;
  description: string;
  icon: typeof Check;
}> = [
  {
    value: "inherit",
    label: "Use mode default",
    description: "Follow the organization mode selected above.",
    icon: SlidersHorizontal,
  },
  {
    value: "ai_review",
    label: "AI review",
    description: "AI approves, denies, or escalates the call to a human.",
    icon: Sparkles,
  },
  {
    value: "ask",
    label: "Ask human",
    description: "Pause every call until an authorized person decides.",
    icon: ShieldAlert,
  },
  {
    value: "deny",
    label: "Deny",
    description: "Remove this tool from the agent's available tool list.",
    icon: Ban,
  },
];

function toolSource(tool: Tool): string {
  const source = tool.toolkits?.[0]?.toLowerCase() || "built-in";
  return source.startsWith("mcp:") ? "mcp" : source;
}

function toolCategory(tool: Tool): string {
  const source = toolSource(tool);
  if (source !== "built-in") return source;
  const capability = (tool.capability_id || "").toLowerCase();
  const name = tool.name.toLowerCase();
  if (capability.startsWith("document.") || /^(pdf|docx|xlsx|pptx|ocr)_/.test(name)) return "documents";
  if (capability.startsWith("workspace.") || name.startsWith("workspace_")) return "workspace";
  if (capability.startsWith("memory.") || name.startsWith("brain_")) return "memory";
  if (capability.startsWith("slack.") || name.startsWith("slack_")) return "slack";
  if (capability.startsWith("web.") || name.startsWith("web_")) return "web";
  if (capability.startsWith("skill.") || name.startsWith("skill")) return "skills";
  if (name.startsWith("automation_")) return "automations";
  if (name.startsWith("browser_")) return "browser";
  if (name.startsWith("delegate_") || name.includes("child")) return "delegation";
  return "system";
}

function labelize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function toolSourceLabel(tool: Tool): string {
  const source = toolSource(tool);
  const category = toolCategory(tool);
  return source === category ? labelize(source) : `${labelize(source)} · ${labelize(category)}`;
}

function ToolDetailModal({
  tool,
  mode,
  onClose,
  onSaved,
}: {
  tool: Tool;
  mode: PolicyMode;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [saving, setSaving] = useState<ExecutionOverride | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  async function setPolicy(execution: ExecutionOverride) {
    setSaving(execution);
    setError(null);
    try {
      await api.post("/v1/tool-policies", {
        organization_id: ORG,
        tool_name: tool.name,
        execution,
      });
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  const execution = EXECUTION_COPY[tool.policy.execution];

  return (
    <div className="tool-drawer-backdrop" onClick={onClose}>
      <aside
        className="tool-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tool-drawer-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="tool-drawer-head">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted">
              <Wrench size={13} /> Tool details
            </div>
            <h2 id="tool-drawer-title" className="mb-0 mt-1 truncate font-mono text-[19px] font-medium text-ink">
              {tool.name}
            </h2>
          </div>
          <button type="button" className="tool-drawer-close" onClick={onClose} aria-label="Close tool details">
            <X size={18} />
          </button>
        </header>

        <div className="tool-drawer-body">
          <section className="tool-drawer-intro">
            <p className="tool-drawer-summary">
              {tool.description || "No description provided."}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wider text-muted">
              <span className="font-mono">{toolSourceLabel(tool)}</span>
              {tool.capability_id && (
                <>
                  <span aria-hidden="true">·</span>
                  <span className="font-mono normal-case tracking-normal">{tool.capability_id}</span>
                </>
              )}
            </div>
          </section>

          {!tool.runtime_ready && (
          <div className="rounded-lg border border-orange/25 bg-orange-soft p-4">
            <div className="flex items-center gap-2 text-[12px] font-semibold text-orange">
              <CircleAlert size={15} /> Setup needed
            </div>
            <p className="mb-0 mt-1.5 text-[11px] leading-relaxed text-ink">
              This tool is registered for visibility but is not loaded into an agent Task yet.
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {(tool.setup_needed || ["runtime dependency"]).map((requirement) => (
                <code key={requirement} className="rounded bg-panel px-2 py-1 text-[10px] text-orange">
                  {requirement}
                </code>
              ))}
            </div>
          </div>
          )}

          <div className="tool-drawer-facts">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted">Risk</div>
            <div className="mt-1 text-[12px] font-medium text-ink">{labelize(tool.risk || "read")}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted">Access</div>
            <div className="mt-1 text-[12px] font-medium text-ink">
              {tool.policy.access === "enabled" ? "Enabled" : "Forbidden"}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted">Execution</div>
            <div className="mt-1 text-[12px] font-medium text-ink">{execution.label}</div>
          </div>
          </div>

          <fieldset>
            <div className="mb-3 flex items-end justify-between gap-4">
              <legend className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                Tool override
              </legend>
              <span className="text-[10px] text-muted">Saved changes apply on the next call</span>
            </div>
            <div className="tool-policy-grid">
            {OVERRIDE_OPTIONS.map((option) => {
              const Icon = option.icon;
              const active = tool.policy.override === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  className={`tool-policy-option ${active ? "tool-policy-option-active" : ""}`}
                  disabled={saving !== null}
                  aria-pressed={active}
                  onClick={() => setPolicy(option.value)}
                >
                  <span className="tool-policy-option-icon">
                    {saving === option.value ? (
                      <span className="tool-saving-dot" aria-label="Saving" />
                    ) : (
                      <Icon size={15} />
                    )}
                  </span>
                  <span className="min-w-0 text-left">
                    <span className="block text-[12px] font-semibold text-ink">{option.label}</span>
                    <span className="mt-0.5 block text-[11px] leading-snug text-muted">
                      {option.description}
                    </span>
                  </span>
                  {active && <Check size={15} className="ml-auto flex-none text-brand" />}
                </button>
              );
            })}
            </div>
            <p className="mb-0 mt-3 text-[10px] leading-relaxed text-muted">
              Organization mode: {MODE_COPY[mode].label}. {MODE_COPY[mode].description}
            </p>
            {error && <p className="mb-0 mt-2 text-[11px] text-bad">Could not save: {error}</p>}
          </fieldset>

          <div className="grid gap-2 md:grid-cols-2">
            <details className="group rounded-lg border border-line bg-paper/50">
              <summary className="cursor-pointer select-none px-3 py-3 text-[11px] font-semibold text-ink">
                Full description
              </summary>
              <p className="m-0 border-t border-line p-3 text-[11px] leading-relaxed text-muted">
                {tool.description || "No description provided."}
              </p>
            </details>
            <details className="group rounded-lg border border-line bg-paper/50">
              <summary className="cursor-pointer select-none px-3 py-3 text-[11px] font-semibold text-ink">
                Input schema
              </summary>
              <pre className="m-0 max-h-[360px] overflow-auto border-t border-line p-3 font-mono text-[10px] leading-relaxed text-ink">
                {JSON.stringify(tool.input_schema ?? {}, null, 2)}
              </pre>
            </details>
          </div>
        </div>
      </aside>
    </div>
  );
}

export default function Tools() {
  const [revision, setRevision] = useState(0);
  const { data, error, loading } = useAsync<ToolsResponse>(
    () => api.get("/v1/tools", { organization_id: ORG }),
    [revision]
  );
  const [query, setQuery] = useState("");
  const [readiness, setReadiness] = useState("all");
  const [risk, setRisk] = useState("all");
  const [source, setSource] = useState("all");
  const [access, setAccess] = useState("all");
  const [category, setCategory] = useState("all");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [savingMode, setSavingMode] = useState<PolicyMode | null>(null);
  const [modeError, setModeError] = useState<string | null>(null);

  const tools = data?.tools || [];
  const selected = tools.find((tool) => tool.name === selectedName) || null;
  const sources = useMemo(() => [...new Set(tools.map(toolSource))].sort(), [tools]);
  const categories = useMemo(() => [...new Set(tools.map(toolCategory))].sort(), [tools]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tools.filter((tool) => {
      const matchesQuery =
        !q ||
        tool.name.toLowerCase().includes(q) ||
        (tool.description || "").toLowerCase().includes(q) ||
        (tool.capability_id || "").toLowerCase().includes(q) ||
        toolSource(tool).includes(q);
      const matchesReadiness =
        readiness === "all" || (readiness === "ready" ? tool.runtime_ready : !tool.runtime_ready);
      return (
        matchesQuery &&
        matchesReadiness &&
        (risk === "all" || tool.risk === risk) &&
        (source === "all" || toolSource(tool) === source) &&
        (access === "all" || tool.policy.access === access) &&
        (category === "all" || toolCategory(tool) === category)
      );
    });
  }, [tools, query, readiness, risk, source, access, category]);

  const writeCount = tools.filter((tool) => tool.risk !== "read").length;
  const reviewCount = tools.filter((tool) => tool.policy.execution === "ai_review").length;
  const forbiddenCount = tools.filter((tool) => tool.policy.access === "forbidden").length;
  const hasFilters = [readiness, risk, source, access, category].some((value) => value !== "all") || query;

  async function setMode(mode: PolicyMode) {
    setSavingMode(mode);
    setModeError(null);
    try {
      await api.post("/v1/tool-policy-mode", { organization_id: ORG, mode });
      setRevision((value) => value + 1);
    } catch (e) {
      setModeError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSavingMode(null);
    }
  }

  function openTool(tool: Tool) {
    setSelectedName(tool.name);
  }

  function clearFilters() {
    setQuery("");
    setReadiness("all");
    setRisk("all");
    setSource("all");
    setAccess("all");
    setCategory("all");
  }

  return (
    <>
      <PageHeader
        title="Tools"
        subtitle="The live tool registry, runtime readiness, and the policy path applied before every call."
      />

      <Section>
        <div className="tool-mode-panel">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[12px] font-semibold text-ink">
              <ShieldCheck size={16} className="text-brand" /> Organization mode
            </div>
            <p className="mb-0 mt-1 text-[11px] text-muted">
              Sets defaults for every tool. Individual overrides remain in effect.
            </p>
          </div>
          <div className="tool-mode-options" role="radiogroup" aria-label="Organization tool mode">
            {(Object.keys(MODE_COPY) as PolicyMode[]).map((mode) => {
              const active = data?.policy_mode === mode;
              return (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  className={`tool-mode-button ${active ? "tool-mode-button-active" : ""}`}
                  disabled={savingMode !== null || loading}
                  onClick={() => setMode(mode)}
                >
                  <span>{MODE_COPY[mode].label}</span>
                  {savingMode === mode && <span className="tool-saving-dot" aria-label="Saving" />}
                </button>
              );
            })}
          </div>
          <div className="tool-mode-description" aria-live="polite">
            {data ? MODE_COPY[data.policy_mode].description : "Loading policy mode…"}
          </div>
          {modeError && <div className="text-[11px] text-bad">Could not change mode: {modeError}</div>}
        </div>
      </Section>

      <Section>
        <Card>
          <div className="tool-summary" aria-label="Tool registry summary">
            <div><strong>{data?.count ?? "—"}</strong><span>Registered</span></div>
            <div><strong>{data?.ready_count ?? "—"}</strong><span>Ready</span></div>
            <div><strong>{data?.setup_needed_count ?? "—"}</strong><span>Need setup</span></div>
            <div><strong>{writeCount || "—"}</strong><span>Write risk</span></div>
            <div><strong>{reviewCount || "—"}</strong><span>AI reviewed</span></div>
            <div><strong>{forbiddenCount}</strong><span>Forbidden</span></div>
          </div>

          <div className="tool-filters">
            <label className="tool-search">
              <span className="sr-only">Search tools</span>
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input
                className="input w-full pl-9"
                placeholder="Search name, description, or capability…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <select className="select" aria-label="Filter by readiness" value={readiness} onChange={(e) => setReadiness(e.target.value)}>
              <option value="all">All readiness</option>
              <option value="ready">Ready</option>
              <option value="setup">Setup needed</option>
            </select>
            <select className="select" aria-label="Filter by risk" value={risk} onChange={(e) => setRisk(e.target.value)}>
              <option value="all">All risk</option>
              <option value="read">Read</option>
              <option value="write">Write</option>
              <option value="destructive">Destructive</option>
            </select>
            <select className="select" aria-label="Filter by source" value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="all">All sources</option>
              {sources.map((value) => <option key={value} value={value}>{labelize(value)}</option>)}
            </select>
            <select className="select" aria-label="Filter by category" value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="all">All categories</option>
              {categories.map((value) => <option key={value} value={value}>{labelize(value)}</option>)}
            </select>
            <select className="select" aria-label="Filter by access" value={access} onChange={(e) => setAccess(e.target.value)}>
              <option value="all">All access</option>
              <option value="enabled">Enabled</option>
              <option value="forbidden">Forbidden</option>
            </select>
            {hasFilters && (
              <button type="button" className="tool-clear" onClick={clearFilters}>Clear</button>
            )}
          </div>

          <div className="tool-result-line" aria-live="polite">
            <span>{filtered.length} of {tools.length} tools</span>
            <span>Policies take effect on the next tool call</span>
          </div>

          {loading && <Empty>loading tools…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {!loading && !error && filtered.length === 0 && (
            <Empty>No tools match these filters.</Empty>
          )}
          {filtered.length > 0 && (
            <div className="tool-registry">
              <div className="tool-registry-head" aria-hidden="true">
                <span>Tool</span><span>Purpose</span><span>Governance</span><span />
              </div>
              {filtered.map((tool) => {
                const execution = EXECUTION_COPY[tool.policy.execution];
                return (
                  <button key={tool.name} type="button" className="tool-registry-row" onClick={() => openTool(tool)}>
                    <span className="tool-identity">
                      <span className="tool-identity-mark"><Wrench size={14} /></span>
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-[12px] font-medium text-ink">{tool.name}</span>
                        <span className="tool-source block">
                          {toolSourceLabel(tool)}
                        </span>
                      </span>
                    </span>
                    <span className="tool-purpose">{tool.description || "No description provided."}</span>
                    <span className="tool-governance">
                      <span className={tool.runtime_ready ? "tool-status tool-status-ready" : "tool-status tool-status-setup"}>
                        {tool.runtime_ready ? <Check size={12} /> : <CircleAlert size={12} />}
                        {tool.runtime_ready ? "Ready" : "Setup needed"}
                      </span>
                      <span className="tool-governance-rule" aria-hidden="true" />
                      <Pill tone={tool.risk === "read" ? "muted" : tool.risk === "destructive" ? "bad" : "orange"}>{tool.risk || "read"}</Pill>
                      <span className={`tool-access ${tool.policy.access === "enabled" ? "tool-access-enabled" : "tool-access-forbidden"}`}>
                        {tool.policy.access === "enabled" ? "Enabled" : "Forbidden"}
                      </span>
                      <Pill tone={execution.tone}>{execution.label}</Pill>
                    </span>
                    <ChevronRight size={16} className="justify-self-end text-muted" aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          )}
        </Card>
      </Section>

      {selected && data && (
        <ToolDetailModal
          tool={selected}
          mode={data.policy_mode}
          onClose={() => setSelectedName(null)}
          onSaved={() => setRevision((value) => value + 1)}
        />
      )}
    </>
  );
}
