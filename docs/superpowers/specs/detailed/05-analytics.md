# 05 — Analytics Context (Detailed Design)

**Amended 2026-07-23** (audit remediation, Tier 0/1). See "Audit fixes applied" at the end.

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 3**. The
read side. Analytics owns **no truth** — it builds rebuildable read-model **projections**
from the ledger outbox and answers "where does money go, am I building wealth, how long
does my cash last" questions. INR only.

## Responsibilities

- Consume `TransactionPosted` / `TransactionReversed` / `HoldingRevalued` / `CategoryReassigned`
  from the outbox and maintain **read-model projections** (owner-local monthly/FY rollups per
  account and per category, net-worth series with unrealized gain, liquidity buckets).
- Serve derived metrics: **net worth**, **monthly burn**, **cash runway**, **income vs
  expense**, **spending trends**, **category aggregation**, **liquidity analysis**.
- Guarantee **deterministic rebuild**: dropping all projection tables and replaying the
  outbox from genesis reproduces byte-identical projection state (conventions §8, a
  tested property).
- **Never** query or mutate ledger truth for a user-facing number except through the
  projection tables it owns, or through `LedgerApi` for point-in-time account metadata it
  cannot project (used read-only, never for the headline figures).

**Non-responsibilities:** no posting, no reversal, no balance mutation, no snapshot
writes into `ledger`. Analytics is strictly downstream and eventually consistent; truth
stays strongly consistent in `ledger`.

## Schema

All tables are analytics-owned projections (prefixed `analytics_`), **rebuildable and
truncatable**. None is a source of truth; none has `UPDATE`/`DELETE` restrictions beyond
the rebuild path. Money is `BIGINT` minor units (paise) + `currency CHAR(3)`; `owner_id`
on every row (conventions §2, §4).

