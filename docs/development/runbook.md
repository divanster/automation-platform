# Development Runbook

> **Status (Phase 2 skeleton): the backend skeleton exists; the business
> workflow, worker, real AI adapters, frontend, and installer do not.**
>
> **Two machines are involved, with different jobs** (section 0). Claude Code
> develops code in a Windows **VM** and runs only database-free checks there.
> **PostgreSQL 18 is to be installed on the owner's LOCAL Windows computer (it is
> not installed yet)**, and every real-database check and application smoke test is
> to run there.
>
> **Local validation is pending.** It is not done until the owner runs the
> commands in section 6 on the local computer and provides the results. Nothing in
> this document records a local installation or a PostgreSQL-backed test as
> completed. The production settings are a baseline only; the production server
> and service packaging are not built.

Commands are for **Windows PowerShell**. Section 6 is for the **local computer**;
sections 2 to 5 describe the VM's checks and the shared project conventions.
Nothing in this runbook assumes that the local computer has the VM's
permissions, Python installation, drive letters, or paths.

## 0. Where each activity happens

| Activity | VM (Claude Code) | Local Windows computer (owner) |
| --- | --- | --- |
| Code and documentation development, Git commits | **Yes** | Pull only (the owner may also work there) |
| Moving code between machines | Owner pushes from the VM with their normal Git authentication | Clone or fast-forward update from GitHub |
| Python virtual environment | Its own `.venv` (git-ignored), used for DB-free checks | Creates its **own** `.venv` from the committed locks; **never copies the VM's** |
| DB-free checks (`check`, compile, 242 tests) | **Yes** | Yes, as the first environment check |
| PostgreSQL 18 | **None. Never installed in the VM.** | To be installed and configured by the owner (not done yet) |
| Migrations, PostgreSQL-backed tests, real readiness, application smoke test | **Not performed. No acceptance claims.** | **Owner runs these and reports the results** |
| PyCharm | Not used | **Not confirmed as configured**; set up later (section 6.9) |

Rules that apply to both machines:

1. **Do not install PostgreSQL in the VM**, and do not register database services
   there.
2. **The VM never connects to the local computer's database.** No VM setting may
   point `AP_DB_HOST` at another machine, and no tunnel or port forwarding is set up.
3. **PostgreSQL is not exposed over the network.** It listens on loopback only
   and no inbound firewall rule is created for it.
4. **Passwords are typed interactively on the local computer.** They never appear
   in commands, files, this repository, or the conversation. Do not use
   `PGPASSWORD` and do not read or print existing secret values.
5. Code moves between machines **only through Git (GitHub)**.

## 1. Selected versions

Recorded 2026-11-10. Rationale and sources: [ADR 0007](../decisions/0007-technology-baseline.md).

| Item | Version | Where it comes from |
| --- | --- | --- |
| Interpreter | CPython **3.14**, 64-bit **AMD64**. The VM has 3.14.4 (recorded in `.python-version`). The local computer's Python is **not confirmed**. | Selected and tested environment; see the lock note below |
| pip (bootstrap) | 26.0.1 on the VM | Shipped with the interpreter; not pinned by the lock files |
| Django | **5.2.19** (5.2 LTS line) | `requirements/runtime.in` |
| Django REST Framework | **3.18.3** | `requirements/runtime.in` |
| Psycopg 3 | **3.3.8**, installed as `psycopg[binary]` | `requirements/runtime.in` |
| `psycopg-binary` (transitive) | 3.3.8, wheel `cp314-cp314-win_amd64`, bundled libpq reported as 18.4 | Resolved |
| asgiref, sqlparse, tzdata (transitive) | 3.13.0, 0.6.1, 2026.6 | Resolved |
| PostgreSQL server | **18**, native Windows install (not Docker, not WSL), on the owner's **local** computer | Owner decision; **not installed or validated yet** |

Why `psycopg[binary]`: its wheel bundles libpq, so no PostgreSQL client install and
no compiler is needed. Psycopg describes the binary packages as produced on a
best-effort basis; whether to switch to a source-built `psycopg[c]` for production
is a pending packaging decision.

Compatibility notes:

- Django 5.2.8 and later support Python 3.14.
- Django 5.2 documents "PostgreSQL 14 and higher" and does not name 18
  explicitly, so Django 5.2 with PostgreSQL 18 is **unverified until the
  PostgreSQL-backed tests have been run on the local computer**.
