import type { ReactNode } from "react";

import { Icon, type IconName } from "./ui/Icon";
import { SkeletonRows } from "./ui/Skeleton";

/** `skeleton` adds placeholder lines under the label; the label itself is what assistive technology announces. */
export function LoadingState({ label = "Loading…", skeleton = 0 }: { label?: string; skeleton?: number }) {
  return (
    <div className="status status-loading" role="status" aria-live="polite" aria-busy="true">
      <div className="status-loading-label">
        <span className="spinner" aria-hidden="true" />
        <span>{label}</span>
      </div>
      {skeleton > 0 ? <SkeletonRows rows={skeleton} /> : null}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  children,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="status status-error" role="alert">
      <Icon name="alert" />
      <strong>{title}</strong>
      <p>{message}</p>
      {children}
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ children, icon = "inbox", title, action }: { children: ReactNode; icon?: IconName; title?: string; action?: ReactNode }) {
  return (
    <div className="status status-empty">
      <Icon name={icon} />
      {title ? <p className="empty-title">{title}</p> : null}
      <p>{children}</p>
      {action}
    </div>
  );
}

const NOTICE_ICONS: Record<"info" | "warning" | "error", IconName> = { info: "info", warning: "alert", error: "alert" };

export function Notice({ kind = "info", children }: { kind?: "info" | "warning" | "error"; children: ReactNode }) {
  return (
    <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <Icon name={NOTICE_ICONS[kind]} />
      <div className="notice-body">{children}</div>
    </div>
  );
}