```sql
-- Bookmark: last outbox event this context durably folded in. One row per owner.
-- Enables incremental catch-up and is the rebuild fence (reset to genesis to replay).
CREATE TABLE analytics_projection_offset (
    owner_id           UUID PRIMARY KEY,
    last_event_id      UUID,                 -- v7 id of last applied outbox row (time order)
    last_applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Staleness marker for the net-worth running-total series (audit 05-MEDIUM). NULL = series
    -- current. When a posting lands in month P, set to LEAST(networth_dirty_from, P): every
    -- analytics_networth_month row with period >= this value must be recomputed. The debounced
    -- pass recomputes from this watermark forward and resets it to NULL. "recompute if stale"
    -- is now testable: stale iff networth_dirty_from IS NOT NULL.
    networth_dirty_from DATE                 -- owner-local first-of-month; NULL when series is current
);

-- Monthly rollup of net signed movement per account. The atomic fact table; every
-- higher-level metric is derived from this + account metadata. period = first day of
-- the OWNER-LOCAL civil month (conventions §3), e.g. 2026-07-01. All civil-period
-- bucketing is owner-local, not UTC — a post-23:30 IST posting buckets into its IST month.
CREATE TABLE analytics_account_month (
    id            UUID PRIMARY KEY,        -- v7
    owner_id      UUID NOT NULL,
    account_id    UUID NOT NULL,
    account_type  TEXT NOT NULL,           -- ASSET|LIABILITY|EQUITY|INCOME|EXPENSE (denormalized)
    period        DATE NOT NULL,           -- month bucket, first-of-month in owner-local tz (§3)
    net_minor     BIGINT NOT NULL,         -- signed sum of postings on this account in the month
    posting_count INT   NOT NULL,          -- for reversibility / debug
    currency      CHAR(3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, account_id, period)
);
CREATE INDEX aam_owner_period_idx ON analytics_account_month (owner_id, period);
CREATE INDEX aam_owner_type_period_idx ON analytics_account_month (owner_id, account_type, period);

-- Monthly rollup per spending/earning category. Category is a SHARED DIMENSION resolved
-- via the category/ context's CategoryResolver (conventions §12), NOT an account-name
-- assumption (see "Category dimension"). One row per (category, period). period is the
-- owner-local civil month (§3).
CREATE TABLE analytics_category_month (
    id            UUID PRIMARY KEY,        -- v7
    owner_id      UUID NOT NULL,
    category      TEXT NOT NULL,           -- resolved category key from CategoryResolver, e.g. 'FOOD','RENT','SALARY','UNCATEGORIZED'
    flow          TEXT NOT NULL,           -- INCOME|EXPENSE (which side the category rolls up)
    period        DATE NOT NULL,           -- month bucket, owner-local (§3)
    -- SIGNED natural-value accumulator (audit 05-CRITICAL, conventions §8). Stores
    -- Σ nat(type, amount), NOT an absolute magnitude — so a reversal's credit (nat<0)
    -- subtracts and post+reverse nets to exactly 0. Read side takes |amount_minor| for
    -- display magnitude. A magnitude-only fold cannot net out a reversal and is forbidden.
    amount_minor  BIGINT NOT NULL,         -- signed Σ nat(type, amount) for the month
    txn_count     INT   NOT NULL,          -- signed: +1 per contributing posting, −1 per reversal posting
    currency      CHAR(3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, category, flow, period)
);
CREATE INDEX acm_owner_period_idx ON analytics_category_month (owner_id, period);

-- Net-worth snapshot series: cumulative asset & liability totals at each month boundary,
-- plus the unrealized-gain term. Assets are carried at COST in the ledger; market value is
-- added exactly once via unrealized_gain_minor (conventions §9). Derived from
-- analytics_account_month running totals + the HoldingRevalued projection; stored so the
-- series query is O(months). period is the owner-local month-end boundary (§3).
CREATE TABLE analytics_networth_month (
    id              UUID PRIMARY KEY,      -- v7
    owner_id        UUID NOT NULL,
    period          DATE NOT NULL,         -- owner-local month-end boundary this row represents
    assets_minor    BIGINT NOT NULL,       -- cumulative asset balance at COST (natural sign) as of period end
    liabilities_minor BIGINT NOT NULL,     -- cumulative liability balance (natural sign, positive = owed)
    unrealized_gain_minor BIGINT NOT NULL DEFAULT 0, -- Σ (mark − cost) as of period end (from HoldingRevalued); §9
    net_worth_minor BIGINT NOT NULL,       -- assets_minor − liabilities_minor + unrealized_gain_minor (§9)
    liquid_minor    BIGINT NOT NULL,       -- subset of assets flagged liquid (see liquidity)
    currency        CHAR(3) NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, period)
);
CREATE INDEX anw_owner_period_idx ON analytics_networth_month (owner_id, period);

-- Liquidity classification of accounts, projected from account metadata. Lets liquidity
-- analysis and runway pick "liquid" balances without re-deriving rules at query time.
-- The seeded Transfers:In-Transit clearing account (system_kind=IN_TRANSIT, an ASSET) is
-- bucketed LIQUID (audit 05-MEDIUM): a two-legged transfer is still liquid money, just
-- relabeled, so liquid balance and runway stay stable across the mid-transfer window.
CREATE TABLE analytics_account_liquidity (
    owner_id     UUID NOT NULL,
    account_id   UUID NOT NULL,
    account_type TEXT NOT NULL,
    bucket       TEXT NOT NULL,            -- LIQUID|NEAR_LIQUID|ILLIQUID|LIABILITY (IN_TRANSIT ASSET → LIQUID)
    currency     CHAR(3) NOT NULL,
    PRIMARY KEY (owner_id, account_id)
);

-- Latest per-account unrealized gain (mark − cost), projected from HoldingRevalued /
-- manual_valuation marks (conventions §9, §12). The net-worth pass sums the current mark
-- for each account to form unrealized_gain_minor. Idempotent last-writer-wins keyed on
-- (owner, account); a mark REPLACES the prior mark for that account (never accumulates),
-- so net worth adds market appreciation exactly once on top of the ledger's cost basis.
CREATE TABLE analytics_holding_valuation (
    owner_id            UUID NOT NULL,
    account_id          UUID NOT NULL,
    unrealized_gain_minor BIGINT NOT NULL, -- mark − cost as of as_of; may be negative (loss)
    as_of               DATE NOT NULL,     -- owner-local effective date of the mark
    mark_event_id       UUID NOT NULL,     -- v7 id of the HoldingRevalued/mark event applied (staleness/replay)
    currency            CHAR(3) NOT NULL,
    PRIMARY KEY (owner_id, account_id)
);
```

