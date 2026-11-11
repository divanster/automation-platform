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
| `canonical.py` | Canonical serialization (format version 1) and SHA-256 content hashing: `canonicalize_request`, `CanonicalContent`, `canonical_decimal_string`. An internal preparation component. |

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

## Canonical serialization and content hashing (format version 1)

> **An internal preparation component, not an export feature.** It defines the exact
> bytes that a content hash covers, so that later phases can bind a decision to one
> specific reviewed content. Nothing is stored, signed, authorized, or exported, no
> file or database is touched, and no revision or approval exists yet.

`canonicalize_request(request, *, limits=None, source_text=None, ambiguities=())`
returns one `CanonicalContent` with `canonical_format_version`, `canonical_bytes`,
`content_hash` (SHA-256, 64 lowercase hex characters), `hash_algorithm`, and the
`validation` result. The bytes, the version, and the hash always come from one
operation, and the hash is re-checked against the bytes when the result is built, so a
caller cannot hash a different representation. The module uses only `hashlib`, `json`,
`decimal`, and the other allowed standard-library modules. **`source_text` is required
whenever the draft has source references** (see below).

### What the hash covers

| Included: the reviewed business content | Excluded: future revision or audit metadata |
| --- | --- |
| `requester` (name, contact); `items` in order (description, quantity, unit, notes); `notes`; `source_refs` in order (field_path, excerpt, line) | Validation findings (warnings included); extractor-reported ambiguities; the limits and source text used for validation; extractor or provider identity and version; timestamps; authors; identifiers |

This matches the data model, where a `ResultRevision` keeps its payload, content hash,
findings, origin, and author as separate things, and no confirmed decision conflicts
with it. Source references **remain part of the business content** (confirmed), so
editing one changes the hash.

### Source references and the source text

Because source references are hashed content, they must be verified:

- **If the draft has any source reference, `source_text` is required** and every
  reference is validated against it (the coordinate convention above). Omitting it
  raises `SourceTextRequiredError`: verification is **never silently skipped**. This is
  checked first, so a caller who forgot the argument is told so instead of receiving a
  misleading list of findings.
- **If there are no source references,** `source_text` may be omitted, and the other
  validation rules still apply. Supplying one anyway changes nothing.
- **The source text is never added to the canonical payload.** The hash therefore
  fingerprints the included content only. It does **not** prove the identity or
  authenticity of the original document: two different documents that contain the same
  referenced lines give the same hash. The identity and hash of the original document
  remain future revision metadata (the specified upload step is to record a SHA-256 of the
file; nothing of that is implemented).

### What is refused

| Refusal | Exception | When |
| --- | --- | --- |
| Blocking findings | `DraftNotValidError` (carries the full validation result) | Any blocking finding, including a blocking ambiguity and an invalid source reference. The message names a count and never echoes content |
| Missing source text | `SourceTextRequiredError` | Source references present, `source_text` not supplied |
| Serialization safety | `SerializationSafetyError` (`reason` and `path`) | A draft that passes business validation but cannot be serialized safely (next section) |
| Other representation failures | `CanonicalizationError` | For example a quantity that is not a `Decimal` (see below) |

