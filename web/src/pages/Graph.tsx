import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { ForceGraphMethods, NodeObject } from "react-force-graph-2d";
import { GitBranch, Search, Share2, Workflow } from "lucide-react";
import { api, Json, ORG, PRINCIPAL, fmt } from "../api";
import { useAsync } from "../components";
import { PageHeader, Section, Card, Empty, StatCard } from "../components";

type GraphResponse = { nodes: Json[]; edges: Json[]; permission_decision_id?: string };
type Entity = Record<string, unknown> & { id: string; entity_type?: string; name?: string };
type Edge = Record<string, unknown> & { from_id: string; to_id: string; edge_type?: string };

const TYPE_COLORS: Record<string, string> = {
  person: "#e8720c",
  team: "#1c7a4d",
  project: "#c9a227",
  service: "#7950a7",
  customer: "#cb5b79",
  channel: "#3b6fb0",
  task: "#767d72",
  agent_run: "#c8f24c",
  service_account: "#2e8272",
  organization: "#141a15",
};

function colorForType(type: string): string {
  if (TYPE_COLORS[type]) return TYPE_COLORS[type];
  let hash = 0;
  for (let i = 0; i < type.length; i++) hash = (hash * 31 + type.charCodeAt(i)) >>> 0;
  return `hsl(${hash % 360}, 55%, 48%)`;
}

function useContainerWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(800);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.floor(w));
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

