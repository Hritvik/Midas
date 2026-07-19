# 06 — Budgeting Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 4**.

A read-side decision-support context. Its single job is to answer **"how much can I
safely spend?"** Budgeting owns envelopes, monthly allocations, actual-spend rollups,
safe-to-spend, and overspend alert records. It **consumes** `TransactionPosted` /
`TransactionReversed` from the ledger outbox and **never mutates ledger truth** — all
its tables are rebuildable projections (conventions §8). INR only.

## Responsibilities

- **Envelope budgeting:** one envelope per (category, period); a monthly allocation
  amount per envelope.
- **Actual-spend attribution:** map ledger postings to envelopes by category as
  `TransactionPosted` events arrive; maintain per-envelope spend rollups.
- **Safe-to-spend:** compute how much the owner can discretionarily spend over the
  remaining days of the current period, net of committed obligations and essential
  allocations.
- **Rollover:** carry an envelope's unspent (or overspent) balance into the next period
  when the envelope is flagged rollover.
- **Overspend detection:** detect envelope overspend and write **alert records** other
  systems / the UI read. Delivery (push/email) is out of scope.
- **Discretionary-spend visibility:** expose per-envelope funded/spent/available and a
  discretionary-vs-essential split.

**Explicit dependencies (assumptions marked):**

- **Category dimension** is defined by the **Analytics** context (`05-analytics`). Both
  contexts share one **category concept** — a stable `category_id` (UUID) per owner.
  Budgeting stores `category_id` on each envelope and resolves an incoming posting's
  category through a **category-resolution port** (`CategoryResolver`, provided by
  Analytics). *Assumption:* every expense posting can be resolved to at most one
  `category_id`; postings with no category fall to a reserved **Uncategorized** category
  and are surfaced but not silently dropped. Until Analytics ships the shared resolver,
  Budgeting reads a local mirror projection built from the same outbox.
- **Committed/recurring obligations** for safe-to-spend come from the **Forecasting**
  context (`07-forecasting`), which owns recurring-transaction detection. Budgeting reads
  them through a `CommittedObligationsPort`. *Assumption:* if Forecasting is unavailable
  in Phase 4, the term degrades to `0` and safe-to-spend is flagged
  `obligationsAvailable=false` rather than silently overstating spendable cash.
- **Liquid balance** for safe-to-spend comes from **Ledger** via `LedgerApi.balances` /
  `netPosition`, summing accounts tagged liquid (see §Safe-to-spend). Ledger is the
  source of truth; Budgeting only reads it.

## Schema

All tables are owner-scoped projections. `budget_*` prefix per conventions §5 (Budgeting
is not the core domain). PKs are UUID v7, money is `BIGINT` minor units + `currency`.

