# 08 — Investment Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 5**.

The portfolio brain. Tracks *what securities are held*, *at what cost*, and *what they are
worth* — the security-level detail the ledger deliberately does not carry. Every cash
movement here is mirrored as a double-entry transaction in `ledger` via `LedgerApi`; the
ledger remains the money truth, Investment is the instrument truth.

## Responsibilities

- Instrument/security master (symbol, kind, currency).
- Holdings + position tracking per instrument.
- `investment_transaction` log (BUY/SELL/DIVIDEND/SIP/BONUS/SPLIT) with quantities as
  scaled integers — the security-level detail behind each ledger cash movement.
- **Tax lots**: lot-based cost basis; FIFO/LIFO/specific-lot selection on SELL; realized
  gain computed per consumed lot.
- Cost basis (lot-based truth; average exposed as a derived view).
- Unrealized gain vs a mark price (`price_quote`, manual entry in Phase 5).
- XIRR and realized/unrealized gain — **derived** calcs, cached, never stored truth.
- Corporate actions (split, bonus, dividend) as transactions that adjust lots/quantities.
- Asset allocation analysis + rebalancing suggestions (delta to target).
- Emits `InvestmentTransactionRecorded`, `HoldingRevalued`, `RealizedGainRecorded` to the
  outbox; consumes `PriceQuoteRecorded` internally.

**Scope guards (Phase 5):** INR only (the `currency` column exists everywhere per
convention so multi-currency is additive). **External price feeds are DEFERRED** — prices
are entered manually into `price_quote`; a market-data adapter is a later phase and slots
in behind the same table with zero schema change.

## Relationship to the ledger

Investment never holds cash truth. Each investment transaction that moves cash **also**
posts a balanced double-entry `ledger_transaction` via `LedgerApi.postTransaction(...)`,
in the same application flow, sharing the `Idempotency-Key`:

| Investment event | Ledger postings (balanced, sum-zero) |
|---|---|
| BUY (qty × price + fees) | debit **investment asset account** (the holding), credit **cash/bank account** |
| SELL (proceeds − fees) | debit **cash/bank account**, credit **investment asset account** (at cost); realized gain/loss to an **income/expense** account |
| DIVIDEND (cash) | debit **cash/bank account**, credit **dividend income account** |
| SIP instalment | same as BUY (a SIP instalment *is* a BUY tagged with a `sip_plan_id`) |
| BONUS / SPLIT | no cash movement → **no** ledger posting; adjusts lots/quantities only |

The ledger sees rupees moving between an investment asset account and a cash account. The
Investment context sees *which instrument, how many units, at what per-unit price, and
which tax lots*. The link is `investment_transaction.ledger_txn_id` → `ledger_transaction.id`.

The per-instrument "investment asset account" is a `type=ASSET` ledger account
(`institution.kind=BROKER`, e.g. Zerodha). One ledger asset account may back many
instruments; instrument-level breakdown lives here, not in the ledger.

## Schema