**Sign / natural-balance convention.** Postings are signed as in `ledger` (debit +,
credit −). Analytics stores raw signed `net_minor` in `analytics_account_month` and
applies **natural-balance presentation per type** only when composing higher-level
metrics: assets/expenses are debit-normal, liabilities/income/equity credit-normal.
Presentation sign flips live in the projection builder, documented at each metric below,
never in ad-hoc query code.

## Category dimension — shared resolver (conventions §12)

Category is a **shared dimension owned by the `category/` context** (conventions §12), not
an analytics-local, account-name-derived notion. Analytics consumes the
`CategoryResolver` port; it does not define its own category taxonomy.

1. **Source of truth — `CategoryResolver`.** For each income/expense posting the projector
   calls `CategoryResolver.resolve(ownerId, posting)` and stores the returned canonical
   **category key**. The resolver owns the hierarchical, user-editable taxonomy and its
   mapping rules (account path, transaction tag, learned rules). Analytics treats the key
   as opaque and replay-stable.
2. **Grain — one explicit level.** The category key is the **immediate child of
   `Expenses`/`Income`** in the account path — e.g. both `Expenses:Food:Groceries` and
   `Expenses:Food:Restaurants` resolve to `FOOD` (not `GROCERIES`/`RESTAURANTS`). The
   resolver is the single place this grain is defined; analytics never re-derives it from a
   leaf name. (Previously ASSUMPTION-CAT said "the leaf … *is* the category" while giving a
   parent-node example — that contradiction is removed; audit 05-MEDIUM.)
3. Postings that touch **no** income/expense account (pure asset↔asset / asset↔liability
   transfers, including the In-Transit clearing legs) contribute to net worth and liquidity
   but **not** to category rollups.
4. Postings the resolver cannot map roll up under `UNCATEGORIZED`.
5. **Re-homing history.** A `CategoryReassigned` event from the `category/` context re-homes
   historical rollups; the projector moves the affected `(category, flow, period)` signed
   totals from the old key to the new (a signed subtract-then-add, still replay-exact).

`CategoryResolver` results are cached in-context and are replay-stable for a fixed taxonomy
version. Account **metadata** (type, immutability of identity) is still read read-only via
`LedgerApi.account(id)` — never a truth read for a headline number.

## Metric formulas (exact)

Let month buckets be `m`, first-of-month in **owner-local time** (conventions §3, not UTC).
For account `a`, `net(a,m)` = `analytics_account_month.net_minor`. Natural balance operator
`nat(type, x)` = `x` for debit-normal types (ASSET, EXPENSE), `-x` for credit-normal
(LIABILITY, INCOME, EQUITY).

- **Account balance at month end `M`** (cumulative):
  `bal(a, M) = nat(type(a), Σ_{m ≤ M} net(a, m))`. This is **cost basis** for investment
  asset accounts (the ledger holds cost, conventions §9).

- **Net worth at `M`** (conventions §9 canonical formula):
  `assets(M)      = Σ_{a: type=ASSET}     bal(a, M)`   (investments at cost)
  `liabilities(M) = Σ_{a: type=LIABILITY} bal(a, M)`
  `unrealized(M)  = Σ_{a} analytics_holding_valuation.unrealized_gain_minor as of M`
  `netWorth(M)    = assets(M) − liabilities(M) + unrealized(M)`.
  Market appreciation is added **exactly once** via the `unrealized(M)` term (from the
  `HoldingRevalued` / manual-mark projection); the ledger asset accounts carry cost, so
  there is no double count. Postings hit **leaf accounts only** (§9), so per-type sums
  iterate leaves. Stored as one `analytics_networth_month` row per `M`; the series is these
  rows ordered by `period`. (This reconciles 05 with 08-investment: 08 emits
  `HoldingRevalued`; 05 folds it here, and neither re-adds market value on top of cost.)

- **Monthly burn (expense outflow for month `m`):**
  `burn(m) = Σ_{a: type=EXPENSE} nat(EXPENSE, net(a, m))`
  i.e. net expense-account debits in the owner-local month `m`. Equivalent to
  `Σ analytics_category_month.amount_minor WHERE flow='EXPENSE' AND period=m` — the identity
  holds **only because both sides are now signed** (audit 05-CRITICAL): a reversal credit
  subtracts on both the account path and the category path, so the two agree.
  Note `burn(m)` **may be negative** in a month dominated by corrections/reversals booked at
  correction-time (see below); the runway pipeline guards against that rather than surfacing
  a negative spend as truth.

