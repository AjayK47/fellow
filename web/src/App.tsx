import { NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Approvals from "./pages/Approvals";
import Policies from "./pages/Policies";
import Tools from "./pages/Tools";
import Skills from "./pages/Skills";
import Setup from "./pages/Setup";
import Connections from "./pages/Connections";
import Channels from "./pages/Channels";
import Automations from "./pages/Automations";
import Graph from "./pages/Graph";
import Reality from "./pages/Reality";
import Jobs from "./pages/Jobs";
import Audit from "./pages/Audit";

type NavItem = { to: string; label: string; end?: boolean };
type NavGroup = { title: string; items: NavItem[] };

const GROUPS: NavGroup[] = [
  {
    title: "Observe",
    items: [
      { to: "/", label: "Overview", end: true },
      { to: "/runs", label: "Runs" },
      { to: "/approvals", label: "Approvals" },
    ],
  },
  {
    title: "Govern",
    items: [
      { to: "/policies", label: "Policies" },
      { to: "/tools", label: "Tools" },
      { to: "/skills", label: "Skills" },
    ],
  },
  {
    title: "Wire",
    items: [
      { to: "/setup", label: "Setup" },
      { to: "/connections", label: "Connections" },
      { to: "/channels", label: "Channels" },
      { to: "/automations", label: "Automations" },
    ],
  },
  {
    title: "Know",
    items: [
      { to: "/graph", label: "Graph" },
      { to: "/reality", label: "Reality" },
    ],
  },
  {
    title: "Operate",
    items: [
      { to: "/jobs", label: "Jobs" },
      { to: "/audit", label: "Audit" },
    ],
  },
];

export default function App() {
  return (
    <div className="grid h-screen grid-cols-[248px_1fr] overflow-hidden">
      <aside className="flex flex-col overflow-y-auto bg-ink text-paper p-6">
        <div className="flex items-center gap-3 font-bold tracking-tight">
          <i className="block h-6 w-6 rounded-[50%_50%_42%_58%] bg-acid" />
          <span>Docket</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted">
            by Ajay
          </span>
        </div>
        <nav className="mt-10 grid gap-5">
          {GROUPS.map((g) => (
            <div key={g.title} className="grid gap-1.5">
              <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
                {g.title}
              </div>
              {g.items.map((it) => (
                <NavLink
                  key={it.to}
                  to={it.to}
                  end={it.end}
                  className={({ isActive }) =>
                    `border-b border-[#292c27] py-2.5 font-mono text-[13px] no-underline transition-colors ${
                      isActive ? "text-acid" : "text-[#8d9086] hover:text-paper"
                    }`
                  }
                >
                  {it.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="mt-auto border-t border-[#3b3e38] pt-5 font-mono text-[11px] text-muted">
          <a
            href="/docs"
            target="_blank"
            rel="noreferrer"
            className="text-[#8d9086] no-underline hover:text-acid"
          >
            API docs ↗
          </a>
        </div>
      </aside>
      <main className="overflow-y-auto">
        <div className="mx-auto w-full max-w-[1500px] p-8 pb-20">
          <Routes>
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:runId" element={<RunDetail />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/policies" element={<Policies />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/setup" element={<Setup />} />
            <Route path="/connections" element={<Connections />} />
            <Route path="/channels" element={<Channels />} />
            <Route path="/automations" element={<Automations />} />
            <Route path="/graph" element={<Graph />} />
            <Route path="/reality" element={<Reality />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/" element={<Overview />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}