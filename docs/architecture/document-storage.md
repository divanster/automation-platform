# Document Storage (DOC-1) and PDF Text Extraction (DOC-2)

> **Status. DOC-1 (sections 1 to 10): merged into `main` (PR #37). DOC-2 (section 11, PDF text extraction): implemented on `feat/document-text-extraction` (not committed, not merged).**
> Both are **DB-free validated in the development VM only**. The PostgreSQL tests (`backend/tests/test_documents_postgres.py` and `backend/tests/test_document_text_postgres.py`) are written and
> **NOT run**; PostgreSQL validation (including the migrations `documents.0001`, `audit.0004` and `document_text.0001`) is pending. No document was ever sent to any provider; there is no OCR, no AI
> use of documents and no frontend for documents, and no real deployment has used any of it. This is **development-validated, not production-ready.**
> Authoritative companions: [data-model.md](data-model.md) (the `Document` entity), [deployment.md](deployment.md) (storage location,
> backup), [ai-policy.md](ai-policy.md) (documents are untrusted data) and the [runbook](../development/runbook.md) sections 5.13 and 5.14.

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
| `processing_state` | `uploaded` \| `processing` \| `processed` \| `failed` (CHECK). **DOC-1 only ever sets `uploaded`**, and **DOC-2 does not change it either** (section 11.9): the other states are the closed vocabulary a later slice will use when it decides how several processing steps combine into one document state |

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
* **No malware scan, no content sanitization, no PDF parsing by the upload path.** See section 3. (DOC-2's text extraction parses a stored PDF, but only in an isolated, resource-limited process: section 11.)
* **No quota** per user or in total: an authorized user can fill the disk (bounded only by the size limit and the upload throttle, which is per process).
* **No re-hash** of an object on download (size only), no integrity monitor, no retention or deletion, no versioning, no duplicate policy, no case link.
* **Upload is not resumable**; a dropped connection stores nothing. **No range requests.**
* **Hard links** are refused where the storage looks; a process with write access to the directory can still do anything the directory's permissions allow.
* The `document.downloaded` event is written before the bytes are sent (see section 7).
* The ASGI refusal is a guard, not a feature: a production server decision is still open.
* Audit and record immutability are ORM-level only.
* Nothing here is evidence of real-world behaviour yet: the PostgreSQL tests (including concurrent uploads, atomic rollback and the migrations) have not been run, and no browser or real server exercised it.

## 11. PDF text extraction (DOC-2)

DOC-2 reads the **text layer** of a stored, text-based PDF, page by page, deterministically, in the existing persistent worker. **There is no OCR, no AI and no provider call**; no frontend. It is a **Core** capability
(`backend/apps/document_text`, a Django app of its own: `apps/documents` is unchanged), not a module, and it adds no permission, no `setup_roles` change and no second task system.

### 11.1 Architecture

```text
 client ──(session + CSRF)──▶ /api/documents/<id>/text-extractions/…   (apps/api/views_document_text.py: transport only)
                                      │
                         apps/document_text/services.py        authorize ▶ ONE transaction: lock the document ▶ TextExtraction row + enqueue_task + audit
                                      │                                                   (the worker cannot see the task before the commit)
              PostgreSQL: apps.tasks queue (kind documents.extract_text, payload {"extraction_id"})
                                      │
 runworker ─▶ apps/document_text/handlers.py ─▶ execution.run_text_extraction                      (config/task_registry.py + config/document_extraction.py compose it)
                  T1: lock the request, existing result? ▶ return it   │  re-check the requester may still download the document
                  no transaction:  DocumentStore.open ▶ read ≤ limit ▶ verify SHA-256 ▶ isolation.IsolatedExtractor
                                                                                   │  stdin: the PDF   stdout: ONE validated answer
                                                          child process (apps/document_text/child.py): limits on itself ▶ pypdf ▶ pages
                  T2: lock again ▶ result + pages + audit event, or nothing
```

* **One queue, one worker.** The kind `documents.extract_text` is a Core kind (`config/task_registry.CORE_UNGATED_KINDS`; not legacy, not a module's: it needs no module state), claimable by a worker that has the extraction runtime
  installed. `check_handler_composition` still stops the process at start if a handler has no owner. Request Intake's registry, handler, reconciler and behaviour are unchanged (its tests pin it). DOC-2 adds one
  maintenance hook, `documents.reconcile_text`, which closes a request whose task failed for good (11.7).
* **The web process never loads the PDF library.** Only the child imports `pypdf` (`apps/document_text/pdf_text.py`, the only importer in the backend; a test starts a fresh process and checks that neither the API, the
  composition root nor the worker command loads it). The version of the installed library is read from package metadata.
* **Dependency.** `pypdf==7.1.1` (pure Python, BSD-3-Clause, **no required dependency** on Python 3.11 and later) is pinned in `requirements/runtime.in` and, with its SHA-256, in `requirements/runtime.txt`. It was
  chosen over a hand-written reader because a PDF text reader (cross-reference repair, object streams, fonts, encodings) parsed from hostile input is exactly the code not to write twice. **None of its optional extras
  is installed** (no `cryptography`, `pillow`, `fonttools`, `brotli`): an encrypted file is refused, never decrypted, and a test checks that these packages are absent.

### 11.2 Supported PDFs, and what is not

* **Supported:** PDFs whose pages draw their text with text operators in a **text layer** (the usual output of a word processor, a reporting tool or a "print to PDF"), unencrypted, within the limits below, readable by the library
  (classic and cross-reference-stream files, compressed content streams, the common simple and composite font encodings the library decodes). Anything DOC-1 stored is acceptable input.
* **Not supported, and said so by a code, never by invented text:** a **scanned or image-only page** (it is `empty`: there is no OCR), an **encrypted** file (`encrypted_pdf`, even one that opens with an empty password: no password or
  key is supported), a file the library cannot parse (`malformed_pdf`: damaged, truncated or hostile), more pages or text than the limits allow, text drawn as vector outlines or in an image, text in annotations or form fields.
* **Reading order.** The text of a page is returned in the order the page's content stream draws it, as the library's `extract_text()` reports it in its default mode. That is reading order **only where the producer drew
  the page in reading order**: multi-column pages, tables, text boxes, headers and footers, and rotated text may come out in another order, run together or be split. Nothing is re-ordered, de-hyphenated or "repaired".
* **Text normalization (the only changes).** Line ends become `\n`; NUL and the other control characters (except `\n` and `\t`) and lone surrogates are **removed** (PostgreSQL cannot store NUL; a lone surrogate is not text).
  No Unicode normalization, no whitespace trimming. **Bidirectional control characters are kept**: a display of extracted text must neutralize them (no UI exists yet).
* **Determinism.** The same bytes, the same limits and the same library version give the same pages and the same `text_sha256` (SHA-256 of the canonical JSON of the pages); the child runs with a fixed `PYTHONHASHSEED`.
  A different library version may give different text; the result records the version.

### 11.3 The result

One `TextExtraction` (a request) has at most one `TextExtractionResult` and, for a result that read the file, one `TextExtractionPage` per page. All three tables are **append-only at the ORM level** (as `ResultRevision`):
a new extraction of the same document is a new row; earlier extractions, results and pages stay as history and are never overwritten. A page is `text`, `empty` or `failed`; the result is:

| Result status | Meaning | `error_code` |
| --- | --- | --- |
| `extracted` | every page was read and at least one has text | none |
| `partial` | some pages have text, others `failed` (`page_extraction_failed`, `page_text_too_long`, `text_limit_exceeded`) | `some_pages_failed` |
| `empty` | the file has no pages, or no page has text (a scan) | `no_pages` / `no_text` |
| `failed` | nothing usable; **no page rows** | `not_authorized`, `document_missing`, `integrity_mismatch`, `document_too_large`, `encrypted_pdf`, `malformed_pdf`, `too_many_pages`, `extraction_timeout`, `resource_limit`, `extractor_failed`, `extraction_unavailable` |

The API reports `queued` and `running` while there is no result. Per page: `page_number` (1-based), `status`, `error_code`, `text`, `char_count` and `source_reference` =
`document:<document id>:sha256:<hash of the stored bytes>:page:<n>`, which names the exact bytes the page came from. The result also records the extractor name and version, the page and character counts, the document's
SHA-256 and size and the text hash. **No text is fabricated and none is cut short**: text over a limit makes that page (and, past the total limit, every later page) `failed`; a page that raised is `failed` with no text.

### 11.4 Limits (provisional, NOT measured on real documents; all in `config/document_extraction.py`, read by the worker only)

| Limit | Default | Variable | Range |
| --- | --- | --- | --- |
| Size of the stored PDF | 20 MiB | `AP_DOCUMENT_EXTRACTION_MAX_BYTES` | 1 KiB to 100 MiB |
| Pages | 200 | `AP_DOCUMENT_EXTRACTION_MAX_PAGES` | 1 to 1000 |
| Characters per page | 100 000 | `AP_DOCUMENT_EXTRACTION_MAX_PAGE_CHARS` | 1 to 1 000 000 (not above the total) |
| Characters in all | 500 000 | `AP_DOCUMENT_EXTRACTION_MAX_TOTAL_CHARS` | 1 to 5 000 000 |
| Wall clock (and CPU seconds) of the extraction process | 20 s | `AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS` | 1 to 120 |
| Memory of the extraction process | 1024 MiB | `AP_DOCUMENT_EXTRACTION_MEMORY_MB` | 128 to 4096 |

An invalid value is a configuration error that stops the worker at start-up (naming the variable, never the value); it is never clamped. The page count is checked **before any page is read**, and against the real page tree,
not the file's own `/Count`. The library's own safety limits (decompressed stream sizes, parsing work, page-tree depth and size) stay in force and every decompression and buffer cap is tightened to at most an eighth of the memory limit.
**The worker's lease must cover the timeout plus 15 seconds** (the queue has no heartbeat): a worker with a shorter lease **still starts** and runs everything else as before but **holds the kind back** (its extraction tasks stay
`queued`, never failed) and says so in its log; the default lease (60 s) covers the default timeout.

### 11.5 Isolation (time and memory)

The extraction runs in a **separate, short-lived Python process** (`isolation.py`, `child.py`, `child_limits.py`), never in the worker. The parent writes the PDF to its standard input from a thread, reads **at most a computed cap** from
its standard output and enforces the wall-clock timeout with `kill`; stderr is discarded. The child gets only the interpreter's needs in its environment (a fixed `PYTHONHASHSEED` and, on Windows, `SYSTEMROOT`): **no database setting,
secret or `AP_*` variable**; no shell (`shell=False`), no window, a neutral working directory, the interpreter that runs the worker and a fixed command line (`-s -P -B child.py` plus six integers): never a path from a setting or a request.
Before it reads a byte the child puts limits on **itself**, and **fails closed** if it cannot:

* **Windows** (run for real in the development VM): a job object with a committed-memory limit (`ProcessMemoryLimit`), a CPU-time limit, an **active-process limit of one** (it cannot start another program) and kill-on-close. A
  memory limit ends in a `MemoryError`, reported as `resource_limit`.
* **POSIX** (**not run in the development VM**): `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_CORE=0`, `RLIMIT_FSIZE=0` and, where the platform has it, `RLIMIT_NPROC=0`. A platform that cannot set the required ones (macOS does not honor `RLIMIT_AS`)
  fails closed: `sandbox_unavailable`, retried, then `extraction_unavailable`.

**What the child's answer is trusted for: nothing.** The parent validates it strictly (`rules.parse_child_output`): the exact keys and types, closed vocabularies, page numbering, the page and total limits, sanitized text and the
consistency of every status with its pages. A timeout, a limit, a crash, garbage or an oversized answer is a stored `failed` result with a code; an inability to start the process or put its limits in force is not the document's
fault and is retried. This is **not a sandbox**: the child runs as the same operating-system account and keeps its file and network access; a vulnerability in the library could do what that account can do (the limits bound resources, the
minimal environment keeps secrets out, nothing more). **No embedded content is executed**: the library does not run JavaScript, launch actions, embedded files or form logic, and `jbig2dec` is switched off explicitly.

### 11.6 Authorization

Extracted text is the document's content, so **every operation needs what downloading that document needs** (`download_own_document` for the uploader, `download_any_document` for any document, a superuser passes both; the permission
part first, before any record is looked up; the scope part after, answered with the same 404 as an unknown record). The Auditor (view metadata only) can neither request nor read an extraction. The decision is
`apps.documents.access` (`Action.DOWNLOAD`), reused, not copied. **The worker re-checks it** on a freshly loaded user when it runs: a requester who was deactivated or lost the permission or the scope since gets a stored
`not_authorized` result and **no stored byte is read**. A missing document is a 404; a missing or altered object is a failed result (`document_missing`, `integrity_mismatch`: the stored bytes are re-hashed against the recorded SHA-256, which
DOC-1's download does not do). The original is only ever **read**: never modified, moved or copied, and no temporary copy is written.

### 11.7 Worker integration, idempotency and recovery

* **Request.** One transaction locks the document row (concurrent requests are serialized), then returns an existing extraction that makes a new one pointless or creates one: **a pending one** (queued or running) is
  returned as it is; **a finished `extracted`, `partial` or `empty` one by the installed library version** is returned as it is; otherwise (none yet, the latest `failed`, or another library version) a new `TextExtraction` is created
  and its task queued with the idempotency key `documents.extract_text:<extraction id>` and a payload that is only that id. Changing a limit does not re-extract. A refused or failed request queues nothing and records nothing.
* **Run.** `run_text_extraction` (11.1). At most one result exists per request (the database: one-to-one). T1 and T2 both look for it **under the request's row lock**, so a retry, a recovery after a crash between the result and the
  queue's bookkeeping, and two runs racing (a stale worker and its successor) never create a second result, a second page set or a second audit event; the late run discards its work. The task result is small and identical
  for a first run and every repeat (`status`, `error_code`, `page_count`; never any text).
* **Failure kinds.** A problem with the **document** or the requester's right is a stored `failed` result and a **successful** task (the work is done and its outcome kept). A problem **outside** the document (the storage cannot be used, the
  process cannot start) raises, the handler maps it to the queue's `temporary_failure` (a missing runtime or an unknown request is `rejected`), and the queue retries within its attempt limit with its backoff. When the attempts run out the
  maintenance hook records a `failed` result `extraction_unavailable` (idempotent, safe from several workers; the API reports that state meanwhile); the document can be requested again.
* **Audit.** `document.text_requested` (the user, in the request's transaction), `document.text_extracted` (the system actor, in the result's transaction; extraction id, status, code, counts, the text hash and the library version) and
  `document.text_read` (the user who was handed the text, written **before** it is returned: a failed audit write returns nothing). Subject type `document`, Core's reserved `document` namespace, no migration. **No text, file name,
  path, storage key or hash of the file is ever in an event** (a detail key that is not listed is refused). The log holds identifiers and codes only.

### 11.8 API

`POST /api/documents/<id>/text-extractions/` (request), `GET` the same path (list, no text), `GET .../<extraction id>/` (status and facts, **no text field in its serializer**) and `GET .../<extraction id>/text/` (the pages;
`409 extraction_not_ready` before the result). Contract, errors, throttles and the permission table: [rest-api-slice.md](../development/rest-api-slice.md) section 23. The request body is ignored; nothing the client sends reaches a limit,
a path or the extractor.

### 11.9 Model and migration

`document_text.0001_text_extraction` (additive, three `CreateModel` operations, one index and the constraints; depends on `documents.0001` and `tasks.0001`; **no existing table changes**, `audit` is unchanged): `TextExtraction` (document,
requester, a one-to-one task, `requested_at`), `TextExtractionResult` (one-to-one with the request; status, code, extractor name and version, page and character counts, the document's SHA-256 and size, the text hash) and
`TextExtractionPage` (page number, status, code, `text`, count; unique per result). The database checks the vocabularies, that a code is empty exactly for `extracted`, that a failed result has no text and a page count only when known,
the hash shapes, the page numbers and that a failed page has a code and no text. All foreign keys are `PROTECT`. **`Document.processing_state` is not touched**: a document may have several extractions, and how steps combine into
one document state is a later decision. The constraints were written without a PostgreSQL server and are covered by tests that have not been run.

### 11.10 Known limits and remaining risks

* **Nothing is validated against real documents.** Synthetic PDFs only; reading-order quality, font coverage, speed, memory use and the default limits are unmeasured. No evaluation set, no corpus, no accuracy figure.
* **Not a sandbox** (11.5). The POSIX limits were never run. A hostile PDF that defeats the library runs with the worker account's rights; the Windows job object is the only kernel-enforced limit that was exercised.
* **Plaintext at rest** (section 10) now also applies to the **extracted text in PostgreSQL**, which holds the document's content for as long as the row exists. There is no retention, deletion, redaction, quota or per-document cap on extractions
  (a failed extraction can be requested again, per user throttled).
* **Text is untrusted data.** Any later use (a prompt, a UI, an export) must treat it as such ([ai-policy.md](ai-policy.md) section 7); bidirectional controls are kept.
* **The lease is the only time bound on the worker side** (no heartbeat); a stale worker's late result is discarded, but its extraction ran.
* **No malware scan** and no PDF sanitization; no per-user or total quota; no manual cancel of a queued extraction.
* **Requests are not de-duplicated across limit changes** and a document re-extracted by a new library version keeps the old one's history.
* **A short worker lease silently holds the kind back** (logged once at start-up), so a request can stay `queued` while a differently configured worker would run it.
* **`/api/auth/me/` and the frontend** do not know these permissions or routes; no UI shows extracted text or its provenance.
* **Windows service identity:** the child runs as the worker's account; the DOC-1 caveats about service accounts apply.
* The PostgreSQL tests (persistence, the one-to-one result, retry and recovery, concurrent requests and runs, permissions, audit, the migration) have **not been run**.