- **Income for month `m`:**
  `income(m) = Σ_{a: type=INCOME} nat(INCOME, net(a, m))` (credits to income accounts).

- **Income vs expense (month `m`):** the pair `(income(m), burn(m))` and
  `netFlow(m) = income(m) − burn(m)` (savings if > 0, dissaving if < 0).

- **Average monthly burn** over a trailing window of up to `W` **complete** owner-local
  months ending at the last complete month before `asOf` (default `W = 3`, tunable —
  ASSUMPTION-RUNWAY). Let `A` = the count of window months **with actual expense activity**
  (a non-zero `analytics_account_month` expense row, i.e. months the user was keeping books):
  `avgBurn(asOf, W) = ( Σ_{i=1..W} burn(m_i) ) / A`, HALF_EVEN rounding to paise applied
  once at the division boundary (conventions §2). Dividing by **`A`, not the fixed `W`**,
  avoids diluting the average with empty pre-history/sabbatical months and overstating runway
  (audit 05-HIGH). The current partial month is excluded so a mid-month query is not
  understated.
  - **Insufficient-data guard.** If `A = 0`, `avgBurn` is undefined (no activity). The
    implementation MAY instead require a **full** window (`A = W`) before reporting runway,
    exposing `monthsCounted = A` so the caller sees the basis.
  - **Negative-burn guard (audit 05-HIGH, conventions §10).** Reversals/corrections are
    booked at **correction-time** (§10): a reversal of an old month's expense lands as a
    credit in the current month, which can drive that month's `burn` — and hence `avgBurn` —
    **negative**. A negative or zero `avgBurn` is treated as **insufficient data**, never as
    a negative denominator: runway is reported as `∞`/`n/a` (see below), never a negative
    number of months.

- **Liquid balance at `asOf`:** `liquid(asOf) = Σ_{a: bucket=LIQUID} bal(a, lastMonthEnd)`.
  **"Liquid" (ASSUMPTION-LIQUID)** = asset accounts in bucket `LIQUID`: cash, savings,
  checking, wallet, liquid-fund/sweep, **and the `Transfers:In-Transit` clearing account**
  (`system_kind=IN_TRANSIT`; audit 05-MEDIUM). Because a two-legged transfer parks money in
  In-Transit between legs, bucketing it `LIQUID` keeps `liquid(asOf)` and runway **stable
  across the mid-transfer window** — the money is still liquid, just relabeled. `NEAR_LIQUID`
  (e.g. short FDs) is excluded from runway by default but reported separately; `ILLIQUID`
  (property, locked instruments) never counts. Bucket assignment lives in
  `analytics_account_liquidity` and is a tunable classification.

- **Cash runway (months):**
  `runway(asOf) = liquid(asOf) / avgBurn(asOf, W)`.
  - `avgBurn = 0` (no burn) → runway `∞` (unbounded).
  - **`avgBurn < 0` or `A = 0`** (net corrections / no activity in the window) → runway
    `n/a`, **never a negative number of months** (audit 05-HIGH); the response flags
    `insufficientData`. Otherwise result is a decimal number of months (e.g. `8.4`), computed
    from minor-unit integers with HALF_EVEN at the final division. The numerator/denominator
    and `monthsCounted` are returned so the UI can show the assumption.

- **Category aggregation (month `m`):** `analytics_category_month` rows for `period=m`,
  ranked by `|amount_minor|` desc (the accumulator is signed; display uses its magnitude);
  `flow` splits income vs expense categories.

- **Spending trend (category `c`):** the series `|amount_minor|` over consecutive `period`s
  for `(c, flow='EXPENSE')`; deltas / moving average computed at the read API, not stored.

- **Liquidity analysis (`asOf`):** balances grouped by `bucket`
  (`LIQUID`, `NEAR_LIQUID`, `ILLIQUID`, `LIABILITY`) at `lastMonthEnd`, plus the
  liquid-to-net-worth ratio and emergency-fund coverage `liquid(asOf) / avgBurn(asOf, W)`
  (= runway, subject to the same guards, surfaced in the liquidity view too).
  - **Liquid-to-net-worth ratio guard (audit 05-MEDIUM).** `liquid(asOf) / netWorth(asOf)`
    is reported **only when `netWorth(asOf) > 0`**. When `netWorth(asOf) ≤ 0` (net-debtor, or
    exactly zero → division by zero) the ratio is `n/a`; the raw `liquid` figure is still
    returned so the view is informative for a net-debtor.