```sql
-- Security master. One row per tradeable instrument the owner holds/tracks.
CREATE TABLE instrument (
    id            UUID PRIMARY KEY,              -- v7
    owner_id      UUID NOT NULL,
    symbol        TEXT NOT NULL,                 -- 'INFY', 'INF209K01157' (ISIN), MF scheme code
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,                 -- EQUITY|MF|ETF|BOND
    currency      CHAR(3) NOT NULL DEFAULT 'INR',
    asset_class   TEXT NOT NULL DEFAULT 'EQUITY',-- EQUITY|DEBT|GOLD|CASH|OTHER (allocation bucket)
    ledger_account_id UUID NOT NULL,             -- backing ASSET account in ledger (owned by ledger ctx)
    status        TEXT NOT NULL DEFAULT 'ACTIVE',-- ACTIVE|DELISTED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, symbol)
);
CREATE INDEX instrument_owner_kind_idx ON instrument (owner_id, kind);

-- Current position per instrument. A cache/projection over investment_transaction + tax_lot;
-- rebuildable by replaying the transaction log. NOT truth.
CREATE TABLE holding (
    id              UUID PRIMARY KEY,
    owner_id        UUID NOT NULL,
    instrument_id   UUID NOT NULL REFERENCES instrument(id),
    quantity_scaled BIGINT NOT NULL DEFAULT 0,   -- SUM of open lot quantities (scale 1e8)
    cost_basis_minor BIGINT NOT NULL DEFAULT 0,  -- SUM of open lot remaining cost (paise)
    currency        CHAR(3) NOT NULL DEFAULT 'INR',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, instrument_id)
);

-- Append-only security-level transaction log. Each cash-moving row links to a ledger txn.
-- No UPDATE/DELETE in app code (corrections are reversal rows, mirroring the ledger).
CREATE TABLE investment_transaction (
    id              UUID PRIMARY KEY,            -- v7
    owner_id        UUID NOT NULL,
    instrument_id   UUID NOT NULL REFERENCES instrument(id),
    kind            TEXT NOT NULL,               -- BUY|SELL|DIVIDEND|SIP|BONUS|SPLIT
    trade_date      TIMESTAMPTZ NOT NULL,        -- business/effective time
    quantity_scaled BIGINT NOT NULL DEFAULT 0,   -- units, scale 1e8; SELL negative not used (see kind)
    price_minor     BIGINT NOT NULL DEFAULT 0,   -- per-unit price in paise (scale: rupees→minor)
    fees_minor      BIGINT NOT NULL DEFAULT 0,   -- brokerage + taxes, paise
    gross_minor     BIGINT NOT NULL DEFAULT 0,   -- quantity×price rounded to paise (see rounding note)
    cash_minor      BIGINT NOT NULL DEFAULT 0,   -- net cash moved (BUY: gross+fees; SELL: gross-fees)
    split_ratio_num INT,                         -- SPLIT/BONUS numerator   (e.g. 2 for 2:1 split)
    split_ratio_den INT,                         -- SPLIT/BONUS denominator (e.g. 1 for 2:1 split)
    currency        CHAR(3) NOT NULL DEFAULT 'INR',
    sip_plan_id     UUID REFERENCES sip_plan(id),
    lot_method      TEXT,                        -- SELL only: FIFO|LIFO|SPECIFIC
    ledger_txn_id   UUID,                        -- FK to ledger_transaction (null for BONUS/SPLIT)
    idempotency_key TEXT,
    reverses_txn_id UUID REFERENCES investment_transaction(id),
    status          TEXT NOT NULL DEFAULT 'POSTED', -- POSTED|REVERSED
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX inv_txn_instr_time_idx ON investment_transaction (owner_id, instrument_id, trade_date);

-- Tax lots: one row per acquisition parcel (BUY / SIP instalment / BONUS units).
-- The cost-basis truth. A SELL consumes lots and closes/reduces them.
CREATE TABLE tax_lot (
    id                 UUID PRIMARY KEY,          -- v7
    owner_id           UUID NOT NULL,
    instrument_id      UUID NOT NULL REFERENCES instrument(id),
    open_txn_id        UUID NOT NULL REFERENCES investment_transaction(id),
    acquired_date      TIMESTAMPTZ NOT NULL,      -- for holding-period / LTCG-STCG classification
    original_qty_scaled BIGINT NOT NULL,          -- units acquired (scale 1e8)
    remaining_qty_scaled BIGINT NOT NULL,         -- units still open (0 = fully consumed)
    cost_per_unit_minor BIGINT NOT NULL,          -- acquisition cost per unit incl. allocated fees, paise (scale 1e0 with 1e8 qty; see basis note)
    currency           CHAR(3) NOT NULL DEFAULT 'INR',
    status             TEXT NOT NULL DEFAULT 'OPEN', -- OPEN|PARTIAL|CLOSED
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tax_lot_open_idx ON tax_lot (owner_id, instrument_id, status, acquired_date, id);

-- Realized-gain ledger: one row per lot consumed by a SELL. Append-only. The audit trail
-- behind realized P&L and STCG/LTCG classification.
CREATE TABLE lot_consumption (
    id               UUID PRIMARY KEY,
    owner_id         UUID NOT NULL,
    sell_txn_id      UUID NOT NULL REFERENCES investment_transaction(id),
    tax_lot_id       UUID NOT NULL REFERENCES tax_lot(id),
    qty_scaled       BIGINT NOT NULL,             -- units taken from this lot (scale 1e8)
    proceeds_minor   BIGINT NOT NULL,             -- allocated net proceeds for this slice, paise
    cost_minor       BIGINT NOT NULL,             -- allocated cost for this slice, paise
    realized_gain_minor BIGINT NOT NULL,          -- proceeds_minor - cost_minor (signed)
    term             TEXT NOT NULL,               -- SHORT|LONG (holding-period at sell)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX lot_consumption_sell_idx ON lot_consumption (sell_txn_id);

-- Recurring SIP definition. Each instalment produces a BUY investment_transaction.
CREATE TABLE sip_plan (
    id            UUID PRIMARY KEY,
    owner_id      UUID NOT NULL,
    instrument_id UUID NOT NULL REFERENCES instrument(id),
    amount_minor  BIGINT NOT NULL,                -- rupees-per-instalment in paise
    currency      CHAR(3) NOT NULL DEFAULT 'INR',
    cadence       TEXT NOT NULL,                  -- MONTHLY|WEEKLY|QUARTERLY
    day_of_period INT NOT NULL,                   -- e.g. 5 = 5th of month
    start_date    DATE NOT NULL,
    end_date      DATE,
    status        TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE|PAUSED|ENDED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mark price for revaluation / unrealized gain. Manual entry in Phase 5; a feed adapter
-- writes the same table later. Latest quote ≤ asOf is the mark.
CREATE TABLE price_quote (
    id            UUID PRIMARY KEY,
    owner_id      UUID NOT NULL,                  -- owner-scoped (personal manual entry); global feed later
    instrument_id UUID NOT NULL REFERENCES instrument(id),
    as_of         TIMESTAMPTZ NOT NULL,           -- price date
    price_minor   BIGINT NOT NULL,                -- per-unit price, paise
    currency      CHAR(3) NOT NULL DEFAULT 'INR',
    source        TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL|FEED (FEED deferred)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, instrument_id, as_of, source)
);
CREATE INDEX price_quote_lookup_idx ON price_quote (instrument_id, as_of);

-- Target asset allocation for rebalancing. Weights are basis points (sum ≤ 10000).
CREATE TABLE target_allocation (
    id            UUID PRIMARY KEY,
    owner_id      UUID NOT NULL,
    asset_class   TEXT NOT NULL,                  -- EQUITY|DEBT|GOLD|CASH|OTHER
    target_bps    INT NOT NULL,                   -- basis points, e.g. 6000 = 60%
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, asset_class)
);

-- Derived-calc cache (XIRR, gains). Never truth; safe to drop & recompute.
CREATE TABLE portfolio_metric_cache (
    id            UUID PRIMARY KEY,
    owner_id      UUID NOT NULL,
    scope         TEXT NOT NULL,                  -- 'PORTFOLIO' | 'INSTRUMENT:<id>'
    metric        TEXT NOT NULL,                  -- XIRR|UNREALIZED_GAIN|REALIZED_GAIN
    as_of         TIMESTAMPTZ NOT NULL,
    value_scaled  BIGINT NOT NULL,                -- rate scale 1e6 (XIRR) or paise (gains)
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, scope, metric, as_of)
);
```

