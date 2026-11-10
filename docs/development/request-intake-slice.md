# Request-intake preparation slice (database-independent)

> **Status: a small preparation slice, not the first workflow.** It is plain
> Python with DB-free tests. There are no Django models, migrations, endpoints,
> storage, persistence, approvals, exports, or background execution, and no real
> AI provider. It does **not** complete Phase 5 or start Phase 3. Phase 2 database
> acceptance is still pending the owner's local PostgreSQL validation. Nothing
> here touched a database or an AI provider, and everything described below as
> implemented was run only in the development VM, DB-free.

## What exists

Package `backend/workflows/request_intake/` (not a Django app; not in
`INSTALLED_APPS`; no models, migrations, views, or URLs):

| Module | Contents |
| --- | --- |
| `schema.py` | Frozen dataclasses `StructuredRequest`, `Requester`, `RequestedItem`, `SourceRef`, `Ambiguity`, and the source-reference excerpt convention. |
| `quantity.py` | The canonical quantity: `to_decimal` and its conversion rules. |
| `paths.py` | The field-path grammar and which paths are required. |
| `findings.py` | `Finding`, `FindingCode`, `Severity`, `ValidationResult`, the code-to-severity table, and `FINDINGS_CONTRACT_VERSION`. |
| `validation.py` | `validate_request(...)` and `ValidationLimits`: pure functions, no I/O, no state. |
| `extraction.py` | The `RequestExtractor` contract, `ExtractionResult`, and `DeterministicTestExtractor` (a fixture parser). |

### Schema (from `first-workflow.md`, section 4)

`requester.name` (required), `requester.contact`, `items` (at least one),
`items[].description` (required), `items[].quantity` (required, a positive
number), `items[].unit`, `items[].notes`, `notes`, `source_refs`. There is no
price, currency, stock, availability, or delivery field, and a test enforces it.

**Immutability.** Every class is a frozen dataclass and every collection is a
tuple. A frozen dataclass alone would still alias a `list` a caller passes in, so
the constructors copy lists into tuples (this was a real defect found in review).
There are no shared default instances, and a test walks the object graph of
drafts, extraction results, and validation results to prove they contain no
mutable container.

### Quantities (decided)

- **Fractions are allowed** in this initial generic schema. Unit-specific integer
  restrictions (for example whole boxes only) are **deferred**.
- **`Decimal` is the canonical validated type.** `RequestedItem` converts `int` and
  `float` quantities to `Decimal` when it is constructed; validation compares
  exact `Decimal` values.
- **Rejected:** booleans and other non-numbers (`invalid_type`); NaN and infinity
  (`invalid_value`); zero and negative values (`invalid_value`); a quantity above
  the maximum **only if a maximum is configured** (`exceeds_maximum`); a missing
  quantity (`missing_required`).
- **No silent rounding and no new precision limit.** A `Decimal` keeps exactly the
  digits and exponent it was given (`2.50` stays `2.50`), and an `int` is converted
  exactly at any size. Only construction, comparison, and formatting are used; no
  arithmetic is performed, so results do not depend on the ambient decimal
  context. Tests cover 40+ digit values, `1E+1000`, and a lowered context
  precision.

Conversion behavior (`quantity.to_decimal`):

| Input | Result |
| --- | --- |
| `Decimal` | Returned unchanged (finite values only) |
| `int` | Exact `Decimal` of the same value (`bool` is not accepted) |
| `float` | `Decimal(repr(value))`: the shortest decimal string that converts back to the same float, so `0.1` becomes `Decimal("0.1")` |
| text, `bool`, `None`, containers | `TypeError`. Text such as `"5"` is **not** converted by validation; reading numbers from text is an extractor's job |
| NaN, infinity | `ValueError` |

A `float` is a binary approximation, so converting it needs a convention. The
shortest-round-trip convention recovers the digits a person almost certainly meant
and never changes which float the result converts back to; a test checks that
round trip. Callers needing exact digits should pass a `Decimal` or an `int`. This
conversion convention is a choice made within this slice, recorded here so it can
be reviewed.

### Validation (from `first-workflow.md`, sections 5 and 6)

`validate_request(request, *, limits=None, source_text=None, ambiguities=())`
returns a `ValidationResult`. **Valid means no blocking findings.** It never
mutates its arguments (tested, including identity of nested objects).

| Code | Severity | Meaning |
| --- | --- | --- |
| `missing_required` | blocking | A required value is absent (`None`, blank, or no items) |
| `invalid_type` | blocking | A value has the wrong type (for example text or a boolean as a quantity) |
| `invalid_value` | blocking | A quantity is zero, negative, or not finite |
| `exceeds_maximum` | blocking | A quantity is above the configured `max_quantity` |
| `exceeds_length` | blocking | A text field is longer than the configured `max_text_length` |
| `invalid_source_ref` | blocking | A source reference does not match the supplied source text |
| `ambiguous_required_value` | blocking | The extractor reported an ambiguity on a **required** field |
| `ambiguous_optional_value` | warning | The extractor reported an ambiguity on an **optional** field |
| `duplicate_item` | warning | Two items have the same description under the conservative rule |
| `value_not_in_source` | warning | A value cannot be located in the supplied source text |

