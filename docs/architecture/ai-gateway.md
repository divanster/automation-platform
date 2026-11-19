# AI Gateway Foundation (AI-1a)

> **Status: implemented as a foundation; no real AI.** AI-1a adds provider-neutral contracts, data-locality policy
> enforcement, a static provider registry and a deterministic **test** provider, in the pure standard-library package
> `backend/apps/ai_gateway` with a composition root `backend/config/ai_gateway.py` whose built-in tuples are **empty**. Nothing in
> the application calls it: Request Intake still extracts with its own deterministic fixture parser, no real provider, credential,
> network call, setting, endpoint, screen or database table exists, and no module can use it (`ai.invoke` is still a *future*
> capability). Its DB-free tests pass in the development VM (section 13). It is a foundation to review, not a working AI feature, and it
> makes no production-readiness claim. The policy rules are those proposed in [ai-policy.md](ai-policy.md); this slice implements them
> in code and renames one mode (section 6).

## 1. Scope

In scope: the contract between a caller and a provider; immutable model profiles; the three data-locality policy modes and their
enforcement before any provider code runs; a static allowlisted provider registry; stable error codes; strict structured-output
validation; a deterministic test provider.

Not in scope (later slices, section 11): a real local, company-endpoint or cloud provider; credentials and their storage; the
per-company policy setting; the administrator's cloud-provider approval; a module capability facade; prompt building; evaluation;
Request Intake integration; any API route or screen.

## 2. Where it sits

Inspected before writing it: `apps/modules` and `apps/module_host` (manifest, registry, `ModuleState`, `ModuleContext`),
`apps/intake` and `workflows/request_intake` (the extraction contract), `config/settings/*`, `config/env.py`, the task registry and
the existing boundary tests. The boundaries that matter:

| Existing piece | What AI-1a does with it |
| --- | --- |
| `workflows.request_intake.RequestExtractor` (text in, candidate out) | Untouched. It is the *workflow's* extraction seam; a later slice may implement it on top of the gateway. Request Intake's extraction behaviour is unchanged. |
| `apps/modules` (`ai.invoke` in `FUTURE_CAPABILITIES`) | Untouched. A module cannot declare `ai.invoke` yet; pinned by a test. No `ModuleContext` facade. |
| Static composition roots (`config/modules.py`, `config/task_registry.py`) | Followed: providers are named in one static tuple in `config/ai_gateway.py`, nothing is discovered. |
| `apps/modules` as a pure, non-Django package | Followed: `apps/ai_gateway` is not an installed app, has no model, migration, URL or command, and imports no Django. |
| `config/env.py` and settings | **No setting or environment variable was added.** There is no secret, endpoint or switch to configure yet; the policy mode is passed per call. |
| `apps/audit` | Not used. The gateway returns provenance metadata; recording it is for the caller. |

No new Core service was invented: the gateway is a library the worker or a workflow will call, not a service.

## 3. Package layout

```text
backend/apps/ai_gateway/          pure standard library; no Django; not an installed app
    errors.py        stable error codes, fixed messages, retry classes
    jsonvalue.py     bounded, immutable JSON; canonical JSON; the token estimate
    policy.py        PolicyMode, DataLocality, PolicyContext, enforce_policy
    structured.py    the closed JSON Schema subset and its validator
    contracts.py     ModelProfile, ProviderCapabilities/Descriptor, GenerationRequest/Response, Provenance
    provider.py      AIProvider (adapter base class), ProviderCall, ProviderResult
    registry.py      ProviderRegistry (static, immutable)
    gateway.py       AIGateway (policy, call, validation)
    adapters/deterministic.py   the deterministic TEST provider (not re-exported by the package)
backend/config/ai_gateway.py      composition root: BUILTIN_PROVIDERS = (), BUILTIN_PROFILES = ()
```

Adapters import only the contract above them and the standard library; the gateway core never imports an adapter; no business module,
workflow package, Core app or `config` module other than `config/ai_gateway.py` imports the package (all pinned by
`tests/test_ai_gateway_boundaries.py`).

## 4. Provider contract

An adapter subclasses `AIProvider` and provides:

- `descriptor`: a `ProviderDescriptor` (provider id, a short `kind` label, **locality**, and `ProviderCapabilities`). Read **once**, when
  the provider is registered; the registry keeps that snapshot.
- `generate(call) -> ProviderResult`: receives a `ProviderCall` (model id, instructions, input text, output schema, structured-output
  mode, timeout, output-token limit, generation parameters) and returns the model's **raw text** plus the reported model id and token
  usage, or raises `AIGatewayError` with one of the adapter codes (section 8). The call holds **no policy, company, workflow or
  credential information**.

`ProviderCapabilities` declare the supported model ids, structured-output modes (`native_schema`, `json_mode`, `prompt_only`), maximum
input and output tokens, the generation parameters the provider honours (`temperature`, `top_p`, `seed`), and `test_only`.

**Data locality declaration.** Every provider declares where it processes data: `local` (inside this installation), `company_endpoint`
(a server the company operates) or `cloud` (a third-party service).

An adapter must honour `timeout_seconds` with the provider's own timeout; the gateway cannot interrupt a running call (section 12).

## 5. Request, response and model profiles

`GenerationRequest` carries a profile id, `instructions`, `input_text` (data, never instructions), an output schema and optional,
lower-only `max_output_tokens` and `timeout_seconds`. It is validated and frozen when built; instructions, input and schema are
excluded from `repr`.

`GenerationResponse` carries the **validated** output (an immutable mapping, excluded from `repr`) and a `Provenance`: profile,
provider, kind, locality, the policy mode in force, requested and *reported* model id, `test_only`, duration, and reported token usage.
Provenance holds no prompt, input, output or credential.

**`ModelProfile`** is immutable and self-validating:

| Field | Meaning |
| --- | --- |
| `profile_id`, `provider_id` | Lowercase identifiers (`[a-z][a-z0-9_-]{0,63}`) |
| `model_id` | Provider's model name (`[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}`) |
| `policy_mode` | The mode the profile was approved for; its provider's locality must be allowed by it (checked at start-up) |
| `timeout_seconds` | Greater than 0, at most 300 |
| `limits` | `max_input_tokens`, `max_output_tokens` (bounded positive integers) |
| `structured_output` | The mode used to ask for structure |
| `parameters` | Optional `temperature` (0 to 2), `top_p` (above 0, at most 1), `seed`; `None` means the provider default |

A profile holds **no API key, credential, credential reference, endpoint or URL** (a test pins this by field name and by refusing extra
fields). There is no database model: profiles are static objects in the composition root. Credential references arrive with the first
provider that needs one.

## 6. Policy modes and data-locality enforcement

| Mode | Allowed localities |
| --- | --- |
| `local_only` | `local` |
| `company_endpoint` | `local`, `company_endpoint` |
| `approved_cloud` | `local`, `company_endpoint`, `cloud` (a cloud provider must also be named in `PolicyContext.approved_cloud_providers`) |

**Owner decisions (confirmed at the AI-1a review):** the policies are cumulative (`local_only` < `company_endpoint` < `approved_cloud`); a
cloud provider needs an explicit entry in `approved_cloud_providers`; the policy mode is an explicit argument of every call; there is no
automatic provider or model fallback.

**Naming.** [ai-policy.md](ai-policy.md) and [ADR 0004](../decisions/0004-provider-neutral-ai-gateway.md) proposed the third mode as
`approved_external` with the locality `external`; AI-1a uses `approved_cloud` and `cloud`, as requested for this slice. The semantics are
the same. The ADR still carries the old name; renaming it is a documentation decision for the owner.

Rules, each pinned by tests:

- `local_only` never calls a company endpoint or cloud provider, even one an administrator approved.
- **Enforced before any provider code runs**, on every call, against the `PolicyContext` passed to *that* call: tightening the mode
  between two calls takes effect on the next one. A call with no `PolicyContext`, or an invalid mode, fails with `configuration_error`
  (there is no default mode).
- The locality must be allowed by **both** the profile's mode and the mode in force.
- **No silent fallback.** The gateway calls the one provider the profile names or fails; it never tries another, more or less
  restrictive. The gateway has no fallback, retry or default-provider concept at all.