### Scale conventions (no floats — ever)

- **Money:** `BIGINT` minor units (paise), per convention §2. `currency` on every table.
- **Quantities:** `quantity_scaled` / `*_qty_scaled` are integer **units × 1e8**
  (`QTY_SCALE = 100_000_000`). One share/unit = `100_000_000`. 1e8 comfortably covers MF
  units quoted to 3–4 decimals and fractional/bonus shares without precision loss.
- **Prices:** `price_minor` is per-unit price in paise (a ₹1,543.50 share = `154350`).
- **XIRR rate:** stored/cached as an integer **rate × 1e6** (`RATE_SCALE = 1_000_000`);
  e.g. 12.3456% → `123456`.
- **Allocation weights:** basis points (`10000 = 100%`).
- **gross = quantity × price** is computed as `(quantity_scaled × price_minor) / QTY_SCALE`
  with **HALF_EVEN** rounding to paise, at the derivation boundary (convention §2). The
  rounding site is the `Money`/quantity value object, never inline `long` math.

**Sign / semantics:** `quantity_scaled` on `investment_transaction` is always the positive
traded quantity; direction is carried by `kind` (BUY adds, SELL removes). `cash_minor` is
always positive; direction is carried by the ledger postings.

## Cost basis: lot-based truth, average as a view

- **Truth is lot-based.** Every acquisition creates a `tax_lot`. This is required for tax
  lot accounting and FIFO/LIFO. The `holding` row is a rollup cache.
- **Average cost** is a *derived view*, never a stored basis:
  `avg_cost_per_unit = SUM(open lot remaining cost_minor) / SUM(open lot remaining_qty_scaled × 1)`
  exposed via `InvestmentApi.averageCost(instrumentId)`. Presentation-boundary HALF_EVEN.

## Tax lots and realized gain

