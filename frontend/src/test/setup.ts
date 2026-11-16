import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";

import { clearCsrfToken, setUnauthenticatedHandler } from "../api/http";

// The HTTP client keeps module-level state (the CSRF token, the session-expiry handler). Reset it between tests.
afterEach(() => {
  clearCsrfToken();
  setUnauthenticatedHandler(null);
});
