# Midas — Shared Design Contract (Detailed Design, Doc 00)

**Date:** 2026-07-19 · **Amended:** 2026-07-23 (audit remediation, Tier 0)
**Status:** Approved conventions. Every context detailed-design doc inherits these rules.
**Parent:** `../2026-07-19-midas-personal-treasury-os-design.md`
**Audit:** `../2026-07-23-audit-and-opportunities.md`

This document is the single source of truth for cross-cutting conventions. Per-context
docs (01–11) MUST NOT redefine these — they reference them.

---

## 1. Identifiers

- **Primary keys:** `UUID` v7 (time-ordered) generated app-side, for index locality.
- **Event ordering does NOT rely on v7 wall-clock.** The outbox carries a monotonic
  DB-assigned `seq BIGINT` (`GENERATED ... AS IDENTITY`) used for delivery ordering and the
  contiguous offset (§6); v7 remains the PK. This survives NTP step-backs (audit 01-LOW).
- **Natural/business keys** (`idempotency_key`, `reference`) are separate columns, never PK.
- Foreign keys named `<entity>_id`.

## 2. Money & quantities

- **Storage:** `BIGINT` **minor units** (paise for INR) + a `currency CHAR(3)` column.
  Never `FLOAT`/`DOUBLE`, never a decimal rupee value.
- **In Java:** a `Money` value object (`long amountMinor` + `Currency`); no raw money math.
- **Rounding:** banker's (HALF_EVEN) only at explicit presentation/derivation boundaries.
- **Cost/valuation representation (audit 08-CRITICAL):** store any *aggregate* money quantity
  (cost basis, lot cost, proceeds) as a **total** `BIGINT` paise column and derive per-unit
  for display. NEVER store a rounded per-unit value and multiply it back — that leaks against
  the ledger on every fractional-unit holding. Quantities are integers scaled by a documented
  factor (`QTY_SCALE = 1e8`).
- **Currency:** INR only in Phases 0–5; the column exists everywhere so multi-currency is
  additive. One transaction is single-currency until multi-currency lands.

## 3. Time, timezone & periods

- **Storage:** `TIMESTAMPTZ`, always UTC. **JSON:** ISO-8601 UTC.
- **Business vs system time:** `LedgerTransaction.timestamp` is *effective* business time;
  `created_at`/`updated_at` are system time. Point-in-time queries use business time.
