import type { ReactNode } from "react";

import { useAuth } from "./AuthContext";
import { PageHeader } from "../components/PageHeader";
import { EmptyState } from "../components/Status";

/**
 * Renders its children only for an administrator (a superuser). Anyone else gets a plain "not available" page and the
 * children are never mounted, so no settings request is made and nothing is shown. This is a UI hint: the server answers
 * 403 `permission_denied` to a non-administrator on every call, whatever this component decides.
 */
export function RequireAdministrator({ children }: { children: ReactNode }) {
  const { isAdministrator } = useAuth();
  if (isAdministrator) return <>{children}</>;
  return (
    <section>
      <PageHeader title="Not available" />
      <EmptyState icon="alert" title="Not available for your account">
        This page is for administrators only.
      </EmptyState>
    </section>
  );
}