- **What the lock actually constrains.** The lock records exactly one sha256 hash
  per package. Six of the seven locked packages are **pure-Python universal
  wheels** (`py3-none-any`; `py2.py3-none-any` for tzdata) whose declared
  `Requires-Python` is 3.10 or newer (asgiref 3.11 or newer, tzdata 2 or newer), so
  they do **not** require Python 3.14. Only `psycopg-binary` is interpreter- and
  platform-specific: the lock holds the hash of the single file
  `psycopg_binary-3.3.8-cp314-cp314-win_amd64.whl`. With `--require-hashes
  --only-binary=:all:`, pip can accept only that file for `psycopg-binary`, so on
  another interpreter (for example 3.13) or CPU architecture (for example Windows
  on ARM) the install **fails at that one package**, because no wheel with a
  recorded hash matches.
- **Selected and tested environment: CPython 3.14 on Windows AMD64**, tested only
  on the VM. Other interpreters or platforms are untested. Supporting one would
  need a regenerated lock for `psycopg-binary` (a separate, unapproved decision);
  this documentation change regenerates nothing and changes no versions.

## 2. Virtual environments and dependencies

### Dependency files

| File | Purpose |
| --- | --- |
| `requirements/runtime.in` | **Direct runtime** dependencies, each pinned to an exact version |
| `requirements/development.in` | Direct **development** dependencies (none yet; includes runtime) |
| `requirements/runtime.txt` | **Generated** full runtime set including transitive packages, exact versions, sha256 hashes |
| `requirements/development.txt` | **Generated** development set (currently identical to runtime) |

Rules (from ADR 0007): exact pins only, transitive dependencies recorded,
runtime and development kept separate, no unbounded versions. For a runtime-only
environment (what a deployment would install), use `requirements\runtime.txt`
instead of `development.txt`.

To change a dependency: edit the exact pin in the `.in` file, install it in a
**clean** `.venv` with `python -m pip install --only-binary=:all: -r
requirements\development.in`, record the resolved set with `python -m pip
freeze`, download the wheels with `python -m pip download --only-binary=:all:
--no-deps` into a temporary folder, compute hashes with `python -m pip hash
--algorithm sha256 <wheel>`, update the `.txt` files, and re-verify by
installing them into another fresh environment. A helper script for this is a
possible later improvement.

Verified on the VM on 2026-11-10: a fresh environment built only from
`requirements/development.txt` produced an installed set identical to the VM's
`.venv`, a file with a deliberately altered hash was rejected, and the DB-free
tests passed in the fresh environment. **This has not been verified on the local
computer.**

### VM environment (code development and DB-free checks)

The VM's environment is `.venv/` in the VM's repository root (git-ignored). Do not
install anything into the global Python.

```powershell
cd C:\Users\YotovIvan\Projects\newnew            # VM path

& "D:\Software\Python.Python.3.14\python.exe" -m venv .venv      # VM interpreter path
.\.venv\Scripts\python.exe --version             # expect Python 3.14.4
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements\development.txt
.\.venv\Scripts\python.exe -m pip check          # expect: No broken requirements found.
```

### Local-computer environment

Created separately on the local computer, with that computer's own Python. See
section 6, part B. **Do not copy the VM's `.venv`**: virtual environments embed
absolute paths and are not portable.

## 3. Environment configuration

The application reads **process environment variables only**. It does not read a
`.env` file and has no dotenv dependency (a dependency would add a package for
something a few lines of PowerShell can do). Variables are documented, with
placeholders, in `.env.example`. These are the variables read by
`backend/config/settings/`:

| Variable | Used by | Notes |
| --- | --- | --- |
| `AP_DB_NAME`, `AP_DB_USER`, `AP_DB_PASSWORD` | all | Empty by default; opening a connection without them fails (no fallback database) |
| `AP_DB_HOST`, `AP_DB_PORT` | all | Default `127.0.0.1` and `5432`. The host must stay `127.0.0.1`. |
| `AP_DB_CONNECT_TIMEOUT` | all | Seconds, default `3`, minimum `1` |
| `AP_SECRET_KEY` | development optional, production **required** | Development generates a random per-process key if unset |
| `AP_ALLOWED_HOSTS` | development optional, production **required** | Comma-separated. Development defaults to localhost only; production rejects wildcards |
| `AP_CSRF_TRUSTED_ORIGINS` | production optional | Comma-separated explicit origins |
| `AP_RUN_DB_TESTS` | tests | Only `1` enables PostgreSQL-backed tests |

**On the VM, no real database variables are configured.** On the local computer,
section 6, part E sets the `AP_DB_*` variables for one PowerShell window at a
time. A local, git-ignored `.env` file is optional; if you use one, fill it in
yourself, never commit it, and load it into the current session:

```powershell
Copy-Item .env.example .env
# edit .env (the password is typed by you and never shared), then:
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim()
}
```

Settings modules: `manage.py` defaults to `config.settings.development`;
`wsgi.py` and `asgi.py` default to `config.settings.production`.

