# 05 — Analytics Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 3**. The
read side. Analytics owns **no truth** — it builds rebuildable read-model **projections**
from the ledger outbox and answers "where does money go, am I building wealth, how long
does my cash last" questions. INR only.

## Responsibilities

- Consume `TransactionPosted` / `TransactionReversed` from the outbox and maintain
  **read-model projections** (monthly rollups per account and per category, net-worth
  series, liquidity buckets).
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
    owner_id        UUID PRIMARY KEY,
    last_event_id   UUID,                 -- v7 id of last applied outbox row (time order)
    last_applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Monthly rollup of net signed movement per account. The atomic fact table; every
-- higher-level metric is derived from this + account metadata. period = first day of
-- the calendar month (UTC), e.g. 2026-07-01.
CREATE TABLE analytics_account_month (
    id            UUID PRIMARY KEY,        -- v7
    owner_id      UUID NOT NULL,
    account_id    UUID NOT NULL,
    account_type  TEXT NOT NULL,           -- ASSET|LIABILITY|EQUITY|INCOME|EXPENSE (denormalized)
    period        DATE NOT NULL,           -- month bucket, first-of-month UTC
    net_minor     BIGINT NOT NULL,         -- signed sum of postings on this account in the month
    posting_count INT   NOT NULL,          -- for reversibility / debug
    currency      CHAR(3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, account_id, period)
);
CREATE INDEX aam_owner_period_idx ON analytics_account_month (owner_id, period);
CREATE INDEX aam_owner_type_period_idx ON analytics_account_month (owner_id, account_type, period);

-- Monthly rollup per spending/earning category. Category is a DIMENSION derived at
-- projection time (see "Category dimension" assumption). One row per (category, period).
CREATE TABLE analytics_category_month (
    id            UUID PRIMARY KEY,        -- v7
    owner_id      UUID NOT NULL,
    category      TEXT NOT NULL,           -- resolved category key, e.g. 'FOOD','RENT','SALARY','UNCATEGORIZED'
    flow          TEXT NOT NULL,           -- INCOME|EXPENSE (which side the category rolls up)
    period        DATE NOT NULL,           -- month bucket
    amount_minor  BIGINT NOT NULL,         -- positive magnitude of flow in the month
    txn_count     INT   NOT NULL,
    currency      CHAR(3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, category, flow, period)
);
CREATE INDEX acm_owner_period_idx ON analytics_category_month (owner_id, period);

-- Net-worth snapshot series: cumulative asset & liability totals at each month boundary.
-- Derived from analytics_account_month running totals; stored so the series query is O(months).
CREATE TABLE analytics_networth_month (
    id              UUID PRIMARY KEY,      -- v7
    owner_id        UUID NOT NULL,
    period          DATE NOT NULL,         -- month-end boundary this row represents
    assets_minor    BIGINT NOT NULL,       -- cumulative asset balance (natural sign) as of period end
    liabilities_minor BIGINT NOT NULL,     -- cumulative liability balance (natural sign, positive = owed)
    net_worth_minor BIGINT NOT NULL,       -- assets_minor - liabilities_minor
    liquid_minor    BIGINT NOT NULL,       -- subset of assets flagged liquid (see liquidity)
    currency        CHAR(3) NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, period)
);
CREATE INDEX anw_owner_period_idx ON analytics_networth_month (owner_id, period);