```sql
-- A named budget period template (monthly in Phase 4). One active period definition
-- per owner; periods are generated from it. Kept explicit so weekly/custom is additive.
CREATE TABLE budget_period (
    id            UUID PRIMARY KEY,               -- v7
    owner_id      UUID NOT NULL,
    period_key    TEXT NOT NULL,                  -- 'YYYY-MM' (monthly)
    starts_on     DATE NOT NULL,                  -- inclusive, owner-local calendar
    ends_on       DATE NOT NULL,                  -- inclusive
    status        TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN|CLOSED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, period_key)
);
CREATE INDEX budget_period_owner_idx ON budget_period (owner_id, starts_on);

-- The envelope: a budget bucket for one category, persistent across periods.
-- Holds policy (rollover, essential/discretionary); per-period money lives in
-- budget_allocation and budget_spend_rollup.
CREATE TABLE budget_envelope (
    id             UUID PRIMARY KEY,              -- v7
    owner_id       UUID NOT NULL,
    name           TEXT NOT NULL,                 -- 'Groceries', 'Dining out'
    category_id    UUID NOT NULL,                 -- shared category concept (Analytics)
    kind           TEXT NOT NULL DEFAULT 'DISCRETIONARY', -- ESSENTIAL|DISCRETIONARY
    rollover       BOOLEAN NOT NULL DEFAULT FALSE,-- unspent carries to next period
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    status         TEXT NOT NULL DEFAULT 'ACTIVE',-- ACTIVE|ARCHIVED
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, category_id),               -- one envelope per category per owner
    UNIQUE (owner_id, name)
);
CREATE INDEX budget_envelope_owner_idx ON budget_envelope (owner_id, status);

-- Per-period funded amount for an envelope, plus rollover carried in from the prior
-- period. Editable (allocations are a user decision, not truth) → has updated_at.
CREATE TABLE budget_allocation (
    id               UUID PRIMARY KEY,            -- v7
    owner_id         UUID NOT NULL,
    envelope_id      UUID NOT NULL REFERENCES budget_envelope(id),
    period_id        UUID NOT NULL REFERENCES budget_period(id),
    allocated_minor  BIGINT NOT NULL DEFAULT 0,   -- money the user put in this period
    rollover_minor   BIGINT NOT NULL DEFAULT 0,   -- carried from prior period (signed)
    currency         CHAR(3) NOT NULL DEFAULT 'INR',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, envelope_id, period_id),
    CHECK (allocated_minor >= 0)
);
CREATE INDEX budget_allocation_period_idx ON budget_allocation (owner_id, period_id);

-- Actual spend attributed to an envelope in a period. A projection rebuilt from the
-- outbox: dropping and replaying TransactionPosted MUST reproduce it (conventions §8).
-- spent_minor is the running attributed spend; posting_count aids debugging.
CREATE TABLE budget_spend_rollup (
    id              UUID PRIMARY KEY,             -- v7
    owner_id        UUID NOT NULL,
    envelope_id     UUID NOT NULL REFERENCES budget_envelope(id),
    period_id       UUID NOT NULL REFERENCES budget_period(id),
    spent_minor     BIGINT NOT NULL DEFAULT 0,    -- attributed expense, positive = spent
    posting_count   INT    NOT NULL DEFAULT 0,
    currency        CHAR(3) NOT NULL DEFAULT 'INR',
    last_event_id   UUID,                         -- high-water mark for idempotent apply
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, envelope_id, period_id)
);
CREATE INDEX budget_spend_rollup_period_idx ON budget_spend_rollup (owner_id, period_id);

-- Individual attribution line: which posting hit which envelope. Append-only audit of
-- the projection; lets a reversal subtract exactly and makes replay deterministic.
CREATE TABLE budget_spend_attribution (
    id              UUID PRIMARY KEY,             -- v7
    owner_id        UUID NOT NULL,
    envelope_id     UUID NOT NULL REFERENCES budget_envelope(id),
    period_id       UUID NOT NULL REFERENCES budget_period(id),
    txn_id          UUID NOT NULL,                -- ledger transaction id (not FK: cross-context)
    posting_id      UUID NOT NULL,
    amount_minor    BIGINT NOT NULL,              -- positive = spend; negative = reversal
    currency        CHAR(3) NOT NULL DEFAULT 'INR',
    occurred_at     TIMESTAMPTZ NOT NULL,         -- business time from the event
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, posting_id)                 -- idempotent: a posting attributes once
);
CREATE INDEX budget_attr_env_period_idx ON budget_spend_attribution (envelope_id, period_id);

-- Overspend / threshold alert records. Records only — other systems/UI read them;
-- Budgeting does NOT deliver them (conventions/PRD: delivery out of scope).
CREATE TABLE budget_alert (
    id              UUID PRIMARY KEY,             -- v7
    owner_id        UUID NOT NULL,
    envelope_id     UUID REFERENCES budget_envelope(id), -- null = period-level alert
    period_id       UUID NOT NULL REFERENCES budget_period(id),
    alert_type      TEXT NOT NULL,                -- ENVELOPE_OVERSPENT|ENVELOPE_NEAR_LIMIT|SAFE_TO_SPEND_NEGATIVE
    severity        TEXT NOT NULL DEFAULT 'WARNING', -- INFO|WARNING|CRITICAL
    threshold_bp    INT,                          -- basis points of allocation that tripped it (e.g. 8000 = 80%)
    over_minor      BIGINT,                       -- amount over the funded line (if applicable)
    currency        CHAR(3) NOT NULL DEFAULT 'INR',
    status          TEXT NOT NULL DEFAULT 'OPEN', -- OPEN|RESOLVED (auto-resolves when condition clears)
    detail          JSONB,                        -- machine-readable context for the reader
    raised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    -- one open alert of a type per envelope+period; re-raise updates, not duplicates
    UNIQUE (owner_id, period_id, envelope_id, alert_type, status)
);
CREATE INDEX budget_alert_owner_open_idx ON budget_alert (owner_id, status, raised_at);
```