## 4. Starting the development server

On the **local computer**, after the database is provisioned (section 6):

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
  configured it starts, and readiness returns 503. This behavior was observed on
  the VM, where no database exists; the VM's readiness 503 is **not** a database
  check.
- Run `migrate` only on the local computer, after section 6, part D. No superuser
  has been created; do that yourself later with
  `python manage.py createsuperuser`.

## 5. Checks and tests

### Checks that need no database (VM and local computer)

```powershell
cd backend
python manage.py check                      # system checks (development settings)
python -m compileall -q apps config tests manage.py
python manage.py test                       # DB-free tests (see below)
```

`python manage.py test` runs **242 tests that need no PostgreSQL**. The project
test runner (`config/test_runner.py`) excludes tests tagged `requires_postgres`
unless `AP_RUN_DB_TESTS=1` is set, so a default run never asks Django to create a
test database on whatever server the environment points at. SQLite is never used.
These are the only checks the VM can run; they say nothing about PostgreSQL.

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

### Checks that need PostgreSQL (LOCAL COMPUTER ONLY; pending)

The exact, ordered commands are in **section 6, part F**. They are run only
against the **dedicated development server on the local computer** created in
section 6. With `AP_DB_NAME=automation_dev`, Django creates and drops a separate
`test_automation_dev` database (Django prefixes `test_` unless a `TEST` name is
configured, and this project configures none), so the role needs `CREATEDB`.
`--keepdb` and `--noinput` are deliberately not used, and no cleanup of an existing
database is part of the plan.

The PostgreSQL-backed tests are in `backend/tests/test_postgres_required.py`:
classes `PostgresUserTests` (2 tests) and `PostgresReadinessTests` (1 test), both
tagged `requires_postgres` and guarded by `AP_RUN_DB_TESTS=1`.

`python manage.py makemigrations --check --dry-run` also tries to read the
migration history from the database. Without a reachable database it prints a
`RuntimeWarning` and still reports whether model changes are missing migrations;
a DB-free equivalent is part of the test suite
(`tests/test_user_model.py`, `MigrationStateTests`).

## 6. Local Windows computer: setup and validation (PENDING, owner-operated)

> **Nothing in this section has been run.** No local repository, Python
> environment, PostgreSQL installation, migration, or test has been completed or
> observed by anyone but you. Local validation stays **pending until the owner
> runs these commands on the local computer and provides the results.**

Run everything in this section in **your own PowerShell window on the local
computer**, in order, **in the same window** (variables carry over). Stop at the
first unexpected result and report it; do not work around it. The commands do not
assume the VM's paths, drive letters, permissions, or Python location. Anything in
angle brackets is a **placeholder you must replace**.

### 6.1 Prerequisites

If an item is missing, **it is a prerequisite to obtain first**; the commands
below cannot run without it.

| Prerequisite | How to check | If missing |
| --- | --- | --- |
| Git for Windows | `git --version` | Install Git for Windows, reopen PowerShell |
| Access to GitHub for this repository, via the local computer's normal Git authentication | `git clone` or `git fetch` succeeds | Fix authentication locally. Never paste a token into a command, a URL, or the chat |
| The documentation commit is **in `origin/main` on GitHub** (found after an explicit `git fetch origin`; a commit that merely exists in a local repository proves nothing about a push) | Step A7 | The owner pushes from the VM with their normal Git authentication; continue only afterwards |
| CPython **3.14, 64-bit, AMD64** (the selected, tested environment) | Step B1 | Install CPython 3.14 (64-bit AMD64) from python.org. Another interpreter or Windows on ARM would fail at the `psycopg-binary` entry of the lock (section 1); supporting one is a separate, unapproved lock regeneration |
| Internet access to the Python package index (pip downloads the pinned wheels) | Step B3 | If a proxy or filter blocks it, report the exact error |
| PostgreSQL 18 | Part C | Install it (part C). Installing with the EDB installer needs administrator rights on that computer, which are **not assumed** |
| Windows PowerShell | You are using it | n/a |

### 6.A Repository

```powershell
# A1. Choose where the project lives on THIS (local) computer. This is NOT the VM path.
$LocalProject = "<LOCAL_PROJECT_PATH>"
if ($LocalProject -like "*<*") { throw "Replace <LOCAL_PROJECT_PATH> with a real folder first." }

# A2. Is Git available?
git --version

# A3. Is the repository already cloned there?
if (Test-Path -LiteralPath "$LocalProject\.git") { "Repository already present" } else { "No repository at that path" }
```

If it is **absent**, clone it (A4). A4 refuses to touch a non-empty folder that is
not a clone:

