# Open Questions

> **Status: Phase 1 review and Phase 2 implementation applied (2026-11-10).**
> Items below are either
> **resolved** (with the recording location), or **unresolved**. "Recommended
> direction" entries are suggestions for discussion, not confirmed choices.
> Nothing here is implemented.

## 0. Resolved

| Topic | Resolution | Recorded in |
| --- | --- | --- |
| Python version | **3.14 series** selected; installed on the VM as 3.14.4; officially supported; fallback to 3.13 if a dependency lacks 3.14 support. Exact patch pinned in Phase 2. | [ADR 0007](../decisions/0007-technology-baseline.md) |
| Django version | **5.2 LTS line, 5.2.8 or later**. Exact patch pinned in Phase 2. | ADR 0007 |
| API framework | **Django REST Framework, 3.18 series.** Exact version pinned in Phase 2. | ADR 0007 |
| Dependency management | **Standard `venv` and `pip`**, with the reproducibility policy in ADR 0007. | ADR 0007 |
| Initial authentication | **Django built-in users and session authentication.** No JWT, no custom user fields, no external identity in the initial skeleton. | ADR 0007 |
| Application database | **PostgreSQL only.** | ADR 0007, [ADR 0002](../decisions/0002-postgresql-primary-storage.md) |
| Tenancy | One installation serves one company; no multi-tenant hosted implementation in the initial scope. | [overview.md](overview.md) |
| Public identifiers | UUIDs. | [data-model.md](data-model.md) |
| First input format | Plain text (UTF-8). PDF parsing deferred. | [first-workflow.md](first-workflow.md) |
| First export format | JSON. CSV export deferred. | first-workflow.md |
| First AI adapter | Deterministic test adapter before real providers. | [ai-policy.md](ai-policy.md), [ADR 0004](../decisions/0004-provider-neutral-ai-gateway.md) |
| External actions | No automatic email sending and no ERP writes. | overview.md, first-workflow.md |
| PostgreSQL | **Version 18**, native Windows install (not Docker, not WSL), on the owner's **local** computer, not in the development VM. Selected by the owner; installed locally (reported as 18.6) and used for the validation in the [local validation record](../development/local-validation-record.md). Its reported `listen_addresses='*'` setting is open (item 1.5). | [runbook](../development/runbook.md) |
| Django user model | `accounts.User` extending `AbstractUser`, no added fields, no custom authentication backend. Implemented; its migration is not yet applied. | ADR 0007, runbook |
| PostgreSQL driver | Psycopg 3 as `psycopg[binary]` 3.3.8 (wheel for CPython 3.14 on Windows AMD64). Python 3.14 compatibility confirmed by a wheels-only install. | ADR 0007, runbook |
| Exact versions and lock | Python 3.14.4, Django 5.2.19, DRF 3.18.3, psycopg 3.3.8, hash-locked. | runbook, `requirements/` |
| Repository and module layout | `backend/` with `config/` (split settings), `apps/accounts`, `apps/health`, `tests/`. | runbook |
| Health endpoint access | Minimal public (no login) liveness and readiness endpoints are **accepted for the current local development skeleton**, returning only a fixed `ok` or `unavailable`. Authentication elsewhere is unaffected (enforced by tests). Network exposure for a future hub remains a deployment decision (item 1.4). | runbook, [deployment.md](deployment.md) |
| Quantities | Fractions are allowed in the initial generic schema. **`Decimal` is the canonical validated type.** Booleans, NaN, infinity, zero, and negative values are rejected. Nothing is silently rounded and no precision limit is introduced. `int` and `float` inputs are converted (a `float` through its shortest round-trip digits); validation does not convert text. Unit-specific integer restrictions are deferred (item 2.6). | [request-intake-slice.md](../development/request-intake-slice.md) |
| Ambiguity | Ambiguity affecting a **required** field blocks approval (`ambiguous_required_value`, even if the field still has a value); ambiguity in an **optional** field is a warning (`ambiguous_optional_value`). One severity per code is preserved, and `missing_required` is not weakened. | request-intake-slice.md |
| Duplicate items | Warning only (`duplicate_item`), by a conservative rule (trim, collapse whitespace, casefold on the description). Never merged, deleted, or summed; semantic similarity is never proof. | request-intake-slice.md |
| Source references | Validated against the actual input when a source text is supplied (`invalid_source_ref`, blocking). Coordinate convention: 1-based `str.splitlines()` line numbers; excerpt = the stripped line shortened to 200 characters with `...`. Valid for the fixture format only (item 2.10). | request-intake-slice.md |
| Finding codes and field paths | The **initial stable contract, version 1** (`FINDINGS_CONTRACT_VERSION`), pinned by a test. An incompatible change requires a version bump (item 2.11). | request-intake-slice.md |
| Text length limits | No business limits are invented. Only an optional, explicitly configured limit exists; values stay unresolved for the later input and storage phase (item 2.8). | request-intake-slice.md |
| Canonical serialization and content hash (internal) | **Format version 1**: UTF-8 compact JSON with sorted fixed keys, text preserved exactly, `null` distinct from empty, order preserved, and quantities as canonical plain decimal strings (`1`, `1.0`, `1.00` give `1`; no rounding, no exponent, independent of the decimal context). SHA-256 over exactly those bytes. Hashed: requester, items, notes, source references. Not hashed: validation findings, ambiguities, execution metadata, timestamps, provider identity, and the source text. **Source references are confirmed part of the content, so a source text is required whenever there are any and every reference is verified against it (missing text is refused, never skipped); with none, it may be omitted.** Blocking findings prevent serialization. The hash fingerprints the included content only: it is not a signature or authorization and does not prove the identity or authenticity of the original document (that identity and hash remain future revision metadata). Version 1 fixes both the canonicalization rules and the current payload structure; an incompatible change to either needs a new version, and historical hashes are never silently recalculated. Stored revisions and approvals remain deferred. | request-intake-slice.md |
| Revision identity, approval, and export eligibility (pure policy; persistence slice added) | The case, workflow run, revision, and approver identifiers are **`uuid.UUID`**, supplied by the caller (nothing is generated; strings are rejected and only an explicit `parse_uuid` converts canonical UUID text); revision numbers are caller-supplied integers. A revision is bound to a case and a run, is built **through the existing canonicalization** and is checked for hash, byte, and format-version consistency; blocking findings prevent revisions and approvals. An approval binds to the exact case, run, revision id, **and** content hash, requires an explicit `warnings_reviewed=True` when warnings exist (a declaration, not proof of human action), and never removes warnings. Export eligibility also takes the expected run and returns stable reason codes; an approval for revision A never authorizes revision B even with an equal hash, and an approval from another run never authorizes an export even with equal case, payload, and hash; old approvals are preserved. The generic policy modules import no request-intake type; one integration module does. Enforcing UUID format at API and model boundaries is Phase 3 work. The pure check cannot prevent an edit racing an export: the future application layer must re-check atomically at commit time. No persistence, authentication, export, or state machine exists. | [revision-approval-policy.md](../development/revision-approval-policy.md) |

