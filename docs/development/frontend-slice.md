# Frontend slices UI-1 and UI-2: the web interface

> **Status.** **UI-1** (sections 1 to 12: sign-in, requests, request detail) was written in the development VM; the owner
> reports that it was then run in a real browser against the local Django, PostgreSQL and worker stack (that run's output is
> not recorded in this repository). **UI-2** (section 13: draft editing, approval, rejection, warning acknowledgement,
> audit history, stale-revision handling) was written and tested in the development VM against a stubbed `fetch`
> (sections 9 and 13.8). **The owner then ran one UI-2 workflow in a real browser against the local Django API, PostgreSQL and
> persistent worker (submit, extraction, edit, approval by a different user, audit history) and reports that it passed
> (section 13.7a, recorded 2026-11-16).** That is development validation of one workflow, supported by owner-supplied
> screenshots described to Claude and not stored here; it is **not** a test of all UI behavior (the stale-revision conflict and
> rejection were not shown) and **not** production readiness. Section 13.7 remains the full checklist.

## 1. Scope

A React + TypeScript + Vite single-page app in `frontend/` that talks to the REST API of
[rest-api-slice.md](rest-api-slice.md) and nothing else. It holds no business rules: every permission, state rule and
validation stays in the backend, and the UI only shows what the server says.

In this slice: sign-in and sign-out with session restoration, a paginated and filterable request list, a new-request
form, and a request detail page that follows processing and shows the extraction outcome, current revision, content
hash, draft and findings.

Not in UI-1: editing a draft, approving, rejecting and audit history (these are UI-2, section 13). Not in either slice: the
source text, task monitoring, users and roles, workflows, AI providers, settings, document upload, exports, and any serving
of the built app by Django. Real AI is not part of them: extraction is still the deterministic fixture parser.

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
npm test                # vitest run (264 tests, including UI-2)
npm run build           # typecheck + production build into frontend/dist (git-ignored)
```

`npm test` and `npm run build` need no backend and no database.

## 4. Running it with Django and the worker (owner's computer)

Three terminals. The backend steps are the runbook's; nothing about them changes.

```powershell
# 1. Django (from backend\; the database settings come from the repository's .env, see runbook section 3)
python manage.py runserver 127.0.0.1:8010

# 2. The worker (from backend\, same .env). Without it requests stay "Queued" forever.
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
| `/` | redirect: `/dashboard` for a user with `intake.view_own_request` or `intake.view_any_request`, else `/requests` (FE-6) | |
| `/dashboard` | `DashboardPage` (FE-6, section 16; FE-DASH-2, section 18) | `GET /api/dashboard/summary/` ×1 per refresh, polled |
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
- **Detail and polling** (`lib/useRequestDetail.ts`). UI-1 polled every 2 s while processing and stopped in every other state.
  **FE-5 replaced this** with the generic live-refresh hook described in section 15 (3 s while processing, slower otherwise,
  quiet errors with back-off, no polling while the tab is hidden or the user is signed out).
- **Shown.** State, processing task state and attempts, the safe failure code, extraction outcome, current revision number,
  full SHA-256 content hash, the draft, findings split into blocking and warnings with their stable `code` and `field_path`,
  approval and rejection counts, and the server's `allowed_actions` and `action_blockers` as read-only information.
  UI-1 had no edit, approve or reject controls; UI-2 added them (section 13).
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
| `npm test` | UI-1 alone: 143 tests in 9 files. With UI-2 (section 13.8): 264 tests in 14 files, all passing |
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

- **Source text.** The API does not return the request text, so the draft cannot be compared with the original in the UI
  (UI-1 and UI-2). The review controls themselves were added in UI-2 (section 13).
- **Fixture extraction.** The text must follow the labelled-line template shown on the form; anything else yields an
  invalid or empty draft. The UI says so.
- **Line endings.** A browser `<textarea>` reports line breaks as `\n`, so CRLF pasted by a user is stored as LF. The
  application does not otherwise change the text.
- **Submitter names.** The API returns the submitter's `public_id` only; the UI shows "You" or the first 8 characters.
- **Same-origin only; no production serving.** `npm run dev` is the only supported way to run it. Django does not serve
  `frontend/dist`, and `BrowserRouter` deep links need a server-side fallback to `index.html` that does not exist yet.
  `vite preview` is not a production server. TLS, HSTS and CSP are open deployment items.
- **Polling, not push.** Intervals are provisional (see section 15 for the FE-5 values and limits). A request with no running
  worker keeps being polled (every 10 s after the first minute).
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

---

## 13. UI-2: review and approval

UI-2 adds the review actions to the request detail page, on top of the existing REST API. **No backend behavior changed and no
business rule is implemented in React**: the server decides what a user may do (`allowed_actions`, `action_blockers`), validates
every draft, and enforces every state, hash, warning and self-approval rule. The UI shows controls only for what the server
allows, sends what the API's serializers accept, and displays the server's answer. The server may still refuse an action the
UI offered (the request may have changed since it was loaded); every such refusal is handled.

### 13.1 What it does

| Area | Behavior |
| --- | --- |
| **Edit draft** | Offered when `allowed_actions` contains `edit` and the detail has a `draft`. A structured form for the requester (name, contact), the items (description, quantity, unit, notes) and the request notes. Nothing is saved until the user presses "Save as new revision". |
| **Approve** | A section shown only when `allowed_actions` contains `approve`. Shows the revision number and full content hash, any findings, and one checkbox per distinct current warning; "Approve revision N…" stays disabled until every warning is ticked, then asks for confirmation. |
| **Reject** | A section shown only when `allowed_actions` contains `reject`. A choice among the API's four reason codes (no free text), then a confirmation. |
| **Audit history** | A section shown to users who hold `audit.view_auditevent`: the request's events, oldest first, 20 per page. |
| **Conflicts** | A stale revision (someone else saved first) never discards or overwrites anything silently (13.3). |

After every successful action the page loads the request and its audit history again from the server; nothing is updated locally.
Approve and Reject are disabled, with an explanation, while a draft edit is open, so the wrong revision cannot be approved by
accident.

### 13.2 API contracts used

All paths are under `/api/requests/{run_id}/` and need the session cookie; unsafe requests carry `X-CSRFToken` (section 6). The
field names are those of `backend/apps/api/serializers.py`, and the refusal codes are those of `apps/intake/review_rules.py`.