The three outcomes stay distinct: **valid input** (`is_valid`), **missing required
data** (`missing_required`), and **invalid field values** (`invalid_type`,
`invalid_value`, `exceeds_maximum`, `exceeds_length`, `invalid_source_ref`).
`ValidationResult` exposes `blocking`, `warnings`, `missing_required`,
`invalid_values`, and `ambiguities`.

**Ambiguity.** An ambiguity on a required field (`requester.name`, `items`,
`items[N]`, `items[N].description`, `items[N].quantity`) blocks approval even if the
field still carries a value. On an optional field it is a warning. Blocking and
warning ambiguity use **different codes**, so each code keeps exactly one severity.
`missing_required` is not weakened: a required value left `None` yields both
`missing_required` and `ambiguous_required_value`. An ambiguity naming a path that is
not in the grammar raises `ValueError` (an extractor bug, not data).

**Duplicates.** Warning only. Two items are reported if their descriptions are
identical after stripping surrounding whitespace, collapsing runs of whitespace, and
casefolding. Not applied: Unicode normalization, punctuation removal, plural or
stem handling, abbreviations, synonyms, or any semantic similarity (`A4 paper`,
`A4-paper` and `A4 papers` are not duplicates). Nothing is merged, deleted, or
summed; tests assert the draft is unchanged.

**Source references.** When a source text is supplied, `source_refs` are validated
against it. Coordinate convention (the fixture format only):

- `line` is **1-based** and counts lines exactly as Python's `str.splitlines()` does
  on the exact text given to the extractor (so `\r\n` is one separator, and `\v`, `\f`,
  `\x1c` to `\x1e`, `\x85`, `\u2028`, `\u2029` also split; a trailing line break does
  not add a line);
- `excerpt` is the cited line stripped of surrounding whitespace and cut to 200
  characters with a trailing `...` when longer;
- there are no character columns or offsets;
- a reference with no line only has to appear verbatim in the text.

A reference is invalid if its field is unknown or is not a value field, it names an
item that does not exist, its line is not an integer within the text, or its excerpt
differs from the cited line. No more general provenance matching is claimed (for
example for quotes produced by a language model); that is an open question. Without
a source text, references are not checked.

**Field paths and codes: the initial stable contract, version 1.** Paths are
`requester.name`, `requester.contact`, `items`, `items[N]`, `items[N].description`,
`items[N].quantity`, `items[N].unit`, `items[N].notes`, `notes`, plus the container
paths `requester`, `source_refs`, and `source_refs[N]` used by type and reference
findings. `N` is a zero-based decimal index without leading zeros. Codes, their
severities, and the path grammar are versioned by `FINDINGS_CONTRACT_VERSION` (now
`1`); a test pins the whole table, and an incompatible change requires a version
bump and a decision about already-stored findings. Messages are fixed text and never
echo the offending value; findings are ordered deterministically (rule findings in
field order, duplicates, source-location warnings, source-reference findings, then
ambiguity findings).

**Limits.** `ValidationLimits` is optional; `None` means "not configured" and the
rule is skipped. No business limit is invented and none has a default; a very long
text or a huge quantity is valid unless a limit is explicitly configured. Text
length limits remain unresolved for the later input and storage phase.

## The deterministic test extractor

`RequestExtractor` is the smallest provider-neutral contract: `extract(text)` returns
an `ExtractionResult` holding **candidate** data (`StructuredRequest`), any
`Ambiguity` records, and the extractor's name and version. It does not validate, and
validation does not extract. The contract lives at workflow level so that the AI
gateway, which must not know about business workflows, stays independent; wrapping a
real adapter behind it is deferred.

`DeterministicTestExtractor` (`name = "deterministic-test-fixture-parser"`,
`kind = "fixture_parser"`, `understands_arbitrary_documents = False`) is a **fixture
parser for one test template**. It is not an AI model, not document understanding,
and must never be presented as, or substituted for, a real extraction provider.

**What it can do.** Read one fixed, labelled-line template, repeatably:

```text
Requester: Jane Doe
Contact: jane@example.invalid
Item: 10 | boxes | A4 paper | for the print room
Item: 2 | | Stapler
Notes: Needed this week.
```

- Labels are case-insensitive. The first `Requester`, `Contact`, and `Notes` line
  wins. Each `Item` line is `quantity | unit | description [| notes]`.
