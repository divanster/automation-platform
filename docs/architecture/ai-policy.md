# AI Gateway and Data Policy

> **Status: Not implemented.** No gateway, adapter, or policy enforcement
> exists. Confirmed directions: a provider-neutral gateway, a deterministic
> test adapter before any real provider, no silent fallback to cloud, and no
> automatic email sending or ERP writes in the first slice. The exact policy
> modes, capability checks, and evaluation procedure below are **proposed**
> and remain unproven until implemented and tested.

## 1. Goals

- Let workflows use AI without being tied to one provider.
- Let each company decide where its data may be processed, and enforce that
  decision in code.
- Treat model output as untrusted until validated, and keep humans in control of
  consequential results.

See [ADR 0004](../decisions/0004-provider-neutral-ai-gateway.md).

## 2. Provider-neutral interface

All model use goes through one **AI gateway**. Workflow code never calls a
provider directly.

Conceptual contract (illustrative, not final):

- **Input:** a task description (for example "extract structured fields"), the
  input text, the expected output schema, a `ModelProfile` reference, and a
  policy context (the company data-policy mode and the workflow identity).
- **Output:** either structured data that conforms to the requested schema, or a
  typed failure (`policy_denied`, `capability_missing`, `provider_unavailable`,
  `invalid_output`, `timeout`), together with provenance (adapter, reported
  model identifier, parameters, duration).
- **Adapters** implement the contract for one provider kind. The gateway owns
  policy checks, schema validation, timeouts, and provenance recording so that
  adapters stay thin.

The gateway does not depend on business workflows
([overview.md](overview.md), section 5).

## 3. Adapter order

1. **Deterministic test adapter (first).** Returns predetermined, repeatable
   output derived from its input (for example a fixture lookup by content hash,
   or a simple rule-based parse of a fixed test template). It makes no network
   calls, needs no credentials, and has locality `local`. It exists so the whole
   workflow, including failure paths, can be tested without a real model.
2. **Local adapter (later, Phase 6).** A model running on the same machine.
3. **Company-endpoint adapter (later, Phase 6).** A model server operated by the
   company.
4. **External adapter (later, Phase 6).** A third-party hosted service, only
   under the approved-external mode.

The specific local runtime and external provider are open questions
([open-questions.md](open-questions.md)). None is selected.

## 4. Data-policy modes

Each company has exactly one policy mode, stored in `CompanyProfile`. Each
`ModelProfile` declares its **locality**. The gateway permits a call only when
the profile's locality is allowed by the mode.

| Mode | Allowed locality | Meaning |
| --- | --- | --- |
| `local_only` | `local` | Data is processed only on the installation's own machine. |
| `company_endpoint` | `local`, `company_endpoint` | Data may also go to model servers the company itself operates. |
| `approved_external` | `local`, `company_endpoint`, `external` | Data may go to a third-party service, but only a provider an administrator has explicitly approved and recorded. |

Rules:

- The default mode for a new installation is `local_only`.
- Changing the mode, or approving an external provider, is an administrator
  action and is recorded in the audit history.
- **No silent cloud fallback.** If the configured provider is unavailable, the
  call fails with `provider_unavailable`. The gateway never retries against a
  provider of a less restrictive locality. Any fallback must be an explicitly
  configured alternative within the *already allowed* localities, and its use is
  recorded in the task attempt.
- Policy is enforced in the gateway, in the worker's code path, on every call,
  not only in a settings screen.
- **Policy is evaluated at execution time against the current mode**, not
  against the mode recorded in a run's execution snapshot. The snapshot records
  what was used for explainability; it does not grant permission. If an
  administrator tightens the mode (for example to `local_only`) while a task is
  queued or retrying, the task fails with `policy_denied` (non-retryable)
  instead of calling a now-disallowed provider.
- A mode change affects future calls. Already-recorded results keep their
  provenance.

## 5. Embeddings

Embeddings are model calls and follow the **same data-policy modes**, the same
locality checks, and the same no-silent-fallback rule. Embedding text is not
exempt because it is "only vectors": the text still leaves the machine. Semantic
retrieval (and pgvector) is optional and not required by the first workflow
([ADR 0002](../decisions/0002-postgresql-primary-storage.md)).

## 6. Structured-output validation

- Every request that expects structure carries a schema owned by the workflow
  package.
- The gateway validates the model output against the schema before returning it.
- Output that fails validation is an attempt failure (`invalid_output`). It is
  **never** silently repaired, truncated into shape, or filled with defaults.
- Passing schema validation does not make the content true. Deterministic
  business-rule validation and human review still apply
  ([first-workflow.md](first-workflow.md)).

## 7. Documents are untrusted data

- Document text is **data to be processed, not instructions to follow.** Text in
  a document that tries to instruct the model (for example "ignore previous
  instructions") must not change system behavior.
- Prompts are built by the workflow package. Document content is delimited and
  labeled as data.
- The model can only **return a value**. Its output cannot trigger an action by
  itself; the domain layer decides what to do with validated output.
- Prompt injection cannot be fully eliminated. The design therefore limits
  consequences: no tools, schema validation, deterministic checks, and human
  approval before anything consequential.

## 8. No tool access for the model

The model has **no arbitrary shell access, no filesystem access, and no
arbitrary API access**. It receives text and returns structured data. If tool
use is ever introduced it requires a separate reviewed design, an allow-list of
specific capabilities, and human approval for consequential actions.

## 9. Credentials

- Provider credentials are stored **outside source control** and are never sent
  to the frontend, written to logs, or placed in audit `details`.
- A `ModelProfile` holds only a *reference* to a credential, not the secret.
- The storage mechanism for credentials on Windows is an open question.
- Only the worker and gateway code paths that need a credential can resolve it.

## 10. Capability checks

Providers and models do not behave identically. The gateway must not assume
that they do.

- Each `ModelProfile` declares capabilities relevant to the product (for
  example support for structured output, maximum input size, supported
  languages, and whether images are accepted). Declarations are checked against
  what the provider actually reports or demonstrates where possible.
- Before a call, the gateway verifies that the profile supports what the task
  needs. A missing capability yields `capability_missing`, not a degraded
  guess.
- Input that exceeds a profile's limits fails explicitly; it is not silently
  truncated.

## 11. Model changes require evaluation

Changing the model, provider, adapter, prompt template, or key parameters for a
workflow can change results. Therefore:

- A fixed **evaluation set** of representative, non-customer or consented
  documents with expected results is kept for each workflow. Its content and
  storage must not include real customer data unless authorized.
- A candidate profile is evaluated against it before becoming the active profile
  for a workflow.
- The evaluation outcome and the decision are recorded and linked to the
  `ModelProfile` version.
- Runs record which profile version produced them
  ([data-model.md](data-model.md)).
- Acceptance thresholds are not proposed here; they are defined per workflow
  when real adapters are introduced.

## 12. Missing information must not be invented

Prompts and validation must require the model to return `null` or an explicit
"not found" for anything not present in the document. Validation checks (such as
verifying that extracted values can be located in the source where
applicable) reduce, but do not remove, the risk of fabricated values. Missing
required data becomes a blocking finding that a human resolves, not a guess
([first-workflow.md](first-workflow.md), section 5).

## 13. Logging and data minimization

- By default, prompts, document text, and raw model output are not written to
  logs or audit events. Attempt records keep metadata and provenance only.
- Any debug capture of content must be explicit, time-limited, and subject to
  the same access controls as the documents themselves.

## 14. What is not decided

The local runtime, the external provider, the credential store, evaluation
thresholds, prompt-injection test corpus, and whether local models are bundled
with the installer. See [open-questions.md](open-questions.md).
