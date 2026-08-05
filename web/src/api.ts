const BASE = "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  params?: Record<string, string | number | undefined>
): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url.pathname + url.search, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, typeof data === "string" ? data : JSON.stringify(data));
  }
  return data as T;
}

export const api = {
  get: <T>(p: string, params?: Record<string, string | number | undefined>) =>
    request<T>("GET", p, undefined, params),
  post: <T>(p: string, body?: unknown) => request<T>("POST", p, body),
  put: <T>(p: string, body?: unknown) => request<T>("PUT", p, body),
  patch: <T>(p: string, body?: unknown) => request<T>("PATCH", p, body),
  del: <T>(p: string) => request<T>("DELETE", p),
};

// Populated once at boot from GET /v1/session (see fetchSession below).
// Exported as `let`, not `const`: ES module bindings are live, so every
// page that imported ORG/PRINCIPAL before session load sees the resolved
// values automatically once fetchSession() assigns them, with no need to
// route every one of the ~45 existing call sites through a context.
export let ORG = "";
export let PRINCIPAL = "";

export interface DashboardSession {
  organization_id: string;
  principal_id: string;
  workspace_name: string | null;
  auth_disabled: boolean;
}

/** Resolves the signed-in workspace/person from the server session cookie.
 * Returns null (never throws) on 401 — "not signed in" — so callers can
 * render a login page instead of treating it as a transient fetch error. */
export async function fetchSession(): Promise<DashboardSession | null> {
  try {
    const session = await request<DashboardSession>("GET", "/v1/session");
    ORG = session.organization_id;
    PRINCIPAL = session.principal_id;
    return session;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export async function logout(): Promise<void> {
  await request("POST", "/v1/session/logout");
}

export type Json = Record<string, unknown> | unknown[];

export function fmt(value: unknown, max = 160): string {
  if (value === null || value === undefined) return "—";
  const s = typeof value === "object" ? JSON.stringify(value) : String(value);
  const collapsed = s.replace(/\s+/g, " ");
  return collapsed.length > max ? collapsed.slice(0, max) + " …" : collapsed;
}

export function reltime(iso: string | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = Date.now() - t;
  const abs = Math.abs(diff);
  const future = diff < 0;
  const m = abs / 60000;
  if (m < 1) return future ? "in a moment" : "just now";
  if (m < 60) return `${future ? "in " : ""}${Math.round(m)}m${future ? "" : " ago"}`;
  const h = m / 60;
  if (h < 24) return `${future ? "in " : ""}${Math.round(h)}h${future ? "" : " ago"}`;
  const d = h / 24;
  if (d < 30) return `${future ? "in " : ""}${Math.round(d)}d${future ? "" : " ago"}`;
  return new Date(t).toISOString().slice(0, 16).replace("T", " ");
}