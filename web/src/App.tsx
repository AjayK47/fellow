import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import {
  Activity,
  Bell,
  BookOpen,
  CheckCircle2,
  Clock,
  Database,
  ExternalLink,
  Kanban,
  ListChecks,
  LogOut,
  Menu,
  MessageSquare,
  Plug,
  ScrollText,
  Search,
  Server,
  Settings2,
  Share2,
  ShieldCheck,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import Overview from "./pages/Overview";
import Sessions from "./pages/Sessions";
import SessionDetail from "./pages/SessionDetail";
import Approvals from "./pages/Approvals";
import Policies from "./pages/Policies";
import Tools from "./pages/Tools";
import Skills from "./pages/Skills";
import Setup from "./pages/Setup";
import Connections from "./pages/Connections";
import Channels from "./pages/Channels";
import Automations from "./pages/Automations";
import MCP from "./pages/MCP";
import Tasks from "./pages/Tasks";
import TaskDetail from "./pages/TaskDetail";
import Graph from "./pages/Graph";
import Reality from "./pages/Reality";
import Jobs from "./pages/Jobs";
import Audit from "./pages/Audit";
import Login from "./pages/Login";
import { api, ORG, fetchSession, logout, onUnauthorized, DashboardSession } from "./api";
import { useVisibleInterval } from "./components";

type NavItem = { to: string; label: string; end?: boolean; icon: typeof Activity };
type NavGroup = { title: string; items: NavItem[] };

const GROUPS: NavGroup[] = [
  {
    title: "Observe",
    items: [
      { to: "/", label: "Dashboard", end: true, icon: Activity },
      { to: "/sessions", label: "Sessions", icon: Sparkles },
      { to: "/approvals", label: "Approvals", icon: CheckCircle2 },
    ],
  },
  {
    title: "Govern",
    items: [
      { to: "/policies", label: "Policies", icon: ShieldCheck },
      { to: "/tools", label: "Tools", icon: Wrench },
      { to: "/skills", label: "Skills", icon: BookOpen },
    ],
  },
  {
    title: "Wire",
    items: [
      { to: "/setup", label: "Setup", icon: Settings2 },
      { to: "/connections", label: "Integrations", icon: Plug },
      { to: "/channels", label: "Channels", icon: MessageSquare },
      { to: "/automations", label: "Automations", icon: Clock },
      { to: "/mcp", label: "MCP servers", icon: Server },
      { to: "/tasks", label: "Tasks", icon: Kanban },
    ],
  },
  {
    title: "Know",
    items: [
      { to: "/graph", label: "Graph", icon: Share2 },
      { to: "/reality", label: "Reality", icon: Database },
    ],
  },
];

const OPERATIONS: NavItem[] = [
  { to: "/jobs", label: "Jobs", icon: ListChecks },
  { to: "/audit", label: "Audit", icon: ScrollText },
];

const ALL_NAV = [...GROUPS.flatMap((g) => g.items), ...OPERATIONS];

function NavRow({ item, badge, onNavigate }: { item: NavItem; badge?: number; onNavigate?: () => void }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        `group flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium no-underline transition-colors ${
          isActive ? "bg-brand-soft text-brand" : "text-ink/70 hover:bg-paper hover:text-ink"
        }`
      }
    >
      {({ isActive }) => (
        <>
          <Icon size={16} strokeWidth={isActive ? 2.4 : 2} className="flex-none" />
          <span className="flex-1">{item.label}</span>
          {!!badge && (
            <span className="flex h-4 min-w-[16px] items-center justify-center rounded-full bg-bad px-1 text-[9px] font-semibold text-white">
              {badge > 99 ? "99+" : badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  );
}

function Sidebar({
  pendingApprovals,
  workspaceName,
  showSignOut,
  onSignOut,
  onNavigate,
}: {
  pendingApprovals: number;
  workspaceName?: string | null;
  showSignOut: boolean;
  onSignOut: () => void;
  onNavigate?: () => void;
}) {
  return (
    <aside className="flex h-screen flex-col overflow-y-auto border-r border-line bg-panel px-4 py-5">
      <Link to="/" className="flex items-center gap-2.5 px-2 no-underline">
        <span className="flex h-8 w-8 items-center justify-center rounded-[10px] bg-brand text-acid">
          <Sparkles size={16} strokeWidth={2.4} />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-ink">Toda</span>
      </Link>

      <nav className="mt-8 grid gap-6">
        {GROUPS.map((g) => (
          <div key={g.title} className="grid gap-0.5">
            <div className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
              {g.title}
            </div>
            {g.items.map((it) => (
              <NavRow
                key={it.to}
                item={it}
                badge={it.to === "/approvals" ? pendingApprovals : 0}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        ))}
      </nav>

      <div className="mt-6 grid gap-0.5 border-t border-line pt-5">
        <div className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
          Operations
        </div>
        {OPERATIONS.map((it) => (
          <NavRow key={it.to} item={it} onNavigate={onNavigate} />
        ))}
      </div>

      <div className="mt-auto grid gap-3 border-t border-line pt-5">
        <a
          href="/docs"
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-2 px-3 text-[12px] font-medium text-muted no-underline hover:text-brand"
        >
          API docs <ExternalLink size={12} />
        </a>
        <div className="flex items-center gap-2.5 rounded-lg border border-line bg-paper px-3 py-2.5">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-brand-dark text-[11px] font-semibold text-white">
            {ORG.slice(-2).toUpperCase()}
          </span>
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-[12px] font-medium text-ink">{workspaceName || ORG}</div>
            <div className="text-[10px] text-muted">organization</div>
          </div>
          {showSignOut && (
            <button
              onClick={onSignOut}
              className="flex h-7 w-7 flex-none items-center justify-center rounded-md text-muted transition-colors hover:bg-line/60 hover:text-ink"
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut size={14} />
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}

function NavSearch() {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return ALL_NAV.filter((it) => it.label.toLowerCase().includes(q)).slice(0, 6);
  }, [query]);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function go(item: NavItem) {
    navigate(item.to);
    setQuery("");
    setOpen(false);
  }

  return (
    <div ref={boxRef} className="relative w-full max-w-[380px]">
      <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
      <input
        className="input w-full pl-9"
        placeholder="Search pages, tasks, or tools…"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && matches[0]) go(matches[0]);
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && matches.length > 0 && (
        <div className="card absolute left-0 right-0 top-[calc(100%+6px)] z-40 overflow-hidden p-1 shadow-popover">
          {matches.map((m) => {
            const Icon = m.icon;
            return (
              <button
                key={m.to}
                className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] text-ink transition-colors hover:bg-brand-soft hover:text-brand"
                onClick={() => go(m)}
              >
                <Icon size={14} />
                {m.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Topbar({ pendingApprovals, onOpenNavigation }: { pendingApprovals: number; onOpenNavigation: () => void }) {
  return (
    <div className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-panel/90 px-4 py-3.5 backdrop-blur sm:px-6 lg:px-8">
      <button
        type="button"
        className="flex h-9 w-9 flex-none items-center justify-center rounded-lg border border-line bg-panel text-ink/70 transition-colors hover:bg-paper hover:text-ink lg:hidden"
        onClick={onOpenNavigation}
        aria-label="Open navigation"
      >
        <Menu size={17} />
      </button>
      <NavSearch />
      <div className="ml-auto flex items-center gap-2">
        <Link
          to="/approvals"
          className="relative flex h-9 w-9 items-center justify-center rounded-full border border-line bg-panel text-ink/70 no-underline transition-colors hover:bg-paper hover:text-ink"
          aria-label="Pending approvals"
        >
          <Bell size={16} />
          {pendingApprovals > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-bad px-1 text-[9px] font-semibold text-white">
              {pendingApprovals > 99 ? "99+" : pendingApprovals}
            </span>
          )}
        </Link>
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand-dark text-[11px] font-semibold text-white">
          {ORG.slice(-2).toUpperCase()}
        </div>
      </div>
    </div>
  );
}

type SessionState = "pending" | DashboardSession | null;

export default function App() {
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const [session, setSession] = useState<SessionState>("pending");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const err = params.get("error");
    if (err) {
      setLoginError(err);
      window.history.replaceState({}, "", window.location.pathname);
    }
    // A login that expires mid-session turns every subsequent request into
    // a 401; without this the console would keep rendering a stale page
    // whose every refresh silently fails. Dropping session state sends the
    // person back to the login screen instead.
    onUnauthorized(() => {
      setLoginError("session_expired");
      setSession(null);
    });
    fetchSession()
      .then(setSession)
      .catch(() => setSession(null));
  }, []);

  const pollApprovals = useCallback(async () => {
    if (!session || session === "pending") return;
    try {
      const res = await api.get<{ count: number }>("/v1/approvals", {
        organization_id: ORG,
        status: "pending",
      });
      setPendingApprovals(res.count ?? 0);
    } catch {
      // transient — next poll retries
    }
  }, [session]);
  // Pending-approval count is a passive badge, not the actual notification —
  // approvals are delivered to Slack with Approve/Deny buttons regardless.
  // 20s (and paused entirely while the tab isn't visible) is plenty.
  useVisibleInterval(pollApprovals, 20_000);

  if (session === "pending") return null;
  if (!session) return <Login error={loginError} />;

  async function handleSignOut() {
    await logout();
    window.location.href = "/";
  }

  return (
    <div className="grid h-screen grid-cols-1 overflow-hidden bg-paper lg:grid-cols-[256px_1fr]">
      <div className="hidden lg:block">
        <Sidebar
          pendingApprovals={pendingApprovals}
          workspaceName={session.workspace_name}
          showSignOut={!session.auth_disabled}
          onSignOut={handleSignOut}
        />
      </div>

      {mobileNavigationOpen && (
        <div
          className="fixed inset-0 z-50 bg-ink/45 backdrop-blur-[2px] lg:hidden"
          onClick={() => setMobileNavigationOpen(false)}
        >
          <div
            className="relative h-full w-[min(320px,calc(100vw-32px))] bg-panel shadow-popover"
            onClick={(event) => event.stopPropagation()}
          >
            <Sidebar
              pendingApprovals={pendingApprovals}
              workspaceName={session.workspace_name}
              showSignOut={!session.auth_disabled}
              onSignOut={handleSignOut}
              onNavigate={() => setMobileNavigationOpen(false)}
            />
            <button
              type="button"
              className="absolute right-3 top-3 flex h-10 w-10 items-center justify-center rounded-lg text-muted transition-colors hover:bg-paper hover:text-ink"
              onClick={() => setMobileNavigationOpen(false)}
              aria-label="Close navigation"
            >
              <X size={18} />
            </button>
          </div>
        </div>
      )}

      <div className="grid h-screen grid-rows-[auto_1fr] overflow-hidden">
        <Topbar pendingApprovals={pendingApprovals} onOpenNavigation={() => setMobileNavigationOpen(true)} />
        <main className="overflow-y-auto">
          <div className="mx-auto w-full max-w-[1400px] p-4 pb-20 sm:p-6 sm:pb-20 lg:p-8 lg:pb-20">
            <Routes>
              <Route path="/sessions" element={<Sessions />} />
              <Route path="/sessions/:sessionId" element={<SessionDetail />} />
              <Route path="/approvals" element={<Approvals />} />
              <Route path="/policies" element={<Policies />} />
              <Route path="/tools" element={<Tools />} />
              <Route path="/skills" element={<Skills />} />
              <Route path="/setup" element={<Setup />} />
              <Route path="/connections" element={<Connections />} />
              <Route path="/channels" element={<Channels />} />
              <Route path="/automations" element={<Automations />} />
              <Route path="/mcp" element={<MCP />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/tasks/:taskId" element={<TaskDetail />} />
              <Route path="/graph" element={<Graph />} />
              <Route path="/reality" element={<Reality />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/audit" element={<Audit />} />
              <Route path="/" element={<Overview />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  );
}
