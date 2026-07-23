# 06 — Budgeting Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 4**.
**Amended 2026-07-23** (audit remediation — see "## Audit fixes applied" at the end).

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
  remaining days of the current period. It is a **period-scoped available-to-spend
  flow** (this period's income + carried buffer), not the whole liquid stock — floored
  by a target-minimum-balance and net of *all* known future essentials/obligations (not
  just this one period's), so the number can never advertise draining lifetime savings.
- **Rollover:** carry an envelope's unspent (or overspent) balance into the next period
  when the envelope is flagged rollover.
- **Overspend detection:** detect envelope overspend and write **alert records** other
  systems / the UI read. Delivery (push/email) is out of scope.
- **Discretionary-spend visibility:** expose per-envelope funded/spent/available and a
  discretionary-vs-essential split.

**Explicit dependencies (assumptions marked):**

- **Category dimension** is owned by the dedicated **`category/` context** (conventions
  §12) — a hierarchical, user-editable taxonomy exposing a single `CategoryResolver` port
  that analytics, budgeting, forecasting, and import all consume. No context (Analytics
  included) defines its own category notion. Budgeting stores `category_id` on each
  envelope and resolves an incoming posting's category through **the shared
  `CategoryResolver`**. *Assumption:* every expense posting resolves to at most one
  `category_id`; postings with no category fall to a reserved **Uncategorized** category
  and are surfaced, not silently dropped. Until the `category/` context ships the shared
  resolver, Budgeting reads a local mirror projection built from the same outbox — **and
  on the mirror→resolver swap (or any resolver id change) runs a re-attribution pass**
  (see §Actual-spend attribution) so stored `envelope.category_id` and resolver output
  can never silently disagree.
- **Committed/recurring obligations** for safe-to-spend come from the shared
  **recurring-obligation registry** (conventions §12.3, reserved seam #3) — the canonical
  `obligation` interface (cadence, amount, counterparty, next-due, usage signal) that
  import detects into, forecasting projects, and budgeting reserves. Budgeting reads it
  through a `CommittedObligationsPort`. Future-dated lumps beyond this period (next
  quarter's insurance, annual premiums) are reserved here as a **sinking-fund reserve**,
  not left unmodelled. *Assumption:* if the registry is unavailable in Phase 4, the term
  degrades to `0` and safe-to-spend is flagged `obligationsAvailable=false` rather than
  silently overstating spendable cash.
- **Liquid balance** for safe-to-spend comes from **Ledger** via `LedgerApi.balances` /
  `netPosition`, summing accounts tagged liquid (see §Safe-to-spend). Ledger is the
  source of truth; Budgeting only reads it. **`system_kind` accounts (e.g. the In-Transit
  clearing account) are pinned non-liquid and excluded from `liquidBalance`** — money in
  flight, or a mis-inferred external payment parked in In-Transit, must never inflate
  spendable cash by netting against the funding account.

## Schema

All tables are owner-scoped projections. `budget_*` prefix per conventions §5 (Budgeting
is not the core domain). PKs are UUID v7, money is `BIGINT` minor units + `currency`.

```sql
-- A named budget period template (monthly in Phase 4). One active period definition
-- per owner; periods are generated from it. Kept explicit so weekly/custom is additive.
-- Period bucketing is computed in OWNER-LOCAL time (owner_settings.timezone), NOT UTC,
-- and `kind` carries the first-class period types of conventions §3 including the India
-- Financial Year (Apr 1 – Mar 31). starts_on/ends_on are owner-local civil dates.
CREATE TABLE budget_period (
    id            UUID PRIMARY KEY,               -- v7
    owner_id      UUID NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'MONTH',  -- DAY|WEEK|MONTH|FY (conventions §3)
    period_key    TEXT NOT NULL,                  -- 'YYYY-MM' (MONTH), 'FY2026-27' (FY), etc.
    starts_on     DATE NOT NULL,                  -- inclusive, owner-local calendar
    ends_on       DATE NOT NULL,                  -- inclusive, owner-local calendar
    status        TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN|CLOSED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, kind, period_key)
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
    spent_minor     BIGINT NOT NULL DEFAULT 0,    -- net attributed EXPENSE-leg amount (signed sum);
                                                  -- an expense debit adds, an expense credit (refund) subtracts
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
    amount_minor    BIGINT NOT NULL,              -- SIGNED EXPENSE-leg amount: positive = spend
                                                  -- (expense debit), negative = spend-reducer
                                                  -- (expense credit / refund, or reversal compensation)
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

**Stock vs flow (the load-bearing fix).** Safe-to-spend is a **period-scoped
available-to-spend flow**, NOT the whole liquid stock minus one period of commitments.
The pool is *this period's* money to spend — period income plus any carried buffer —
never a lifetime of accumulated savings. A target minimum balance is always protected,
and *all* known future essentials/obligations (not just this period's) are reserved via
the sinking-fund reserve, so the number can never advertise draining savings.

```
availablePool = periodIncomeToDate            -- income posted into this period so far
              + carriedBuffer                 -- unspent buffer carried from prior period(s)

spendablePool = min( availablePool,            -- flow ceiling: only this period's money
                     liquidBalance             -- solvency ceiling: never budget cash we lack
                       − targetMinimumBalance ) -- (§term) safety floor that always stays

safeToSpend =  spendablePool
             − reservedEssentialsAndObligations   -- de-duplicated; see below
             − sinkingFundReserve                 -- future-dated lumps beyond this period
             − remainingDiscretionaryRollovers    -- protected carried balances, optional

safeToSpendPerDay = HALF_EVEN( safeToSpend / max(1, remainingDaysInPeriod) )
```

Term definitions:

- **`availablePool`** — the flow: `periodIncomeToDate` (income postings bucketed into the
  current owner-local period) `+ carriedBuffer` (buffer carried forward). This — not the
  liquid stock — is the base, so an owner with large savings is not told nearly their
  whole net liquid position is discretionary.
- **`liquidBalance`** — sum of balances of accounts the owner marks **liquid** (checking,
  savings, wallet), from `LedgerApi.balances(asOf=now)`. Used only as a **solvency
  ceiling** (you cannot spend cash you do not hold), never as the base pool. *Assumption:*
  liquidity is a Ledger/`category`-side attribute; Budgeting reads it, does not own it.
  Illiquid assets (investments, property) and credit lines are excluded, **and
  `system_kind` accounts (In-Transit clearing) are pinned non-liquid and excluded** so
  money in flight / a mis-inferred external outflow can't inflate the ceiling.
- **`targetMinimumBalance`** — an owner-configured safety floor (default a sensible
  minimum, e.g. one month of essentials) that is never offered as spendable. This is what
  stops the headline from ever advertising a drain of savings.
- **`reservedEssentialsAndObligations`** — the **single, de-duplicated** reservation of
  committed money, so rent/EMI/SIP is reserved **exactly once** (audit HIGH #3). Built by:
  1. For each `ESSENTIAL` envelope in the period, its remaining reserve
     `max(0, allocated + rollover − spent)`.
  2. For each committed outflow from the `CommittedObligationsPort` (recurring-obligation
     registry, conventions §12.3) due in `(now, period.ends_on]`, **only if it does not
     map to an ESSENTIAL envelope already counted in (1)** — obligations whose category
     resolves to a reserved essential envelope are dropped from this term (the two notions
     overlap almost perfectly: "rent, EMIs, SIPs, known bills" *are* what users park in
     ESSENTIAL envelopes).
  3. **Match obligations to actual postings:** an obligation already paid this period
     (e.g. an EMI auto-debited early on the 18th though its nominal due date is the 25th)
     is detected via its matching expense posting and **excluded** — it has already left
     `liquidBalance`, so re-reserving it would double-count against already-spent money.
  Discretionary envelopes are deliberately excluded — spending them **is** the
  safe-to-spend budget.
- **`sinkingFundReserve`** — future-dated lumps beyond the current period (next quarter's
  insurance, annual premiums) reserved from the recurring-obligation registry so they are
  not silently treated as spendable now. *Assumption:* if the registry is absent → `0`,
  response flags `obligationsAvailable=false`.
- **`remainingDiscretionaryRollovers`** *(optional, default off)* — if the owner opts to
  protect carried rollover balances from the safe-to-spend pool, subtract them; otherwise
  `0`. Marked assumption: default behavior treats rollover as spendable.
- **`remainingDaysInPeriod`** — inclusive days from `today` to `period.ends_on` in the
  owner's local calendar; floored at 1 to avoid divide-by-zero on the last day. The
  per-day division rounds **HALF_EVEN** (conventions §2). On the last day
  (`remainingDays == 1`) `perDay == safeToSpend`; the response labels this so the UI does
  not present the whole remaining pool as a single day's allowance.

The result carries provenance so the UI can explain it:
`{ safeToSpendMinor, perDayMinor, terms:{...each term...}, obligationsAvailable,
lastDayOfPeriod, asOf }`. If `safeToSpend < 0`, a `SAFE_TO_SPEND_NEGATIVE` alert is
raised (§Overspend).

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

1. **Attribute strictly on `account.type == EXPENSE`, resolved via
   `LedgerApi.account(accountId).type` — never on posting sign** (conventions §10). Skip
   any posting whose account is not an EXPENSE account. This is what makes refunds and
   transfers correct:
   - A **refund** posted as a *new* transaction (not a ledger reversal) credits the
     expense account — a **negative** expense-leg amount — and MUST reduce spend. A
     sign-based test (`amountMinor > 0`) would wrongly skip it, leaving phantom spend and
     overstating safe-to-spend.
   - A **credit-card bill payment / transfer** (`debit LIABILITY / credit ASSET`) has no
     EXPENSE leg, so nothing attributes — no phantom Unbudgeted spend, no spurious alert.
2. Take the **signed expense-leg `amountMinor`** as the attribution amount (expense debit
   positive = spend; expense credit negative = spend-reducer). Do not infer expense-ness
   from sign.
3. Resolve `category_id` via the shared **`CategoryResolver`** (conventions §12);
   unresolved → **Uncategorized** envelope.
4. Find the envelope for `(owner, category_id)`. If none exists, attribute to a synthetic
   **Unbudgeted** envelope so spend is never invisible (feeds discretionary visibility).
5. Map `occurred_at` (event business time) → the containing `budget_period` in
   **owner-local** time (conventions §3); auto-create the period from the template if the
   event lands in a not-yet-materialized period.
6. Insert a `budget_spend_attribution` row (signed amount) and add it into
   `budget_spend_rollup.spent_minor` (signed sum) in the **same DB transaction**; advance
   `last_event_id`.
7. Evaluate overspend thresholds for the affected envelope (§Overspend) — **only when the
   envelope's `funded > 0`** (Unbudgeted/Uncategorized/zero-allocation envelopes are
   info-only, never OVERSPENT).

On **`TransactionReversed`** (consumed per conventions §10 — a true reversal, distinct
from a refund): look up prior attributions by `txn_id`, insert compensating negative
attribution rows, and decrement the matching rollups. Because attribution is keyed per
posting and append-only over **signed** amounts, both reversals and refunds net out
exactly and replay-safe.

**Category resolver / mirror swap — historical re-attribution.** Because envelopes store
`category_id` chosen against the local mirror while the real `CategoryResolver` may resolve
the same concept to a different id, a swap (or any `CategoryReassigned`) MUST trigger a
**re-attribution pass** over the shared `category/` resolver (conventions §12): historical
attributions are re-homed to the envelope that now matches the resolved concept. This
prevents essential spend (e.g. rent) from being stranded in Unbudgeted — which would
otherwise leave the Rent envelope showing `spent=0`, keep its full essential reserve, and
double-subtract rent (once from `liquidBalance`, once as an un-consumed reserve). The
re-attribution asserts every `envelope.category_id` survives the swap or is remapped.

## Overspend detection & alerts

After each rollup change (and on allocation edits), evaluate the envelope. **Guard
`funded = allocated + rollover > 0` first** — if `funded ≤ 0` (Unbudgeted, Uncategorized,
any freshly-created or zero-allocation envelope), the envelope is **info-only**: no
OVERSPENT/NEAR_LIMIT alert is raised and no `/funded` division is performed (this both
prevents the CRITICAL-alert storm on every un-enveloped posting and avoids the
divide-by-zero / NaN in `pctUsed`/`threshold_bp`). With `funded > 0`:

- `spent > funded` → raise/refresh `ENVELOPE_OVERSPENT`
  (`over_minor = spent − funded`, `severity=CRITICAL`).
- `spent ≥ nearLimitThreshold × funded` (default 80%, `threshold_bp=8000`) and not yet
  overspent → `ENVELOPE_NEAR_LIMIT` (`WARNING`).
- Period-level: `safeToSpend < 0` → `SAFE_TO_SPEND_NEGATIVE` (envelope_id null).

`pctUsed`/`threshold_bp` (basis points of `funded`) are only defined and only computed
when `funded > 0`; the info-only envelopes report `pctUsed = null`.

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
- `TransactionReversed` — compensate prior attributions (conventions §10; consumed by
  every context holding matched/derived state).
- `CategoryReassigned` (from the shared `category/` context, conventions §12) — re-home
  the affected postings' spend between envelopes. On the mirror→resolver swap this drives
  the historical **re-attribution pass** (§attribution); it is no longer merely
  "tolerated-if-absent" — a swap that changes resolved ids MUST re-attribute so essential
  spend is never stranded in Unbudgeted.

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
- **Reversal & refund:** `TransactionReversed` exactly backs out prior attributions; a
  post + reverse nets an envelope's spend to its pre-txn value. **Refund (not a reversal):
  a new txn crediting the expense account reduces spend** via the signed expense leg —
  spent returns to pre-bill value even though no `TransactionReversed` arrives.
- **Attribution keys on account type, not sign:** a credit-card bill payment
  (`debit LIABILITY / credit ASSET`) attributes **nothing** (no phantom Unbudgeted spend,
  no CRITICAL alert); an expense credit reduces spend; only EXPENSE-account legs attribute.
- **Safe-to-spend (property):** the pool is the period **flow** (`periodIncome +
  carriedBuffer`) capped by `liquidBalance − targetMinimumBalance`, so a large liquid
  stock never inflates the headline; rent/EMI is reserved **exactly once** (an obligation
  mapping to a reserved ESSENTIAL envelope is not double-subtracted); an early-paid
  obligation matched to its posting is not re-reserved; `system_kind`/In-Transit accounts
  are excluded from `liquidBalance`; discretionary envelopes are never counted as reserved;
  `perDay` (HALF_EVEN) never divides by zero and is labelled on the last day. Degrades to
  `obligationsAvailable=false` when the port is absent.
- **Zero-funded envelope:** a posting to an Unbudgeted/Uncategorized/zero-allocation
  envelope raises **no** OVERSPENT/NEAR_LIMIT alert and performs no `/funded` division.
- **Resolver/mirror swap re-attribution:** after swapping the mirror for the real
  `CategoryResolver` (or a `CategoryReassigned`), historical spend is re-homed to the
  matching envelope; rent no longer strands in Unbudgeted and is not double-subtracted.
- **Owner-local & FY periods:** a post-23:30 IST transaction buckets into its IST period;
  FY (`Apr 1 – Mar 31`) periods are supported alongside MONTH/WEEK/DAY (conventions §3).
- **Rollover:** unspent carries only for `rollover=TRUE` envelopes; overspend carries as
  signed debt unless `rolloverFloorZero`; re-closing a period is a no-op.
- **Overspend alerts:** crossing 80% raises NEAR_LIMIT once (not per posting); crossing
  100% raises OVERSPENT; clearing the condition auto-resolves; the unique constraint
  prevents duplicate OPEN alerts.
- **API contract tests:** happy + validation (422 on negative allocation) + error `code`
  per endpoint; `Idempotency-Key` replay returns the original result.
- **No-truth-mutation:** static/integration assertion that Budgeting holds no
  INSERT/UPDATE grant on `ledger_transaction` / `posting`.

## Audit fixes applied

**Amended 2026-07-23** per the adversarial design audit (06 Budgeting). Mapping of each
finding to the change:

| Audit finding | Severity | Resolution |
|---|---|---|
| Safe-to-spend subtracts one period of commitments from the *entire* liquid stock | HIGH | §Safe-to-spend recast as a **period-scoped available-to-spend flow** (`periodIncomeToDate + carriedBuffer`), capped by `liquidBalance − targetMinimumBalance`. Every term defined; a `targetMinimumBalance` floor + `sinkingFundReserve` for all known future essentials mean the headline can never advertise draining savings. Future lumps reserved via the recurring-obligation registry seam (conventions §12.3). |
| Attribution filtered by posting *sign* not *account type* (refunds not credited, transfers booked as spend) | HIGH | §attribution step 1 now keys strictly on `account.type == EXPENSE` resolved via `LedgerApi`, using the **signed expense-leg amount** so expense credits (refunds) reduce spend and liability/asset legs never attribute (conventions §10). Schema comments on `spent_minor`/`amount_minor` rewritten to signed semantics. |
| Essential allocations and committed obligations double-counted (rent/EMI twice) | HIGH | `reservedEssentialsAndObligations` is a **single de-duplicated** term: an obligation mapping to an already-reserved ESSENTIAL envelope is dropped, and obligations **matched to actual postings** (early-paid) are excluded, so rent/EMI is reserved exactly once. |
| In-Transit clearing account liquidity unspecified | MEDIUM | `system_kind` accounts (In-Transit) **pinned non-liquid and excluded** from `liquidBalance` (§Explicit dependencies, §Safe-to-spend `liquidBalance`). |
| Zero-funded envelope trips CRITICAL overspent + divide-by-zero | MEDIUM | §Overspend guards `funded > 0` before any overspend/near-limit evaluation and before any `/funded`; Unbudgeted/Uncategorized/zero-allocation envelopes are info-only (`pctUsed = null`). |
| `category_id` divergence (stored envelope vs resolver/mirror) | MEDIUM | Resolver owned by the shared `category/` context (conventions §12); mirror→resolver swap / `CategoryReassigned` triggers a **historical re-attribution pass** so essential spend is never stranded in Unbudgeted. |
| perDay rounding & last-day full-balance signal | LOW | `safeToSpendPerDay` rounds **HALF_EVEN** (conventions §2); last-day (`remainingDays == 1`) is labelled `lastDayOfPeriod` in the response. |
| `TransactionReversed` handling / owner-local + FY periods | conventions §3/§10 | `TransactionReversed` explicitly consumed; `budget_period` carries `kind (DAY|WEEK|MONTH|FY)` and buckets in owner-local time. |

Cross-references: conventions §2 (money/rounding), §3 (owner-local periods + FY), §10
(reversal vs refund, signed attribution), §12 (shared `category/` resolver +
recurring-obligation registry seam).
