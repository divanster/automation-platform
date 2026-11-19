import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Icon } from "./Icon";

// Transient notifications. They are for confirmation of something that already happened ("Request submitted"), never
// for the only copy of an important result or an error the user must act on: those stay inline on the page.
// Success and info toasts dismiss themselves after a few seconds (paused while hovered or focused); error toasts stay
// until dismissed. Every toast can be dismissed with its button.

export type ToastKind = "success" | "info" | "error";
const AUTO_DISMISS_MS = 6000;

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastContextValue {
  notify(kind: ToastKind, message: string): void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

function ToastView({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: number) => void }) {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (toast.kind === "error" || paused) return;
    const timer = setTimeout(() => onDismiss(toast.id), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [toast.id, toast.kind, paused, onDismiss]);

  return (
    <div
      className={`toast toast-${toast.kind}`}
      role={toast.kind === "error" ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <Icon name={toast.kind === "error" ? "alert" : toast.kind === "success" ? "check" : "info"} />
      <div className="toast-message">{toast.message}</div>
      <button type="button" className="icon-button" aria-label="Dismiss notification" onClick={() => onDismiss(toast.id)}>
        <Icon name="close" />
      </button>
    </div>
  );
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((t) => t.id !== id)), []);
  const notify = useCallback((kind: ToastKind, message: string) => {
    const id = nextId.current++;
    setToasts((current) => [...current.slice(-3), { id, kind, message }]); // at most four at a time
  }, []);

  const value = useMemo(() => ({ notify }), [notify]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-region" role="region" aria-label="Notifications">
        {toasts.map((toast) => (
          <ToastView key={toast.id} toast={toast} onDismiss={dismiss} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (value === null) throw new Error("useToast must be used inside ToastProvider");
  return value;
}
