# REST API slice (authentication, requests, review, audit)

> **Status: implemented; DB-free tests pass in the VM; the 98 PostgreSQL-requiring API tests are WRITTEN BUT HAVE
> NOT BEEN RUN.** The development VM has no PostgreSQL and never connects to one, and no SQLite substitute was
> used. Nothing in this document about PostgreSQL-backed behavior (password verification against stored hashes,
> the session table, row locking, concurrency, the real request flow) is proven until the project owner runs the
> local steps in section 14 and reports the output. Sections 1 to 6 are the contract the code was built to
> (it was written before the implementation); the rest describes what was built. This slice has no React UI, no
> real AI, no export, no file upload, and no production-readiness claim.

## 1. Scope and principles

An authenticated JSON API over the services that already exist (`apps.intake.submission.submit_request_as`,
`apps.intake.review`, `apps.audit`). Views are transport adapters: they authenticate the session, validate the
shape of the input with explicit DRF serializers, call ONE service function with `request.user`, and map the
service's result or exception to a response. **No business rule, permission rule, validation rule,
canonicalization or approval rule is implemented in a view.** Authorization stays in the services (they reload
the user from the database and check permission and scope on every call). Where an operation had no service
(listing, the detail read model, a paginated audit read), a small read service was added in `apps.intake`, not
in a view.

* Django session authentication, CSRF protection, no JWT, no tokens in browser storage, no CORS.
* The DRF defaults stay `IsAuthenticated` + `SessionAuthentication` + JSON. Two views are public exceptions,
  documented and pinned by tests: the CSRF bootstrap and the login. The health views were already public.
* JSON only, `application/json`; request bodies are bounded; unknown input fields are rejected.
* Responses with user data carry `Cache-Control: no-store`.

## 2. Endpoint inventory

| Method and path | Auth | Purpose | Service it calls |
| --- | --- | --- | --- |
| `GET /api/auth/csrf/` | public | Set the CSRF cookie and return the token | Django `get_token` |
| `POST /api/auth/login/` | public, CSRF required, throttled | Start a session | Django `authenticate`, `login` |
| `POST /api/auth/logout/` | session, CSRF | End the session | Django `logout` |
| `GET /api/auth/me/` | session | Current user, roles, permissions | `accounts.profile.current_user_profile` (new, read-only) |
| `POST /api/requests/` | session | Submit a request | `intake.submission.submit_request_as` |
| `GET /api/requests/` | session | Paginated list of visible requests | `intake.queries.list_requests` (new) |
| `GET /api/requests/{run_id}/` | session | Detail, processing status, allowed actions | `intake.queries.get_request_detail` (new, composes `review.get_review_summary`) |
| `POST /api/requests/{run_id}/edit/` | session, CSRF | Save an edited draft as the next revision | `intake.review.edit_request` |
| `POST /api/requests/{run_id}/approve/` | session, CSRF | Approve the current revision | `intake.review.approve_request` |
| `POST /api/requests/{run_id}/reject/` | session, CSRF | Reject the current revision | `intake.review.reject_request` |
| `GET /api/requests/{run_id}/audit/` | session | Paginated audit events | `intake.queries.list_run_audit` (new, wraps `review.list_audit_events`) |

`{run_id}` is a UUID (the workflow run id). A path that is not a UUID does not match a route (404).

## 3. Authentication and CSRF contract

1. `GET /api/auth/csrf/` sets the `csrftoken` cookie and returns `{"csrf_token": "<token>"}`.
2. Every unsafe request (POST) must send the token in the `X-CSRFToken` header and the `csrftoken` cookie.
   This includes `POST /api/auth/login/`: DRF's session authentication only checks CSRF for an already
   authenticated user, so the login view enforces it explicitly with Django's own `CsrfViewMiddleware` check.
3. `POST /api/auth/login/` with `{"username": ..., "password": ...}`: on success the session is started (the
   session key and the CSRF token are rotated) and the response is the same body as `/me/` plus a fresh
   `csrf_token`. Any failure (unknown user, wrong password, inactive user) gives the SAME response
   (`403 invalid_credentials`), with no hint which. The password is never returned or logged.
4. `POST /api/auth/logout/` flushes the session server-side; the old cookie no longer authenticates.
5. A missing or invalid session on a protected route gives `403 not_authenticated` (the session-auth contract:
   DRF answers 403 when no authentication class defines a `WWW-Authenticate` challenge). 401 is not used.

Login throttling: two limits with stable `429 throttled` and a `Retry-After` header, counted per client IP and
per (hashed) username. Details and limits in section 8.

## 4. Error contract

Every error body is `{"error": {"code": "<stable code>", "message": "<fixed text>", "details": {...}}}`.
`message` is fixed per code and never contains submitted values; `details` is present only where listed.

