# ADR 0005: Company configuration, not forks

## Status

Accepted. This follows the owner's rule of no customer-specific code forks.

Qualification: the exact configuration mechanism and schema format are not
designed yet.

## Context

Different companies will want somewhat different behavior. Customer-specific
forks of the code multiply maintenance, make security fixes and updates
expensive, and eventually turn the product into custom projects.

## Decision

Maintain **one shared product core** and **predefined, versioned workflow
packages**. Differences between companies are expressed only as **limited,
validated company configuration** that a workflow package explicitly declares.
Configuration contains no executable code. There are **no customer-specific code
forks or branches**. A requirement that configuration cannot express is handled
as a product change or a new workflow package version available to all
companies. Configuration is versioned, and a workflow run snapshots the version
it used ([data-model.md](../architecture/data-model.md)).

## Alternatives

- **Customer forks or branches.** Maximum flexibility per customer, unsustainable
  maintenance.
- **Plugin or scripting system for customers.** Flexible, but a large security and
  support surface and effectively custom code.
- **Unrestricted configuration (free-form rules or prompts).** Hard to validate,
  test, and support.

## Consequences and trade-offs

- Updates and fixes apply to all companies through one codebase.
- Some customer requests will be declined or delayed because they do not fit
  configuration.
- Workflow packages must declare configuration schemas carefully, which adds
  design effort.
- Testing must cover configuration variants.

## Conditions for revisiting

- Repeated, important requests cannot be met by configuration or new package
  versions.
- A formally reviewed extension mechanism (with sandboxing and a support
  boundary) is proposed.
- The pilot shows that the configuration surface is too narrow or too complex.
