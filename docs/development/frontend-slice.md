# Frontend slice UI-1: first usable web interface

> **Status.** Written and tested in the development VM only, against a stubbed `fetch`. **It has not yet been run
> against the real Django API, PostgreSQL, or the worker, and its layout has not been looked at in a real browser.**
> Section 11 is the checklist the owner runs on the local Windows computer. Nothing in this document is claimed as
> validated beyond what section 9 lists.

## 1. Scope

A React + TypeScript + Vite single-page app in `frontend/` that talks to the REST API of
[rest-api-slice.md](rest-api-slice.md) and nothing else. It holds no business rules: every permission, state rule and
validation stays in the backend, and the UI only shows what the server says.

In this slice: sign-in and sign-out with session restoration, a paginated and filterable request list, a new-request
form, and a request detail page that follows processing and shows the extraction outcome, current revision, content
hash, draft and findings.

Not in this slice (UI-2 and later): editing a draft, approving, rejecting, the source text, audit history, task
monitoring, users and roles, workflows, AI providers, settings, document upload, exports, and any serving of the built app
by Django. Real AI is not part of it: extraction is still the deterministic fixture parser.

## 2. Dependencies

Exact versions (no ranges) in `frontend/package.json`; the full tree with integrity hashes is `frontend/package-lock.json`.

| Package | Version | Why |
| --- | --- | --- |
| `react`, `react-dom` | 19.3.1 | UI |
| `react-router` | 8.5.1 | Routing (`BrowserRouter`, `Routes`, search params) |
| `vite`, `@vitejs/plugin-react` | 8.3.8, 6.3.0 | Dev server, proxy, production build |
| `typescript` | 7.0.2 | Type checking (`tsc --noEmit`) |
| `vitest`, `jsdom` | 5.1.0, 29.1.1 | Test runner and DOM |
| `@testing-library/react`, `user-event`, `jest-dom` | 16.3.3, 14.6.12, 7.0.1 | Component tests |
| `@types/react`, `@types/react-dom`, `@types/node` | 19.3.0, 19.3.0, 26.6.6 | Types (`@types/node` is for `vite.config.ts`) |

No Redux, no UI kit, no CSS framework, no data-fetching library, no CORS package. `npm audit` reported 0
vulnerabilities when this was written. These are recent major versions chosen at implementation time; the project's
dependency policy (ADR 0007) applies: exact pins, deliberate upgrades.

## 3. Setup and commands

Needs Node.js and npm (written with Node 24.14.1 and npm 11.11.0; `package.json` does not pin an engine).

```powershell
cd frontend
npm ci                  # install exactly what package-lock.json records
npm run dev             # Vite dev server on http://localhost:5173 (strict port)
npm run typecheck       # tsc for the app and for vite.config.ts
npm test                # vitest run (143 tests)
npm run build           # typecheck + production build into frontend/dist (git-ignored)
```

`npm test` and `npm run build` need no backend and no database.

## 4. Running it with Django and the worker (owner's computer)

Three terminals. The backend steps are the runbook's; nothing about them changes.

```powershell
# 1. Django (from backend\, with your usual AP_DB_* environment loaded; see runbook section 3)
python manage.py runserver 127.0.0.1:8010

# 2. The worker (same environment). Without it requests stay "Queued" forever.
python manage.py runworker

# 3. The frontend, pointing the proxy at Django
$env:AP_DEV_BACKEND = "http://127.0.0.1:8010"    # omit if Django listens on the default 127.0.0.1:8000
cd frontend
npm run dev
```

