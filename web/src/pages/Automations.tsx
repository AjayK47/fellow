import { api, Json, ORG, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Autos = { automations: Json[]; count: number };

export default function Automations() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync<Autos>(
    () => api.get("/v1/automations", { organization_id: ORG }), [tick]
  );
  async function runTick() {
    await api.post("/v1/automations:tick", {});
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Automations"
        subtitle="Interval schedules that activate the same main-agent runtime. Tick to fire due automations immediately."
        actions={<button className="btn" onClick={runTick}>tick now</button>}
      />
      <Section>
        <Card title="Schedules" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.automations.length === 0 && <Empty>no automations</Empty>}
          {data && (
            <table className="table">
              <thead><tr><th>id</th><th>name</th><th>interval</th><th>enabled</th><th>last run</th><th>goal</th></tr></thead>
              <tbody>
                {data.automations.map((a) => {
                  const ao = a as Record<string, unknown>;
                  return (
                    <tr key={String(ao.id)}>
                      <td>{String(ao.id)}</td>
                      <td>{fmt(ao.name, 40)}</td>
                      <td>{String(ao.interval_seconds || ao.cron)}</td>
                      <td>{ao.enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td>{fmt(ao.last_run_at, 22)}</td>
                      <td>{fmt(ao.goal, 100)}</td>
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

import { useState } from "react";