**Derived-value convention:** an envelope's **available** for a period is
`allocated_minor + rollover_minor - spent_minor` (a `Money` derivation, not stored).
Negative available = overspent. `budget_spend_rollup` is a materialized accelerator;
`budget_spend_attribution` is the append-only source it is rebuildable from.

**No truth mutation:** none of these tables feed the ledger. Editing an allocation is a
budget decision; it emits a Budgeting event but never a posting.

## Safe-to-spend

The headline number. Computed for the **current open period** over its **remaining days**.
All terms are `Money` (paise, INR). Marked assumptions are the seams that degrade
gracefully.

```
safeToSpend =  liquidBalance
             − upcomingCommittedObligations      (remaining in this period)
             − remainingEssentialAllocations     (unspent essential envelopes)
             − remainingDiscretionaryRollovers   (protected carried balances, optional)

safeToSpendPerDay = safeToSpend / max(1, remainingDaysInPeriod)
```

Term definitions:

- **`liquidBalance`** — sum of balances of accounts the owner marks **liquid** (checking,
  savings, wallet), from `LedgerApi.balances(asOf=now)`. *Assumption:* an account's
  liquidity flag is an Analytics/Ledger-side attribute; Budgeting reads it, does not own
  it. Illiquid assets (investments, property) and credit lines are excluded.
- **`upcomingCommittedObligations`** — sum of recurring/committed outflows with a due date
  in `(now, period.ends_on]` from `CommittedObligationsPort` (Forecasting). Rent, EMIs,
  SIPs, known bills. *Assumption:* if Forecasting is absent → `0`, response flags
  `obligationsAvailable=false`.
- **`remainingEssentialAllocations`** — for each `ESSENTIAL` envelope in the period,
  `max(0, allocated + rollover − spent)`, summed. This *reserves* money the user has
  committed to essentials so it is never reported as safe to spend. Discretionary
  envelopes are deliberately excluded — spending them **is** the safe-to-spend budget.
- **`remainingDiscretionaryRollovers`** *(optional, default off)* — if the owner opts to
  protect carried rollover balances from the safe-to-spend pool, subtract them; otherwise
  `0`. Marked assumption: default behavior treats rollover as spendable.
- **`remainingDaysInPeriod`** — inclusive days from `today` to `period.ends_on` in the
  owner's local calendar; floored at 1 to avoid divide-by-zero on the last day.

The result carries provenance so the UI can explain it:
`{ safeToSpendMinor, perDayMinor, terms:{...each term...}, obligationsAvailable,
asOf }`. If `safeToSpend < 0`, a `SAFE_TO_SPEND_NEGATIVE` alert is raised (§Overspend).

**Determinism note:** safe-to-spend is a *query-time* computation over projections +
live ledger reads; it is not stored. Two calls with the same inputs and clock return the
same number (injected `Clock`, conventions §3).

## Rollover

Per-envelope `rollover` flag. When a period **closes** (`budget_period.status → CLOSED`,
driven by a scheduled month-end job or the first event in the next period):

1. For each envelope with `rollover = TRUE`, compute closing
   `available = allocated + rollover_minor − spent` for the closing period.
2. Create/patch the **next** period's `budget_allocation` for that envelope, setting its
   `rollover_minor = closingAvailable` (signed — a negative available carries the
   overspend forward as a debt against next period, unless the envelope is configured
   `rolloverFloorZero`, in which case negatives clamp to 0 and the overspend is dropped).
3. Envelopes with `rollover = FALSE` start the next period with `rollover_minor = 0`;
   unspent funds simply expire.

