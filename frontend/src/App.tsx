import { Navigate, Route, Routes } from "react-router";

import { RequireAuth } from "./auth/RequireAuth";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { NewRequestPage } from "./pages/NewRequestPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RequestDetailPage } from "./pages/RequestDetailPage";
import { RequestsPage } from "./pages/RequestsPage";

// The router and AuthProvider are supplied by the caller (main.tsx in the app, a memory router in tests).
export function App() {
  return (
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
  );
}
