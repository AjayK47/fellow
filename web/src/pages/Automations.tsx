import { FormEvent, useId, useState } from "react";
import { ChevronDown, ChevronUp, Pencil, Pause, Play, Plus, Trash2, Zap } from "lucide-react";
import { api, ApiError, Json, ORG, PRINCIPAL, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Modal, Pill } from "../components";

type Autos = { automations: Json[]; count: number };

// Suggestions only. The interval is a free-form number of minutes: the
// backend accepts anything from 60s up, and an agent-created schedule (say,
// every 3 minutes) has to round-trip through this form unchanged.
const INTERVAL_SUGGESTIONS = [1, 3, 5, 15, 30, 60, 360, 1440];
const MIN_INTERVAL_MINUTES = 1;

function formatInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.round(seconds % 60);
  const parts = [
    days && `${days}d`,
    hours && `${hours}h`,
    minutes && `${minutes}m`,
    rest && `${rest}s`,
  ].filter(Boolean);
  return `every ${parts.join(" ")}`;
}

function IntervalField({ defaultSeconds }: { defaultSeconds: number }) {
  const listId = `interval-suggestions-${useId()}`;
  return (
    <label className="grid gap-1.5 text-[12px]">
      <span className="font-medium text-ink">Fires every</span>
      <div className="flex items-center gap-2">
        <input
          className="input"
          type="number"
          name="interval_minutes"
          min={MIN_INTERVAL_MINUTES}
          step={1}
          list={listId}
          defaultValue={String(Math.max(MIN_INTERVAL_MINUTES, Math.round(defaultSeconds / 60)))}
        />
        <span className="text-muted">minutes</span>
      </div>
      <datalist id={listId}>
        {INTERVAL_SUGGESTIONS.map((minutes) => (
          <option key={minutes} value={minutes} />
        ))}
      </datalist>
    </label>
  );
}

