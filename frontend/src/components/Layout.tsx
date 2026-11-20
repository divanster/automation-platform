import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet, useLocation } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { Sidebar } from "./shell/Sidebar";
import { UserMenu } from "./shell/UserMenu";
import { Icon } from "./ui/Icon";
import { Notice } from "./Status";

const SIDEBAR_ID = "app-sidebar";
const WIDE_QUERY = "(min-width: 900px)"; // keep in step with the 900px breakpoint in styles/tokens.css

/**
 * The application shell for a signed-in user: sidebar, top bar and the content container.
 *
 * On wide screens the sidebar is always visible and can be collapsed to icons. On narrow screens it is an off-canvas
 * drawer opened from the top bar; while it is open the page behind it is inert, Escape or the backdrop closes it, and
 * focus returns to the button that opened it.
 */
export function Layout() {
  const { state, logout, can, isAdministrator } = useAuth();
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const { pathname } = useLocation();
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const main = useRef<HTMLElement>(null);

  const closeNav = useCallback((focus: "menu" | "main" = "menu") => {
    setNavOpen(false);
    // The shell-body is still inert in this render; focus moves once React has committed the change.
    requestAnimationFrame(() => (focus === "menu" ? menuButton.current : main.current)?.focus());
  }, []);

  // A drawer left open by navigation (browser back, a link in the page) must not stay open.
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    closeButton.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") closeNav("menu");
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [navOpen, closeNav]);

  // Growing the window past the breakpoint turns the drawer into the permanent sidebar.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(WIDE_QUERY);
    const onChange = () => {
      if (query.matches) setNavOpen(false);
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

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
    <div className="shell" data-collapsed={collapsed} data-nav-open={navOpen}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Sidebar
        id={SIDEBAR_ID}
        can={can}
        isAdministrator={isAdministrator}
        collapsed={collapsed}
        onToggleCollapse={() => setCollapsed((v) => !v)}
        onNavigate={() => {
          if (navOpen) closeNav("main");
        }}
        onClose={() => closeNav("menu")}
        closeButtonRef={closeButton}
      />
      <div className="sidebar-backdrop" aria-hidden="true" onClick={() => closeNav("menu")} />
      <div className="shell-body" inert={navOpen}>
        <header className="topbar">
          <button
            type="button"
            className="icon-button menu-toggle"
            ref={menuButton}
            aria-label="Open navigation"
            aria-expanded={navOpen}
            aria-controls={SIDEBAR_ID}
            onClick={() => setNavOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <div className="topbar-spacer" />
          <UserMenu user={user} signingOut={signingOut} onSignOut={onSignOut} />
        </header>
        <main id="main" className="app-main" ref={main} tabIndex={-1}>
          {signOutError ? <Notice kind="error">{signOutError}</Notice> : null}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