All are subclasses of `CanonicalizationError` (itself a `ValueError`). **Warnings
never block.** They are not in the bytes, they are not treated as resolved, and they
stay available on `content.validation`. Canonicalization converts nothing: a quantity
that is not already a `Decimal` (possible only by bypassing the draft's constructor) is
refused, never converted.

### Serialization safety versus business validation

A draft can pass business validation and still fail canonical serialization safety
checks. Validation has no business limit on how large a quantity is or on whether text
is encodable, and it was **not** changed. Serialization refuses, with
`SerializationSafetyError`:

- **`reason="decimal_expansion"`:** a quantity whose plain decimal form would need more
  than 999,999 padding zeros (for example `1E+1000000`). The size check is made from
  the digit count and exponent **before any padded string is built**, so a refusal
  allocates almost nothing (tested: under 64 KiB even for an exponent of 10^18).
  Nothing is rounded, truncated, or switched to exponent notation. The number mirrors
  the default decimal context's exponent range; it is a **memory-safety guard, not a
  business quantity limit**, it does not limit the digits of the coefficient, and
  large representable quantities (such as `1E+1000`) are serialized exactly.
- **`reason="unencodable_text"`:** text containing an unpaired surrogate, which cannot
  be written as UTF-8. It is refused explicitly with the field path; no character is
  replaced, dropped, or repaired. A valid surrogate pair is one ordinary character.

`error.path` names the field when known (for example `items[1].quantity`); messages
never echo the value. Whether to also enforce these in validation is open question 2.13.

### The version-1 rules (exact)

This is the project's own explicitly defined format. It is **not** claimed to be
compatible with RFC 8785 or any other canonical-JSON standard.

1. The bytes are the UTF-8 encoding, with no BOM, of one compact JSON object
   `{"canonical_format_version":1,"payload":{...}}`, with the separators `,` and `:`,
   no whitespace between tokens, and no trailing newline.
2. Object keys are fixed names sorted by Unicode code point at every level: payload
   `items`, `notes`, `requester`, `source_refs`; requester `contact`, `name`; item
   `description`, `notes`, `quantity`, `unit`; source reference `excerpt`,
   `field_path`, `line`.
3. Every schema field is always present. An absent optional value is `null`, and no
   source references is `[]`. `null` and the empty string are **different** values.
4. Text is preserved exactly: no trimming, case change, whitespace collapsing, or
   Unicode normalization (the composed and decomposed spellings of an accented letter
   are different content).
5. Arrays keep their order (items, source references).
6. String escaping is that of Python's `json` module with `ensure_ascii=False`: the
   quote, the backslash, and the control characters below U+0020 are escaped (short
   forms for backspace, form feed, newline, carriage return, and tab; otherwise
   backslash, `u`, and four lowercase hex digits). Every other character, including
   U+007F, U+2028, U+2029, and all non-ASCII text, is written as raw UTF-8. Tests pin
   these bytes.
7. A quantity is a string in canonical plain decimal notation (next section). A source
   reference `line` is a JSON integer or `null`.
8. No floats, NaN, infinity, timestamps, or random values appear. The result does not
   depend on Python's hash seed (tested in fresh processes).
9. The hash is SHA-256 over exactly these bytes, as lowercase hex.

### Decimal representation

`canonical_decimal_string(Decimal)` gives numerically equal values the same string, in
plain notation, never with an exponent:

| Value | Canonical string |
| --- | --- |
| `1`, `1.0`, `1.00`, `1E+0`, `10E-1` | `1` |
| `2.50`, `25E-1` | `2.5` |
| `100`, `1E+2`, `1.00E+2` | `100` |
| `0.0010`, `1E-3` | `0.001` |
| `1E-5` | `0.00001` |
| `0`, `0.000`, `-0` | `0` |
| `-2.50` | `-2.5` |

The only digits removed are trailing zeros of the value, which cannot change it, and
the exponent is applied as written. There is no rounding, no `normalize` or
`quantize`, and no float; the digits come from `Decimal.as_tuple`, so the result does
not depend on the active decimal context (tested under several precisions, rounding
modes, and exponent limits). The draft itself still holds the quantity as given
(`2.50`); only the canonical form treats `2.5` and `2.50` as the same value, so the
hash records a quantity's numeric value, not how it was written. A coefficient of any
length is accepted.

**Safety guard.** A quantity needing more than 999,999 padding zeros (for example
`1E+1000000`) is refused with `SerializationSafetyError`. This is a memory-safety guard,
not a business or precision limit; see "Serialization safety versus business validation"
above.

### The golden fixture

A small draft (accented text, a quote, `null` and empty-string optionals, quantities
`10.0` and `2.50`, and two source references, one with a `null` line) is canonicalized
together with a three-line source text that verifies both references (the middle line
is unreferenced and does not appear in the bytes). The expected canonical bytes were
typed by hand into the test:

```text
{"canonical_format_version":1,"payload":{"items":[{"description":"A4 paper","notes":"","quantity":"10","unit":"boxes"},{"description":"Café beans","notes":null,"quantity":"2.5","unit":null}],"notes":"Deliver to \"Room 5\"","requester":{"contact":null,"name":"Jane Doe"},"source_refs":[{"excerpt":"Requester: Jane Doe","field_path":"requester.name","line":1},{"excerpt":"Item: 2.50 | | Café beans","field_path":"items[1].description","line":null}]}}
```

That is 450 bytes in UTF-8, where `é` is the two bytes `C3 A9`. The expected hash,
`b161c35b51484a3ff92b96e44ccfb7e1f62e1c37c6acaeec4a8d692c9e0a9c67`, was calculated
independently over exactly those bytes with GNU `sha256sum`, PowerShell
`Get-FileHash`, and `openssl dgst -sha256`; all three agreed, and the code's output
equals both the bytes and the hash.

### What the hash is not, and how it must be used

- A hash is a fingerprint of content. It is **not** a signature, a MAC, proof of
  authorship, or an authorization: there is no key, and anyone can compute it.
- Stored revision identity, storing the hash and format version, and approvals bound
  to a revision and its hash are **deferred**; nothing here records a decision.
- It does not prove the identity or authenticity of the original document (see above).

### Format-version contract

- **Version 1 fixes both the canonicalization rules and the current payload field
  structure:** which fields exist, their names, and which are included in the hashed
  content.
- An incompatible change to **either** (a canonicalization rule, the included fields,
  the payload or schema structure, or the quantity representation) requires a new
  `CANONICAL_FORMAT_VERSION`. The version is inside the hashed bytes, so a version-1 hash
  can never equal a version-2 hash by accident.
- **Historical bytes and hashes must never be silently recomputed** under new rules.
  Store the format version with every hash. A hash recorded under version 1 keeps its
  version; to move to version 2, compute the new hash alongside it and keep the old one
  as the historical record.
- There is deliberately **no separate schema-version field** in the version-1 envelope;
  whether the hash should be tied to a result-schema version is open question 2.14.

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
| Canonical bytes and hash follow the version-1 rules and are deterministic | In-process tests (escaping, Unicode, null versus empty, order, change sensitivity, refusals), plus runs in fresh processes under different `PYTHONHASHSEED` values |
| The golden draft has exactly the fixed bytes and hash | A golden test against hand-typed bytes and an expected hash calculated by three independent tools |
| Source references require a verifying source text; the source text is never hashed | Tests for the required-text refusal, verification failures, the no-reference case, and that different documents with the same referenced lines give one hash |
| The serialization safety refusals happen before large allocations | Boundary tests with a small patched limit (no huge strings), and a `tracemalloc` check that refusing an exponent of 10^18 allocates under 64 KiB |
| Canonicalization converts nothing | A test that a non-`Decimal` quantity is refused, and an AST check that the module uses no `float`, `round`, `quantize`, `normalize`, or conversion helpers |
| The Decimal canonical form does not depend on the decimal context | Tests under several precisions, rounding modes, and exponent limits |
| Canonicalization and hashing also do no file, network, subprocess, or database work | The same clean-child runtime check now canonicalizes one valid draft; the static scan allows only `hashlib` and `json` in addition to the earlier list |

These checks are evidence about this code over the inputs exercised, not a
guarantee for all inputs. Audit events cover Python-level operations, not a C
extension making system calls without raising an audit event, and they are not a
sandbox.

## Running the tests

From `backend\` with the project's virtual environment.

Pure-domain tests, **no Django and no settings** (works with plain `unittest`):

```powershell
python -m unittest tests.test_request_intake_validation tests.test_request_intake_quantity tests.test_request_intake_extraction tests.test_request_intake_contract tests.test_request_intake_isolation tests.test_request_intake_canonical tests.test_request_intake_boundaries tests.test_request_intake_runtime_boundaries
```

The whole DB-free suite through Django's test discovery (this also runs the existing
tests, which need Django settings, and the one Django-dependent slice module):

```powershell
python manage.py test
```

None of these needs a database. A default `manage.py test` excludes the
PostgreSQL-required tests (`requires_postgres`), and SQLite is never used.

**Why the two totals differ.** When this section was written, in the development VM
only, the standalone command above ran **282** tests and `manage.py test` ran **343**:
282 pure-domain slice tests (Django's discovery also runs them, since they are
`unittest` test cases) + 2 Django-dependent slice tests (`test_request_intake_django`)
+ 59 pre-existing skeleton tests (settings, health, user model, API security, ...).
Those pre-existing tests need Django settings and so are not part of the standalone
command. The earlier figures (181 and 242) belong to the committed request-intake
revision, before the canonical module added 101 tests. Counts are only valid for the
commit they were run on, so re-run before quoting them. The 3 PostgreSQL-required tests are **not**
counted anywhere above and **have not been run**.

## Deferred (not implemented)

Until the owner's real PostgreSQL validation is complete and later phases begin:

- Django models, migrations, and persistence of drafts and stored revisions (revision
  identity and numbers, storing the content hash and its canonical format version).
- Approvals, rejections, and invalidation on edit, bound to a revision and its content
  hash; and export (and its execution-time re-check). The canonical bytes are an
  internal component, not an export artifact.
- The workflow state machine, tasks, and background execution.
- Document upload, storage, and file hashing.
- The AI gateway, data-policy enforcement, capability checks, and any local or
  external model adapter.
- Configuration versioning and snapshots (the limits here are plain arguments).
- API endpoints and any UI.
- Authorization and audit events.

## Unresolved questions

Recorded in [open-questions.md](../architecture/open-questions.md), items 2.6 to
2.14: unit-specific integer restrictions; how a quantity is written in the JSON
**export** (the canonical hashing form above is decided, the export format is not),
and whether the `float` conversion convention is the one to keep; text length limits
(no business limit is invented); the source-location matching rules;
source-reference conventions for extractors other than the fixture parser; handling
of stored findings after a contract version bump; how the extractor contract relates
to the AI gateway; whether the serialization safety refusals should also be enforced
in validation (they currently are not, by design); and whether the hash should be tied
to a result-schema version. Whether source references belong to the hashed content is
**decided** (they do, and a source text is then required).
