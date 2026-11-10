# Development Runbook

> **Status (Phase 2 skeleton): the backend skeleton exists; the business
> workflow, worker, real AI adapters, frontend, and installer do not.**
> PostgreSQL 18 is the selected native database, but it is **not provisioned**
> on the development VM yet, so every PostgreSQL-backed check is still pending.
> The exact setup and validation procedure is prepared in section 6; it has not
> been run.
> The production settings are a baseline only; the production server and service
> packaging are not built.

Commands are for **Windows PowerShell**, run from the repository root unless
stated. Everything here runs without administrator rights, except installing
PostgreSQL with the EDB installer ("Route I" in section 6); the binaries-archive
route ("Route Z") needs none.

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

The exact, ordered commands are in **section 6, part D**. They must only be run
against the **dedicated local development server** created in section 6. With
`AP_DB_NAME=automation_dev`, Django creates and drops a separate
`test_automation_dev` database (Django prefixes `test_` unless a `TEST` name is
configured, and this project configures none), so the role needs `CREATEDB`.
`--keepdb` and `--noinput` are deliberately not used.

`python manage.py makemigrations --check --dry-run` also tries to read the
migration history from the database. Without a reachable database it prints a
`RuntimeWarning` and still reports whether model changes are missing migrations;
a DB-free equivalent is part of the test suite
(`tests/test_user_model.py`, `MigrationStateTests`).

## 6. PostgreSQL 18 (native Windows): setup procedure and database validation

> **Status: prepared, NOT performed.** As inspected on 2026-11-10 the VM has no
> PostgreSQL files, registry entries, services, listeners (ports 5432 to 5435
> were free), `PG*` environment variables, or processes. No PostgreSQL server
> has been contacted by any check so far. Nothing below has been run, so no
> PostgreSQL-backed check has passed.

Selected: PostgreSQL **18**, native Windows (not Docker, not WSL), local-only,
password authentication only. Docker and WSL are not used.

**Passwords are typed by you, locally, and never appear in this repository, in
commands, or in the conversation.** Do not use `PGPASSWORD`, put a password in a
SQL file, or paste one anywhere.

### What was inspected, and what it means

| Finding | Consequence |
| --- | --- |
| The Windows account is **not** a local Administrator and the shell is not elevated | The standard EDB installer (which writes to `Program Files` and registers a Windows service) cannot run without an elevated approval. **Route Z** avoids this. |
| EDB documents a **binaries zip archive** "without the installer" for expert users or for bundling Postgres into another application installer, and states that unless you specifically need the files you should download the installer instead | Route Z is supported but is the less-guided path; see "Route Z caveats". |
| System-wide VC++ runtime DLLs (`vcruntime140*.dll`, `msvcp140*.dll`) are present | Probably sufficient for the zip binaries, but **unverified** until `postgres.exe --version` runs. |
| `www.postgresql.org` and `www.enterprisedb.com` are reachable from the VM (HEAD requests only; nothing downloaded) | No web-filter blocking was seen on these hosts. |
| Django creates the test database from the server's default template and passes no encoding unless a test `CHARSET` is set (none is) | The **cluster must be UTF-8**. Both routes verify this with `SHOW server_encoding`. |

### Official sources

- Windows downloads page (links to the EDB installer and the binaries archive):
  https://www.postgresql.org/download/windows/
- EDB installer downloads (certified installer, PostgreSQL 18 for 64-bit Windows):
  https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
- EDB binaries zip archive (Windows x86-64; 18.6 was listed on 2026-11-10):
  https://www.enterprisedb.com/download-postgresql-binaries
- `initdb` reference (options used below):
  https://www.postgresql.org/docs/18/app-initdb.html

Download only from these pages. Record the exact version you install (for example
18.6) and the SHA-256 of the file you downloaded
(`Get-FileHash <file> -Algorithm SHA256`) in your own notes. No checksum or
signature published by EDB was confirmed during this review.

### Components needed

Only the PostgreSQL **server** and the **command-line tools** (`psql`, `pg_ctl`,
`initdb`). **pgAdmin 4** and **Stack Builder** (both bundled with the installer)
are not needed and can be skipped.

### Development names and privileges

| Item | Value |
| --- | --- |
| Role | `automation_dev` |
| Database | `automation_dev` (owned by the role) |
| Django test database | `test_automation_dev` (created and dropped by Django during DB-required tests) |
| Role attributes | `LOGIN`, `NOSUPERUSER`, `NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS`, and `CREATEDB` **only** so Django can create the test database |
| Network | Listens on `127.0.0.1` only, port `5432` |
| Authentication | `scram-sha-256` for every `pg_hba.conf` entry; **no `trust`** |

The role gets no rights on other databases beyond the PostgreSQL defaults.
Production role and privilege design is a separate, later decision.

### A. User-operated installation (choose ONE route)

The following placeholders are proposals; change them if you like. Keep binaries
and data **outside the repository**.

#### Route Z: binaries archive (recommended on this VM; no administrator rights, no service, no firewall change)

