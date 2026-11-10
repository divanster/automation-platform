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
| PostgreSQL | **Version 18**, native Windows install (not Docker, not WSL). Selected by the owner; **not yet installed or provisioned**. | [runbook](../development/runbook.md) |
| Django user model | `accounts.User` extending `AbstractUser`, no added fields, no custom authentication backend. Implemented; its migration is not yet applied. | ADR 0007, runbook |
| PostgreSQL driver | Psycopg 3 as `psycopg[binary]` 3.3.8 (wheel for CPython 3.14 on Windows AMD64). Python 3.14 compatibility confirmed by a wheels-only install. | ADR 0007, runbook |
| Exact versions and lock | Python 3.14.4, Django 5.2.19, DRF 3.18.3, psycopg 3.3.8, hash-locked. | runbook, `requirements/` |
| Repository and module layout | `backend/` with `config/` (split settings), `apps/accounts`, `apps/health`, `tests/`. | runbook |
| Health endpoint access | Minimal public (no login) liveness and readiness endpoints are **accepted for the current local development skeleton**, returning only a fixed `ok` or `unavailable`. Authentication elsewhere is unaffected (enforced by tests). Network exposure for a future hub remains a deployment decision (item 1.4). | runbook, [deployment.md](deployment.md) |

## 1. Phase 2 follow-ups

No decision is blocking Phase 2 implementation any more. What remains is
verification that needs a database:

| # | Item | Status |
| --- | --- | --- |
| 1.1 | **Provision PostgreSQL 18 natively** and run the PostgreSQL-backed checks. | Pending; steps in the [runbook](../development/runbook.md), section 6 |
| 1.2 | **Confirm Django 5.2 works with PostgreSQL 18 in practice.** Django 5.2 documents "PostgreSQL 14 and higher" and does not name 18. | Pending the item above |
| 1.3 | **Binary versus source-built Psycopg for production.** Psycopg describes binary packages as best-effort. | Deferred to Phase 8 packaging |
| 1.4 | **Network exposure of the public health endpoints and the Django admin route** for a future company hub. Local development use is accepted (see section 0); a hub may need these restricted to local or monitoring networks, or `/admin/` disabled or moved. | Deferred to the hub deployment decision ([deployment.md](deployment.md)) |

## 2. Decisions needed before Phase 3 (data, documents, authorization)

| # | Question | Recommended direction (not confirmed) |
| --- | --- | --- |
| 2.1 | **Duplicate upload policy** within a case. | Reject or link to the existing document and report it to the user. |
| 2.2 | **Maximum upload size and storage limits.** | Choose after measuring typical documents; no value proposed here. |
| 2.3 | **Role model details**, including whether one person may edit and approve the same revision. | Separate `operator` and `approver` roles; decide whether self-approval is allowed, configurable, or forbidden. |
| 2.4 | **Password policy and account recovery** for local accounts. | Use Django's validators as a starting point; define recovery for a standalone installation without email. |
| 2.5 | **Audit tamper-evidence and retention.** | Application-level append-only plus restricted database permissions at minimum; decide on hash chaining and retention periods. |

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