- **Financial-Year rollups (conventions §3).** Every month-grained metric above also rolls
  up to the India FY period (**Apr 1 – Mar 31**, owner-local), a first-class period type
  alongside month/week. FY figures are the sum of the constituent owner-local months:
  `burnFY(fy) = Σ_{m ∈ fy} burn(m)`, `incomeFY`, `netFlowFY`, and category-per-FY likewise;
  net worth for an FY is the value at the FY-end boundary (Mar 31 owner-local). Exposed via a
  `period=FY` selector on the series/aggregation endpoints (needed by tax/80C/capital-gains).

## Projections & how they are built from the outbox

A single `EventConsumer` (`AnalyticsProjector`, conventions §6 / platform `EventConsumer`
interface) owns all analytics tables. It is registered with the `OutboxDispatcher`,
idempotent via `processed_event (consumer_name='analytics', event_id)`, and processes
events **in outbox `id` (v7 time) order**.

**`handle(TransactionPosted evt)`** — within its own DB transaction:
1. Dedup check (`processed_event`) — redelivery is a no-op (conventions §6).
2. Derive `period = first-of-month of evt.timestamp **in the owner's IANA timezone**
   (conventions §3, owner-local — NOT `date_trunc` in UTC). `evt.timestamp` is effective
   business time.
3. For each posting: resolve `account_type` and `liquidity bucket` from cached `LedgerApi`
   account metadata, and (for income/expense) `category` from `CategoryResolver`
   (conventions §12); **upsert** `analytics_account_month`
   (`net_minor += amount_minor`, `posting_count += 1`) keyed on
   `(owner_id, account_id, period)`.
4. For each income/expense posting: **upsert** `analytics_category_month` with the **signed
   natural value** — `amount_minor += nat(type, amount)`, `txn_count += sign(nat(type,
   amount))` (i.e. `+1` for a normal posting, `−1` for a reversal credit) — on
   `(owner, category, flow, period)`. This is the audit 05-CRITICAL fix: the fold is signed,
   not magnitude, so post-then-reverse nets to exactly 0 and cannot double-count (conventions
   §8, §10). Read side takes `|amount_minor|` for display.
5. Set the net-worth staleness watermark: `networth_dirty_from = LEAST(networth_dirty_from,
   period)` on `analytics_projection_offset` (recompute lazily — see below) since a
   cumulative total shifted from `period` onward.
6. Ensure an `analytics_account_liquidity` row exists for each touched account (the
   In-Transit system account is seeded `LIQUID`).
7. Write `processed_event` and advance `analytics_projection_offset` in the **same** tx.

**`handle(TransactionReversed evt)`** — a reversal is a normal balanced transaction with
negated postings already emitted by `ledger` as its **own** `TransactionPosted`, whose
business `timestamp` is the **correction time (now)**, not the original's (conventions §10,
02-ledger reversal). So the negation lands in the correction month's bucket, not the
original's — a deliberate correction-time convention, and the reason a correction month's
`burn`/`avgBurn` can go negative and is guarded (see runway). The projector treats
`TransactionReversed` as a **marker only** (idempotency/audit bookkeeping); the arithmetic
arrives through the reversal transaction's own `TransactionPosted`. Every fold is a signed
`+= nat(type, amount)`, which guarantees order-independent, replay-identical, post+reverse=0
sums.

**`handle(HoldingRevalued evt)`** — `{accountId, unrealizedGainMinor, asOf, currency}` from
08-investment (and the same shape from a `manual_valuation` mark, conventions §9/§12).
**Last-writer-wins upsert** of `analytics_holding_valuation` on `(owner, account)`: the mark
**replaces** the prior mark for that account (never accumulates), so market appreciation is
counted exactly once on top of the ledger's cost basis — no double count (audit 05-HIGH).
Then set `networth_dirty_from = LEAST(networth_dirty_from, first-of-month(asOf))` and write
`processed_event` in the same tx. Replay-exact because it is idempotent per account and the
net-worth pass reads only the latest mark.

**Net-worth series recompute (steps 5 / HoldingRevalued).** `analytics_networth_month` is a
running total, so a late-arriving posting or mark for an old month shifts every later row.
Rather than rewrite on every event, the projector recomputes in a **debounced pass**
(triggered at end of a dispatch batch, or on read when stale). **Staleness is explicit and
testable** (audit 05-MEDIUM): the series is stale iff `analytics_projection_offset
.networth_dirty_from IS NOT NULL`; the pass recomputes rows with `period >=
networth_dirty_from`, walking `analytics_account_month` ordered by `period` while carrying
cumulative asset (cost) / liability / liquid running totals, adding the current
`analytics_holding_valuation` unrealized-gain sum for the period to form
`net_worth_minor = assets − liabilities + unrealized`, upserting one row per period, and
finally resetting `networth_dirty_from = NULL`. The pass is a pure function of
`analytics_account_month` + liquidity buckets + `analytics_holding_valuation`, so it is
deterministic and idempotent.

### Rebuild / replay procedure (tested property, conventions §8, §10)

```
1. BEGIN maintenance
2. TRUNCATE analytics_account_month, analytics_category_month,
           analytics_networth_month, analytics_account_liquidity,
           analytics_holding_valuation;
   DELETE FROM processed_event WHERE consumer_name = 'analytics';
   DELETE FROM analytics_projection_offset;   -- reset fence + networth_dirty_from to genesis
