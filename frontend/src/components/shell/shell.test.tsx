import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiError, installFakeApi, json, type Handler } from "../../test/fakeApi";
import { RUN_ID, detail, listItem, operator, page } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { signedIn } from "../../test/server";
import { openAccountMenu } from "../../test/shell";
import { ToastProvider, useToast } from "../ui/Toast";

const data: Handler = (call) => {
  if (call.method === "GET" && call.path === "/api/requests/") return json(200, page([listItem()]));
  if (call.method === "GET" && call.path === `/api/requests/${RUN_ID}/`) return json(200, detail());
  return apiError(404, "not_found");
};

const shell = () => document.querySelector<HTMLElement>(".shell")!;
const mainNav = () => within(screen.getByRole("navigation", { name: "Main" }));

afterEach(() => {
  vi.useRealTimers();
  document.documentElement.removeAttribute("data-theme");
});

describe("navigation", () => {
  it("lists only the implemented pages and marks the current one", async () => {
    installFakeApi(signedIn(data));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");

    const links = mainNav().getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(["Requests", "New request"]);
    expect(mainNav().getByRole("link", { name: "Requests" })).toHaveAttribute("aria-current", "page");
    expect(mainNav().getByRole("link", { name: "New request" })).not.toHaveAttribute("aria-current");
  });

  it("marks New request as current on its own page, and Requests on a request's page", async () => {
    installFakeApi(signedIn(data));
    const view = renderApp("/requests/new");
    await screen.findByRole("heading", { name: "New request" });
    expect(mainNav().getByRole("link", { name: "New request" })).toHaveAttribute("aria-current", "page");
    expect(mainNav().getByRole("link", { name: "Requests" })).not.toHaveAttribute("aria-current");
    view.unmount();

    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(mainNav().getByRole("link", { name: "Requests" })).toHaveAttribute("aria-current", "page");
    expect(mainNav().getByRole("link", { name: "New request" })).not.toHaveAttribute("aria-current");
  });

  it("hides New request from a user without the submit permission", async () => {
    installFakeApi(signedIn(data, operator({ permissions: ["intake.view_own_request"] })));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    expect(mainNav().queryByRole("link", { name: "New request" })).not.toBeInTheDocument();
    expect(mainNav().getByRole("link", { name: "Requests" })).toBeInTheDocument();
  });

  it("offers a skip link, one main landmark and one h1", async () => {
    installFakeApi(signedIn(data));
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("sets the tab title and breadcrumbs from the page header", async () => {
    installFakeApi(signedIn(data));
    renderApp(`/requests/${RUN_ID}`);
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    expect(document.title).toBe("REQ-20260101-AAAA0001 · Automation Platform");
    const crumbs = within(screen.getByRole("navigation", { name: "Breadcrumb" }));
    expect(crumbs.getByRole("link", { name: "Requests" })).toHaveAttribute("href", "/requests");
  });
});

describe("responsive shell", () => {
  it("opens the navigation drawer from the menu button and moves focus into it", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    const toggle = await screen.findByRole("button", { name: "Open navigation" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(shell()).toHaveAttribute("data-nav-open", "false");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(shell()).toHaveAttribute("data-nav-open", "true");
    expect(screen.getByRole("button", { name: "Close navigation" })).toHaveFocus();
    expect(document.querySelector(".shell-body")).toHaveAttribute("inert");
  });

  it("closes the drawer with Escape and gives focus back to the menu button", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    const toggle = await screen.findByRole("button", { name: "Open navigation" });
    await user.click(toggle);
    await user.keyboard("{Escape}");
    expect(shell()).toHaveAttribute("data-nav-open", "false");
    await waitFor(() => expect(toggle).toHaveFocus());
    expect(document.querySelector(".shell-body")).not.toHaveAttribute("inert");
  });

  it("closes the drawer with its close button and with the backdrop", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    await user.click(await screen.findByRole("button", { name: "Open navigation" }));
    await user.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(shell()).toHaveAttribute("data-nav-open", "false");

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await user.click(document.querySelector(".sidebar-backdrop")!);
    expect(shell()).toHaveAttribute("data-nav-open", "false");
  });

  it("closes the drawer after following a link and moves focus to the content", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    await user.click(await screen.findByRole("button", { name: "Open navigation" }));
    await user.click(mainNav().getByRole("link", { name: "New request" }));
    expect(await screen.findByRole("heading", { name: "New request" })).toBeInTheDocument();
    expect(shell()).toHaveAttribute("data-nav-open", "false");
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("collapses and expands the sidebar", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    const collapse = await screen.findByRole("button", { name: "Collapse sidebar" });
    expect(shell()).toHaveAttribute("data-collapsed", "false");
    await user.click(collapse);
    expect(shell()).toHaveAttribute("data-collapsed", "true");
    // Links keep their names when only icons are visible.
    expect(mainNav().getByRole("link", { name: "Requests" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(shell()).toHaveAttribute("data-collapsed", "false");
  });
});

describe("account menu and theme", () => {
  it("shows the user and roles once, and keeps Sign out inside the menu", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    expect(screen.getByText("synthetic.operator")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();

    const trigger = await openAccountMenu(user);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("switches between light, dark and system themes on the document element", async () => {
    installFakeApi(signedIn(data));
    const user = userEvent.setup();
    renderApp("/requests");
    await screen.findByText("REQ-20260101-AAAA0001");
    const root = document.documentElement;
    expect(root).not.toHaveAttribute("data-theme");

    await openAccountMenu(user);
    expect(screen.getByRole("radio", { name: "System" })).toBeChecked();

    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(root).toHaveAttribute("data-theme", "dark");
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();

    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(root).toHaveAttribute("data-theme", "light");

    await user.click(screen.getByRole("radio", { name: "System" }));
    expect(root).not.toHaveAttribute("data-theme");
  });
});

describe("toast notifications", () => {
  function Trigger({ kind }: { kind: "success" | "error" }) {
    const { notify } = useToast();
    return (
      <button type="button" onClick={() => notify(kind, `A ${kind} message`)}>
        Notify
      </button>
    );
  }

  it("announces a success politely, then dismisses it by itself", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <ToastProvider>
        <Trigger kind="success" />
      </ToastProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Notify" }));
    const region = screen.getByRole("region", { name: "Notifications" });
    expect(within(region).getByRole("status")).toHaveTextContent("A success message");
    expect(within(region).queryByRole("alert")).not.toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(7000);
    });
    expect(within(region).queryByText("A success message")).not.toBeInTheDocument();
  });

  it("keeps an error until it is dismissed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <ToastProvider>
        <Trigger kind="error" />
      </ToastProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Notify" }));
    expect(screen.getByRole("alert")).toHaveTextContent("A error message");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("confirms a submitted request with a toast on the request page", async () => {
    const created = { run_id: RUN_ID, case_id: "c", reference: "REQ-20260101-AAAA0001", state: "queued", created: true };
    installFakeApi(
      signedIn((call) => {
        if (call.method === "POST" && call.path === "/api/requests/") return json(201, created);
        return data(call);
      }),
    );
    const user = userEvent.setup();
    renderApp("/requests/new");
    await user.type(await screen.findByLabelText("Request text"), "Requester: Synthetic");
    await user.click(screen.getByRole("button", { name: "Submit request" }));
    await screen.findByRole("heading", { name: /REQ-20260101-AAAA0001/ });
    const region = screen.getByRole("region", { name: "Notifications" });
    expect(within(region).getByRole("status")).toHaveTextContent("Request submitted and queued for extraction.");
  });
});