| Action | Request | Success | Refusals the UI handles |
| --- | --- | --- | --- |
| Edit | `POST edit/` `{"base_revision_id": uuid or null, "draft": {requester: {name, contact}, items: [{description, quantity, unit, notes}], notes, source_refs: [{field_path, excerpt, line}]}}`. Every field may be `null`; `quantity` is decimal **text**. | `200` `{revision_id, revision_number, content_hash, run_state, warnings, approval_invalidated}` | `400 draft_invalid` (`details.findings`), `400 validation_error` (`details.fields`), `400 draft_not_serializable`, `409 stale_base_revision`, `409 unchanged_content`, `409 run_state_not_editable`, `409 nothing_to_edit`, `403 permission_denied` |
| Approve | `POST approve/` `{"revision_id", "content_hash", "acknowledged_warnings": [{code, field_path}]}` (exactly these keys) | `200` `{approval_id, revision_id, content_hash, run_state, acknowledged_warnings}` | `403 self_approval_forbidden`, `409 stale_revision`, `409 content_hash_mismatch`, `409 run_not_in_review`, `409 revision_rejected`, `400 warnings_not_acknowledged`, `400 unknown_warning_acknowledged`, `403 permission_denied` |
| Reject | `POST reject/` `{"revision_id", "content_hash", "reason_code"}` | `200` `{rejection_id, revision_id, reason_code, run_state}`; the run stays in `needs_review` | `409 stale_revision`, `409 content_hash_mismatch`, `409 run_not_in_review`, `400 validation_error` (unknown reason), `403 permission_denied` |
| Audit | `GET audit/?page&page_size` | `200` `{count, page, page_size, results: [{id, occurred_at, actor_kind, actor_public_id, action, subject_type, subject_id, details}]}` | `403 permission_denied` |

`GET /api/requests/{run_id}/` (UI-1) supplies everything the actions need: `current_revision` (id, number, hash), `draft`,
`draft_source`, `findings`, `warnings`, `allowed_actions` and `action_blockers`.

**Mirrored constants.** The API has no endpoint that lists the four rejection reason codes, so `frontend/src/api/types.ts` repeats
them (and the run states and review actions). `backend/tests/test_frontend_contract.py` (DB-free) reads that file and fails if
the lists differ from `apps.intake.policy.REJECTION_REASONS`, `apps.cases.run_state.STATES` and `apps.intake.access.Action`.

### 13.3 Draft editing

- **Initial values.** From `detail.draft`: the current revision's draft, or, for a request whose extraction was invalid (no revision
  yet), the extraction result. The base revision is `current_revision.id`, or `null` for that first edit (saving then creates
  revision 1).
- **Exact preservation.** A field the user did not touch is sent back exactly as received: `null` stays `null`, `""` stays `""`,
  `"2.50"` stays `"2.50"`, and text with `\r\n` keeps it (a textarea shows `\n`, so the two are compared as equal). A touched field
  is sent as typed, **without trimming**; a touched field that is emptied is sent as `null`, never as a made-up value. Quantities
  are text inputs and are never converted to a number. `source_refs` are sent back unchanged.
- **Items.** An item can be added (empty, no references) or removed. A source reference names an item by position and the server
  refuses a reference to a missing item (a blocking `invalid_source_ref`), so removing an item drops its references and renumbers
  the later ones; the editor says how many were dropped. Items cannot be reordered. After an add or remove, earlier findings are
  cleared, because their item positions no longer match.
