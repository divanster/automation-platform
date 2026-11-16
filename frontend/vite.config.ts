import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// The browser talks ONLY to this dev server; /api is forwarded to Django, so the app is same-origin and no CORS is
// configured anywhere (docs/development/frontend-slice.md).
//
// changeOrigin MUST stay false. Django's CSRF check (CsrfViewMiddleware._origin_verified) compares the browser's
// Origin header (http://localhost:5173) with "http://" + the request's Host header. With changeOrigin: true the proxy
// rewrites Host to the backend (127.0.0.1:8000), the two no longer match, and every unsafe request fails with
// 403 csrf_failed. With false, Host stays "localhost:5173" and the check passes.
//
// AP_DEV_BACKEND overrides the Django address (default http://127.0.0.1:8000, the runserver default).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "AP_");
  const backend = env.AP_DEV_BACKEND || "http://127.0.0.1:8000";
  const proxy = { "/api": { target: backend, changeOrigin: false } };
  return {
    plugins: [react()],
    server: { host: "localhost", port: 5173, strictPort: true, proxy },
    preview: { host: "localhost", port: 4173, strictPort: true, proxy },
    build: { sourcemap: false },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      restoreMocks: true,
    },
  };
});