export default function Graph() {
  const { data, error, loading } = useAsync<GraphResponse>(
    () => api.get("/v1/graph/explorer", {
      organization_id: ORG,
      acting_principal_id: PRINCIPAL,
    }), []
  );
  const { data: factsData } = useAsync<{ facts: Json[] }>(
    () => api.get("/v1/facts", { organization_id: ORG, acting_principal_id: PRINCIPAL }), []
  );
  const { data: casesData } = useAsync<{ cases: Json[]; count: number }>(
    () => api.get("/v1/cases", { organization_id: ORG, acting_principal_id: PRINCIPAL }), []
  );

  const fgRef = useRef<ForceGraphMethods<NodeObject<Entity>, any>>();
  const { ref: canvasRef, width } = useContainerWidth<HTMLDivElement>();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Entity | null>(null);
  const [hovered, setHovered] = useState<Entity | null>(null);

  const nodes = (data?.nodes ?? []) as Entity[];
  const edges = (data?.edges ?? []) as Edge[];

  const degree = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of edges) {
      counts.set(e.from_id, (counts.get(e.from_id) ?? 0) + 1);
      counts.set(e.to_id, (counts.get(e.to_id) ?? 0) + 1);
    }
    return counts;
  }, [edges]);

  const graphData = useMemo(
    () => ({
      nodes: nodes.map((n) => ({ ...n })),
      links: edges
        .filter((e) => nodes.some((n) => n.id === e.from_id) && nodes.some((n) => n.id === e.to_id))
        .map((e) => ({ ...e, source: e.from_id, target: e.to_id })),
    }),
    [nodes, edges]
  );

  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of nodes) {
      const type = String(n.entity_type || "unknown");
      counts.set(type, (counts.get(type) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [nodes]);

  const topDegreeIds = useMemo(() => {
    return new Set(
      [...degree.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([id]) => id)
    );
  }, [degree]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return nodes.filter((n) => String(n.name ?? "").toLowerCase().includes(q)).slice(0, 8);
  }, [nodes, query]);

  function focusNode(node: Entity) {
    setSelected(node);
    const target = graphData.nodes.find((n) => n.id === node.id) as NodeObject<Entity> | undefined;
    if (target && fgRef.current && typeof target.x === "number" && typeof target.y === "number") {
      fgRef.current.centerAt(target.x, target.y, 600);
      fgRef.current.zoom(3, 600);
    }
  }

  const neighborEdges = selected
    ? edges.filter((e) => e.from_id === selected.id || e.to_id === selected.id)
    : [];

  return (
    <>
      <PageHeader
        title="Graph"
        subtitle="People, teams, projects, services, customers, ownership, membership, and dependencies — the permission-filtered relationship graph behind every answer."
      />

      <Section>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard icon={<Share2 size={17} />} label="Entities" value={nodes.length} delta="in this authorized view" deltaTone="muted" />
          <StatCard icon={<Workflow size={17} />} label="Relationships" value={edges.length} delta="edges between entities" deltaTone="muted" />
          <StatCard icon={<GitBranch size={17} />} label="Cases · facts" value={`${casesData?.count ?? 0} · ${factsData?.facts?.length ?? 0}`} delta="correlated situations · bitemporal facts" deltaTone="muted" />
        </div>
      </Section>

      {error && <Section><Card><Empty>error: {error}</Empty></Card></Section>}

      <Section>
        <Card>
          <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
            <div className="relative w-full max-w-[280px]">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input
                className="input w-full pl-9"
                placeholder="Find an entity…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              {matches.length > 0 && (
                <div className="card absolute left-0 right-0 top-[calc(100%+6px)] z-20 max-h-[240px] overflow-auto p-1 shadow-popover">
                  {matches.map((m) => (
                    <button
                      key={m.id}
                      className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] hover:bg-brand-soft"
                      onClick={() => {
                        focusNode(m);
                        setQuery("");
                      }}
                    >
                      <span className="h-2 w-2 flex-none rounded-full" style={{ background: colorForType(String(m.entity_type)) }} />
                      {String(m.name)}
                      <span className="ml-auto text-[11px] text-muted">{String(m.entity_type)}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {typeCounts.map(([type, count]) => (
                <span key={type} className="flex items-center gap-1.5 text-[11px] text-muted">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: colorForType(type) }} />
                  {type} · {count}
                </span>
              ))}
            </div>
          </div>

          <div ref={canvasRef} className="relative h-[560px] w-full overflow-hidden">
            {loading && <Empty>loading graph…</Empty>}
            {data && nodes.length === 0 && (
              <div className="flex h-full items-center justify-center">
                <Empty>No authorized entities yet — the graph fills in as evidence is ingested and materialized.</Empty>
              </div>
            )}
            {nodes.length > 0 && (
              <ForceGraph2D
                ref={fgRef}
                width={width}
                height={560}
                graphData={graphData}
                backgroundColor="#ffffff"
                nodeRelSize={3}
                nodeVal={(n: any) => 2 + Math.min(degree.get(n.id) ?? 0, 12)}
                nodeColor={(n: any) => colorForType(String(n.entity_type))}
                linkColor={() => "#d8dbd3"}
                linkWidth={1}
                linkDirectionalArrowLength={3}
                linkDirectionalArrowRelPos={1}
                onNodeClick={(n: any) => focusNode(n)}
                onNodeHover={(n: any) => setHovered(n)}
                cooldownTicks={80}
                nodeCanvasObject={(node: any, ctx, globalScale) => {
                  const r = 2 + Math.min(degree.get(node.id) ?? 0, 12);
                  ctx.beginPath();
                  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
                  ctx.fillStyle = colorForType(String(node.entity_type));
                  ctx.fill();
                  const showLabel =
                    topDegreeIds.has(node.id) ||
                    node.id === hovered?.id ||
                    node.id === selected?.id;
                  if (showLabel) {
                    const label = String(node.name ?? "");
                    const fontSize = Math.max(11 / globalScale, 3.2);
                    ctx.font = `${fontSize}px Manrope, sans-serif`;
                    ctx.textAlign = "left";
                    ctx.textBaseline = "middle";
                    ctx.fillStyle = "#141a15";
                    ctx.fillText(label, node.x + r + 3, node.y);
                  }
                }}
              />
            )}
            {selected && (
              <div className="card absolute bottom-4 right-4 w-[280px] shadow-popover">
                <div className="card-head">
                  <h2>{String(selected.name)}</h2>
                  <button className="text-[11px] text-muted hover:text-ink" onClick={() => setSelected(null)}>close</button>
                </div>
                <div className="grid gap-2 p-4 text-[12px]">
                  <div className="flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-full" style={{ background: colorForType(String(selected.entity_type)) }} />
                    <span className="text-muted">{String(selected.entity_type)} · {selected.id}</span>
                  </div>
                  {!!(selected.aliases as string[] | undefined)?.length && (
                    <div className="text-muted">aliases: {fmt(selected.aliases, 80)}</div>
                  )}
                  <div className="text-muted">{neighborEdges.length} connection{neighborEdges.length === 1 ? "" : "s"}</div>
                  {neighborEdges.slice(0, 6).map((e, i) => (
                    <div key={i} className="rounded-lg bg-paper px-2.5 py-1.5 font-mono text-[10px] text-ink">
                      {e.from_id === selected.id ? "→" : "←"} {e.edge_type} {e.from_id === selected.id ? e.to_id : e.from_id}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Card>
      </Section>
    </>
  );
}
