import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty } from "../components";

type Policies = { policies: Json[]; count: number };

export default function Policies() {
  const { data, error, loading } = useAsync<Policies>(
    () => api.get("/v1/policies", { organization_id: ORG }),
    []
  );
  return (
    <>
      <PageHeader
        title="Policies"
        subtitle="Versioned conditional policies: default-deny after adoption, forbid precedence, typed predicates, approval-gated writes, execution-time preconditions."
      />
      <Section>
        <Card title="Active policies" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.policies.length === 0 && <Empty>no policies</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>name</th><th>effect</th><th>version</th><th>status</th><th>predicates</th></tr></thead>
              <tbody>
                {data.policies.map((p) => {
                  const po = p as Record<string, unknown>;
                  return (
                    <tr key={String(po.id)}>
                      <td>{String(po.id)}</td>
                      <td>{fmt(po.name, 60)}</td>
                      <td>{String(po.effect)}</td>
                      <td>{String(po.version)}</td>
                      <td>{String(po.status)}</td>
                      <td>{fmt(po.predicates, 120)}</td>
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