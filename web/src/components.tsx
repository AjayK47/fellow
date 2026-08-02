import { ReactNode } from "react";

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
    <header className="flex items-start justify-between gap-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
          Docket Console
        </div>
        <h1 className="m-0 mt-3 text-[clamp(34px,4.5vw,58px)] leading-[0.9] tracking-tight">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-3 max-w-[640px] font-mono text-[13px] text-muted">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
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
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-[440px]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-head">
          <h2>{title}</h2>
          <button
            className="cursor-pointer border-none bg-transparent font-mono text-[16px] leading-none text-muted hover:text-ink"
            onClick={onClose}
            aria-label="Close"
          >
            ×
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
    <div className="px-4 py-12 text-center font-mono text-[12px] text-muted">{children}</div>
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

import { useEffect, useState } from "react";