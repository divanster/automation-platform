# Deployment

> **Status: Proposed. No deployment artifact is built or installed** (the
> Phase 2 skeleton runs only as a development server). Packaging tools and
> exact directory paths are not selected. This document
> describes intended deployment structure and operational responsibilities, and
> it names the questions that must be answered by experiment.

## 1. Environments

### Development environment

- Windows, per the project's first target.
- Backend and database run locally on the developer machine; the frontend is
  developed later.
- Development conveniences (debug mode, test adapter, sample data) must not be
  present in a deployed installation by default.
- The technology baseline (Python 3.14, Django 5.2 LTS, DRF, `venv` and `pip`)
  is recorded in [ADR 0007](../decisions/0007-technology-baseline.md), and the
  exact versions in use in the
  [development runbook](../development/runbook.md). PostgreSQL 18, installed
  natively on Windows (not Docker or WSL), is selected but **not yet
  provisioned** on the development VM.
- Development data must not contain real customer documents or credentials.

### Standalone deployment (first target)

One Windows machine runs everything for one company:

```text
+------------------------- one Windows machine --------------------------+
|                                                                        |
|  Browser or desktop window  --->  Backend API service                  |
|   (UI lifetime)                    (service lifetime)                  |
|                                        |            \                  |
|                                        v             v                 |
|                                   PostgreSQL     Document storage      |
|                                   (local only)   (service account)     |
|                                        ^                               |
|                                        |                               |
|                                   Background worker service            |
|                                                                        |
|  Optional: local AI runtime (same machine)                             |
+------------------------------------------------------------------------+
```

- The API and worker are separate processes of the same codebase and are
  intended to run as managed services (mechanism not chosen).
- PostgreSQL accepts **local connections only** in standalone mode (loopback or
  local socket equivalent). It is not reachable from the network.
- The user interface reaches the API over the local machine.
- No central control portal is needed to run
  ([ADR 0006](../decisions/0006-local-execution-independent-of-control-plane.md)).

### Future company-hub deployment

A **company hub** is a company-operated shared installation (for example on a
company server) used by several of the same company's users from their own
machines. It is still one installation serving one company.

- Users connect to the **application** (API over the network, with transport
  encryption and authentication). **They never connect directly to the
  database.** The database accepts connections only from the application
  services.
- Network exposure, TLS certificates, identity integration (for example
  company single sign-on), and firewall rules are decided with the client's IT
  department before a hub pilot.
- The standalone design must not preclude this: no assumption that the browser
  and server share a machine, no database access from the UI, and authorization
  enforced in the API.
- The Phase 2 skeleton exposes minimal public (no login) liveness and readiness
  endpoints and a Django admin route. These are accepted for local development
  only. Whether they stay reachable, are restricted to local or monitoring
  networks, or are disabled or moved is a decision for hub network exposure.
- Out of scope, intentionally: **synchronizing independent desktop databases.**
  A company that wants shared access uses a hub, not many synced copies.

## 2. UI lifetime versus service lifetime

- Backend services (API, worker, database) run independently of any user
  interface window or browser tab.
- Closing the UI must not stop the worker or interrupt running tasks.
- Starting the UI must not be required for queued work to progress.
- The UI reflects state from the database through the API; it does not own state.

## 3. Directories

Locations are **proposed categories**, kept separate and outside the source tree.
Exact paths are an installer decision.

| Directory | Contents | Notes |
| --- | --- | --- |
| Application | Program code of the installed release | Read-only to the running services where possible |
| Data | PostgreSQL data, managed document storage, export artifacts | Access limited to the service account; sensitive |
| Logs | Application and service logs | Rotation and size limits needed; must not contain document content or secrets |
| Backup | Backup output | Should be placed on separate storage from the data directory; destination is a decision for the company |
| Models | Local model files, if a local runtime is used | Large; may be shared with the runtime's own location; not in source control |

The repository's `.gitignore` already excludes `runtime/`, `backups/`, `logs/`,
and `models/` so such directories cannot be committed by accident during
development.

## 4. Database lifecycle

The database lifecycle is owned by **installation tooling**, not by Django
automatically:

