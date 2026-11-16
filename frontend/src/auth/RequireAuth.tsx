import { Navigate, Outlet, useLocation } from "react-router";

import { ErrorState, LoadingState } from "../components/Status";
import { describeError } from "../lib/errors";
import { useAuth } from "./AuthContext";

/**
 * Route guard. It only decides what to SHOW: the backend refuses every API call that needs a session or a permission,
 * so removing this component would hide nothing the server protects.
 */
export function RequireAuth() {
  const { state, retry } = useAuth();
  const location = useLocation();

  if (state.status === "loading") return <LoadingState label="Checking your session…" />;
  if (state.status === "error") {
    return <ErrorState title="Could not check your session" message={describeError(state.error)} onRetry={retry} />;
  }
  if (state.status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
  }
  return <Outlet />;
}