```powershell
# A4. Clone only if absent.
if (-not (Test-Path -LiteralPath "$LocalProject\.git")) {
    if ((Test-Path -LiteralPath $LocalProject) -and (Get-ChildItem -LiteralPath $LocalProject -Force | Select-Object -First 1)) {
        throw "That folder exists, is not a Git clone, and is not empty. Choose another path. Nothing was changed."
    }
    git clone https://github.com/divanster/automation-platform.git $LocalProject
}
```

Whether you just cloned it or it was already there, run A5 to A7. They **stop** on
the first problem. Never use `reset`, `--force`, or `clean`, and never work around
a failure:

```powershell
# A5. Verify identity and REQUIRE a clean working tree before anything is updated.
Set-Location -LiteralPath $LocalProject
git rev-parse --show-toplevel          # must equal your LOCAL_PROJECT_PATH
$Expected = "https://github.com/divanster/automation-platform.git"
$Origin   = git remote get-url origin
if ($Origin -ne $Expected) {
    $Shown = $Origin -replace '//[^/@]+@', '//***@'          # never print credentials, should a URL contain any
    throw "origin is '$Shown', expected '$Expected'. Not continuing."
}
$Branch = git branch --show-current
if ($Branch -ne "main") { throw "Current branch is '$Branch', expected 'main'. Not continuing." }
if (git status --porcelain) { git status --short; throw "The working tree is not clean. NOT updating. Report the lines above." }

# A6. Fetch origin explicitly, then update with fast-forward only.
git fetch origin
if ($LASTEXITCODE -ne 0) { throw "git fetch origin failed. Fix authentication or network locally and report the error." }
git merge --ff-only origin/main        # stops safely if the histories diverged
if ($LASTEXITCODE -ne 0) { throw "Fast-forward is not possible. Do NOT reset, force, or merge around it. Report it." }

# A7. Verify the documentation commit is contained in origin/main (as just fetched) and in your HEAD.
#     The hash is deliberately not hardcoded: the commit is found by its subject line.
$DocCommit = git log origin/main --format=%H -n 1 --grep='^docs: separate VM development from local database validation$'
if (-not $DocCommit) { throw "The documentation commit is NOT in origin/main, so it has not been pushed. Ask the owner to push from the VM, then repeat A6 and A7." }
git merge-base --is-ancestor $DocCommit origin/main; "contained in origin/main: $($LASTEXITCODE -eq 0)"
git merge-base --is-ancestor $DocCommit HEAD;        "contained in your HEAD:   $($LASTEXITCODE -eq 0)"   # both must be True
Test-Path "backend\manage.py", "requirements\development.txt", "docs\development\runbook.md"            # expect True, True, True
git log --oneline -5
```

Why A7 exists: commands such as `git cat-file -e <commit>` only prove that the commit
object exists **in your local repository**; they say nothing about whether it was
pushed. Only finding the commit reachable from the freshly fetched `origin/main`
proves it is on GitHub.

### 6.B Python and the local virtual environment

```powershell
Set-Location -LiteralPath $LocalProject

# B1. Find a CPython 3.14, 64-bit, AMD64. Use the 'py' launcher if present ...
py -0p
py -3.14 -c "import sys, platform, struct; print(sys.version); print(platform.machine(), struct.calcsize('P') * 8, 'bit')"
# ... or call a specific interpreter by full path:
# & "<PATH-TO-PYTHON-3.14-EXE>" -c "import sys, platform, struct; print(sys.version); print(platform.machine(), struct.calcsize('P') * 8, 'bit')"
#     expect: a 3.14.x version and "AMD64 64 bit"
```