3. Re-drive AnalyticsProjector over ALL outbox rows in ascending id (v7) order,
   status in (SENT, PENDING), from genesis — TransactionPosted, TransactionReversed,
   and HoldingRevalued.  (A dedicated replay driver reads the outbox directly; it does
   NOT wait for live dispatch.)
4. Run the net-worth recompute pass.
5. COMMIT
```

Because every fold is **signed-additive** and keyed on immutable `(account, period)` /
`(category, flow, period)` tuples, holding marks are idempotent last-writer-wins per
`(owner, account)`, and the net-worth pass is a pure function of those tables, the result is
**independent of event ordering within a month and of redelivery**, and a post-then-reverse
pair nets to exactly zero on both the account and category folds — replaying from genesis
yields byte-identical projection state. This equality is asserted by a property test
(below).

## Core read API (`AnalyticsApi`)

Package-private impl, public interface only (conventions §11). All methods are
owner-scoped via `OwnerContext`, read-only, and serve from projection tables (never from
`ledger` truth for the returned numbers).

- **`netWorth(asOf)`** → assets (at cost), liabilities, unrealizedGain, netWorth (= assets −
  liabilities + unrealizedGain, §9), liquid at nearest `analytics_networth_month ≤ asOf`
  (Money each). O(1). Recompute first if `networth_dirty_from` is set.
- **`netWorthSeries(from, to)`** → ordered monthly `NetWorthPoint` list. O(months).
- **`monthlyBurn(month)`** / **`burnSeries(from, to)`** → expense outflow per period.
- **`incomeVsExpense(from, to)`** → per-period `(income, expense, netFlow)` series.
- **`cashRunway(asOf, window=W)`** → `{ liquidMinor, avgMonthlyBurnMinor, runwayMonths,
  window, monthsCounted, insufficientData }` — returns the inputs alongside the result so the
  assumption is transparent. `runwayMonths` is `∞` when burn is zero and **`null` (with
  `insufficientData=true`) when `avgBurn ≤ 0` or `monthsCounted = 0`** — never negative
  (audit 05-HIGH).
- **`categoryBreakdown(month, flow)`** → ranked `CategorySlice` list (category,
  amountMinor = `|signed accumulator|`, txnCount, share%).
- **`categoryTrend(category, from, to)`** → monthly series + moving average.
- **`liquidity(asOf)`** → balances by bucket + emergency-fund coverage, plus the
  liquid-to-net-worth ratio which is **`null`/"n/a" when `netWorth(asOf) ≤ 0`** (audit
  05-MEDIUM); the raw liquid figure is always returned.
- **All month-grained methods accept `period=MONTH|WEEK|FY`** (default `MONTH`) so callers
  can request owner-local FY rollups (conventions §3).
- **`rebuild()`** → maintenance op (admin only) running the replay procedure above;
  returns rows folded + duration. Not user-facing.

Cross-context callers use `AnalyticsApi` only, never analytics tables directly
(conventions §11).

## Events consumed

From the `ledger` outbox (conventions §6; payloads are versioned, unknown fields
tolerated):

- **`TransactionPosted`** — `{txnId, timestamp, postings:[{accountId, amountMinor,
  currency}], description, source}`. The workhorse: drives all rollups. (Reversal
  transactions arrive here too, as their own posted transaction with negated postings.)
- **`TransactionReversed`** — `{txnId, reversesTxnId, timestamp}`. `timestamp` is the
  correction time (conventions §10). Consumed as an idempotency/audit marker only; arithmetic
  comes via the reversal's `TransactionPosted`.
- **`HoldingRevalued`** — `{accountId, unrealizedGainMinor, asOf, currency}` from
  08-investment (and the same shape from a `manual_valuation` mark, §9/§12). Folded into
  `analytics_holding_valuation` (last-writer-wins per account) to supply net worth's
  unrealized-gain term. Consuming this is what lets 05 count market value **exactly once** on
  top of the ledger's cost basis (audit 05-HIGH; reconciled with 08).
- **`CategoryReassigned`** — from the `category/` context (§12); re-homes historical
  `(category, flow, period)` signed totals from the old key to the new.

Analytics **produces no events** — it is a terminal read-side consumer.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/analytics/net-worth?asOf=` | net worth at a point in time |
| GET | `/api/v1/analytics/net-worth/series?from=&to=` | net-worth monthly series |
| GET | `/api/v1/analytics/burn?from=&to=` | monthly burn series |
| GET | `/api/v1/analytics/income-vs-expense?from=&to=` | income vs expense per month |
| GET | `/api/v1/analytics/runway?asOf=&window=` | cash runway (+ inputs) |
| GET | `/api/v1/analytics/categories?month=&flow=` | category aggregation for a month |
| GET | `/api/v1/analytics/categories/{category}/trend?from=&to=` | spending trend for a category |
| GET | `/api/v1/analytics/liquidity?asOf=` | liquidity buckets + ratios |
| POST | `/api/v1/analytics/rebuild` | admin: replay projections from genesis |

