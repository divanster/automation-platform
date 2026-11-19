import type { IconName } from "../ui/Icon";

// The navigation lists ONLY screens that exist. Adding an entry here without a route would be a dead link, and a
// permission on an entry is a display hint: the backend still refuses what the user may not do.

export interface NavItem {
  id: string;
  label: string;
  to: string;
  icon: IconName;
  /** Shown only to users who hold this permission (a hint; the server decides). */
  permission?: string;
  /** Shown only to users who hold at least one of these permissions (a hint; the server decides). */
  anyPermission?: readonly string[];
  isActive(pathname: string): boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  {
    id: "dashboard",
    label: "Dashboard",
    to: "/dashboard",
    icon: "grid",
    // The same condition as the dashboard's own check (lib/permissions.ts): without a view permission it has no data.
    anyPermission: ["intake.view_own_request", "intake.view_any_request"],
    isActive: (pathname) => pathname === "/dashboard",
  },
  {
    id: "requests",
    label: "Requests",
    to: "/requests",
    icon: "list",
    // The list and every request's detail page, but not the new-request form, which has its own entry.
    isActive: (pathname) => pathname === "/requests" || (pathname.startsWith("/requests/") && !pathname.startsWith("/requests/new")),
  },
  {
    id: "new-request",
    label: "New request",
    to: "/requests/new",
    icon: "plus",
    permission: "intake.submit_request",
    isActive: (pathname) => pathname.startsWith("/requests/new"),
  },
];
