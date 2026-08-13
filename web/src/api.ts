const BASE = "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Every /v1 data route is bound to the signed-in workspace server-side, so
// an expired or cleared login turns every request into a 401. Without a
// hook for that the console would sit on a stale page showing empty tables
// and failed refreshes forever, with nothing telling the person to sign in
// again. App registers a handler that drops back to the login screen.
let unauthorizedHandler: (() => void) | null = null;

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler;
}

/** Pull the human-readable sentence out of an error body.
 *
 * FastAPI answers with `{"detail": "..."}`, and stringifying that put raw JSON
 * in front of people ({"detail":"The installation owner already exists"}).
 * Validation failures use a list of `{loc, msg}` instead, so both shapes are
 * unwrapped here rather than at every call site. */
function errorMessage(data: unknown, status: number): string {
  if (typeof data === "string" && data.trim()) return data;
  if (data && typeof data === "object") {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: unknown } | undefined;
      if (typeof first?.msg === "string") return first.msg;
    }
    const message = (data as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return `Request failed (${status})`;
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
    // /v1/session is how "am I signed in?" is *asked* — a 401 from it is an
    // answer, not a session that just expired, so it must not recurse into
    // the handler that re-asks.
    if (res.status === 401 && path !== "/v1/session") unauthorizedHandler?.();
    throw new ApiError(res.status, errorMessage(data, res.status));
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
  user_id: string;
  email: string;
  name: string;
  role: "owner" | "admin" | "member";
  auth_disabled: boolean;
}

export async function login(email: string, password: string): Promise<DashboardSession> {
  const session = await request<DashboardSession>("POST", "/v1/auth/login", { email, password });
  ORG = session.organization_id;
  PRINCIPAL = session.principal_id;
  return session;
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