-- Liquidity classification of accounts, projected from account metadata. Lets liquidity
-- analysis and runway pick "liquid" balances without re-deriving rules at query time.
CREATE TABLE analytics_account_liquidity (
    owner_id     UUID NOT NULL,
    account_id   UUID NOT NULL,
    account_type TEXT NOT NULL,
    bucket       TEXT NOT NULL,            -- LIQUID|NEAR_LIQUID|ILLIQUID|LIABILITY
    currency     CHAR(3) NOT NULL,
    PRIMARY KEY (owner_id, account_id)
);
```

**Sign / natural-balance convention.** Postings are signed as in `ledger` (debit +,
credit −). Analytics stores raw signed `net_minor` in `analytics_account_month` and
applies **natural-balance presentation per type** only when composing higher-level
metrics: assets/expenses are debit-normal, liabilities/income/equity credit-normal.
Presentation sign flips live in the projection builder, documented at each metric below,
never in ad-hoc query code.

## Category dimension — assumption (marked)

Category aggregation needs a dimension the ledger event does not carry directly. **Design
assumption (ASSUMPTION-CAT):**

1. **Primary source — account mapping.** Category is derived from the `EXPENSE` /
   `INCOME` account touched by the transaction. The leaf account name/path *is* the
   category (e.g. `Expenses:Food:Groceries` → `FOOD`). This is intrinsic to double-entry:
   every expense debits an expense account, every income credits an income account.
2. **Optional override — transaction tag.** If a future `category` tag rides on the
   transaction (via `description`/`reference` metadata or an added event field), it
   overrides the account-derived category. **Phase 3 does not require the tag**; the
   builder reads it only if present (consumers tolerate unknown fields, conventions §6).
3. Postings that touch **no** income/expense account (pure asset↔asset / asset↔liability
   transfers) contribute to net worth and liquidity but **not** to category rollups.
4. Unmappable income/expense postings roll up under `UNCATEGORIZED`.

The account→category map is resolved via `LedgerApi.account(id)` metadata (type + name
path), cached in-context. This is read-only metadata use, not a truth read for a headline
number, and is itself replay-stable because account identity is immutable.

## Metric formulas (exact)

Let month buckets be `m`, first-of-month UTC. For account `a`, `net(a,m)` =
`analytics_account_month.net_minor`. Natural balance operator `nat(type, x)` = `x` for
debit-normal types (ASSET, EXPENSE), `-x` for credit-normal (LIABILITY, INCOME, EQUITY).

- **Account balance at month end `M`** (cumulative):
  `bal(a, M) = nat(type(a), Σ_{m ≤ M} net(a, m))`.

- **Net worth at `M`:**
  `assets(M)      = Σ_{a: type=ASSET}     bal(a, M)`
  `liabilities(M) = Σ_{a: type=LIABILITY} bal(a, M)`
  `netWorth(M)    = assets(M) − liabilities(M)`.
  Stored as one `analytics_networth_month` row per `M`; the series is these rows ordered
  by `period`.

- **Monthly burn (expense outflow for month `m`):**
  `burn(m) = Σ_{a: type=EXPENSE} nat(EXPENSE, net(a, m))`
  i.e. total expense-account debits in the calendar month `m`. Equivalent to
  `Σ analytics_category_month.amount_minor WHERE flow='EXPENSE' AND period=m`.

- **Income for month `m`:**
  `income(m) = Σ_{a: type=INCOME} nat(INCOME, net(a, m))` (credits to income accounts).

- **Income vs expense (month `m`):** the pair `(income(m), burn(m))` and
  `netFlow(m) = income(m) − burn(m)` (savings if > 0, dissaving if < 0).

- **Average monthly burn** over a trailing window of `W` **complete** calendar months
  ending at the last complete month before `asOf` (default `W = 3`, tunable —
  ASSUMPTION-RUNWAY):
  `avgBurn(asOf, W) = ( Σ_{i=1..W} burn(m_i) ) / W`, HALF_EVEN rounding to paise applied
  once at the division boundary (conventions §2). The current partial month is excluded so
  a mid-month query is not understated.

- **Liquid balance at `asOf`:** `liquid(asOf) = Σ_{a: bucket=LIQUID} bal(a, lastMonthEnd)`.
  **"Liquid" (ASSUMPTION-LIQUID)** = asset accounts in bucket `LIQUID`: cash, savings,
  checking, wallet, liquid-fund/sweep. `NEAR_LIQUID` (e.g. short FDs) is excluded from
  runway by default but reported separately; `ILLIQUID` (property, locked instruments)
  never counts. Bucket assignment lives in `analytics_account_liquidity` and is a tunable
  classification.

- **Cash runway (months):**
  `runway(asOf) = liquid(asOf) / avgBurn(asOf, W)`.
  If `avgBurn = 0` → runway is reported as `∞` (unbounded, no burn). Result is a decimal
  number of months (e.g. `8.4`), computed from minor-unit integers with HALF_EVEN at the
  final division; the numerator/denominator are also returned so the UI can show the
  assumption.

- **Category aggregation (month `m`):** `analytics_category_month` rows for `period=m`,
  ranked by `amount_minor` desc; `flow` splits income vs expense categories.

- **Spending trend (category `c`):** the series `amount_minor` over consecutive `period`s
  for `(c, flow='EXPENSE')`; deltas / moving average computed at the read API, not stored.

- **Liquidity analysis (`asOf`):** balances grouped by `bucket`
  (`LIQUID`, `NEAR_LIQUID`, `ILLIQUID`, `LIABILITY`) at `lastMonthEnd`, plus liquid-to-net-worth
  ratio `liquid(asOf) / netWorth(asOf)` and emergency-fund coverage
  `liquid(asOf) / avgBurn(asOf, W)` (= runway, surfaced in the liquidity view too).

## Projections & how they are built from the outbox

A single `EventConsumer` (`AnalyticsProjector`, conventions §6 / platform `EventConsumer`
interface) owns all analytics tables. It is registered with the `OutboxDispatcher`,
idempotent via `processed_event (consumer_name='analytics', event_id)`, and processes
events **in outbox `id` (v7 time) order**.

**`handle(TransactionPosted evt)`** — within its own DB transaction:
1. Dedup check (`processed_event`) — redelivery is a no-op (conventions §6).
2. Derive `period = date_trunc('month', evt.timestamp)` (business time, conventions §3).
3. For each posting: resolve `account_type` and `category`/`liquidity bucket` from cached
   `LedgerApi` account metadata; **upsert** `analytics_account_month`
   (`net_minor += amount_minor`, `posting_count += 1`) keyed on
   `(owner_id, account_id, period)`.
4. For each income/expense posting: **upsert** `analytics_category_month`
   (`amount_minor += |nat(type, amount)|`, `txn_count += 1`) on `(owner, category, flow, period)`.
5. Mark `analytics_networth_month` rows from `period` onward **dirty** (recompute lazily —
   see below) since a cumulative total changed.
6. Ensure an `analytics_account_liquidity` row exists for each touched account.
7. Write `processed_event` and advance `analytics_projection_offset` in the **same** tx.

**`handle(TransactionReversed evt)`** — a reversal is a normal balanced transaction with
negated postings already emitted by `ledger` as its **own** `TransactionPosted` (see
02-ledger reversal). To keep replay deterministic and avoid double-counting, the projector
treats `TransactionReversed` as a **marker only** (idempotency bookkeeping); the negation
arithmetic arrives through the reversal transaction's own `TransactionPosted`. This makes
folding **purely additive** — every fold is a `+= amount_minor` — which is what guarantees
order-independent, replay-identical sums.

**Net-worth series recompute (step 5).** `analytics_networth_month` is a running total, so
a late-arriving posting for an old month shifts every later row. Rather than rewrite on
every event, the projector recomputes affected rows in a **debounced pass** (triggered at
end of a dispatch batch, or on read if stale): for the owner, walk
`analytics_account_month` ordered by `period`, carry cumulative asset/liability/liquid
running totals, and upsert one `analytics_networth_month` row per period. This pass is a
pure function of `analytics_account_month` + liquidity buckets, so it is itself
deterministic and idempotent.

### Rebuild / replay procedure (tested property, conventions §8, §10)

```
1. BEGIN maintenance
2. TRUNCATE analytics_account_month, analytics_category_month,
           analytics_networth_month, analytics_account_liquidity;
   DELETE FROM processed_event WHERE consumer_name = 'analytics';
   DELETE FROM analytics_projection_offset;   -- reset fence to genesis
