import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { ApiError, login } from "../api";

export default function Login({ error }: { error?: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setLocalError(null);
    try {
      await login(email, password);
      window.location.assign("/");
    } catch (cause) {
      setLocalError(cause instanceof ApiError && cause.status === 403
        ? "This account is still waiting for access."
        : "That email and password were not accepted.");
    } finally {
      setSubmitting(false);
    }
  }

  const message =
    error === "invalid_state"
      ? "That link expired or was already used. Sign in again."
      : error === "session_expired"
      ? "You were signed out. Sign in again to continue."
      : error
      ? "Sign-in did not complete. Try again."
      : null;

  return (
    <main className="ob ob--auth">
      <section className="ob__card">
        <a href="/" className="ob__mark" aria-label="Toda home">toda</a>
        <h1>Sign in</h1>
        <p>Use your organization account. Slack connects your data — it does not control who gets in here.</p>

        {message && <div className="ob__note is-bad">{message}</div>}

        <form onSubmit={signIn} className="ob__form">
          <label className="ob__field">
            <span>Work email</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@company.com"
              required
            />
          </label>
          <label className="ob__field">
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {localError && <span className="ob__feedback is-bad">{localError}</span>}
          <button className="ob__primary" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <a href="/request-access" className="ob__link">
          Request access <ArrowRight size={13} />
        </a>
      </section>
    </main>
  );
}
