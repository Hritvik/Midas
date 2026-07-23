# 01 — Platform Context (Detailed Design)

**Amended 2026-07-23** (Tier 0 spine — audit remediation; conforms to `00-conventions.md` §6, §7).

**Inherits:** `00-conventions.md`. **Parent:** the design spec.

Cross-cutting foundation every other context depends on. No business domain of its own.

## Audit fixes applied

- **01-CRITICAL (per-consumer delivery):** delivery state moved off the `outbox` row into a
  per-`(event, consumer)` `outbox_delivery` table; one consumer's DEAD/backoff never affects
  another. → Schema, `OutboxDispatcher`.
- **01-CRITICAL (out-of-order fence):** each consumer advances a **contiguous low-water
  offset** — only over an unbroken prefix of `seq`, never past a still-undelivered lower
  `seq`; backoff/reordering can no longer silently drop an event. → Schema (`seq`),
  `OutboxDispatcher`.
- **01-CRITICAL (owner-context async):** dispatcher opens an `OwnerContext` from
  `event.owner_id` around every consumer `handle()`; async owner scoping is part of the
  `EventConsumer` contract. → `OutboxDispatcher`, `EventConsumer`.
- **01-HIGH (mark-SENT boundary):** a delivery is marked `SENT` only **after** the consumer's
  `processed_event` write commits — SENT is derived from the ack, never a separate earlier tx.
  → `OutboxDispatcher`.
- **01-HIGH (idempotency race):** `IdempotencyService` is now **reserve-first** — INSERT a
  `PENDING` record as the lock; the loser catches the unique-violation and returns/awaits the
  winner's result. → `IdempotencyService`, Idempotency semantics.
- **01-MEDIUM (same-tx finalize):** the idempotency record's result is finalized in the SAME
  DB tx as the business effect + outbox row. → `IdempotencyService`.
- **01-MEDIUM (outbox retention):** `outbox` is declared the durable event log — never pruned;
  archival copies to a companion table the rebuild also reads. → Schema, `OutboxWriter`.
- **Known #4 (DEAD replay):** `DEAD` is per-`(event, consumer)` and **replayable** — reset to
  re-drive after a fix so a lagging projection catches up. → `OutboxDispatcher`.
- **Known #5 (idempotency purge):** financial-write idempotency records are not purged (see
  §7); retention below applies only to non-financial scopes. → Idempotency semantics.

## Responsibilities

- Owner/tenant seam (`OwnerContext`, owner-scoped base repository).
- Idempotency store.
- Transactional outbox: table, writer helper, polling dispatcher, consumer registry,
  processed-event dedup.
- Global error model (problem+json), clock, config.

## Schema

