import { Link, useLocation } from "react-router";
import type { RefObject } from "react";

import { Icon } from "../ui/Icon";
import { NAV_ITEMS } from "./navigation";

export function Sidebar({
  id,
  can,
  isAdministrator,
  collapsed,
  onToggleCollapse,
  onNavigate,
  onClose,
  closeButtonRef,
}: {
  id: string;
  can(permission: string): boolean;
  isAdministrator: boolean;
  collapsed: boolean;
  onToggleCollapse(): void;
  /** A navigation link was followed (the mobile drawer closes). */
  onNavigate(): void;
  /** The mobile drawer's own close button. */
  onClose(): void;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
}) {
  const { pathname } = useLocation();
  const items = NAV_ITEMS.filter(
    (item) =>
      (!item.administratorOnly || isAdministrator) &&
      (item.permission === undefined || can(item.permission)) &&
      (item.anyPermission === undefined || item.anyPermission.some((p) => can(p))),
  );

  return (
    <aside className="sidebar" id={id}>
      <div className="sidebar-head">
        <Link className="brand" to="/" onClick={onNavigate}>
          <span className="brand-mark" aria-hidden="true">
            <Icon name="bolt" />
          </span>
          <span className="brand-name">Automation Platform</span>
        </Link>
        <button type="button" className="icon-button sidebar-close" ref={closeButtonRef} onClick={onClose} aria-label="Close navigation">
          <Icon name="close" />
        </button>
      </div>
      <nav className="sidebar-nav" aria-label="Main">
        <ul className="nav-list">
          {items.map((item) => (
            <li key={item.id}>
              <Link className="nav-link" to={item.to} aria-current={item.isActive(pathname) ? "page" : undefined} title={collapsed ? item.label : undefined} onClick={onNavigate}>
                <Icon name={item.icon} />
                <span className="nav-label">{item.label}</span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className="sidebar-foot">
        <button
          type="button"
          className="nav-link collapse-toggle"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : undefined}
        >
          <Icon name="panel-left" />
          <span className="nav-label" aria-hidden="true">
            Collapse
          </span>
        </button>
      </div>
    </aside>
  );
}
