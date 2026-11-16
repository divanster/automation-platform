import { useState } from "react";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { Notice } from "./Status";

const PERM_SUBMIT = "intake.submit_request";

export function Layout() {
  const { state, logout, can } = useAuth();
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  if (state.status !== "authenticated") return null; // RequireAuth renders this layout only for a signed-in user
  const { user } = state;

  async function onSignOut() {
    setSigningOut(true);
    setSignOutError(null);
    const problem = await logout();
    setSigningOut(false);
    if (problem) setSignOutError(problem);
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <div className="brand">Automation Platform</div>
        <nav className="main-nav" aria-label="Main">
          <NavLink to="/requests" end>
            Requests
          </NavLink>
          {can(PERM_SUBMIT) ? <NavLink to="/requests/new">New request</NavLink> : null}
        </nav>
        <div className="user-box">
          <span className="user-name" title={user.roles.length ? user.roles.join(", ") : "No product role"}>
            {user.username}
            <span className="muted"> · {user.is_superuser ? "Superuser" : user.roles.length ? user.roles.join(", ") : "No role"}</span>
          </span>
          <button type="button" className="button" onClick={onSignOut} disabled={signingOut}>
            Sign out
          </button>
        </div>
      </header>
      <main id="main" className="app-main">
        {signOutError ? <Notice kind="error">{signOutError}</Notice> : null}
        <Outlet />
      </main>
    </div>
  );
}