```powershell
# Placeholders (proposed locations on D:)
$PgRoot = "D:\Dev\postgresql-18"
$PgData = "D:\Dev\postgresql-18-data"
$PgLogs = "D:\Dev\postgresql-18-logs"

# 1. Download the Windows x86-64 PostgreSQL 18 zip from the EDB binaries page above
#    in your browser. Then (replace the placeholder path):
Get-FileHash "<path-to-downloaded-zip>" -Algorithm SHA256      # note the hash

# 2. Extract. The archive contains a top-level "pgsql" folder.
Expand-Archive -Path "<path-to-downloaded-zip>" -DestinationPath $PgRoot
$PgBin = "$PgRoot\pgsql\bin"
& "$PgBin\postgres.exe" --version        # expect: postgres (PostgreSQL) 18.x

# 3. Create the cluster: UTF-8, no locale, scram-sha-256 everywhere.
#    initdb prompts for the superuser (postgres) password. Type it yourself.
& "$PgBin\initdb.exe" -D $PgData -U postgres -E UTF8 --no-locale -A scram-sha-256 --pwprompt

# 4. Check that no 'trust' entry exists (this prints non-comment lines; there are no secrets in it).
Get-Content "$PgData\pg_hba.conf" | Where-Object { $_ -notmatch '^\s*(#|$)' }

# 5. Start the server on loopback only.
New-Item -ItemType Directory -Force $PgLogs | Out-Null
& "$PgBin\pg_ctl.exe" -D $PgData -l "$PgLogs\server.log" -o "-c listen_addresses=127.0.0.1 -c port=5432" -w start
& "$PgBin\pg_ctl.exe" -D $PgData status
netstat -ano -p TCP | Select-String ':5432\s'      # expect only 127.0.0.1:5432 LISTENING
```

Route Z caveats:

- Choosing `--no-locale` gives the C collation. That is deterministic and fine for
  a development database; locale and collation for real installations are an open
  decision.
- If `initdb`/`postgres` fails with a missing DLL, the VC++ redistributable is
  the likely cause; report the exact error rather than working around it.
- If Windows Firewall shows an "allow access" prompt for `postgres.exe`, choose
  **Cancel/deny**. Loopback needs no rule, and no firewall change is part of this setup.
- The server is an ordinary process, not a service. It stops when your session
  ends or when you run `pg_ctl ... stop`, and you start it again with step 5.
  Registering a service is a separate decision (Phase 8).
- The binaries archive is described by EDB as a convenience for expert users.

#### Route I: EDB installer (only if you can obtain administrator approval)

The installer registers a Windows service (normally named like
`postgresql-x64-18`) and installs under `Program Files`. In the wizard:

1. Components: select **PostgreSQL Server** and **Command Line Tools**; clear
   **pgAdmin 4** and **Stack Builder**.
2. Installation and data directories: accept the defaults, or choose paths
   outside the repository.
3. **Superuser (`postgres`) password:** type it yourself in the wizard.
4. **Port:** `5432`.
5. **Locale:** accept the default, then verify the encoding in part B.

Afterwards (no secrets are shown by these commands; adjust the version folder
if yours differs):

```powershell
$PgBin = "C:\Program Files\PostgreSQL\18\bin"
$PgData = "C:\Program Files\PostgreSQL\18\data"

Get-Service | Where-Object { $_.Name -match 'postgres' }          # note the service name and status
netstat -ano -p TCP | Select-String ':5432\s'                      # expect only loopback addresses
Get-Content "$PgData\pg_hba.conf" | Where-Object { $_ -notmatch '^\s*(#|$)' }   # expect no 'trust'
```

If the server listens on anything other than loopback, or `pg_hba.conf`
contains `trust`, stop and report it; do not continue.

### B. Role and database provisioning

Run in **your own** PowerShell window. Each command that connects as `postgres`
prompts for the superuser password; type it locally. Set `$PgBin` first (from
route Z or I).

```powershell
$Psql = "$PgBin\psql.exe"
$Conn = @("-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres")

# 1. Confirm the real server version, encoding and listen address.
& $Psql @Conn -c "SHOW server_version;" -c "SHOW server_encoding;" -c "SHOW listen_addresses;"
#    expect: 18.x, UTF8, and 127.0.0.1 (route Z) or localhost (route I)

# 2. Create the development role. No password in this command.
& $Psql @Conn -c "CREATE ROLE automation_dev LOGIN NOSUPERUSER NOCREATEROLE CREATEDB NOREPLICATION NOBYPASSRLS;"

# 3. Set the role's password interactively (psql prompts twice and sends it hashed).
& $Psql @Conn -c "\password automation_dev"

# 4. Create the development database owned by that role.
& $Psql @Conn -c "CREATE DATABASE automation_dev OWNER automation_dev ENCODING 'UTF8' TEMPLATE template0;"

# 5. Verify attributes and databases (a test_automation_dev database must NOT exist yet).
& $Psql @Conn -c "\du automation_dev" -c "SELECT datname, pg_catalog.pg_get_userbyid(datdba) AS owner, pg_encoding_to_char(encoding) AS encoding FROM pg_database ORDER BY datname;"

# 6. Connect as the application role (prompts for its password).
& $Psql -h 127.0.0.1 -p 5432 -U automation_dev -d automation_dev -c "SELECT current_user, current_database(), version();"
```