- **Owner timezone (audit Theme 5 / known #1):** every owner has an IANA timezone
  (`owner_settings.timezone`, default `Asia/Kolkata`). **All civil-period bucketing — day,
  week, month, and Financial Year — is computed in owner-local time**, not UTC. Applies to
  analytics rollups, budgeting periods, forecasting months, and import dedup day-truncation. A
  post-23:30 IST transaction must bucket into its IST day/month, and the same event must
  dedup-collapse regardless of time-vs-date export format.
- **Financial Year (India):** FY runs **Apr 1 – Mar 31** and is a first-class period type
  alongside calendar month/week (needed by tax, 80C, capital-gains, advance-tax). A period is
  `(kind: DAY|WEEK|MONTH|FY, ownerLocalStart, ownerLocalEnd)`.
- App takes an injected `Clock` (never `Instant.now()` inline).

## 4. Owner / tenant seam

- Every aggregate-root table has `owner_id UUID NOT NULL`.
- Phases 0–5: a single hard-coded system owner, injected via `OwnerContext`.
- **`OwnerContext` MUST be populated on BOTH paths (audit 01-CRITICAL):** the request path
  (request-scoped) AND the async outbox-dispatch path — the dispatcher opens an owner scope
  from `event.owner_id` around every consumer `handle()`. This is part of the `EventConsumer`
  contract, not an afterthought; otherwise cross-owner projections corrupt at the Phase-6 flip.
- Phase 6 flips `OwnerContext` to an authenticated principal. **No schema change.**
- Every repository query is owner-scoped via a shared base repository.

## 5. Persistence & migrations

- PostgreSQL 16+. **Flyway**, one versioned file per change, never edited after merge.
- Each context owns its tables; prefixed where ambiguous (`import_statement_entry`).
- **Append-only tables** (`ledger_transaction`, `posting`, `outbox`, audit) have NO
  `UPDATE`/`DELETE` grants — DB role with `INSERT, SELECT` only. **Corollary (audit
  02-CRITICAL):** never store a *mutable status* on an append-only row. Derive lifecycle
  states (e.g. a transaction being `REVERSED`) from the existence of a later row, not an
  in-place update.
- **Cross-row commit-time invariants** (e.g. postings sum-zero) use a **`DEFERRABLE INITIALLY
  DEFERRED CONSTRAINT TRIGGER`**, not a CHECK (Postgres CHECKs can't defer or aggregate across
  sibling rows — audit 02-HIGH).

## 6. Outbox / domain events

- **Envelope** (`outbox`): `id UUID (v7)`, `seq BIGINT IDENTITY` (ordering), `owner_id`,
  `aggregate_type`, `aggregate_id`, `event_type`, `payload JSONB`, `schema_version`,
  `occurred_at`. The outbox is the **durable event log** (never pruned; archival, if ever,
  copies to a companion table the rebuild also reads) — because projections rebuild from it
  (§8). Pruning it breaks rebuild (audit 01-MEDIUM).
- **Write path:** producer writes the outbox row in the SAME DB tx as the state change
  (transactional outbox; no dual write).
- **Per-consumer delivery (audit 01-CRITICAL ×2):** delivery state is tracked **per
  `(event, consumer)`** in an `outbox_delivery(event_seq, consumer_name, status, attempts,
  next_attempt_at)` table — NOT as one status on the event row. One consumer's failure/DEAD
  never affects another's delivery or liveness. Each consumer has its own **contiguous
  low-water offset**: the offset advances only over an unbroken prefix of `seq`, never past a
  still-undelivered lower `seq` (so backoff/reordering can't skip and silently drop an event).
- **Dispatch:** poller selects due deliveries (`FOR UPDATE SKIP LOCKED`), runs the consumer
  under the event's owner scope (§4), and marks that delivery `SENT` **only after** the
  consumer's `processed_event` write has committed (SENT derived from ack; never a separate
  earlier tx — audit 01-HIGH). At-least-once.
- **Consumers idempotent** on `(consumer_name, event_id)` in `processed_event`; redelivery is a
  no-op. A poison delivery exhausts backoff → `DEAD` for that consumer only, alerts, and is
  **replayable**: after a fix, a DEAD delivery can be reset to re-drive so the projection
  catches up (closes known #4). Payloads carry `schemaVersion`; consumers tolerate unknown
  fields.
- Later: dispatcher backend swaps to Kafka with no producer/consumer change.

## 7. Idempotency

- All POSTs creating financial state require an `Idempotency-Key` header.
- **Reserve-first (audit 01-HIGH):** `execute(scope,key,hash,supplier)` first INSERTs a
  `PENDING` `idempotency_record` — the unique constraint IS the lock. A concurrent loser
  catches the conflict and waits for / returns the winner's result. No check-then-act.
- **Same-tx finalize:** the idempotency record's result is written in the SAME DB transaction
  as the business effect (and its outbox row), so all commit or none — a crash can't leave an
  executed effect with no record (audit 01-MEDIUM).
- Same key + same body → replay original; same key + different body → `409`.
- **Cross-module writes (audit 08-MEDIUM):** when a context posts to the ledger as part of its
  own write (investment mirror, reconciliation confirm), the ledger post participates in the
  **same physical DB transaction**; on replay, look up the pre-existing txn by shared key and
  attach — never re-post. Multi-leg operations derive **per-leg sub-keys** deterministically
  from the request key.
- Financial-write idempotency keys are **not purged** (or the ledger's own persisted
  `idempotency_key` is the permanent backstop) so a late retry can't double-post (known #5).

## 8. Read vs write separation

- **Truth** lives in `ledger` (append-only). Everything user-facing (analytics, budgeting,
  forecasts, insights) reads **projections** rebuilt from the outbox — never mutates truth.
- Projections are **rebuildable**: replaying the outbox from genesis reproduces identical
  state (tested). Every fold must therefore be either commutative/additive on **signed**
  values or carry enough lineage to be replay-exact — a magnitude-only fold that can't net out
  a reversal is forbidden (audit 05-CRITICAL).
- Projections may be eventually consistent; truth is strongly consistent. **Do not seed a
  derived computation from an eventually-consistent projection when strong consistency is
  required** — read `LedgerApi` for point-in-time balances (audit 07-MEDIUM).

## 9. Net worth (canonical definition)

Single authoritative formula (resolves the 05↔08 contradiction, audit Theme 2):

> **net worth(asOf) = Σ balance(ASSET) − Σ balance(LIABILITY) + Σ unrealizedGain(holdings)**

- Investment asset accounts hold **cost basis** in the ledger (truth). Market appreciation is
  added exactly once as `Σ unrealizedGain` from `InvestmentApi`; analytics never adds market
  value on top of the cost already in the ledger.
- INCOME/EXPENSE/EQUITY accounts are flows/plugs and are excluded.
- **Postings hit leaf accounts only** (audit 02-HIGH); parent accounts are pure aggregation,
  so per-type sums iterate leaves and never double-count.
- **Illiquid / manually-valued assets** (real estate, gold, EPF/PPF/NPS) reuse the same seam:
  a `manual_valuation` mark contributes its `unrealizedGain` (mark − cost) via the same term.
  See §12.

## 10. Corrections: reversals & refunds (cross-cutting)

Distinct concepts — both first-class (audit Theme 1):

- **Reversal** = undoing a booking error. A new transaction with negated postings,
  `source=REVERSAL`, `reverses_txn_id` set. Its business **timestamp = correction time
  (now)**, never the original's — so point-in-time history before the correction is unchanged
  (audit 02-CRITICAL). `REVERSED` is derived from the reversing row's existence (§5), not
  stored. `UNIQUE(owner_id, reverses_txn_id)` + guard: only a `POSTED`, non-reversal,
  not-already-reversed txn may be reversed.
- **Refund/chargeback** = a real new event (money genuinely came back). An ordinary
  transaction **crediting the original expense account** (not a reversal), optionally carrying
  `related_txn_id` for analytics linkage. This is why spend/envelope attribution must key on
  `account.type` + **signed** amount (an expense credit reduces spend), never on posting sign
  (audit 05/06).
- Consumers that hold matched/derived state (reconciliation, analytics, budgeting) MUST handle
  `TransactionReversed` (audit 04-HIGH).

## 11. REST API conventions

- Base `/api/v1`, JSON, resource-oriented plural nouns.
- Idempotency-Key on all financial-state POSTs (§7).
- Errors: RFC 9457 `application/problem+json` with a stable machine `code` enum per context.
- Money in JSON: `{ "amountMinor": 12345, "currency": "INR" }`. Quantities:
  `{ "quantityScaled": 1234560000 }`. Never floats.
- Cursor pagination (`?cursor=&limit=`), cursor = opaque `seq`. Time = ISO-8601 UTC.
- Bean-Validation on DTOs; 422 on violation.

## 12. Shared dimensions, reserved seams & the context map

- **Category is a shared dimension owned by a dedicated `category/` context** (audit Part E
  #1) — a hierarchical, user-editable taxonomy + a `CategoryResolver` port that analytics,
  budgeting, forecasting, and import all consume. No context defines its own category notion;
  no per-context `category TEXT`. A `CategoryReassigned` event re-homes historical rollups.
- **Notification** is a first-class `notification/` context (audit Part B #1): the sole
  consumer that delivers `budget_alert` / `InsightRaised` / `DiscrepancyFlagged` / due-reminder
  events (ranking, dedup, digest, quiet hours, channels). Producers already emit; delivery is
  no longer "out of scope".
- **Reserved seams (design now, full build later — the approved "reserve seams" decision):**
  1. **Account financial-product metadata** — nullable `rate_bps`, `tenure_months`,
     `credit_limit_minor`, `yield_bps`, `statement_day`, `due_day` on/beside `account`, so the
     loan model and the `IDLE_CASH`/`DEBT_OPT` detectors have a real source (audit Part E #2).
     A detector MUST suppress (not default-to-zero) when the number is absent.
  2. **Manual valuation / mark** — a `manual_valuation(account_id, as_of, value_minor)` seam
     for illiquid assets feeding §9's unrealized-gain term.
  3. **Recurring-obligation registry** — a canonical `obligation` interface (cadence, amount,
     counterparty, next-due, usage signal) that import *detects into*, forecasting projects,
     budgeting reserves, and optimization audits — replacing today's four partial notions
     (audit Part E #4).

- **Context map (post-amendment):** `ledger`, `import`, `reconciliation`, `analytics`,
  `budgeting`, `forecasting`, `investment`, `optimization`, `platform`, **`category` (new)**,
  **`notification` (new)**. Roadmap contexts (own phases, seams reserved above): `liabilities`,
  `goals`, `tax`, `copilot`.

## 13. Failure posture

- Ledger writes fail closed (any invariant violation aborts the tx).
- Import/reconciliation are fault-tolerant, retry-safe, resumable; a bad row quarantines and
  never aborts the batch.
- All external-facing operations are idempotent (§7).

## 14. Testing baseline (per context)

- Domain invariants: unit + property tests.
- Persistence: Testcontainers Postgres (real constraints/triggers).
- Idempotency: replay-same-input → no duplicate effect; concurrent same-key → one effect.
- Projections: replay-from-genesis determinism, **including a post-then-reverse-nets-to-zero
  test on every signed fold** (audit 05).
- API: contract tests per endpoint (happy + validation + error `code`).

## 15. Java/module conventions

- Java 21, Spring Boot 3.x. One module/package per context under `com.midas.<context>`.
- Each context exposes a narrow public API interface (`LedgerApi`, `ImportApi`, `CategoryApi`,
  `NotificationApi`, …); cross-context calls go through the interface only.
- Package-private implementation; only the API interface + DTOs are public.