| HTTP | `code` | When | `details` |
| --- | --- | --- | --- |
| 400 | `malformed_json` | The body is not valid JSON, or not a JSON object | |
| 400 | `validation_error` | A field is missing, has the wrong type, is too long, is unknown | `fields`: `{path: [error code, ...]}` (codes only, never values) |
| 400 | `invalid_request_text` | The submit service refused the text (`blank`, `contains_nul`, `unencodable`, `too_large`, `not_text`) | `reason` |
| 400 | `draft_invalid` | The edited draft has blocking findings; nothing was stored | `findings`: `[{code, field_path, blocking, message}]` (no excerpts) |
| 400 | `draft_not_serializable` | A value validates but cannot be serialized safely | `path` |
| 400 | `warnings_not_acknowledged`, `unknown_warning_acknowledged` | The acknowledgement is not exactly the current warnings | `missing`, `unexpected`: `[{code, field_path}]` |
| 403 | `invalid_credentials` | Login failed (any reason) | |
| 403 | `not_authenticated` | No valid session (including an inactive user) | |
| 403 | `csrf_failed` | Missing or wrong CSRF token | |
| 403 | `permission_denied` | The user holds no permission for the action | |
| 403 | `self_approval_forbidden` | The approver submitted or authored the request | |
| 404 | `not_found` | Unknown run, OR a run the user may not see (indistinguishable by design) | |
| 405, 406, 413, 415 | `method_not_allowed`, `not_acceptable`, `payload_too_large`, `unsupported_media_type` | Transport | |
| 409 | `stale_base_revision`, `stale_revision` | The revision acted on is not the current one | |
| 409 | `content_hash_mismatch`, `run_not_in_review`, `run_state_not_editable`, `no_current_revision`, `nothing_to_edit`, `revision_rejected`, `unchanged_content` | The request conflicts with the current state | |
| 409 | `idempotency_conflict` | The client request id was used for different text | |
| 429 | `throttled` | A rate limit was exceeded | `retry_after_seconds` |
| 500 | `internal_error` | Anything unexpected; the response says nothing else, the log has the exception class only | |

A review refusal with a reason the table does not list is a generic `409 conflict`, and a framework error it does
not list keeps its HTTP status with the code `request_error`; neither is a 500.

**Hidden resources.** An existing request the user is not scoped to (an operator reading another operator's
request) returns the same `404 not_found` as an unknown id. A user with NO permission for an action gets
`403 permission_denied` before any lookup, so that answer reveals nothing about any id.

## 5. Request and response contracts

All field names are snake_case. Timestamps are ISO 8601 UTC. UUIDs are canonical lowercase text.

**`GET /api/auth/me/`, and the login response** (`200`):
`{"public_id", "username", "is_superuser", "roles": ["Operator"...], "permissions": ["intake.submit_request"...]}`
(`permissions` lists only the permissions the product defines; a superuser holds all of them).

**`POST /api/requests/`** body `{"text": str, "client_request_id": uuid | omitted}` (nothing else).
`201` `{"run_id", "case_id", "reference", "state", "created": true}`; a replay of the same client request id and
text returns `200` with `"created": false` and the original ids. The text is never logged and never returned.

**`GET /api/requests/`** query `page` (>= 1, default 1), `page_size` (1 to 100, default 20), optional `state`
(one of the run states); unknown query parameters are rejected. `200`
`{"count", "page", "page_size", "results": [{"run_id", "case_id", "reference", "state", "failed_step",
"created_at", "current_revision_number", "outcome_status", "submitted_by"}]}` ordered newest first with the run
id as the tie-break. `submitted_by` is the submitter's `public_id`. Visibility: a user holding
`intake.view_any_request` (Approver, Auditor) or a superuser sees every request; a user holding only
`intake.view_own_request` (Operator) sees only requests they submitted; no view permission gives
`403 permission_denied`.

