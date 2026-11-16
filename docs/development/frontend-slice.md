# Frontend slices UI-1 and UI-2: the web interface

> **Status.** **UI-1** (sections 1 to 12: sign-in, requests, request detail) was written in the development VM; the owner
> reports that it was then run in a real browser against the local Django, PostgreSQL and worker stack (that run's output is
> not recorded in this repository). **UI-2** (section 13: draft editing, approval, rejection, warning acknowledgement,
> audit history, stale-revision handling) was written and tested in the development VM only, against a stubbed `fetch`.
> **UI-2 has not yet been run against the real API, PostgreSQL or the worker, and its layout has not been looked at in a
> real browser.** Section 13.7 is the checklist the owner runs on the local Windows computer. Nothing is claimed as
> validated beyond what sections 9 and 13.8 list.

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
7. **Self-approval.** As the Operator, open your own request in review: no Approve section, and the text says approval is not
   possible because you submitted or edited it. (A superuser is bound by the same rule.)
8. **Rejection (Approver).** On another request choose a reason, confirm. Expect "rejected" and Approve now not possible
   ("the current revision was rejected"). Edit as the Operator: the new revision can be approved again.
9. **Stale approval.** The Approver has the request open; the Operator saves a new revision; the Approver clicks Approve and
   confirms: expect "changed after you opened it, so nothing was recorded", the new revision shown, warnings unticked.
10. **Audit (Auditor).** Open a request: events oldest first with System, You or short ids, revision numbers and reason codes;
    Operator and Approver see no audit section.
11. **Console and storage.** Developer tools: no `console` output from the app, and Local Storage, Session Storage and IndexedDB
    stay empty.

Report anything that differs with the exact message. Failures here are integration findings.

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
timing of concurrent edits, and anything visual (layout, focus rings, small screens) are unverified until 13.7 is run.

### 13.9 Remaining limitations

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

New: `frontend/src/api/review.ts`, `lib/draftForm.ts`, `components/DraftEditor.tsx`, `ApprovePanel.tsx`, `RejectPanel.tsx`,
`ConfirmStep.tsx`, `AuditHistory.tsx`, the test files above, `src/test/review.ts`, and `backend/tests/test_frontend_contract.py`.
Changed: `frontend/src/api/types.ts`, `lib/errors.ts`, `components/Pagination.tsx`, `pages/RequestDetailPage.tsx`, `styles.css`, one
UI-1 test, `README.md`, `CLAUDE.md`, `docs/development/runbook.md`, and this document. No backend behavior, model, migration or
dependency changed.
