import { Fragment, useMemo, useState } from "react";
import { ChevronDown, ChevronUp, PackageCheck, Search, Sparkles } from "lucide-react";
import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Skills = { skills: Json[]; count: number };
type Versions = { versions: Json[] };
type Catalog = { packages: Json[]; readiness: Json[]; capabilities: string[] };
type Capability = { id: string; available: boolean; setup_needed?: string[] };
type Capabilities = { capabilities: Capability[] };

function VersionsPanel({
  skillId,
  currentVersion,
  onChanged,
}: {
  skillId: string;
  currentVersion: number;
  onChanged: () => void;
}) {
  const { data, error, loading } = useAsync<Versions>(
    () => api.get(`/v1/skills/${skillId}/versions`),
    [skillId]
  );
  async function approve(version: number) {
    await api.post(`/v1/skills/${skillId}/versions/${version}:approve`, {});
    onChanged();
  }
  const versions = data?.versions ?? [];
  return (
    <tr>
      <td colSpan={5} className="bg-paper !p-0">
        <div className="border-l-2 border-brand/40 px-5 py-3">
          {loading && <Empty>loading versions…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {!loading && versions.length === 0 && <Empty>no version history</Empty>}
          {versions.length > 0 && (
            <div className="grid gap-1.5">
              {versions.map((v) => {
                const vo = v as Record<string, unknown>;
                const version = Number(vo.version);
                return (
                  <div key={version} className="flex items-center justify-between gap-3 text-[12px]">
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono text-muted">v{version}</span>
                      <Pill tone={vo.status === "approved" ? "good" : vo.status === "deprecated" ? "bad" : "orange"}>
                        {String(vo.status)}
                      </Pill>
                      <span className="text-muted">{fmt(vo.updated_at, 30)}</span>
                    </div>
                    {vo.status === "draft" && version > currentVersion && (
                      <button className="btn" onClick={() => approve(version)}>Approve</button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

export default function Skills() {
  const [tick, setTick] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const { data, error, loading } = useAsync<Skills>(
    () => api.get("/v1/skills", { organization_id: ORG }),
    [tick]
  );
  const { data: catalog } = useAsync<Catalog>(() => api.get("/v1/skills/catalog", { organization_id: ORG }), []);
  const { data: capabilities } = useAsync<Capabilities>(() => api.get("/v1/capabilities", { organization_id: ORG }), []);

  async function approveDraft(id: string, version: number) {
    await api.post(`/v1/skills/${id}/versions/${version}:approve`, {});
    setTick((t) => t + 1);
  }
  async function deprecate(id: string) {
    await api.post(`/v1/skills/${id}:deprecate`, {});
    setTick((t) => t + 1);
  }

  const allSkills = (data?.skills ?? []) as Record<string, unknown>[];
  const pendingDrafts = allSkills.filter((s) => s.status === "draft" && !s.immutable);
  const q = query.trim().toLowerCase();
  const filteredSkills = q
    ? allSkills.filter(
        (s) =>
          String(s.name ?? "").toLowerCase().includes(q) ||
          String(s.id ?? "").toLowerCase().includes(q)
      )
    : allSkills;
  const sortedSkills = [...filteredSkills].sort((a, b) => {
    const aDraft = a.status === "draft" && !a.immutable ? 0 : 1;
    const bDraft = b.status === "draft" && !b.immutable ? 0 : 1;
    return aDraft - bDraft;
  });

  const readyCaps = (capabilities?.capabilities ?? []).filter((c) => c.available);
  const setupCaps = (capabilities?.capabilities ?? []).filter((c) => !c.available);

  return (
    <>
      <PageHeader
        title="Skills"
        subtitle="Reusable, versioned procedures Docket can prepare and apply — built-in packages plus agent-proposed drafts pending your approval."
        actions={
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
            <input
              className="input w-[220px] pl-9"
              placeholder="Search skills…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        }
      />

      {pendingDrafts.length > 0 && (
        <Section>
          <div className="rounded-xl2 border border-orange/30 bg-orange-soft px-5 py-4">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-orange">
              <Sparkles size={15} />
              {pendingDrafts.length} skill {pendingDrafts.length === 1 ? "draft" : "drafts"} awaiting your approval
            </div>
            <div className="mt-3 grid gap-2">
              {pendingDrafts.map((s) => (
                <div key={String(s.id)} className="flex items-center justify-between gap-3 rounded-lg bg-panel px-3.5 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-[13px] font-medium text-ink">{fmt(s.name, 60)}</div>
                    <div className="truncate text-[11px] text-muted">{fmt(s.description, 100)}</div>
                  </div>
                  <button
                    className="btn flex-none"
                    onClick={() => approveDraft(String(s.id), Number(s.version ?? 1))}
                  >
                    Approve
                  </button>
                </div>
              ))}
            </div>
          </div>
        </Section>
      )}

      <Section>
        <Card title="Platform skill packages" meta={`${catalog?.packages.length ?? 0} immutable`}>
          <p className="border-b border-line px-5 py-3 text-[12px] text-muted">
            Built-in and connector procedures live in versioned <code className="rounded bg-paper px-1 py-0.5 font-mono text-[11px]">SKILL.md</code> packages.
            Resetting SQLite cannot remove them.
          </p>
          {catalog && (
            <table className="table">
              <thead>
                <tr>
                  <th className="w-full">package</th>
                  <th className="whitespace-nowrap">kind</th>
                  <th className="whitespace-nowrap">readiness</th>
                  <th className="whitespace-nowrap">hash</th>
                </tr>
              </thead>
              <tbody>
                {catalog.packages.map((pkg) => {
                  const po = pkg as Record<string, unknown>;
                  const readiness = catalog.readiness.find(
                    (item) => String((item as Record<string, unknown>).name) === String(po.name)
                  ) as Record<string, unknown> | undefined;
                  return (
                    <tr key={String(po.name)}>
                      <td className="font-medium">{String(po.display_name ?? po.name)}</td>
                      <td><Pill tone="muted">{String(po.kind)}</Pill></td>
                      <td>
                        <Pill tone={readiness?.ready ? "good" : "orange"}>
                          {readiness?.ready ? "ready" : "missing capability"}
                        </Pill>
                      </td>
                      <td className="font-mono text-[11px] text-muted">
                        {String(po.content_hash ?? "").slice(0, 12)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>

      <Section>
        <Card title="Capability readiness" meta={`${capabilities?.capabilities.length ?? 0} registered`}>
          <div className="grid gap-4 p-5">
            <div>
              <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-good">
                <PackageCheck size={13} /> Ready · {readyCaps.length}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {readyCaps.length === 0 && <span className="text-[12px] text-muted">none yet</span>}
                {readyCaps.map((c) => (
                  <Pill key={c.id} tone="good">{c.id}</Pill>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-orange">
                Needs setup · {setupCaps.length}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {setupCaps.length === 0 && <span className="text-[12px] text-muted">everything registered is ready</span>}
                {setupCaps.map((c) => (
                  <Pill key={c.id} tone="orange">{c.id}</Pill>
                ))}
              </div>
            </div>
          </div>
        </Card>
      </Section>

      <Section>
        <Card title="Skills" meta={String(sortedSkills.length)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && sortedSkills.length === 0 && <Empty>{q ? `no skills match “${q}”` : "no skills"}</Empty>}
          {sortedSkills.length > 0 && (
            <table className="table">
              <thead><tr><th>name</th><th>status</th><th>description</th><th className="w-[220px]">action</th></tr></thead>
              <tbody>
                {sortedSkills.map((s) => {
                  const id = String(s.id);
                  const version = Number(s.version ?? 1);
                  const isDraft = s.status === "draft" && !s.immutable;
                  const isOpen = expanded === id;
                  return (
                    <Fragment key={id}>
                      <tr>
                        <td>
                          <div className="font-medium">{fmt(s.name, 50)}</div>
                          <div className="font-mono text-[10px] text-muted">{id}</div>
                        </td>
                        <td>
                          <Pill tone={s.immutable ? "good" : s.status === "approved" ? "good" : s.status === "deprecated" ? "bad" : "orange"}>
                            {s.immutable ? "immutable" : String(s.status)}
                          </Pill>
                        </td>
                        <td className="text-muted">{fmt(s.description, 120)}</td>
                        <td>
                          <div className="flex flex-wrap gap-2">
                            {isDraft && (
                              <button className="btn" onClick={() => approveDraft(id, version)}>Approve</button>
                            )}
                            {!s.immutable && s.status === "approved" && (
                              <button className="btn-ghost" onClick={() => deprecate(id)}>Deprecate</button>
                            )}
                            <button
                              className="btn-ghost flex items-center gap-1"
                              onClick={() => setExpanded(isOpen ? null : id)}
                            >
                              Versions {isOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                            </button>
                          </div>
                        </td>
                      </tr>
                      {isOpen && (
                        <VersionsPanel skillId={id} currentVersion={version} onChanged={() => setTick((t) => t + 1)} />
                      )}
                    </Fragment>
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
