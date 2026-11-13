# First Workflow: Request Document to Approved Structured Draft

> **Status: The workflow is not implemented.** Only a small database-independent
> preparation slice exists (the result schema, pure validation, and a
> deterministic test extractor, as plain Python; see
> [request-intake-slice.md](../development/request-intake-slice.md)). A first
> orchestration slice also exists (text submission, a queued task, extraction by the worker, an outcome,
> revision 1 for a valid draft, the run states `queued`, `extracting`, `needs_review` and `failed`; see
> [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md); its PostgreSQL
> tests and a smoke test passed in the owner's local run, for the tested scenarios only). A review-services slice
> adds authorized edit, approve and reject services and audit events (see
> [review-services-slice.md](../development/review-services-slice.md); its PostgreSQL tests are written but not
> run). Export, cancellation, retry, upload, storage, an API, and a login mechanism are not implemented. This
> specification defines the first vertical
> slice. Confirmed directions: plain-text input, JSON export, a deterministic
> test AI adapter, and no automatic email sending or ERP writes (PDF parsing and
> CSV export are deferred). The detailed rules, states, and acceptance
> criteria are proposals that are normative for this design but untested.

## 1. Purpose

The first slice exercises every core boundary with the smallest useful
workflow:

**document upload -> extraction -> validation -> human review and approval -> export**

Concrete example: a user uploads a **request document** (for example a written
request for items). The system converts it into a **structured draft** with
the requester, the requested items, their quantities, and notes. A human
reviews and approves a specific revision of that draft, and only that approved
revision can be exported.

Scope limits for this slice:

- No pricing, availability, stock, or delivery promises appear in the draft or
  export. The workflow records what was *requested*, nothing more.
- No automatic email sending, ERP writes, payments, or any other consequential
  external action. Export produces a file in managed storage that an
  authorized user can download.
- Extraction uses a **deterministic test AI adapter** only. Real local and
  external adapters come later ([ai-policy.md](ai-policy.md)).

Terminology used here (`Case`, `WorkflowRun`, `ResultRevision`, `Approval`,
`ExportArtifact`, `TaskAttempt`) is defined in [data-model.md](data-model.md).

## 2. Inputs

| Input | Description |
| --- | --- |
| Request document | One file uploaded by an authorized user into a `Case` |
| Company configuration | Validated values exposed by this workflow package (section 9), snapshotted at run start |
| Workflow definition | The versioned workflow package definition |

### Supported formats (confirmed direction)

| Format | Status |
| --- | --- |
| UTF-8 plain text (`.txt`) | **Supported. This is the only input format in this slice.** |
| PDF (including PDFs with a text layer) | **Deferred.** No PDF parsing in this slice; a PDF upload is rejected. |
| Scanned images, OCR, Office formats, email files, archives | Not supported in this slice |

Upload handling (proposed details):

- Content must be valid UTF-8 text. Content that is not valid text, or is
  empty, is rejected. File type is determined from content, not only the
  filename extension.
- A maximum file size is enforced (value to be decided; see open questions).
- A SHA-256 hash of the stored bytes is recorded.
- The original file is stored unchanged and is never modified afterwards.
- An unsupported or unreadable file is rejected at upload with a clear error
  and creates no workflow run.

## 3. Outputs

1. **Result revisions.** Immutable structured drafts (`ResultRevision`). The
   first is produced by extraction; each human edit produces a new revision.
2. **Validation findings** attached to each revision.
3. **Approval record** tied to one specific revision.
4. **Export artifact.** A file generated from the approved revision, stored in
   managed storage. The export format for this slice is **JSON** (confirmed
   direction). **CSV export is deferred.**
5. **Audit events** for every significant step.

## 4. Result schema

The result schema belongs to the workflow package and is versioned with it.

| Field | Required | Notes |
| --- | --- | --- |
| `requester.name` | Required | Person or party making the request, as stated in the document |
| `requester.contact` | Optional | Free text contact detail if present in the document |
| `items` | Required, at least one | List of requested items |
| `items[].description` | Required | What is requested, as stated |
| `items[].quantity` | Required | Positive number as stated. Fractions are allowed in this generic schema; the canonical validated type is `Decimal`, kept exactly as given (no rounding, no precision limit). Booleans, NaN, infinity, zero, and negative values are invalid. Unit-specific integer restrictions are deferred. |
| `items[].unit` | Optional | Unit text if stated (for example "boxes"); not normalized in this slice |
| `items[].notes` | Optional | Item-level remarks |
| `notes` | Optional | Request-level remarks |
| `source_refs` | Optional | Per field, a short excerpt or location from the document that supports the value |

The schema contains **no** price, currency, stock level, availability, or
delivery-date fields.

## 5. Missing-data behavior

The system must **not invent missing information**.

- A value that cannot be found in the document is stored as `null` (or an
  empty list for `items`), never guessed or filled with a default.
- Each missing required value produces a **blocking** finding
  (`missing_required`) on the revision.
- An ambiguous value (for example a quantity written as "a few") is stored as
  `null` with a finding that quotes the source excerpt. The finding is
  **blocking** (`ambiguous_required_value`) when the field is required and a
  **warning** (`ambiguous_optional_value`) when it is optional. A required
  field's ambiguity blocks approval even if the field still holds a value, and it
  never replaces the `missing_required` finding for a missing required value.
- (Implementation note: the orchestration slice saves a draft with blocking findings as an `ExtractionOutcome`
  with status `invalid`, not as a `ResultRevision`, because a revision cannot hold blocking findings. The
  review-services slice likewise REFUSES an edit that has blocking findings (returning the findings and storing
  nothing) and offers a first edit that creates revision 1 from an invalid outcome; see
  [review-services-slice.md](../development/review-services-slice.md), decision P9. The next sentence describes
  the design, not the current code.)
- A draft with blocking findings is still saved as a revision so a human can
  see it and complete it. It cannot be approved until the blocking findings are
  resolved in a newer revision.
- Optional fields that are absent produce no finding.

## 6. Validation rules

Validation is deterministic domain code (no AI involved). It runs after
extraction and again on every human edit, using the same code in both cases.
Findings have a severity: **blocking** (prevents approval) or **warning**
(shown to the reviewer; does not prevent approval).

| Rule | Severity |
| --- | --- |
| Result conforms to the schema version of the workflow definition (types, required fields present) | Blocking |
| `requester.name` is non-empty after trimming whitespace | Blocking |
| `items` contains at least one item | Blocking |
| Every item has a non-empty `description` | Blocking |
| Every `quantity` is a finite number (not a boolean) greater than zero | Blocking |
| `quantity` does not exceed the configured maximum (if configured) | Blocking |
| Text fields do not exceed configured length limits (if configured) | Blocking |
| An extractor-reported ambiguity affects a required field | Blocking |
| A source reference does not match the supplied source text (unknown field, missing item, line outside the text, or excerpt differing from the cited line) | Blocking |
| Two items have identical normalized descriptions (never merged, deleted, or summed) | Warning |
| An extractor-reported ambiguity affects an optional field | Warning |
| An extracted value cannot be located in the source text (simple deterministic check, applies to text sources) | Warning |

Numeric limits and length limits are company configuration values within
ranges the package allows. This document proposes no specific numbers, and no
business limit is invented: a limit that is not configured is not applied.

Finding codes, severities, and field paths are the **initial stable contract,
version 1**. An incompatible change (renaming or removing a code, changing a
severity, or changing the path grammar) requires a version bump. The
database-independent preparation slice implements these rules as plain Python
(see [request-intake-slice.md](../development/request-intake-slice.md)); how
they are applied to stored revisions is not implemented.

## 7. Human review and approval

A revision with **no blocking findings** is called **valid**; a revision with
one or more blocking findings is **invalid**.

1. After extraction, the run waits in `needs_review` with revision 1 and its
   findings visible. Revision 1 may be invalid (for example, missing
   requester); this is a normal outcome, not a run failure.
2. An authorized user may **edit** the draft, but only while the run is in
   `needs_review`, `approved`, or `exporting`. Saving an edit never changes an
   existing revision: it creates a **new revision** (revision number + 1) with
   the editing user recorded, re-runs validation, and stores new findings. The
   new revision may itself be invalid; it is saved anyway so the user can
   continue working.
3. An authorized approver may **approve** or **reject** a specific revision of
   a run in `needs_review`.
   - Approval is recorded against that revision's identifier **and content
     hash**.
   - Approval of an invalid revision is refused.
   - Rejection records a reason and leaves the run in `needs_review`.
4. Which users may edit and approve is decided by roles (see
   [data-model.md](data-model.md)). Whether the same person may both edit and
   approve is an open question.

### Approval and export rules

(A database-independent, pure-Python version of rules 1, 3, 5 and 6 exists in
[revision-approval-policy.md](../development/revision-approval-policy.md), scoped to the
case, the run, the revision, and its content hash; nothing is persisted, authenticated, or
enforced at commit time yet.)

These rules are normative for the whole architecture. Other documents must not
contradict them.

1. **Approval authorizes exactly one immutable result revision.** It is
   recorded against a revision id and that revision's content hash. A
   revision never changes after it is created.
2. **Editing creates a new revision.** An edit never modifies an existing
   revision or an existing approval.
3. **A previous approval does not authorize the new revision.** After an edit,
   the new revision has no approval until an approver approves it. The older
   approval simply no longer applies to the current revision; this is derived
   from the revision relationship, and the approval row is not modified. The
   run returns from `approved` to `needs_review`, and an `approval.invalidated`
   audit event records the consequence.
4. **Historical approvals are preserved.** Approvals, rejections, and all
   earlier revisions are kept permanently as history, including those that no
   longer apply to the current revision.
5. **Invalid results cannot be approved or exported.** Approval is refused for
   a revision with blocking findings. Export is refused unless the current
   revision has an effective approval. Because revisions are immutable and
   approval requires validity, an approved revision is valid.
6. **Export checks the approved revision at execution time.** The check is
   made when the export is requested and **again by the worker when it
   commits the export result**. If the current revision is no longer the
   approved revision at that moment, the result is discarded and no
   `ExportArtifact` is created for it.

An approval is **effective** when all of these hold: its decision is
`approved`; its revision is the run's current revision; its recorded content
hash equals that revision's content hash; and the revision is valid.

## 8. Export

- Export is permitted **only for the approved revision**, meaning the current
  revision has an effective approval (see the rules above).
- The export request creates an export task. The worker **re-checks** the
  approval condition when it commits the result, so an edit that arrives while
  the export is running cannot produce an export of an unapproved revision.
- The `ExportArtifact` records the revision, the approval, the format, a file
  hash, and the storage reference.
- Exporting the same revision in the same format twice returns the existing
  artifact rather than creating a duplicate.
- Export writes to managed storage only. Nothing is sent anywhere: no email,
  no ERP write, no external system.

## 9. Company configuration for this workflow (proposed)

Only values the package declares, for example:

- maximum quantity per item,
- maximum text lengths,
- whether `requester.contact` is required,
- the company AI data-policy mode and the allowed model profile for extraction.

Configuration is validated on save, versioned, and snapshotted per run.

## 10. Failure and cancellation behavior

**Failure**

- Each task is retried a bounded number of times with backoff
  ([background-execution.md](background-execution.md)). When retries are
  exhausted, the run moves to `failed` and records which step failed
  (`failed_step`: `extract` or `export`).
- Model output that does not conform to the structured-output schema counts as
  an attempt failure. It is never silently repaired into a result.
- A failure never deletes the uploaded document or earlier revisions.
- An authorized user may **retry** a failed run. A failed `extract` returns the
  run to `queued`. A failed `export` returns the run to `approved`, provided
  the approval is still effective for the current revision. A retry creates a
  **new task** (a new retry generation) for the failed step; it does not
  reopen the failed task. Earlier tasks and their attempts remain as history
  ([background-execution.md](background-execution.md)).
- Editing is not permitted in `failed`. A user retries or cancels first.
- A run's task failure never invalidates a revision or an approval by itself.

**Cancellation**

- An authorized user may cancel a run in `queued`, `extracting`,
  `needs_review`, `approved`, `exporting`, or `failed`.
- Cancellation is cooperative for running tasks: a result arriving after
  cancellation is discarded and is not turned into a revision or export.
  If cancellation and task completion race, whichever transition is committed
  first wins, because both are conditional on the run's current state.
- Cancelled is terminal. The original document, existing revisions, and audit
  history are kept.
- `exported` is terminal and cannot be cancelled.

**Corrections after export.** Exported results are not edited. A correction
starts a new `WorkflowRun` in the same `Case`. (Whether to add a "reopen"
action is an open question.)

## 11. Proposed workflow state machine

> **Proposed; partly implemented.** State names here are the canonical names for
> `WorkflowRun.state` throughout the architecture documents. The orchestration slice stores the state and
> performs `(new) -> queued`, `queued -> extracting`, `extracting -> needs_review` and the failure transitions
> only (PostgreSQL tests not run). It differs from this table in two documented ways: a failed attempt that the
> queue retries does **not** move the run back to `queued` (the run stays `extracting`, and `extracting ->
> extracting` is allowed), and `queued -> failed` is allowed because a task can fail for good before its handler
> moved the run. See [request-intake-orchestration-slice.md](../development/request-intake-orchestration-slice.md).

States:

| State | Meaning |
| --- | --- |
| `queued` | Run created; extraction task waiting or being retried |
| `extracting` | Extraction task is executing (includes deterministic validation of the produced draft) |
| `needs_review` | A current revision exists and awaits human review, editing, or approval |
| `approved` | The current revision has an effective approval |
| `exporting` | Export task is executing |
| `exported` | Export artifact exists for the approved revision (terminal) |
| `failed` | A step exhausted its retries; `failed_step` records which |
| `cancelled` | Run cancelled (terminal) |

Transitions:

| From | Event | To |
| --- | --- | --- |
| (none) | Document accepted, run created | `queued` |
| `queued` | Extraction task claimed | `extracting` |
| `extracting` | Extraction and validation committed as revision 1 | `needs_review` |
| `extracting` | Attempt fails with retries remaining | `queued` (task waits for backoff) |
| `extracting` | Retries exhausted | `failed` (`failed_step = extract`) |
| `needs_review` | Human edit saved | `needs_review` (new revision; may be valid or invalid) |
| `needs_review` | Approval of current revision (valid: no blocking findings) | `approved` |
| `needs_review` | Approval attempted on an invalid revision | refused; stays `needs_review` |
| `needs_review` | Rejection recorded | `needs_review` |
| `approved` | Human edit saved | `needs_review` (new revision; earlier approval no longer applies) |
| `approved` | Export requested | `exporting` |
| `exporting` | Artifact committed, approval still effective | `exported` |
| `exporting` | Human edit saved | `needs_review` (new revision; export task cancel requested; any late export result discarded) |
| `exporting` | Approval no longer effective at commit | `needs_review` (result discarded) |
| `exporting` | Retries exhausted | `failed` (`failed_step = export`) |
| `failed` | Retry requested, `failed_step = extract` | `queued` |
| `failed` | Retry requested, `failed_step = export`, approval effective | `approved` |
| `queued`, `extracting`, `needs_review`, `approved`, `exporting`, `failed` | Cancellation | `cancelled` |

Any transition not listed is invalid and must be rejected by the domain layer.
In particular, editing is rejected in `queued`, `extracting`, `failed`,
`exported`, and `cancelled`; approval is rejected outside `needs_review`.

> **Implementation note (review-services slice).** `edit_request` allows `needs_review` and `approved` and
> refuses `exporting` for now (export and its cancellation protocol do not exist yet); approval and rejection
> require `needs_review`; a rejection leaves the run in `needs_review` and the rejected revision cannot then be
> approved; self-approval is forbidden. See [review-services-slice.md](../development/review-services-slice.md),
> section 2.

Validation is a stage, not a separate state, because it is deterministic and
runs inside the extraction commit and each edit. If validation later becomes
long-running, a `validating` state can be introduced.

## 12. Acceptance criteria

The slice is accepted when automated tests demonstrate all of the following
using the deterministic test adapter:

1. **AC-1** A supported document upload creates a `Case`, a `Document` with
   recorded SHA-256 and storage reference, a `WorkflowRun` in `queued`, and an
   extraction task.
2. **AC-2** An unsupported (including a PDF), empty, non-UTF-8, or oversized
   file is rejected and creates no run.
3. **AC-3** The worker, as a separate process, claims the task and produces
   revision 1; the run ends in `needs_review`.
4. **AC-4** For a document missing the requester or quantities, the draft
   contains `null` values and blocking findings, and nothing is invented.
5. **AC-5** Each validation rule in section 6 has a passing and a failing test.
6. **AC-6** Saving an edit creates a new revision and leaves earlier revisions
   byte-for-byte unchanged.
7. **AC-7** Approval of a revision with blocking findings is refused.
8. **AC-8** Approval is recorded against a specific revision id and content
   hash.
9. **AC-9** Editing an approved result creates a new revision, moves the run to
   `needs_review`, leaves the earlier approval row unchanged and preserved, no
   longer treats it as effective for the new revision, and records an
   `approval.invalidated` audit event.
10. **AC-10** Export is refused unless the current revision has an effective
    approval, and the check is repeated when the export result is committed.
11. **AC-11** Exporting the same revision and format twice yields one artifact.
12. **AC-12** Killing the worker mid-task leads to recovery after lease expiry
    without a duplicate revision.
13. **AC-13** Retry exhaustion produces `failed` with the correct
    `failed_step`; a manual retry follows the transitions in section 11.
14. **AC-14** Cancellation at each cancellable state follows section 10 and
    leaves documents, revisions, and audit history intact.
15. **AC-15** Every transition in section 11 is covered by a test, and a
    sample of invalid transitions is rejected.
16. **AC-16** The audit history contains an event for upload, extraction,
    each revision, approval, invalidation, export, failure, and cancellation,
    without document text or extracted values in the event details.
17. **AC-17** No network call to any AI provider or external system is made
    during the slice.
18. **AC-18** Users without the required role cannot upload, edit, approve, or
    export.
19. **AC-19** An edit saved while the run is `exporting` produces a new
    revision, returns the run to `needs_review`, and no `ExportArtifact` is
    created for the earlier revision after that point.
20. **AC-20** An invalid revision can be neither approved nor exported, and an
    approval cannot be created for a revision other than the current one.
21. **AC-21** After any number of edits, every earlier revision and every
    earlier approval and rejection is still retrievable and unchanged.
22. **AC-22** A manual retry after `failed` creates a new task and leaves the
    failed task and its attempts intact.

## 13. Out of scope for this slice

Pricing, inventory, ERP writes, automatic email sending, payments, PDF
parsing, CSV export, OCR, real AI adapters, semantic retrieval,
multi-document requests, per-item approvals, and multi-step approval chains.
