import { useEffect, useState } from "react";
import { KeyRound, Slack } from "lucide-react";
import { fetchAuthMethods, localLogin } from "../api";

export default function Login({ error }: { error?: string | null }) {
  const [methods, setMethods] = useState({ local_token: false, slack: false });
  const [token, setToken] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    fetchAuthMethods().then(setMethods).catch(() => undefined);
  }, []);

  async function signInWithToken(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setLocalError(null);
    try {
      await localLogin(token);
      window.location.assign("/");
    } catch {
      setLocalError("That administrator token was not accepted.");
    } finally {
      setSubmitting(false);
    }
  }
  const message =
    error === "invalid_state"
      ? "That sign-in link expired or was already used — try again."
      : error === "token_exchange_failed" || error === "incomplete_userinfo"
      ? "Slack didn't return what we needed to sign you in. Try again."
      : error === "session_expired"
      ? "You were signed out — sign in again to continue."
      : error
      ? "Sign-in was cancelled or failed."
      : null;

  return (
    <div className="flex h-screen items-center justify-center bg-paper">
      <div className="card w-full max-w-[380px] p-8 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-brand-soft text-brand">
          <KeyRound size={22} />
        </div>
        <h1 className="mt-4 text-[20px] font-semibold tracking-tight text-ink">
          Sign in to Toda
        </h1>
        <p className="mt-2 text-[13px] leading-relaxed text-muted">
          Use the administrator credential created by this self-hosted installation.
        </p>
        {message && (
          <p className="mt-3 rounded-lg bg-bad-soft px-3 py-2 text-[12.5px] text-bad">
            {message}
          </p>
        )}
        {methods.local_token && (
          <form onSubmit={signInWithToken} className="mt-6 space-y-3 text-left">
            <label className="block text-[12px] font-medium text-muted" htmlFor="admin-token">
              Administrator token
            </label>
            <input
              id="admin-token"
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              className="w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink outline-none focus:border-brand"
              required
            />
            {localError && <p className="text-[12px] text-bad">{localError}</p>}
            <button disabled={submitting} className="btn flex w-full items-center justify-center gap-2">
              <KeyRound size={15} /> {submitting ? "Signing in…" : "Sign in locally"}
            </button>
          </form>
        )}
        {!methods.local_token && !methods.slack && (
          <div className="mt-6 border-l-2 border-brand bg-brand-soft px-4 py-3 text-left text-[12px] leading-relaxed text-ink">
            Run <code className="font-mono">toda setup</code> on the host machine to establish local administrator access.
          </div>
        )}
        {methods.slack && (
          <a
            href="/slack/login"
            className="btn mt-3 flex w-full items-center justify-center gap-2"
          >
            <Slack size={15} /> Sign in with Slack
          </a>
        )}
      </div>
    </div>
  );
}
