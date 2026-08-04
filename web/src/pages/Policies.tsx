import { ShieldCheck } from "lucide-react";
import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

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
          {data && data.policies.length === 0 && (
            <div className="flex flex-col items-center gap-3 px-4 py-14 text-center">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-brand-soft text-brand">
                <ShieldCheck size={20} />
              </span>
              <div>
                <div className="text-[13px] font-medium text-ink">No conditional policies yet</div>
                <p className="mx-auto mt-1 max-w-[360px] text-[12px] text-muted">
                  Without one, every tool falls back to its own risk-based default: reads run
                  freely, writes always pause for approval. Create a policy through{" "}
                  <code className="rounded bg-paper px-1 py-0.5 font-mono text-[11px]">POST /v1/policies</code>{" "}
                  to add org-specific conditions, forbids, or approval rules.
                </p>
              </div>
            </div>
          )}
          {data && data.policies.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="whitespace-nowrap">id</th>
                  <th className="whitespace-nowrap">name</th>
                  <th className="whitespace-nowrap">effect</th>
                  <th className="whitespace-nowrap">version</th>
                  <th className="whitespace-nowrap">status</th>
                  <th className="w-full">predicates</th>
                </tr>
              </thead>
              <tbody>
                {data.policies.map((p) => {
                  const po = p as Record<string, unknown>;
                  return (
                    <tr key={String(po.id)}>
                      <td className="font-mono text-[12px]">{String(po.id)}</td>
                      <td className="font-medium">{fmt(po.name, 60)}</td>
                      <td>
                        <Pill tone={po.effect === "forbid" ? "bad" : po.effect === "permit" ? "good" : "muted"}>
                          {String(po.effect)}
                        </Pill>
                      </td>
                      <td className="text-muted">{String(po.version)}</td>
                      <td>
                        <Pill tone={po.status === "active" ? "good" : "muted"}>{String(po.status)}</Pill>
                      </td>
                      <td className="text-muted">{fmt(po.predicates, 120)}</td>
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
