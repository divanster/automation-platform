# AI Gateway Foundation (AI-1a), the KPMG Workbench provider (AI-1b), Request Intake extraction (AI-2a), the OpenAI-compatible provider (AI-CON-1), the native Ollama provider (AI-CON-2) and multi-provider AI settings (AI-SET-2)

> **AI-SET-2 status (on `feat/ai-multi-provider-settings`, not committed): the AI settings can now hold several explicitly configured, named CONNECTIONS, and Request Intake's extraction can use the one an administrator selects.** A connection is one statically allowlisted provider type (`kpmg_workbench`, `openai_compatible` or `ollama`) with a locality, an endpoint (network providers), a credential REFERENCE (the name of a backend environment variable, never a value), the model profiles it exposes and an enabled flag. The selection is one connection and one of its profiles; the data policy, the endpoint rules and the guarded transports apply to it exactly as to an environment-configured provider; nothing falls back to another connection, model, provider or the deterministic parser. Checked with DB-free tests and a MOCKED transport only (168 new tests, 3250 DB-free tests in all); its PostgreSQL tests are written and **not run**, nothing was sent to any provider, and there is no frontend. Section 19 describes it and its limits; the legacy, environment-configured Workbench path, the deterministic default and the worker's retry, recovery and module gating are unchanged.