- A profile whose own mode does not allow its provider's locality cannot be configured (`configuration_error`).
- Fail closed: anything missing or invalid is an error, never a default.

## 7. The call

`AIGateway(registry, profiles).generate(request, policy=PolicyContext(...))`:

1. the request type and the profile; 2. **policy**; 3. limits (a per-call value may only lower the profile's; input over the
profile's token limit is refused, never truncated; the estimate is UTF-8 bytes divided by three, rounded up, a guard and not a
tokenizer); 4. the provider call; 5. result checks: failure mapping, late answer (`timeout`), the output limit, strict JSON (no NaN, no
duplicate keys, a bounded depth), a JSON object, and the schema. Nothing is repaired, filled in or cut to fit.

**Start-up checks** (construction): every profile names a registered provider; the model, structured-output mode, token limits and
parameters are within the provider's declared capabilities (`unsupported_model`, `capability_missing`); a test-only provider is refused
unless `allow_test_providers=True`.

**Structured output** uses a closed JSON Schema subset (`type` is required everywhere; `properties`, `required`,
`additionalProperties` as a boolean, `items`, `enum`, length, item-count and numeric bounds, `title`, `description`; the root is an
object). A schema using any other keyword is refused rather than ignored; `pattern` and `$ref` are deliberately absent. Violations are
reported as `path: rule` built from schema names only. Valid against the schema does **not** make the content true: business-rule
validation and human review still apply ([ai-policy.md](ai-policy.md), section 6).

## 8. Error codes

Every failure is an `AIGatewayError` with a stable `code`, a **fixed** message (never provider text, a prompt or a value), the
provider and profile ids, and a `retryable` flag.

| Code | Retryable | Raised for |
| --- | --- | --- |
| `provider_unavailable` | yes | provider cannot be reached or is down |
| `timeout` | yes | provider timed out, or answered after the deadline |
| `rate_limit` | yes | provider refused for rate |
| `policy_violation` | no | locality not allowed, or cloud provider not approved |
| `authentication_failure` | no | provider rejected the credentials |
| `unsupported_model` | no | provider does not support the model |
| `invalid_structured_output` | no | not JSON, not an object, over the limit, or schema violation |
| `configuration_error` | no | missing or invalid configuration, unknown profile, missing policy |
| `invalid_request` | no | bad request, limit above the profile's, input too large |
| `capability_missing` | no | profile needs more than the provider declares |
| `provider_error` | no | any other provider failure, including a foreign exception |

An adapter may raise only the codes `provider_unavailable`, `timeout`, `rate_limit`, `authentication_failure`, `unsupported_model`,
`invalid_structured_output` and `provider_error`; any other code, and any non-gateway exception, becomes `provider_error`. The adapter's
own message is dropped and the provider's exception is not attached as the error's context.

The log lines (`apps.ai_gateway` logger, silent unless the application configures logging) carry identifiers, a code, a reason token and
a duration, never content.

## 9. Provider registration

`ProviderRegistry.build(providers)` takes provider **objects**. It is immutable, ordered by provider id regardless of input order,
rejects duplicate ids (no "last one wins"), rejects strings, classes and anything that is not an `AIProvider`, snapshots descriptors,
and reads no environment, path or `PYTHONPATH`, imports nothing and makes no external call. The only place that lists providers is
`config/ai_gateway.py`; adding a real provider later is a reviewed edit to that tuple.

## 10. The deterministic test provider

`DeterministicTestProvider` is a fixture, **not an AI model**: `kind = deterministic_test_fixture`, locality `local`, `test_only`, no
network, no credentials. Output is a registered fixture (keyed by the SHA-256 of the exact input) or a value generated from the schema,
so the same input always gives the same output. Scenarios: success, malformed JSON, not an object, schema mismatch, timeout,
unavailable, rate limit, authentication failure, unsupported model, and an unexpected foreign exception (to prove nothing leaks).

It is never selected automatically: the production composition lists no provider; a gateway refuses a test-only provider unless built
with `allow_test_providers=True`, which only tests pass; its provenance says `test_only=True`.

## 11. Future real-provider integration

Not part of AI-1a, listed so the next slices do not bypass the foundation:

- **AI-1b (a real provider):** one adapter implementing `AIProvider`, a locality declaration that matches where it really sends data, its
  own socket-level timeouts, mapping of its failures to the adapter codes, and a credential **reference** resolved only inside that
  adapter, from a store outside source control (open question: [open-questions.md](open-questions.md)).
- **Policy source:** where the company's mode and the administrator's cloud-provider approvals are stored and audited (ai-policy.md,
  section 4, mentions `CompanyProfile`, which does not exist). Until then callers pass a `PolicyContext`.
- **Module access:** an `ai.invoke` facade in `ModuleContext`, guarded by a declared capability.
- **Workflow use:** a Request Intake extractor built on the gateway, with prompts delimited as data, validated by the existing business
  rules; this changes extraction behaviour and needs its own slice and evaluation ([ai-policy.md](ai-policy.md), section 11).
- **Provenance recording** on task attempts or revisions, and embeddings under the same policy.

## 12. Security limitations

- **The locality is the provider's own declaration.** Nothing observes where a provider sends data. A wrongly declared or malicious
  provider defeats `local_only`. The defences are that providers are reviewed code listed in one static tuple, and that the descriptor is
  snapshotted at registration; that is a review control, not a technical one.
- **Not a sandbox.** An admitted provider runs in the calling process with its privileges and can open sockets. The architecture tests
  show what the gateway package imports, not what a provider does.
- **Timeouts are cooperative.** The gateway cannot interrupt a running provider call; it refuses an answer that arrives late. A real
  adapter must set its own network timeout.
- **Fallback is absent in the gateway, not policed inside a provider.** The gateway calls exactly the provider and model a profile names
  and never retries with another. A provider that silently runs a different model is not detected or refused; the model it reports is
  recorded in the provenance (`reported_model_id`) and not trusted.
- **The token estimate is a guard**, not a tokenizer.
- **Approval of a cloud provider is a parameter**, not a stored, audited administrator decision.
- **Prompt-injection and truthfulness are not addressed here**: the gateway validates shape, not meaning.
- **Output limits apply after the fact**: an oversized answer is refused, but a provider has already produced it.
- **Logging is metadata only by construction and by test**, but a future adapter that logs content would not be caught by them.
- No concurrency, load or failure-injection testing was done; DB-free tests only.

## 13. Test coverage

DB-free (no PostgreSQL, no network); 166 new tests, each of 21 deliberate mutations of the implementation was caught by them:

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_gateway_contracts.py` | 26 | profile validation and immutability, no credential fields, capabilities, descriptors, request validation, error table |
| `test_ai_gateway_policy.py` | 26 | the policy table, local-only refusal, cloud approval, per-call evaluation, fail closed, no silent fallback, policy before provider invocation |
| `test_ai_gateway_registry.py` | 15 | determinism, duplicates, immutability, descriptor snapshot, no discovery, no network |
| `test_ai_gateway_structured.py` | 16 | the schema subset, validation rules, safe violations, bounded JSON, token estimate |
| `test_ai_gateway_core.py` | 37 | success and provenance, limits, error mapping, late answers, output validation, start-up checks |
| `test_ai_gateway_deterministic.py` | 17 | identity, repeatability, fixtures, every scenario, no network or credentials |
| `test_ai_gateway_secrets.py` | 8 | no prompt, input, output, provider message or secret in errors, tracebacks, logs or reprs |
| `test_ai_gateway_boundaries.py` | 21 | purity, no network or dynamic-import facility, adapters separate, only the composition root imports it, Request Intake and Core unchanged, empty production composition |

Development VM results: the full DB-free suite passed, **2338 tests** (2172 before AI-1a plus these 166); the seven pure test files (145
tests) also pass under plain `python -m unittest` with no Django settings configured, which shows the package needs no Django;
`manage.py check` reported no issues; `makemigrations --check --dry-run` reported no changes (AI-1a has no model or migration; the
consistency check was run with the history check patched out because the VM has no database); `compileall` and `git diff --check` were
clean; the links in the changed documents resolve. These are VM results from this session, with no stored output.

Not tested: any real provider, PostgreSQL, concurrency, or behaviour in a deployed installation.