**On SELL** (`kind=SELL`, `lot_method ∈ {FIFO, LIFO, SPECIFIC}`):

1. Select open lots for the instrument:
   - **FIFO** — ascending `acquired_date, id`.
   - **LIFO** — descending `acquired_date, id`.
   - **SPECIFIC** — caller supplies an explicit ordered list of `tax_lot_id` + qty.
2. Consume lots in order until the sold quantity is satisfied; the last lot may be
   partially consumed (`remaining_qty_scaled` reduced, `status → PARTIAL`; `→ CLOSED` at 0).
3. Allocate net proceeds across consumed slices pro-rata by quantity.
4. Write one `lot_consumption` row per slice and its realized gain.

**Realized-gain formula** (per consumed slice, all integer paise):

```
sold_qty_scaled      = Σ slice qty_scaled                     (= SELL quantity_scaled)
net_proceeds_minor   = gross_minor − fees_minor               (SELL cash_minor)

slice.proceeds_minor = round_half_even( net_proceeds_minor × slice.qty_scaled / sold_qty_scaled )
slice.cost_minor     = round_half_even( lot.cost_per_unit_minor × slice.qty_scaled / QTY_SCALE )
slice.realized_gain_minor = slice.proceeds_minor − slice.cost_minor

realized_gain(SELL)  = Σ slice.realized_gain_minor
```

A residual-paise correction assigns the rounding remainder to the final slice so
`Σ slice.proceeds_minor == net_proceeds_minor` exactly (no phantom paise). `term` is
`LONG` if `sell.trade_date − lot.acquired_date ≥` the instrument's LTCG threshold
(equity/equity-MF 12 months; debt per rules), else `SHORT` — classification only, not tax
computation (tax engine is out of Phase 5 scope).

The SELL's ledger transaction credits the investment asset account at **cost**
(`Σ slice.cost_minor`), debits cash at **net proceeds**, and posts the realized gain/loss
to an income/expense account, keeping the ledger balanced.

## Corporate actions

Modeled as `investment_transaction` rows with **no cash movement** (`ledger_txn_id` null),
adjusting lots/quantities only:

- **SPLIT** (`split_ratio_num:split_ratio_den`, e.g. 2:1). For every open lot:
  ```
  remaining_qty_scaled ← remaining_qty_scaled × num / den
  original_qty_scaled  ← original_qty_scaled  × num / den
  cost_per_unit_minor  ← round_half_even( cost_per_unit_minor × den / num )
  ```
  **Invariant preserved:** total cost basis is unchanged (`qty × cost_per_unit` constant);
  only the per-unit split changes. `acquired_date` is preserved per lot (holding period
  carries through a split).
- **BONUS** (e.g. 1:1 bonus). New units at **zero cost** added to the position: creates a
  new `tax_lot` with `cost_per_unit_minor = 0`, `acquired_date =` the bonus record date
  (bonus shares start their own holding period), quantity = entitled bonus units. Total
  basis unchanged; per-unit average basis falls.
- **DIVIDEND** (`kind=DIVIDEND`). Cash event: no lot/quantity change; posts a ledger
  transaction (cash ← dividend income). Reinvested dividends are modeled as a DIVIDEND
  (cash in) immediately followed by a BUY (cash out), preserving double-entry.

## XIRR — derived, numerically solved, never stored truth

XIRR is the money-weighted return: the rate `r` solving `Σ CFᵢ / (1+r)^(tᵢ/365) = 0`.

**Cash-flow series** (portfolio- or instrument-scoped, as of `T`):

- **Outflows (negative):** every BUY / SIP instalment `cash_minor` at `trade_date`.
- **Inflows (positive):** every SELL net proceeds and every DIVIDEND `cash_minor` at
  `trade_date`.
- **Terminal inflow:** current **market value at `T`** = `Σ (open lot remaining_qty_scaled × mark_price_minor / QTY_SCALE)` using the latest `price_quote ≤ T`, added as a synthetic
  positive cash flow dated `T`.

Solved numerically: **Newton–Raphson** seeded at `r = 0.1`, falling back to **bisection**
on `[-0.9999, 10.0]` when the derivative is near-zero or Newton diverges. Convergence at
`|f(r)| < 1e-7` (on rupee-scaled sums). It is a **derived calc, not stored truth** — cached
in `portfolio_metric_cache` keyed by `(scope, XIRR, as_of)` and invalidated whenever a new
`investment_transaction` or `price_quote` for that scope arrives. Dropping the cache and
recomputing yields the same value (deterministic).

