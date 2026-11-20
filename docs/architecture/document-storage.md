# Document Storage (DOC-1): Secure PDF Upload Backend

> **Status: implemented on `feat/document-upload-backend` (not committed, not merged). DB-free validated in the development VM only.**
> The PostgreSQL tests (`backend/tests/test_documents_postgres.py`) are written and **NOT run**; PostgreSQL validation (including the
> two migrations) is pending. No document was ever sent to any provider, no AI, OCR, extraction, worker task or frontend exists for
> documents, and no real deployment has used it. This is **development-validated, not production-ready.**
> Authoritative companions: [data-model.md](data-model.md) (the `Document` entity), [deployment.md](deployment.md) (storage location,
> backup), [ai-policy.md](ai-policy.md) (documents are untrusted data) and the [runbook](../development/runbook.md) section 5.13.

## 1. Decision: Core, not a module

Document storage is a **Core capability** (`backend/apps/documents`, a Django app) and **no module was created**.

* It is shared infrastructure for every future workflow (like `audit`, `cases` and `tasks`), not business logic of one workflow. The module
  contract reserves the `document` and `documents` namespaces for Core and lists `documents.read` only as a *future* capability
  ([module-contract.md](module-contract.md)); nothing here grants a module any access to a document.
* A module that wants a document later will receive a facade over this app (as `ModuleContext` fronts `cases` and `audit`); that is a
  separate slice and is not built. Request Intake is unchanged and does not use documents.
* The permissions are Core's too: a **separate matrix** in `apps/accounts/roles.py` (`DOCUMENT_PERMISSIONS`, `DOCUMENT_ROLE_PERMISSIONS`,
  `granted_permissions`), so the Request Intake matrix that the module derivation is pinned to is byte-for-byte unchanged.

## 2. Storage architecture

```text
 client ──(raw PDF body, session + CSRF)──▶ /api/documents/  (apps/api/views_documents.py: transport only)
                                                  │
                                      apps/documents/services.py     authorize ▶ validate declared facts ▶ store ▶ ONE transaction (row + audit event)
                                         │                  │
                         apps/documents/storage.py      PostgreSQL: documents_document (METADATA only) + audit_event
                         (filesystem, stdlib only)
                                         │
              <AP_DOCUMENT_STORAGE_DIRECTORY>/incoming/<random>.partial     (an upload while it is received and checked)
              <AP_DOCUMENT_STORAGE_DIRECTORY>/objects/<2 hex>/<32 hex>      (an accepted document; random name)
```

* **Bytes never go to PostgreSQL.** The table holds the id, owner, the client's file name **as a label**, content type, size, SHA-256,
  upload time, processing state and a random `storage_key`. There is no `BinaryField`, `FileField` or text column, and a test pins it.
* **The directory is explicit and has no default** (`AP_DOCUMENT_STORAGE_DIRECTORY`). Unset means *disabled*: every upload and download fails
  closed with `503 document_storage_disabled`. It must be an absolute path outside the Git working tree, outside the repository (the
  application's source tree) and outside any static or media root (`config/document_storage.forbidden_roots`); a UNC or device path is
  refused. There is **no `MEDIA_ROOT`, `MEDIA_URL` or static route** for documents: the only way to a file is the authorized download.
* **No path from any user value.** The on-disk name is a fresh random 128-bit key chosen by the storage (`rules.new_storage_key`). The
  client's file name, content type and owner never reach a path. A key read from the database is checked against the exact key shape
  (`^[0-9a-f]{2}/[0-9a-f]{32}$`, no trailing newline) before it is joined to the objects directory, and the joined path is then
  confirmed to be directly inside it, so traversal is impossible by construction and by check.
* **Restrictive permissions** come from a `DirectoryPolicy` chosen by the composition root and **re-verified on every operation**:
  * POSIX: the root is a directory owned by the process with mode `0700`; files are created `0600` and checked through the open descriptor.
  * Windows: the hardened policy the secret store already uses (`config/secret_store_dpapi.WindowsDirectoryPolicy`: a protected DACL for the
    current identity, SYSTEM and Administrators, a local fixed ACL-capable volume, no link or junction in the path, owner checks, and a check
    of every file through its open handle). It is reused, not copied. Sub-directories are created **without a mode** on Windows so they inherit
    the root's protected ACL (Python 3.13's `os.mkdir(path, 0o700)` would replace it with an ACL that includes `OWNER RIGHTS`, which the policy
    refuses; this was found by the real-policy test, which ran on the development machine).
  * Any other platform fails closed. Symbolic links, junctions and hard-linked objects are refused.
