import { Navigate, Route, Routes } from "react-router";

import { RequireAuth } from "./auth/RequireAuth";
import { RequireAdministrator } from "./auth/RequireAdministrator";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui/Toast";
import { AiSettingsPage } from "./pages/AiSettingsPage";
import { LoginPage } from "./pages/LoginPage";
import { NewRequestPage } from "./pages/NewRequestPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RequestDetailPage } from "./pages/RequestDetailPage";
import { RequestsPage } from "./pages/RequestsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ThemeProvider } from "./theme/ThemeProvider";
import { useAuth } from "./auth/AuthContext";
import { canViewRequests } from "./lib/permissions";

// The landing page: the dashboard for anyone who may view requests (it would only show an error to anyone else), and the
// requests list, as before, for everyone else.
function Home() {
  const { can } = useAuth();
  return <Navigate to={canViewRequests(can) ? "/dashboard" : "/requests"} replace />;
}

// The router and AuthProvider are supplied by the caller (main.tsx in the app, a memory router in tests). Theme and
// toasts are presentation only and live here so the real application and the tests share them.
export function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<RequireAuth />}>
            <Route element={<Layout />}>
              <Route index element={<Home />} />
              <Route path="dashboard" element={<DashboardPage />} />
              <Route path="requests" element={<RequestsPage />} />
              <Route path="requests/new" element={<NewRequestPage />} />
              <Route path="requests/:runId" element={<RequestDetailPage />} />
              <Route
                path="settings/ai"
                element={
                  <RequireAdministrator>
                    <AiSettingsPage />
                  </RequireAdministrator>
                }
              />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Routes>
      </ToastProvider>
    </ThemeProvider>
  );
}
