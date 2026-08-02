import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";
import { Fragment, useState } from "react";

type Skills = { skills: Json[]; count: number };
type Versions = { versions: Json[] };

function VersionsRow({ skillId, currentVersion, onChanged }: { skillId: string; currentVersion: number; onChanged: () => void }) {
  const { data, error, loading } = useAsync<Versions>(
    () => api.get(`/v1/skills/${skillId}/versions`),
    [skillId]
  );
  async function approve(version: number) {
    await api.post(`/v1/skills/${skillId}/versions/${version}:approve`, {});
    onChanged();
  }
  if (loading) return <tr><td colSpan={5}><Empty>loading versions…</Empty></td></tr>;
  if (error) return <tr><td colSpan={5}><Empty>error: {error}</Empty></td></tr>;
  const versions = data?.versions ?? [];
  return (
    <tr>
      <td colSpan={5}>
        <table className="table">
          <thead><tr><th>version</th><th>status</th><th>updated</th><th>action</th></tr></thead>
          <tbody>
            {versions.map((v) => {
              const vo = v as Record<string, unknown>;
              const version = Number(vo.version);
              return (
                <tr key={version}>
                  <td>{version}</td>
                  <td>
                    <Pill tone={vo.status === "approved" ? "good" : vo.status === "deprecated" ? "bad" : "orange"}>
                      {String(vo.status)}
                    </Pill>
                  </td>
                  <td>{String(vo.updated_at ?? "")}</td>
                  <td>
                    {vo.status === "draft" && version > currentVersion && (
                      <button className="btn" onClick={() => approve(version)}>Approve</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </td>
    </tr>
  );
}

export default function Skills() {
  const [tick, setTick] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, error, loading } = useAsync<Skills>(
    () => api.get("/v1/skills", { organization_id: ORG }),
    [tick]
  );
  async function approveDraft(id: string, version: number) {
    await api.post(`/v1/skills/${id}/versions/${version}:approve`, {});
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Skills"
        subtitle="Reusable, versioned skill programs Docket can prepare and apply. Includes agent-proposed drafts pending your approval."
      />
      <Section>
        <Card title="Skills" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.skills.length === 0 && <Empty>no skills</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>name</th><th>status</th><th>description</th><th>action</th></tr></thead>
              <tbody>
                {data.skills.map((s) => {
                  const so = s as Record<string, unknown>;
                  const id = String(so.id);
                  const version = Number(so.version ?? 1);
                  return (
                    <Fragment key={id}>
                      <tr>
                        <td>{id}</td>
                        <td>{fmt(so.name, 50)}</td>
                        <td>
                          <Pill tone={so.status === "approved" ? "good" : so.status === "deprecated" ? "bad" : "orange"}>
                            {String(so.status)}
                          </Pill>
                        </td>
                        <td>{fmt(so.description, 140)}</td>
                        <td className="flex gap-2">
                          {so.status === "draft" && (
                            <button className="btn" onClick={() => approveDraft(id, version)}>Approve</button>
                          )}
                          <button className="btn btn-ghost" onClick={() => setExpanded(expanded === id ? null : id)}>
                            {expanded === id ? "Hide versions" : "Versions"}
                          </button>
                        </td>
                      </tr>
                      {expanded === id && (
                        <VersionsRow skillId={id} currentVersion={version} onChanged={() => setTick((t) => t + 1)} />
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
