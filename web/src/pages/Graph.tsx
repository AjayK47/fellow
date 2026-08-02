import { api, Json, ORG, PRINCIPAL, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty } from "../components";

type Graph = {
  entities: Json[];
  edges: Json[];
  organizations?: Json[];
};

export default function Graph() {
  const { data, error, loading } = useAsync<Graph>(
    () => api.get("/v1/graph/explorer", {
      organization_id: ORG,
      acting_principal_id: PRINCIPAL,
    }), []
  );
  return (
    <>
      <PageHeader
        title="Graph"
        subtitle="People, teams, projects, services, customers, ownership, membership, dependencies. Permission-filtered view of the company graph."
      />
      <Section>
        <Card title="Entities" meta={String(data?.entities?.length ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>type</th><th>name</th><th>aliases</th><th>attributes</th></tr></thead>
              <tbody>
                {(data.entities || []).slice(0, 200).map((e) => {
                  const eo = e as Record<string, unknown>;
                  return (
                    <tr key={String(eo.id)}>
                      <td>{String(eo.id)}</td>
                      <td>{String(eo.entity_type)}</td>
                      <td>{fmt(eo.name, 40)}</td>
                      <td>{fmt(eo.aliases, 80)}</td>
                      <td>{fmt(eo.attributes, 140)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
      <Section>
        <Card title="Edges" meta={String(data?.edges?.length ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>from</th><th>type</th><th>to</th><th>sources</th></tr></thead>
              <tbody>
                {(data.edges || []).slice(0, 200).map((e, i) => {
                  const eo = e as Record<string, unknown>;
                  return (
                    <tr key={i}>
                      <td>{String(eo.from_id)}</td>
                      <td>{String(eo.edge_type || eo.type)}</td>
                      <td>{String(eo.to_id)}</td>
                      <td>{fmt(eo.source_ids, 80)}</td>
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