function intervalSecondsFrom(form: FormData): number {
  const minutes = Number(form.get("interval_minutes") || 60);
  return Math.max(MIN_INTERVAL_MINUTES, Math.round(minutes)) * 60;
}

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
        interval_seconds: scheduleKind === "interval" ? intervalSecondsFrom(form) : null,
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
            <IntervalField defaultSeconds={3600} />
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
  const [editing, setEditing] = useState<Record<string, unknown> | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, loading } = useAsync<Autos>(
    () => api.get("/v1/automations", { organization_id: ORG }), [tick]
  );
  async function toggle(id: string, enabled: boolean) {
    await api.post(`/v1/automations/${id}:${enabled ? "disable" : "enable"}`, {});
    setTick((t) => t + 1);
  }
  async function runNow(id: string) {
    setActionError(null);
    try {
      await api.post(`/v1/automations/${id}:run`, { organization_id: ORG });
      setTick((t) => t + 1);
    } catch (e) { setActionError(e instanceof ApiError ? e.message : String(e)); }
  }
  async function remove(id: string) {
    if (!window.confirm("Delete this automation?")) return;
    setActionError(null);
    try {
      await api.del(`/v1/automations/${id}?organization_id=${encodeURIComponent(ORG)}`);
      setTick((t) => t + 1);
    } catch (e) { setActionError(e instanceof ApiError ? e.message : String(e)); }
  }
  async function saveEdit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!editing) return;
    const form = new FormData(e.currentTarget);
    const kind = String(form.get("schedule_kind"));
    const destinationType = String(form.get("destination_type"));
    const destinationId = String(form.get("destination_id") || "default");
    setActionError(null);
    try {
      await api.put(`/v1/automations/${String(editing.id)}`, {
        organization_id: ORG,
        name: String(form.get("name") || ""),
        goal: String(form.get("goal") || ""),
        principal_id: PRINCIPAL,
        conversation_id: `automation:${destinationType}:${destinationId}`,
        destination: { type: destinationType, id: destinationId, is_private: form.get("is_private") === "on" },
        interval_seconds: kind === "interval" ? intervalSecondsFrom(form) : null,
        cron_expression: kind === "cron" ? String(form.get("cron_expression") || "") : null,
        timezone: String(form.get("timezone") || "UTC"),
      });
      setEditing(null);
      setTick((t) => t + 1);
    } catch (error) { setActionError(error instanceof ApiError ? error.message : String(error)); }
  }
  return (
    <>
      <PageHeader
        title="Automations"
        subtitle="Create recurring work for the main-agent runtime, then edit, pause, run, or delete each schedule here."
      />
      {actionError && <div className="mt-4 rounded-lg bg-bad-soft px-4 py-3 text-[12px] text-bad">{actionError}</div>}
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
                  <th className="whitespace-nowrap">status</th>
                  <th className="whitespace-nowrap">next run</th>
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
                    ? formatInterval(Number(ao.interval_seconds))
                    : "—";
                  return (
                    <tr key={String(ao.id)}>
                      <td className="font-medium">{fmt(ao.name, 40)}</td>
                      <td className="font-mono text-[12px] text-muted">{schedule}</td>
                      <td>{ao.status === "enabled" ? <Pill tone="good">enabled</Pill> : <Pill tone="muted">paused</Pill>}</td>
                      <td className="text-muted">{fmt(ao.next_run_at, 22)}</td>
                      <td className="text-muted">{fmt(ao.goal, 90)}</td>
                      <td>
                        <div className="flex items-center gap-1">
                          <button className="btn-ghost flex items-center gap-1" onClick={() => runNow(String(ao.id))}><Zap size={12} />Run</button>
                          <button className="btn-ghost" onClick={() => setEditing(ao)} aria-label="Edit automation"><Pencil size={12} /></button>
                          <button className="btn-ghost" onClick={() => toggle(String(ao.id), ao.status === "enabled")} aria-label={ao.status === "enabled" ? "Pause" : "Enable"}>{ao.status === "enabled" ? <Pause size={12} /> : <Play size={12} />}</button>
                          <button className="btn-ghost text-bad" onClick={() => remove(String(ao.id))} aria-label="Delete automation"><Trash2 size={12} /></button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </Section>
      {editing && (
        <Modal title="Edit automation" onClose={() => setEditing(null)}>
          <form className="grid gap-3" onSubmit={saveEdit}>
            <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Name</span><input className="input" name="name" defaultValue={String(editing.name || "")} required /></label>
            <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Goal</span><textarea className="input min-h-24" name="goal" defaultValue={String(editing.goal || "")} required /></label>
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Destination</span><select className="select" name="destination_type" defaultValue={String((editing.destination as Record<string, unknown>)?.type || "internal_agent_session")}><option value="internal_agent_session">Internal</option><option value="slack_channel">Slack channel</option><option value="dm">Slack DM</option></select></label>
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Destination ID</span><input className="input" name="destination_id" defaultValue={String((editing.destination as Record<string, unknown>)?.id || "default")} /></label>
            </div>
            <label className="flex items-center gap-2 text-[12px]"><input type="checkbox" name="is_private" defaultChecked={Boolean((editing.destination as Record<string, unknown>)?.is_private)} />Private destination</label>
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Schedule</span><select className="select" name="schedule_kind" defaultValue={String(editing.schedule_kind || "interval")}><option value="interval">Interval</option><option value="cron">Cron</option></select></label>
              <IntervalField defaultSeconds={Number(editing.interval_seconds) || 3600} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Cron expression</span><input className="input font-mono" name="cron_expression" defaultValue={String(editing.cron_expression || "")} placeholder="0 9 * * 1-5" /></label>
              <label className="grid gap-1.5 text-[12px]"><span className="font-medium">Timezone</span><input className="input" name="timezone" defaultValue={String(editing.timezone || "UTC")} /></label>
            </div>
            <button className="btn mt-1">Save changes</button>
          </form>
        </Modal>
      )}
    </>
  );
}
