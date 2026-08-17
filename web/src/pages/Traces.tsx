import { useState } from "react";
import { ChevronDown, ChevronRight, CircleAlert } from "lucide-react";
import { api, ORG, fmt, reltime } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, Pill } from "../components";

type TraceRow = {
  trace_id: string;
  started_at: string;
  ended_at: string;
  event_count: number;
  failures: number;
  root_operation: string | null;
  duration_ms: number | null;
};

type Span = {
  span_id: string;
  parent_span_id: string | null;
  operation: string;
  component: string;
  phase: string;
  started_at: string;
  duration_ms: number | null;
  context: Record<string, unknown>;
  input: unknown;
  output: unknown;
  error: unknown;
  children: Span[];
};

type TraceList = { traces: TraceRow[]; count: number };
type TraceDetail = { trace_id: string; tree: Span[]; count: number };

/** Milliseconds are the unit the recorder stores, but not the unit anyone
 * reads. A model call is seconds; a database write is a fraction of one. */
function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
}

/** The slowest step in a trace is the one worth finding, so every bar is
 * drawn relative to the slowest span in that trace rather than to its own
 * parent — the eye should land on the bottleneck without reading numbers. */
function widestSpan(spans: Span[]): number {
  return spans.reduce(
    (max, span) => Math.max(max, span.duration_ms ?? 0, widestSpan(span.children)),
    0,
  );
}

const COMPONENT_TONE: Record<string, string> = {
  slack: "bg-[#4A9D7E]",
  router: "bg-[#7C6BB8]",
  session: "bg-[#C08A3E]",
  agent_runtime: "bg-[#3E7CC0]",
  model_client: "bg-[#B85C8A]",
  tools: "bg-[#5CA5B8]",
  queue: "bg-[#8A8A8A]",
};

function SpanRow({
  span,
  depth,
  widest,
}: {
  span: Span;
  depth: number;
  widest: number;
}) {
  const [open, setOpen] = useState(depth < 2);
  const [showPayload, setShowPayload] = useState(false);
  const failed = span.phase === "failed";
  const hasChildren = span.children.length > 0;
  const pct = widest > 0 ? Math.max(1, ((span.duration_ms ?? 0) / widest) * 100) : 0;
  const tone = COMPONENT_TONE[span.component] ?? "bg-[#8A8A8A]";

  return (
    <>
      <div
        className={`flex items-center gap-2 border-b border-line/60 py-1.5 pr-3 text-[12px] hover:bg-paper ${
          failed ? "bg-bad/5" : ""
        }`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
      >
        <button
          className={`flex-none cursor-pointer border-none bg-transparent p-0 leading-none ${
            hasChildren ? "text-muted hover:text-ink" : "invisible"
          }`}
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? "Collapse" : "Expand"}
        >
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>

        {failed && <CircleAlert size={12} className="flex-none text-bad" />}

        <button
          className="flex-none cursor-pointer border-none bg-transparent p-0 font-mono text-[12px] text-ink hover:underline"
          onClick={() => setShowPayload((v) => !v)}
          title="Show what went in and what came back"
        >
          {span.operation}
        </button>

        <span className="flex-none font-mono text-[10px] uppercase tracking-wide text-muted">
          {span.component}
        </span>

        <div className="mx-2 h-[6px] min-w-[40px] flex-1 rounded-full bg-line/50">
          <div
            className={`h-full rounded-full ${failed ? "bg-bad" : tone}`}
            style={{ width: `${pct}%` }}
          />
        </div>

        <span className="flex-none font-mono text-[11px] tabular-nums text-muted">
          {duration(span.duration_ms)}
        </span>
      </div>

      {showPayload && (
        <div
          className="border-b border-line/60 bg-paper py-2 pr-3 text-[11px]"
          style={{ paddingLeft: `${28 + depth * 16}px` }}
        >
          {span.error != null && (
            <div className="mb-2">
              <div className="mb-0.5 font-mono text-[10px] uppercase tracking-wide text-bad">error</div>
              <pre className="m-0 overflow-x-auto whitespace-pre-wrap break-words text-[11px] text-bad">
                {JSON.stringify(span.error, null, 2)}
              </pre>
            </div>
          )}
          {(["input", "output"] as const).map((field) =>
            span[field] == null ? null : (
              <div key={field} className="mb-2 last:mb-0">
                <div className="mb-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">
                  {field}
                </div>
                <pre className="m-0 max-h-[280px] overflow-auto whitespace-pre-wrap break-words text-[11px] text-ink/80">
                  {JSON.stringify(span[field], null, 2)}
                </pre>
              </div>
            ),
          )}
          {Object.keys(span.context).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {Object.entries(span.context)
                .filter(([key]) => key !== "trace_id" && key !== "span_id")
                .map(([key, value]) => (
                  <span
                    key={key}
                    className="rounded bg-line/40 px-1.5 py-0.5 font-mono text-[10px] text-muted"
                  >
                    {key}={fmt(value, 40)}
                  </span>
                ))}
            </div>
          )}
        </div>
      )}

      {open &&
        span.children.map((child) => (
          <SpanRow key={child.span_id} span={child} depth={depth + 1} widest={widest} />
        ))}
    </>
  );
}