**`GET /api/requests/{run_id}/`** `200`:
`run_id`, `case_id`, `reference`, `state`, `failed_step`, `created_at`, `submitted_by`;
`processing` = `{"step": "extract", "task_state", "attempt_count", "failure_code"}` (the failure code is the
queue's closed safe vocabulary, never a message) or `null`;
`outcome_status` (`valid`, `invalid`, or `null` before extraction);
`current_revision` = `{"id", "number", "content_hash", "created_at"}` or `null`;
`draft` = the parsed structured draft (`{"requester": {"name", "contact"}, "items": [{"description",
"quantity" (decimal text), "unit", "notes"}], "notes", "source_refs": [{"field_path", "excerpt", "line"}]}`)
of the current revision, or of the invalid extraction outcome while the run has no revision, or `null`;
`draft_source` (`revision`, `extraction_outcome`, `null`);
`findings` (`[{"code", "field_path", "blocking", "message", "excerpt"}]`) and `warnings` (`[{"code",
"field_path"}]`);
`approval_count`, `rejection_count` (for the current revision);
`allowed_actions` (a subset of `edit`, `approve`, `reject`, derived from the existing access and review rules for
THIS user and run) and `action_blockers` (`{action: reason code}` for actions the user may attempt in general but
not now, for example `approve: self_approval_forbidden`).
Never returned: canonical bytes, the source text, task payloads or results, other users' personal data.

**`POST /api/requests/{run_id}/edit/`** body `{"base_revision_id": uuid | null, "draft": {...}}`;
`base_revision_id` is required (`null` means the first edit of an invalid extraction). `draft` has the shape of
the detail `draft`, every field optional (a missing value is `null`; the domain reports missing data as
findings), `quantity` a decimal string or an integer (never a float), bounded sizes. `200`
`{"revision_id", "revision_number", "content_hash", "run_state", "warnings": [{"code", "field_path"}],
"approval_invalidated"}`.

**`POST /api/requests/{run_id}/approve/`** body `{"revision_id": uuid, "content_hash": str,
"acknowledged_warnings": [{"code", "field_path"}, ...]}` (exactly these keys; an `approver_id` or any other
key is a `400 validation_error`). The approver is `request.user`. `200` `{"approval_id", "revision_id",
"content_hash", "run_state", "acknowledged_warnings": [...]}`.

**`POST /api/requests/{run_id}/reject/`** body `{"revision_id": uuid, "content_hash": str, "reason_code": one of
the fixed reasons}` (no free text). `200` `{"rejection_id", "revision_id", "reason_code", "run_state"}`; the run
stays in `needs_review`.

**`GET /api/requests/{run_id}/audit/`** same `page`, `page_size`. Requires `audit.view_auditevent` (Auditor,
superuser). `200` `{"count", "page", "page_size", "results": [{"id", "occurred_at", "actor_kind",
"actor_public_id", "action", "subject_type", "subject_id", "details"}]}` oldest first. `details` is the audit
vocabulary's restricted object (identifiers, state names, counts, hashes, reason codes; never content).

## 6. Role-based access

| Endpoint | Operator | Approver | Auditor | Superuser |
| --- | :---: | :---: | :---: | :---: |
| `POST /api/requests/` | yes | no (403) | no (403) | yes |
| `GET /api/requests/`, `GET .../{id}/` | own only | all | all | all |
| `POST .../edit/` | own only | no (403) | no (403) | yes |
| `POST .../approve/`, `.../reject/` | no (403) | yes (not own work, for approve) | no (403) | yes (not own work, for approve) |
| `GET .../audit/` | no (403) | no (403) | yes | yes |

These are the rules of `apps.accounts.roles` and `apps.intake.access`; the API adds none. A user with no role
gets `403 permission_denied` everywhere above.

## 7. Examples (synthetic data only)

Every value below is invented. `<token>` is the `csrf_token` returned by the API; `<uuid>` stands for a UUID.

**Bootstrap, login, current user**

```http
GET /api/auth/csrf/
200  {"csrf_token": "<token>"}            (and Set-Cookie: csrftoken=...; HttpOnly; SameSite=Lax)

POST /api/auth/login/        X-CSRFToken: <token>
{"username": "olive", "password": "<password>"}
200  {"public_id": "<uuid>", "username": "olive", "is_superuser": false,
      "roles": ["Operator"], "permissions": ["intake.edit_own_request", "intake.submit_request", "intake.view_own_request"],
      "csrf_token": "<new token>"}          (and Set-Cookie: sessionid=...; HttpOnly; SameSite=Lax)

POST /api/auth/login/        X-CSRFToken: <token>      (wrong password, unknown user, or inactive user: all the same)
403  {"error": {"code": "invalid_credentials", "message": "The credentials are not valid."}}
```

**Submit, then read**

```http
POST /api/requests/          X-CSRFToken: <new token>
{"text": "Requester: Jane Doe\nItem: 10 | boxes | A4 paper\n", "client_request_id": "<uuid>"}
201  {"run_id": "<uuid>", "case_id": "<uuid>", "reference": "REQ-20260102-0A1B2C3D", "state": "queued", "created": true}

GET /api/requests/<run_id>/            (after the worker has run)
200  {"run_id": "<uuid>", "case_id": "<uuid>", "reference": "REQ-20260102-0A1B2C3D", "state": "needs_review",
      "failed_step": "", "created_at": "2026-01-02T03:04:05Z", "submitted_by": "<uuid>",
      "processing": {"step": "extract", "task_state": "succeeded", "attempt_count": 1, "failure_code": null},
      "outcome_status": "valid",
      "current_revision": {"id": "<uuid>", "number": 1, "content_hash": "<64 hex>", "created_at": "2026-01-02T03:04:06Z"},
      "draft": {"requester": {"name": "Jane Doe", "contact": null},
                "items": [{"description": "A4 paper", "quantity": "10", "unit": "boxes", "notes": null}],
                "notes": null, "source_refs": [{"field_path": "requester.name", "excerpt": "Requester: Jane Doe", "line": 1}]},
      "draft_source": "revision", "findings": [], "warnings": [], "approval_count": 0, "rejection_count": 0,
      "allowed_actions": ["edit"], "action_blockers": {}}
```

**Edit, approve, reject**

```http
POST /api/requests/<run_id>/edit/
{"base_revision_id": "<revision id the editor loaded>", "draft": {"requester": {"name": "Jane Doe"},
 "items": [{"description": "A4 paper", "quantity": "12", "unit": "boxes"}], "notes": "Needed Friday"}}
200  {"revision_id": "<uuid>", "revision_number": 2, "content_hash": "<64 hex>", "run_state": "needs_review",
      "warnings": [{"code": "value_not_in_source", "field_path": "items[0].quantity"}, {"code": "value_not_in_source", "field_path": "notes"}],
      "approval_invalidated": false}

POST /api/requests/<run_id>/approve/                 (signed in as an Approver; the approver is the session user)
{"revision_id": "<current revision id>", "content_hash": "<its hash>",
 "acknowledged_warnings": [{"code": "value_not_in_source", "field_path": "items[0].quantity"}, {"code": "value_not_in_source", "field_path": "notes"}]}
200  {"approval_id": "<uuid>", "revision_id": "<uuid>", "content_hash": "<64 hex>", "run_state": "approved", "acknowledged_warnings": [...]}

POST /api/requests/<run_id>/reject/
{"revision_id": "<id>", "content_hash": "<hash>", "reason_code": "incomplete_content"}
200  {"rejection_id": "<uuid>", "revision_id": "<uuid>", "reason_code": "incomplete_content", "run_state": "needs_review"}
```

**Refusals**

```http
POST .../approve/   {..., "approver_id": "<uuid>"}
400  {"error": {"code": "validation_error", "message": "The request is not valid.", "details": {"fields": {"approver_id": ["unknown_field"]}}}}

POST .../approve/   (as the user who submitted the request)
403  {"error": {"code": "self_approval_forbidden", "message": "You may not approve a request you submitted or edited."}}

POST .../edit/      (base revision is no longer current)
409  {"error": {"code": "stale_base_revision", "message": "The revision is not the current revision."}}

POST .../edit/      (blocking findings; nothing stored)
400  {"error": {"code": "draft_invalid", "message": "The draft has blocking findings and was not saved.",
      "details": {"findings": [{"code": "missing_required", "field_path": "requester.name", "blocking": true, "message": "Requester name is required."}]}}}

GET /api/requests/<another user's run>/     (as an Operator)
404  {"error": {"code": "not_found", "message": "The resource does not exist."}}       (identical to an unknown id)

POST /api/auth/login/  (sixth quick attempt for one account)
429  {"error": {"code": "throttled", "message": "Too many requests.", "details": {"retry_after_seconds": 41}}}    Retry-After: 41

GET /api/requests/?page=2&page_size=10
200  {"count": 23, "page": 2, "page_size": 10, "results": [ ...10 items... ]}
```

## 8. Authentication and CSRF design

| Concern | Decision |
| --- | --- |
| Mechanism | Django sessions (`SessionAuthentication`, database-backed `django_session`). No JWT, no API tokens, no OAuth; nothing authentication-related is stored in `localStorage`, `sessionStorage` or any JavaScript-readable place. |
| Passwords | Verified by Django's `authenticate` with the project's configured hashers and validators. This slice hashes and compares no password anywhere (a static test fails if the API names a password hashing or checking function). The password is read from the body, passed to `authenticate`, and never stored, logged, or returned. |
| CSRF | The CSRF middleware is on, but DRF views are exempt from it and DRF checks the token only for a request that already has a session. Every unsafe request of a signed-in user is therefore checked by `SessionAuthentication`, and the **login**, which has no session yet, is checked by `CsrfEnforcingAuthentication` (Django's own check, run before the credentials are read). A missing, wrong, or foreign-session token is `403 csrf_failed`. |
| CSRF token delivery | `GET /api/auth/csrf/` sets the `csrftoken` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in production) and returns the token in the body, so a client never needs to read the cookie. The login response carries a fresh token (Django rotates it at login). |
| Session cookie | `HttpOnly`, `SameSite=Lax`, host-only (no `Domain`), `Secure` in production, absolute lifetime `AP_SESSION_AGE_SECONDS` (default 8 hours, minimum 300). |
| Session fixation | `login` rotates the session key and the CSRF token. |
| Logout | `POST /api/auth/logout/` (CSRF required) flushes the server-side session; a replayed old cookie authenticates nobody. |
| Revocation | The session stores no permissions: every request reloads the user. Deactivating a user, deleting them, removing a role, or changing their password takes effect on the next request (Django's session auth hash covers the password), and the services reload the user again. |
| Inactive users | `authenticate` refuses them, so they get the same `403 invalid_credentials` as a wrong password. |
| Failure messages | One response for unknown user, wrong password, and inactive user. The error body never names the account. |
| CORS | None. No CORS package is installed and no header is sent; the API is same-origin. A test fails if `corsheaders` appears or an `Access-Control-*` header does. |
| Caching | Every API response carries `Cache-Control: no-store`. |
| Production | `production.py` is unchanged and keeps `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, explicit hosts, and the required secret key. `manage.py check --deploy` still reports exactly the two warnings it reported before this slice, `security.W004` (HSTS) and `security.W008` (HTTPS redirect). Those are TLS-termination decisions of the deployment slice and were **not** silenced or weakened. |
| Configuration | `AP_SESSION_AGE_SECONDS`, `AP_NUM_PROXIES` (default 0), `AP_API_MAX_BODY_BYTES` (default 262144), and the existing `AP_CSRF_TRUSTED_ORIGINS`, `AP_ALLOWED_HOSTS`, `AP_SECRET_KEY`. Invalid values stop the application at start. |

The two public exceptions are `CsrfView` and `LoginView`. A test pins the exact set of public views (the two health views, these two) and fails if any other view becomes public, if the login's authentication class stops enforcing CSRF, or if any other view overrides authentication or permissions.

## 9. Login throttling

Three limits, all of which must pass, on DRF's throttling and Django's cache (no new dependency): per client IP per minute (`10/min`), per username per minute (`5/min`), per username per hour (`20/hour`). Rates are provisional, not measured, and live in `API_THROTTLE_RATES`. A throttled attempt is a `429 throttled` with `Retry-After` and never reaches the password check. The username limit is what bounds guessing against one account from many addresses. The username is normalized (NFKC, trimmed, case-folded, so `Olive`, ` olive ` and a full-width variant share one limit) and **hashed** before it becomes a cache key, so the cache holds no username (a test inspects the keys). Signed-in API use has a generous per-user ceiling (`600/min`).

What this does and does not provide (also stated in `apps/api/throttles.py`):

* **Per process.** The default cache is Django's local-memory cache. Counters live in one process and reset when it restarts. With several server processes each keeps its own counters, so the effective limit is the configured one times the number of processes. A shared cache (for example Django's database cache) would fix that; it needs a table and an operational step, so it is an open deployment decision and **not** added here.
* **Client IP.** `REMOTE_ADDR` is used. DRF with `NUM_PROXIES` unset would trust the whole `X-Forwarded-For` header, which a client can forge to dodge the IP limit, so the default is `AP_NUM_PROXIES=0` (ignore the header; a test proves a forged header does not help). Behind a reverse proxy set it to the number of trusted proxies, or the limit sees only the proxy's address.
* **Every attempt counts**, successful or not, and there is no account lockout: a throttled attacker waits and tries again. Throttling slows guessing; it does not replace strong passwords (open question 2.4).
* A legitimate user who mistypes five times in a minute is told to wait. That is the intended trade-off.

## 10. Validation and limits

| Layer | What it checks |
| --- | --- |
| Parser | `application/json` only (`415` otherwise); body at most `API_MAX_BODY_BYTES` (`413`, refused before decoding); NaN and Infinity refused; a syntax error, a non-object body, or a deeply nested document (which would raise `RecursionError`) is `400 malformed_json`. Neither the body nor the decoder's message is returned. |
| Serializers | Strings are strings (DRF's `CharField` would turn `5` into `"5"`; the API's field refuses it), unknown fields are refused at every nesting level, UUIDs parse, lists are bounded (500 items, 500 source references, 1,000 acknowledged warnings), text fields at most 20,000 characters, submit text at most 65,536 characters. Errors carry **codes** (`required`, `invalid`, `max_length`, `unknown_field`, ...), never the submitted value. A key that is not a plain identifier is reported as `_unknown`. |
| The domain, through the services | Everything about whether the content is acceptable: blank or oversize request text (`RequestTextError`, on the 64 KiB **byte** rule, not the character guard), missing data, invalid values and units, quantities, source references against the stored source text, duplicates, the warning acknowledgement, stale revisions, hashes, states. The API adds none of these and reports the services' own stable codes. |

**Quantity safety bound.** A draft quantity is accepted as decimal text or an integer, never a float (a JSON float cannot carry a decimal exactly), and only if its plain decimal form is at most 1,000 characters. This is a transport safety guard, not a business rule, and it is an ADDITIONAL, earlier safeguard: the domain enforces its own bound (more than 999,999 padding zeros, either exponent sign) in validation and canonicalization (section 17), so a value that somehow reached the domain is still a stable `400 draft_not_serializable` with only the field path, never a hang, a 500, or an echo of the value.

## 11. Existing services reused, and the new read services

| API operation | Existing service reused | Notes |
| --- | --- | --- |
| Submit | `apps.intake.submission.submit_request_as` | Authorization, validation, idempotency (`client_request_id`), the audit event, and the queued task are the service's. |
| Edit | `apps.intake.review.edit_request` | The draft goes through `build_edit_revision` and `create_request_intake_revision` unchanged. |
| Approve | `apps.intake.review.approve_request` | The approver is `request.user`, reloaded by the service. |
| Reject | `apps.intake.review.reject_request` | Reason codes are the policy's own list (`REJECTION_REASONS`). |
| Audit | `apps.intake.review.list_audit_events` | Wrapped for paging and for a 404 on an unknown run. |
| Summary | `apps.intake.review.get_review_summary` | Composed into the detail. |
| Authentication | Django's `authenticate`, `login`, `logout` | |

Three small **read-only** services were added because the operations had no service (they live in `apps`, not in views, so the access rules stay in one place):

* `apps.intake.queries.list_requests`: the visible runs (`view_any_request` or a superuser sees all; `view_own_request` only the user's own), newest first with the run id as tie-break, paged.
* `apps.intake.queries.get_request_detail`: the detail read model above. The allowed actions and their blockers come from the same pure rules the write services apply (`access_problem`, `edit_problem`, `approval_problem`, `rejection_problem`, `is_self_approval`), evaluated with the inputs a correct client would send; an action the user has no permission or scope for is neither listed nor explained.
* `apps.intake.queries.list_run_audit`: one page of the existing audit reader's result.
* `apps.accounts.profile.current_user_profile`: identity, roles and product permissions for `/me/`.
* `apps.intake.draft_json.draft_from_canonical_bytes`: decodes a stored revision's canonical bytes for DISPLAY (it validates and hashes nothing; a test proves a decoded draft canonicalizes to the same bytes).

Static tests (`tests/test_api_boundaries.py`) fail if a view imports a model, the ORM, or `workflows`, calls more than one service, passes a keyword the service does not accept, takes the actor from anything but `request.user`, or if the API package names a canonicalization, validation, approval, persistence, or password function.

## 12. Files

New: `backend/apps/api/` (`apps.py`, `base.py`, `errors.py`, `parsers.py`, `serializers.py`, `throttles.py`, `urls.py`, `views_auth.py`, `views_requests.py`); `backend/apps/accounts/profile.py`; `backend/apps/intake/queries.py`; `backend/tests/` (`test_api_serializers.py`, `test_api_errors.py`, `test_api_views.py`, `test_api_queries.py`, `test_api_boundaries.py`, `test_api_postgres.py`); this document.

Changed: `config/settings/base.py` (`apps.api`, session and CSRF cookie attributes, `CACHES`, `API_THROTTLE_RATES`, `API_MAX_BODY_BYTES`, the DRF parser, exception handler and `NUM_PROXIES`), `config/urls.py` (`api/`), `apps/intake/draft_json.py` (`draft_from_canonical_bytes`), and, for the Decimal resource-safety fix (section 17), `workflows/request_intake/quantity.py` (the shared bound and arithmetic), `validation.py` (never formats a value beyond the bound or longer than the source text) and `canonical.py` (uses the shared rule; behavior unchanged), plus `tests/test_request_intake_decimal_safety.py` (new) and `tests/test_request_intake_boundaries.py` (registers it as a pure module); tests that pin deliberate changes: `test_api_security.py` (the public-endpoint inventory now names the CSRF bootstrap and the login explicitly, and adds three tests), `test_settings.py` and `test_request_intake_django.py` (the project-apps list), `test_intake_composition.py` (the source-file list); status text in `CLAUDE.md`, `README.md`, the architecture documents and the runbook. No model, migration, or dependency changed.

## 13. Validation

### 13.1 In the VM (no database), from `backend\` with `..\.venv\Scripts\python.exe`

| Command | Result |
| --- | --- |
| `manage.py test` | `Ran 1199 tests`, OK (991 before this slice plus 208 new, 24 of them the pure-domain Decimal resource-safety tests; the PostgreSQL tests are excluded by the project runner) |
| `manage.py test tests.test_api_serializers tests.test_api_errors tests.test_api_views tests.test_api_queries tests.test_api_boundaries tests.test_api_security` | 188 tests: serializers 39, errors 29, views (CSRF, login, throttling, delegation) 57, read services 27, static boundaries and settings 29, public-endpoint inventory 7 |
| `manage.py test tests.test_request_intake_decimal_safety` (also `python -m unittest`, no Django) | 24 tests: the shared padding rule and length arithmetic against the real formatting, huge exponents of both signs never formatted and never allocating (`tracemalloc`), the boundary at a lowered constant, context independence, ordinary values and floats unchanged, canonicalization unchanged |
| `python -m unittest` on the pure-domain modules (no Django) | `Ran 547 tests`, OK (523 plus the 24 Decimal-safety tests) |
| `manage.py check` | no issues |
| `manage.py check --deploy` (production settings, placeholder values) | only `security.W004` and `security.W008`, exactly as before this slice |
| `makemigrations --check --dry-run` (history check patched out; no connection is opened) | `No changes detected` |
| `python -m compileall -q apps config tests manage.py workflows` | exit code 0 |
| `git diff --check`; a scan of the new files for CRLF, trailing whitespace, missing final newline | clean |
| Link check over the changed documents | every relative link resolves |
| The PostgreSQL test module, collected without `AP_RUN_DB_TESTS` | 0 tests run, no connection |

The view tests drive the REAL URL, middleware, CSRF, throttling, parser and error stack with `APIClient(enforce_csrf_checks=True)`; only Django's `authenticate`, `login` and `logout` and the services are replaced by mocks, and `SimpleTestCase` forbids any database access, so they cannot touch one. The new querysets (`list_requests`, the processing link, the author lookups) are compiled to PostgreSQL SQL **without executing them** (compilation only), which catches a wrong field path; it proves nothing about results.

**Mutation check.** Switching each safeguard off in turn made the tests fail every time: the login's CSRF enforcement (10 failures), its throttles (7), the proxy-header setting (2), unknown-field strictness (31), the 404-for-hidden-requests mapping (4), and the no-echo error body (11).

**No SQLite and no PostgreSQL.** As instructed, the PostgreSQL-requiring tests were NOT run anywhere and no SQLite substitute was used. They were desk-checked line by line against the services and the extractor's real output for their synthetic texts, which found and fixed defects in the tests themselves (a size test aimed at the wrong layer, UUID types, a dictionary that collapsed repeated audit actions, an assumption about warnings, and the cost of password hashing).

### 13.2 PostgreSQL tests (written, NOT RUN): `backend/tests/test_api_postgres.py`, 98 tests, tagged `requires_postgres`

`AuthLifecycleTests` 15, `CsrfEnforcementTests` 5, `LoginThrottleDbTests` 3, `SubmissionApiTests` 9, `ListingApiTests` 11, `DetailApiTests` 12, `EditApiTests` 12, `ApprovalApiTests` 10, `RejectionApiTests` 6, `AuditApiTests` 6, `IsolationApiTests` 4, `EndToEndApiTests` 2, `ApiConcurrencyTests` 3. They cover: login, logout and the real session table (the session is deleted, a replayed cookie fails, the key and CSRF token rotate); the cookie attributes; inactive, deactivated, deleted, role-removed and password-changed users; roles and permissions in `/me/`; CSRF refusal on every unsafe route (no, wrong, empty, foreign-session token) with nothing changed; login throttling against real accounts; submission, idempotency and the service's text rules; listing with visibility per role, stable complete disjoint pagination, newest-first order and the state filter; the detail (draft, findings, warnings, processing status including a task that failed for good, allowed actions per role and state, self-approval as a blocker); edits (stale base, first edit of an invalid extraction, invalid, unchanged and hostile drafts, scope, approved-then-edited); approvals (the approver is the session user, a client-supplied approver refused, self-approval by submitter, author and superuser, exact warning acknowledgement, history preserved); rejections (reason codes only, rejected revision not approvable); the audit endpoint (order, permission, paging, no content, no trace of refused attempts); cross-user isolation; the full API, worker, review and approval path (the worker cannot run inside a transaction, so that class is a `TransactionTestCase`); and competing connections (two approvals, two edits, an edit racing an approval). The tests use a fast password hasher for speed only, and one test proves a real login with the project's own hasher. Each class refuses to run unless the database name starts with `test_`.

## 14. Local startup and validation (owner; on the local Windows computer only)

Use the runbook's setup (`$LocalProject`, `$Py`, `AP_DB_*`, a dedicated development server), no `--keepdb`, no `--noinput`.

```powershell
Set-Location "$LocalProject\backend"
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set. Remove-Item Env:AP_RUN_DB_TESTS and repeat." }

# 1. No migration, model or dependency changed in this slice. Confirm, and create the roles if not already done.
& $Py manage.py migrate
& $Py manage.py makemigrations --check --dry-run      # expect: No changes detected
& $Py manage.py setup_roles

# 2. DB-free tests.
& $Py manage.py test                                  # expect: Ran 1199 tests, OK

# 3. The new PostgreSQL tests only. Creates and drops test_<AP_DB_NAME>.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test tests.test_api_postgres --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                        # expect: Ran 98 tests, OK, none skipped, exit code 0
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }

# 4. Everything that needs PostgreSQL (356 earlier + 98 new = 454), to prove nothing regressed.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                        # expect: Ran 454 tests, OK
} finally { Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue }
```

**Starting the development server** (localhost only; plain HTTP is fine in development because the secure-cookie flags are production-only):

```powershell
& $Py manage.py runserver 127.0.0.1:8000 --noreload   # in one window; the worker in another: & $Py manage.py runworker
```

Users get a role by membership of the `Operator`, `Approver` or `Auditor` group (Django admin at `/admin/`, or `manage.py shell`). Passwords are set with `manage.py changepassword <username>`; never paste one into a command or a chat.

**Manual smoke test** (PowerShell session; the `WebSession` keeps the cookies, and the CSRF token comes from the response body). It writes one request to the development database. Type the password at the prompt.

```powershell
$base = "http://127.0.0.1:8000"
$web  = $null
$token = (Invoke-RestMethod "$base/api/auth/csrf/" -SessionVariable web).csrf_token
$cred  = Get-Credential -UserName "<an Operator>" -Message "development user"
$body  = @{ username = $cred.UserName; password = $cred.GetNetworkCredential().Password } | ConvertTo-Json
$me    = Invoke-RestMethod "$base/api/auth/login/" -Method Post -WebSession $web -ContentType "application/json" -Headers @{ "X-CSRFToken" = $token } -Body $body
$token = $me.csrf_token; $me.roles
$sub   = Invoke-RestMethod "$base/api/requests/" -Method Post -WebSession $web -ContentType "application/json" -Headers @{ "X-CSRFToken" = $token } -Body (@{ text = "Requester: Jane`nItem: 2 | boxes | A4 paper`n" } | ConvertTo-Json)
$sub                                                   # expect state "queued"
# ... let the worker run, then:
Invoke-RestMethod "$base/api/requests/$($sub.run_id)/" -WebSession $web | Select-Object state, allowed_actions, processing   # expect needs_review, edit
Invoke-RestMethod "$base/api/auth/logout/" -Method Post -WebSession $web -Headers @{ "X-CSRFToken" = $token }
```

Repeat the login as an Approver (a second session variable) to approve with the `current_revision` id and hash from the detail. Do not paste responses containing business content into a chat.

Report the exact `Ran N tests` lines and any failure. If a concurrency test fails or hangs, report it as it is; do not loosen it. **Do not treat any API behavior as proven until this run is reported.**

## 15. Deferred

React UI and any browser client; real AI; export execution; file upload and document storage; an endpoint that returns the source text (a reviewer will need it, and it carries its own privacy decision); a revision-history endpoint; revision listing and diffs; password reset, account recovery, multi-factor authentication, and account lockout (open question 2.4); a shared throttling cache; API versioning, OpenAPI schema generation, and rate-limit tuning from measurements; per-case or per-team scoping (open question 2.3); a run cancel/retry endpoint.

## 16. Security limitations

* **Nothing here was run against PostgreSQL.** Password verification against stored hashes, the session table, row locking, and every database-backed behavior of the 98 tests are unproven until the owner's local run.
* **Throttling is per process** and depends on `AP_NUM_PROXIES` being right behind a proxy (section 9). There is no lockout and no CAPTCHA.
* **HTTP only in development; TLS is not configured.** `security.W004` and `security.W008` remain, by design, until the deployment slice decides TLS termination, HSTS and the HTTPS redirect. The production cookie flags are only effective over HTTPS.
* **No password policy beyond Django's validators, no reset flow, no MFA.** Account creation and role assignment are administrative (admin site or shell).
* **Plaintext business content.** The detail returns the parsed draft and findings, which may quote the source text, to anyone who may view the request. The database stores them in plaintext.
* **Same-origin only.** A browser client on another origin is not supported and nothing here should be loosened to make one work without a design decision.
* **The CSRF cookie is `HttpOnly`**, so a client must use the token returned in the body; a client that reads the cookie will not find it.
* **Audit is ORM-level append-only**, and refused attempts are logged by code only (slice 2a limitations apply unchanged). The audit endpoint pages in memory over one run's events, which are few.
* **Installation-wide approver and auditor pools** (slice 2a, P4): any approver can read and approve any other user's request.
* The throttle rates, page-size cap, body cap, and field bounds are provisional, not measured.
* The error handler returns a bare 500 for anything unrecognized and logs only the exception class; a defect in a service is therefore visible only as a class name in the log.

## 17. Findings

* **Domain validation formatted a quantity in plain notation before its own safety guard (FIXED in this slice, at the domain boundary).** `workflows/request_intake/validation.py` (`_number_in_text`) called `format(value, "f")` on every positive quantity when a source text was supplied, and `canonical.py`'s `MAX_PADDING_ZEROS` guard ran only later. A `Decimal` such as `1e-999999999` therefore made validation build a string of about a gigabyte (the check hung until it was killed). The extractor's own text parser cannot produce such a value, so the existing submit and worker path was not affected; a draft supplied directly (the edit endpoint) could. The fix: the constant and the arithmetic moved to `workflows/request_intake/quantity.py` (`MAX_PADDING_ZEROS`, `plain_padding_zeros`, `plain_notation_length`, all reading only the Decimal's digits and exponent), canonicalization uses them, and validation now never formats a value beyond the bound or longer than the source text (it reports "not found in the source", a warning; canonicalization is the layer that refuses). The API keeps its 1,000-character plain-form bound as an additional transport safeguard, using the domain's own arithmetic. Tests: `tests/test_request_intake_decimal_safety.py` (24, pure) and the mapping tests in `tests/test_api_errors.py`.
* **DRF's `CharField` coerces numbers to strings** and its default `Throttled.wait` is already rounded up; both are handled and tested in this slice.
