import { FormEvent, useState } from "react";
import { ChevronDown, ChevronUp, Pause, Play, Plus } from "lucide-react";
import { api, ApiError, Json, ORG, PRINCIPAL, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type Autos = { automations: Json[]; count: number };

const INTERVAL_PRESETS = [
  { label: "Every 5 minutes", seconds: 300 },
  { label: "Every 15 minutes", seconds: 900 },
  { label: "Every hour", seconds: 3600 },
  { label: "Every 6 hours", seconds: 21600 },
  { label: "Every day", seconds: 86400 },
];

function NewAutomationForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [scheduleKind, setScheduleKind] = useState<"interval" | "cron">("interval");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSaving(true);
    setError(null);
    try {
      const destinationType = String(form.get("destination_type") || "internal_agent_session");
      const destinationId = String(form.get("destination_id") || "");
      await api.post("/v1/automations", {
        organization_id: ORG,
        name: String(form.get("name") || ""),
        goal: String(form.get("goal") || ""),
        principal_id: PRINCIPAL,
        conversation_id: `automation:${destinationType}:${destinationId || "default"}`,
        destination: {
          type: destinationType,
          id: destinationId || "default",
          is_private: form.get("is_private") === "on",
        },
        interval_seconds: scheduleKind === "interval" ? Number(form.get("interval_seconds") || 3600) : null,
        cron_expression: scheduleKind === "cron" ? String(form.get("cron_expression") || "") : null,
        timezone: String(form.get("timezone") || "UTC"),
      });
      (document.getElementById("new-automation-form") as HTMLFormElement | null)?.reset();
      setOpen(false);
      onCreated();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <button
        className="flex w-full cursor-pointer items-center justify-between px-5 py-4 text-[13px] font-semibold text-ink"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="flex items-center gap-2">
          <Plus size={15} className="text-brand" /> New automation
        </span>
        {open ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
      </button>
      {open && (
        <form id="new-automation-form" className="grid gap-3 border-t border-line px-5 py-5" onSubmit={submit}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5 text-[12px]">
              <span className="font-medium text-ink">Name</span>
              <input className="input" name="name" placeholder="Weekly export health check" required />
            </label>
            <label className="grid gap-1.5 text-[12px]">
              <span className="font-medium text-ink">Destination type</span>
              <select className="select" name="destination_type" defaultValue="internal_agent_session">
                <option value="internal_agent_session">Internal (no delivery)</option>
                <option value="slack_channel">Slack channel</option>
                <option value="dm">Slack DM</option>
              </select>
            </label>
          </div>
          <label className="grid gap-1.5 text-[12px]">
            <span className="font-medium text-ink">Goal</span>
            <textarea
              className="input min-h-[70px]"
              name="goal"
              placeholder="Check whether the export worker queue has backed up since the last run, and report only if something changed."
              required
            />
          </label>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5 text-[12px]">
              <span className="font-medium text-ink">Destination ID</span>
              <input className="input" name="destination_id" placeholder="channel or thread id (blank for internal)" />
            </label>
            <label className="mt-6 flex items-center gap-2 text-[12px] font-medium text-ink">
              <input type="checkbox" name="is_private" className="h-4 w-4 rounded border-line accent-brand" />
              Private destination
            </label>
          </div>

          <div className="mt-1 flex gap-2 border-b border-line pb-3">
            {(["interval", "cron"] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => setScheduleKind(kind)}
                className={`rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors ${
                  scheduleKind === kind ? "bg-brand-soft text-brand" : "text-muted hover:bg-paper"
                }`}
              >
                {kind === "interval" ? "Interval" : "Cron expression"}
              </button>
            ))}
          </div>
          {scheduleKind === "interval" ? (
            <label className="grid gap-1.5 text-[12px]">
              <span className="font-medium text-ink">Fires</span>
              <select className="select" name="interval_seconds" defaultValue="3600">
                {INTERVAL_PRESETS.map((p) => (
                  <option key={p.seconds} value={p.seconds}>{p.label}</option>
                ))}
              </select>
            </label>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="grid gap-1.5 text-[12px]">
                <span className="font-medium text-ink">Cron expression</span>
                <input className="input font-mono" name="cron_expression" placeholder="0 9 * * 1-5" />
              </label>
              <label className="grid gap-1.5 text-[12px]">
                <span className="font-medium text-ink">Timezone</span>
                <input className="input" name="timezone" defaultValue="UTC" placeholder="America/New_York" />
              </label>
            </div>
          )}

          <div className="flex items-center gap-3 pt-1">
            <button className="btn" type="submit" disabled={saving}>
              {saving ? "Creating…" : "Create automation"}
            </button>
            {error && <span className="text-[12px] text-bad">error: {error}</span>}
          </div>
        </form>
      )}
    </Card>
  );
}

export default function Automations() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync<Autos>(
    () => api.get("/v1/automations", { organization_id: ORG }), [tick]
  );
  async function runTick() {
    await api.post("/v1/automations:tick", {});
    setTick((t) => t + 1);
  }
  async function toggle(id: string, enabled: boolean) {
    await api.post(`/v1/automations/${id}:${enabled ? "disable" : "enable"}`, {});
    setTick((t) => t + 1);
  }
  return (
    <>
      <PageHeader
        title="Automations"
        subtitle="Interval or cron schedules that activate the same main-agent runtime. Tick to fire due automations immediately."
        actions={<button className="btn" onClick={runTick}>Tick now</button>}
      />
      <Section>
        <NewAutomationForm onCreated={() => setTick((t) => t + 1)} />
      </Section>
      <Section>
        <Card title="Schedules" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.automations.length === 0 && <Empty>no automations yet — create one above</Empty>}
          {data && data.automations.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th className="whitespace-nowrap">name</th>
                  <th className="whitespace-nowrap">schedule</th>
                  <th className="whitespace-nowrap">enabled</th>
                  <th className="whitespace-nowrap">last run</th>
                  <th className="w-full">goal</th>
                  <th className="whitespace-nowrap">action</th>
                </tr>
              </thead>
              <tbody>
                {data.automations.map((a) => {
                  const ao = a as Record<string, unknown>;
                  const schedule = ao.cron_expression
                    ? `${ao.cron_expression} (${ao.timezone || "UTC"})`
                    : ao.interval_seconds
                    ? `every ${Math.round(Number(ao.interval_seconds) / 60)}m`
                    : "—";
                  return (
                    <tr key={String(ao.id)}>
                      <td className="font-medium">{fmt(ao.name, 40)}</td>
                      <td className="font-mono text-[12px] text-muted">{schedule}</td>
                      <td>{ao.enabled ? <Pill tone="good">on</Pill> : <Pill tone="muted">off</Pill>}</td>
                      <td className="text-muted">{fmt(ao.last_run_at, 22)}</td>
                      <td className="text-muted">{fmt(ao.goal, 90)}</td>
                      <td>
                        <button
                          className="btn-ghost flex items-center gap-1.5"
                          onClick={() => toggle(String(ao.id), Boolean(ao.enabled))}
                        >
                          {ao.enabled ? <Pause size={12} /> : <Play size={12} />}
                          {ao.enabled ? "Pause" : "Enable"}
                        </button>
                      </td>
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
