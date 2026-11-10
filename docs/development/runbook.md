# Development Runbook

> **Status (Phase 2 skeleton): the backend skeleton exists; the business
> workflow, worker, real AI adapters, frontend, and installer do not.**
> PostgreSQL 18 is the selected native database, but it is **not provisioned**
> on the development VM yet, so every PostgreSQL-backed check is still pending.
> The production settings are a baseline only; the production server and service
> packaging are not built.

Commands are for **Windows PowerShell**, run from the repository root unless
stated. Nothing here requires administrator rights.

## 1. Selected versions

Recorded 2026-11-10. Rationale and sources: [ADR 0007](../decisions/0007-technology-baseline.md).

| Item | Version | Where it comes from |
| --- | --- | --- |
| Interpreter | CPython **3.14.4**, 64-bit (AMD64) | Installed on the VM; recorded in `.python-version` |
| pip (bootstrap) | 26.0.1 | Shipped with the interpreter; not pinned by the lock files |
| Django | **5.2.19** (5.2 LTS line) | `requirements/runtime.in` |
| Django REST Framework | **3.18.3** | `requirements/runtime.in` |
| Psycopg 3 | **3.3.8**, installed as `psycopg[binary]` | `requirements/runtime.in` |
| `psycopg-binary` (transitive) | 3.3.8, wheel `cp314-cp314-win_amd64`, bundled libpq reported as 18.4 | Resolved |
| asgiref, sqlparse, tzdata (transitive) | 3.13.0, 0.6.1, 2026.6 | Resolved |
| PostgreSQL server | **18**, native Windows install (not Docker, not WSL) | Owner decision; **not installed yet** |

Why `psycopg[binary]`: its wheel bundles libpq, so no local PostgreSQL client
install and no compiler is needed. Psycopg describes the binary packages as
produced on a best-effort basis; whether to switch to a source-built `psycopg[c]`
for production is a pending packaging decision.

Compatibility notes:

- Django 5.2.8 and later support Python 3.14.
- Django 5.2 documents "PostgreSQL 14 and higher" and does not name 18
  explicitly, so Django 5.2 with PostgreSQL 18 is **unverified until the
  PostgreSQL-backed tests have been run**.

## 2. Virtual environment and dependencies

The environment is `.venv/` in the repository root (git-ignored). Do not install
anything into the global Python.

```powershell
cd C:\Users\YotovIvan\Projects\newnew

# Create the environment with the selected interpreter (adjust the path if yours differs).
& "D:\Software\Python.Python.3.14\python.exe" -m venv .venv

# Activate. If script execution is blocked, skip this and call .\.venv\Scripts\python.exe directly.
.\.venv\Scripts\Activate.ps1

python --version        # expect Python 3.14.4

# Install exactly the locked set: hash-verified, binary wheels only, no source builds.
python -m pip install --require-hashes --only-binary=:all: -r requirements\development.txt

python -m pip check     # expect: No broken requirements found.
```

For a runtime-only environment (what a deployment would install), use
`requirements\runtime.txt` instead of `development.txt`.

### Dependency files

| File | Purpose |
| --- | --- |
| `requirements/runtime.in` | **Direct runtime** dependencies, each pinned to an exact version |
| `requirements/development.in` | Direct **development** dependencies (none yet; includes runtime) |
| `requirements/runtime.txt` | **Generated** full runtime set including transitive packages, exact versions, sha256 hashes |
| `requirements/development.txt` | **Generated** development set (currently identical to runtime) |

Rules (from ADR 0007): exact pins only, transitive dependencies recorded,
runtime and development kept separate, no unbounded versions. The lock files are
resolved for **CPython 3.14 on Windows AMD64** because `psycopg-binary` is
platform specific; another interpreter or platform needs a regenerated lock.

To change a dependency: edit the exact pin in the `.in` file, install it in a
**clean** `.venv` with `python -m pip install --only-binary=:all: -r
requirements\development.in`, record the resolved set with `python -m pip
freeze`, download the wheels with `python -m pip download --only-binary=:all:
--no-deps` into a temporary folder, compute hashes with `python -m pip hash
--algorithm sha256 <wheel>`, update the `.txt` files, and re-verify by
installing them into another fresh environment. A helper script for this is a
possible later improvement.

Verified on 2026-11-10: a fresh environment built only from
`requirements/development.txt` produced an installed set identical to `.venv`, a
file with a deliberately altered hash was rejected, and the DB-free tests passed
in the fresh environment.

## 3. Environment configuration

The application reads **process environment variables only**. It does not read a
`.env` file and has no dotenv dependency (a dependency would add a package for
something a few lines of PowerShell can do). Variables are documented, with
placeholders, in `.env.example`.

| Variable | Used by | Notes |
| --- | --- | --- |
| `AP_DB_NAME`, `AP_DB_USER`, `AP_DB_PASSWORD` | all | Empty by default; opening a connection without them fails (no fallback database) |
| `AP_DB_HOST`, `AP_DB_PORT` | all | Default `127.0.0.1` and `5432` |
| `AP_DB_CONNECT_TIMEOUT` | all | Seconds, default `3`, minimum `1` |
| `AP_SECRET_KEY` | development optional, production **required** | Development generates a random per-process key if unset |
| `AP_ALLOWED_HOSTS` | development optional, production **required** | Comma-separated. Development defaults to localhost only; production rejects wildcards |
| `AP_CSRF_TRUSTED_ORIGINS` | production optional | Comma-separated explicit origins |
| `AP_RUN_DB_TESTS` | tests | Only `1` enables PostgreSQL-backed tests |

