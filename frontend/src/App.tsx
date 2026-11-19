import { Navigate, Route, Routes } from "react-router";

import { RequireAuth } from "./auth/RequireAuth";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui/Toast";
import { LoginPage } from "./pages/LoginPage";
import { NewRequestPage } from "./pages/NewRequestPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RequestDetailPage } from "./pages/RequestDetailPage";
import { RequestsPage } from "./pages/RequestsPage";
import { ThemeProvider } from "./theme/ThemeProvider";

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
              <Route index element={<Navigate to="/requests" replace />} />
              <Route path="requests" element={<RequestsPage />} />
              <Route path="requests/new" element={<NewRequestPage />} />
              <Route path="requests/:runId" element={<RequestDetailPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Routes>
      </ToastProvider>
    </ThemeProvider>
  );
}
