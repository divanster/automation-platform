# ADR 0007: Technology baseline for the backend skeleton

## Status

Accepted, with the qualifications in the "Open items" section. Exact patch
versions and the PostgreSQL major version are **not** selected yet.

Verification date: **2026-11-10**.

## Context

Phase 2 builds the backend skeleton. Its toolchain choices must be closed first
so that Phase 2 does not make them implicitly. The project owner directed these
directions: a supported Python version compatible with the stack, a supported
Django LTS line, Django REST Framework, standard `venv` and `pip`, Django users
with session authentication, and PostgreSQL as the only application database.

Three things are deliberately kept apart in this record:

1. what is **installed on the development VM**,
2. what is **officially supported** by the upstream projects,
3. what is **selected for this project**.

## Verification

### 1. Installed on the development VM (observed 2026-11-10, read-only)

| Item | Observation |
| --- | --- |
| Python | 3.14.4 found on `PATH` (installed under `D:\Software\Python.Python.3.14\`, and a second launcher location under the user profile). No `py` launcher was found, so other installed Python versions were **not enumerated**. |
| pip | 26.0.1 (shipped with that Python) |
| `venv` module | Available |
| PostgreSQL tools (`psql`, `pg_ctl`) | **Not found on `PATH`.** No PostgreSQL installation was detected. |

Nothing was installed, created, or modified on the machine.

### 2. Officially supported (as read on 2026-11-10)

| Subject | Finding | Source |
| --- | --- | --- |
| Python release status | 3.14: bugfix, first release 2025-10-07, end of life 2030-10. 3.13, 3.12, 3.11: security-only. 3.15: bugfix (listed first release 2026-10-09). 3.10: end-of-life. | https://devguide.python.org/versions/ |
| Django supported releases | 5.2 is an **LTS** release (latest listed 5.2.19; mainstream support ended 2025-12-03; extended support ends April 2028). 6.0 and 6.1 are non-LTS. The next LTS, 6.2, is listed for April 2027. | https://www.djangoproject.com/download/ |
| Django 5.2 and Python | Django 5.2 supports Python 3.10, 3.11, 3.12, 3.13, and **3.14 (added in 5.2.8)**. | https://docs.djangoproject.com/en/5.2/faq/install/ |
| DRF release and requirements | Latest listed 3.18.3 (released 2026-10-06 per the page). Requires Python 3.10+. Stated support: **Django 5.2, 6.0, 6.1**. Classifiers list Python 3.10 to 3.15. The project states it officially supports only the latest patch release of each Python and Django series. | https://pypi.org/project/djangorestframework/ and https://github.com/encode/django-rest-framework |
| Django user model | Changing `AUTH_USER_MODEL` after tables exist is possible but complex and cannot be done automatically. | https://docs.djangoproject.com/en/5.2/topics/auth/customizing/ |
| PostgreSQL support | Supported majors 14 to 18; 18 is newest (18.6 listed). Final release dates: 14 on 2026-11-12, 15 on 2027-11-11, 16 on 2028-11-09, 17 on 2029-11-08, 18 on 2030-11-14. | https://www.postgresql.org/support/versioning/ |

### Verification limitations

- Pages were read through a fetch tool that summarizes page content, not by
  manually reading the raw pages. Figures should be re-checked against the
  linked pages before they are relied on for a release decision.
- The official DRF documentation site (`django-rest-framework.org`) could **not**
  be reached from this environment: the request was redirected to a corporate
  web-filter block page, which was not followed. DRF facts above come from its
  PyPI project page and its GitHub repository, which the project maintains, not
  from the documentation site.
- Compatibility between Python 3.14 and third-party packages that this project
  will need (notably a PostgreSQL driver) was **not verified**.

## Decision

### Selected for this project

| Component | Selection | Notes |
| --- | --- | --- |
| Python | **3.14 series** (the version installed on the VM is 3.14.4) | Officially in bugfix support until 2030-10, and supported by Django 5.2.8 and later and by DRF. The exact patch version is pinned in Phase 2. **Fallback:** if a required dependency does not support 3.14, use the 3.13 series, which Django 5.2 also supports. |
| Django | **5.2 LTS line**, at version 5.2.8 or later | 5.2.8 is the first release supporting Python 3.14. The exact patch is pinned in Phase 2 (latest listed on the verification date was 5.2.19). |
| API framework | **Django REST Framework, 3.18 series** | Latest listed on the verification date was 3.18.3. Exact version pinned in Phase 2. |
| Dependency management | **Standard `venv` and `pip`** | No additional dependency manager initially. |
| Authentication | **Django's built-in users and session authentication** | See user-model open item. |
| Database | **PostgreSQL as the only application database** | Major version not yet selected. |

### Explicitly excluded from the initial skeleton

- JWT or other token-based authentication
- Custom user fields
- External identity integration (single sign-on)
- Any second application database, and any separate message broker
  ([ADR 0003](0003-persistent-background-worker.md))

### Dependency reproducibility policy

These rules apply from Phase 2:

1. **Exact versions are recorded during implementation.** Every direct
   dependency is pinned to an exact version.
2. **Transitive dependencies are reproducible.** The complete installed set,
   including transitive packages, is recorded as exact pins, so a clean
   environment reproduces the same package set. Hash verification is
   recommended; the mechanism is decided in Phase 2.
3. **Development and production dependencies are distinguishable.** They are
   recorded separately (for example, test and lint tools never appear in the
   production set).
4. **No unbounded "latest" dependencies.** No dependency is declared without a
   version constraint, and unpinned installs are not used for anything that
   gets tested or released.
5. Dependency changes are deliberate, reviewed changes, and the supported
   upstream version windows above are re-checked when versions are bumped.
6. The Python version used is recorded in the repository so developers and
   automation use the same one.

No dependency file or virtual environment has been created by this decision.

## Open items (not decided here)

- **PostgreSQL major version** and how developers obtain a local instance
  (native install or Docker). PostgreSQL is not installed on the VM. Candidate
  majors are those listed as supported above; the choice must consider the
  Windows installation story and driver support.
- **PostgreSQL driver** and its Python 3.14 support.
- **Django user model.** The owner's direction is Django's built-in users with
  no custom fields. Because changing the user model after the first migration is
  complex, Phase 2 must confirm, **before the first migration**, whether to use
  the built-in model directly or declare a project user model that adds no
  fields. Recommendation: declare a project user model with no added fields
  at the start, which keeps the owner's "no custom fields" direction.
- **Django 6.2 LTS transition.** Django 5.2 extended support ends April 2028,
  and the next LTS (6.2) is listed for April 2027. A planned move to 6.2 is
  expected during the project's life; it depends on DRF declaring support
  (DRF's stated support, on the verification date, did not yet list 6.2).

## Alternatives

- **Python 3.13 now.** Also supported by Django 5.2 and likely the safer choice
  for package wheels, but it is security-only status and older. Kept as the
  fallback.
- **Django 6.1 (non-LTS).** Newer, but shorter support and not the LTS line the
  owner asked for.
- **Plain Django views or Django Ninja.** Possible; DRF was directed for its
  maturity and permission and serializer ecosystem.
- **uv, Poetry, or pip-tools.** Offer lockfiles and tooling, but add another
  tool; the owner directed plain `venv` and `pip`. The reproducibility policy
  above is the requirement regardless of tool.
- **JWT or external identity.** Out of scope for a standalone installation;
  can be added behind the same API later.

## Consequences and trade-offs

- A single, conventional, well-documented Python web stack.
- Python 3.14 is recent; if a dependency lags, the fallback to 3.13 costs
  little because Django 5.2 supports both.
- Starting on Django 5.2 LTS commits the project to an LTS-to-LTS upgrade
  before April 2028.
- DRF supports only the latest patch of each series, so patch upgrades must be
  taken promptly.
- Plain `pip` provides no lockfile feature by itself; reproducibility depends on
  following the policy above.

## Conditions for revisiting

- A needed dependency does not support Python 3.14 (use the fallback) or Django
  5.2.
- DRF drops or delays Django LTS support, or becomes unmaintained.
- Django 6.2 is released and DRF supports it before Phase 2 starts, which could
  justify starting on 6.2 instead of upgrading later.
- Reproducibility with plain `pip` proves unreliable in practice.