Expected in step 5: `automation_dev` shows `Create DB` and no `Superuser`,
`Create role`, `Replication`, or `Bypass RLS`. If `test_automation_dev` already
exists, stop and report it; do not delete it unseen.

### C. Environment configuration

Set these in the **same** PowerShell window you will run the validation in. They
exist only for that window's lifetime. The password is read without echo and is
not written to any file:

```powershell
$env:AP_DB_NAME = "automation_dev"
$env:AP_DB_USER = "automation_dev"
$env:AP_DB_HOST = "127.0.0.1"
$env:AP_DB_PORT = "5432"
$secure = Read-Host "automation_dev password (not echoed)" -AsSecureString
$env:AP_DB_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
Remove-Variable secure
```

Variable names are exactly those read by `backend/config/settings/base.py`:
`AP_DB_NAME`, `AP_DB_USER`, `AP_DB_PASSWORD`, `AP_DB_HOST`, `AP_DB_PORT`, plus the
optional `AP_DB_CONNECT_TIMEOUT` (default 3 seconds). Do not echo
`$env:AP_DB_PASSWORD`. Leave `AP_RUN_DB_TESTS` unset except where a command below
sets it.

### D. Database validation (run in order; stop at the first unexpected result)

```powershell
cd C:\Users\YotovIvan\Projects\newnew
.\.venv\Scripts\Activate.ps1        # or prefix commands with .\.venv\Scripts\ and call python.exe
cd backend

# D1. The application connects with the application role (real connection).
python manage.py check --database default
python manage.py shell -c "from django.db import connection; cur = connection.cursor(); [print(q, '->', (cur.execute(q), cur.fetchone())[1]) for q in ('SELECT current_user, current_database()', 'SHOW server_version', 'SHOW server_encoding')]"
#     expect: automation_dev / automation_dev, 18.x, UTF8

# D2. Migrations: inspect, apply to the development database, re-inspect.
python manage.py showmigrations
python manage.py migrate
python manage.py showmigrations                     # expect every line marked [X], including accounts 0001_initial

# D3. Migration consistency with a reachable database (no RuntimeWarning expected now).
python manage.py makemigrations --check --dry-run   # expect: No changes detected

# D4. DB-required tests, on the dedicated test database. No --keepdb, no --noinput.
$env:AP_RUN_DB_TESTS = "1"
python manage.py test --tag requires_postgres -v 2
Remove-Item Env:AP_RUN_DB_TESTS
#     expect: creates 'test_automation_dev', Ran 3 tests ... OK, then destroys it.
#     If asked whether to delete an existing test database, answer 'no' and report it.

# D5. DB-free regression suite (excludes the PostgreSQL tests by default).
python manage.py test                               # expect: Ran 59 tests ... OK

# D6. Optional: everything together (59 + 3).
$env:AP_RUN_DB_TESTS = "1"; python manage.py test; Remove-Item Env:AP_RUN_DB_TESTS   # expect: Ran 62 tests ... OK
```

Real readiness and localhost startup (the server needs the same `AP_DB_*`
variables, so start it in the window from part C, then probe from a second
window; the probe needs no variables):

```powershell
# Window 1 (from part C, inside backend\):
python manage.py runserver 127.0.0.1:8000 --noreload

# Window 2:
foreach ($p in "live", "ready") {
    $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health/$p/"
    "$p -> $($r.StatusCode) $($r.Content)"
}
#     expect: live -> 200 {"status":"ok"}   and   ready -> 200 {"status":"ok"}
```

Optional real failure check (route Z; with `runserver` still running, stop the
database, probe, then start it again):

```powershell
& "$PgBin\pg_ctl.exe" -D $PgData -m fast stop
try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health/ready/" } catch { $_.Exception.Response.StatusCode.value__ }   # expect 503
& "$PgBin\pg_ctl.exe" -D $PgData -l "$PgLogs\server.log" -o "-c listen_addresses=127.0.0.1 -c port=5432" -w start
```

Stop `runserver` with `Ctrl+C`. Stop the database with
`& "$PgBin\pg_ctl.exe" -D $PgData -m fast stop` (route Z).

**What to report back (contains no secrets):** the output of D1 to D6 and the
readiness probe, and whether `SHOW server_version` / `SHOW server_encoding` /
`listen_addresses` matched. Never include passwords or the contents of
`pg_hba.conf` beyond the non-comment lines already shown.

### Known risks and unverified items

| Item | Status |
| --- | --- |
| Django 5.2 with PostgreSQL 18 | Unverified; D1 to D6 are the first real test |
| Route Z binaries run on this VM (VC++ runtime, application control) | Unverified until `postgres.exe --version` succeeds |
| Route I needs administrator approval this account does not have | Blocker for Route I unless elevation is obtained |
| Server encoding of an installer-created cluster | Must be confirmed `UTF8` by B1 |
| `psycopg[binary]` bundled libpq (18.4) against a real 18.x server | Unverified until D1 |
| Installer and service-registration choices for client machines | Separate, later decision (Phase 8) |

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
