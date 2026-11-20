# AI Gateway and Data Policy

> **Status: foundation implemented (AI-1a); a first workflow use (AI-2a) is written, opt-in and not validated end to end.** The provider-neutral contracts, the three policy modes with enforcement
> before any provider call, a static provider registry, strict structured-output validation, stable error codes and a deterministic
> **test** adapter exist as a standalone, standard-library package (`backend/apps/ai_gateway`; see [ai-gateway.md](ai-gateway.md)). The
> unit tests are DB-free and pass in the development VM. **As of AI-1a nothing called the gateway** (since AI-2a only Request Intake's opt-in AI extractor does, see below): no module or API uses it, no
> credential store, per-company policy setting or administrator approval exists, and by default Request Intake still extracts with its own
> deterministic fixture parser. Confirmed directions: a provider-neutral gateway, a deterministic test adapter before any real provider,
> no silent fallback to cloud, and no automatic email sending or ERP writes in the first slice. Embeddings (section 5), credential
> handling (section 9), evaluation (section 11) and the rest of this document that ai-gateway.md does not describe remain **proposed**
> and unproven.
>
> **AI-1b: a first real adapter exists, untested against the real service.** The KPMG Workbench provider (`kpmg_workbench`, locality
> `company_endpoint`, deployments GPT-5 and GPT-4.1) is implemented to the client-side HTTP contract reported from the FI integration audit and
> tested with a mocked transport only; no request has ever been sent to it, and that contract is not proof of live compatibility (see
> [ai-gateway.md](ai-gateway.md), section 14, in particular 14.10). It is not registered in the production
> composition; as of AI-1b nothing called it, and since AI-2a only Request Intake's opt-in extractor builds it. Its key comes from a backend environment variable; the credential **store** (section 9) is still open.
>
> **AI-2a: Request Intake can use the gateway, only by explicit opt-in.** `AP_INTAKE_EXTRACTION_MODE=ai` makes the worker extract through the gateway with the `workbench-gpt5` profile; the default is still the deterministic fixture parser,
> and nothing switches between the two automatically. `ai` mode requires an explicit `AP_AI_POLICY_MODE` (no default; `local_only` is refused) and the Workbench key, and the worker refuses to start otherwise. The policy is enforced at start-up and again by the
> gateway on every call, before any provider code runs; the module's state is re-checked immediately before the text is sent; failures follow the gateway's retryable and permanent classes with no fallback; the answer is validated by the existing business
> rules and approved only by a person. Still **not** implemented: a stored company policy, an administrator's approval record, a credential store, an evaluation set and thresholds (section 11), and any measured accuracy. See
> [ai-gateway.md](ai-gateway.md), section 15. Sections 4, 6, 7, 11 and 12 below remain the policy this follows.
>
> **Stored AI settings (backend only; written, DB-free tested, PostgreSQL tests not run).** An administrator can store the extraction mode, provider, profile, data policy and call deadline through `PUT /api/ai-settings/extraction/` instead of environment variables; a stored row
> replaces the three mode, policy and timeout variables when the worker starts, and a change needs a worker restart (no hot reload). Each successful save records an `ai_settings.updated` audit event (actor, time, revision, mode, provider, profile, policy and timeout identifiers; no secret) in the same transaction (audit migration `0003`; its PostgreSQL tests are not run). This is still **not** the approved company policy of section 4: there is no approval record,
> the event keeps the new values only, audit immutability is ORM-level, and the key is still environment-only. See [ai-gateway.md](ai-gateway.md), section 16.
>
> **AI-CON-1: an OpenAI-compatible provider foundation (written, DB-free tested with a mocked transport; not run against any real server; opt-in; used by nothing).** It supports an explicit endpoint, model, optional key and one declared locality (`local`, `company_endpoint` or
> `cloud`) with no default and no fallback; the data policy is enforced by the gateway before the adapter runs, and destination rules (an explicit approved-origin list, a strict URL grammar, https for remote endpoints, plain http for loopback only, resolved-address checks against SSRF
> and DNS rebinding, no redirects, no implicit proxy) are enforced by the adapter and a guarded transport. A company endpoint reaches only operator-approved CIDR ranges (`AP_AI_OPENAI_COMPANY_CIDRS`, required, no default), an explicit denylist of special-purpose and metadata ranges
> applies to company and cloud endpoints, and a proxy is refused for host-name endpoints (a proxy would resolve names where no address rule can be applied). Request Intake, the AI settings catalog and the deterministic default are unchanged. See [ai-gateway.md](ai-gateway.md), section 17.
>
> **AI-CON-2: a native Ollama provider (written, DB-free tested with a mocked transport; not run against any real Ollama; opt-in; used by nothing).** The provider `ollama` calls Ollama's own `/api/chat` on THIS machine only: its locality is always `local` (a constant, not a setting), its endpoint must be `localhost` or a loopback literal and listed in an explicit approved-origin list, the model is named explicitly (no default, never downloaded, no other model tried), and there is no key, no proxy (a configured proxy is refused), no redirect and no fallback. `local_only` permits it, `company_endpoint` and `approved_cloud` permit it by the cumulative rule, and no cloud provider approval is involved. The guarded transport checks that every resolved address is loopback. Request Intake, the AI settings catalog, the Workbench and the OpenAI-compatible provider are unchanged. See [ai-gateway.md](ai-gateway.md), section 18.
>
> **Naming used by the code.** The third mode is `approved_cloud` with the locality `cloud` (this document first proposed
> `approved_external` and `external`; ADR 0004 still uses the old names). The error codes are `provider_unavailable`,
> `policy_violation`, `timeout`, `invalid_structured_output`, `rate_limit`, `authentication_failure`, `unsupported_model`,
> `configuration_error`, `capability_missing`, `invalid_request` and `provider_error` (this document's earlier `policy_denied` and
> `invalid_output` are `policy_violation` and `invalid_structured_output`).

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
  (Implemented: `GenerationRequest` and `PolicyContext`; the workflow identity is not part of the policy context yet.)
- **Output:** either structured data that conforms to the requested schema, or a
  typed failure (`policy_violation`, `capability_missing`, `provider_unavailable`,
  `invalid_structured_output`, `timeout`, and the other codes in [ai-gateway.md](ai-gateway.md), section 8), together with
  provenance (provider, reported model identifier, duration, token usage; parameters are in the profile).
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
   *AI-CON-1: an OpenAI-compatible adapter can serve this role for a server on loopback (`local`: plain http allowed, loopback only, key optional). It has never run against a real server, nothing selects it
   yet, and bundling or managing a local runtime is still undecided.* *AI-CON-2: a native Ollama adapter (`ollama`, `POST /api/chat`) is the dedicated local adapter: always `local`, loopback endpoint only, explicit approved origin and model, no key, no proxy, no
   redirect, no model download and no fallback. It has never run against a real Ollama and nothing selects it yet; it does not install, start or manage Ollama.*
3. **Company-endpoint adapter.** A model server operated by the
   company. *AI-1b: the KPMG Workbench adapter is this kind, classified `company_endpoint` by the owner; it refuses to run under `local_only`, may run under
   `company_endpoint`, and by the cumulative rule under `approved_cloud`. Written, not verified against the service. AI-CON-1: the OpenAI-compatible adapter can also be declared `company_endpoint`
   (https only, key required, and only addresses inside the CIDR ranges the operator approved in `AP_AI_OPENAI_COMPANY_CIDRS`, which has no default) or, for a public host, `cloud` (https only, key required, global addresses only, and the provider id must be named as approved).
   Special-purpose and metadata ranges are refused for both, whatever is approved, and a proxy is refused for host-name endpoints. A public cloud host therefore cannot become a company endpoint by declaration alone.
   The locality is always the operator's explicit declaration, cross-checked against the host and never inferred from a name.*
4. **External adapter (later, Phase 6).** A third-party hosted service, only
   under the approved-cloud mode.

The specific local runtime and external provider are open questions
([open-questions.md](open-questions.md)). None is selected.

## 4. Data-policy modes

Each company has exactly one policy mode, stored in `CompanyProfile` (**not
implemented**: AI-1a takes the mode as a `PolicyContext` argument of every call). Each
provider declares its **locality**, and a `ModelProfile` names its provider and the mode it
was approved for. The gateway permits a call only when the provider's locality is allowed
by both the profile's mode and the mode in force.

| Mode | Allowed locality | Meaning |
| --- | --- | --- |
| `local_only` | `local` | Data is processed only on the installation's own machine. |
| `company_endpoint` | `local`, `company_endpoint` | Data may also go to model servers the company itself operates. |
| `approved_cloud` | `local`, `company_endpoint`, `cloud` | Data may go to a third-party service, but only a provider an administrator has explicitly approved and recorded. |

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
  queued or retrying, the task fails with `policy_violation` (non-retryable)
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
- Output that fails validation is an attempt failure (`invalid_structured_output`). It is
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
- *As implemented in AI-1b (Workbench only):* the key is read from the backend process environment (`AP_AI_WORKBENCH_API_KEY`; for development, from the
  git-ignored `.env`) when the Workbench gateway is built, held only in the adapter's configuration object and sent only in the request's authentication
  header (`Ocp-Apim-Subscription-Key`, raw value); it is not a `ModelProfile` field, not in Django settings, logs, errors, provenance or any API response. A missing key fails
  closed, and the endpoint host is allowlisted so the key cannot be sent to another destination. This is an interim mechanism, not the decided credential store; the proxy and
  TLS rules are in [ai-gateway.md](ai-gateway.md), section 14.5.
- *As implemented in AI-CON-1 (OpenAI-compatible provider):* the same interim approach with its own variable (`AP_AI_OPENAI_API_KEY`), sent only as `Authorization: Bearer <key>`. It is optional for a `local` endpoint and required for
  `company_endpoint` and `cloud` (fail closed). The endpoint is not a secret but is still never stored in a `ModelProfile`; it must be approved separately in an explicit origin list, a company endpoint must also have its approved CIDR ranges, and a transport refuses to send to a resolved
  address its locality may not reach (loopback-only, approved-ranges-only, global-only, and never a special-purpose or metadata range), to a redirect target, through a proxy for a host name, or in plain text unless the endpoint is on loopback. See [ai-gateway.md](ai-gateway.md), section 17.4.
- *As implemented in AI-CON-2 (native Ollama provider):* there is no credential at all (Ollama's native API takes no key here and the adapter sends no `Authorization` header), so nothing secret is configured, stored or logged. The endpoint is still approved explicitly, must be loopback, and is never stored in a `ModelProfile`. See [ai-gateway.md](ai-gateway.md), section 18.4.

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
- *As implemented in AI-1a:* the **provider** declares its capabilities (models, structured-output modes, token limits, generation
  parameters) and the gateway checks each profile against them when it is built (`unsupported_model`, `capability_missing`). Images,
  languages and demonstrating a capability at run time are not implemented.

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