- Empty values become `None`. A quantity written as an optionally signed number in
  **ASCII digits** (`10`, `-3`, `2.50`) becomes a `Decimal` with exactly the digits as
  written, and is **not** validated by the extractor (`0` and `-3` pass through for
  validation to reject). Digits from other scripts are not numbers (another defect
  found in review: `\d` had accepted them).
- Every extracted value gets a source reference following the convention above.
- Calls are independent: the extractor holds no state and returns new, immutable
  results; the same text always gives an equal result.

**What it cannot do.** It does not understand arbitrary documents or free prose
(such text yields an empty candidate), does not guess, and does not fill gaps: a
quantity such as `a few` becomes `None` plus an ambiguity quoting `a few`; a
malformed `Item` line becomes an empty item plus an ambiguity on `items[N]`. Lines
with other labels (for example `Price:`) are ignored, so no price or availability
data is ever produced.

## What the tests actually show

All new tests are pure-domain: standard-library `unittest`, no Django, no database,
no SQLite. They are in `backend/tests/` (`test_request_intake_*.py` and
`intake_support.py`), except `test_request_intake_django.py`, which needs Django
settings and only checks that the package is not wired into the project.

| What is tested | How, and its limits |
| --- | --- |
| Validation does not mutate input; drafts hold no mutable nested state; repeated calls are independent | In-process tests (including a walk of the object graph) |
| Quantity, ambiguity, duplicate, source-reference, and finding-contract rules | In-process tests, including a golden table of codes and severities |
| The package cannot import Django, drivers, network, or provider modules | **Static**: `ast` scan of the package source against an allow-list of standard-library modules, and of named built-ins (`open`, `exec`, `eval`, ...). This shows what the code *can* reach; it proves nothing about what runs |
| The extract-and-validate flow runs without Django, databases, network, or providers | **Runtime**: in a clean child interpreter those modules are made un-importable (`sys.modules[name] = None`) and the flow still runs |
| The flow performs no file, socket, subprocess, database, or dynamic-code events | **Runtime**: an audit hook (`sys.addaudithook`) in that child records events during the flow; none is a forbidden kind (a control test shows the hook does record a file `open`). Also in-process with `socket` and `open` replaced by functions that raise |
| The pure test modules themselves do not need Django | A static scan of their imports, and a run with `django` poisoned in the test process |

These checks are evidence about this code over the inputs exercised, not a
guarantee for all inputs. Audit events cover Python-level operations, not a C
extension making system calls without raising an audit event, and they are not a
sandbox.

## Running the tests

From `backend\` with the project's virtual environment.

Pure-domain tests, **no Django and no settings** (works with plain `unittest`):

```powershell
python -m unittest tests.test_request_intake_validation tests.test_request_intake_quantity tests.test_request_intake_extraction tests.test_request_intake_contract tests.test_request_intake_isolation tests.test_request_intake_boundaries tests.test_request_intake_runtime_boundaries
```

The whole DB-free suite through Django's test discovery (this also runs the existing
tests, which need Django settings, and the one Django-dependent slice module):

```powershell
python manage.py test
```

None of these needs a database. A default `manage.py test` excludes the
PostgreSQL-required tests (`requires_postgres`), and SQLite is never used.

**Why the two totals differ.** At the commit that introduced this document, the
standalone command above ran **181** tests. `manage.py test` ran **242**:
181 pure-domain slice tests (Django's discovery also runs them, since they are
`unittest` test cases) + 2 Django-dependent slice tests (`test_request_intake_django`)
+ 59 pre-existing skeleton tests (settings, health, user model, API security, ...).
Those pre-existing tests need Django settings and so are not part of the standalone
command. Counts are only valid for the commit they were run on, so re-run before
quoting them. The 3 PostgreSQL-required tests are **not** counted anywhere above and
**have not been run**.

## Deferred (not implemented)

Until the owner's real PostgreSQL validation is complete and later phases begin:

- Django models, migrations, and persistence of drafts (`ResultRevision`, content
  hashes, revision numbers).
- Approvals, rejections, invalidation on edit, and export (and its execution-time
  re-check).
- The workflow state machine, tasks, and background execution.
- Document upload, storage, and file hashing.
- The AI gateway, data-policy enforcement, capability checks, and any local or
  external model adapter.
- Configuration versioning and snapshots (the limits here are plain arguments).
- API endpoints and any UI.
- Authorization and audit events.

## Unresolved questions

Recorded in [open-questions.md](../architecture/open-questions.md), items 2.6 to
2.12: unit-specific integer restrictions; the canonical equality and export
representation of a `Decimal` quantity (and whether the `float` conversion
convention above is the one to keep); text length limits (no business limit is
invented); the source-location matching rules; source-reference conventions for
extractors other than the fixture parser; handling of stored findings after a
contract version bump; and how the extractor contract relates to the AI gateway.