## 1. Phase 2 follow-ups

No decision is blocking Phase 2 implementation any more. The database verification
was run locally by the owner (see the
[local validation record](../development/local-validation-record.md)); what remains is below:

| # | Item | Status |
| --- | --- | --- |
| 1.1 | **Install PostgreSQL 18 natively on the owner's local Windows computer** and run the PostgreSQL-backed checks there. | **Done by the owner** (PostgreSQL 18.6): 35 PostgreSQL-required tests OK and both health endpoints `ok` on port 8010 (pasted output); `showmigrations`, `makemigrations --check --dry-run`, and the local DB-free suite owner-confirmed without logs. See the [local validation record](../development/local-validation-record.md) |
| 1.1a | **Local computer prerequisites** (Git access, CPython 3.14 64-bit AMD64, package index access, and administrator rights if the EDB installer is used) and the PyCharm project. | CPython 3.14.10 on Windows AMD64 reported. Not reported: the hash-locked install on the local computer, administrator rights, and the PyCharm project (open) |
| 1.2 | **Confirm Django 5.2 works with PostgreSQL 18 in practice.** Django 5.2 documents "PostgreSQL 14 and higher" and does not name 18. | **Confirmed for the tested environment** (PostgreSQL 18.6; test suite and smoke test). Other 18.x releases and configurations are not covered |
| 1.3 | **Binary versus source-built Psycopg for production.** Psycopg describes binary packages as best-effort. | Deferred to Phase 8 packaging |
| 1.4 | **Network exposure of the public health endpoints and the Django admin route** for a future company hub. Local development use is accepted (see section 0); a hub may need these restricted to local or monitoring networks, or `/admin/` disabled or moved. | Deferred to the hub deployment decision ([deployment.md](deployment.md)) |
| 1.5 | **PostgreSQL `listen_addresses='*'`.** The owner reported this setting on the local development server. It listens on every interface and conflicts with the runbook's loopback-only rule and with [deployment.md](deployment.md) (local connections only). Not known: `pg_hba.conf` address rules, the Windows firewall state, or whether the computer is reachable from other machines. The database **must not be described as loopback-only**. | **Unresolved deployment-hardening item.** Resolve (limit to loopback, or document and justify restricted exposure) before any shared, hub, or production use |

