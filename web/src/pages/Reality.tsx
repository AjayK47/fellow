import { api, Json, ORG, PRINCIPAL, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";
import { useState } from "react";

export default function Reality() {
  const [tab, setTab] = useState<"facts" | "conflicts" | "cases" | "judgments">("facts");
  const facts = useAsync<{ facts: Json[] }>(
    () => api.get("/v1/facts", { organization_id: ORG, acting_principal_id: PRINCIPAL }), [tab]
  );
  const conflicts = useAsync<{ conflicts: Json[]; count: number }>(
    () => api.get("/v1/fact-conflicts", { organization_id: ORG, acting_principal_id: PRINCIPAL }), [tab]
  );
  const cases = useAsync<{ cases: Json[]; count: number }>(
    () => api.get("/v1/cases", { organization_id: ORG, acting_principal_id: PRINCIPAL }), [tab]
  );
  const judgments = useAsync<{ judgments: Json[]; count: number }>(
    () => api.get("/v1/judgments", { organization_id: ORG, acting_principal_id: PRINCIPAL }), [tab]
  );
  const active = tab === "facts" ? facts : tab === "conflicts" ? conflicts : tab === "cases" ? cases : judgments;
  return (
    <>
      <PageHeader
        title="Reality"
        subtitle="Bi-temporal company facts, semantic contradictions, cross-source cases, and human judgment guidance."
      />
      <Section>
        <div className="flex gap-2 border-b border-ink">
          {(["facts", "conflicts", "cases", "judgments"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 font-mono text-[12px] uppercase tracking-wider ${
                tab === t ? "bg-ink text-white" : "text-muted"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </Section>
      <Section>
        <Card title={tab}>
          {active.loading && <Empty>loading…</Empty>}
          {active.error && <Empty>error: {active.error}</Empty>}
          {tab === "facts" && facts.data && (
            <table className="table">
              <thead><tr><th>subject</th><th>predicate</th><th>object</th><th>valid</th><th>source</th></tr></thead>
              <tbody>
                {(facts.data.facts || []).slice(0, 200).map((f, i) => {
                  const fo = f as Record<string, unknown>;
                  return (
                    <tr key={i}>
                      <td>{String(fo.subject_id)}</td>
                      <td>{String(fo.predicate)}</td>
                      <td>{fmt(fo.object_value || fo.object, 120)}</td>
                      <td>{fmt(fo.valid_at, 16)}</td>
                      <td>{fmt(fo.source_ids, 60)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {tab === "conflicts" && conflicts.data && (
            <table className="table">
              <thead><tr><th>id</th><th>subject</th><th>predicate</th><th>status</th><th>claims</th></tr></thead>
              <tbody>
                {(conflicts.data.conflicts || []).slice(0, 200).map((c) => {
                  const co = c as Record<string, unknown>;
                  return (
                    <tr key={String(co.id)}>
                      <td>{String(co.id)}</td>
                      <td>{String(co.subject_id)}</td>
                      <td>{String(co.predicate)}</td>
                      <td>
                        <Pill tone={co.status === "resolved" ? "good" : "orange"}>{String(co.status)}</Pill>
                      </td>
                      <td>{fmt(co.claims, 140)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {tab === "cases" && cases.data && (
            <table className="table">
              <thead><tr><th>id</th><th>title</th><th>status</th><th>entities</th><th>tokens</th></tr></thead>
              <tbody>
                {(cases.data.cases || []).slice(0, 200).map((c) => {
                  const co = c as Record<string, unknown>;
                  return (
                    <tr key={String(co.id)}>
                      <td>{String(co.id)}</td>
                      <td>{fmt(co.title, 60)}</td>
                      <td><Pill tone={co.status === "open" ? "orange" : "muted"}>{String(co.status)}</Pill></td>
                      <td>{fmt(co.entity_ids, 60)}</td>
                      <td>{fmt(co.correlation_tokens, 80)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {tab === "judgments" && judgments.data && (
            <table className="table">
              <thead><tr><th>id</th><th>type</th><th>principal</th><th>summary</th></tr></thead>
              <tbody>
                {(judgments.data.judgments || []).slice(0, 200).map((j) => {
                  const jo = j as Record<string, unknown>;
                  return (
                    <tr key={String(jo.id)}>
                      <td>{String(jo.id)}</td>
                      <td>{String(jo.feedback_type || jo.judgment_type)}</td>
                      <td>{String(jo.principal_id)}</td>
                      <td>{fmt(jo.summary || jo.payload, 140)}</td>
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