# ADR 0004: Provider-neutral AI gateway

## Status

**Accepted:** one provider-neutral AI gateway with interchangeable adapters; a
deterministic test adapter before any real provider; no silent fallback to a
cloud provider; no tool, shell, or filesystem access for models.

**Proposed, unproven:** the exact three-mode policy model, capability checks,
and the model-evaluation procedure described in
[ai-policy.md](../architecture/ai-policy.md). These are untested designs.

## Context

Companies have different data-protection requirements. Some require that data
never leaves the machine; others accept a company-operated server or an approved
external provider. Models and providers change quickly and do not behave
identically. Document content is untrusted and model output may be wrong.

## Decision

Route all model use through one **AI gateway** with a provider-neutral interface
and interchangeable **adapters**. The gateway enforces a per-company data-policy
mode (`local_only`, `company_endpoint`, `approved_external`) with **no silent
fallback to a less restrictive locality**, applies the same policy to
embeddings, validates structured output, checks provider capabilities, and
records provenance. Start with a **deterministic test adapter**; add real local
and external adapters later. Models receive no tool, filesystem, or shell
access. Details are in [ai-policy.md](../architecture/ai-policy.md).

## Alternatives

- **Call one provider's SDK directly from workflows.** Fast to start, but locks
  in the provider and scatters policy enforcement.
- **Adopt a third-party abstraction library as the gateway.** May save effort;
  must be evaluated for policy control, Windows support, and maintenance. Not
  evaluated here.
- **Local-only forever.** Simplest policy, but excludes companies that approve an
  external provider.

## Consequences and trade-offs

- Policy is enforced in one place and can be tested.
- The interface must stay general enough for different providers, so some
  provider-specific features will not be exposed.
- Capability differences require explicit checks and sometimes explicit failure.
- Evaluation work is needed whenever a model changes.

## Conditions for revisiting

- Adapters need provider-specific features that the neutral interface cannot
  express without leaking details.
- A mature external abstraction satisfies the policy requirements.
- Regulation or a client contract imposes data-handling rules the three modes
  cannot express.