## 2. Decisions needed before Phase 3 (data, documents, authorization)

| # | Question | Recommended direction (not confirmed) |
| --- | --- | --- |
| 2.1 | **Duplicate upload policy** within a case. | Reject or link to the existing document and report it to the user. |
| 2.2 | **Maximum upload size and storage limits.** | Choose after measuring typical documents; no value proposed here. |
| 2.3 | **Role model details**, including whether one person may edit and approve the same revision. | Separate `operator` and `approver` roles; decide whether self-approval is allowed, configurable, or forbidden. |
| 2.4 | **Password policy and account recovery** for local accounts. | Use Django's validators as a starting point; define recovery for a standalone installation without email. |
| 2.5 | **Audit tamper-evidence and retention.** | Application-level append-only plus restricted database permissions at minimum; decide on hash chaining and retention periods. |
| 2.6 | **Unit-specific integer restrictions** (for example whole boxes only). The generic schema allows fractions; whether a given unit must be a whole number is deferred, and depends on how units are modelled (units are free text and not normalized in the first slice). | Deferred; decide with the unit model and company configuration. |
| 2.7 | **How a quantity is written in the JSON export.** For content hashing this is decided (see section 0): numerically equal quantities have one canonical plain-decimal string. The export artifact is a separate feature, and whether it writes a quantity as a string or a JSON number (and whether it keeps the as-given digits) is open. Also open: whether the `float` to `Decimal` conversion convention (shortest round-trip digits) is the one to keep. | Decide with the JSON export format (Phase 5). The canonical bytes are an internal component and must not be assumed to be the export format. |
| 2.8 | **Text length limits.** No business limit is invented. The slice only has an optional, explicitly configured `max_text_length` (default: none) applied to every text field. Values, per-field versus single limits, and units (characters versus bytes) are unresolved. | Leave unresolved for the later input and storage phase. |
| 2.9 | **Source-location check rules** (the `value_not_in_source` warning). Which normalization, and how numbers are matched (`10` versus `ten`, `2.5` versus `2.50`). The slice compares text after trimming, collapsing whitespace, and casefolding, and looks up quantities as standalone digits exactly as the canonical `Decimal` prints; it only ever warns. | Keep conservative; revisit when real extraction output is evaluated. |
| 2.10 | **Source-reference conventions for extractors other than the fixture parser.** Validation checks references only against the fixture format (1-based `splitlines()` lines, stripped and shortened excerpts). Quotes produced by a language model, character offsets, and fuzzy matching are not covered and nothing more general is claimed. | Decide when real adapters exist (Phase 6). |
| 2.11 | **Handling of stored findings after a contract version bump.** The finding codes and field-path grammar are version 1 (see section 0). Open: how findings stored under an older version are read, migrated, or re-validated when an incompatible change is made. | Decide when revisions are persisted (Phases 3 and 5). |
| 2.12 | **How the `RequestExtractor` contract relates to the AI gateway.** The slice defines it at workflow level so the gateway stays free of business types; real adapters would sit behind it. | Decide when the gateway is built (Phase 6). |
| 2.13 | **Whether the serialization safety refusals should also be enforced in validation.** A quantity whose plain decimal form needs more than 999,999 padding zeros (for example `1E+1000000`), and text containing an unpaired surrogate, pass business validation but cannot be serialized. They are refused with the documented `SerializationSafetyError` (reasons `decimal_expansion` and `unencodable_text`), before any large allocation, and nothing is rounded, replaced, or switched to exponent notation. The distinction is documented: a draft can pass validation and still fail serialization safety. Validation was deliberately not changed, and the 999,999 bound is a memory-safety guard, not a business limit. | Decide later whether validation should reject these earlier (for example at upload or edit time). Do not weaken either silently. |
| 2.14 | **Binding the content hash to a result-schema version.** The version-1 envelope contains the canonical format version only, because no workflow-definition or result-schema version exists yet. Open: whether, and where, the hash should be tied to a result-schema version (inside the bytes, which would be a new canonical format version, or recorded on the revision or run). | Decide with `WorkflowDefinition` versioning (Phases 3 and 5). |
| 2.15 | **Approver identity and evidence of review.** `warnings_reviewed=True` is a caller declaration, and the approval records only the acknowledged warning codes and paths. The persisted approval stores `approver_id` as an unverified domain actor identifier (a plain UUID): it is **not** a foreign key to `accounts.User` (which has an integer key and no public UUID, and is unchanged), and nothing authenticates or authorizes it. **Linking approvals to authenticated users is required before any API exposes approval operations.** Open: how a user account maps to the approver UUID, authentication, authorization (roles, four-eyes, see 2.3), and what evidence a warning review leaves (who, when, per warning). Rejection records and the `approval.invalidated` audit event are also not modelled. | Decide with authentication (Phase 3) and the audit design. |
| 2.16 | **How the atomic re-check at export commit is implemented.** The persistence slice stores revisions with a database-unique `(run, revision number)` and a current-revision pointer, and serializes revision and approval writes per run by locking the run row (written for PostgreSQL, **not yet run**). The persisted eligibility check is still a plain read without lock or snapshot. Open: the export-commit transaction and locking design (re-reading the current revision and approval atomically), and what happens to a result discarded by the re-check. | Decide in Phases 4 and 5, after the queue design is proven; verify the writer-serialization tests on PostgreSQL first. |
| 2.17 | **Storing invalid revisions.** The data model says invalid (blocking-finding) revisions are stored so people can correct them. The pure policy refuses to construct a revision with blocking findings, so the persistence slice stores **valid revisions only**. Open: how a draft with blocking findings is stored and edited, and whether it is a `ResultRevision` with a validity flag or a separate draft record. | Decide with the editing flow (Phase 5). |
| 2.18 | **Database-level immutability.** Revisions and approvals are immutable at the ORM and service boundary only; there is no trigger and no revoked UPDATE or DELETE privilege, so raw SQL or an administrator can change them (a change to the hash alone or the bytes alone is detected when the row is loaded, because the domain re-verifies it, but a change to both together stays consistent and is not detected, so the hash check is not tamper protection). Open: triggers or a restricted runtime database role, and how this fits audit tamper-evidence (2.5). | Decide with the runtime database role design (Phase 3 or 8). |
| 2.19 | **Deferred attributes of `WorkflowRun`, `ResultRevision`, and `Approval`.** The slice stores only what the policy needs. Not yet stored: run state, definition and configuration snapshot references, revision origin, parent revision, and author, the rule "at most one extraction revision per run", approval decisions other than "approved" (rejections and their reasons), and the `needs_review` guard on creating an approval (the slice does enforce that an approval is for the run's current revision). | Add with the state machine, authentication, and audit slices; each is a migration to the same tables. |

## 3. Decisions needed before a client pilot

| # | Question | Recommended direction (not confirmed) |
| --- | --- | --- |
| 3.1 | **Supported Windows environment.** Editions, client versus server, and hardware. | Determine empirically from the installation experiment; publish no hardware requirements until measured. |
| 3.2 | **AI runtime and external provider.** Which local runtime, whether a company-endpoint option is offered, and whether any external provider is approved. | Begin with a local runtime and treat any external provider as optional and explicitly approved per company. Evaluate on Windows support, license terms, and structured-output reliability. |
| 3.3 | **Credential storage on Windows** for provider credentials and database passwords. | Use an operating-system-supported protected store or equivalent; choose and review before any external adapter. |
| 3.4 | **Installer and service packaging.** | Decide from the feasibility experiment. |
| 3.5 | **Backup destination and responsibilities.** | The company chooses and owns the destination; the product provides the procedure and verification. |
| 3.6 | **Update delivery** without a mandatory control portal. | Manual or company-controlled delivery first. |
| 3.7 | **Licensing and support boundaries.** | Requires a business and legal decision. **No license is added to the repository until explicitly authorized.** |
| 3.8 | **Data handling agreement for the pilot**, including use of real documents for evaluation. | Agree in writing before any real data is used. |
| 3.9 | **Language and locale** of documents and UI. | Confirm with the pilot company; affects model choice and evaluation. |
| 3.10 | **Django 6.2 LTS transition plan.** | Plan an LTS-to-LTS move before Django 5.2 extended support ends (April 2028), contingent on DRF support. |

## 4. Deferred decisions

| # | Question | Notes |
| --- | --- | --- |
| 4.1 | Company hub details: network exposure, TLS, identity integration, maintenance process. | Design stays compatible; decisions wait for a hub requirement. |
| 4.2 | Optional control portal scope. | Not required ([ADR 0006](../decisions/0006-local-execution-independent-of-control-plane.md)). |
| 4.3 | Semantic retrieval and pgvector. | Only when a workflow needs it ([ADR 0002](../decisions/0002-postgresql-primary-storage.md)). |
| 4.4 | PDF parsing, CSV export, OCR, and additional formats. | After the first slice. |
| 4.5 | Whether exported runs can be "reopened" for correction. | The current proposal is a new run in the same case. |
| 4.6 | Consequential external actions (email, ERP, payments) and ambiguous-outcome handling. | Requires its own reviewed design ([background-execution.md](background-execution.md), section 13). |
| 4.7 | Multi-step approval chains and per-item approval. | Out of scope for the first slice. |
| 4.8 | Frontend tooling and component library. | Decide before Phase 7. |
| 4.9 | Personal-data deletion and retention rules. | Depends on the pilot company's jurisdiction and policies. |
| 4.10 | Task queue library versus custom implementation. | Decided at the start of Phase 4 ([ADR 0003](../decisions/0003-persistent-background-worker.md)); queue correctness remains unproven until its tests pass. |
| 4.11 | JWT or external identity (single sign-on). | Excluded from the initial skeleton; revisit for a hub. |

## 5. Assumptions embedded in the documents

To be corrected if wrong:

- A "company hub" means a company-operated shared installation for one company,
  not a multi-company service.
- `local_only` is the default AI data-policy mode.
- Dates and effort estimates are intentionally absent from the plan.
