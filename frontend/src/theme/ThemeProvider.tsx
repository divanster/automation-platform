import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

// Light, dark, or follow the operating system. "system" leaves `data-theme` off the root element, so the CSS media query
// (prefers-color-scheme) decides; "light" and "dark" force a palette.
//
// The choice is kept in memory only. The application's security rules (security.test.ts) forbid browser storage, so it
// resets to "system" on a full page reload; see docs/development/frontend-slice.md, section 14.

export type ThemePreference = "system" | "light" | "dark";
export const THEME_PREFERENCES: readonly ThemePreference[] = ["light", "dark", "system"];

interface ThemeContextValue {
  preference: ThemePreference;
  setPreference(preference: ThemePreference): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children, initial = "system" }: { children: ReactNode; initial?: ThemePreference }) {
  const [preference, setPreference] = useState<ThemePreference>(initial);

  useEffect(() => {
    const root = document.documentElement;
    if (preference === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", preference);
    return () => root.removeAttribute("data-theme");
  }, [preference]);

  const value = useMemo(() => ({ preference, setPreference }), [preference]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