Series/aggregation endpoints accept an optional `&period=MONTH|WEEK|FY` selector (default
`MONTH`) to request owner-local FY (or week) rollups (conventions §3).

All GETs are read-only (no `Idempotency-Key`). Money as `{amountMinor, currency}`; timestamps
serialized ISO-8601 UTC, but **all civil-period bucketing is owner-local** (§3); cursor
pagination where lists are unbounded; errors as `problem+json` with context code enum prefix
`ANALYTICS_` (e.g. `ANALYTICS_RANGE_INVALID` on `from > to`, 422). Missing projection data
returns an empty series, not an error (eventual consistency).

## Testing

- **Replay determinism (key property, conventions §8/§10):** post a randomized set of
  transactions + reversals; snapshot all projection tables; run `rebuild()`; assert
  byte-identical state. Also assert order-independence: shuffle intra-month event delivery
  → same result.
- **Consumer idempotency:** redelivering the same `TransactionPosted` (same `event_id`) is
  a no-op — rollups unchanged (conventions §6/§10).
- **No-truth-read invariant:** static/architecture test that analytics production code
  never writes `ledger` tables and reads `LedgerApi` only for account **metadata**, never
  for balances/headline numbers (enforces conventions §8).
- **Formula correctness:** for a fixed fixture, assert net worth, burn, income vs expense,
  category totals, liquidity, and runway match hand-computed expected values (INR paise),
  including HALF_EVEN rounding at the runway division boundary.
- **Reversal accounting — signed folds net to zero (conventions §8/§14, audit 05-CRITICAL):**
  post then reverse → **both** `analytics_account_month.net_minor` **and**
  `analytics_category_month.amount_minor` (and `txn_count`) for the affected category net
  back to the pre-transaction figures; `categoryBreakdown` and account-path `monthlyBurn`
  agree; no double-counting. This is the mandatory post-then-reverse-category-nets-to-zero
  test the previous suite omitted.
- **Correction-time reversal + negative-burn guard (audit 05-HIGH):** book an old-month
  expense, reverse it in a later month; assert the reversal lands in the correction month's
  bucket (§10), that a correction-dominated month can make `burn`/`avgBurn` ≤ 0, and that
  `cashRunway` then returns `runwayMonths = null` with `insufficientData=true` — **never a
  negative runway**.
- **avgBurn divides by active months (audit 05-HIGH):** window with empty pre-history /
  sabbatical months → `avgBurn` divides by `monthsCounted` (months with activity), not fixed
  `W`; assert runway is not overstated and `monthsCounted` is surfaced.