Rollover is **idempotent and rebuildable**: it is derived from closed-period rollups, so
replaying the outbox from genesis reproduces every period's `rollover_minor` (tested,
conventions §10). Re-closing an already-closed period is a no-op.

## Actual-spend attribution (consuming the outbox)

Consumer `budgeting.spend-projector` handles `TransactionPosted` / `TransactionReversed`.
Dedup is on `(consumer_name, event_id)` via `processed_event` (platform), plus the
`UNIQUE (owner_id, posting_id)` on `budget_spend_attribution` as a second guard so a
redelivery cannot double-count.

On **`TransactionPosted`**, for each posting:

1. Skip postings that are not expense-side (attribution targets expense category flow;
   sign/account-type filter per the ledger sign convention).
2. Resolve `category_id` via `CategoryResolver`; unresolved → Uncategorized envelope.
3. Find the envelope for `(owner, category_id)`. If none exists, attribute to a synthetic
   **Unbudgeted** envelope so spend is never invisible (feeds discretionary visibility).
4. Map `occurred_at` (event business time) → the containing `budget_period`; auto-create
   the period from the template if the event lands in a not-yet-materialized month.
5. Insert a `budget_spend_attribution` row and increment `budget_spend_rollup.spent_minor`
   in the **same DB transaction**; advance `last_event_id`.
6. Evaluate overspend thresholds for the affected envelope (§Overspend).

On **`TransactionReversed`**: look up prior attributions by `txn_id`, insert compensating
negative attribution rows, and decrement the matching rollups. Because attribution is
keyed per posting and append-only, reversal is exact and replay-safe.

## Overspend detection & alerts

After each rollup change (and on allocation edits), evaluate the envelope:

- `spent > allocated + rollover` → raise/refresh `ENVELOPE_OVERSPENT`
  (`over_minor = spent − funded`, `severity=CRITICAL`).
- `spent ≥ nearLimitThreshold × funded` (default 80%, `threshold_bp=8000`) and not yet
  overspent → `ENVELOPE_NEAR_LIMIT` (`WARNING`).
- Period-level: `safeToSpend < 0` → `SAFE_TO_SPEND_NEGATIVE` (envelope_id null).

Alert writes are **idempotent** via the `UNIQUE (owner_id, period_id, envelope_id,
alert_type, status)` constraint: an existing OPEN alert is updated (over_minor, detail)
rather than duplicated. When the condition clears (allocation raised, reversal), the
alert auto-transitions `OPEN → RESOLVED` with `resolved_at`. Alerts are **records other
systems read**; Budgeting does not push, email, or notify — that is a downstream reader's
job (PRD: delivery out of scope).

## Core operations (`BudgetingApi`)

- **`createEnvelope(cmd)`** — name, `categoryId`, `kind`, `rollover`. Enforces one
  envelope per category per owner. Emits `EnvelopeCreated`.
- **`allocate(cmd)`** — set/patch `allocated_minor` for `(envelope, period)`; auto-creates
  the period from the template if missing. Idempotent on `Idempotency-Key`. Re-evaluates
  overspend. Emits `AllocationSet`.
- **`updateEnvelopePolicy(envelopeId, cmd)`** — flip `rollover`, `kind`, archive. Emits
  `EnvelopePolicyChanged`.
- **`safeToSpend(asOf?)`** — the headline computation (§Safe-to-spend) for the current
  open period; returns the number plus per-term provenance and `obligationsAvailable`.
- **`envelopeStatus(period?)`** — per-envelope `{allocated, rollover, spent, available,
  pctUsed, state}` for a period; the discretionary dashboard source.
- **`discretionarySummary(period?)`** — period totals split ESSENTIAL vs DISCRETIONARY vs
  UNBUDGETED: funded, spent, available, and the discretionary safe-to-spend.
- **`closePeriod(periodId)`** — closes a period and applies rollover to the next
  (§Rollover). Idempotent; re-close is a no-op. Normally invoked by the scheduled
  month-end job, exposed for backfill/testing.
- **`listAlerts(status?)`** — read the alert records (also exposed over REST for the UI).

`BudgetingApi` is the only cross-context entry point (conventions §11). It calls out to
`LedgerApi`, `CategoryResolver`, and `CommittedObligationsPort` — never into another
context's repositories.

