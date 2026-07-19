# Midas — Shared Design Contract (Detailed Design, Doc 00)

**Date:** 2026-07-19
**Status:** Approved conventions. Every context detailed-design doc inherits these rules.
**Parent:** `../2026-07-19-midas-personal-treasury-os-design.md`

This document is the single source of truth for cross-cutting conventions. Per-context
docs (01–08) MUST NOT redefine these — they reference them.

---

## 1. Identifiers

- **Primary keys:** `UUID` v7 (time-ordered) generated app-side. Time-ordering keeps
  index locality without leaking a sequence. Column type `UUID`.
- **Natural/business keys** (e.g. `idempotency_key`, `reference`) are separate columns,
  never the PK.
- Foreign keys named `<entity>_id`.

## 2. Money

- **Storage:** `BIGINT` **minor units** (paise for INR) + a `currency CHAR(3)` column.
  Never `FLOAT`/`DOUBLE`. Never store a decimal rupee value.
- **In Java:** a `Money` value object wrapping `long amountMinor` + `Currency`. All
  arithmetic goes through it; no raw `long` money math in services.
- **Rounding:** banker's rounding (HALF_EVEN) only at explicit presentation/derivation
  boundaries, documented where used.
- **Currency:** INR only in Phases 0–5. The column exists everywhere so multi-currency
  is additive. Mixing currencies in one transaction is rejected until multi-currency
  lands.

## 3. Time

- **Storage:** `TIMESTAMPTZ`, always UTC.
- **Business time vs system time:** `LedgerTransaction.timestamp` is the *effective*
  business time (when the money moved). `created_at`/`updated_at` audit columns are
  system time. Point-in-time queries use business time.
- App passes clock in via an injected `Clock` (never `Instant.now()` inline) for
  testability.

## 4. Owner / tenant seam

- Every aggregate-root table has `owner_id UUID NOT NULL`.
- Phases 0–5: a single hard-coded system owner UUID, injected via `OwnerContext`
  (request-scoped bean). No auth yet.
- Phase 6 flips `OwnerContext` to resolve from an authenticated principal. **No schema
  change required** — this is the whole point of the seam.
- Every repository query is owner-scoped. A shared base repository enforces this so a
  context can't accidentally leak cross-owner rows.

## 5. Persistence & migrations

- PostgreSQL 16+. **Flyway** for migrations, one versioned file per change, never edited
  after merge.
- Each context owns its tables; table names prefixed by context where ambiguous
  (`import_statement_entry`, `recon_match`). Ledger tables are unprefixed (`account`,
  `ledger_transaction`, `posting`) as the core domain.
- **Append-only tables** (`ledger_transaction`, `posting`, `outbox`, audit tables) have
  NO `UPDATE`/`DELETE` grants in application code — enforced by convention + a DB role
  with `INSERT, SELECT` only on those tables.
- Money-critical invariants are enforced at BOTH the DB level (CHECK constraints, e.g.
  posting sum) where feasible and the domain level.

## 6. Outbox / domain events

- **Envelope** (`outbox` table): `id UUID (v7), owner_id, aggregate_type, aggregate_id,
  event_type, payload JSONB, occurred_at TIMESTAMPTZ, status, attempts, next_attempt_at`.
- **Write path:** the producing service writes the outbox row inside the SAME DB
  transaction as the state change (transactional outbox). No 2-phase commit, no dual
  write.
- **Dispatch:** a polling dispatcher (Phase 0) reads `status='PENDING'` ordered by `id`
  (v7 = time order), delivers to in-process consumers, marks `SENT`. At-least-once.
- **Consumers are idempotent** — keyed on `(consumer_name, event_id)` in a
  `processed_event` table; a redelivery is a no-op.
- Event `payload` is a versioned JSON contract (`schemaVersion` field). Consumers
  tolerate unknown fields.
- Later: the dispatcher backend swaps to Kafka with no change to producers/consumers.

## 7. REST API conventions

- Base path `/api/v1`. JSON only. Resource-oriented, plural nouns
  (`/api/v1/accounts`, `/api/v1/transactions`).
- **Idempotency:** all POSTs that create financial state require an `Idempotency-Key`
  header; stored in `platform` idempotency store; replay returns the original result.
- **Errors:** RFC 9457 `application/problem+json` — `{type, title, status, detail,
  instance, code}`. Stable machine `code` enum per context (e.g.
  `LEDGER_UNBALANCED_TRANSACTION`).
- **Money in JSON:** `{ "amountMinor": 12345, "currency": "INR" }`. Never a float.
- **Pagination:** cursor-based (`?cursor=&limit=`), cursor = opaque v7-id. No offset
  pagination on ledger/analytics.
- **Time in JSON:** ISO-8601 UTC (`2026-07-19T10:15:30Z`).
- **Validation:** request DTOs validated (Bean Validation); 422 on violation.

## 8. Read vs write separation

- **Truth** lives in `ledger` (append-only). **Everything else that shows numbers**
  (analytics, budgeting dashboards, forecasts) reads from **projections** rebuilt from
  the outbox — never mutates truth.
- Projections are **rebuildable**: dropping and replaying the outbox from genesis MUST
  reproduce identical projection state (deterministic replay). This is a tested property.
- Projections may be eventually consistent; truth is strongly consistent.

## 9. Errors & failure posture

- Ledger writes: fail closed. Any invariant violation aborts the whole transaction.
- Import/reconciliation: fault-tolerant, retry-safe, resumable. A bad row quarantines,
  never aborts the batch.
- All external-facing operations are idempotent (keys above).

## 10. Testing baseline (per context)

- Domain invariants: unit + property tests.
- Persistence: integration tests on real Postgres (Testcontainers).
- Idempotency: replay-same-input → no duplicate effect.
- Projections: replay-from-genesis determinism test.
- API: contract tests per endpoint (happy + validation + error `code`).

## 11. Java/module conventions

- Java 21, Spring Boot 3.x. One Gradle/Maven module or package per context under
  `com.midas.<context>`.
- Each context exposes a narrow **public API interface** (`LedgerApi`, `ImportApi`, …);
  cross-context calls go through the interface only — never into another context's
  internals/repositories. This is what keeps the monolith modular and later-splittable.
- Package-private implementation; only the API interface + DTOs are public.