- **Net worth = cost + unrealized, counted once (audit 05-HIGH, §9):** BUY at cost then
  `HoldingRevalued` to a higher mark → `netWorth` = assets(cost) − liabilities + unrealized
  gain, with the appreciation added **exactly once** (no double count); a second mark
  replaces the first (last-writer-wins), not accumulates. Agreement test reconciles with
  08-investment.
- **Liquidity guards:** liquid-to-net-worth ratio is `n/a` when `netWorth ≤ 0` (incl. exactly
  0 → no divide-by-zero); a mid-flight two-legged transfer through the `IN_TRANSIT` (LIQUID)
  account leaves `liquid`/runway unchanged across the in-flight window (audit 05-MEDIUM).
- **Staleness marker (audit 05-MEDIUM):** a backdated posting sets
  `networth_dirty_from`; a read while dirty recomputes and clears it; `netWorthSeries`
  reflects the shifted cumulative totals (not the stale pre-backdate series).
- **Owner-local + FY bucketing (§3):** a post-23:30 IST transaction buckets into its IST
  day/month; FY (`period=FY`) rollups sum the correct owner-local Apr–Mar months.
- **Category grain (audit 05-MEDIUM):** `Expenses:Food:Groceries` and
  `Expenses:Food:Restaurants` both resolve (via `CategoryResolver`) to the single `FOOD` key.
- **Cumulative correctness:** cross-check `netWorth(asOf)` from projections against
  `LedgerApi.netPosition(asOf)` + `InvestmentApi` unrealized gain for random dates — the
  projection must agree with truth (agreement test, not a runtime dependency).
- **Persistence:** integration tests on real Postgres (Testcontainers) for upsert
  concurrency and offset advancement in-tx with `processed_event`.
- **API contract:** per endpoint happy + validation (`from > to` → `ANALYTICS_RANGE_INVALID`,
  422) + empty-projection (empty series, 200).
- **Eventual consistency:** a just-posted transaction not yet dispatched is absent from
  analytics, then appears after dispatch — asserted, not treated as a bug.

## Audit fixes applied

**Amended 2026-07-23.** Maps each audit finding (`audit/05-analytics.md`) to the change.

| Audit finding | Severity | Fix |
|---|---|---|
| Category fold stored absolute magnitude → reversal double-counts | CRITICAL | `analytics_category_month.amount_minor` is now a **signed** `Σ nat(type, amount)` accumulator (`txn_count` signed too); step 4 folds signed; display takes magnitude. Post-then-reverse-category-nets-to-zero is now a mandatory test (conventions §8/§14). |
| Correction-time reversal in wrong month drives burn / runway negative | HIGH | Documented reversals as correction-time (§10); `burn` may be negative; `avgBurn ≤ 0` treated as insufficient data; runway returns `null`/`n/a`, never negative. Test added. |
| avgBurn divides by fixed `W` incl. empty months → runway overstated | HIGH | `avgBurn` divides by `monthsCounted` = window months **with activity** (or requires a full window); `monthsCounted` surfaced. Test added. |
| Net worth at cost / 08 double-count contradiction | HIGH | Adopted conventions §9 canonical formula (assets at cost − liabilities + Σ unrealizedGain). Added `analytics_holding_valuation` + a `HoldingRevalued` consumer (last-writer-wins, counted once). Reconciled wording with 08-investment. |
| Liquid/net-worth ratio unguarded when net worth ≤ 0 | MEDIUM | Ratio returns `n/a` when `netWorth ≤ 0` (incl. zero); raw liquid still returned. |
| `Transfers:In-Transit` liquidity bucket undefined | MEDIUM | `IN_TRANSIT` system ASSET is bucketed `LIQUID` so mid-transfer runway is stable. |
| "Mark dirty / recompute if stale" unimplementable — no marker | MEDIUM | Added `analytics_projection_offset.networth_dirty_from DATE`; recompute pass reads/clears it; staleness is now testable. |
| Category grain leaf-vs-parent contradiction | MEDIUM | Category comes from the shared `CategoryResolver` (§12); grain fixed to the immediate child of `Expenses`/`Income` (`Expenses:Food:Groceries` → `FOOD`); example corrected. |
| (Cross-cutting) UTC period bucketing | — | All day/week/month/FY bucketing is owner-local (§3); FY rollups exposed via `period=FY`. |