```sql
-- Idempotency: dedup create-POSTs and imports (reserve-first, §7).
CREATE TABLE idempotency_record (
    id              UUID PRIMARY KEY,             -- v7
    owner_id        UUID NOT NULL,
    scope           TEXT NOT NULL,                -- e.g. 'transaction', 'import'
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,                -- SHA-256 of canonical request body
    status          TEXT NOT NULL DEFAULT 'PENDING', -- PENDING (reserved) | DONE
    response_status INT,                          -- NULL until finalized (same tx as effect)
    response_body   JSONB,                        -- NULL until finalized
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at    TIMESTAMPTZ,
    UNIQUE (owner_id, scope, idempotency_key)     -- the reservation lock
);

-- Transactional outbox = the durable event log (see conventions §6).
-- NEVER pruned; if archived, rows copy to a companion table the rebuild (§8) also reads.
CREATE TABLE outbox (
    id              UUID PRIMARY KEY,             -- v7 (PK, index locality)
    seq             BIGINT GENERATED ALWAYS AS IDENTITY, -- DB-assigned ordering (§1, §6)
    owner_id        UUID NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    UUID NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    schema_version  INT  NOT NULL DEFAULT 1,
    occurred_at     TIMESTAMPTZ NOT NULL
    -- NO status/attempts/next_attempt_at here: delivery is per-(event, consumer) below.
);
CREATE UNIQUE INDEX outbox_seq_idx ON outbox (seq);

-- Per-(event, consumer) delivery state (audit 01-CRITICAL). One consumer's failure/DEAD
-- never affects another's delivery or liveness.
CREATE TABLE outbox_delivery (
    event_seq       BIGINT NOT NULL REFERENCES outbox (seq),
    consumer_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | SENT | DEAD
    attempts        INT  NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_seq)
);
CREATE INDEX outbox_delivery_dispatch_idx
    ON outbox_delivery (consumer_name, status, next_attempt_at, event_seq);

-- Per-consumer contiguous low-water offset (audit 01-CRITICAL). Advances only over an
-- unbroken prefix of seq; never past a still-undelivered lower seq.
CREATE TABLE consumer_offset (
    consumer_name    TEXT PRIMARY KEY,
    contiguous_seq   BIGINT NOT NULL DEFAULT 0     -- all seq <= this are SENT for this consumer
);

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
  inserts an outbox row (DB assigns `seq`). Throws if no active transaction. On insert,
  seeds a `PENDING` `outbox_delivery` row for every registered consumer (co-committed).
  The outbox is the durable event log — never pruned (§6); no delete path exists.
- **`OutboxDispatcher`** — `@Scheduled` poll (configurable, default 1s), driven **per
  consumer**:
    - Selects that consumer's due `outbox_delivery` rows (`status = PENDING`,
      `next_attempt_at <= now()`) joined to `outbox`, ordered by `seq`, using
      `SELECT ... FOR UPDATE SKIP LOCKED` so multiple instances (Phase 6) don't
      double-dispatch.
    - For each event, opens an `OwnerContext` scope from `event.owner_id` (set on entry,
      clear on exit) around the consumer's `handle()` — async owner scoping, part of the
      `EventConsumer` contract (§4).
    - Marks a delivery `SENT` **only after** the consumer's `processed_event` write has
      committed — SENT is derived from that ack, never written in a separate earlier tx
      (audit 01-HIGH). Redelivery before ack is a dedup no-op.
    - On consumer failure: increment `attempts`, exponential backoff `next_attempt_at`;
      after N attempts → `DEAD` **for that (event, consumer) only** + alert. A DEAD
      delivery is **replayable**: after a fix it can be reset to `PENDING` to re-drive, so
      the projection catches up (known #4).
    - **Contiguous low-water offset:** `consumer_offset.contiguous_seq` advances only over
      an unbroken prefix of `seq` — never past a still-`PENDING`/`DEAD` lower `seq`. Backoff
      or Phase-6 reordering can never skip a stuck lower `seq` and silently drop an event
      (audit 01-CRITICAL). Catch-up/rebuild fences read from this contiguous mark, not a
      max-applied high-water mark.
- **`EventConsumer`** — interface `{ String name(); void handle(Event e); }`. The
  dispatcher wraps each call in the processed-event dedup check **and** in the
  `event.owner_id` owner scope. Consumers are idempotent on `(consumer_name, event_id)`.
- **`IdempotencyService`** — `execute(scope, key, requestHash, supplier)`, **reserve-first**
  (audit 01-HIGH):
    - INSERT a `PENDING` `idempotency_record` first — the `UNIQUE(owner_id, scope,
      idempotency_key)` constraint IS the lock (no check-then-act).
    - **Winner** (insert succeeds) runs the supplier, then finalizes the record
      (`status = DONE`, `response_status`/`response_body`) in the **SAME DB transaction** as
      the business effect and its outbox row — all commit or none (audit 01-MEDIUM).
    - **Loser** (unique-violation) reads the existing record: if `DONE` with matching hash →
      return the stored response; if still `PENDING` → await the winner's commit then return
      its result; if hash differs → `409 IDEMPOTENCY_KEY_REUSED`.
    - **Cross-module note (§7):** when the effect includes a ledger post, that post shares the
      **same physical DB transaction** (never a re-post on replay — look up the pre-existing
      txn by shared key and attach). Multi-leg operations derive **per-leg sub-keys**
      deterministically from the request key.

## Idempotency semantics

- Reserve-first: the supplier executes **at most once** regardless of concurrency (the
  `PENDING` reservation row is the lock; the loser awaits/returns the winner's result).
- Same key + same request body → original response replayed (no re-execution).
- Same key + different body → `409 IDEMPOTENCY_KEY_REUSED`.
- The record's result is finalized in the same DB tx as the business effect + outbox row,
  so a crash can never leave an executed effect with no record.
- **Financial-write** records are **not purged** (§7, known #5) so a late retry can't
  double-post. The retention window (default 30 days) applies only to non-financial scopes.

## Testing

- Outbox: write-in-same-tx (rollback of business op rolls back outbox row + its delivery
  rows), dispatch at-least-once, consumer dedup makes redelivery a no-op.
- Per-consumer delivery: one consumer's `DEAD` leaves other consumers delivering `SENT` for
  the same event; a reset DEAD delivery re-drives and the projection catches up.
- Contiguous offset: a stuck lower `seq` (backoff/DEAD) blocks `contiguous_seq` from
  advancing past it even after higher `seq` are `SENT` — no below-fence event loss.
- Mark-SENT boundary: a crash between `handle()` and the ack leaves the delivery `PENDING`
  (redelivered, dedup no-op), never a `SENT` row with no `processed_event`.
- Async owner scope: dispatching an owner-B event runs `handle()` under owner B's
  `OwnerContext`; owner-scoped repos resolve B's rows, not the system owner's.
- Idempotency: replay returns identical response; body-mismatch conflicts; concurrent
  same-key → exactly one supplier execution (reservation lock); crash-after-effect leaves a
  finalized record (no re-execute).
- Owner scoping: repository cannot read another owner's row.

## Public API

`PlatformApi` is internal-only (no REST). Consumed by other contexts via
`OutboxWriter`, `IdempotencyService`, `OwnerContext`.