> **AI-CON-2 status (merged into `main`, PR #30): a native Ollama provider (`ollama`) exists for a model server on THIS machine, opt-in.** It calls Ollama's own `POST /api/chat` (not the OpenAI-compatible `/v1` surface), with an explicit loopback endpoint, an explicit approved-origin list and an explicit model; locality is always `local`; no key, no proxy, no redirect, no model download, no fallback. Checked with DB-free tests and a MOCKED transport only; it has never talked to a real Ollama. Section 18 describes it and its limits; since AI-SET-2 (section 19) a stored connection of type `ollama` can select it for Request Intake.

> **AI-CON-1 status (merged into `main`, PR #29): an OpenAI-compatible provider (`openai_compatible`) exists for approved local and company endpoints (and, under the strictest policy, a cloud one), opt-in.** It has an explicit endpoint, approval list, locality and model, an optional key (required unless `local`), strict destination rules checked against the RESOLVED addresses by a guarded transport, and no fallback. Checked with DB-free tests and a MOCKED transport only; it has never talked to a real server. Section 17 describes it and its limits; since AI-SET-2 (section 19) a stored connection of type `openai_compatible` can select it for Request Intake.

> **AI-2a status (added after AI-1b was merged, PR #20): Request Intake can now extract with the gateway, but only when an operator explicitly enables it.** The default
> is unchanged: with nothing set, the worker uses the deterministic fixture parser and no AI provider is called. Section 15 describes the extraction modes, the policy and
> failure rules and what was not done. AI-2a was checked in the development VM with DB-free tests and a MOCKED transport only (the VM never contacts the Workbench);
> its 35 PostgreSQL tests and a 736-test selected regression passed in the owner's local run (owner-supplied; tested scenarios only; section 15, status). Where an older statement below says that nothing calls the gateway, it describes AI-1a and AI-1b as merged;
> since AI-2a the only caller is Request Intake's AI extractor, in ai mode only.

> **AI-1b status (added after AI-1a was merged, PR #19; AI-1b itself was merged as PR #20): the KPMG Workbench provider adapter is written, aligned with the client-side HTTP
> contract reported from the FI integration audit (GP-0299-FI-APP), and tested against a MOCKED transport only. No request has been sent to
> the Workbench by the development VM or by any test, no credential exists in this repository, and (until AI-2a) nothing called the gateway. The owner has since reported one local
> smoke test of the Workbench with GPT-5 (section 14.10(d); owner-supplied, not observed by the VM).** Section 14 describes it and separates what the audit
> reported, what AI-1b added, and what still needs live verification (14.10). The rest of this banner and sections 1 to 13 describe the AI-1a
> foundation; where AI-1b changes a statement, the section says so.

> **Status of AI-1a: implemented as a foundation; no real AI.** AI-1a adds provider-neutral contracts, data-locality policy
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

Not in scope of AI-1a (later slices, section 11; AI-1b added the Workbench provider, section 14): a real local, company-endpoint or cloud provider; credentials and their storage; the
per-company policy setting; the administrator's cloud-provider approval; a module capability facade; prompt building; evaluation;
Request Intake integration; any API route or screen.

## 2. Where it sits

Inspected before writing it: `apps/modules` and `apps/module_host` (manifest, registry, `ModuleState`, `ModuleContext`),
`apps/intake` and `workflows/request_intake` (the extraction contract), `config/settings/*`, `config/env.py`, the task registry and
the existing boundary tests. The boundaries that matter:

| Existing piece | What AI-1a does with it |
| --- | --- |
| `workflows.request_intake.RequestExtractor` (text in, candidate out) | Untouched by AI-1a. It is the *workflow's* extraction seam; **AI-2a implements it on top of the gateway** (`apps/intake/ai_extraction.py`, section 15), used only in ai mode. |
| `apps/modules` (`ai.invoke` in `FUTURE_CAPABILITIES`) | Untouched. A module cannot declare `ai.invoke` yet; pinned by a test. No `ModuleContext` facade. |
| Static composition roots (`config/modules.py`, `config/task_registry.py`) | Followed: providers are named in one static tuple in `config/ai_gateway.py`, nothing is discovered. |
| `apps/modules` as a pure, non-Django package | Followed: `apps/ai_gateway` is not an installed app, has no model, migration, URL or command, and imports no Django. |
| `config/env.py` and settings | **AI-1a added no setting or environment variable**; the policy mode is passed per call. AI-1b adds the `AP_AI_WORKBENCH_*` variables, read lazily by `config/ai_workbench.py` from the process environment and **not** through Django settings (section 14.3). |
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
    transport.py     (AI-1b) the HTTP seam: HttpRequest, HttpResponse, HttpTransport, TransportError; opens no socket
    network.py       (AI-CON-1) pure destination rules: the strict URL grammar, Origin, NetworkClass, address_permitted
    adapters/deterministic.py   the deterministic TEST provider (not re-exported by the package)
    adapters/workbench.py       (AI-1b) the KPMG Workbench provider; pure, talks only to an HttpTransport
    adapters/openai_compatible.py   (AI-CON-1) the OpenAI-compatible provider; pure, talks only to an HttpTransport
    adapters/ollama.py          (AI-CON-2) the native Ollama provider (POST /api/chat); loopback only; pure, talks only to an HttpTransport
backend/config/ai_gateway.py      composition root: BUILTIN_PROVIDERS = (), BUILTIN_PROFILES = ()   (unchanged by AI-1b, AI-CON-1 and AI-CON-2)
backend/config/ai_workbench.py    (AI-1b) opt-in composition: environment -> settings -> Workbench provider, profiles and gateway
backend/config/ai_http_transport.py   (AI-1b) the Workbench's real transport (standard-library http.client, TLS, proxy); refuses a request that carries a network class
backend/config/ai_openai_compatible.py   (AI-CON-1) opt-in composition for the OpenAI-compatible provider (AP_AI_OPENAI_*)
backend/config/ai_ollama.py       (AI-CON-2) opt-in composition for the native Ollama provider (AP_AI_OLLAMA_*); uses the guarded transport with no proxy
backend/config/ai_guarded_transport.py   (AI-CON-1) the guarded transport: resolves, validates every address, connects to a validated one; the other code that opens sockets
```

Adapters import only the contract above them and the standard library; the gateway core never imports an adapter; no business module,
workflow package or Core app imports the package, and the only `config` modules that do are `config/ai_gateway.py` and, since AI-1b,
`config/ai_workbench.py` and `config/ai_http_transport.py` (since AI-CON-1 also `config/ai_openai_compatible.py` and `config/ai_guarded_transport.py`, since AI-CON-2 also `config/ai_ollama.py`); only `config/ai_workbench.py`, `config/ai_openai_compatible.py` and `config/ai_ollama.py` import an adapter (all pinned by
`tests/test_ai_gateway_boundaries.py`). The package itself stays free of `socket`, `ssl`, `http`, `urllib` and `os`: the real network code
is outside it.

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

- **AI-1b (a real provider): the Workbench adapter is implemented (section 14); the checklist below stays the standard for the next one.** One adapter implementing `AIProvider`, a locality declaration that matches where it really sends data, its
  own socket-level timeouts, mapping of its failures to the adapter codes, and a credential **reference** resolved only inside that
  adapter, from a store outside source control (open question: [open-questions.md](open-questions.md)).
- **Policy source:** where the company's mode and the administrator's cloud-provider approvals are stored and audited (ai-policy.md,
  section 4, mentions `CompanyProfile`, which does not exist). Until then callers pass a `PolicyContext`.
- **Module access:** an `ai.invoke` facade in `ModuleContext`, guarded by a declared capability.
- **Workflow use:** a Request Intake extractor built on the gateway, with prompts delimited as data, validated by the existing business
  rules; this changes extraction behaviour and needs its own slice and evaluation ([ai-policy.md](ai-policy.md), section 11). **Done in AI-2a (section 15) behind an explicit
  mode; the evaluation set and acceptance thresholds this item calls for do not exist yet.**
- **Provenance recording** on task attempts or revisions, and embeddings under the same policy.
- **Wiring the Workbench into the production composition** (`config/ai_gateway.py`) and a caller: not done in AI-1b (section 14.1).

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

Not tested: any real provider, PostgreSQL, concurrency, or behaviour in a deployed installation. AI-1b's tests are in section 14.11.

## 14. KPMG Workbench provider (AI-1b)

> **Status: written and aligned with the client-side HTTP contract reported from a read-only audit of the FI integration (GP-0299-FI-APP);
> tested with a MOCKED transport only; never run against the Workbench.** The contract is client-side evidence: it shows what a working FI client
> sends and expects, not that this repository's requests are accepted. No live request was made in the development VM and none is made by any
> test. As of AI-1b nothing called the provider; since AI-2a Request Intake's AI extractor calls it, in ai mode only (section 15). FI business logic was not copied.

### 14.1 What was added, and what was not

| Added | Where |
| --- | --- |
| The HTTP seam (`HttpRequest`, `HttpResponse`, `HttpTransport`, `TransportError`); no socket | `apps/ai_gateway/transport.py` |
| The provider `kpmg_workbench`: request building, answer parsing, error mapping, destination validation, bounded same-deployment retries inside the call's deadline | `apps/ai_gateway/adapters/workbench.py` |
| The real transport: `http.client`, verified TLS, explicit proxy, separate connect and read timeouts, no redirects, a response cap | `config/ai_http_transport.py` |
| The opt-in composition: environment to settings to provider, two profiles and a gateway | `config/ai_workbench.py` |

Not done, on purpose: the Workbench is **not** listed in `config/ai_gateway.py` (its two tuples are still empty, so the application's
default gateway still has no provider); no caller, module capability (`ai.invoke` is still future), API route, screen, setting, model or
migration; Request Intake extraction is unchanged; no per-company policy setting or administrator approval; no AI-2 work. A caller that
wants the Workbench calls `config.ai_workbench.build_workbench_gateway()` explicitly, which is a decision for the slice that first uses AI.

### 14.2 Provider and profiles

The provider declares provider id `kpmg_workbench`, kind `kpmg_workbench_azure_openai`, locality **`company_endpoint`** (the owner's
classification; the gateway cannot observe where data goes, section 12), `test_only = False`, **one** structured-output mode (`json_mode`, the
`json_object` response format of the verified request), **no** generation parameter, and provisional limits of 200,000 input and 32,000 output
tokens. Two immutable profiles are built by `config/ai_workbench.py`; a request names one explicitly, and **nothing ever switches from one to the other**:

| Profile | Deployment (model id) | Role |
| --- | --- | --- |
| `workbench-gpt5` | `gpt-5-2025-08-07-dzs-sdc` | preferred |
| `workbench-gpt41` | `gpt-4-1-2025-04-14-dzs-sdc` | optional, manually selected |

Both profiles: policy mode `company_endpoint`, structured output `json_mode`, **no** sampling parameter, timeout `AP_AI_WORKBENCH_TIMEOUT_SECONDS` (default 90), limits
100,000 input and 16,000 output tokens (provisional, unmeasured). Because the verified request carries no temperature, top-p, seed or token limit for either deployment, a profile
that sets any generation parameter, or asks for `native_schema` or `prompt_only` output, cannot be configured (`capability_missing` at gateway construction); the adapter refuses
the same things again at call time, before anything is sent; an unknown deployment is `unsupported_model`. The output limit is enforced by the gateway after the fact, not sent.
The model the Workbench reports is recorded in the provenance (`reported_model_id`) next to the deployment that was requested; an answer that names none is recorded as
`unreported`, never as the requested deployment.

### 14.3 Configuration contract

Read by `config/ai_workbench.py` from the **process environment** (for local development `manage.py` copies `AP_*` names from the git-ignored `.env`, which
accepts these names). Nothing is read at import; the Django settings hold none of these values. A missing or invalid value raises `configuration_error`
naming the variable, never its value. **Only the key is required.**

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `AP_AI_WORKBENCH_API_KEY` | **yes** | none | The subscription key. Visible ASCII only, 1 to 512 characters. |
| `AP_AI_WORKBENCH_AUTH_HEADER` | no | `Ocp-Apim-Subscription-Key` | The NAME of the header that carries the key. The value sent is the raw key, with no scheme prefix. Names the adapter sets itself (`Content-Type`, `Accept`, `Host`, `x-kpmg-charge-code`, ...) are refused. |
| `AP_AI_WORKBENCH_CHAT_PATH` | no | `/deployments/{deployment}/chat/completions?api-version={api_version}` | Path and query appended to the endpoint. `{deployment}` is required; `{api_version}` is allowed; `..`, `//`, a scheme, an authority or `@` are refused. Override only after live evidence. |
| `AP_AI_WORKBENCH_ENDPOINT` | no | `https://api.workbench.kpmg/genai/azure/openai` | Base URL. **The host must be `api.workbench.kpmg`** (lower case, no port, no credentials, no query or fragment); any other value is refused, so no configuration can send the key elsewhere. Only the path part can differ. |
| `AP_AI_WORKBENCH_API_VERSION` | no | `2024-10-21` | Substituted for `{api_version}`; `[0-9A-Za-z.-]`, at most 32 characters. |
| `AP_AI_WORKBENCH_CHARGE_CODE` | no | `00000000` (a placeholder) | Sent as `x-kpmg-charge-code`. `[A-Za-z0-9._-]`, 1 to 64 characters. |
| `AP_AI_WORKBENCH_TIMEOUT_SECONDS` | no | `90` | The deadline of a whole call, retries and back-off included (1 to 300). |
| `AP_AI_WORKBENCH_MAX_RETRIES` | no | `2` | Further attempts after the first (0 to 5). |
| `AP_AI_WORKBENCH_PROXY_URL` | no | none | An explicit proxy, `http://[user:password@]host[:port]` (section 14.5). |
| `AP_AI_WORKBENCH_CA_BUNDLE` | no | none | An extra PEM file of CA certificates added to the platform trust store (a company TLS-inspection proxy). |

Constants, not configuration: the region `westeurope` (informational: nothing is sent for it), the two deployments, the host allowlist, the connect timeout (10 s, never above
the deadline), the back-off (1, 2, 4, 8 s, capped) and the response cap (8 MiB).

### 14.4 Credential handling

- The key exists only in the process environment (or the git-ignored `.env` that `manage.py` reads for development) and in the `WorkbenchConfig` object built
  from it (`repr` and comparison exclude it). It is copied into exactly one place, the authentication header of an `HttpRequest`, whose `repr` excludes
  headers, body and URL.
- Never in source, the profile, the descriptor, provenance, an error message, a log line, a reason token, the Django settings or a response to React. Errors
  raised for a call carry the fixed messages of section 8; the server's body, status line and text, the transport's exception text and the prompt are never
  copied. Provider and transport exceptions are not chained to the error that replaces them.
- Fail closed: no key, no provider, no gateway. A key containing a space, control character, line break or non-ASCII character is refused (it could not be a
  valid header value and could inject one).
- **The key can only go to the allowed host.** The endpoint host is checked against an allowlist when the configuration is built, the deployment is one of two known
  identifiers and the API version a validated token before either enters the URL, and the finished URL is checked once more immediately before the request is handed to the
  transport. The transport refuses `http://`, follows no redirect and takes no proxy from the environment.
- The proxy URL may carry proxy credentials; it is excluded from the settings `repr` and from the transport `repr`, and sent only as `Proxy-Authorization` to the
  proxy.
- `.env` stays git-ignored; `.env.example` lists the variable names with no values. The VM held no key and the tests use an obviously fake sentinel.
- `SSLKEYLOGFILE` is neutralised in the transport: the standard library would otherwise write the TLS session secrets of this traffic to that file.

### 14.5 Network, TLS and proxy

- **No network at import, registry construction or provider construction.** Tests patch the socket functions to fail, and import the modules in a subprocess, to
  show it. The real transport connects only inside `send`.
- **TLS verification is always on** (platform trust store, host-name checking, TLS 1.2 or later, plus the optional extra CA file). There is no option to turn it
  off, and a context without verification is refused.
- **Proxy: explicit only.** With `AP_AI_WORKBENCH_PROXY_URL` every request goes through an HTTP `CONNECT` tunnel to the proxy and TLS is then verified against the
  real host; without it the connection is direct. `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY` are **never read**, so nothing is bypassed or added
  implicitly (a machine that needs a proxy and has only the environment variable fails closed with `provider_unavailable`). Only `http://` proxies with basic or
  no authentication are supported, not NTLM or Kerberos.
- **Timeouts.** The connect timeout bounds the TCP connect, the proxy tunnel and the TLS handshake; the read timeout, the time left of the call's deadline,
  bounds each later socket operation. They are cooperative: a server that sends a byte at a time can exceed the read timeout in total; the adapter and the
  gateway refuse a late answer but cannot interrupt the socket.
- **Retries: at most `max_retries` (default 2) further attempts to the same deployment**, only for transient conditions that never reached the model (a failed
  connection, HTTP 429, 502, 503, 504), with the back-off above, never past the 90-second deadline. A read timeout, a TLS failure, an authentication failure, any other
  status and any answer that arrived (including text that is not valid JSON) are not retried.
- **No automatic provider or model fallback.** A failing `workbench-gpt5` never causes a call to `workbench-gpt41` or to another provider; a test asserts that no
  request to the other deployment is made after each kind of failure.

### 14.6 The call

One `ProviderCall` becomes one **POST** to `{endpoint}/deployments/{deployment}/chat/completions?api-version=2024-10-21`, for example
`https://api.workbench.kpmg/genai/azure/openai/deployments/gpt-5-2025-08-07-dzs-sdc/chat/completions?api-version=2024-10-21`. The deployment is routed by the path. The headers
are `Content-Type: application/json`, `Accept: application/json`, `Ocp-Apim-Subscription-Key: <raw key>` and `x-kpmg-charge-code: 00000000`. The body is exactly
`{"messages": [{"role": "system", ...}, {"role": "user", ...}], "response_format": {"type": "json_object"}}` and nothing else: **no `model`** (the API version is in the URL),
**no temperature** (for either deployment, by default or at all), **no other sampling parameter** and **no token-limit parameter**. The system message holds the instructions followed
by a description of the output JSON Schema (because `json_object` does not carry the schema, and the word JSON must appear); the user message holds the input text, which is data and is never
merged into the instructions.

The answer is read from `choices[0].message.content`, which holds JSON-encoded text; the adapter returns it as the model's raw text and the gateway then applies its strict JSON
parse, the object check and the schema check. Nothing is repaired. Beyond the verified envelope the adapter is conservative: a `finish_reason` of `length` is
`invalid_structured_output`, any value other than `stop` is `provider_error`, an absent one is accepted; a refusal, a non-string content or an unrecognised envelope is
`provider_error`; `model` and `usage` are recorded if present.

### 14.7 Error mapping

| Condition | Gateway code | Retried |
| --- | --- | --- |
| HTTP 401 or 403 | `authentication_failure` (the gateway has no separate authorization code; adding one would change the public code table, an owner decision) | no |
| HTTP 404 | `unsupported_model` (the deployment is not available to this key; it can also mean a wrong path) | no |
| HTTP 408 | `timeout` | no |
| HTTP 429 | `rate_limit` | yes |
| HTTP 502, 503, 504 | `provider_unavailable` | yes |
| any other status, including 3xx, 400, 500, and a 2xx other than 200 | `provider_error` | no |
| connection refused, name resolution failure, connect timeout, proxy tunnel failure | `provider_unavailable` | yes |
| read timeout; the deadline reached; an answer after the deadline | `timeout` | no |
| TLS certificate or handshake failure | `provider_error` | no |
| malformed or oversized HTTP answer, a reset connection, any foreign exception | `provider_error` | no |
| a 200 whose content is not the requested JSON, breaks the schema, or was cut off (`finish_reason = length`) | `invalid_structured_output` | no |
| a 200 whose envelope or content is not recognised, or whose `finish_reason` is not `stop` or `length` | `provider_error` | no |
| a deployment the adapter does not know, or a profile naming one | `unsupported_model` | no |
| a generation parameter or a mode other than `json_mode` in a profile | `capability_missing` at start-up; `provider_error` if it reached a call | no |
| a missing or invalid configuration value, including an endpoint on another host | `configuration_error`, at construction, never at a call | no |
| `local_only`, or a profile whose mode forbids a company endpoint | `policy_violation` or `configuration_error`, before the adapter runs | no |

### 14.8 Data locality

The provider is `company_endpoint`. `local_only` refuses it before the provider runs (no request is built, nothing is sent); `company_endpoint` permits it;
`approved_cloud` permits it by the cumulative rule, with no cloud approval needed because its locality is not `cloud`. The mode is an argument of every call;
there is still no stored company policy, and no prompt or document can reach the Workbench unless a caller builds this gateway **and** passes a permitting
mode. Whether the Workbench really is a company-operated endpoint, and whether its retention and logging are acceptable for the data a workflow sends, are
decisions for the owner that no code verifies.

### 14.9 Local validation and live prerequisites

**What the development VM can run (no PostgreSQL, no network, no dependency installation), from `backend\`:**

```powershell
& $Py manage.py test tests.test_ai_workbench_adapter tests.test_ai_workbench_transport tests.test_ai_workbench_composition tests.test_ai_gateway_transport_contract tests.test_ai_gateway_boundaries
& $Py manage.py test                                    # the whole DB-free suite
& $Py -m unittest tests.test_ai_gateway_contracts tests.test_ai_gateway_policy tests.test_ai_gateway_registry tests.test_ai_gateway_structured tests.test_ai_gateway_core tests.test_ai_gateway_deterministic tests.test_ai_gateway_secrets tests.test_ai_gateway_transport_contract tests.test_ai_workbench_adapter tests.test_ai_workbench_transport   # pure, no Django settings
& $Py manage.py check
```

**Before anyone sends a first live request** (none of this was done; each item needs an owner decision or evidence):

1. A key provisioned outside the repository, a real charge code (the shipped `00000000` is a placeholder), and the network route from the machine that runs the worker (a
   proxy URL and a CA bundle if the company requires them).
2. The owner's confirmation that the Workbench is a `company_endpoint` for the data that will be sent, and a slice that decides where this gateway is built and who passes the policy.
3. A first probe with **synthetic, non-customer text only**, compared with the mapping table above. An assumption that fails will show as `provider_error` or `unsupported_model`; find
   out why with a transport trace in a debugger, not by adding body logging.
4. The items of section 14.10(c), each of which that probe should confirm or refute.

### 14.10 Evidence, and what it does not prove

**(a) Reported as confirmed by the FI audit (client side).** The owner supplied these from a read-only audit of the existing FI integration (GP-0299-FI-APP) and described them as
"confirmed client-side contract rules, not proof of live Workbench compatibility". **The summary given to AI-1b did not say which items come from FI production code and which from FI mocked
tests, so this document does not assign them; the owner should annotate the table.** Nothing was copied from FI.

| Item | Contract |
| --- | --- |
| Method and URL | `POST https://api.workbench.kpmg/genai/azure/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21` |
| Authentication | `Ocp-Apim-Subscription-Key: <raw API key>` |
| Charge code | `x-kpmg-charge-code: 00000000` |
| Region | `westeurope` (nothing is sent for it) |
| Deployments | preferred `gpt-5-2025-08-07-dzs-sdc`; alternative `gpt-4-1-2025-04-14-dzs-sdc`; chosen by the path |
| Request body | `messages` (system, user) and `response_format` `{"type": "json_object"}` |
| With `api-version` in the URL | no `model` in the body; no temperature by default; no sampling parameters; no token-limit parameters until verified |
| Answer | `choices[0].message.content`, JSON-encoded text |
| Source of items (production code / mocked tests) | **not itemised in what was supplied** |

**(b) Added by AI-1b as conservative handling; not from FI.** Reading `finish_reason`, `model`, `usage` and `refusal` when present; the status-to-code mapping and which statuses are
retried; the back-off; the 200,000 and 32,000 token declarations and the 100,000 and 16,000 token profile limits; the response cap; the host allowlist; the proxy and CA options; the
reading of 404 as an unknown deployment.

**(c) Still requiring live verification.** The tests prove that the adapter does what it says on a mock; they prove nothing about the Workbench.

1. That the live gateway accepts exactly this request (URL, header, charge code, body) from this machine, with a real key and charge code, and that `00000000` is accepted outside FI.
2. That `gpt-5-2025-08-07-dzs-sdc` produces valid JSON for `json_object` **with no temperature and no token limit**, and that the deployment's own default output limit suffices for the profile's 16,000-token expectation.
3. That the same minimal request works for `gpt-4-1-2025-04-14-dzs-sdc`.
4. That the answer carries `choices[0].message.content` as described, and whether it also carries `model`, `usage` and `finish_reason` (an answer without `model` is recorded as `unreported`).
5. The status codes for bad key, unknown deployment, rate limit and outage (401 and 403 for credentials, 404 for an unknown deployment, 429, 502, 503 and 504 are assumed); `Retry-After` is not read.
6. That a company proxy and TLS inspection, if any, work with the explicit proxy and the extra CA file (only `http://` proxies with basic or no authentication are supported).
7. That the output description in the system message gives schema-conforming JSON often enough: `json_object` does not enforce the schema, the gateway validates and rejects, nothing is repaired.
8. Data handling by the Workbench (locality, retention, logging, charge-code policy): the owner's classification; nothing verifies it.

**(d) Owner-supplied smoke test (reported with AI-2a; not observed by the VM).** The owner ran one local GPT-5 call through the Workbench provider and reported: provider
`kpmg_workbench`, profile `workbench-gpt5`, deployment `gpt-5-2025-08-07-dzs-sdc`, reported model `gpt-5-2025-08-07`, structured JSON valid, **2.33 seconds**, **107 input
tokens and 148 output tokens**. No output or log is stored here. In that one run it is evidence that this request shape was accepted for the GPT-5 deployment, that `choices[0].message.content`
held valid JSON, and that the answer reported a model and token usage (items 1, 2 and 4 below, for GPT-5, in that run). It does **not** cover the GPT-4.1 deployment, the status codes, a proxy,
Request Intake's prompt and schema, longer inputs, rate limits, retention, or any repeat. The VM made no request.

### 14.11 Test coverage

DB-free, mocked transport, no network, no credential; 147 new tests (the full DB-free suite is 2485, up from 2338):

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_workbench_adapter.py` | 88 | the defaults, configuration validation, **endpoint destination rejection**, the exact URL per deployment, API-version routing, the raw subscription-key header, the charge-code header, the minimal body (model, temperature, sampling and token limits absent; json-object format), identical GPT-5 and GPT-4.1 bodies, refusal of any parameter or other mode, answer-envelope parsing, error mapping, bounded retries and back-off, deadlines and per-attempt timeouts, no fallback, local-only refusal with zero requests, no secret or prompt in errors, logs or reprs |
| `test_ai_workbench_transport.py` | 26 | the real transport on a fake `http.client`: TLS verification and no way to disable it, explicit proxy only (environment proxies ignored, `NO_PROXY` cannot bypass), proxy credentials, connect and read timeouts, no redirect, response cap, failure kinds, no leakage, `SSLKEYLOGFILE`, no socket when built |
| `test_ai_workbench_composition.py` | 28 | the environment contract (key alone is enough), overrides, fail-closed configuration, endpoint on another host refused, errors that never echo a value, the opt-in gateway and profiles, no network or environment read at import or registration, the production root untouched, Request Intake untouched |
| `test_ai_gateway_transport_contract.py` | 5 | the pure transport contract |
| `test_ai_gateway_boundaries.py` | 21 (edited) | the new files and the only allowed importers of the package and of the adapter |

Each of 27 deliberate mutations of the alignment (URL, header, charge code, body contents, destination checks, envelope parsing, mode and parameter limits, retry bounds, key requirement) was caught by
these tests. Not tested: the real Workbench, a real proxy, real TLS certificates, concurrency, and PostgreSQL.

## 15. Request Intake extraction with the gateway (AI-2a)

> **Status: implemented on `feat/ai-request-intake-extraction` (implementation `e2fac8a`); checked in the development VM with DB-free tests and a MOCKED HTTP transport (148 new tests, 2636 DB-free tests in all pass). The project owner
> ran the 35 PostgreSQL tests in `tests/test_intake_ai_postgres.py` and a selected PostgreSQL regression locally: all 35 passed, and the regression passed with 736 tests in 238.962 s, exit code 0, test database destroyed (owner-supplied; the VM did
> not run or observe them; no output is stored; see the [local validation record](../development/local-validation-record.md)). That covers the tested scenarios only, with a scripted provider: existing review, approval, audit and worker behaviour were preserved
> in them.** The VM never contacts the Workbench and holds no key. The only live evidence about the provider is the owner's single local GPT-5 smoke test (section 14.10(d)), which did not use Request Intake's prompt or schema.
> **There is no real GPT-5 end-to-end test of Request Intake, no extraction accuracy evaluation set, no frontend change and no production-readiness claim.**

### 15.1 What was added, and what was not

| Added | Where |
| --- | --- |
| The prompt, the output JSON schema and the conversion of a validated answer into a candidate (pure; no gateway import) | `backend/apps/intake/ai_output.py` |
| `AIRequestExtractor`, a `RequestExtractor` that calls `AIGateway.generate` (the only file of the app that names the gateway) | `backend/apps/intake/ai_extraction.py` |
| `AIExtractionError` (stable code, `retryable`) and `ExtractionConfigurationError` | `backend/apps/intake/errors.py` |
| An optional extractor source the worker installs; failure mapping onto the queue's existing codes | `backend/apps/intake/handlers.py` |
| The extraction modes, their environment settings, the start-up checks and the module re-check | `backend/config/intake_extraction.py`; installed by `prepare_worker` in `backend/config/task_registry.py`, called by `runworker` |

Not done, on purpose: no frontend change, no document upload, no connector, no second provider, no AI-2b, no model, migration, endpoint, audit action, failure code, task kind, queue or worker; no
change to `run_extraction`, validation, canonicalization, hashing, review or approval; no change to the gateway package, the Workbench adapter, `ModuleState` gating or `config/ai_gateway.py` (still empty);
no stored company policy, credential store or administrator approval; no `ai.invoke` module capability (the module's manifest is unchanged: the worker's composition root builds the gateway, not the module).

### 15.2 Modes and configuration

Two explicit modes, no automatic switch and no fallback in either direction. Read from the **process environment** by `config/intake_extraction.py` when the worker starts (for development
`manage.py` copies `AP_*` names from the git-ignored `.env`); not Django settings, not database fields, nothing read at import. An empty value counts as unset.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AP_INTAKE_EXTRACTION_MODE` | `deterministic` | `deterministic`: the fixture parser, no AI provider is built or called, nothing else is read. `ai`: the AI extractor. Any other value stops the worker at start-up. Setting the Workbench variables never switches the mode on. |
| `AP_AI_POLICY_MODE` | none (**required in `ai` mode**) | `local_only`, `company_endpoint` or `approved_cloud`: the installation's explicit data policy. The Workbench is a `company_endpoint` provider, so `local_only` is refused at start-up (it would fail every call); the gateway enforces the mode again on every call. |
| `AP_INTAKE_AI_TIMEOUT_SECONDS` | `45` | The deadline of one AI call, retries inside the provider included, 5 to 300. It can only lower the profile's own timeout (`AP_AI_WORKBENCH_TIMEOUT_SECONDS`, 90): the lower of the two is used. |
| `AP_AI_WORKBENCH_*` | as section 14.3 | The provider's settings. Only `AP_AI_WORKBENCH_API_KEY` is required. |

The profile is not configurable: Request Intake uses `workbench-gpt5` (`INTAKE_AI_PROFILE_ID`), checked against an allowlist (`APPROVED_INTAKE_AI_PROFILES`, one entry). `workbench-gpt41` is
not approved for this workflow; approving it, or any provider, is a reviewed code edit. Nothing ever switches profile, model or provider.

**Fail closed at start-up.** `runworker` calls `prepare_worker(lease_seconds)` before its first iteration. In `ai` mode it builds the Workbench gateway (key and settings validated, nothing sent),
checks the profile and the policy with the gateway's own `enforce_policy`, and checks the lease (15.7). Any missing or invalid value stops the command with an error that names the variable and never its
value; nothing is installed and no task is claimed. The default mode adds one log line (`Request Intake extraction mode: deterministic ...`) and changes no output of the command.

### 15.3 The path, and where the AI call sits

```text
submit_request -> run(queued) + task                                   (unchanged)
worker claim (kind gate: module enabled and version-matched)           (unchanged)
  -> request_intake.extract handler -> run_extraction(run_id, extractor)
       T1 (short): lock the run, return an existing outcome, move to extracting, read the text
       no transaction: AIRequestExtractor.extract(text)
            preflight: the module's kind is still claimable (the worker's own kind gate, read again)
            AIGateway.generate  -> policy -> Workbench adapter -> schema validation
            candidate_from_output(...)  -> a StructuredRequest + ambiguities
       build_extraction -> create_request_intake_revision -> validate, canonicalize, hash   (unchanged)
       T2 (short, atomic): re-check; revision 1 (valid draft only) + ExtractionOutcome + needs_review + audit events   (unchanged)
```

A slow model never holds a lock or a connection transaction (the worker refuses to run a handler inside one). Nothing is written before T2, so any failure up to and including the answer leaves no
revision, no outcome and no approval; the run stays `extracting` until a retry succeeds or the reconciler fails it.

### 15.4 Structured output

The schema (`OUTPUT_SCHEMA`) is the gateway's closed JSON Schema subset and names exactly four keys, each required, with `additionalProperties: false` at every level:
`requester` (`name`, `contact`), `items` (each `description`, `quantity`, `unit`, `notes`), `notes`, `ambiguities` (each `field_path`, `excerpt`). The field names are the domain's
(`workflows.request_intake.schema`); a test compares them. There is no key for a decision, status, price or approval, and an answer that adds any key is refused by the gateway. The `quantity` is a
**string** of digits (a JSON number would reach the code as a float and lose digits such as the trailing zero of `2.50`) and becomes an exact `Decimal`.

The conversion (`candidate_from_output`) is a candidate, not a result. It refuses, rather than repairs, an answer in which: a quantity is not an optionally signed ASCII decimal number (the fixture parser's own
rule; a quantity that cannot be read is to be reported as an ambiguity with `null`, as the fixture parser reports it); an ambiguity names a field path outside the documented grammar or an item that does not
exist (the validator would otherwise raise, and an unexpected exception is retried by the queue); an ambiguity excerpt does not occur in the source text (the validator's trim, collapse and case-fold comparison); a text value holds a NUL character or a lone surrogate
(JSON permits both as escapes; PostgreSQL cannot store the first and UTF-8 cannot encode the second, so the write would fail after the provider was paid and be retried as if transient; the submitted text is refused for the same reasons).
It does **not** judge the business meaning: a missing requester or a zero quantity is the validator's finding. Strings are kept exactly as given (an empty optional string is not turned into `null`). `source_refs`
are not requested (a model cannot be trusted to count lines against the fixture convention), so a draft has none; the `value_not_in_source` warning still checks every value against the text.

The request text is sent as data between two marker lines whose marker (`REQUEST_TEXT_<n>`) does not occur in the text, in the user message; the instructions are a constant and contain no request content. This reduces,
and does not remove, prompt-injection risk: a hostile text can still produce a wrong but well-formed answer, which the validator's warnings and the human reviewer are there to catch.

### 15.4a Extraction quality hardening (AI-2b)

Two defects were seen in the owner's first real GPT-5 Request Intake run (owner-reported; not observed by the development VM) and are addressed without touching the workflow, canonicalization, hashing, approval or any stored revision:

* **A sentence full stop copied into the contact** (`jane.example@example.invalid.`). The value was found in the source, so nothing flagged it. The prompt (now **version 2**, so new outcomes record `2|<profile>|<model>`; older outcomes keep `1|...`) tells the model that an address ends at its last letter or digit, and
  `candidate_from_output` applies one deterministic rule to the answer: a contact that is **exactly one plain ASCII e-mail address plus trailing sentence punctuation** (`. , ; : ! ?`) is stored without the punctuation. Nothing else is changed: a phone number, a name,
  an address the pattern does not match and any other string are kept as given; a value that holds `@`, ends in punctuation and is not one clean address (`Jane <jane@example.invalid>.`) is kept and reported as an ambiguity on `requester.contact` (an optional field: a warning), when it can be quoted from the source.
  The raw model answer is not stored, so the removed characters are not recorded anywhere.
* **`2.50` became `2.5` and then warned.** The extraction kept `2.50`, but canonicalization (unchanged, version 1) stores `2.5`, and an edit starts from the stored revision, so the check "is this number in the source?" looked for `2.5` in a text that says `2.50`. The check now also accepts
  extra trailing zeros in the source (`2.5` in `2.50`, `2` in `2.00`) and still warns when the value has more precision than the source (`2.50` against `2.5`), for `2.5` against `2.505` or `12.5`, and for a number glued to another digit group by a comma (`5` in `1,5`). It is still only a warning.

Not changed: a quantity that is not a plain number is still refused (`invalid_ai_output`), a zero or negative quantity is still the validator's blocking finding, a value the source does not contain is still a warning that a reviewer must acknowledge, and there is no fallback and no automatic approval.
Tested with a scripted provider only; **no live Workbench request was made for AI-2b**, so that the model now omits the full stop is not shown (the deterministic rule does not depend on it), and no accuracy measurement exists.

### 15.5 What is preserved

Validation, canonicalization, the content hash, revision 1, the `ExtractionOutcome`, the run states and the audit events are produced by the **same existing code** for both modes; a test compares the stored
revision's hash and bytes with an independent `canonicalize_request` of the same candidate, and the provenance is never part of the hashed content. The audit actions and their safe detail keys are Core's and
unchanged (`revision.created` carries `origin: "extraction"`); no AI detail is added. Approval stays human: the AI answer has no field that could approve, the revision is `needs_review`, the existing
review rules (a different approver, acknowledged warnings, exact hash) apply, and an invalid outcome is repaired through the existing edit path. The outcome records which extractor produced it:
`extractor_name` `ai-request-extractor`, `extractor_version` `<prompt version>|<profile>|<model the provider reported>` (for example `2|workbench-gpt5|gpt-5-2025-08-07`; outcomes written before AI-2b hold prompt version `1`; `unreported` when the provider names none;
a model id too long for the 50-character column is replaced by a fixed token, never cut). Older outcomes are never rewritten.

### 15.6 Data policy and privacy

Before any text leaves the machine, in this order: ai mode was enabled by name; the policy was stated explicitly and permits the provider's declared locality (start-up, then the gateway on every call, before
provider code runs); the profile is on the approved list; the module's task kind is still claimable (read again immediately before sending; a disabled or version-mismatched module stops the call, which is retried
and, if the module stays unavailable, held by the claim gate); configuration was complete. Anything missing is an error, never a default.

Not logged and not stored outside the existing outcome: the request text, the prompt, the raw answer, the key and extracted values. The gateway's log lines hold identifiers, a code and a duration; the handler logs the
run id and the stable `code`, `reason` and `retryable` tokens; errors carry fixed messages and are raised without chaining the provider's exception. `runworker` now also writes the `apps.intake` and `apps.ai_gateway`
loggers to the console while it runs. The draft, ambiguities and findings of an outcome are plaintext business content in PostgreSQL, as before. **What the Workbench retains or logs, and whether it is acceptable for the data a
workflow sends, is the owner's decision; no code verifies it** (section 14.8).

### 15.7 Failure semantics

| Failure | Gateway code | `AIExtractionError` | Queue | Task `failure_code` |
| --- | --- | --- | --- | --- |
| provider unreachable or 502, 503, 504 | `provider_unavailable` | retryable | retried within the attempt limit (default 3) with the queue's back-off | `temporary_failure`, then terminal after the last attempt |
| read timeout, late answer | `timeout` | retryable | as above | as above |
| HTTP 429 | `rate_limit` | retryable | as above | as above |
| module disabled or version-mismatched between claim and call | (not a gateway code) `module_unavailable` | retryable | as above | as above |
| HTTP 401 or 403 | `authentication_failure` | permanent | task fails at once | `rejected` |
| policy forbids the provider, configuration error, unknown profile or deployment, capability, request | `policy_violation`, `configuration_error`, `unsupported_model`, `capability_missing`, `invalid_request` | permanent | task fails at once | `rejected` |
| not JSON, not an object, wrong shape, extra key, truncated | `invalid_structured_output` | permanent | task fails at once | `rejected` |
| answer passes the schema but Request Intake refuses it (15.4) | `invalid_ai_output` (not a gateway code) | permanent | task fails at once | `rejected` |
| any other provider failure | `provider_error` | permanent | task fails at once | `rejected` |
| invalid extraction setting seen by the handler | (not a gateway code) | permanent | task fails at once | `rejected` |
| a draft with blocking findings (business validation) | (not a failure) | | the task **succeeds** | none: an `invalid` outcome, no revision, `needs_review`, a person edits it |

Only `retryable` is read; the queue's closed set of failure codes is unchanged (no code was added). A permanently failed task is moved to `failed` (`failed_step` `extract`) by the existing reconciler, and
**there is no manual retry of a failed run yet**, so an invalid answer or a transient outage that outlasts three attempts needs the request to be submitted again. The handler's retries multiply with the
provider's own (up to 2 further attempts inside one call): at most 3 attempts times 3 requests. A failed call never falls back to the fixture parser, to another profile or to another provider (a test
patches the fixture parser to fail and asserts it is not called). The reason for a failure is in the worker's log (code and reason tokens) and the task's safe `failure_code`; no new table records it.

**The lease.** The worker has no heartbeat, so an attempt must finish within `AP_WORKER_LEASE_SECONDS` (default 60). In ai mode `runworker` refuses to start unless the lease is at least the call deadline plus 15
seconds (default 45 + 15 = 60). A lease that expires anyway (a slow database, a stalled call) lets another worker run the task; a completed outcome is never overwritten (15.8).

### 15.8 Idempotency and recovery

At most one outcome exists per run (a database constraint). A retry after a crash that follows the commit finds the outcome in T1 and **does not call the provider again**; a worker that lost its lease mid-call
and finishes late finds the other worker's outcome in T2 and discards its answer. Two workers may each pay for one AI call when they race; the first committed result stands. A retry after a failure before T2
asks the provider again (a different answer is possible: model output is not deterministic), so the AI mode is **at-least-once with respect to provider calls** and exactly-once with respect to stored results.

### 15.9 Limitations (remaining)

* **Never run end to end.** No Request Intake extraction has been sent to the Workbench by anyone; the prompt, schema and refusal rules were written without an evaluation set. How often GPT-5 answers within the
  schema, quotes ambiguities verbatim, or reads quantities as digits is unknown, and a refused answer fails the whole extraction (there is no partial acceptance).
* **No evaluation, thresholds or drift detection** (ai-policy.md, section 11), no measured accuracy, cost or latency beyond the owner's single smoke test.
* **No source references from the model**, no verbatim check of values beyond the existing warning, and no protection against a plausible wrong answer other than that warning and the human review.
* Prompt injection is mitigated (data markers, no tools, a closed schema, no decision field, a human approver), not eliminated.
* The policy is an environment setting of the worker, not a stored, audited company decision; the locality is the provider's own declaration (section 12).
* No manual retry of a failed run; no per-run record of provenance beyond `extractor_name` and `extractor_version`; token usage and duration are in the gateway's response and are not stored.
* The module re-check narrows, and does not close, the window between the claim and the call (and a module disabled during the call cannot stop it); the API still accepts submissions while the module is disabled.
* The 45-second deadline, the 15-second lease margin, the 200-item and 100-ambiguity bounds and the 64-character quantity bound are provisional and unmeasured.
* The request text limit is 65,536 bytes; whether the profile's provisional input limit is adequate for real requests is unmeasured.

### 15.10 Tests

DB-free, no PostgreSQL, no network (the real Workbench adapter behind a fake transport): 148 new tests; each of 16 deliberate mutations of the implementation (retry mapping, exact quantities, excerpt and path checks, the schema's
extra-key refusal, the default mode, the lease check, the preflight, policy widening, a silent fallback, delimiting, the version bound, the approved profile, the module-gate answer, configuration-error retry, the NUL check) was caught.

| File | Tests | Covers |
| --- | --- | --- |
| `test_intake_ai_output.py` | 42 | the schema (accepted by the gateway subset, four keys, no decision field, string quantity), conversion, exact digits, refusals, text PostgreSQL cannot store, ambiguities, delimiting, provenance bounds |
| `test_intake_ai_extraction.py` | 37 | the request sent, policy before any send, every failure class with exactly one request, no fallback, no leakage to errors or logs, preflight, and the whole pipeline: revision one, hash equal to the domain's, findings, warnings, no approval |
| `test_intake_ai_composition.py` | 53 | the settings and defaults, deterministic mode builds nothing, fail-closed start-up, the lease rule, the handler wiring and failure mapping, `prepare_worker`, the `runworker` start-up and logging |
| `test_intake_ai_boundaries.py` | 16 | the extractor is the only importer of the gateway, no environment or credential in the app, the gateway stays generic, no model, migration, setting, audit action or failure code, the workflows untouched |
| `test_intake_ai_postgres.py` | 35 (**passed in the owner's local run**; owner-supplied, not run by the VM) | AI extraction through the real worker, revision, validation, review, approval and rejection, retry and exhaustion, recovery, rollback, idempotency, module disabled, competing workers, the deterministic regression |

Two existing test files were edited, pins only: `test_intake_composition.py` (the app's source-file list and the list of pure modules) and `test_ai_gateway_boundaries.py` (the importers of the gateway and the files that may name it); the existing extraction, worker, gating and
module tests pass unchanged. Not tested: the real Workbench with Request Intake's prompt, concurrency beyond the written PostgreSQL tests, behaviour in a deployed installation, and cost or latency. The PostgreSQL tests ran only in the owner's local run, not in the VM.

## 16. AI settings backend (administrator-configured extraction)

> **Status: written, DB-free tested in the development VM, NOT run against PostgreSQL, NOT run against the Workbench, no frontend.** Branch `feat/ai-settings-api`; not committed or merged when this was written.

### 16.1 What was added, and what was not

An administrator can read and change how Request Intake extracts through a small API, instead of editing the worker's `.env`. Added: the Django app `apps.ai_settings` (one singleton row, pure validation rules, two services), `GET` and `PUT /api/ai-settings/extraction/`
(`apps/api/views_ai_settings.py`), a stored-settings read in the worker's composition (`config/intake_extraction.py`, `config/task_registry.py`) one additive migration (`ai_settings.0001`), and an audit event for every successful change (`ai_settings.updated`, with audit migration `0003`; section 16.9).

Not added: any frontend, any credential store (the Workbench key stays in the process environment and is never stored, accepted or returned), a hot reload (16.5), a second provider (AI-SET-2 later added named connections for three provider types, section 19), a model other than `workbench-gpt5` for the legacy path, a per-company policy,
a worker status or heartbeat, or any change to the gateway package, to Request Intake's extraction path, its review and approval, or its default (deterministic).

### 16.2 Where AI configuration comes from

| Source | When used | Read by | When |
| --- | --- | --- | --- |
| Environment (`AP_INTAKE_EXTRACTION_MODE`, `AP_AI_POLICY_MODE`, `AP_INTAKE_AI_TIMEOUT_SECONDS`) | no stored row exists | the worker | once, at start-up |
| The stored row (`ai_settings_aiextractionsettings`, id 1) | a row exists; it then **replaces** those three variables | the worker | once, at start-up |
| `AP_AI_WORKBENCH_*` (the key, endpoint, charge code, proxy, TLS) | always, in `ai` mode | the worker | once, at start-up |

With no row, behaviour is exactly what section 15 describes. Precedence is between two sources of one decision; it is **not** a provider fallback: a stored `ai` row that cannot be honoured (no key, invalid values, lease too short) stops the worker, and an invalid or unreadable row
is never replaced by the environment or by deterministic mode. A table that has not been migrated also stops the worker. The profile of a stored row must be one the composition root approves for Request Intake (`workbench-gpt5`), and is re-validated at start-up against the same rules as the API.

### 16.3 API contract

Both methods need a logged-in **administrator** (`is_superuser`; reloaded from the database on every call). Operator, Approver and Auditor roles, and Django model permissions, never grant access. Authorization is decided before validation, so a refused caller learns nothing about the catalog.
`PUT` needs the CSRF token like every state-changing API call. Other methods: 405.

`GET /api/ai-settings/extraction/` and a successful `PUT` return:

```json
{
  "settings": {"extraction_mode": "deterministic|ai", "provider_id": null, "profile_id": null, "policy_mode": null, "timeout_seconds": null},
  "stored": false, "revision": 0, "updated_at": null, "updated_by": null,
  "apply": "worker_restart_required",
  "catalog": {
    "providers": [{"provider_id": "kpmg_workbench", "label": "...", "locality": "company_endpoint",
                   "status": {"configured": true, "issue": null},
                   "profiles": [{"profile_id": "workbench-gpt5", "model_id": "...", "permitted_policy_modes": ["company_endpoint", "approved_cloud"]}]}],
    "policy_modes": ["company_endpoint", "approved_cloud"],
    "timeout": {"min": 5, "max": 300, "default": 45}
  }
}
```

`PUT` body: `extraction_mode` (`deterministic` or `ai`); in `ai` mode also `provider_id`, `profile_id`, `policy_mode` and `timeout_seconds` (all required; there is no default, as for the environment path), in `deterministic` mode none of them (null or absent); `expected_revision` (0 when
`stored` is false). A save is accepted or refused as a whole.

* `status.issue` is `missing_api_key` or `invalid_configuration`; it never contains a value. It is the status **in the web process's environment**; the worker has its own environment, and the API cannot see it.
* `400 validation_error` with `details.fields`, one fixed code per field (`invalid_choice`, `required`, `not_allowed_in_deterministic_mode`, `unknown_provider`, `unknown_profile`, `profile_not_of_provider`, `policy_forbids_provider`, `out_of_range`, `exceeds_worker_lease`,
  `invalid_value`, `unknown_field`, `invalid_type`). Submitted values are never echoed. `local_only` is `policy_forbids_provider` (the only provider is a company endpoint).
* `409 stale_settings_revision` when `expected_revision` is not the stored revision (checked under a row lock).
* The timeout must satisfy timeout + 15 s ≤ the web process's `WORKER_LEASE_SECONDS` (default 60). The worker checks this again with **its** lease; a worker started with a shorter lease than the web process refuses to start.

### 16.4 Security behaviour

* **No secret anywhere.** No column, serializer field or response can hold a key, header or endpoint; a submitted `api_key` or any unknown field is refused as `unknown_field` and not echoed. The key is read only from the worker's (and, for the yes/no status, the web process's) environment.
* **Admin only**, refusals logged by identifier (`ai_settings.read` / `ai_settings.update`), no role grant, CSRF enforced, authentication required.
* **Policy enforced twice.** The catalog's `permitted_policy_modes` is computed by the gateway's own `enforce_policy`, so the API cannot offer a pairing the gateway would refuse; the gateway enforces it again before every provider call.
* **No provider or model fallback, no automatic mode switch.** The catalog contains only what the composition root approves; the model profile is never chosen by the client beyond that list.
* **Fail closed** at the worker for an invalid or unreadable row. Logging of a save: actor public id, revision, mode, profile and policy only.
* **Audited.** A successful save writes one `ai_settings.updated` audit event in the same transaction (section 16.9); the event holds identifiers and numbers only, never a key, proxy, charge code or text.
* The model's CHECK constraints enforce a singleton row, a known mode, a positive revision and all-or-nothing (deterministic: no AI field; ai: all of them). Which provider, profile and policy are acceptable is checked in Python (API and worker), not by the database; raw SQL can store a value
  that the worker will then refuse at start-up.

### 16.5 Runtime behaviour: restart required, no hot reload

`runworker` reads the stored row **once**, in `prepare_worker`, and installs an immutable extractor source; nothing on the task path reads the settings or the environment again. **A saved change therefore reaches no running worker.** Every response carries
`apply: "worker_restart_required"`; the change takes effect only when the worker process is stopped and started again. There is no heartbeat, so neither the API nor a future UI can say which revision a running worker uses; until that exists a UI must say "saved; restart the worker".
The web process does not need a restart for a saved change (it reads the row per request), but it must be restarted for a changed `WORKER_LEASE_SECONDS` or Workbench variable to show in its validation and status. The migration must be applied before the worker starts.

### 16.6 Not changed

Request Intake's handler, extractor, validation, canonicalization, hashing, revisions, review, approval and the queue are untouched; no task kind, failure code or setting was added, and the only audit addition is the `ai_settings.updated` action and `ai_settings` subject type (16.9); `runworker` is unchanged. With no stored row, the deterministic default and the environment
path behave as before (existing tests pass unchanged apart from pins: the installed-apps and route lists, and `load_stored_settings` patched in the tests that call `prepare_worker` without a database).

### 16.7 Limitations

* **Audit is partial.** Each successful change is recorded (16.9), but only the new values: the previous values are not in the event (the previous revision is `revision - 1`), refused and failed attempts are not audited, and audit immutability is ORM-level only (no database trigger, no hash chain). It is not the approved company policy record of `ai-policy.md` section 4: there is no approval step.
* Provider status is the web process's view; the worker may differ. No worker status or active-revision report.
* Login throttling is per process; TLS, HSTS and the HTTPS redirect remain undecided (see `rest-api-slice.md`).
* One provider, one approved profile, one policy-locality rule; the credential store, a company-level policy record, approvals and evaluation remain open (ai-policy.md).
* Never exercised against a real PostgreSQL server or the Workbench; `tests/test_ai_settings_postgres.py` (including its audit and migration tests) is **written but never run**. PostgreSQL validation is pending.

### 16.8 Tests

DB-free, in the development VM, no network and no key: 145 new tests (2841 DB-free tests in all, OK).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_settings_rules.py` | 29 | the catalog built by the composition root (only the Workbench and `workbench-gpt5`, gateway-derived permitted policies, no environment read) and the pure rules: all-or-nothing, membership, `local_only`, timeout bounds and lease, strict types, unknown fields, codes only |
| `test_ai_settings_api.py` | 41 | authorization (every role, anonymous, inactive), CSRF, contract shape, validation errors, conflict, secret redaction, status probe failures, no write on refusal |
| `test_ai_settings_worker.py` | 25 | stored-row precedence, invalid or tampered rows fail closed with no environment fallback, `local_only`, lease, unreadable database, no hot reload, the unchanged environment path |
| `test_ai_settings_boundaries.py` | 18 | no gateway import outside the composition root, pure rules, a thin view, no secret column, an additive `ai_settings` migration plus only the audit `0003` one, `runworker` unchanged |
| `test_ai_settings_audit.py` | 32 | one event per successful save with the contract fields, none for validation failure, refusal, stale revision, lost race or a read, rollback when the audit write fails, no secret in the event, the contract pins |
| `test_ai_settings_postgres.py` | not run | singleton and CHECK constraints, loader, round trip, revision conflict, concurrent first save, authorization against real users; the stored audit event, no event on refusal/400/409, a real rollback, ORM immutability, audit `0003` and its reverse guard |

Existing tests edited (pins and patches only): `test_api_boundaries`, `test_module_boundaries`, `test_request_intake_django`, `test_settings`, `test_intake_ai_composition`, `test_intake_composition`, `test_tasks_worker`, and, for the new audit vocabulary and migration, `test_audit_vocabulary`, `test_review_audit`, `test_workflow_attribution`, `test_intake_ai_boundaries` (its audit-vocabulary pin now ignores the `ai_settings` names only).

### 16.9 Audit event for a settings change

| Item | Value |
| --- | --- |
| Action / subject type | `ai_settings.updated` / `ai_settings` (a Core action; the `ai_settings` namespace is reserved against modules) |
| Subject id | the constant `SETTINGS_SUBJECT_ID` (a UUIDv5 of `urn:automation-platform:ai-settings:extraction`): there is one settings record |
| Actor | kind `user`, `actor_public_id` = the administrator's `User.public_id` (never a client-supplied value) |
| Timestamp | `occurred_at`, set by the audit row (`auto_now_add`) |
| Details (exactly these keys) | `revision` (the new revision), `extraction_mode`, `provider_id`, `profile_id`, `policy_mode`, `timeout_seconds` (the AI fields are `null` for a deterministic save), and, since AI-SET-2, `connection_id` (the selected connection; `null` for the legacy, environment-configured provider or a deterministic save), `connection_count` (the number of stored connections) and `connections_changed` (whether this save created, changed or removed a connection). The three AI-SET-2 keys are additive and backward-compatible: the six original keys keep their names and meaning, events recorded before AI-SET-2 simply lack the three new keys and stay valid, and a save with no connections records `null`, the stored count and `false` (section 19.7) |
| Not recorded | any key, header, proxy URL, charge code, endpoint or text; the previous values; `case_id` and `run_id` are empty |
| Transaction | written with `apps.audit.recorder.record_event` inside the save's `transaction.atomic()`, after the row write: they commit or roll back together, and a failed audit write rolls the change back (the error propagates; the API answers 500) |
| No event | validation error, authorization refusal, stale revision (409), a lost first-save race, a read |

Detail values are checked by the existing event builder (booleans, integers, null or short identifier tokens), so free text and secret-shaped values are refused.

**Migration `audit.0003_ai_settings_subject_type`** (depends on `0002`): the subject-type CHECK `audit_event_subject_type_valid` gains `ai_settings`. The action CHECK is shape-only and needs no change. No row is read, changed or deleted; PostgreSQL scans the existing rows while it adds the constraint. **Reverse** restores the previous list only while no `ai_settings` event is stored; otherwise it raises `IrreversibleError` naming a count, and never deletes an event. The migration has not been executed against PostgreSQL by the development VM (the owner has not yet run it).

**Runtime:** unchanged. The audit event is written by the web process at save time; a saved change still reaches the worker only after a worker restart (16.5).

## 17. OpenAI-compatible provider (AI-CON-1)

> **Status: written and DB-free tested in the development VM only (157 new tests, 2998 DB-free tests in all, OK), with a MOCKED transport, a fake resolver and a fake connection factory. No request has been sent to any real endpoint, no server (local, company or cloud) has ever answered this adapter, no key exists in the repository, and the Ollama-native API, a frontend, document upload and further cloud providers are not part of it. The provider is opt-in and not used by anything in the application. A read-only endpoint security review found three risks, all fixed in this slice (17.10): hostname endpoints are refused through a proxy, a company endpoint is confined to explicitly approved CIDR ranges, and an explicit denylist of special-purpose and metadata ranges applies to company and cloud endpoints.**

### 17.1 What was added, and what was not

Added: the provider `openai_compatible` (`apps/ai_gateway/adapters/openai_compatible.py`), the pure destination rules (`apps/ai_gateway/network.py`), a `network_class` field and an `approved_networks` field on `HttpRequest` and a transport error kind `destination` (`transport.py`), the guarded real transport (`config/ai_guarded_transport.py`) and the opt-in composition (`config/ai_openai_compatible.py`, variables `AP_AI_OPENAI_*`).

Not added: any change to Request Intake extraction, the AI settings catalog (still the Workbench only) or the worker; any setting, table, migration, endpoint or frontend; Ollama's native API (added later by AI-CON-2, section 18); `json_schema` (native structured output); streaming; tool or function calling; embeddings; a credential store; a stored company policy; a second provider instance per process. `config/ai_gateway.py` still has empty tuples, the deterministic default is unchanged, and the Workbench adapter, composition and transport behave as before (an `HttpRequest` without a `network_class` is exactly the old request; the Workbench transport now refuses a request that asks for address rules it cannot enforce).

### 17.2 Supported endpoint types

A server that implements `POST {base}/chat/completions` in the OpenAI chat-completions shape and honours `response_format: {"type": "json_object"}`: for example a local vLLM, llama.cpp server or Ollama's `/v1` surface, or a company gateway in front of such a model. Three explicit localities:

| Declared locality | Endpoint | Scheme | Key | Reaches (resolved addresses) | Profile approved for |
| --- | --- | --- | --- | --- | --- |
| `local` | `localhost` or a loopback literal (`127.0.0.0/8`, `::1`) only | `http` or `https` | optional | loopback only | `local_only` |
| `company_endpoint` | a company host name or a literal inside an approved CIDR range | `https` only | required | ONLY addresses inside `AP_AI_OPENAI_COMPANY_CIDRS` (required, no default); never a denied special-purpose range, whatever is approved | `company_endpoint` |
| `cloud` | a public host name or a global literal | `https` only | required | global only | `approved_cloud` (the provider id must also be in the policy's approved cloud providers) |

A server on a LAN is a `company_endpoint` with TLS, not a `local` one: `local` means this machine.

### 17.3 Provider contract

* **Identity.** `provider_id` `openai_compatible`, kind `openai_compatible_chat`, locality = the operator's declaration (never inferred from a name), `test_only` false. One endpoint and one model per process; the one profile is `openai-compatible`. There is no second profile, provider or model to fall back to.
* **Capabilities.** `json_mode` only; `temperature`, `top_p` and `seed` are accepted and sent only when the profile sets them; input and output token limits come from the configuration (defaults 32 000 and 4 096; they are the operator's limits, not a claim about the model).
* **Request.** `POST {endpoint}/chat/completions`, headers `Content-Type`, `Accept` and, when a key exists, `Authorization: Bearer <key>`. Body: `model` (the configured identifier), `messages` (system: the instructions plus the output JSON Schema in words; user: the input text), `response_format` `json_object`, `stream` false, `max_tokens` (the call's output limit), plus the optional sampling parameters.
* **Response.** The answer is `choices[0].message.content`; `model` is recorded as the reported model (`unreported` when absent, never replaced by the requested one); `usage.prompt_tokens` and `usage.completion_tokens` become the token counts when they are non-negative integers. `finish_reason` `length` is `invalid_structured_output`; any other value than `stop` or none, a refusal or no content is `provider_error`. The gateway validates the structure against the schema in every case.
* **Errors** (adapter code, reason token; no server text is ever copied): 401 and 403 `authentication_failure`; 404 `unsupported_model`; 408 and a transport timeout `timeout`; 429 `rate_limit` (retried); 502, 503, 504 and a refused connection `provider_unavailable` (retried); 3xx `provider_error` / `redirect_refused`; a TLS failure, a protocol failure or a refused destination `provider_error`; everything else `provider_error` with `http_<status>`. The gateway reports adapter reasons as `provider_failed`; the finer token is visible only to code that calls the provider directly (the tests do).
* **Retries.** At most `max_retries` (default 2, maximum 5) further attempts to the same endpoint and model, only for a refused connection, 429, 502, 503 and 504, with back-off, all inside the call's deadline. A timeout is not retried (the model may have received the request).
* **Provenance.** The gateway's `Provenance` records the provider id and kind, the locality, the policy mode, the requested and the reported model, the duration and the token counts; never a prompt, input, output, key or endpoint.

### 17.4 Endpoint security

| Control | Where | What it does |
| --- | --- | --- |
| Explicit allowlist | `OpenAICompatibleConfig` | The endpoint's origin (`scheme://host:port`) must be one of `AP_AI_OPENAI_ALLOWED_ORIGINS`; there is no default, so a changed endpoint alone sends nothing. The built URL is checked against the list again immediately before every request. Each listed origin must itself suit the declared locality. |
| No arbitrary URLs | `network.parse_url` | A strict grammar: `http(s)`, a lower-case host, an optional port from 1 to 65535, a path of plain characters only; no credentials (`user@`), query, fragment, percent-encoding, `..`, `//`, backslash or whitespace. Numeric host spellings (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`) and IPv6 zone identifiers are not hosts. |
| TLS | the grammar, the config, the transport | `https` is required for every `company_endpoint` and `cloud` endpoint. Certificates and host names are always verified (no switch to turn it off). |
| Plain `http` | the config, the transport | Only for a `local` endpoint whose host is `localhost` or a loopback literal, and only with the `loopback` network class. |
| Locality mismatch | `origin_problem` | A loopback host is never `company_endpoint` or `cloud`; a non-loopback host is never `local`; a literal address must be one its locality may reach (for example the cloud metadata address `169.254.169.254` is refused for both). |
| Resolved addresses (SSRF, DNS rebinding) | `config/ai_guarded_transport.py` with `network.address_permitted` | The transport resolves the host itself and refuses the call unless EVERY address it may use is permitted for the request's network class (company: inside an approved range; cloud: global; both: not in the denylist); IPv4-mapped IPv6 addresses are judged as the IPv4 address. It then connects to a validated address, not to the name (the name is still what `Host` and certificate verification use), so a second lookup cannot return something else. |
| Approved company ranges | `network.parse_networks`, `OpenAICompatibleConfig`, the transport | `AP_AI_OPENAI_COMPANY_CIDRS` is required for `company_endpoint` and refused for `local` and `cloud`. Plain CIDR notation only (no host bits, no netmask form), no broader than /8 (IPv4) or /16 (IPv6), and no range that overlaps a denied range, at most 20. The ranges travel with every request (`HttpRequest.approved_networks`); without them the `internal` class reaches nothing. A literal company endpoint must lie inside them. A public address is a company address only if the company approved the range that holds it: a public cloud host cannot become a company endpoint by declaration alone. |
| Special-purpose addresses | `network.DENIED_NETWORKS` | Written out, not derived from `ipaddress.is_private` or `is_global`, and applied BEFORE any approved range, to company and cloud: `0.0.0.0/8`, `100.64.0.0/10` (includes `100.100.100.200`), `127.0.0.0/8`, `169.254.0.0/16` (the metadata address `169.254.169.254`, `169.254.170.2`), `168.63.129.16/32` (Azure), `192.0.0.0/24`, `192.0.2.0/24`, `192.88.99.0/24`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/4`, `240.0.0.0/4`; IPv6 `::/96` (unspecified, loopback, IPv4-compatible), `::ffff:0:0:0/96` (IPv4-translated), `64:ff9b::/96` and `64:ff9b:1::/48` (NAT64), `100::/64`, `2001::/23` (Teredo and the IETF ranges), `2001:db8::/32`, `2002::/16` (6to4), `3fff::/20`, `5f00::/16`, `fd00:ec2::/32` (AWS), `fe80::/10`, `fec0::/10`, `ff00::/8`. IPv4-mapped forms are judged as the IPv4 address they carry, so `::ffff:169.254.169.254` is refused too. |
| Redirects | the transport and the adapter | `http.client` follows none; a 3xx answer is refused (`redirect_refused`) and never retried, so the key cannot follow a redirect elsewhere. |
| Proxy | the config and the transport | Only an explicit `AP_AI_OPENAI_PROXY_URL`; `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are never read. **A proxy is refused for every host-name endpoint** (`proxy_not_allowed_for_hostname_endpoint` at configuration, `destination` in the transport): the proxy would resolve the name where no address rule can be applied, and there is no acknowledgement, flag or other bypass. It stays possible only for an IP-literal endpoint, which is validated locally like any address before the tunnel is opened. A `local` endpoint with a proxy is refused at configuration (`proxy_not_allowed_for_local`) and, in the transport, a loopback request is refused while a proxy is set: it is never silently sent around the proxy or through it. |
| Transport that cannot enforce | `HttpRequest.network_class` | A transport that gets a request carrying a network class and cannot enforce it refuses it (`destination`); the Workbench transport does exactly that. |

### 17.5 Data locality

The locality is the operator's explicit declaration (`AP_AI_OPENAI_LOCALITY`, no default) and the provider reports it in its descriptor. It is cross-checked against the host (17.4) but never derived from the name: a host called `local.corp.example` declared `local` is refused, and so is a loopback host declared `company_endpoint`. The gateway then applies the policy before any adapter code runs, on every call: `local_only` permits only `local`; `company_endpoint` permits `local` and `company_endpoint`; `approved_cloud` additionally permits `cloud`, and only when the provider id is in the context's approved cloud providers. There is no fallback to a provider of another locality. The profile's own policy mode is the one its locality needs (`local_only`, `company_endpoint` or `approved_cloud`), so the policy of the profile and the policy in force must both allow the locality.

### 17.6 Configuration

Backend environment only, read by `config/ai_openai_compatible.py` when a function runs (never at import, never through Django settings, never from a database; `ModelProfile` holds no endpoint or secret). Required, no defaults: `AP_AI_OPENAI_ENDPOINT`, `AP_AI_OPENAI_ALLOWED_ORIGINS`, `AP_AI_OPENAI_LOCALITY`, `AP_AI_OPENAI_MODEL`; required when the locality is `company_endpoint` (and refused for `local` and `cloud`): `AP_AI_OPENAI_COMPANY_CIDRS`, comma-separated CIDR ranges such as `10.20.0.0/16,192.168.40.0/24`; required unless the locality is `local`: `AP_AI_OPENAI_API_KEY`. Optional: `AP_AI_OPENAI_TIMEOUT_SECONDS` (90), `AP_AI_OPENAI_MAX_RETRIES` (2), `AP_AI_OPENAI_MAX_INPUT_TOKENS` (32000), `AP_AI_OPENAI_MAX_OUTPUT_TOKENS` (4096), `AP_AI_OPENAI_PROXY_URL` (IP-literal endpoints only), `AP_AI_OPENAI_CA_BUNDLE`. A missing or invalid value raises `configuration_error` naming the variable or a field token (`missing_ap_ai_openai_company_cidrs`, `invalid_openai_approved_networks`, `openai_approved_networks_not_applicable`, `proxy_not_allowed_for_hostname_endpoint`, ...), never the value. The test runner removes every `AP_AI_*` variable, so a developer's `.env` cannot reach a test.

### 17.7 Credential handling

The key is held in `OpenAICompatibleConfig.api_key` (excluded from `repr` and comparison), used only to build the `Authorization` header of an `HttpRequest` (whose `repr` excludes headers and body), and appears in no error, message, reason, log line, profile or settings `repr`. A foreign exception from a transport is classified and dropped, and a server's error body is never copied. The key is never stored in the database or returned by any endpoint. A key is optional for a `local` endpoint only; for the other localities its absence stops configuration (fail closed).

### 17.8 Limitations (remaining)

* **Never run against a real server.** Whether vLLM, llama.cpp, Ollama's `/v1` or a company gateway accepts this exact request (in particular `response_format` `json_object` and `max_tokens`) is unverified; servers differ, and some ignore `json_object`. The gateway still validates every answer, so a server that ignores it fails closed with `invalid_structured_output` rather than returning unchecked data.
* **Not wired in (as of AI-CON-1; superseded by AI-SET-2).** When this was written, Request Intake extraction, the AI settings API and its catalog, and modules did not use this provider. Since AI-SET-2 (section 19) an administrator-declared connection of type `openai_compatible` can be selected for Request Intake; modules still cannot use it.
* **One endpoint and one model per process,** and one profile; no per-company policy, no stored approval record, no credential store, no key rotation.
* **Name resolution is outside the deadline.** `getaddrinfo` cannot be given a timeout. The address rules protect the connection this process makes; they are not a firewall and do not replace network egress rules or a vetted endpoint.
* **Proxy use is narrow.** Because a proxy is refused for host-name endpoints (17.4), a company or cloud endpoint reached through a proxy-only network cannot use this provider until it is given as an IP literal (certificate verification then needs an IP subject alternative name) or the network allows a direct connection.
* **Approved ranges are the operator's word.** The code confirms that every resolved address lies in the ranges the operator approved and in no denied range; it cannot tell whether the operator's ranges are the right ones, that a host inside them is the company's own, or that a company gateway forwards data nowhere else. A range that contains only a public address the company does not own would be accepted if it was approved on purpose. Company ranges inside `100.64.0.0/10` (carrier-grade NAT) are refused and cannot be approved.
* **The denylist is a fixed list.** It names the metadata and special-purpose ranges known today; a provider-specific address outside every listed range (a new metadata address, an address a cloud provider reserves inside an otherwise public range) is not caught for a `cloud` endpoint, whose only other rule is "global". `ipaddress` classifications can also differ between Python versions, which is why the list is explicit.
* **Locality is a declaration.** The code can check the host and the resolved addresses against what is declared; it cannot observe where a company gateway forwards data, or that a "local" server on loopback is not itself a tunnel.
* **`json_schema` structured output, streaming, tool calling, a `Retry-After` header and token budgets beyond the profile's limits are not supported.** Provisional, unmeasured limits.
* **Cooperative timeouts and no sandbox,** as in section 12.

### 17.9 Tests

DB-free, in the development VM, mocked HTTP only, no PostgreSQL, no network, no key: 157 new tests (2998 DB-free tests in all, OK).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_network.py` | 24 | the URL grammar (credentials, query, fragment, encoding, numeric spellings, ports, paths, no echo), the approved-range rules (reach, none approved, a broken list, the denylist beating an approved range, about 55 special-purpose and metadata addresses, ordinary neighbours not caught by mistake) and the strict CIDR parser (malformed, too broad, touching a denied range) |
| `test_ai_openai_adapter.py` | 59 | local and corporate configuration, required, malformed and refused CIDR ranges, literal company endpoints against the ranges, special-purpose literals, locality mismatches, the approval list, invalid destinations, bearer authentication and its absence, the request body and the ranges that travel with it, structured JSON and invalid output, status and transport error classification, redirects, rate limits and retries, deadlines, no fallback, the policy matrix, no secret in errors, logs or reprs |
| `test_ai_openai_transport.py` | 38 | SSRF and rebinding rules with a fake resolver (loopback, approved and unapproved ranges, public answers, mixed answers, special-purpose and metadata answers, literals), a hostile resolver that answers differently on later calls, plain http for loopback only, connection to the validated address, fail-over, proxy refusal for every host name and the IP-literal exception, redirects, timeouts, the response cap, fixed error kinds, the pinned connection classes, the Workbench transport's refusal |
| `test_ai_openai_composition.py` | 36 | environment loading that fails closed and names only variables (including the CIDR variable and the proxy refusal with no bypass variable), profiles by locality, configuration -> adapter -> guarded transport end to end with a fake resolver, the deterministic default, the Workbench and the AI settings catalog unchanged |

Existing tests edited (lists and one pin only): `test_ai_gateway_boundaries`, `test_ai_gateway_transport_contract`, `test_intake_ai_boundaries`.

### 17.10 Endpoint security review and its three fixes

A read-only review of 17.1 to 17.9 (before these fixes the suite had 2962 tests) found three confirmed risks. All three are fixed, with no change to Request Intake, the AI settings, the Workbench or the deterministic default:

1. **A proxy resolved host names where no address rule could see them (Medium in principle).** A proxy is now refused for every host-name endpoint, in the configuration (`proxy_not_allowed_for_hostname_endpoint`) and again in the transport (`destination`), with no acknowledgement or other bypass. IP-literal endpoints may still use a proxy and are validated locally first. Direct, validated, pinned connections are unchanged.
2. **A `company_endpoint` accepted any public address (Medium).** `private or global` is gone: a company endpoint now reaches only addresses inside the CIDR ranges in `AP_AI_OPENAI_COMPANY_CIDRS`, which is required, has no default and is refused for the other localities. Every resolved address is checked against them at connection time; a missing, malformed, too broad or denied-range-touching list stops configuration. A public cloud host can no longer be classified as a company endpoint by declaration alone; TLS verification is unchanged.
3. **Special-purpose and metadata ranges were only partly covered (Low).** `network.DENIED_NETWORKS` is an explicit list (17.4) applied before any approved range and to `cloud` as well, instead of relying on `is_private` and `is_global`. It adds `0.0.0.0/8`, `198.18.0.0/15`, `192.0.0.0/24`, `192.88.99.0/24`, `168.63.129.16`, `fd00:ec2::/32`, the documentation ranges and the IPv6 transition forms to the loopback, link-local and metadata ranges already refused.

Not changed: the Workbench transport and adapter (the Workbench request carries no network class), Request Intake, the AI settings catalog (still the Workbench only), the local-only behaviour (loopback only, plain http only there) and the deterministic default.

## 18. Native Ollama provider (AI-CON-2)

> **Status: written and DB-free tested in the development VM only (84 new tests, 3082 DB-free tests in all, OK), with a MOCKED transport, a fake resolver and a fake connection factory. No request has been sent to any Ollama server, no Ollama has ever answered this adapter, nothing was installed or started, no model was downloaded and no database was touched. The provider is opt-in and not used by anything in the application: Request Intake extraction, the AI settings catalog (Workbench only), the worker, modules and the frontend are unchanged, and `config/ai_gateway.py` still has empty tuples.**

### 18.1 What was added, and what was not

Added: the provider `ollama` (`apps/ai_gateway/adapters/ollama.py`, pure, standard library only) and its opt-in composition (`config/ai_ollama.py`, variables `AP_AI_OLLAMA_*`). It reuses, unchanged, the gateway contracts, the policy rules, the destination grammar in `network.py` and the guarded transport (`config/ai_guarded_transport.py`); no existing module of the gateway, the Workbench, the OpenAI-compatible provider or the transport was edited.

Not added: wiring into Request Intake extraction, the AI settings API or its catalog (multi-provider settings are a later, reviewed change); a frontend; document upload; any model download, creation, deletion or listing (`/api/pull`, `/api/create`, `/api/delete`, `/api/tags` are never called); starting, stopping or installing Ollama or changing a system service; a remote Ollama host (use `openai_compatible` against its `/v1` surface with TLS, a key and approved ranges); streaming, tool calling, embeddings, vision, `keep_alive` and `think`; a credential store or a per-company policy; a second Ollama instance or model per process.

### 18.2 Provider contract

* **Identity.** `provider_id` `ollama`, kind `ollama_native_chat`, locality `local` (a constant, not a setting, so there is no declaration to get wrong), `test_only` false. One endpoint and one model per process; the one profile is `ollama-local`, approved for `local_only`. There is no second profile, model or provider to fall back to.
* **Capabilities.** `json_mode` and `native_schema`; `temperature`, `top_p` and `seed` are accepted and sent only when the profile sets them; the token limits come from the configuration (defaults 3 072 input and 1 024 output, derived from Ollama's small default context window; they are the operator's limits, not a claim about the model).
* **Request.** `POST {endpoint}/api/chat`, headers `Content-Type` and `Accept` only (no `Authorization`: Ollama's native API has no key here). Body: `model` (the configured identifier), `messages` (system: the instructions plus the output JSON Schema in words; user: the input text), `stream` `false`, `format` and `options`. `format` is the string `"json"` for `json_mode` and the output JSON Schema itself for `native_schema` (Ollama's structured outputs). `options` always holds `num_predict` (the call's output limit) and holds `temperature`, `top_p` and `seed` only when the profile sets them and `num_ctx` only when `AP_AI_OLLAMA_NUM_CTX` is set. Nothing else is sent.
* **Response.** The answer is `message.content` of a single non-streamed JSON document with `done` true. `model` is recorded as the reported model (`unreported` when absent, never replaced by the requested one); `prompt_eval_count` and `eval_count` become the token counts when they are non-negative integers. `done_reason` `length` is `invalid_structured_output`; `done` not true, an `error` field, no content or any other `done_reason` than `stop` or none is `provider_error`. The gateway validates the structure against the schema in every case; nothing is repaired.
* **Errors** (adapter code, reason token; no server text is ever copied, in particular not Ollama's `error` string, which names the model): 401 and 403 `authentication_failure` (a reverse proxy in front of Ollama); 404 `unsupported_model` (the model is not installed, or the route is not there: the model is never pulled); 408 and a transport timeout `timeout`; 429 `rate_limit` (retried); 502, 503 (Ollama's "server busy") and 504 and a refused connection (Ollama is not running) `provider_unavailable` (retried); 3xx `provider_error` / `redirect_refused`; a TLS, protocol or refused-destination failure `provider_error`; everything else `provider_error` with `http_<status>`.
* **Retries.** At most `max_retries` (default 2, maximum 5) further attempts to the same endpoint and model, only for a refused connection, 429, 502, 503 and 504, with back-off, all inside the call's deadline. A timeout is not retried.
* **Provenance.** The gateway's `Provenance` records the provider id and kind, the locality `local`, the policy mode, the requested and the reported model, the duration and the token counts; never a prompt, input, output or endpoint.

### 18.3 Model selection

The model is explicit: `AP_AI_OLLAMA_MODEL` has no default, and the provider supports exactly that one identifier (for example `llama3.1:8b`). A call for any other model is refused before a request is made (`unsupported_model`). If the model is not installed, Ollama answers 404 and the call fails with `unsupported_model`; nothing is downloaded and no other model is tried. Installing a model is the operator's own action (`ollama pull`, outside this application).

### 18.4 Local endpoint security

| Control | Where | What it does |
| --- | --- | --- |
| Loopback only | `OllamaConfig`, `origin_problem` | The endpoint and every approved origin must be `localhost` or a loopback literal (`127.0.0.0/8`, `::1`). A LAN address, a company or cloud host, `0.0.0.0`, a `*.localhost` name or a host that only SOUNDS local is refused at configuration, even when listed as approved. There is no switch to relax this. |
| Explicit approval | `OllamaConfig` | The endpoint's origin must be in `AP_AI_OLLAMA_ALLOWED_ORIGINS` (no default); the built URL is checked against the list and must be the `/api/chat` path again immediately before every request. |
| No arbitrary URLs | `network.parse_url` | The strict grammar of 17.4: no credentials, query, fragment, percent-encoding, `..`, numeric host spellings or zone identifiers. |
| Resolved addresses | `config/ai_guarded_transport.py` | Every request carries the `loopback` network class: the transport resolves the host itself, refuses the call unless EVERY address is loopback (a `localhost` that resolves to a LAN or public address sends nothing), and connects to a validated address, not to the name. |
| Redirects | the transport and the adapter | None is followed; a 3xx answer is refused (`redirect_refused`). |
| Proxy | `config/ai_ollama.py`, the transport | There is no proxy setting. `AP_AI_OLLAMA_PROXY_URL` is REFUSED when set (`proxy_not_allowed_for_local`); `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are never read; the real transport is built with no proxy, and the guarded transport would in any case refuse a loopback request while one is configured. A loopback request is never sent through a proxy or around one. |
| No credentials | the grammar, the profile | A URL with credentials is refused without echo; the provider has no key; the profile holds no endpoint or secret. |
| No work at import | `config/ai_ollama.py` | Importing reads no environment and opens no connection; constructing the transport or the gateway resolves and connects to nothing; a call is the only thing that does. |
| No cloud | the policy, the adapter | The provider is `local`, so no policy mode needs to approve a cloud provider for it, and there is no code path that sends to a non-loopback address. |
| No model or service management | the adapter | Only `/api/chat` is ever called (pinned by a test over the adapter's string constants); Ollama is never started, installed or configured. |

### 18.5 Data policy

The provider's locality is `local`: `local_only` permits it, `company_endpoint` permits it (cumulative rule), and `approved_cloud` permits it without naming it in the approved cloud providers (that list applies to `cloud` providers only). The profile is approved for `local_only` and the gateway applies the installation's mode on every call before any adapter code runs. Nothing falls back to another provider: when Ollama is down the call fails with `provider_unavailable`, whichever mode is in force.

### 18.6 Configuration

Backend environment only, read by `config/ai_ollama.py` when a function runs (never at import, never through Django settings, never from a database). Required, no defaults: `AP_AI_OLLAMA_ENDPOINT` (for example `http://127.0.0.1:11434`; `/api/chat` is appended), `AP_AI_OLLAMA_ALLOWED_ORIGINS`, `AP_AI_OLLAMA_MODEL`. Optional: `AP_AI_OLLAMA_STRUCTURED_OUTPUT` (`json_mode`, the default, or `native_schema`), `AP_AI_OLLAMA_TIMEOUT_SECONDS` (90), `AP_AI_OLLAMA_MAX_RETRIES` (2), `AP_AI_OLLAMA_MAX_INPUT_TOKENS` (3072), `AP_AI_OLLAMA_MAX_OUTPUT_TOKENS` (1024), `AP_AI_OLLAMA_NUM_CTX` (unset by default; when set it is sent as `num_ctx` and the two token limits must fit inside it). A missing or invalid value raises `configuration_error` naming the variable or a field token (`missing_ap_ai_ollama_model`, `invalid_ollama_allowed_origins`, `ollama_endpoint_not_approved`, `ollama_limits_exceed_num_ctx`, `proxy_not_allowed_for_local`, ...), never the value. The test runner removes every `AP_AI_*` variable, so a developer's `.env` cannot reach a test.

### 18.7 Limitations (remaining)

* **Never run against a real Ollama.** Whether the exact request is accepted (in particular `format` with a JSON Schema, which needs a recent Ollama release and a model that supports it, and `options.num_predict`) and what a given release reports in `model`, `done_reason` and the counts is unverified. `json_mode` is the default because it is the more widely supported; the gateway validates every answer, so a server that ignores `format` fails closed with `invalid_structured_output`.
* **Silent prompt truncation is not detectable here.** Ollama truncates a prompt that does not fit its loaded context window and still answers. The adapter cannot see the window; the defaults (3 072 input, 1 024 output) assume a 4 096-token window, and `AP_AI_OLLAMA_NUM_CTX` plus a configuration check ties the limits to a declared window. The gateway's own pre-flight estimate (bytes divided by three) is a guard, not a tokenizer.
* **Small local models are unmeasured.** There is no evaluation set; a small model may return syntactically valid but wrong values. Validation and human review apply as for every AI output.
* **Loopback only.** A GPU server on a LAN or a shared company host is not supported by this provider; that is a `company_endpoint` and goes through `openai_compatible` (17) or a later reviewed slice.
* **Not wired in (as of AI-CON-2; superseded by AI-SET-2).** When this was written, Request Intake, the AI settings API and its catalog and modules did not use it; one endpoint and one model per process. Since AI-SET-2 (section 19) a stored connection of type `ollama` can be selected for Request Intake (one model per profile, one gateway per worker process); modules still cannot use it.
* **Locality is a code-level rule, not a firewall.** The code confirms the destination is loopback; it cannot tell what another local process listening on that port does (a local Ollama could itself be configured to forward requests). Name resolution is not bounded by the deadline, and timeouts are cooperative (section 12).
* **Model files are the operator's responsibility.** Provenance records the model name Ollama reports, not a digest of the weights.

### 18.8 Tests

DB-free, in the development VM, mocked HTTP only, no PostgreSQL, no network, no Ollama: 84 new tests (3082 DB-free tests in all, OK).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_ollama_adapter.py` | 54 | loopback-only configuration (every non-loopback host refused even when approved), the origin allowlist, credentials and odd URLs without echo, the explicit model, the native request on the wire (path, headers, body keys, `format`, `options`, not the OpenAI shape), model selection, structured JSON and invalid output, truncation and incomplete answers, status and transport classification (a missing model is `unsupported_model`), retries and deadlines, no fallback, no route but `/api/chat`, the policy matrix, no secret or content in errors, logs or reprs |
| `test_ai_ollama_composition.py` | 30 | environment loading that fails closed and names only variables, proxy refusal with no bypass, profile and gateway, configuration -> adapter -> guarded transport end to end with a fake resolver (loopback dialled only, `localhost` resolving elsewhere refused, a proxy refused, redirects refused, proxy environment variables ignored), no network at import, the deterministic default, the Workbench and the OpenAI-compatible provider unchanged |

Existing tests edited (file lists only): `test_ai_gateway_boundaries`, `test_intake_ai_boundaries`.

## 19. Multi-provider AI settings: named connections (AI-SET-2)

> **Status: written and DB-free tested in the development VM only (168 new tests, 3250 DB-free tests in all, OK), with a MOCKED transport, a fake resolver where a transport is built and no network. Its PostgreSQL tests (`tests/test_ai_connections_postgres.py`, and the changed expectation in `tests/test_ai_settings_postgres.py`) are written and have NEVER been run: PostgreSQL validation is pending. No request was sent to any provider, no credential exists in the repository, there is no frontend, and nothing is production-ready.** Branch `feat/ai-multi-provider-settings`; not committed.

### 19.1 What was added, and what was not

Added: a `connections` list and a `connection_id` selection in the one AI settings row (`ai_settings.0002_connections`, additive and reversible); pure connection rules in `apps/ai_settings/rules.py`; the composition `config/ai_connections.py` (the static list of three provider types, the status probe, the gateway builder and the credential-reference resolver); an extended `GET`/`PUT /api/ai-settings/extraction/`; a worker selection in `config/intake_extraction.py`; a second audit action (`ai_settings.connection_changed`); tests and documentation.

Not added: a frontend, document upload, a credential store (the secret stays in the process environment), hot reload, a company-level policy record, an approval step for a new connection, a worker status report, a model list or model download, an Ollama or OpenAI-compatible probe, a second gateway per process, more than one selected connection, any fallback, or any change to the gateway package, the adapters, the guarded transport, Request Intake's extraction path, review, approval, revisions, the queue or the module gate.

### 19.2 The connection model

A connection (`apps.ai_settings.rules.ConnectionValues`) has exactly these fields:

| Field | Meaning |
| --- | --- |
| `connection_id` | stable identifier, `[a-z][a-z0-9_-]{0,47}`; unique in the list |
| `display_name` | label for people, 1 to 80 printable characters; never audited or logged |
| `provider_type` | one of `kpmg_workbench`, `openai_compatible`, `ollama` (a static list built by `config/ai_connections.py`; anything else is `unknown_provider_type`) |
| `locality` | `local`, `company_endpoint` or `cloud`; the administrator's declaration, checked against the provider type and the endpoint (19.4) |
| `endpoint` | the base URL for `openai_compatible` and `ollama`; null for the Workbench (its endpoint stays the operator's, allowlisted by the adapter) |
| `credential_ref` | the NAME of a backend environment variable (`AP_AI_CRED_*`) or null; never a value (19.5) |
| `enabled` | a disabled connection cannot be selected and stops a worker whose stored selection names it |
| `profiles` | the model profiles the connection exposes: `{profile_id, model_id}`, at most 10, unique `profile_id` |

At most 10 connections. The list is stored in the same singleton row as the selection (`connection_id` and the existing `provider_id`, `profile_id`, `policy_mode`, `timeout_seconds`), so a save changes the list, the selection and the revision together under one row lock, and writes its audit events in the same transaction. `provider_id` is kept, and for a connection it is the connection's provider TYPE (derived; if sent it must match).

**Profiles.** For `kpmg_workbench` the profiles are fixed by the catalog (the Workbench profiles Request Intake approves; today `workbench-gpt5` with its deployment): the administrator may only pick among them. For `openai_compatible` and `ollama` the administrator declares the model identifiers a connection may call (the gateway's own model-id pattern); each profile is one model, and the worker builds a gateway for the SELECTED profile only. A profile that names an Ollama model does not download, list or pull it.

**Two selection paths.** `connection_id` null is the legacy path, unchanged: `provider_id` is `kpmg_workbench`, and the key and every other value come from `AP_AI_WORKBENCH_*` exactly as before (existing rows and clients keep working; the migration changes no existing row). `connection_id` set selects a stored connection. Nothing converts one into the other.

### 19.3 Provider selection and the worker

`prepare_worker` still reads the stored row once at start-up; `load_stored_settings` now also returns the connections that have a readable structure. `config/intake_extraction.settings_from_stored` validates the selection with the same `validate_values` the API used, against the same catalog, and then checks the SELECTED connection again (`invalid_connection` if it no longer passes). It stops the worker (`ExtractionConfigurationError`, a message of field names and codes only) when: the connection does not exist or is disabled, its profile is not among its profiles, it no longer passes the catalog, the data policy forbids its locality, its credential variable is not set, its endpoint is not in the operator's approved origins, a company range is missing, the lease cannot cover the deadline, or the module gate is missing. Other, unselected connections are not examined (a damaged unrelated connection does not stop the worker), and an unreadable item is never used.

The gateway is then built by `config/ai_connections.build_connection_gateway`: ONE provider (the connection's) and ONE profile (the selected one). There is no second endpoint, model, provider or locality to fall back to, no fallback to the environment-configured Workbench when a stored selection fails, and no fallback to deterministic extraction. The data policy is evaluated at start-up and again by the gateway on every call; Request Intake's `module_preflight` still re-checks that the module is claimable before any request leaves, and the queue's retry classes, leases and recovery are untouched (a provider failure maps to the same retryable and permanent codes). Approval and revision semantics are untouched: the answer is a candidate that goes through the existing validation, canonicalization, hashing, review and human approval, and `extractor_version` records the selected profile and the model the provider reports.

### 19.4 Policy and endpoint enforcement

* **Locality and policy.** Each provider type allows fixed localities: `kpmg_workbench` only `company_endpoint`; `ollama` only `local`; `openai_compatible` any of the three. The catalog lists, per locality, the policy modes the gateway permits (asked of `enforce_policy`, not written by hand): `local` runs under every mode, `company_endpoint` under `company_endpoint` and `approved_cloud`, `cloud` only under `approved_cloud`. Selecting a connection under a mode that forbids its locality is `policy_forbids_provider` (API and worker), and the gateway refuses it again before every provider call (`local_only` never reaches a company or cloud connection).
* **A cloud connection and `approved_cloud`.** The gateway requires a cloud provider to be named in the policy's approved providers. There is still no stored company policy, so selecting a `cloud` connection together with the policy mode `approved_cloud` IS the explicit approval: `policy_for` then names that provider type as approved. It is an administrator decision recorded in the `ai_settings.updated` audit event (connection, provider, policy mode), not an approval step with a second person.
* **Endpoint rules are the existing ones.** `config/ai_connections.py` runs `load_openai_compatible_settings` / `load_ollama_settings` / `load_workbench_settings` on a COPY of the environment in which the connection's endpoint, locality, model and key replace the provider's single-connection variables. The adapters' URL grammar, the locality rule, https for remote endpoints, plain http for loopback only, the guarded transport's resolved-address checks, the special-purpose denylist, no redirect, no implicit proxy and the Ollama loopback rule therefore apply unchanged. The save-time check is the same pure rule (shape, locality, loopback); it cannot know the operator's CIDR ranges, so a company endpoint that is an address literal is judged against them at start-up.
* **The operator, not the stored settings, decides what an endpoint may be.** Approved origins (`AP_AI_OPENAI_ALLOWED_ORIGINS`, `AP_AI_OLLAMA_ALLOWED_ORIGINS`), the company CIDR ranges (`AP_AI_OPENAI_COMPANY_CIDRS`), a proxy, a CA bundle, limits and retries stay environment variables. A stored endpoint whose origin is not approved there is refused (`endpoint_not_approved` in the status, a start-up error in the worker): a connection can never widen an allowlist. Those variables are shared by every connection of a type; for a locality only the origins that suit it are used, which can only NARROW the list (a loopback origin is dropped for a company connection; the company ranges and a proxy are dropped for a local one). A proxy is still refused for a host-name endpoint and for Ollama.
* **No model download, no new network behaviour.** Nothing lists, pulls or deletes a model; building a gateway and the status probe make no request.

### 19.5 Credential references

* A connection stores a REFERENCE: the name of an environment variable that must match `AP_AI_CRED_[A-Z0-9_]{1,48}`. The prefix means a reference can never name `AP_SECRET_KEY`, a database password, another provider's `AP_AI_*_API_KEY` or `PATH`; the shape is checked when saved and again where it is resolved.
* The value is read only in the backend process that needs it: by the worker when it starts (placed in the adapter's configuration object, which hides it from `repr`) and, only to test that it is set and acceptable, by the status probe of the web process. It is never in the database, a `ModelProfile`, an API request or response, an audit event, an error, a log line or Django settings. A request that carries `api_key`, `token`, `password`, `header` or any other unknown field is refused (`unknown_field`) and not echoed.
* A reference whose variable is not set is an error (`missing_credential_reference`; the status `missing_api_key`), never an unauthenticated call. `AP_AI_OPENAI_API_KEY` is never used for a stored connection.
* Credential requirement by type: Workbench optional (without a reference it reads `AP_AI_WORKBENCH_API_KEY` exactly as before; with one it uses that variable, so two Workbench connections can use two keys, but they share the other `AP_AI_WORKBENCH_*` settings), OpenAI-compatible required unless `local`, Ollama none (a reference is refused).
* This is still the interim environment mechanism, not the decided credential store (ai-policy.md, section 9).

### 19.6 API contract changes

Both methods keep their authorization (administrator only), CSRF, optimistic revision (`409 stale_settings_revision`), the restart notice (`apply: worker_restart_required`) and fixed-code errors. Added to the request: `connection_id` and `connections`. `connections`, when present, is the COMPLETE list and replaces the stored one; when absent the stored list is kept, so the old body keeps working. A client that read the settings must send the connection objects without the read-only `status` field (an unknown field is refused). Added to the response: `settings.connection_id`, `connections[]` (the fields above plus `status`: `{configured, issue}` with `issue` one of `missing_api_key`, `invalid_configuration`, `endpoint_not_approved` or null, as seen by the process that answers), `unreadable_connections` (a count; only a hand-edited row has any), and in `catalog`: `provider_types[]` (`provider_type`, `label`, `localities`, `needs_endpoint`, `credential`, `profile_source`, `profiles`), `locality_policies[]` and `credential_ref_prefix`.

Validation errors are keyed by path (`connections[0].endpoint`, `connections[1].profiles[0].model_id`) with fixed codes: `required`, `invalid_value`, `invalid_type`, `too_many`, `unknown_field`, `unknown_provider_type`, `locality_not_allowed_for_provider`, `invalid_endpoint`, `endpoint_locality_mismatch`, `plain_http_not_allowed`, `endpoint_not_loopback`, `destination_not_allowed`, `not_allowed`, `unknown_profile`, `model_not_of_profile`, `duplicate_connection_id`, `duplicate_profile_id`, and for the selection `unknown_connection`, `connection_disabled`, `invalid_connection`, `provider_not_of_connection`, `policy_forbids_provider`. A selected connection cannot be removed or disabled in the same save. A value is never echoed.

### 19.7 Audit

`ai_settings.updated` keeps its subject and now also carries `connection_id`, `connection_count` and `connections_changed`. A save that creates, changes or removes a connection also records one `ai_settings.connection_changed` event per connection (Core action in the reserved `ai_settings` namespace; subject type `ai_settings`, subject id a UUIDv5 of the connection id; details `revision`, `change` (`created`, `updated`, `removed`), `connection_id`, `provider_type`, `locality`, `enabled`, `has_credential_ref`, `profile_count`). Never recorded: an endpoint, a credential reference name, a display name, a model name, a secret. All events of a save commit or roll back with the row (a failed audit write, including the second one, rolls back the whole save); a refusal, validation error or stale revision writes none. No audit migration is needed (the action CHECK is shape-only; the subject type is unchanged). Audit immutability is still ORM-level only.

### 19.8 Migration and runtime

* **`ai_settings.0002_connections`** (depends on `0001`): adds `connection_id` (nullable) and `connections` (JSON, default empty list) and replaces the all-or-nothing CHECK by `ai_settings_all_or_nothing_v2`, which also requires `connection_id` to be null in the deterministic mode. It reads, writes and deletes no data (no `RunPython`, no `RunSQL`); every existing row satisfies the new constraint and keeps its meaning (no connection: the legacy provider). Reverse drops the two columns and restores the old constraint; a stored connection list is lost on reverse. **Reverse risk:** reversing `ai_settings.0002` removes `connection_id` and `connections`. An AI row that selected an `ollama` or `openai_compatible` connection then keeps `provider_id` of that type but no connection, and the worker of the previous version refuses to start (`unknown_provider`), because only the Workbench is a legacy provider; it fails closed and never falls back. Before any reversal: take a verified database backup; save the settings as `deterministic` mode or as the legacy Workbench selection (`provider_id` `kpmg_workbench`, `workbench-gpt5`, no `connection_id`) through the API, so no row depends on a connection; and do not silently discard active connections (record them first, because the reverse deletes the list and nothing restores it). The reverse has not been run against PostgreSQL. **Apply the migration before the worker starts**: the worker reads the new columns and an unmigrated table stops it (as in 16.5). It has not been run against PostgreSQL.
* **Worker restart.** Unchanged: a saved change, including a changed connection list, reaches a worker only when it starts; every response says `worker_restart_required`. The web process needs no restart for a saved change, but it must be restarted for a changed `AP_AI_*` or credential variable to show in its status probe, and the worker for the same variable to take effect. The operator must set the `AP_AI_CRED_*` variable in the WORKER's environment too: the status the API shows is the web process's view.

### 19.9 Limitations (remaining)

* Nothing here has run against a real provider, a real PostgreSQL server or a browser; the status of the PostgreSQL tests is "written, not run".
* **The operator's allowlists are shared per type and not labelled by locality.** An origin approved for one connection of a type is approved for every connection of that type in a matching locality; the guarded transport's address rules (and, for company endpoints, the CIDR ranges) are what stop a mislabeled host, not the origin list. Per-connection allowlists, proxy and CA settings, limits, retries, `num_ctx` and structured-output mode are not stored (they are the operator's, shared by type).
* Two Workbench connections share one endpoint, charge code, proxy and CA bundle. Request Intake approves only `workbench-gpt5`; approving another Workbench profile is still a reviewed code edit. Profiles of `openai_compatible` and `ollama` connections are approved by the administrator's own selection: there is no per-workflow approval list for them, no evaluation set and no accuracy measurement, and a small or local model may return wrong values (validation and human review still apply).
* A `cloud` selection under `approved_cloud` is its own approval (19.4); there is no stored company policy, no second-person approval and no approval record beyond the audit event.
* One extraction connection and profile per worker process; no per-run or per-company selection; no health check or heartbeat reports which revision a running worker uses.
* A connection cannot be tested from the API (a test would make a request); `status` only says that the variables are usable in that process.
* Login throttling per process; TLS, HSTS and the HTTPS redirect remain undecided; audit immutability is ORM-level; the credential store is open (ai-policy.md, section 9).
* A hand-edited row can hold a shape no check detects until the worker starts (the CHECK constraints do not inspect the connection list); the worker and the read treat any unreadable item as absent and refuse a selection that depends on it.

### 19.10 Tests

DB-free, in the development VM, mocked HTTP only, no PostgreSQL, no network: 168 new tests (3250 DB-free tests in all, OK).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_connections_rules.py` | 47 | several connections of every type, structure and shapes, unknown fields (no credential field can exist), provider types, localities, endpoints per locality, Ollama loopback, credential-reference prefix, profiles (fixed and declared), duplicates, selection, disabled and unknown connections, the policy-locality matrix |
| `test_ai_connections_composition.py` | 41 | provider and model selection through the real adapters and gateway over a recording transport, each provider's own endpoint and key, no cross-connection key, policy refusal before any request, cloud approval, operator allowlist narrowing never widening, proxy and CIDR refusals, credential resolution, status codes, no secret in profiles, reprs, errors or the probe, no network and no dynamic loading |
| `test_ai_connections_api.py` | 41 | services and API: one-transaction saves, replace and keep semantics, selection against the list, selected connection not removable, stale revision, lost race, refusal before validation, audit events and rollback, no secret in responses, logs or events, indexed error paths, the legacy body |
| `test_ai_connections_worker.py` | 28 | worker selection of each type and model, fail-closed refusals (missing, disabled, unapproved, no credential, policy, retired type, unapproved profile, lease, gate) with no fallback and no value in a message, the legacy Workbench path, module gate, failure classification, `prepare_worker`, no hot reload |
| `test_ai_connections_boundaries.py` | 10 | the settings app stays pure and provider-free, only the composition imports the builders, no environment read at import, no network library, no dynamic loading, `runworker` and settings unchanged |
| `test_ai_connections_postgres.py` | not run | persistence, atomic audit with a real rollback (including the second event), migration forward and reverse with an existing row, the new constraint, concurrent updates, the worker reading the real row and extracting through a selected connection |

Existing tests edited (pins and expected shapes only): `test_ai_settings_api`, `test_ai_settings_audit`, `test_ai_settings_boundaries`, `test_ai_settings_postgres`, `test_audit_vocabulary`, `test_review_audit`, `test_ai_gateway_boundaries`, `test_intake_ai_boundaries`, and the support module `ai_settings_support`.

## 20. Managed AI credentials (AI-SEC-2)

**Status: implemented in the backend, development-validated with DB-free tests only; its PostgreSQL tests are written and NOT run (PostgreSQL validation pending); nothing was sent to any provider; no frontend.** It uses the hardened Windows DPAPI secret store of [secret-store.md](secret-store.md); with the store disabled (the default) every managed credential fails closed.

### 20.1 What was added

* **A credential source per connection.** `credential_source` on a stored connection is `environment` (the default, and what every connection stored before this field meant) or `managed`. It is chosen explicitly, per connection, in `PUT /api/ai-settings/extraction/` and is shown in the `connections[]` of the answer. `credential_ref` keeps its shape (`AP_AI_CRED_[A-Z0-9_]{1,48}`) and now names an environment variable (`environment`) or a reference in the secret store (`managed`). A managed connection needs a reference and a provider type that takes a credential (Ollama takes none: `credential_source: not_allowed`). No migration: the field lives in the existing `connections` JSON, and a row stored before it reads as `environment`. An item of a save that **omits** `credential_source` keeps the stored source of the connection with the same identifier (a client that does not know the field cannot switch a managed credential back to the environment by leaving it out; switching is `"credential_source": "environment"`); a new connection that omits it is an environment connection.
* **The credential API** (administrator only), section 20.2.
* **Resolution** (`config/ai_connections.py`), section 20.3.
* **The secret store composition** (`config/secret_store.py`), section 20.4.

### 20.2 API contract (`apps/ai_credentials`, `apps/api/views_ai_credentials.py`)

All routes require an authenticated session and an administrator (`is_superuser`, reloaded on every call; no role grants it), enforce CSRF on `POST`, `PUT` and `DELETE`, are throttled like every API view and send `Cache-Control: no-store`. The authorization is decided **before** the reference or the value is looked at.

| Route | Does | Success | Refusals |
| --- | --- | --- | --- |
| `POST /api/ai-credentials/` body `{reference, value}` | creates; never overwrites | `201` status | `409 credential_already_exists` |
| `PUT /api/ai-credentials/<reference>/` body `{value}` | replaces (rotation); never creates | `200` status | `404 credential_not_found` |
| `DELETE /api/ai-credentials/<reference>/` | deletes | `200` status `not_set` | `404 credential_not_found`; `409 credential_in_use` (`details.connections`: identifiers) while a stored connection with `credential_source` managed names it |
| `GET /api/ai-credentials/<reference>/` | status | `200` status | none beyond the common ones |

The status body is fixed: `{reference, source: "managed", status: "set" | "not_set" | "unreadable", store: <backend id>, connections: [<connection ids that read it>], apply: "worker_restart_required"}`. `unreadable` means a file exists but THIS process cannot decrypt it (written by another Windows identity, damaged, or moved from another machine); the status read decrypts the secret only to learn that and discards it. **No response, error or log line carries a value**, and the body has no field that could.

Common errors: `400 validation_error` with codes under `reference` (`invalid_reference`, `invalid_type`) or `value` (`invalid_secret`, `invalid_type`, `too_long`, `blank`) or `unknown_field` / `required`; `403 not_authenticated`, `permission_denied`, `csrf_failed`; `503 secret_store_disabled`, `secret_store_unavailable`, `secret_store_busy` (retryable). A reference must match `AP_AI_CRED_[A-Z0-9_]{1,48}` (the same shape as a connection's), so this API can never touch another use of the store. A value is a printable string of 1 to 4096 characters with no leading or trailing space: it is **not trimmed** (a value with surrounding space is refused, never silently changed), it is never echoed (the error handler returns codes only), and it is hidden from Django's error reports (`sensitive_post_parameters`). There is no `PATCH`, no list, no export and no endpoint that returns a value.

### 20.3 Secret resolution

* **Explicit source, one source.** An `environment` connection reads the environment variable its reference names and **never** asks the secret store. A `managed` connection reads the secret store under its reference and **never** consults the environment: an `AP_AI_OPENAI_API_KEY` or `AP_AI_WORKBENCH_API_KEY` in the environment is replaced by the managed key, and a managed key that cannot be read is an error. There is no fallback in either direction and none to another store.
* **Where.** `connection_environ` builds the copy of the environment the provider loaders read, as before; for a managed connection the key comes from `resolve_managed_credential(reference, store)`. The store is the one the process is configured for (20.4); tests inject one. Nothing is read at import and nothing is cached.
* **Failures are fixed reasons.** Not stored: `missing_credential_reference` (status `missing_api_key`). Disabled, unavailable, busy, unreadable for this Windows identity, or misconfigured: `credential_store_unavailable` (status `invalid_configuration`). The messages name no reference, path or value.
* **The worker reads it once, at start-up** (`build_extractor_source`, from the selected connection): a missing or unreadable managed credential stops the worker (`ExtractionConfigurationError`), exactly like a missing environment variable. A credential created, replaced or deleted later reaches a worker only when it is **restarted**; every credential response says `apply: worker_restart_required`. The status probe of the web process reads the store of the web process.
* **Locality, provider and policy rules are unchanged**: the credential source changes only where the key comes from. The adapters' endpoint and origin rules, the guarded transport, the operator's allowlists and the gateway's policy check apply to a managed connection exactly as to an environment one.

### 20.4 The secret store of a process (`config/secret_store.py`)

`AP_SECRET_STORE_BACKEND` is `disabled` (unset or empty: every operation fails with `secret_store_disabled`; this is the default) or `windows_dpapi`; anything else is `secret_store_unavailable`. `windows_dpapi` needs `AP_SECRET_STORE_DIRECTORY` (an absolute directory outside the repository, prepared by an operator with `prepare_windows_secret_directory`), and optionally `AP_SECRET_STORE_TIMEOUT_SECONDS`, `AP_SECRET_STORE_APPROVED_IDENTITY_SIDS` and `AP_SECRET_STORE_EXPECTED_IDENTITY_SID`. **Set the same values for the web process and the worker.** They are backend-only: no Django setting, API response or database column holds them, `.env.example` lists them commented and empty, and the test runner removes every `AP_SECRET_STORE_*` variable from the process.

**Service identity.** DPAPI binds a secret to the Windows account that wrote it. The process that writes (the web process, on `POST` and `PUT`) and the one that reads (the worker) must therefore run as the **same Windows account**, and `AP_SECRET_STORE_EXPECTED_IDENTITY_SID` pins that SID so a process under any other account refuses to use the store (`secret_store_unavailable`) instead of failing later. With different accounts nothing is decrypted: the status says `unreadable`, and the worker refuses to start. This needs no unsupported assumption: ordinary and virtual service accounts are supported, and LocalSystem, LocalService and NetworkService only when the operator names the SID (secret-store.md, sections 1.2 and 2); service runs under those accounts, a virtual account and a gMSA were **not** tested. Development and production accounts are not assumed identical: each stores its own secrets in its own directory.

### 20.5 Audit

One append-only event per success, written with `record_event` **inside** the transaction and **before** the secret store is changed (the store change is the last step before the commit): `ai_credential.created`, `ai_credential.replaced`, `ai_credential.deleted` (Core actions in the reserved `ai_credential` namespace; subject type `ai_settings`; no audit migration). An event holds the actor's public id, the time (set by the database), an opaque subject id (a UUIDv5 of the reference, so it does not reveal the name) and one detail, `store` (the backend's identifier). **No value, no reference name, no path.** A refused or failed operation, and a failed store call, roll the event back; a failed audit write stops the change. Every operation first takes a PostgreSQL transaction-level advisory lock keyed on the reference, before the audit INSERT, so two operations on one reference run one after the other and the audit order is the store write order; other references are independent (no global lock; a delete then also locks the settings row, in that order only). The secret store is not part of the database transaction, and **the outcome of a failed commit is unknown**: nothing is ever deleted or overwritten to guess it. The original exception propagates unchanged, and when the store may have changed ONE fixed ERROR line is logged (action, actor's public id, opaque subject id; never a reference, value or path) for the operator to reconcile. A crash between the store change and the commit leaves the credential changed and unaudited (accepted window). Contract, guarantees and the reconciliation procedure: [secret-store.md](secret-store.md), section 9. Reads (status) are not audited; audit immutability is ORM-level only.

`ai_settings.connection_changed` gained one detail, `credential_source` (a fixed token), still without any reference name.

### 20.6 Deleting

`DELETE` is refused while any stored connection with `credential_source: managed` names the reference (`credential_in_use`), decided under the settings row's lock (`select_for_update`) inside the delete's transaction (after the reference's advisory lock; section 20.5), so a concurrent settings save cannot add a user between the check and the delete. Disabling a connection does not free its credential; remove the reference from it (or switch it to `environment`) first. An environment variable of the same name is irrelevant and is never touched.

### 20.6a Failure recovery (summary; full contract in [secret-store.md](secret-store.md), section 9)

A credential write answers an error when the audit INSERT, the lock, the store call or the COMMIT fails. Before the store call that is definite (nothing changed). After it, a failed COMMIT has an unknown outcome: **nothing is deleted or overwritten to guess it**, the error propagates unchanged, and one fixed ERROR line (`AI credential <created|replaced|deleted>: the secret store may have changed but the audit commit was not confirmed; reconcile (actor=… subject=… store=…)`) is logged. A crash between the store change and the commit leaves a changed, unaudited credential and no log line (accepted window). Reconcile with `GET` the status, look for the event by the opaque subject id, then record the truth with an audited `PUT` or `DELETE`, and restart the worker (runbook, section 5.11). The response of a committed success never depends on a database read after the commit (the list of connections is read inside the transaction, before the store changes).

### 20.7 Not done

No frontend and no key or token typing in the UI; no connection test and no request to any provider; no list or export of credentials; no automatic rotation, expiry or reminder; no per-user or per-company store; no hot reload and no worker restart control; `AP_SECRET_STORE_*` are read per process with no shared status; the status of the worker's own store is not reported to the API; and everything in secret-store.md, section 7 (same-user code, administrators, rollback by a restore, untested service accounts, no real wrong-account run) still applies, plus the accepted credential-write limitations of section 9 (an uncertain commit and the crash window can leave a changed, unaudited credential; the reconciliation is manual).

### 20.8 Tests

DB-free, in the development VM, no PostgreSQL, no network, synthetic sentinel secrets, an in-memory store (and, only on Windows, the real hardened store in a dedicated temporary directory): 128 new tests (3570 DB-free tests in all, OK), 16 deliberate mutations caught (8 for the API and 8 for the transaction hardening).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ai_credentials_services.py` | 47 | authorization before validation (every operation), reference and value validation, create / replace / delete / status semantics, delete refused while in use (row locked), the audit events (actor, opaque subject, no secret, order before the store, rollback, failed audit stops the change), every store failure code, disabled store, no environment use, redaction in errors, tracebacks and logs; **transaction safety**: an ambiguous commit and a rolled-back commit for create, replace and delete delete or overwrite nothing, the original exception object and traceback are preserved, `KeyboardInterrupt` and `SystemExit` at the commit and in the store call, one fixed log line per uncertain failure and none for a definite one, a failing log never replaces the failure, a static check that only the requested operation touches the store; **lock ordering**: the advisory lock inside the transaction before the audit and the store, reference lock before the settings row lock, no lock for a refused operation, threads: one reference serialized with audit order = write order and another reference independent; **post-commit**: no database access after the commit, a failing in-transaction read stops the operation before any change |
| `test_ai_credentials_api.py` | 27 | routing and methods, anonymous / non-administrator / CSRF, the contract and the lifecycle, error mapping, validation (reference, value, unknown fields, malformed body), no secret in any response or log line, `sensitive_post_parameters`, fail-closed wiring |
| `test_ai_credentials_worker.py` | 33 | the credential source in the rules, the settings service keeping an omitted source, managed key reaching the worker and the request, no environment fallback in either direction, a managed credential read once, locality / policy / origin rules unchanged, the status probe, the composition from the environment, and the real DPAPI store end to end on Windows |
| `test_ai_credentials_boundaries.py` | 17 | pure rules, thin views, no environment read or logged value, only the composition roots and the credential service touch the store, fixed response fields, the audit vocabulary, `.env.example` |
| `test_secret_store_dpapi_hardening.py` | +4 | the pinned expected identity |
| `test_ai_credentials_postgres.py` | not run (20 tests written) | real audit rows and rollback, the lifecycle through real sessions and CSRF, no secret in any table, delete versus a concurrent settings save, the stored source and an omitted source; **transaction hardening**: concurrent rotations with `pg_locks` and audit order = write order, a delete and a settings save beside a rotation without a deadlock, a real definite and a real ambiguous COMMIT failure, and a child process killed before and after the store change (`tests/credential_crash_worker.py`) |

Existing tests edited (pins and expected shapes only): `test_audit_vocabulary`, `test_review_audit`, `test_intake_ai_boundaries`, `test_api_boundaries`, `test_secret_store`, `test_runner`, `test_ai_connections_boundaries`, `test_ai_connections_api` and the support module `ai_connections_support`.