Open `http://localhost:5173/`. Roles and users: run `python manage.py setup_roles` once, then create users in
`http://127.0.0.1:8010/admin/` (the admin is on Django's own port, not proxied) and add them to the Operator,
Approver or Auditor group.

## 5. Proxy configuration and why `changeOrigin` is false

`frontend/vite.config.ts` forwards `/api` to Django (`AP_DEV_BACKEND`, default `http://127.0.0.1:8000`). The browser only
ever talks to `localhost:5173`, so the app is **same-origin**: no CORS is configured anywhere and the API's cookies and
CSRF design work unchanged. Only `/api` is proxied.

`changeOrigin` must stay **false**. Django's CSRF check (`CsrfViewMiddleware._origin_verified`, read in the project's
Django 5.2 install) accepts a request only if its `Origin` header equals `scheme://` plus the request's `Host`
(or a trusted origin). The browser sends `Origin: http://localhost:5173`. With `changeOrigin: true` the proxy rewrites
`Host` to the backend (`127.0.0.1:8010`), the two differ, and every unsafe request, including the login, fails with
`403 csrf_failed`. With `false`, `Host` stays `localhost:5173` and they match.

Checked in the VM with a throwaway echo server standing in for Django (no Django, no database): through the proxy the
backend received `Host: localhost:5173` for a request carrying `Origin: http://localhost:5173`, no `Access-Control-*`
header was added, a non-`/api` path was served by Vite, and a deep link such as `/requests/<uuid>` returned the app. That
shows the proxy forwards `Host` unchanged; it does not show that Django accepts the request. `localhost` is in the
development `ALLOWED_HOSTS` default (`backend/config/settings/development.py`).

## 6. Authentication flow

The session is Django's HttpOnly cookie. JavaScript never sees it, and nothing is stored in `localStorage`,
`sessionStorage` or any other browser storage (a test scans the source for this).

1. **Page load.** `GET /api/auth/me/`. `200` restores the session; `403 not_authenticated` shows the login; a network
   or server error shows "Could not check your session" with a retry (the user is not wrongly shown the login).
2. **CSRF.** The CSRF cookie is HttpOnly, so the token comes from the body of `GET /api/auth/csrf/` and is held only in
   memory (`src/api/http.ts`). It is fetched lazily before the first unsafe request, so a page refresh needs no special
   handling, and it is sent as `X-CSRFToken` on every `POST` and never on a `GET`.
3. **Login.** `POST /api/auth/login/` with the token. The server rotates the session and the CSRF token; the response's
   `csrf_token` replaces the one in memory. The password field is cleared on failure, and no input is echoed into an
   error message.
4. **CSRF failure.** A `403 csrf_failed` fetches a new token and retries the request **once**. A second failure is shown.
5. **Expired session.** Any data call that gets `403 not_authenticated` (the API uses 403, not 401) returns the user to
   the login with "Your session has ended", and the page they were on is restored after signing in again. With the
   development settings the secret key is random per process, so restarting `runserver` is an easy way to test this.
6. **Logout.** `POST /api/auth/logout/`; the token is dropped. If the server cannot be reached the user stays shown as
   signed in with an error, because the server session may still be alive. "Already signed out" counts as success.
7. **Permissions.** `/api/auth/me/` permissions only decide what to show (for example the New request link). The server
   re-checks everything, and an unexpected `403 permission_denied` is shown as a message.

## 7. Routes, components and API use

| Route | Screen | API calls |
| --- | --- | --- |
| `/login` | `LoginPage` | `GET /api/auth/csrf/`, `POST /api/auth/login/` |
| `/` | redirect to `/requests` | |
| `/requests` | `RequestsPage` (table, state badges, filter, pagination; `?page=` and `?state=` in the URL) | `GET /api/requests/?page&page_size=20&state` |
| `/requests/new` | `NewRequestPage` | `POST /api/requests/` with `{text, client_request_id}` |
| `/requests/:runId` | `RequestDetailPage` | `GET /api/requests/{run_id}/` (polled) |
| anything else | `NotFoundPage` | |

All routes except `/login` are inside `RequireAuth`, which shows the guard result only (the backend refuses protected calls
on its own). Layout, navigation and sign-out are `components/Layout.tsx`; `components/Status.tsx` holds the loading,
error, empty and notice states.

- **List.** URL parameters are untrusted: an invalid page or unknown state is dropped, not sent. A slow answer for an old
  filter cannot overwrite a newer one (`lib/useResource.ts` aborts and ignores it).
- **Submit.** One `client_request_id` per distinct text: resubmitting the same text after a lost response returns the original
  request; editing the text starts a new id. The text is sent without trimming. On success the app opens the created
  request.