## Unrealized gain

Requires a mark. `unrealized_gain(instrument, T) = market_value(T) − open_cost_basis`,
where `market_value(T) = Σ(open lot remaining_qty_scaled × mark_price_minor) / QTY_SCALE`
(HALF_EVEN to paise) using the latest `price_quote ≤ T`, and `open_cost_basis =
holding.cost_basis_minor`. If no `price_quote` exists for an instrument, its unrealized
gain is reported as **unknown/null** (not zero) — surfaced in the API so the user knows a
manual price is needed. Cached like XIRR; invalidated on new price or transaction.

## Asset allocation & rebalancing

- **Current allocation:** group open-position market value by `instrument.asset_class`;
  `current_bps = round(class_value × 10000 / total_portfolio_value)`.
- **Target allocation:** `target_allocation` rows (basis points).
- **Rebalancing suggestion:** per class, `delta_bps = target_bps − current_bps`, and
  `delta_minor = round_half_even( total_value × delta_bps / 10000 )`. Positive → **BUY**
  that amount into the class; negative → **SELL**. Suggestions are advisory read-model
  output only — they never post transactions. A `min_band_bps` threshold (default 500 bps)
  suppresses trivial drift so tiny deviations don't generate churn.

## Core operations (`InvestmentApi`)

- **`recordBuy(cmd)`** — creates instrument if new, inserts `investment_transaction`
  (BUY), opens a `tax_lot`, updates `holding`, posts the cash ledger transaction via
  `LedgerApi.postTransaction` (asset ← cash), and appends `InvestmentTransactionRecorded`
  to the outbox — all in one DB transaction. Idempotent on `idempotency_key` (shared with
  the ledger post).
- **`recordSell(cmd)`** — selects and consumes lots per `lot_method` (or explicit lots for
  SPECIFIC), writes `lot_consumption` rows + realized gain, reduces `holding`, posts the
  ledger transaction (cash ← asset-at-cost + realized gain/loss), emits
  `RealizedGainRecorded`. Rejects `INVESTMENT_INSUFFICIENT_QUANTITY` if lots can't cover.
- **`recordDividend(cmd)`** — DIVIDEND transaction + ledger post (cash ← dividend income).
- **`recordCorporateAction(cmd)`** — SPLIT or BONUS; adjusts lots/quantities per rules
  above; no ledger post.
- **`runSipInstalment(sipPlanId, at)`** — materializes one BUY from a `sip_plan` (invoked
  by a scheduled job or an outbox-driven scheduler consumer); same path as `recordBuy`
  tagged with `sip_plan_id`.
- **`reverseTransaction(txnId, reason)`** — append a `REVERSED`-marked compensating
  investment transaction, reopen/adjust lots, and reverse the linked ledger transaction
  via `LedgerApi.reverseTransaction`. Never mutates the original row (append-only).
- **`recordPrice(cmd)`** — inserts a manual `price_quote`; emits `PriceQuoteRecorded`,
  invalidating dependent metric caches.
- **`averageCost(instrumentId)`**, **`costBasis(instrumentId)`** — lot-based / average view.
- **`unrealizedGain(scope, asOf)`**, **`realizedGain(scope, from, to)`**,
  **`xirr(scope, asOf)`** — derived metrics (cache-backed).
- **`allocation(asOf)`**, **`rebalanceSuggestions(asOf)`** — allocation analysis + deltas.
- **`holdings(asOf)`** — positions with cost, market value, unrealized gain.

`InvestmentApi` is the only entry point for other contexts (analytics reads holdings +
gains for net-worth breakdown via the outbox stream, per convention §11). Cross-context
cash movement goes through `LedgerApi` — Investment never writes ledger tables directly.

## Events

**Emitted (outbox, transactional per convention §6):**

- `InvestmentTransactionRecorded` — `{txnId, instrumentId, kind, tradeDate, quantityScaled,
  priceMinor, cashMinor, currency, ledgerTxnId, schemaVersion}`.
- `RealizedGainRecorded` — `{sellTxnId, instrumentId, realizedGainMinor, consumptions:
  [{taxLotId, qtyScaled, gainMinor, term}], currency}`.
- `HoldingRevalued` — `{instrumentId, asOf, marketValueMinor, unrealizedGainMinor}` (emitted
  when a new price marks a position).
- `PriceQuoteRecorded` — `{instrumentId, asOf, priceMinor, source}`.

**Consumed:**