- **Findings beside fields.** A `400 draft_invalid` shows each finding (code and the server's message) under its field
  (`requester.name`, `items[2].quantity`, ...), marks blocking fields `aria-invalid`, and links the text with `aria-describedby`. A
  finding for a path with no field (for example `source_refs[0]`) is listed under "Other findings", not dropped. The warnings of
  the loaded revision appear beside their fields as soon as the editor opens. The user's text is always kept.
- **Stale revision.** If the server answers `409 stale_base_revision`, the form keeps what the user typed, fetches the latest
  version, locks the form, and offers: **show** the latest version; **discard** my changes and load the latest; or **keep my
  version and base it on the latest**. The last choice is explicit and warns that saving will replace the content of that
  revision; the user must then press Save again. Nothing is retried automatically.
- **Other refusals.** `unchanged_content`, `run_state_not_editable` and similar answers are explained, the page reloads the
  request where the state changed, and the form keeps the user's text.
- **No duplicate saves.** The button is disabled while saving and a second click is ignored. Cancel with unsaved changes asks
  first (Escape keeps editing).

### 13.4 Approval and rejection

- The sections exist only when the server lists the action in `allowed_actions`; the user's permission list from `/api/auth/me/`
  is not consulted for this (a test covers a profile without the permission but a server that allows the action, and the
  reverse). A request the user submitted or edited has `approve` in `action_blockers` with `self_approval_forbidden`, shown as
  text; if the server still refuses with that code, the message is shown and the request is reloaded.
- **Warning acknowledgement.** One checkbox per distinct `(code, field_path)` warning, with the server's explanation and
  excerpt; there is no "tick all". The request sends exactly those warnings. After a stale refusal the new revision has its own
  warnings and all boxes start unticked.
- **Confirmation.** Approve and Reject each open an inline confirmation (it takes focus, Escape cancels, both buttons are
  disabled while the request is in flight). Rejection states that the revision can no longer be approved.
- **After the answer.** Success shows a message and reloads the request and the audit history. A refusal that means the page was
  out of date (`stale_revision`, `content_hash_mismatch`, `run_not_in_review`, `revision_rejected`, a warnings mismatch,
  `self_approval_forbidden`, ...) shows its message and reloads. A permission refusal or a network error shows its message and does
  not claim success.

### 13.5 Audit history

Columns: time, action, actor, details. The actor is exactly what the API sends: **System**, **You**, or the first 8 characters of
the public id (the full id is the tooltip). **The API returns no user names, so none are shown.** The revision reference is the
event's `revision_number` or `revision_id` when it has one; hashes and ids are shortened. Unknown actions and detail keys are shown
as the raw codes. Everything is rendered as text. The section is not requested at all for a user without
`audit.view_auditevent`; a `403` from the server is shown without a retry button.

### 13.6 Role requirements (the backend's initial policy)

| Role | Edit | Approve / reject | Audit history |
| --- | --- | --- | --- |
| Operator | own requests, in `needs_review` or `approved` | no | no |
| Approver | no | any request in `needs_review`, except one they submitted or edited (for approve) | no |
| Auditor | no | no | yes |
| Superuser | yes | yes, **but not approve for a request they submitted or edited** | yes |

Editing a request that is `approved` returns it to `needs_review` and invalidates the earlier approval (the success message says
so). A rejected revision cannot be approved; editing creates a new revision.

### 13.7 Local browser validation (owner)

Prerequisites as in section 11, plus roles: an **Operator**, an **Approver** (a different person than the Operator), and
optionally an **Auditor**. Use synthetic data. To get a warning to acknowledge, either submit a request with two identical
`Item:` lines (expected: a `duplicate_item` warning) or edit an item description to text that is not in the request (expected: a
`value_not_in_source` warning on the new revision).

1. **Edit and save (Operator).** Open a request in Needs review, press Edit draft. Check the fields match the draft, change an
   item description, press Save. Expect "Saved as revision 2", a new content hash, and that the quantity `2.50` still reads `2.50`.
2. **Nothing saves by itself.** Open the editor, type, wait: the Network tab shows no `POST .../edit/` until you press Save.
3. **Findings beside fields.** Clear the requester name and Save: a blocking finding appears under the Name field, the field is
   outlined, your other text is intact, and no new revision number appears.
4. **Items.** Add an item and save it empty (expect blocking findings); remove an item and confirm the note about dropped
   references.
5. **Conflict.** Open the editor in two browser windows as the same user, save in window A, then save in window B: window B shows
   the conflict panel, keeps your text, and the Save button is disabled until you choose. Try "Show the latest version", then
   each choice.
6. **Approval (Approver).** Open the Operator's request. Expect the Approve section with revision number and hash. With a warning,
   the button stays disabled until the box is ticked. Approve, confirm: "Revision N approved", state Approved.
7. **Role gating and self-approval.** (a) As the Operator, open your own request in review: no Approve section (an Operator
   holds no approve permission, so the server lists no approve action and no blocker). (b) For the self-approval rule itself you
   need a user who *does* hold the approve permission and also submitted or edited the request: a superuser, or a user in both the
   Operator and Approver groups. Open such a request: no Approve section, and the text says approval is not possible because you
   submitted or edited it. A superuser is bound by the same rule.
8. **Rejection (Approver).** On another request choose a reason, confirm. Expect "rejected" and Approve now not possible
   ("the current revision was rejected"). Edit as the Operator: the new revision can be approved again.
9. **Stale approval.** The Approver has the request open; the Operator saves a new revision; the Approver clicks Approve and
   confirms: expect "changed after you opened it, so nothing was recorded", the new revision shown, warnings unticked.
10. **Audit (Auditor).** Open a request: events oldest first with System, You or short ids, revision numbers and reason codes;
    Operator and Approver see no audit section.
11. **Console and storage.** Developer tools: no `console` output from the app, and Local Storage, Session Storage and IndexedDB
    stay empty.

Report anything that differs with the exact message. Failures here are integration findings.

### 13.7a Owner browser validation: result (recorded 2026-11-16)

**Provenance.** This is **owner-supplied local browser evidence**. The owner ran the workflow on the local Windows computer and
described the screenshots to Claude in text. The screenshots are not stored in this repository, and the development VM (which has
no PostgreSQL and no browser session against the stack) neither ran nor observed any of it. Claude did not see the screenshots
themselves; nothing below is stronger than the owner's description.

**Setup (as reported).** The React app (Vite dev server) connected through the same-origin proxy to the real Django REST API, the
owner's local PostgreSQL database and the persistent `runworker` process. No mock backend was involved. Request
`REQ-20261116-B666BD8`; accounts `smoke-operator` (Operator) and `smoke-approver` (Approver), plus one account authorized to view
the audit history (its role was not stated).

**Observed workflow, as reported:**

| Step | Reported result |
| --- | --- |
| A. Submit and extract | A request was submitted through React. The worker completed extraction; the task showed as succeeded on attempt 1; the run reached `needs_review` with revision 1. |
| B. Edit | `smoke-operator` edited the draft in the editor: requester changed to "Ivan Test", quantity changed from 2 to 3 (boxes), description "A4 paper" left as it was. Saving created revision 2 with a new SHA-256 content hash. Two `value_not_in_source` warnings were displayed, for `requester.name` and `items[0].quantity`. |
| C. Authorization | The Operator who submitted and edited the request could not approve it, and the UI offered only the actions the backend permitted. |
| D. Approval | A separate user, `smoke-approver`, logged in and approved revision 2. The UI showed "Revision 2 approved."; the state became approved; approvals/rejections read 1/0; the two warnings stayed visible. |
| E. Audit history | An authorized account saw seven events in the real UI: request submitted; revision 1 created; run state changed to `needs_review`; revision 2 created; draft edited; revision 2 approved; run state changed to `approved`. The editing and approval events showed different actor identifiers, and the approval event recorded two warnings. |

**What this supports (for this one workflow, in this environment).**

- The UI-2 editor, approval and audit screens work against the real API, PostgreSQL and worker for the reported path: a base
  revision was accepted, a new revision with a new content hash was created, and an approval by a second user was recorded.
- Approval succeeded for a revision with two warnings. The backend accepts an approval only when the acknowledged warnings equal
  the revision's current warnings exactly, and the audit event recorded two, which is consistent with both having been sent. The
  screenshots do **not** independently show each checkbox click.
- The audit history showed distinct actor identifiers for the edit and the approval, as the API returns them (identifiers, not
  names).
- Control visibility followed the backend: the Operator was not offered approval. This shows role-based gating (an Operator holds
  no approve permission); it does **not** demonstrate the self-approval rule specifically, which applies to a user who *does* hold
  the approve permission.

**Not demonstrated in the supplied evidence** (so not claimed as validated): the stale-revision conflict panel and its three choices;
rejection and its confirmation; validation findings beside fields after a rejected draft (`draft_invalid`); adding and removing
items and the source-reference renumbering; the first edit of a request whose extraction was invalid; the stale-approval path and
warning re-acknowledgement on a new revision; the "nothing saves by itself" check; keyboard-only use; narrow-screen layout; browser
console and storage inspection (13.7 step 11); the self-approval blocker text of step 7(b); an audit history longer than one page;
and any browser other than the one used. Checklist steps 2 to 5, 7(b), 8, 9 and 11 therefore remain open. Steps 1, 6, 7(a) and 10
correspond to what was reported, only approximately: the executed edit changed the requester and a quantity, not the item
description used as the checklist's example, and the audit account's role was not stated.

**Status.** UI-2 is **development-validated for the tested workflow**. It is not production-ready: see 13.9 and section 10.

### 13.8 Tests and what was run (UI-2)

Run in the development VM; no `AP_*` variable was set and no PostgreSQL was contacted.

| Check | Result |
| --- | --- |
| `npm test` | 264 tests in 14 files, all passing |
| `npm run typecheck` | Clean |
| `npm run build` | Succeeds (121 modules; about 311 kB JavaScript, 96 kB gzipped) |
| `manage.py test` (backend DB-free) | `Ran 1246 tests`, OK (1243 earlier plus 3 contract tests) |
| `manage.py check`, `compileall` | Clean |

New test files: `lib/draftForm.test.ts` (initialization, exact preservation, decimals, dirty tracking, item removal and reference
renumbering, finding placement), `api/review.test.ts` (request shapes, CSRF, error codes), `pages/EditDraft.test.tsx`
(initialization, saving, field findings, first edit, stale conflict, items, leaving), `pages/ApproveReject.test.tsx`
(`allowed_actions` gating, warning acknowledgement, confirmation, self-approval, stale and refused answers, rejection reasons, no
duplicate submission, keyboard use), `pages/AuditHistory.test.tsx` (permission gating, rendering, actors, paging, errors), more
cases in `lib/errors.test.ts`, and the source scan (`security.test.ts`) now also covers the new components. One UI-1 test, which
asserted that no review buttons existed, was rewritten.

Mutation check: eight deliberate defects were introduced one at a time (approve enabled without acknowledging warnings; edit
always sending a null base revision; untouched fields not sent back exactly; the stale-revision flow removed; Approve shown
regardless of `allowed_actions`; rejection without confirmation; the audit section shown without the permission; approval
without the acknowledgement). Each was caught by at least two tests, and the code was restored. The contract test was checked by
adding a fake reason to the frontend list.

**Defect found in the pre-commit review and fixed.** The Approve and Reject panels kept their local state (ticked warnings, the
chosen reason) across a change of revision, so an acknowledgement given for revision 1 would have silently applied to a revision 2
that has a warning with the same code and path but different text. Both panels are now keyed by the revision's id and content
hash, so a new revision always starts with nothing ticked and no reason chosen. Two tests cover it (they fail without the fix,
checked by removing it), and the first review of the stale-approval case had missed it because its test used a *different* warning.

**What these tests cannot show.** They use a stubbed `fetch` and jsdom: a mismatch with the real server's responses, the real
timing of concurrent edits, and anything visual (layout, focus rings, small screens) are not verified by them. One workflow was
later run by the owner against the real stack (13.7a); the rest of the 13.7 checklist has not been reported.

### 13.9 Remaining limitations

- **Validation scope.** UI-2 is development-validated for one reported workflow (13.7a) and by VM tests against a stubbed
  `fetch`. The conflict flow, rejection, findings after a rejected draft, item add and remove, keyboard-only use, small screens and
  the console and storage checks have not been shown in a real browser. Nothing here claims that all possible UI behavior was tested.
- **Not production-ready.** No Windows installer, no production serving configuration (Django does not serve `frontend/dist`; TLS,
  HSTS and CSP are undecided), and the login throttle is per process.
- **Not built, by decision of scope:** real AI (extraction is still the deterministic fixture parser) and document upload.

- **No source text.** The API does not return the request text, so a reviewer compares the draft only with the findings'
  excerpts. This is the most important gap for review quality; it needs a backend decision about who may read the text.
- **No revision history or diff.** Only the current revision is shown (the audit history lists the revisions' numbers). The stale
  conflict panel shows the latest draft but does not highlight differences.
- **Actor names.** The audit history shows ids, not names, because the API returns none. Showing names needs an API change and a
  privacy decision.
- **Item order is fixed**, and source references cannot be edited; they are kept or, for a removed item, dropped.
- **Rejection reason list is mirrored** from the backend (13.2); the contract test catches drift, but a new reason also needs a
  frontend label.
- **Self-approval for a lone user.** The backend forbids approving a request one submitted or edited, including for a superuser, so
  a one-account installation cannot complete approval. This is a product decision, not a UI defect.
- **Editing is per tab.** Two tabs editing the same request are handled by the stale-revision flow, not by locking.
- **Focus after cancelling a confirmation.** The confirmation takes keyboard focus when it opens, but when it is cancelled
  (button or Escape) focus is not returned to the button that opened it; the next Tab starts from the top of the page section.
- **Audit paging.** After an action the audit history reloads the page you are on. Events are oldest first, so with more than 20
  events the new ones are on the last page, which you have to open yourself.
- **Everything in section 10 still applies:** no production serving, polling only, per-process login throttling, fixture extraction.

### 13.10 Files (UI-2)

Documentation recorded after the owner's browser validation (13.7a): this document, `local-validation-record.md`,
`docs/architecture/implementation-plan.md`, and the status lines in `README.md`, `CLAUDE.md`, `docs/development/runbook.md` and
`docs/architecture/overview.md`. No executable code changed in that step.

New: `frontend/src/api/review.ts`, `lib/draftForm.ts`, `components/DraftEditor.tsx`, `ApprovePanel.tsx`, `RejectPanel.tsx`,
`ConfirmStep.tsx`, `AuditHistory.tsx`, the test files above, `src/test/review.ts`, and `backend/tests/test_frontend_contract.py`.
Changed: `frontend/src/api/types.ts`, `lib/errors.ts`, `components/Pagination.tsx`, `pages/RequestDetailPage.tsx`, `styles.css`, one
UI-1 test, `README.md`, `CLAUDE.md`, `docs/development/runbook.md`, and this document. No backend behavior, model, migration or
dependency changed.

## 14. FE-4: app shell and design system

Presentation only. No API call, request or response shape, permission check, or authorization rule changed, and no dependency was added.
Every control still follows the server's `allowed_actions` and the profile's `permissions`; nothing was added that decides who may do what.

### 14.1 What it adds

- **Design system** (`src/styles/`, imported by `src/styles.css`): `tokens.css` (palette, type, spacing, radius, shadow, layout and motion
  tokens; breakpoints 640, 900 and 1100 px), `base.css` (reset, visible `:focus-visible` ring, skip link, reduced-motion and
  forced-colors rules), `components.css`, `shell.css`, `pages.css`. Plain CSS with custom properties; no UI or icon library (icons are
  inline SVG in `components/ui/Icon.tsx`).
- **Themes:** light and dark palettes. `src/theme/ThemeProvider.tsx` offers Light, Dark and System (the default: follows the operating
  system). The choice sets `data-theme` on the document element.
- **Shell** (`components/Layout.tsx`, `components/shell/`): a collapsible sidebar (icons only when collapsed), a top bar, an account menu
  (user, roles, theme, Sign out) and one content container. Below 900 px the sidebar is an off-canvas drawer: the page behind it is
  `inert`, Escape, the backdrop, the close button or following a link closes it, and focus returns to a sensible element. The navigation
  lists only what exists: **Requests** and **New request** (shown only with `intake.submit_request`). There is no Audit page: audit history
  is part of the request page, as before, and all routes are unchanged.
- **Components:** `PageHeader` (breadcrumbs, one `<h1>`, tab title), `ui/Toast` (`role=status` for success and info, auto-dismissed after
  6 s and paused on hover or focus; `role=alert` for errors, which stay until dismissed), `ui/Skeleton`, status/empty/error states,
  `StateBadge` with a dot and always a text label.
- **Requests:** quick-filter chips (same `state` parameter as the select), a table with a per-row Review/View link, optional columns hidden
  on narrow screens, skeleton loading and richer empty states (the original texts are kept).
- **Request page:** two columns from 1100 px (content, then a review column). Allowed actions are grouped under **Operator** (edit) and
  **Approver** (approve, reject); Approve and Reject sit together under "Approver decision". The grouping only presents what the server
  allows; it grants nothing. Audit history is below.
- **Toast use:** only the "Request submitted and queued for extraction." confirmation. Results of review actions stay as inline notices.

### 14.2 Decisions and limitations

- **The theme choice is not saved.** `security.test.ts` forbids `localStorage`, `sessionStorage` and `document.cookie` in application code,
  so the choice lasts until the page is reloaded and then returns to System. Persisting it needs a narrowly scoped, owner-approved
  exception to that guard (or a server-side preference); it was not changed here.
- **No ESLint.** The project has no ESLint dependency or configuration, and none was added. The checks that exist are the TypeScript
  compiler (`npm run typecheck`, strict) and the tests, including the static source guards.
- **Not seen in a browser by the development VM.** Vitest does not process CSS, so layout, the drawer's visibility, colours and
  breakpoints are not exercised by tests (the drawer and collapse tests check state attributes). Text contrast of the palette pairs and
  non-text border contrast were computed from the token values (all pass 4.5:1 for text and 3:1 for control borders and focus rings, in
  both themes), which is not a substitute for a visual review or an accessibility audit with assistive technology.
- The account menu is a disclosure (a button showing a panel), not an ARIA menu; Sign out is reached by opening it first.
- Collapse state is not remembered (same storage restriction).
- Everything in sections 10 and 13.9 still applies.

### 14.3 Tests (development VM)

New: `components/shell/shell.test.tsx` (navigation and `aria-current`, permission-gated link, skip link and landmarks, page header and tab
title, drawer open/close/Escape/link/backdrop and focus, sidebar collapse, account menu, theme switching, toasts) and
`pages/designSystem.test.tsx` (status badges for every state and an unknown one, table row actions, quick filters, review action grouping
and availability). `src/test/shell.ts` opens the account menu for the four existing sign-out tests, and one existing assertion
(`ApproveReject.test.tsx`, "sends nothing at all when no action is allowed") was anchored to button names that *start with* the
action, because the account button is now named after the signed-in user, who may be called `synthetic.approver`.

### 14.4 Visual polish after browser review

Styling only; no behaviour, API or test change. The sidebar is wider (17rem) and the collapse toggle moved from the header to a footer
button below the navigation, so the full application name has the whole header width and wraps instead of being truncated. A collapsed
sidebar keeps the brand mark. The content container is wider (92rem) with fluid padding, the topbar and page header have a clearer
hierarchy, and the requests table is denser (zebra rows, hover and focus-within highlight, monospace references, right-aligned tabular
revision numbers, no-wrap dates). Filters, pagination bar, empty and loading states are restyled. These changes are CSS that Vitest does
not process, so they have not been seen in a browser by the development VM; the theme is still not persisted.

## 15. FE-5: live updates and background synchronization

Frontend only. No backend, API, payload, migration or dependency change (TanStack Query is not installed and was not added; a small
custom hook was enough). No WebSocket, SSE or new endpoint: the app polls the existing `GET /api/requests/` and
`GET /api/requests/{run_id}/`.

### 15.1 Polling architecture

`lib/useLiveResource.ts` is the one polling mechanism. `useRequestDetail` and the requests list both use it; `lib/liveIntervals.ts`
holds the intervals.

| Screen | Interval |
|---|---|
| Requests list | 5 s |
| Detail, processing (`queued`, `extracting`, `exporting`) | 3 s for the first 60 refreshes, then 10 s |
| Detail, `needs_review` | 10 s |
| Detail, `approved`, `exported`, `failed`, `cancelled` | 30 s |

The intervals are provisional, not measured. Rules the hook enforces:

- **One request at a time per resource.** The next timer starts only after the previous request has finished, so refreshes never
  overlap; a visibility event while a request is in flight does not start a second one.
- **Cancellation.** Each effect run owns an `AbortController`. Unmount, a change of route or query parameters (page, state filter),
  logout and a manual `reload()` abort the request in flight; a response that arrives after its effect was cleaned up is ignored,
  even where the abort could not stop it.
- **Authentication.** Nothing is fetched or scheduled while the user is not authenticated. Logging out cancels the timer and the
  request. A `not_authenticated` answer still goes through the existing unauthenticated handler, which signs the user out.
- **Visibility.** While `document.visibilityState` is `hidden` no timer is set (a timer pending when the tab is hidden is cleared).
  When the tab becomes visible the data refreshes at once and polling resumes. The first load happens even in a hidden tab.
- **No duplicate polling.** The effect depends only on the resource key (the stringified dependencies), a reload counter and the
  authenticated flag, and its cleanup removes the timer and the `visibilitychange` listener.
- **Refresh in place.** A refresh, including `reload()`, keeps the previous data and sets `refreshing`; only a change of the key
  returns to the `loading` state (skeleton). The list therefore keeps its rows, filters, page, URL parameters and scroll position.
- **Errors.** A failed refresh keeps the last good data and sets `refreshError`. Network errors, 429, 5xx and unexpected responses
  are retried with back-off (the interval doubled per failure, at most 30 s); other 4xx stop polling until the user presses
  "Try again" or refreshes. A failure before any data has been loaded is the normal error screen.

### 15.2 UI

`components/LiveStatus.tsx` shows, next to the Refresh button on the list and at the top of the detail page: "Refreshing…" while a
background request runs, "Updated HH:MM:SS" after each success, and "Could not refresh: …" with "Try again" when a refresh fails
(a status region, not an alert, so it does not interrupt). Background refreshes never replace the page with a skeleton.

`lib/useStateTransitionToasts.ts` raises one toast when a request is seen moving to `needs_review` ("Request R is ready for
review.") or `failed` ("Request R failed during processing."). It only uses the request reference. The first data of a page is
a baseline, so opening a page never notifies; each request and state is notified once, so later refreshes and repeated polls
say nothing more.

### 15.3 Mutations

Submit, edit, approve and reject keep their existing code paths, payloads, CSRF header, revision-id and content-hash checks and
their in-flight guards (the button is disabled while a submission runs, so a second click cannot send a second request). After a
success the page calls `reload()`: it aborts any poll in flight (so an older answer cannot overwrite the newer data) and refreshes
at once. The audit history reloads when the state, current revision, or approval or rejection counts change, not on every poll.

### 15.4 Dirty-form protection and stale revisions

`DraftEditor` still reads its form state from props once, so a refresh never changes what the user has typed. FE-5 adds a
`seenRevisions` set and compares it with the revision the server now reports:

- Form **untouched**: it updates itself to the newer revision and says so ("Updated to revision N, which was saved elsewhere").
- Form **edited** (and not saving): the draft stays as typed, the existing stale-revision conflict banner appears, and the fields
  and save button are locked until the user chooses explicitly "Keep my version and base it on the latest" or "Discard my changes and load the latest". This is the
  same flow as the existing 409 `stale_base_revision` handling, which still applies when saving.
- A revision the user has already chosen to build on is added to `seenRevisions`, so a poll that still shows the older revision
  cannot raise the conflict again.

### 15.5 Tests (development VM, jsdom, `fetch` stubbed, fake timers)

`pages/LiveUpdates.test.tsx` (34 tests) covers: the intervals, query and filter preservation, rows kept during a refresh, no
overlap, a stale filter response ignored and aborted, hidden pause and visible resume, logout and unmount cancellation, no
polling when unauthenticated, network failure and recovery, route-change abort, dirty-form protection, four stale-revision scenarios,
a refresh after approval that ignores an older in-flight answer, and notification dedupe. `pages/RequestDetailPage.test.tsx` was
updated for the new intervals. The full run was 17 files, 328 tests passed, with `npm run typecheck`, `npm run build` and
`git diff --check` clean.

### 15.6 Limitations

- **Not seen in a real browser.** Nothing in FE-5 was run against the local Django, PostgreSQL and worker, and no owner validation
  is recorded; the tests use a stubbed `fetch` and fake timers.
- Polling is per tab: every open tab polls on its own. There is no push, no shared poll between tabs and no server hint to back off.
- Intervals are provisional and chosen without load measurements. A request with no worker keeps polling (every 10 s after the first minute).
- Findings and warnings inside an open editor are not live-updated; they refresh with the page data around it, and the editor's own
  form keeps the user's values.
- The audit history refreshes on a detected change or after an action, not on a timer.
- Notifications cover only `needs_review` and `failed`, and are seen only by a user with the page open; there is no notification
  for requests the user is not currently viewing.
- A 4xx other than 429 stops background polling until the user retries.

## 16. FE-6: dashboard

> **Superseded in part by FE-DASH-2 (section 18).** Sections 16.1 and 16.2 describe the FE-6 data layer (nine `GET /api/requests/`
> requests per refresh, `lib/dashboardData.ts`), which no longer exists: the dashboard now sends one `GET /api/dashboard/summary/`
> request. They are kept as the record of what FE-6 was; the UI (16.3), polling model (16.4) and permissions (16.5) still hold.

Frontend only. No backend, API, payload, migration or dependency change, and no Workbench request. The dashboard is a new
screen inside the existing app shell (`pages/DashboardPage.tsx`, `lib/dashboardData.ts` (removed in FE-DASH-2), `lib/permissions.ts`); it reuses
`useLiveResource`, `LiveStatus`, `PageHeader`, `StateBadge`, the status components and the FE-4 styles.

### 16.1 Data sources (only `GET /api/requests/`)

The API has no statistics endpoint and the dashboard does not invent one. Every list response carries `count`: the total of all
requests **the signed-in user may view** that match the optional `state` filter, whatever `page_size` is. One refresh therefore sends,
all at once:

- one unfiltered request, `page=1&page_size=5`: its `results` are the recent requests (newest first, as the server orders them)
  and its `count` is **Total requests**;
- one request per run state (eight), `page=1&page_size=1&state=<state>`: only the `count` is used.

| Card | Source |
|---|---|
| Total requests | `count` of the unfiltered request |
| Processing | `count(queued) + count(extracting) + count(exporting)` (the states `lib/format.ts` calls processing) |
| Needs review | `count(needs_review)` |
| Approved | `count(approved)` (`exporting` and `exported` are not included; they appear in the distribution) |
| Failed | `count(failed)` |
| Status distribution | all eight per-state counts, with a share of the total when the total is known |

Nothing is ever added up from the rows of a page. Processing is the only derived figure, and it is a sum of server counts.

### 16.2 Accuracy rules and limits

- **Unavailable is not zero.** Each answer stands on its own (`Promise.allSettled`). A figure whose request failed shows
  "Unavailable" (with a tooltip saying it is not zero). Processing is unavailable when any of its three states is, because a partial
  sum would be wrong. A real `0` shows as `0`.
- If **every** request fails the first error is thrown: a first load shows the normal error state (a 403 is "Not available for your
  account", with no retry button), and a background refresh keeps the last good data with the quiet "Could not refresh" note.
- If only the recent-requests request fails, that panel shows a warning and the figures still show; if only some counts fail, the
  rest still show. A background refresh replaces a figure by "Unavailable" if its request failed that time rather than keeping a
  possibly stale number under a new "Updated" time.
- **Not a snapshot.** The nine requests are separate and a moment apart. A request that changes state between them can make the
  per-state counts differ from the total by a little; the dashboard says so when all figures are known and they disagree
  (`data-testid="counts-disagree"`), and the next refresh corrects it.
- **Visibility.** The server decides what is counted. A user with only `intake.view_own_request` sees counts of their own requests;
  a user with `intake.view_any_request` (Approver, Auditor, and so on) sees all of them. The sentence under the title is chosen from
  the permissions in `/api/auth/me/` and is a wording hint only; it never changes a count.
- **Cost.** Nine small list requests per refresh, every 10 s per open dashboard tab: 54 requests a minute, against the API's
  per-user ceiling of 600 a minute (`api_user` in `config/settings/base.py`; each open tab counts, and a 429 is retried with
  back-off by the hook). The count queries run in PostgreSQL per state and their load has not been measured. A server-side
  summary endpoint would reduce this to one request and is the recommended follow-up.

### 16.3 UI

Responsive KPI cards (`auto-fit` grid; Total, Needs review, Approved and Failed link to `/requests` with the matching `?state=`;
Processing is three states and the list filters by one, so it is not a link), a recent-requests table (reference, state, created,
Review or View), a status-distribution list (each row links to its filter), quick actions (New request only with
`intake.submit_request`, Review requests, All requests), skeleton cards while the first load runs, and an empty state with a New
request action (only for users who may submit) when there are no requests. Light, dark and system themes use the existing tokens;
transitions are disabled under `prefers-reduced-motion`.

Accessibility: one `h1`, `h2` per section (the card group has a visually hidden one), each card's accessible name is its label and
value, the distribution does not rely on colour or the bar (the state name and exact count are text), and a visually hidden
`role="status"` region holds a plain-text summary of the five figures; its text changes only when a figure changes, so a
screen reader is told about changes and not about every poll.

### 16.4 Polling

`useLiveResource` with `DASHBOARD_POLL_MS = 10000` (`lib/liveIntervals.ts`): one refresh at a time (all nine requests belong to one
refresh, so refreshes cannot overlap), no timer while the tab is hidden, an immediate refresh when it becomes visible, abort on
unmount, route change and logout, nothing fetched while signed out, refresh in place with the data kept on screen, quiet error with
back-off. A Refresh button refreshes at once. All behaviour is the hook's, described in section 15.1.

### 16.5 Permissions and navigation

- Sidebar: **Dashboard** is the first entry and is shown only to a user with `intake.view_own_request` or `intake.view_any_request`
  (`NavItem.anyPermission`); the other entries and their rules are unchanged. The brand link now goes to `/`.
- `/` redirects to `/dashboard` for those users and to `/requests` for everyone else; the login page's default destination is `/`, so
  the same rule applies after sign-in. A deep link (for example `/requests/<id>`) still returns the user to it after login.
- `/dashboard` for a user without a view permission shows "Not available for your account" and makes no request. This is a display
  hint; the backend still decides every call. `is_superuser` is not special-cased, as elsewhere in the UI: a superuser sees the
  dashboard when `/api/auth/me/` lists a view permission for them.
- `/requests`, `/requests/new`, `/requests/:runId` and the not-found page are unchanged.

### 16.6 Tests (development VM, jsdom, `fetch` stubbed, fake timers)

`pages/DashboardPage.test.tsx` (43 tests): routing and landing, permission-based visibility and wording, exact counts with
22 requests against 5 rows and one row per state, the request shapes (`page_size` 5 and 1, one per state), a real zero, each missing
figure (a state, the total, the recent list, Processing), disagreement note, distribution shares, card links and accessible names,
recent requests and empty states, skeleton and error states with retry and 403, no duplicate first load, refresh in place every
10 s, no overlap, quiet refresh error, hidden-tab pause and visible resume, a hidden first load, abort on logout and on leaving the
page, the Refresh button, navigation from cards, quick actions and the sidebar, existing routes, and login landing.
`shell.test.tsx` was updated for the new first sidebar entry. Result: 18 files, 371 tests passed; `npm run typecheck`,
`npm run build` and `git diff --check` clean.

### 16.7 Remaining gaps

- **Not seen in a real browser.** No owner validation is recorded for FE-6; visual layout, dark theme and the 10 s refresh against a real
  Django, PostgreSQL and worker are untested.
- No history or trend, and no counts by period. The server-side summary endpoint (BE-DASH-1, section 17) is now used (FE-DASH-2, section 18).
- No AI usage, cost, worker or provider-health figures: the API has no endpoint for any of them, so none are shown.
- "Approved" excludes `exporting` and `exported`; no export exists yet, so both are expected to be 0.
- No stale-while-error indication per figure: a figure whose refresh failed shows "Unavailable" until the next success.
- A superuser whose profile lists no view permission lands on `/requests` as before.
- FE-7, AI Settings and document upload are not part of this slice.

## 17. BE-DASH-1: dashboard summary endpoint (backend only; the frontend was integrated later, in FE-DASH-2, section 18)

`GET /api/dashboard/summary/` was added so that a later frontend slice can replace the nine requests of section 16.1 with one. **This
slice changes no frontend file**: at that point the dashboard still sent nine `GET /api/requests/` requests per refresh, and the cost in
16.2 was unchanged until FE-DASH-2. The endpoint's contract, permission rules, query strategy and tests are in
`rest-api-slice.md` section 18; this section records what the frontend had to do with it.

- **Mapping.** `total`, `needs_review`, `approved` and `failed` are the figures of the same names; `processing` is queued + extracting
  + exporting, computed by the server from the same grouped result (the frontend's `PROCESSING_STATES` and the server's
  `PROCESSING_STATES` in `apps/intake/queries.py` are two copies of one definition, pinned by a test on the server side only);
  `by_state` carries all eight states, each present and zero when empty, for the distribution; `recent_requests` has the five newest
  items in exactly the `RequestListItem` shape the dashboard already renders.
- **Accuracy.** The counts are one grouped query, so `total` always equals the sum of `by_state`; the "counts disagree" note of 16.2
  becomes unnecessary for them. The recent list is a second statement and can be a moment newer than the counts.
- **Failure.** One request means one error state: there is no longer a per-figure "Unavailable". A 403 means the user has no view
  permission, exactly as for the list.
- **Not done here:** the frontend change (done in section 18), a polling-interval review, and removal of `dashboardData.ts`'s nine-request path (done in section 18).

## 18. FE-DASH-2: the dashboard uses the summary endpoint

Frontend only. No backend, API, payload, migration or dependency change, no Workbench request. **Status: development-validated in jsdom
with `fetch` stubbed; NOT seen in a real browser and NOT run against the real Django, PostgreSQL and worker.**

### 18.1 What changed

- **New client** `api/dashboard.ts` (`getDashboardSummary(signal)`): one `GET /api/dashboard/summary/` through the existing `request()`
  in `api/http.ts` (same-origin, session cookie, `cache: no-store`, the 403 `not_authenticated` handling and the abort signal; GET sends
  no CSRF header, as for every safe request). It takes no query parameters. Type `DashboardSummary` in `api/types.ts`.
- **Contract, checked and not invented** against `backend/apps/api/serializers.py` (`DashboardSummaryOutput`) and
  `apps/intake/queries.py`: `total`, `processing`, `needs_review`, `approved`, `failed` (non-negative integers), `by_state` (exactly the
  eight run states, each a non-negative integer) and `recent_requests` (the `RequestListItem` shape, newest first). A body that does not
  match (missing or negative count, a missing or extra state key, a row the dashboard cannot render, not JSON) is an
  `unexpected_response` error, never zeros. The client does **not** check that `total` equals the sum of `by_state`: that is the server's
  invariant (one grouped query) and checking it would be a second calculation.
- **Removed** `lib/dashboardData.ts` and the nine-request aggregation, `Promise.allSettled`, the per-figure "Unavailable" state, the
  derived Processing sum and the "counts disagree" note. The page shows the server's numbers as sent (`total`, `processing`, ... and
  `by_state` for the distribution). The only arithmetic left is display: a distribution row's share is `by_state[state] / total`.
- `DashboardPage.tsx` otherwise unchanged: KPI cards and links, Recent requests, Status distribution, Quick actions, loading skeleton,
  error state, scope sentence, themes and layout. The recent table's hidden caption now says "The N newest requests" with N the number of
  rows sent (the page no longer holds a copy of the server's limit of five). The `data-available` CSS rules in `styles/pages.css` were
  dead after this and are removed.

### 18.2 Request count

| | Before (FE-6) | After (FE-DASH-2) |
|---|---|---|
| HTTP requests per refresh | 9 (`GET /api/requests/` once unfiltered, plus once per run state) | 1 (`GET /api/dashboard/summary/`) |
| Requests per minute per open tab at 10 s | 54 | 6 |
| Database work per refresh (server) | nine requests, each with its own count query (plus a page query) | one request: one grouped aggregate and one limited list query |

The server-side figures are from `apps/intake/queries.py` (BE-DASH-1); load has still not been measured.

### 18.3 Polling (unchanged hook, one request per cycle)

`useLiveResource(getDashboardSummary, [], { interval: () => DASHBOARD_POLL_MS })` with `DASHBOARD_POLL_MS = 10000`, the FE-5 hook
(section 15.1): the next refresh is scheduled only after the previous one finishes (no overlap, no duplicate); no timer while the tab is
hidden and none starts when the page is opened in a hidden tab; one immediate refresh when the tab becomes visible; the request in flight
is aborted on a manual refresh, route change, logout and unmount, and its late answer is ignored; nothing is requested while signed out;
a failed refresh keeps the last good data and shows the quiet "Could not refresh" note with back-off (429, 5xx, no connection, garbled
answer are retried; a 403 is not). The Refresh button refreshes at once. The 10 s interval was not changed.

### 18.4 Behaviour that changed on purpose

- A failure is now all-or-nothing: there is one request, so one error state (first load) or one "Could not refresh" note with the last
  good figures kept (later). A single figure can no longer be "Unavailable" while the others show.
- The "counts disagree" note is gone for the counts: `total` is the sum of `by_state` by construction. The recent list is a second
  server statement and can be a moment newer than the counts; the next refresh corrects it.
- A 403 `permission_denied` on first load shows "Not available for your account" with no retry, as before.

### 18.5 Tests (development VM, jsdom, `fetch` stubbed, fake timers)

`pages/DashboardPage.test.tsx` was rewritten (61 tests): the exact summary request (method, path, no query, same-origin credentials, no
CSRF header, no list request), the response contract (accepted body returned unchanged; refusal of 14 malformed shapes and a non-JSON
body), the server's figures shown as sent (including numbers the browser could not derive), zeros and the empty system, distribution
shares, card links and accessible names, recent requests (order, links, Review shortcut, five rows, empty states with and without
`intake.submit_request`), loading, 500, 403, network failure, malformed answer, an ended session, one request on open, one per 10 s cycle,
refresh in place, no overlap, quiet failure with last good data for 500, network failure and 403, hidden-tab pause and visible resume,
a hidden first load, abort on logout and on leaving the page, the Refresh button, and the existing navigation and routes.
`auth/auth.test.tsx`'s shared stub gained a summary response so the landing dashboard still renders there. Result: 18 files, 389 tests
passed; `npm run typecheck`, `npm run build` and `git diff --check` clean. No backend file was touched or run in this slice.

### 18.6 Remaining limitations

- **Not seen in a real browser**, and not run against the real endpoint: the contract was checked by reading the backend serializer and
  query code, not by a live call. Owner browser review is the next step.
- The strict body check means a backend that adds a run state or a `by_state` key makes the dashboard show an error until the frontend
  (`RUN_STATES` in `api/types.ts`) is updated; `backend/tests/test_frontend_contract.py` already pins the state list.
- Polling is still per open tab (each open dashboard counts against the per-user API ceiling of 600 requests a minute, now 6 instead of
  54); the 10 s interval is provisional and unmeasured.
- Everything in 16.7 other than the endpoint use still applies (no history, no AI or worker figures, "Approved" excludes `exporting` and
  `exported`).
