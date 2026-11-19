import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { ErrorState, LoadingState, Notice } from "../components/Status";
import { Icon } from "../components/ui/Icon";
import { describeError } from "../lib/errors";

/** Only in-app paths are honoured as a post-login destination. */
function safeDestination(from: unknown): string {
  if (typeof from === "string" && from.startsWith("/") && !from.startsWith("//") && !from.startsWith("/login")) return from;
  return "/requests";
}

export function LoginPage() {
  const { state, login, retry } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const destination = safeDestination((location.state as { from?: unknown } | null)?.from);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.status === "loading") return <LoadingState label="Checking your session…" />;
  if (state.status === "error") {
    return <ErrorState title="Could not check your session" message={describeError(state.error)} onRetry={retry} />;
  }
  if (state.status === "authenticated") return <Navigate to={destination} replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(username, password);
      setPassword("");
      navigate(destination, { replace: true });
    } catch (caught) {
      setPassword("");
      setError(describeError(caught));
      setPending(false);
    }
  }

  return (
    <main className="login-page">
      <form className="card login-card" onSubmit={onSubmit} aria-labelledby="login-title">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">
            <Icon name="bolt" />
          </span>
          <span>Automation Platform</span>
        </div>
        <h1 id="login-title">Sign in</h1>
        <p className="muted">Enter your username and password.</p>
        {state.notice ? <Notice kind="warning">{state.notice}</Notice> : null}
        {error ? <Notice kind="error">{error}</Notice> : null}
        <label>
          Username
          <input
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={pending}
          />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={pending}
          />
        </label>
        <button type="submit" className="button button-primary" disabled={pending}>
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