- `PriceQuoteRecorded` (internal) — recompute `holding` market value, emit `HoldingRevalued`,
  invalidate `portfolio_metric_cache` for the instrument + portfolio.
- (Deferred) a future market-data feed publishes `PriceQuoteRecorded` with `source=FEED`
  through the same consumer — no code change here.

Analytics/net-worth downstream consume `InvestmentTransactionRecorded` + `HoldingRevalued`
to fold investment value into net position; the ledger's `TransactionPosted` already
carries the cash leg, so the two never double-count (investment value = market value of the
asset accounts, cost already in the ledger).

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/instruments` | create/register an instrument |
| GET | `/api/v1/instruments` | list (owner-scoped) |
| POST | `/api/v1/investment-transactions` | record BUY/SELL/DIVIDEND (Idempotency-Key required) |
| POST | `/api/v1/investment-transactions/{id}/reversal` | reverse an investment transaction |
| POST | `/api/v1/corporate-actions` | record SPLIT/BONUS |
| POST | `/api/v1/sip-plans` | create a SIP plan |
| POST | `/api/v1/sip-plans/{id}/instalments` | run/backfill one instalment |
| POST | `/api/v1/prices` | record a manual price quote |
| GET | `/api/v1/holdings?asOf=` | positions: qty, cost, market value, unrealized gain |
| GET | `/api/v1/instruments/{id}/lots` | open/closed tax lots |
| GET | `/api/v1/instruments/{id}/cost-basis?method=avg\|lot` | cost basis view |
| GET | `/api/v1/portfolio/xirr?scope=&asOf=` | money-weighted return |
| GET | `/api/v1/portfolio/gains?from=&to=` | realized + unrealized gains |
| POST | `/api/v1/target-allocation` | set target weights (bps) |
| GET | `/api/v1/portfolio/allocation?asOf=` | current vs target allocation |
| GET | `/api/v1/portfolio/rebalance?asOf=` | rebalancing suggestions (delta to target) |

All POSTs creating financial state require an `Idempotency-Key` (convention §7) and reuse
the platform idempotency store. Money in JSON as `{amountMinor, currency}`; quantities as
`{quantityScaled}` (integer, scale 1e8) — never floats. Errors as RFC 9457 problem+json
with a stable `code` enum (`INVESTMENT_INSUFFICIENT_QUANTITY`,
`INVESTMENT_UNKNOWN_INSTRUMENT`, `INVESTMENT_NO_PRICE_FOR_MARK`,
`INVESTMENT_INVALID_SPLIT_RATIO`, `INVESTMENT_LOT_SELECTION_MISMATCH`).

## Testing

- **Property — lot conservation:** for any sequence of BUY/SELL, `SUM(remaining_qty across
  lots)` equals `holding.quantity_scaled` equals `SUM(BUY qty) − SUM(SELL qty)`; no negative
  lot remainder.
- **Property — cost-basis conservation:** `holding.cost_basis_minor == SUM(open lot
  remaining_qty × cost_per_unit)`; realized + remaining cost reconciles to total acquired
  cost.
- **Realized gain:** FIFO vs LIFO vs SPECIFIC on the same trade set produce the expected
  differing per-lot gains; slice proceeds sum exactly to net proceeds (no phantom paise).
- **Corporate actions:** split preserves total cost basis and holding value; bonus adds
  zero-cost units and preserves total basis; per-unit average falls as expected.
- **Ledger coupling:** every cash-moving investment transaction produces exactly one
  balanced `ledger_transaction`; reversal reverses both; BONUS/SPLIT produce none. Sum-zero
  holds on every posted ledger transaction.
- **XIRR:** known-answer tests (single BUY + terminal value → simple CAGR; textbook
  multi-flow cases); Newton and bisection agree; deterministic recompute from log +
  prices matches the cached value.
- **Unrealized gain:** unknown when no price exists (null, not zero); correct against a
  known mark; revaluation on `PriceQuoteRecorded` emits `HoldingRevalued`.
- **Allocation/rebalance:** deltas sum to zero across classes at full target; `min_band_bps`
  suppresses sub-threshold drift; suggestions never post transactions.
- **Idempotency & replay:** same `Idempotency-Key` replays the original result, no double
  lot/holding/ledger effect; `holding` rebuilt from replaying `investment_transaction` +
  `tax_lot` matches live state (projection determinism, convention §8).
- **Persistence:** integration tests on real Postgres (Testcontainers); append-only tables
  reject UPDATE/DELETE from the app role.
```