- Creating the database cluster, roles, and database, and configuring local-only
  access, are installer responsibilities.
- Two database identities are proposed: a **migration/owner** role used only
  during install and update, and a **runtime** role with the minimum rights the
  services need (for example insert and select only on audit events).
- The application does **not** create the database or apply schema migrations
  implicitly on start. Migrations run as an explicit, logged update step, after a
  backup (section 9).
- Django's migration tooling is used for schema changes, but its invocation is
  controlled by the update procedure.

## 5. Administrative permissions and client IT policies

Installing services, creating a database, and writing to protected directories
usually require administrator rights. Client environments may restrict this.

Assumptions to verify with an early experiment and with each client:

- whether administrator rights are available for installation,
- whether application control or antivirus software blocks the installer,
  services, or model runtimes,
- whether services may run under a dedicated low-privilege account,
- whether local ports may be opened and local firewall rules adjusted,
- whether external network access is allowed at all (affects external AI and
  update checks).

A deployment that cannot satisfy these may not be supportable in standalone
form. This is a risk to surface early, not to hide.

## 6. Sleep, shutdown, restart, and offline behavior

- **Shutdown and restart:** services stop cleanly where possible. Tasks in
  progress are recovered through lease expiry
  ([background-execution.md](background-execution.md)). After restart, services
  start without user action (mechanism to be chosen and verified).
- **Sleep and resume:** a lease may expire during sleep. On resume, fencing
  prevents a stale worker from committing results. Elapsed-time assumptions in
  timeouts must be tested around sleep.
- **Machine off:** no processing happens; work resumes when services start. The
  UI must not suggest that work proceeds while the machine is off.
- **Offline:** standalone operation with a `local_only` policy requires no
  internet. External or update features degrade explicitly; they never silently
  change the data policy.
- **Unclean power loss:** PostgreSQL recovers its own state; the file/database
  consistency check in [data-model.md](data-model.md) section 7 applies.

No timing or availability guarantees are made in this document.

## 7. Early installation experiment

Because installation on a client Windows machine is the highest-uncertainty
area, an **installation feasibility experiment** is planned after Phase 4 (the
persistent worker) and **before extensive UI work**
([implementation-plan.md](implementation-plan.md)). It should answer:

- Can the database, API, and worker be installed and started as services on a
  clean, standard Windows machine?
- Do they survive logoff, restart, and sleep?
- What administrator rights and policies are required?
- Can a user install without developer tools on the machine?

It is an experiment with recorded findings, not a production installer.

## 8. Backup and restore

Responsibilities (proposed):

- **Installation tooling** provides a documented backup procedure and a restore
  procedure that capture the **database and the document storage as a consistent
  pair**, and verify file hashes on restore.
- **The company (or its IT provider)** owns the backup **destination**,
  retention, protection of backup media (backups contain sensitive data), and
  scheduling, unless a later support agreement states otherwise.
- **Restore is tested**, not assumed. A backup that has never been restored is
  not considered verified.
- Backup of configuration and model-profile definitions is included; secrets are
  handled according to the credential-storage decision (open question).

## 9. Updates and database migrations

Proposed update sequence:

1. Verify compatibility and confirm a recent, restorable backup exists (or take
   one).
2. Stop the worker and the API so old code never runs against a changed schema.
3. Apply database migrations as an explicit step with logging.
4. Replace application files.
5. Start services and run a health check.
6. On failure, restore from the backup taken in step 1 and the previous
   application version.

Considerations:

- Rolling back a schema change is generally done by **restoring a backup**, not by
  reversing migrations. Migrations should therefore be tested forward on realistic
  data.
- In-progress runs reference their workflow and configuration versions, so
  updates must keep older definition versions available
  ([data-model.md](data-model.md)).
- How updates are delivered, signed, and verified is undecided.
- A hub deployment needs a maintenance window procedure because several users are
  affected.

## 10. What is not decided

Runtime versions, service wrapper or installer technology, update delivery,
backup destination, supported Windows editions, TLS and identity for hub mode,
and whether a local AI runtime ships with the installer.
