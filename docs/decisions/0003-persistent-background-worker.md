# ADR 0003: Persistent background worker

## Status

**Accepted:** a separate, persistent worker process that shares the backend
domain code (an owner design constraint).

**Proposed, unproven:** the PostgreSQL-backed task queue mechanism (claim,
lease, fencing, retries, recovery). Its correctness is **not established**
until it is implemented and the concurrency and recovery tests listed in
[background-execution.md](../architecture/background-execution.md), section 15,
have passed. A library-versus-custom decision is also still open.

## Context

Extraction and similar steps may take long, may call AI providers, and must
survive restarts and request timeouts. Interactive requests must stay
responsive. Each additional stateful service increases installation and support
cost on client machines.

## Decision

Run background work in a **separate, persistent worker process** that shares the
backend domain code, using a **PostgreSQL-backed task queue** instead of a
separate message broker. Design for at-least-once execution with idempotent
effects, leases with fencing, bounded retries, cooperative cancellation, and no
database transaction held open during an AI call. Details are in
[background-execution.md](../architecture/background-execution.md). Whether to
build a small custom queue or adopt an evaluated library is decided at the start
of Phase 4.

## Alternatives

- **Broker-based queue (for example with Redis or RabbitMQ).** Mature features,
  but another service to install, secure, and update on every machine.
- **In-process threads or async tasks in the API.** Work is lost on restart and
  competes with requests.
- **Scheduled jobs only.** Not suitable for event-driven, retryable work.
- **Synchronous processing in requests.** Fails on long AI calls and timeouts.

## Consequences and trade-offs

- One fewer service and a single backup path.
- Queue correctness (claim, lease, recovery) becomes the team's responsibility
  and must be tested; this is a design proposal, not a proven implementation.
- Throughput is limited relative to a dedicated broker; actual needs are not yet
  measured.
- Exactly-once execution is not promised; duplicate prevention relies on
  idempotency and constraints.

## Conditions for revisiting

- Measured throughput or latency needs exceed what the database queue can
  provide.
- Queue maintenance cost exceeds the cost of operating a broker.
- A mature library that fits Windows and Django proves clearly better than a
  custom implementation.
- A hub deployment needs multiple worker hosts that the design cannot support.
