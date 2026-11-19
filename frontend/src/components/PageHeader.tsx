import { useEffect, type ReactNode } from "react";
import { Link } from "react-router";

export interface Crumb {
  label: string;
  to?: string;
}

const APP_NAME = "Automation Platform";

/**
 * The heading block of a page: breadcrumbs, the one <h1>, an optional description and a place for page actions. It also
 * sets the browser tab title, so a screen reader announces the new page after a route change.
 */
export function PageHeader({
  title,
  documentTitle,
  description,
  breadcrumbs,
  actions,
}: {
  title: ReactNode;
  /** Plain-text title for the tab; defaults to `title` when that is a string. */
  documentTitle?: string;
  description?: ReactNode;
  breadcrumbs?: Crumb[];
  actions?: ReactNode;
}) {
  const tabTitle = documentTitle ?? (typeof title === "string" ? title : undefined);
  useEffect(() => {
    document.title = tabTitle ? `${tabTitle} · ${APP_NAME}` : APP_NAME;
    return () => {
      document.title = APP_NAME;
    };
  }, [tabTitle]);

  return (
    <header className="page-header">
      <div className="page-header-main">
        {breadcrumbs && breadcrumbs.length > 0 ? (
          <nav className="breadcrumbs" aria-label="Breadcrumb">
            <ol>
              {breadcrumbs.map((crumb, index) => {
                const last = index === breadcrumbs.length - 1;
                return (
                  <li key={`${crumb.label}:${index}`} aria-current={last ? "page" : undefined}>
                    {crumb.to && !last ? <Link to={crumb.to}>{crumb.label}</Link> : crumb.label}
                  </li>
                );
              })}
            </ol>
          </nav>
        ) : null}
        <h1>{title}</h1>
        {description ? <p className="page-header-description">{description}</p> : null}
      </div>
      {actions ? <div className="page-header-actions">{actions}</div> : null}
    </header>
  );
}
