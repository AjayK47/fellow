import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty } from "../components";

type Tools = { tools: Json[]; tool_policies?: Json[] };

export default function Tools() {
  const { data, error, loading } = useAsync<Tools>(
    () => api.get("/v1/tools", { organization_id: ORG }),
    []
  );
  void ORG;
  return (
    <>
      <PageHeader
        title="Tools"
        subtitle="Connected-app tool inventory and per-tool policies (allow / supervised / forbid). Writes require approval regardless of allow."
      />
      <Section>
        <Card title="Tools" meta={String(data?.tools?.length ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.tools && (
            <table className="table">
              <thead><tr><th>name</th><th>source</th><th>risk</th><th>schema</th></tr></thead>
              <tbody>
                {data.tools.map((t) => {
                  const to = t as Record<string, unknown>;
                  return (
                    <tr key={String(to.name)}>
                      <td>{String(to.name)}</td>
                      <td>{String(to.source)}</td>
                      <td>{String(to.risk)}</td>
                      <td>{fmt(to.parameters || to.input_schema, 160)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
      {data?.tool_policies && (
        <Section>
          <Card title="Tool policies" meta={String(data.tool_policies.length)}>
            <table className="table">
              <thead><tr><th>tool</th><th>permit</th><th>scope</th></tr></thead>
              <tbody>
                {data.tool_policies.map((t, i) => {
                  const to = t as Record<string, unknown>;
                  return (
                    <tr key={i}>
                      <td>{String(to.tool_name)}</td>
                      <td>{String(to.permit)}</td>
                      <td>{fmt(to.scope_ids, 100)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        </Section>
      )}
    </>
  );
}