function TraceTree({ traceId }: { traceId: string }) {
  const { data, error, loading } = useAsync<TraceDetail>(
    () => api.get(`/v1/traces/${traceId}`, { organization_id: ORG }),
    [traceId],
  );
  if (loading) return <Empty>loading…</Empty>;
  if (error) return <Empty>error: {error}</Empty>;
  if (!data?.tree.length) return <Empty>no spans recorded</Empty>;
  const widest = widestSpan(data.tree);
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[560px]">
        {data.tree.map((span) => (
          <SpanRow key={span.span_id} span={span} depth={0} widest={widest} />
        ))}
      </div>
    </div>
  );
}

export default function Traces() {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const { data, error, loading } = useAsync<TraceList>(
    () => api.get("/v1/traces", { organization_id: ORG, errors_only: errorsOnly ? "true" : undefined }),
    [errorsOnly],
  );

  return (
    <>
      <PageHeader
        title="Traces"
        subtitle="Every step Toda took in response to one message — routing, dispatch, each model call and tool call, nested and timed. Click a step to see exactly what went in and what came back."
        actions={
          <label className="flex cursor-pointer items-center gap-2 text-[12px] text-muted">
            <input
              type="checkbox"
              checked={errorsOnly}
              onChange={(e) => setErrorsOnly(e.target.checked)}
            />
            failures only
          </label>
        }
      />
      <Section>
        <Card title="Recent" meta={String(data?.count ?? 0)}>
          {loading && <Empty>loading…</Empty>}
          {error && <Empty>error: {error}</Empty>}
          {data && data.traces.length === 0 && (
            <Empty>
              no traces yet — send Toda a message and it will appear here
            </Empty>
          )}
          {data?.traces.map((trace) => (
            <div key={trace.trace_id} className="border-b border-line last:border-b-0">
              <button
                className="flex w-full cursor-pointer items-center gap-3 border-none bg-transparent px-4 py-3 text-left hover:bg-paper"
                onClick={() =>
                  setSelected((current) => (current === trace.trace_id ? null : trace.trace_id))
                }
              >
                {selected === trace.trace_id ? (
                  <ChevronDown size={14} className="flex-none text-muted" />
                ) : (
                  <ChevronRight size={14} className="flex-none text-muted" />
                )}
                <span className="flex-1 font-mono text-[12px] text-ink">
                  {trace.root_operation ?? "—"}
                </span>
                {trace.failures > 0 && (
                  <Pill tone="bad">
                    {trace.failures} failed
                  </Pill>
                )}
                <span className="flex-none text-[11px] text-muted">
                  {trace.event_count} steps
                </span>
                <span className="flex-none text-[11px] text-muted">
                  {reltime(trace.started_at)}
                </span>
              </button>
              {selected === trace.trace_id && (
                <div className="border-t border-line bg-white pb-2">
                  <TraceTree traceId={trace.trace_id} />
                </div>
              )}
            </div>
          ))}
        </Card>
      </Section>
    </>
  );
}
