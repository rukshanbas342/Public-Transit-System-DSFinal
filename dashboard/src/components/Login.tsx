// Operator login: username/password -> POST /api/login. The token lives in sessionStorage so a
// page refresh keeps you logged in, but closing the tab logs you out.
import { useState, type FormEvent } from "react";
import { api, ApiError } from "../api/client";

export function Login({ onLogin }: { onLogin: (token: string, username: string) => void }) {
  const [username, setUsername] = useState("operator");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(username, password);
      onLogin(res.token, res.username);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Wrong username or password" : String((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="panel login" onSubmit={submit}>
        <div className="brand-mark" aria-hidden>
          <BusIcon />
        </div>
        <h1>Transit Operations</h1>
        <div className="muted">Operator sign-in</div>
        <label htmlFor="u">Username</label>
        <input id="u" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <label htmlFor="p">Password</label>
        <input id="p" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
               autoComplete="current-password" autoFocus />
        <button className="btn primary" disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        {error && <div className="error">{error}</div>}
        <p className="faint" style={{ fontSize: 12, marginTop: 16 }}>
          Default login is set by OPERATOR_USERNAME / OPERATOR_PASSWORD (operator / operator).
        </p>
      </form>
    </div>
  );
}

export function BusIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="3" y="2" width="10" height="10" rx="2" fill="currentColor" />
      <rect x="4.5" y="4" width="7" height="3" rx="0.6" fill="var(--accent)" />
      <circle cx="5.5" cy="13.2" r="1.3" fill="currentColor" />
      <circle cx="10.5" cy="13.2" r="1.3" fill="currentColor" />
    </svg>
  );
}