To use a local, git-ignored `.env` file, copy `.env.example` to `.env`, fill in
the values **yourself** (never commit it), and load it into the current
PowerShell session:

```powershell
Copy-Item .env.example .env
# edit .env, then:
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim()
}
```

Settings modules: `manage.py` defaults to `config.settings.development`;
`wsgi.py` and `asgi.py` default to `config.settings.production`.

## 4. Starting the development server

```powershell
cd backend
python manage.py runserver        # binds to 127.0.0.1:8000 by default
```

- Both health endpoints are deliberately reachable **without a login** (owner
  decision: accepted for the local development skeleton). They return only a
  fixed `ok` or `unavailable`. Every other API view keeps the authenticated,
  session-only defaults; a test fails if any other view becomes public. Network
  exposure for a future hub is a deployment decision.
- `GET http://127.0.0.1:8000/api/health/live/` returns `{"status": "ok"}`.
- `GET http://127.0.0.1:8000/api/health/ready/` returns 200 only if PostgreSQL
  answers `SELECT 1`, otherwise 503 `{"status": "unavailable"}`. A successful
  readiness response does **not** prove migrations are applied or that anything
  other than the database connection works.
- **Django's `runserver` checks migrations at startup, so if a database is
  configured but unreachable it exits with a connection error.** With no database
  configured it starts, and readiness returns 503. This behavior was observed.
- Do not run `migrate` until the PostgreSQL provisioning in section 6 is done.
  No superuser has been created; do that yourself later with
  `python manage.py createsuperuser`.

## 5. Checks and tests

### Checks that need no database

```powershell
cd backend
python manage.py check                      # system checks (development settings)
python -m compileall -q apps config tests manage.py
python manage.py test                       # DB-free tests (see below)
```

`python manage.py test` runs **59 tests that need no PostgreSQL**. The project
test runner (`config/test_runner.py`) excludes tests tagged `requires_postgres`
unless `AP_RUN_DB_TESTS=1` is set, so a default run never asks Django to create a
test database on whatever server the environment points at. SQLite is never used.

Production-settings baseline (use fake placeholders; do not use a real secret):

```powershell
$env:DJANGO_SETTINGS_MODULE = "config.settings.production"
$env:AP_SECRET_KEY = "<any-long-placeholder-for-this-check-only>"
$env:AP_ALLOWED_HOSTS = "app.example.invalid"
python manage.py check --deploy
```

Expected at this stage: only the HSTS and SSL-redirect warnings
(`security.W004`, `security.W008`). They are pending TLS and reverse-proxy
decisions, not oversights.

### Checks that need PostgreSQL (pending: no database is provisioned)

These must only be run against a **dedicated development server** you control.
Django creates and drops a separate `test_<AP_DB_NAME>` database, so the role
needs `CREATEDB`.

```powershell
$env:AP_RUN_DB_TESTS = "1"
cd backend
python manage.py test --tag requires_postgres   # real user creation and real readiness check
python manage.py migrate                         # applies accounts 0001 to the development database
```

`python manage.py makemigrations --check --dry-run` also tries to read the
migration history from the database. Without a reachable database it prints a
`RuntimeWarning` and still reports whether model changes are missing migrations;
a DB-free equivalent is part of the test suite
(`tests/test_user_model.py`, `MigrationStateTests`).

## 6. PostgreSQL 18 (native Windows): provisioning is pending

Selected: PostgreSQL **18**, installed natively on Windows. Docker and WSL are not
used. **Nothing has been installed or configured**, and no PostgreSQL server has
been contacted by any check so far (no PostgreSQL tools are present on the VM).

Planned steps, to be performed and recorded by the owner in a later step:

1. Install PostgreSQL 18 from an official Windows distribution and record which
   one and the exact minor version.
2. Keep the server listening on localhost only (`listen_addresses` limited to
   loopback), matching the standalone design in
   [deployment.md](../architecture/deployment.md).
3. Create a dedicated development role and database, using a password you choose
   and keep **outside** the repository (shown with a placeholder):

   ```sql
   CREATE ROLE ap_dev LOGIN PASSWORD '<choose-a-password-and-keep-it-outside-the-repo>' CREATEDB;
   CREATE DATABASE ap_dev OWNER ap_dev;
   ```

4. Set the `AP_DB_*` variables (section 3), then run the PostgreSQL-backed checks
   in section 5.

Installer and service-registration choices for client machines are a separate,
later decision (Phase 8).

## 7. What is not implemented

| Area | Status |
| --- | --- |
| Backend skeleton (`config`, `accounts`, `health`, tests) | **Implemented** (Phase 2) |
| `accounts.User` (extends `AbstractUser`, no added fields) and its initial migration | **Implemented**; migration **not applied** anywhere |
| Business workflow: cases, documents, revisions, approvals, exports | Not implemented |
| Task queue and background worker | Not implemented |
| AI gateway and any AI adapter | Not implemented |
| Frontend | Not implemented |
| Production application server, Windows service packaging, installer | Not implemented |
| TLS, HSTS, static file serving, log configuration, backups | Not decided or implemented |
| Exposure of the Django admin route (`/admin/`) in production or a hub | Route exists in the skeleton; its production exposure is undecided |
| Production never reads development defaults | Implemented and tested: production requires its own `AP_SECRET_KEY` and explicit hosts, and shared settings hold no key |
| JWT, registration, password reset, custom login endpoints, external identity | Excluded by decision |