3. Re-drive AnalyticsProjector over ALL outbox rows in ascending id (v7) order,
   status in (SENT, PENDING), from genesis.  (A dedicated replay driver reads the
   outbox directly; it does NOT wait for live dispatch.)
4. Run the net-worth recompute pass.
5. COMMIT
```

Because every fold is additive and keyed on immutable `(account, period)` / `(category,
flow, period)` tuples, and the net-worth pass is a pure function of those tables, the
result is **independent of event ordering within a month and of redelivery** — replaying
from genesis yields byte-identical projection state. This equality is asserted by a
property test (below).

## Core read API (`AnalyticsApi`)

Package-private impl, public interface only (conventions §11). All methods are
owner-scoped via `OwnerContext`, read-only, and serve from projection tables (never from
`ledger` truth for the returned numbers).

- **`netWorth(asOf)`** → assets, liabilities, netWorth, liquid at nearest
  `analytics_networth_month ≤ asOf` (Money each). O(1).
- **`netWorthSeries(from, to)`** → ordered monthly `NetWorthPoint` list. O(months).
- **`monthlyBurn(month)`** / **`burnSeries(from, to)`** → expense outflow per month.
- **`incomeVsExpense(from, to)`** → per-month `(income, expense, netFlow)` series.
- **`cashRunway(asOf, window=W)`** → `{ liquidMinor, avgMonthlyBurnMinor, runwayMonths,
  window }` — returns the inputs alongside the result so the assumption is transparent.
  `runwayMonths` null/`∞` when burn is zero.
- **`categoryBreakdown(month, flow)`** → ranked `CategorySlice` list (category,
  amountMinor, txnCount, share%).
- **`categoryTrend(category, from, to)`** → monthly series + moving average.
- **`liquidity(asOf)`** → balances by bucket + liquid/net-worth ratio + emergency-fund
  coverage.
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
- **`TransactionReversed`** — `{txnId, reversesTxnId, timestamp}`. Consumed as an
  idempotency/audit marker only; arithmetic comes via the reversal's `TransactionPosted`.

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

All GETs are read-only (no `Idempotency-Key`). Money as `{amountMinor, currency}`; time
ISO-8601 UTC; cursor pagination where lists are unbounded; errors as `problem+json` with
context code enum prefix `ANALYTICS_` (e.g. `ANALYTICS_RANGE_INVALID` on `from > to`,
422). Missing projection data returns an empty series, not an error (eventual consistency).

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
- **Reversal accounting:** post then reverse → burn / net worth for the period net back to
  the pre-transaction figures; no double-counting.
- **Cumulative correctness:** cross-check `netWorth(asOf)` from projections against
  `LedgerApi.netPosition(asOf)` for random dates — the projection must agree with truth
  (agreement test, not a runtime dependency).
- **Persistence:** integration tests on real Postgres (Testcontainers) for upsert
  concurrency and offset advancement in-tx with `processed_event`.
- **API contract:** per endpoint happy + validation (`from > to` → `ANALYTICS_RANGE_INVALID`,
  422) + empty-projection (empty series, 200).
- **Eventual consistency:** a just-posted transaction not yet dispatched is absent from
  analytics, then appears after dispatch — asserted, not treated as a bug.
