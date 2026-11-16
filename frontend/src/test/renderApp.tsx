import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { App } from "../App";
import { AuthProvider } from "../auth/AuthContext";

/** The real application (router, auth provider, routes) at `path`, backed by whatever `fetch` is stubbed to. */
export function renderApp(path = "/requests") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}