- **Detail and polling** (`lib/useRequestDetail.ts`). Polls every 2 s for the first 30 polls, then every 10 s, only while
  the state is `queued`, `extracting` or `exporting`. It stops in every other state, when a refresh fails (the last good data
  stays visible with the error and a retry), when the user leaves the page, and it skips fetches while the tab is hidden.
- **Shown.** State, processing task state and attempts, the safe failure code, extraction outcome, current revision number,
  full SHA-256 content hash, the draft, findings split into blocking and warnings with their stable `code` and `field_path`,
  approval and rejection counts, and the server's `allowed_actions` and `action_blockers` as read-only information.
  There are no edit, approve or reject controls.
- **Not shown or kept.** Canonical bytes are never requested (the API does not return them), and the source text is not
  available from the API yet.

**Decimals.** A draft item's `quantity` is decimal text. It is typed `string | null`, printed exactly as received, and
never passed through `Number`, `parseFloat` or arithmetic. If the API ever sent a JSON number the client refuses the
response instead of showing a rounded value. A test scans the source for numeric conversion of quantities.

## 8. Security measures

- All user and server content is rendered as React text; there is no `dangerouslySetInnerHTML`, `innerHTML` or `eval`
  (source-scan test, plus a test that renders hostile `<img>` and `<script>` strings and checks no element is created).
- No `console` output anywhere in the application code. No credentials, tokens or content in browser storage or cookies
  written by script. No absolute URLs, `Authorization` header, bearer tokens or JWT.
- Every request goes through one function (`api/http.ts`), same-origin, `credentials: "same-origin"`, `cache: "no-store"`,
  and only paths under `/api/`.
- Error text comes from the stable error `code`, not from server messages, and never echoes a typed or returned value.
- The backend remains authoritative for every decision; the UI follows `allowed_actions` and shows blockers.
- No CSP is configured yet: that belongs to the serving layer (UI-4 or deployment), together with TLS.

## 9. Tests and what was run

Written and run in the development VM. No PostgreSQL was contacted (no `AP_*` variable was set in the shell; the project test
runner excludes PostgreSQL tests by default).

| Check | Result |
| --- | --- |
| `npm run typecheck` | Clean |
| `npm test` | 143 tests in 9 files, all passing |
| `npm run build` | Succeeds (114 modules; about 287 kB JavaScript, 90 kB gzipped) |
| `npm audit` | 0 vulnerabilities |
| `manage.py check` | No issues |
| `manage.py test` (backend DB-free) | `Ran 1201 tests`, OK |

Coverage by file: `api/http.test.ts` (transport, CSRF bootstrap and one retry, error contract, Retry-After, network and
non-JSON errors, expired-session handler, decimal text), `api/auth.test.ts` (login, token rotation, logout),
`lib/errors.test.ts` (every error code's message, nothing echoed), `lib/format.test.ts`, `auth/auth.test.tsx` (session
restoration, protected routes, login and logout, expired session, permission hints), `pages/RequestsPage.test.tsx`,
`pages/NewRequestPage.test.tsx`, `pages/RequestDetailPage.test.tsx` (polling with fake timers, stable states, hostile
content, decimals), `security.test.ts` (source scan).

The tests use a stubbed `fetch` and synthetic fixtures (`src/test/`, never imported by the app). To check that the tests
can fail, four deliberate defects were introduced one at a time (dropping the CSRF header, polling a stable state, using
browser storage, converting a quantity with `Number`); each was caught, and the code was restored.

**What these tests cannot show.** They do not exercise the real server, so a mismatch between the hand-written types
(`src/api/types.ts`, mirrored from `apps/api/serializers.py`) and the real responses would not be caught here. They run in
jsdom, not a browser, so layout and styling are unverified.

## 10. Known limitations

- **Read-only review.** Detail shows what a reviewer needs but offers no edit, approve or reject (UI-2). The source text is
  not exposed by the API yet, so the draft cannot be compared with the original in the UI.
- **Fixture extraction.** The text must follow the labelled-line template shown on the form; anything else yields an
  invalid or empty draft. The UI says so.
