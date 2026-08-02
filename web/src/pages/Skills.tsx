import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty } from "../components";

type Skills = { skills: Json[]; count: number };

export default function Skills() {
  const { data, error, loading } = useAsync<Skills>(
    () => api.get("/v1/skills", { organization_id: ORG }),
    []
  );
  return (
    <>
      <PageHeader
        title="Skills"
        subtitle="Reusable, versioned skill programs Docket can prepare and apply. Prepare a draft, then promote."
      />
      <Section>
        <Card title="Skills" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.skills.length === 0 && <Empty>no skills</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>name</th><th>status</th><th>description</th></tr></thead>
              <tbody>
                {data.skills.map((s) => {
                  const so = s as Record<string, unknown>;
                  return (
                    <tr key={String(so.id)}>
                      <td>{String(so.id)}</td>
                      <td>{fmt(so.name, 50)}</td>
                      <td>{String(so.status)}</td>
                      <td>{fmt(so.description, 160)}</td>
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