* **No plaintext temporary file outside the protected directory.** A body is streamed in 64 KiB chunks straight from the request into
  `incoming/` (never the system temporary directory, never wholly in memory), hashed and counted as it arrives. Django's default upload
  handlers stage a large *multipart* file part in the system temporary directory before any view runs (and DRF's CSRF check parses a form
  body), so `FILE_UPLOAD_HANDLERS` is set to the in-memory handler only with a 256 KiB bound: a larger multipart file part is **discarded,
  never written**. The upload endpoint therefore takes a **raw body**, and **multipart is refused**.
* **Atomic installation, no overwrite.** The checked, flushed file is given its final name with `os.link` (which refuses an existing name),
  then the `incoming` name is removed: a reader sees no object or the whole one. A name collision draws another key (4 attempts, then fails closed).
* **Atomic with the database.** The object is installed *before* the transaction under a key nothing refers to yet. If the insert or the
  audit write fails, the transaction rolls back and the object is **removed**. If the failure is the **commit itself**, its outcome is unknown
  (the server may have committed and the acknowledgement been lost), so the object is **never removed to guess it**: a committed row without
  its file would be data loss, an orphan file is only waste. One fixed ERROR line (the document id) is logged and the original error propagates.
* **Explicit limits.** `AP_DOCUMENT_MAX_BYTES` (default 20 MiB; configurable from 1 KiB to the 100 MiB hard maximum, an out-of-range value is a configuration error, not a clamp).
  The declared `Content-Length` is checked before a byte is read (`413`), and the limit is enforced again while receiving, so a body
  longer than declared or allowed stops at the limit. A per-user upload throttle (20 per minute, 200 per hour; per process) is in addition.

## 3. File-type and PDF validation

A document is accepted only if **all** hold; each is a distinct, closed rejection reason:

| Check | Reason on failure |
| --- | --- |
| `Content-Type` is `application/pdf` (parameters ignored) | `unsupported_type` (415) |
| the file name (a label, last path component only) ends in `.pdf`, has no control, format, separator, surrogate, private-use or unassigned character, at most 255 characters | `invalid_filename` / `unsupported_type` |
| `Content-Length` present and a plain number | `length_required` (411) |
| not empty; not above the limit | `empty`, `too_large` (413) |
| the body has exactly the declared length (a broken stream counts) | `length_mismatch` |
| the first 9 bytes are `%PDF-1.0`..`%PDF-1.7` or `%PDF-2.0` followed by a line end or blank, **at byte 0** (checked on the first bytes received, so garbage is refused before more is written; a polyglot with leading bytes is refused) | `not_a_pdf` |
| `%%EOF` within the last 1 KiB; a `startxref` offset before it, inside the file, pointing at an `xref` table (with a subsection header) or an `N G obj` cross-reference stream whose dictionary names `/XRef` and `/Size`; for a table, a `trailer` with `/Size` | `truncated_pdf`, `malformed_pdf` |

**What this proves and does not prove.** It is a *structural envelope* check with bounded work (it reads at most 9 bytes at the start, 64 KiB at the end and 4 KiB at the cross-reference
target; no object is parsed, no stream decompressed). A truncated upload and a file that merely starts with `%PDF-` fail it. It does
**not** detect encryption, JavaScript, embedded files, malware or a damaged page tree, and it does not guarantee that any reader opens the file.
**A file that passes may still be a hostile or damaged PDF.** Whatever reads the content later (OCR, extraction) must treat it as untrusted
data and parse it in its own isolated step ([ai-policy.md](ai-policy.md) section 7). There is no virus scan.

## 4. Model and migrations

`documents.Document` (migration `documents.0001_initial`, additive: one `CreateModel`):

| Field | Meaning |
| --- | --- |
| `id` | UUID primary key (the public identifier) |
| `owner` | FK to the user who uploaded it, `PROTECT` |
| `original_filename` | the client's name as a **label** (user-provided data); never a path |
| `content_type` | always `application/pdf` (CHECK) |
| `size_bytes` | 1 to the hard maximum (CHECK) |
| `sha256` | SHA-256 of the stored bytes, computed while they were received (CHECK: 64 lowercase hex) |
| `uploaded_at` | set by the database server clock at insert |
| `storage_key` | random key of the object; **unique**, exact shape (CHECK); **never exposed** by the API, a log or an audit event |
| `processing_state` | `uploaded` \| `processing` \| `processed` \| `failed` (CHECK). **DOC-1 only ever sets `uploaded`**: nothing processes a document yet; the other states are the closed vocabulary later slices will use |