- **Line endings.** A browser `<textarea>` reports line breaks as `\n`, so CRLF pasted by a user is stored as LF. The
  application does not otherwise change the text.
- **Submitter names.** The API returns the submitter's `public_id` only; the UI shows "You" or the first 8 characters.
- **Same-origin only; no production serving.** `npm run dev` is the only supported way to run it. Django does not serve
  `frontend/dist`, and `BrowserRouter` deep links need a server-side fallback to `index.html` that does not exist yet.
  `vite preview` is not a production server. TLS, HSTS and CSP are open deployment items.
- **Polling, not push.** Intervals are provisional. A request with no running worker is polled indefinitely (every 10 s
  after the first minute).
- **Per-process login throttling** and the absence of password reset, MFA and lockout are API limitations that apply to the UI.
- **Installation-wide approver and auditor visibility** (an Approver sees every request) is the backend's initial policy.
- **Self-approval is forbidden even for a lone user** (backend rule). A single-account installation cannot complete the
  approve step; this slice does not offer approval, but UI-2 will meet it.

## 11. Local end-to-end validation (owner, on the local Windows computer)

Prerequisites: PostgreSQL running and migrated, `setup_roles` run, an Operator user and an Approver user created, Django
(`runserver`), the worker and `npm run dev` started as in section 4. Use only synthetic data.

1. **Login failures.** Wrong password shows "The username or password is incorrect." and clears the password. Open the
   browser's developer tools, Network tab: the login `POST` carries an `X-CSRFToken` header and the response is `200` for the
   right credentials. Application tab: Local Storage, Session Storage and IndexedDB are empty, and the session cookie is
   HttpOnly.
2. **Session restore.** After signing in, refresh the page: still signed in, no login screen.
3. **Submit.** As the Operator, New request, paste:

   ```text
   Requester: Synthetic Requester
   Contact: synthetic@example.invalid
   Item: 2.50 | kg | Synthetic widget
   Item: 10 | pcs | Second synthetic item | fragile
   ```

   Submit. The app opens the request showing **Queued**.
4. **Processing.** With the worker running, the page changes by itself to **Extracting** (possibly too brief to see) and **Needs review**, then
   stops requesting (Network tab goes quiet). It shows extraction `valid`, revision 1, the 64-character content hash, the
   draft with quantities `2.50` and `10` exactly, and no findings.
5. **Worker stopped.** Stop the worker, submit another request: it stays Queued with the "worker must be running" message
   and the page keeps polling. Start the worker: it completes.
6. **Invalid extraction.** Submit text without a `Requester:` line: extraction is `invalid`, no revision, blocking findings
   with their codes and field paths are listed.
7. **List.** Both requests appear newest first with state badges; the state filter narrows the list and the address bar
   keeps `?state=`; refresh keeps the filter. (Page through with more than 20 requests if available.)
8. **Visibility.** Sign in as the Approver: the Operator's requests are listed (submitter shown as a short id); the New
   request link is absent. Sign in as a second Operator: the first Operator's request URL shows "Not found".
9. **Permissions.** As the Approver, open `/requests/new` directly: the form is not offered. A user with no role sees a
   permission message on the list.
10. **Expired session.** Restart `runserver` (development secret key changes) and click Refresh on the list: back at the
    login with "Your session has ended", and the same page returns after signing in.
11. **Logout.** Sign out returns to the login; the Back button does not show data.
12. **CSRF path.** Optional: in the Network tab confirm no `Access-Control-*` headers and that requests go to
    `localhost:5173/api/...` (the proxy), not to Django's port.

Report anything that differs, with the exact message. Failures here are findings about the integration, not about the
stubbed tests.

## 12. Files

New: `frontend/` (`package.json`, `package-lock.json`, `tsconfig.json`, `tsconfig.node.json`, `vite.config.ts`,
`index.html`, `src/`), and this document. Changed: `.gitignore` (frontend test output), `README.md`, `CLAUDE.md`,
`docs/development/runbook.md` (status table). No backend file, model, migration or Python dependency changed.
