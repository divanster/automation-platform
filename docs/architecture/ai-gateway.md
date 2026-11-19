# AI Gateway Foundation (AI-1a), the KPMG Workbench provider (AI-1b) and Request Intake extraction (AI-2a)

> **AI-2a status (added after AI-1b was merged, PR #20): Request Intake can now extract with the gateway, but only when an operator explicitly enables it.** The default
> is unchanged: with nothing set, the worker uses the deterministic fixture parser and no AI provider is called. Section 15 describes the extraction modes, the policy and
> failure rules and what was not done. AI-2a was checked in the development VM with DB-free tests and a MOCKED transport only (the VM never contacts the Workbench);
> its PostgreSQL tests are written and **have not been run**. Where an older statement below says that nothing calls the gateway, it describes AI-1a and AI-1b as merged;
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
    adapters/deterministic.py   the deterministic TEST provider (not re-exported by the package)
    adapters/workbench.py       (AI-1b) the KPMG Workbench provider; pure, talks only to an HttpTransport
backend/config/ai_gateway.py      composition root: BUILTIN_PROVIDERS = (), BUILTIN_PROFILES = ()   (unchanged by AI-1b)
backend/config/ai_workbench.py    (AI-1b) opt-in composition: environment -> settings -> Workbench provider, profiles and gateway
backend/config/ai_http_transport.py   (AI-1b) the one real transport (standard-library http.client, TLS, proxy); the only code that opens sockets
```

Adapters import only the contract above them and the standard library; the gateway core never imports an adapter; no business module,
workflow package or Core app imports the package, and the only `config` modules that do are `config/ai_gateway.py` and, since AI-1b,
`config/ai_workbench.py` and `config/ai_http_transport.py`; only `config/ai_workbench.py` imports an adapter (all pinned by
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

> **Status: written; checked in the development VM with DB-free tests and a MOCKED HTTP transport (148 new tests, 2636 DB-free tests in all pass); the 35 PostgreSQL tests in
> `tests/test_intake_ai_postgres.py` are written and have NOT been run.** The VM never contacts the Workbench and holds no key. The only live evidence is the owner's single local GPT-5 smoke
> test of the provider (section 14.10(d)), which did not use Request Intake's prompt or schema. Nothing here is production-ready, and no accuracy of AI extraction has been measured.

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

### 15.5 What is preserved

Validation, canonicalization, the content hash, revision 1, the `ExtractionOutcome`, the run states and the audit events are produced by the **same existing code** for both modes; a test compares the stored
revision's hash and bytes with an independent `canonicalize_request` of the same candidate, and the provenance is never part of the hashed content. The audit actions and their safe detail keys are Core's and
unchanged (`revision.created` carries `origin: "extraction"`); no AI detail is added. Approval stays human: the AI answer has no field that could approve, the revision is `needs_review`, the existing
review rules (a different approver, acknowledged warnings, exact hash) apply, and an invalid outcome is repaired through the existing edit path. The outcome records which extractor produced it:
`extractor_name` `ai-request-extractor`, `extractor_version` `<prompt version>|<profile>|<model the provider reported>` (for example `1|workbench-gpt5|gpt-5-2025-08-07`; `unreported` when the provider names none;
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
| `test_intake_ai_postgres.py` | 35 (**not run**) | AI extraction through the real worker, revision, validation, review, approval and rejection, retry and exhaustion, recovery, rollback, idempotency, module disabled, competing workers, the deterministic regression |

Two existing test files were edited, pins only: `test_intake_composition.py` (the app's source-file list and the list of pure modules) and `test_ai_gateway_boundaries.py` (the importers of the gateway and the files that may name it); the existing extraction, worker, gating and
module tests pass unchanged. Not tested: the real Workbench with Request Intake's prompt, PostgreSQL (written only), concurrency beyond the written tests, and cost or latency.