Immutability is **at the Django ORM and service boundary only** (no trigger, no revoked privilege): `save()` on an existing row is refused
unless it names only `processing_state` in `update_fields`; `QuerySet.update()` and `bulk_update()` refuse any other column; `delete()` and
`QuerySet.delete()` are refused. Raw SQL, a database administrator and a migration are not stopped.

Not built, deliberately: a link to a `Case` and the `(case, SHA-256)` uniqueness of the data model, a duplicate-upload policy (open question
2.1), retention, deletion, versions, or a quota. **The same bytes uploaded twice are two documents.**

`audit.0004_document_subject_type` adds the `document` subject type to the CHECK (additive; its reverse is **refused** while a `document` event
exists, and never deletes or rewrites events; same pattern as `audit.0003`).

## 5. API contract

All routes are under `/api/documents/`, JSON errors in the project's contract (`{"error": {"code", "message", "details"?}}`), session
authentication and CSRF (the `X-CSRFToken` header on `POST`), `Cache-Control: no-store` on every response. A path segment that is not a UUID is a plain 404.

| Method and path | Needs | Answer |
| --- | --- | --- |
| `POST /api/documents/` | `documents.upload_document` | `201` + metadata |
| `GET /api/documents/?page=&page_size=` | `view_own_document` or `view_any_document` | `200` `{count, page, page_size, results[]}` newest first (1 to 100 per page) |
| `GET /api/documents/<id>/` | the same, **and** scope | `200` metadata |
| `GET /api/documents/<id>/content/` | `download_own_document` or `download_any_document`, **and** scope | `200` the original, streamed |

**Upload.** The request body **is** the PDF. Headers: `Content-Type: application/pdf`, `Content-Length: <bytes>`,
`X-Document-Filename: <percent-encoded UTF-8 name>` (a browser sends `encodeURIComponent(file.name)`), `X-CSRFToken`. Multipart is refused (415). Example (PowerShell, after logging in with a
session and obtaining the CSRF token; a `curl.exe` equivalent is in the runbook):

```text
POST /api/documents/   Content-Type: application/pdf   X-Document-Filename: scan%201.pdf   <the PDF bytes>
201  {"id": "...", "original_filename": "scan 1.pdf", "content_type": "application/pdf", "size_bytes": 1234, "sha256": "...",
      "uploaded_at": "...", "processing_state": "uploaded", "uploaded_by": "<user public id>", "allowed_actions": ["download"]}
```

`allowed_actions` is `["download"]` when the caller may download that document, else `[]` (a client offers only what the server allows; the server re-checks every call). There is
no storage key, path or content field in any response.

**Download.** `Content-Type: application/pdf`, `Content-Length`, `Content-Disposition: attachment; filename=...` (built by Django's `content_disposition_header`, so a name cannot
inject a header), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'`, `Accept-Ranges: none`, `no-store`. The body is **streamed** in 64 KiB chunks
from an open handle that is closed when the response closes (even if never read); the client's `Accept` header is ignored for this one route. Before streaming, the object is opened and
checked (a regular file, one name, owner and permissions per the policy, **size equal to the recorded size**); there is no re-hash on download.

**Errors.**

| Status and code | When |
| --- | --- |
| `400 invalid_document` + `details.reason` | `invalid_filename`, `empty`, `length_mismatch`, `not_a_pdf`, `truncated_pdf`, `malformed_pdf` |
| `403 not_authenticated` / `csrf_failed` / `permission_denied` | no session; no CSRF token; the caller's roles hold no such permission (decided **before** any record is looked up or any byte is read) |
| `404 not_found` | an unknown document **and** a document the caller may not view: identical, so existence is not disclosed |
| `411 length_required`, `413 document_too_large`, `415 unsupported_document_type` | see section 3 |
| `429 throttled` | the per-user upload limit (POST only; reads are not counted against it) |
| `503 document_storage_disabled` / `document_storage_unavailable` | no directory configured; the directory is missing, unsafe, loosened, or the object is missing or does not match its size; an ASGI request (below) |

