import { Slack } from "lucide-react";

export default function Login({ error }: { error?: string | null }) {
  const message =
    error === "invalid_state"
      ? "That sign-in link expired or was already used — try again."
      : error === "token_exchange_failed" || error === "incomplete_userinfo"
      ? "Slack didn't return what we needed to sign you in. Try again."
      : error
      ? "Sign-in was cancelled or failed."
      : null;

  return (
    <div className="flex h-screen items-center justify-center bg-paper">
      <div className="card w-full max-w-[380px] p-8 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-brand-soft text-brand">
          <Slack size={22} />
        </div>
        <h1 className="mt-4 text-[20px] font-semibold tracking-tight text-ink">
          Sign in to Docket
        </h1>
        <p className="mt-2 text-[13px] leading-relaxed text-muted">
          Sign in with the Slack workspace this dashboard belongs to. Anyone in
          that workspace can sign in — there's nothing else to set up.
        </p>
        {message && (
          <p className="mt-3 rounded-lg bg-bad-soft px-3 py-2 text-[12.5px] text-bad">
            {message}
          </p>
        )}
        <a
          href="/slack/login"
          className="btn mt-6 flex w-full items-center justify-center gap-2"
        >
          <Slack size={15} /> Sign in with Slack
        </a>
      </div>
    </div>
  );
}
