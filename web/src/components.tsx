import { ReactNode, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-5">
      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-brand">Toda control desk</div>
        <h1 className="m-0 text-[30px] font-semibold tracking-[-0.045em] text-ink">{title}</h1>
        {subtitle && <p className="mt-1 max-w-[620px] text-[13px] text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-none flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Section({ children }: { children: ReactNode }) {
  return <div className="mt-5">{children}</div>;
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 p-6 backdrop-blur-[2px]"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-[440px] shadow-popover"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-head">
          <h2>{title}</h2>
          <button
            className="cursor-pointer rounded-md border-none bg-transparent p-1 leading-none text-muted transition-colors hover:bg-paper hover:text-ink"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

export function Card({
  title,
  meta,
  children,
  className = "",
}: {
  title?: string;
  meta?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`card ${className}`}>
      {title && (
        <div className="card-head">
          <h2>{title}</h2>
          {meta && <span>{meta}</span>}
        </div>
      )}
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="px-4 py-12 text-center text-[12px] text-muted">{children}</div>
  );
}

export function Pill({
  tone,
  children,
}: {
  tone?: "good" | "bad" | "muted" | "orange";
  children: ReactNode;
}) {
  const cls =
    tone === "good"
      ? "pill pill-good"
      : tone === "bad"
      ? "pill pill-bad"
      : tone === "orange"
      ? "pill pill-orange"
      : "pill pill-muted";
  return <span className={cls}>{children}</span>;
}

export function StatePill({ state }: { state: string }) {
  const tone =
    state === "completed"
      ? "good"
      : state === "failed" || state === "cancelled"
      ? "bad"
      : state === "running" || state === "pending"
      ? "orange"
      : "muted";
  return <Pill tone={tone}>{state}</Pill>;
}

/** Compact operational counter used on the control desk. */
export function StatCard({
  icon,
  label,
  value,
  delta,
  deltaTone = "good",
  tone = "brand",
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  delta?: string;
  deltaTone?: "good" | "bad" | "muted";
  tone?: "brand" | "bad" | "orange";
}) {
  const bubble =
    tone === "bad"
      ? "bg-bad-soft text-bad"
      : tone === "orange"
      ? "bg-orange-soft text-orange"
      : "bg-brand-soft text-brand";
  const deltaClass =
    deltaTone === "bad" ? "text-bad" : deltaTone === "muted" ? "text-muted" : "text-good";
  return (
    <div className="card relative flex min-h-[142px] items-start justify-between gap-3 overflow-hidden p-4">
      <div className="absolute inset-x-0 top-0 h-[2px] bg-line"><span className="block h-full w-8 bg-brand" /></div>
      <div className="w-full">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10px] uppercase tracking-wider text-muted">{label}</div>
          <div className={`flex h-7 w-7 items-center justify-center rounded-[2px] ${bubble}`}>{icon}</div>
        </div>
        <div className="mt-5 font-mono text-[27px] font-medium leading-none tracking-tight text-ink tabular-nums">
          {value}
        </div>
        {delta && <div className={`mt-2 text-[11px] font-medium ${deltaClass}`}>{delta}</div>}
      </div>
    </div>
  );
}

/** Polls `callback` on an interval, but only while the tab is actually
 * visible — a backgrounded tab is the common case during real usage, and
 * there's no reason to keep hitting the API for a badge nobody's looking
 * at. Fires immediately on mount and again each time the tab regains
 * visibility, so it never feels stale after coming back. */
export function useVisibleInterval(callback: () => void, ms: number) {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    function start() {
      if (id !== null) return;
      callbackRef.current();
      id = setInterval(() => callbackRef.current(), ms);
    }
    function stop() {
      if (id !== null) {
        clearInterval(id);
        id = null;
      }
    }
    function onVisibilityChange() {
      if (document.hidden) stop();
      else start();
    }
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [ms]);
}

export function useAsync<T>(load: () => Promise<T>, deps: unknown[]) {
  // eslint-disable-next-line
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    load()
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line
  }, deps);
  return { data, error, loading };
}