**A WSGI server is required.** Django's ASGI handler spools every request body to the system temporary directory *before* any view runs, which would break the
guarantee above, so the upload view refuses an ASGI request (`503 document_storage_unavailable`). The development server and any WSGI server are fine; the production server is
still undecided ([deployment.md](deployment.md)).

## 6. Authorization

A Django permission is global, so the names carry their scope (like `intake.*`) and the service checks the record. Reading metadata never grants the content.

| Role (Django group, `setup_roles`) | Document permissions (initial development policy) |
| --- | --- |
| Operator | `upload_document`, `view_own_document`, `download_own_document` |
| Approver | `view_any_document`, `download_any_document` (to check a draft against its source) |
| Auditor | `view_any_document` only: metadata, **no** download of the content |
| superuser | passes both parts (as for requests), nothing else is bypassed |

The decision is the pure `apps/documents/access.py`, which only holds the table and delegates to Core's own / any / superuser rule (`apps/accounts/access.py`). The user is reloaded from
the database on every call (an inactive user is refused; a role removed a moment ago is not honoured). **Run `python manage.py setup_roles` again after applying the migration**, or the roles do not hold the new
permissions. `/api/auth/me/` does **not** report the document permissions yet (it reports the Request Intake matrix); the frontend slice must extend it.

## 7. Audit

Two Core actions, subject type `document`, written with `apps.audit.recorder.record_event` **inside the transaction** of the operation:

| Action | When | Details (a closed list; booleans, integers and short tokens only) |
| --- | --- | --- |
| `document.uploaded` | in the same transaction as the row (a failed audit write rolls the row back and removes the object) | `byte_length`, `content_hash` |
| `document.downloaded` | after the object is opened and checked, before streaming; **a failed audit write stops the download** | `byte_length`, `content_hash`, `by_owner` |

Each event has the actor's public id, the database timestamp and the document id as subject. **No file name, path, storage key or content** can be recorded: a detail key that is not listed is refused, and a
value with a space, a slash or a quote is refused. `document.downloaded` records that an **authorized download was started**, not that the client received every byte. Refused attempts are not audited (as for the
rest of the system); they are logged by code only (`authorization refused: action=document.download reason=out_of_scope actor=<public id>`). Audit immutability is ORM-level only, as before.

## 8. Logging

Only the services log, with fixed text and **identifiers only** (a random document UUID and the actor's public id): uploaded, download started, a commit whose outcome is unknown, a cleanup that failed.
Never a file name, hash, path or content; a test scans the source and the log output. Request bodies are never read by the framework, so no parser can echo them.

## 9. Operation

* One-time set-up, by the operator, **as the account the web process runs as** (`manage.py prepare_document_storage`; runbook section 5.13). It creates the directory (its parent must exist) with the platform policy and the
  `incoming` and `objects` folders, **never changes an existing directory** (it only verifies it) and prints no path. A running process never creates the layout; a missing one fails closed.
* Back up the directory **together with the database** (they must be restored as one consistent pair: [deployment.md](deployment.md)). Not backed up by this slice.
* Orphans: a crash can leave a `.partial` file in `incoming/` or an object no row refers to. Both stay inside the protected directory and an operator may delete them **while no upload is running**; nothing
  does it automatically yet.

## 10. Known limits and risks

* **Plaintext at rest.** The bytes are stored unencrypted; the directory's permissions are the only protection. Encryption at rest, key management and a restricted service account are not done. Code running as the same operating-system account can read the files.
* **No malware scan, no content sanitization, no PDF parsing.** See section 3.
* **No quota** per user or in total: an authorized user can fill the disk (bounded only by the size limit and the upload throttle, which is per process).
* **No re-hash** of an object on download (size only), no integrity monitor, no retention or deletion, no versioning, no duplicate policy, no case link.
* **Upload is not resumable**; a dropped connection stores nothing. **No range requests.**
* **Hard links** are refused where the storage looks; a process with write access to the directory can still do anything the directory's permissions allow.
* The `document.downloaded` event is written before the bytes are sent (see section 7).
* The ASGI refusal is a guard, not a feature: a production server decision is still open.
* Audit and record immutability are ORM-level only.
* Nothing here is evidence of real-world behaviour yet: the PostgreSQL tests (including concurrent uploads, atomic rollback and the migrations) have not been run, and no browser or real server exercised it.
