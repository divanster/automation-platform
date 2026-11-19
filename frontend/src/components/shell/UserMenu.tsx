import { useEffect, useId, useRef, useState } from "react";

import type { Profile } from "../../api/types";
import { THEME_PREFERENCES, useTheme, type ThemePreference } from "../../theme/ThemeProvider";
import { Icon, type IconName } from "../ui/Icon";

const THEME_LABELS: Record<ThemePreference, string> = { light: "Light", dark: "Dark", system: "System" };
const THEME_ICONS: Record<ThemePreference, IconName> = { light: "sun", dark: "moon", system: "monitor" };

export function roleSummary(user: Profile): string {
  return user.is_superuser ? "Superuser" : user.roles.length ? user.roles.join(", ") : "No role";
}

function initials(username: string): string {
  return username.slice(0, 2) || "?";
}

/**
 * Account disclosure: the signed-in user, the theme choice and Sign out. It is a button that shows or hides a panel (not
 * an ARIA menu): Tab moves through the panel's controls, Escape closes it and returns focus to the button, and so does
 * a click outside it.
 */
export function UserMenu({ user, signingOut, onSignOut }: { user: Profile; signingOut: boolean; onSignOut(): void }) {
  const [open, setOpen] = useState(false);
  const { preference, setPreference } = useTheme();
  const panelId = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (root.current && event.target instanceof Node && !root.current.contains(event.target)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div className="user-menu" ref={root}>
      <button type="button" className="user-menu-trigger" ref={trigger} aria-expanded={open} aria-controls={open ? panelId : undefined} onClick={() => setOpen((v) => !v)}>
        <span className="avatar" aria-hidden="true">
          {initials(user.username)}
        </span>
        <span className="user-meta">
          <span className="user-name">{user.username}</span>
          <span className="user-role muted">{roleSummary(user)}</span>
        </span>
        <Icon name="chevron" className="icon chevron" />
      </button>
      {open ? (
        <div className="user-menu-panel" id={panelId} role="group" aria-label="Account">
          <fieldset>
            <legend>Theme</legend>
            <div className="theme-options">
              {THEME_PREFERENCES.map((option) => (
                <div className="theme-option" key={option}>
                  <input type="radio" name="theme" id={`theme-${option}`} value={option} checked={preference === option} onChange={() => setPreference(option)} />
                  <label htmlFor={`theme-${option}`}>
                    <Icon name={THEME_ICONS[option]} />
                    {THEME_LABELS[option]}
                  </label>
                </div>
              ))}
            </div>
          </fieldset>
          <button type="button" className="button button-block" onClick={onSignOut} disabled={signingOut}>
            <Icon name="logout" />
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}