## Events consumed / emitted (outbox)

**Consumed** (via platform outbox dispatcher, idempotent consumers):

- `TransactionPosted` — attribute spend to envelopes (§attribution).
- `TransactionReversed` — compensate prior attributions.
- *(soft)* Analytics `CategoryReassigned` — if a transaction's category changes,
  re-attribute the affected postings (move spend between envelopes). Tolerated-if-absent
  in Phase 4 per the shared-category assumption.

**Emitted** (written to `outbox` in the same DB tx as the state change, conventions §6):

- `EnvelopeCreated` — `{envelopeId, categoryId, kind, rollover}`.
- `AllocationSet` — `{envelopeId, periodId, allocatedMinor, currency}`.
- `EnvelopePolicyChanged` — `{envelopeId, changes}`.
- `BudgetOverspent` — `{envelopeId, periodId, overMinor, spentMinor, fundedMinor}`.
- `SafeToSpendChanged` *(optional, throttled)* — `{periodId, safeToSpendMinor, perDayMinor}`
  for UI push / forecasting. Emitted on material change only.
- `PeriodClosed` — `{periodId, rolloverApplied:[{envelopeId, rolloverMinor}]}`.

All payloads carry `schemaVersion`; consumers tolerate unknown fields (conventions §6).

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/budget/envelopes` | create envelope (Idempotency-Key required) |
| GET | `/api/v1/budget/envelopes` | list envelopes (owner-scoped) |
| PATCH | `/api/v1/budget/envelopes/{id}` | update policy (rollover/kind/archive) |
| PUT | `/api/v1/budget/envelopes/{id}/allocations?period=` | set allocation (Idempotency-Key) |
| GET | `/api/v1/budget/safe-to-spend?asOf=` | safe-to-spend + per-term provenance |
| GET | `/api/v1/budget/status?period=` | per-envelope funded/spent/available |
| GET | `/api/v1/budget/discretionary?period=` | essential vs discretionary summary |
| GET | `/api/v1/budget/alerts?status=` | read alert records |
| POST | `/api/v1/budget/periods/{id}/close` | close period + apply rollover (Idempotency-Key) |

Conventions apply: `/api/v1`, cursor pagination on list endpoints, money as
`{amountMinor,currency}`, RFC 9457 problem+json errors with a stable `code` enum
(e.g. `BUDGET_ENVELOPE_CATEGORY_TAKEN`, `BUDGET_PERIOD_CLOSED`,
`BUDGET_ALLOCATION_NEGATIVE`).

## Testing

- **Projection determinism:** drop `budget_spend_rollup` / `budget_spend_attribution` /
  rollover state, replay the outbox from genesis → identical rollups, available balances,
  and `rollover_minor` for every period (conventions §8/§10).
- **Attribution idempotency:** redelivering the same `TransactionPosted` (same event_id,
  same posting_id) does not double-count; consumer dedup + `UNIQUE(posting_id)` both
  hold.
- **Reversal:** `TransactionReversed` exactly backs out prior attributions; a post +
  reverse nets an envelope's spend to its pre-txn value.
- **Safe-to-spend (property):** for generated ledger/allocation states, safe-to-spend =
  liquid − obligations − remaining essentials, never counts discretionary envelopes as
  reserved, and `perDay` never divides by zero on the last day of a period. Degrades to
  `obligationsAvailable=false` when the port is absent.
- **Rollover:** unspent carries only for `rollover=TRUE` envelopes; overspend carries as
  signed debt unless `rolloverFloorZero`; re-closing a period is a no-op.
- **Overspend alerts:** crossing 80% raises NEAR_LIMIT once (not per posting); crossing
  100% raises OVERSPENT; clearing the condition auto-resolves; the unique constraint
  prevents duplicate OPEN alerts.
- **API contract tests:** happy + validation (422 on negative allocation) + error `code`
  per endpoint; `Idempotency-Key` replay returns the original result.
- **No-truth-mutation:** static/integration assertion that Budgeting holds no
  INSERT/UPDATE grant on `ledger_transaction` / `posting`.
