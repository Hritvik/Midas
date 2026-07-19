# 01 — Platform Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec.

Cross-cutting foundation every other context depends on. No business domain of its own.

## Responsibilities

- Owner/tenant seam (`OwnerContext`, owner-scoped base repository).
- Idempotency store.
- Transactional outbox: table, writer helper, polling dispatcher, consumer registry,
  processed-event dedup.
- Global error model (problem+json), clock, config.

## Schema

```sql
-- Idempotency: dedup create-POSTs and imports.
CREATE TABLE idempotency_record (
    id              UUID PRIMARY KEY,             -- v7
    owner_id        UUID NOT NULL,
    scope           TEXT NOT NULL,                -- e.g. 'transaction', 'import'
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,                -- SHA-256 of canonical request body
    response_status INT  NOT NULL,
    response_body   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, scope, idempotency_key)
);

-- Transactional outbox (see conventions §6).
CREATE TABLE outbox (
    id              UUID PRIMARY KEY,             -- v7 = time order
    owner_id        UUID NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    UUID NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    schema_version  INT  NOT NULL DEFAULT 1,
    occurred_at     TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|SENT|DEAD
    attempts        INT  NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX outbox_dispatch_idx ON outbox (status, next_attempt_at, id);

-- Consumer-side dedup (at-least-once → effectively-once).
CREATE TABLE processed_event (
    consumer_name TEXT NOT NULL,
    event_id      UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);
```

## Key components

- **`OwnerContext`** — request-scoped; `currentOwnerId()`. Phase 0–5 returns the fixed
  system owner; Phase 6 resolves from `SecurityContext`.
- **`OwnerScopedRepository<T>`** — base repo injecting `owner_id = :owner` into every
  query; subclasses cannot bypass.
- **`OutboxWriter.append(event)`** — called within an active `@Transactional` boundary;
  inserts an outbox row. Throws if no active transaction.
- **`OutboxDispatcher`** — `@Scheduled` poll (configurable, default 1s). Fetches a batch
  of `PENDING` ordered by `id`, dispatches to registered `EventConsumer`s, marks `SENT`.
  On consumer failure: increment `attempts`, exponential backoff `next_attempt_at`; after
  N attempts → `DEAD` + alert log. `SELECT ... FOR UPDATE SKIP LOCKED` so multiple
  instances (Phase 6) don't double-dispatch.
- **`EventConsumer`** — interface `{ String name(); void handle(Event e); }`. The
  dispatcher wraps each call in the processed-event dedup check.
- **`IdempotencyService`** — `execute(scope, key, requestHash, supplier)`: on hit with
  matching hash → return stored response; on hit with different hash → 409 conflict; on
  miss → run, store, return.

## Idempotency semantics

- Same key + same request body → original response replayed (no re-execution).
- Same key + different body → `409 IDEMPOTENCY_KEY_REUSED`.
- Stored for a retention window (default 30 days), then purged.

## Testing

- Outbox: write-in-same-tx (rollback of business op rolls back outbox row), dispatch
  at-least-once, consumer dedup makes redelivery a no-op, backoff→DEAD path.
- Idempotency: replay returns identical response; body-mismatch conflicts.
- Owner scoping: repository cannot read another owner's row.

## Public API

`PlatformApi` is internal-only (no REST). Consumed by other contexts via
`OutboxWriter`, `IdempotencyService`, `OwnerContext`.