If this fails, or shows a different major.minor or `ARM64`, **stop**: install a
64-bit CPython 3.14 for AMD64 first (see the prerequisites table). The selected and
tested environment is CPython 3.14 on Windows AMD64; any 3.14.x patch version is
acceptable (the VM's 3.14.4 is only what `.python-version` records). A different
interpreter or architecture would fail at the `psycopg-binary` entry of the lock
(section 1); do not work around that by removing `--require-hashes`.

```powershell
# B2. Create a LOCAL .venv (never copy the VM's). If one already exists, inspect it instead of recreating it.
if (Test-Path -LiteralPath ".\.venv") {
    "A .venv already exists. Check it; do not delete it:"
    .\.venv\Scripts\python.exe --version
} else {
    py -3.14 -m venv .venv             # or: & "<PATH-TO-PYTHON-3.14-EXE>" -m venv .venv
    .\.venv\Scripts\python.exe --version     # expect Python 3.14.x
}
# If an existing .venv reports the wrong version, RENAME it (for example to .venv-old) and create a new one.

# B3. Install exactly the committed lock: hash-verified, wheels only, no source builds.
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements\development.txt
.\.venv\Scripts\python.exe -m pip check          # expect: No broken requirements found.
.\.venv\Scripts\python.exe -m pip freeze         # expect the 7 pinned packages listed in section 1
```

If B3 reports a hash mismatch, a missing wheel, or a network error, **stop and
report the exact message**. Do not remove `--require-hashes` or `--only-binary`.

```powershell
# B4. DB-free checks on the local computer (no database variables are set yet).
Get-ChildItem Env: | Where-Object Name -like "AP_*" | Select-Object -ExpandProperty Name   # names only; expect nothing
Set-Location "$LocalProject\backend"
..\.venv\Scripts\python.exe manage.py check          # expect: System check identified no issues
..\.venv\Scripts\python.exe manage.py test           # expect: Ran 242 tests ... OK
Set-Location -LiteralPath $LocalProject
```

### 6.C PostgreSQL 18 on the local computer

**C1. Check for an existing installation (read-only).**

```powershell
# C1a. Services and server programs. Nothing is started, stopped, or changed.
Get-Service | Where-Object { $_.Name -match 'postgres|pgsql' -or $_.DisplayName -match 'PostgreSQL' } | Select-Object Name, Status, StartType
Get-CimInstance Win32_Service | Where-Object { $_.Name -match 'postgres' } | Select-Object Name, State, PathName   # program and data folder; no secrets
Get-Command psql -ErrorAction SilentlyContinue | Select-Object Source

# C1b. The ACTUAL installed server version, read from each postgres.exe. This does not connect to any database.
Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\postgres.exe" -ErrorAction SilentlyContinue | ForEach-Object { "{0} -> {1}" -f $_.FullName, (& $_.FullName --version) }
#     If a service PathName in C1a points to another folder, run:  & "<FOLDER-FROM-PathName>\postgres.exe" --version

# C1c. What is listening on the usual ports, and which process owns it.
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 5432, 5433 } | Select-Object LocalAddress, LocalPort, OwningProcess
```

C1b reports the installed program's version. The version of the **running** server
is confirmed from inside it at step D1 (`SHOW server_version`).

**Never modify, stop, upgrade, reconfigure, or delete an existing PostgreSQL
installation, role, database, or data without the owner's explicit approval.**

An existing PostgreSQL 18 instance is **suitable** only if **all** of these hold:

- its major version is 18 (C1b, and again at D1);
- it is the owner's own development server, not shared and not production;
- it listens on loopback only (C3);
- D1 shows `scram-sha-256` authentication (no `trust`) and `UTF8` encoding;
- the owner holds its `postgres` superuser credentials, or an administrator the
  owner trusts will create the role and database.

| Finding | Action |
| --- | --- |
| Nothing found | Continue with C2 (install) |
| A **suitable** PostgreSQL 18 instance exists | **Do not install another instance.** Skip C2, set `$PgBin` and `$PgPort` from what C1 found, and continue with C3 and part D |
| A PostgreSQL 18 instance exists but is **not suitable** (shared, non-loopback, `trust`, not UTF-8, or no superuser access) | Stop and report. Change nothing in it without the owner's approval; the owner decides whether to approve changes or to install a separate instance |
| Another PostgreSQL major version, or a server you do not recognize | Do not touch it. Report it. Installing a separate PostgreSQL 18 **beside** it on a different port (for example `5433`, used as `$PgPort` everywhere below) leaves it unchanged, but confirm that a second server is acceptable on that computer first |
| Something that is not PostgreSQL is listening on 5432 | Choose another port for the new server |

**C2. Install PostgreSQL 18 (selected plan: the EDB installer).**

Official sources:

- Windows downloads page (links to the EDB installer and the binaries archive):
  https://www.postgresql.org/download/windows/
- EDB installer downloads (certified installer, PostgreSQL 18 for 64-bit Windows):
  https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
- `initdb` reference: https://www.postgresql.org/docs/18/app-initdb.html

Download only from those pages. Record the exact version you install and the
SHA-256 of the file you downloaded (`Get-FileHash <file> -Algorithm SHA256`) in
your own notes; no checksum or signature published by EDB was confirmed during
this review.

The installer writes under `Program Files` and registers a Windows service, so it
**needs administrator rights on that computer, which this runbook does not assume
you have**. If you do not have them, obtain approval, or use the optional
alternative below. In the wizard:

1. Components: select **PostgreSQL Server** and **Command Line Tools**. Clear
   **pgAdmin 4** and **Stack Builder**; they are not needed.
2. Installation and data directories: accept the defaults, or choose folders
   outside the repository.
3. **Superuser (`postgres`) password:** type it yourself in the wizard.
4. **Port:** `5432`, or the alternative you chose in C1.
5. **Locale:** accept the default; the encoding is checked in part D.
6. Do **not** add any inbound firewall rule. If Windows Firewall prompts for
   `postgres.exe`, choose to deny or cancel; loopback needs no rule.

**C3. Verify it is local-only (read-only).**

```powershell
$PgBin  = "<PG18_BIN_FOLDER>"    # usually C:\Program Files\PostgreSQL\18\bin, but confirm from C1; it may differ
$PgPort = 5432                   # or the port you chose in C1
if ($PgBin -like "*<*") { throw "Replace <PG18_BIN_FOLDER> first." }

& "$PgBin\postgres.exe" --version                                  # expect: postgres (PostgreSQL) 18.x
Get-NetTCPConnection -State Listen -LocalPort $PgPort | Select-Object LocalAddress, LocalPort   # expect ONLY 127.0.0.1 and/or ::1
```

If PostgreSQL listens on any non-loopback address (`0.0.0.0` or `::`), **stop and
report it**. Do not continue until `listen_addresses` is limited to loopback
(changing it needs access to the server's configuration and a restart).
Authentication is checked from inside the server in part D.

**Optional alternative (NOT the selected plan): the EDB binaries zip archive**

Use only if the installer cannot be used on the local computer. EDB describes the
archive as a convenience for expert users and recommends the installer otherwise
(https://www.enterprisedb.com/download-postgresql-binaries). It needs no service and
no administrator rights, but the server is an ordinary process you start and stop
yourself. Placeholders; keep these folders **outside the repository**:

```powershell
$PgRoot = "<PG_ROOT_FOLDER>"; $PgData = "<PG_DATA_FOLDER>"; $PgLogs = "<PG_LOGS_FOLDER>"
$PgPort = 5432
if (($PgRoot + $PgData + $PgLogs) -like "*<*") { throw "Replace the placeholders first." }

Get-FileHash "<PATH-TO-DOWNLOADED-ZIP>" -Algorithm SHA256        # note the hash
Expand-Archive -Path "<PATH-TO-DOWNLOADED-ZIP>" -DestinationPath $PgRoot
$PgBin = "$PgRoot\pgsql\bin"
& "$PgBin\postgres.exe" --version                                # expect 18.x; a missing-DLL error means the VC++ runtime is absent: report it

# UTF-8 cluster, scram-sha-256 everywhere. initdb prompts for the superuser password: type it yourself.
& "$PgBin\initdb.exe" -D $PgData -U postgres -E UTF8 --no-locale -A scram-sha-256 --pwprompt

New-Item -ItemType Directory -Force $PgLogs | Out-Null
& "$PgBin\pg_ctl.exe" -D $PgData -l "$PgLogs\server.log" -o "-c listen_addresses=127.0.0.1 -c port=$PgPort" -w start
& "$PgBin\pg_ctl.exe" -D $PgData status
```

Then run the C3 verification and continue with part D. Stop it later with
`& "$PgBin\pg_ctl.exe" -D $PgData -m fast stop`. `--no-locale` gives the C
collation, fine for development; locale and collation for real installations are
an open decision.

### 6.D Role and database provisioning

Development names and privileges:

| Item | Value |
| --- | --- |
| Role | `automation_dev` |
| Database | `automation_dev`, owned by the role |
| Django test database | `test_automation_dev`, created and dropped by Django during the DB-required tests |
| Role attributes | `LOGIN`, `NOSUPERUSER`, `NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS`, and `CREATEDB` **only** so Django can create the test database |
| Network | Loopback (`127.0.0.1`) only |
| Authentication | `scram-sha-256`; **no `trust`** |

Production privileges are a separate future decision. Each command that connects as
`postgres` prompts for the superuser password; type it locally. **No password
appears in any command below.**

```powershell
$Psql = "$PgBin\psql.exe"
$Conn = @("-h", "127.0.0.1", "-p", "$PgPort", "-U", "postgres", "-d", "postgres")

# D1. Real server version, encoding, listen address, and authentication rules (no secrets are shown).
& $Psql @Conn -c "SHOW server_version;" -c "SHOW server_encoding;" -c "SHOW listen_addresses;" -c "SELECT line_number, type, database, user_name, address, auth_method FROM pg_hba_file_rules ORDER BY line_number;"
#     expect: 18.x, UTF8, a loopback listen address, and NO row whose auth_method is 'trust'

# D2. Create the development role (no password in this command).
& $Psql @Conn -c "CREATE ROLE automation_dev LOGIN NOSUPERUSER NOCREATEROLE CREATEDB NOREPLICATION NOBYPASSRLS;"

# D3. Set its password interactively (psql prompts twice and sends it hashed).
& $Psql @Conn -c "\password automation_dev"

# D4. Create the development database owned by that role.
& $Psql @Conn -c "CREATE DATABASE automation_dev OWNER automation_dev ENCODING 'UTF8' TEMPLATE template0;"

# D5. Verify attributes and existing databases. test_automation_dev must NOT exist yet.
& $Psql @Conn -c "\du automation_dev" -c "SELECT datname, pg_catalog.pg_get_userbyid(datdba) AS owner, pg_encoding_to_char(encoding) AS encoding FROM pg_database ORDER BY datname;"

# D6. Connect as the application role (prompts for its password).
& $Psql -h 127.0.0.1 -p $PgPort -U automation_dev -d automation_dev -c "SELECT current_user, current_database(), version();"
```

Expected at D5: `automation_dev` shows `Create DB` and none of `Superuser`,
`Create role`, `Replication`, `Bypass RLS`. If the role or either database
**already exists**, or `test_automation_dev` exists, **stop and report it**; do not
drop or alter anything unseen.

### 6.E Environment configuration (this PowerShell window only)

The variable names are exactly those read by `backend/config/settings/base.py`.
The password is read without echo, is not written to any file, and exists only for
this window's lifetime:

```powershell
$env:AP_DB_NAME = "automation_dev"
$env:AP_DB_USER = "automation_dev"
$env:AP_DB_HOST = "127.0.0.1"                  # must stay loopback; never a remote host
$env:AP_DB_PORT = "$PgPort"
$secure = Read-Host "automation_dev password (not echoed)" -AsSecureString
$env:AP_DB_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
Remove-Variable secure
```

Optional: `AP_DB_CONNECT_TIMEOUT` (seconds, default 3). Do not echo
`$env:AP_DB_PASSWORD`. Leave `AP_RUN_DB_TESTS` unset except where a command below
sets it. Closing the window discards all of these.

### 6.F Validation (run in order; stop at the first unexpected result)

```powershell
$Py = "$LocalProject\.venv\Scripts\python.exe"
Set-Location "$LocalProject\backend"

# Guard: if the flag is already set in this window, the "DB-free" runs below would no longer be DB-free.
if ($env:AP_RUN_DB_TESTS) { throw "AP_RUN_DB_TESTS is already set in this window. Run: Remove-Item Env:AP_RUN_DB_TESTS   then repeat." }

# F1. The application connects with the application role (real connection).
& $Py manage.py check --database default
& $Py manage.py shell -c "from django.db import connection; cur = connection.cursor(); [print(q, '->', (cur.execute(q), cur.fetchone())[1]) for q in ('SELECT current_user, current_database()', 'SHOW server_version', 'SHOW server_encoding')]"
#     expect: automation_dev / automation_dev, 18.x, UTF8

# F2. Migrations: inspect, apply to the development database, re-inspect.
& $Py manage.py showmigrations
& $Py manage.py migrate
& $Py manage.py showmigrations                     # expect every line marked [X], including accounts 0001_initial

# F3. Migration consistency with a reachable database (no RuntimeWarning expected now).
& $Py manage.py makemigrations --check --dry-run   # expect: No changes detected

# F4. DB-FREE tests. AP_RUN_DB_TESTS is unset (guarded above), so the runner excludes every
#     'requires_postgres' test and Django sets up no test database.
& $Py manage.py test                               # expect: Ran 242 tests ... OK
"exit code: $LASTEXITCODE"                         # expect 0

# F5. REAL PostgreSQL tests. These DO touch PostgreSQL: Django creates and drops 'test_automation_dev'
#     on the server from part E. No --keepdb, no --noinput. try/finally guarantees the flag is
#     removed even if the test command fails or is interrupted.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test --tag requires_postgres -v 2
    "exit code: $LASTEXITCODE"                     # expect 0
} finally {
    Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue
}
#     expect: creates 'test_automation_dev', Ran 3 tests ... OK, then destroys it.
#     If Django asks whether to delete an existing test database, answer 'no' and report it.
if ($env:AP_RUN_DB_TESTS) { "WARNING: AP_RUN_DB_TESTS is still set" } else { "AP_RUN_DB_TESTS is unset again" }

# F6. Optional: the whole suite in one run (242 DB-free + 3 PostgreSQL, an expectation: the PostgreSQL tests have not been run). Same try/finally rule.
try {
    $env:AP_RUN_DB_TESTS = "1"
    & $Py manage.py test                           # expect: Ran 245 tests ... OK
    "exit code: $LASTEXITCODE"
} finally {
    Remove-Item Env:AP_RUN_DB_TESTS -ErrorAction SilentlyContinue
}
```

F4 and F5 answer different questions. F4 shows that the application code is sound
without a database. Only F5 (and F1 to F3 and F7) exercise PostgreSQL. **None of
them has been run by anyone yet.**

**F7. Real readiness probe and localhost application smoke test.** The server
needs the same `AP_DB_*` variables, so start it in the window from part E, then
probe from a **second** window (the probe needs no variables):

```powershell
# Window 1 (the window from part E):
& $Py manage.py runserver 127.0.0.1:8000 --noreload

# Window 2:
foreach ($p in "live", "ready") {
    $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/health/$p/"
    "$p -> $($r.StatusCode) $($r.Content)"
}
#     expect: live -> 200 {"status":"ok"}   and   ready -> 200 {"status":"ok"}
(Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/admin/login/").StatusCode   # expect 200: the app serves its login page
Get-NetTCPConnection -State Listen -LocalPort 8000 | Select-Object LocalAddress, LocalPort   # expect only 127.0.0.1
```

Stop `runserver` with `Ctrl+C`. Optional real failure check, only if you have
administrator rights and are sure no other application uses that server: with
`runserver` still running, stop the PostgreSQL service, expect the readiness
probe to return **503** `{"status":"unavailable"}`, then start the service again.
No cleanup commands are provided on purpose: the development database and role are
left in place, and nothing is dropped.

**What to report back (contains no secrets):** the output of every step from A5 and
B1 through F7 (versions, `pip freeze`, `Ran N tests`, the migration listing, the
readiness lines), whether `SHOW server_version`, `SHOW server_encoding`,
`listen_addresses`, and the `pg_hba_file_rules` result matched, and the exact text
of any error. **Never include passwords, tokens, or other secret values.**

### 6.9 PyCharm (later; not confirmed as configured)

The local PyCharm project and interpreter are **not confirmed to exist** and are
not part of this validation. Once the checks above pass, PyCharm can be configured
later to use the local `.venv` and per-run-configuration environment variables
(typed locally, never committed). `.idea/` is already git-ignored. This runbook will
be updated when that is done and confirmed.

### 6.10 Known risks and unverified items

| Item | Status |
| --- | --- |
| Django 5.2 with PostgreSQL 18 | Unverified; F1 to F6 are the first real test |
| The lock on any environment other than CPython 3.14 on Windows AMD64 | Untested; only the `psycopg-binary` entry is interpreter- and platform-specific (section 1) |
| Whether the documentation commit is on GitHub | Unverified; step A7 checks it from the local side |
| `psycopg[binary]` bundled libpq (18.4) against a real 18.x server | Unverified until F1 |
| The local computer's Python, Git, permissions, and network access | **Unknown**; checked by 6.1, A and B |
| Installing with the EDB installer needs administrator rights on the local computer | Not assumed; unconfirmed |
| Hash-locked install on the local computer | Unverified; verified only on the VM |
| Server encoding of an installer-created cluster | Must be confirmed `UTF8` at D1 |
| Installer and service-registration choices for client machines | Separate, later decision (Phase 8) |

## 7. What is implemented, tested, and pending

| Area | Status |
| --- | --- |
| Backend skeleton (`config`, `accounts`, `health`, tests) | **Implemented** (Phase 2) |
| 242 DB-free tests, `manage.py check`, compile checks | **Passing on the VM** (this is all the VM can verify) |
| `accounts.User` (extends `AbstractUser`, no added fields) and its initial migration | **Implemented**; migration **not applied** anywhere |
| PostgreSQL 18 | **Not installed anywhere yet.** To be installed on the owner's local computer; never on the VM |
| Local clone, local `.venv`, hash-locked install on the local computer | **Pending** (owner) |
| Migrations, the 3 PostgreSQL-required tests, real readiness 200, localhost smoke test | **Pending** (owner runs locally and reports); **no acceptance claim** |
| PyCharm project and interpreter | Not confirmed as configured |
| Request-intake preparation slice: plain-Python schema, pure validation, deterministic test fixture parser, 183 DB-free tests ([details](request-intake-slice.md)) | **Implemented**; DB-free only; passing on the VM |
| Business workflow: cases, documents, revisions, approvals, exports (and persistence of the above) | Not implemented |
| Task queue and background worker | Not implemented |
| AI gateway and any AI adapter | Not implemented |
| Frontend | Not implemented |
| Production application server, Windows service packaging, installer | Not implemented |
| TLS, HSTS, static file serving, log configuration, backups | Not decided or implemented |
| Exposure of the Django admin route (`/admin/`) in production or a hub | Route exists in the skeleton; its production exposure is undecided |
| Production never reads development defaults | Implemented and tested: production requires its own `AP_SECRET_KEY` and explicit hosts, and shared settings hold no key |
| JWT, registration, password reset, custom login endpoints, external identity | Excluded by decision |
