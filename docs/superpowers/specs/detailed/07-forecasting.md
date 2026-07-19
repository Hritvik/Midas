# 07 — Forecasting Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 4**
(alongside budgeting). The forward-looking read side: *"what happens to my money if
nothing changes — and what if X changes?"* Forecasts are **derived and ephemeral (or
cached), never truth.** Truth lives in `ledger`; this context only projects it.

## Responsibilities

- **Recurring model:** a `recurring_rule` table the user defines (cadence, amount,
  account, category, start/end). Rules can be seeded from `import`'s detected
  recurrences (candidate → confirmed), or entered by hand.
- **Forecast engine:** project balances forward from *current* balances (read from
  `LedgerApi` / the analytics net-worth projection) + active recurring rules +
  assumptions, over a horizon at monthly granularity. Deterministic.
- **Assumption sets:** named, versioned parameter bundles (inflation, salary growth,
  investment return, cash yield). All default rates are **tunable seeds**, not constants.
- **Scenarios:** a scenario = a base assumption set + an ordered set of overrides/events
  (job loss, home purchase, reduced income, startup attempt, retirement, FIRE). Stored;
  projected deterministically.
- **Cache, not source of truth:** projection outputs are memoized in `forecast_run` /
  `forecast_point`, keyed by an input fingerprint, and invalidated when the ledger moves.
  Dropping the cache and recomputing MUST reproduce identical numbers (deterministic).

**Phase-1 MVP note:** the minimal "recurring transaction projection" shipped in Phase 1
is exactly `recurring_rule` + the base recurrence below with a single default assumption
set and no scenarios. Everything else here (assumption versioning, scenarios, overrides)
layers on without touching that core.

## Schema

All money is `BIGINT` minor units (paise) + `currency CHAR(3)` (INR only, Phases 0–5).
Rates are stored as basis points (`INT`, bps) to stay integer-exact — `600` = 6.00% p.a.

```sql
-- User- or import-defined recurring cash flow. Not truth; a projection input.
CREATE TABLE recurring_rule (
    id             UUID PRIMARY KEY,              -- v7
    owner_id       UUID NOT NULL,
    label          TEXT NOT NULL,                 -- "Salary", "Rent", "SIP Nifty50"
    kind           TEXT NOT NULL,                 -- INCOME|EXPENSE|TRANSFER|INVESTMENT
    account_id     UUID NOT NULL,                 -- ledger account the flow lands on/leaves
    counter_account_id UUID,                       -- optional other leg (transfers/SIP)
    category       TEXT,                          -- analytics category tag
    amount_minor   BIGINT NOT NULL CHECK (amount_minor > 0),
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    cadence        TEXT NOT NULL,                 -- MONTHLY|WEEKLY|QUARTERLY|YEARLY|ONE_OFF
    interval_count INT  NOT NULL DEFAULT 1,       -- every N cadence units
    day_of_period  INT,                           -- e.g. day-of-month 1..28 (salary on 1st)
    start_date     DATE NOT NULL,
    end_date       DATE,                          -- NULL = open-ended
    growth_binding TEXT NOT NULL DEFAULT 'AUTO',  -- AUTO|INFLATION|SALARY|INVESTMENT|NONE
    source         TEXT NOT NULL DEFAULT 'MANUAL',-- MANUAL|IMPORT_DETECTED
    status         TEXT NOT NULL DEFAULT 'ACTIVE',-- CANDIDATE|ACTIVE|PAUSED|ARCHIVED
    origin_pattern_id UUID,                        -- import RecurringPatternDetected ref
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX rec_rule_owner_status_idx ON recurring_rule (owner_id, status);

-- Named, versioned macro assumptions. Immutable per version (append a new version to edit).
CREATE TABLE assumption_set (
    id                    UUID PRIMARY KEY,        -- v7
    owner_id              UUID NOT NULL,
    name                  TEXT NOT NULL,           -- "Baseline 2026", "Conservative"
    version               INT  NOT NULL DEFAULT 1,
    inflation_bps         INT  NOT NULL DEFAULT 600,  -- 6.00% p.a.  (tunable seed)
    salary_growth_bps     INT  NOT NULL DEFAULT 800,  -- 8.00% p.a.  (tunable seed)
    equity_return_bps     INT  NOT NULL DEFAULT 1200, -- 12.00% p.a. (tunable seed)
    debt_return_bps       INT  NOT NULL DEFAULT 700,  -- 7.00% p.a.  (tunable seed)
    cash_yield_bps        INT  NOT NULL DEFAULT 300,  -- 3.00% p.a.  (tunable seed)
    safe_withdrawal_bps   INT  NOT NULL DEFAULT 350,  -- 3.50% p.a., FIRE (tunable seed)
    is_default            BOOLEAN NOT NULL DEFAULT false,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name, version)
);
CREATE UNIQUE INDEX assumption_set_one_default_idx
    ON assumption_set (owner_id) WHERE is_default;

-- A stored scenario = base assumptions + a set of override events.
CREATE TABLE scenario (
    id                UUID PRIMARY KEY,            -- v7
    owner_id          UUID NOT NULL,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,               -- BASELINE|HOME_PURCHASE|REDUCED_INCOME|
                                                   -- JOB_LOSS|STARTUP|RETIREMENT|FIRE|CUSTOM
    assumption_set_id UUID NOT NULL REFERENCES assumption_set(id),
    horizon_months    INT  NOT NULL DEFAULT 120,   -- default 10y (tunable)
    version           INT  NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name, version)
);

-- Ordered overrides applied to the base rules/assumptions at a given projection month.
CREATE TABLE scenario_event (
    id             UUID PRIMARY KEY,               -- v7
    scenario_id    UUID NOT NULL REFERENCES scenario(id),
    at_month       INT  NOT NULL,                  -- 0-based month offset from t0
    op             TEXT NOT NULL,                  -- DISABLE_RULE|SET_RULE_AMOUNT|ADD_RULE|
                                                   -- ONE_OFF_FLOW|OVERRIDE_ASSUMPTION
    target_rule_id UUID REFERENCES recurring_rule(id),  -- for DISABLE/SET
    payload        JSONB NOT NULL,                 -- op-specific args (see below)
    sequence_number INT NOT NULL,                  -- deterministic apply order within a month
    UNIQUE (scenario_id, at_month, sequence_number)
);
CREATE INDEX scenario_event_scenario_idx ON scenario_event (scenario_id, at_month);

-- Memoized projection output. Cache, NOT truth. Safe to drop and recompute.
CREATE TABLE forecast_run (
    id                UUID PRIMARY KEY,            -- v7
    owner_id          UUID NOT NULL,
    scenario_id       UUID REFERENCES scenario(id),-- NULL = bare baseline projection
    input_fingerprint TEXT NOT NULL,               -- SHA-256 over inputs (see caching)
    t0                DATE NOT NULL,               -- projection anchor (business date)
    horizon_months    INT  NOT NULL,
    granularity       TEXT NOT NULL DEFAULT 'MONTHLY',
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_stale          BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (owner_id, input_fingerprint)
);
CREATE INDEX forecast_run_owner_scn_idx ON forecast_run (owner_id, scenario_id, computed_at);

-- One projected point per (run, account-or-aggregate, month).
CREATE TABLE forecast_point (
    id             UUID PRIMARY KEY,               -- v7
    run_id         UUID NOT NULL REFERENCES forecast_run(id) ON DELETE CASCADE,
    month_offset   INT  NOT NULL,                  -- 0..horizon
    as_of          DATE NOT NULL,
    account_id     UUID,                           -- NULL = portfolio aggregate row
    balance_minor  BIGINT NOT NULL,
    inflow_minor   BIGINT NOT NULL DEFAULT 0,
    outflow_minor  BIGINT NOT NULL DEFAULT 0,
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    UNIQUE (run_id, month_offset, account_id)
);
```

`scenario_event.payload` shapes (versioned JSON):
- `SET_RULE_AMOUNT` — `{ "amountMinor": 4500000 }` (reduced income: salary rule set lower).
- `DISABLE_RULE` — `{}` (job loss at month M: `target_rule_id` = salary → flow becomes 0).
- `ADD_RULE` — a full recurring-rule body (home purchase → new EMI; startup → founder draw).
- `ONE_OFF_FLOW` — `{ "accountId": …, "amountMinor": …, "direction": "OUT" }` (home
  down-payment; retirement corpus lump withdrawal).
- `OVERRIDE_ASSUMPTION` — `{ "field": "salary_growth_bps", "value": 0 }` (startup: no raises).

## Forecast engine — the projection recurrence

Monthly granularity. Let `t = 0..H` months from anchor `t0`. Accounts partition into
**investment** accounts `I` (returns compound) and **cash/liability** accounts `C` (flows
accumulate). All rates are annual bps; the monthly factor is derived once:

```
monthlyFactor(rate_bps) = (1 + rate_bps/10000) ^ (1/12)
```

**Base recurrence** (per account `a`, month `t ≥ 1`):

```
B_a(0)  = balanceAt(a, t0)                         -- seeded from LedgerApi / analytics projection

for cash/liability a ∈ C:
    B_a(t) = B_a(t-1)
           + Σ_r  due(r, t) · inflow(r, a, t)       -- rules crediting a
           − Σ_r  due(r, t) · outflow(r, a, t)      -- rules debiting a
           ± oneOff(a, t)                            -- scenario ONE_OFF_FLOW at month t

for investment a ∈ I:
    B_a(t) = B_a(t-1) · monthlyFactor(returnRate(a))  -- growth (equity/debt per account tag)
           + Σ_r  due(r, t) · contribution(r, a, t)   -- SIP/investment inflows
```

**Rule amount with growth binding** (annual step-up applied once per completed year):

```
amount(r, t) = base(r) · (1 + g(r)/10000) ^ floor(t / 12)

g(r) = inflation_bps    if growth_binding = INFLATION  (or AUTO & kind=EXPENSE)
       salary_growth_bps if growth_binding = SALARY     (or AUTO & kind=INCOME)
       0                  if growth_binding = NONE
```

**`due(r, t)`** is 1 iff month `t` falls on rule `r`'s cadence schedule (respecting
`interval_count`, `start_date`, `end_date`), else 0. `ONE_OFF` rules fire once.

**Scenario application:** before evaluating month `t`, apply every `scenario_event` with
`at_month = t` in `sequence_number` order — disabling rules, mutating amounts, adding
rules, injecting one-offs, or overriding an assumption field from that month forward. This
makes each scenario a pure function of `(t0 balances, ruleset, assumption_set, events)`.

**Rounding:** all growth/compounding math is done in integer paise with banker's rounding
(HALF_EVEN) applied **only** at each month boundary when writing `forecast_point` — the
one documented derivation boundary (per conventions §2). Same inputs → same rounded path.

**Scenario semantics (worked):**
- *Home purchase* — `ONE_OFF_FLOW` (down payment out of a cash account at month M) **plus**
  `ADD_RULE` (EMI: `EXPENSE`, `MONTHLY`, start month M, end month M+tenure).
- *Reduced income / job loss* — `SET_RULE_AMOUNT` lowers, or `DISABLE_RULE` zeroes, the
  salary rule from month M. Runway = first `t` where liquid `Σ B_C(t) < 0`.
- *Startup attempt* — `SET_RULE_AMOUNT` (founder draw) + `OVERRIDE_ASSUMPTION` salary
  growth → 0 + optional one-off capital injection.
- *Retirement / FIRE* — disable income rules; enable a withdrawal rule sized by
  `safe_withdrawal_bps · portfolio / 12`; FIRE target = smallest corpus where projected
  passive draw ≥ projected expenses.

## Core operations (`ForecastingApi`)

- **`upsertRecurringRule(cmd)`** — create/edit a rule (owner-scoped, validated cadence &
  account). Import-detected rules arrive as `status=CANDIDATE`; `confirmRule(id)` promotes
  to `ACTIVE`. Mutating any rule invalidates dependent cached runs.
- **`upsertAssumptionSet(cmd)`** — never edits in place; writes `version+1`. Exactly one
  `is_default` per owner.
- **`createScenario(cmd)` / `addScenarioEvent(scenarioId, event)`** — build a stored
  scenario from a base assumption set + ordered events.
- **`project(scenarioId?, horizonMonths?, asOf?)`** — the engine. Reads `t0` balances via
  `LedgerApi.balances(t0)` (or the analytics net-worth projection), runs the recurrence,
  returns a `ForecastResult { points[], summary }`. Pure/deterministic; no ledger writes.
- **`getOrComputeRun(scenarioId)`** — returns a fresh `forecast_run` from cache if its
  `input_fingerprint` matches; else recomputes and memoizes. Never returns stale numbers.
- **`compareScenarios(ids[])`** — projects several scenarios over a shared horizon for
  side-by-side deltas (net worth, runway, FIRE date).
- **`invalidate(ownerId)`** — marks runs `is_stale` when the ledger moves (event consumer).

## Caching & determinism

`input_fingerprint = SHA-256(` canonical JSON of `{ t0, latest ledger txn id at t0,
sorted active rule set + versions, assumption_set version, scenario version + event set,
horizon, granularity }` `)`. A cache hit is only valid when every input is byte-identical,
so a hit is provably the same computation. Cache is derived state (conventions §8): drop
`forecast_run`/`forecast_point`, recompute, and the numbers must be identical — a tested
property.

## Events consumed

Forecasting is a pure read/consumer context — it **emits nothing to the ledger** (it may
emit an internal `ForecastRecomputed` for UI cache-busting only).

| Event | Source | Reaction |
|---|---|---|
| `TransactionPosted` | ledger | new `t0` reality → mark owner's runs `is_stale`; next `project` reseeds balances |
| `TransactionReversed` | ledger | same as above (baseline moved) |
| `RecurringPatternDetected` | import | insert a `recurring_rule` with `status=CANDIDATE`, `source=IMPORT_DETECTED`, `origin_pattern_id` set — awaits user confirm |
| `BudgetEnvelopeChanged` *(opt)* | budgeting | reconcile expense rules with envelope allocations (advisory) |

Consumers are idempotent, keyed `(consumer_name, event_id)` in `processed_event`
(conventions §6). Redelivery of `RecurringPatternDetected` is a no-op via
`origin_pattern_id` uniqueness.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/forecasting/recurring-rules` | create rule (Idempotency-Key) |
| GET | `/api/v1/forecasting/recurring-rules` | list (owner-scoped, filter by status) |
| PATCH | `/api/v1/forecasting/recurring-rules/{id}` | edit / pause / confirm candidate |
| POST | `/api/v1/forecasting/assumption-sets` | create versioned assumption set |
| GET | `/api/v1/forecasting/assumption-sets` | list |
| POST | `/api/v1/forecasting/scenarios` | create scenario |
| POST | `/api/v1/forecasting/scenarios/{id}/events` | append a scenario override event |
| GET | `/api/v1/forecasting/scenarios/{id}/projection?horizon=&asOf=` | compute/fetch projection |
| GET | `/api/v1/forecasting/projection?horizon=&asOf=` | bare baseline projection (no scenario) |
| GET | `/api/v1/forecasting/scenarios/compare?ids=` | side-by-side scenario deltas |

Projection responses carry `{ points:[{monthOffset, asOf, balance:{amountMinor,currency},
inflow, outflow}], summary:{ endNetWorth, runwayMonths, fireDate? }, computedAt,
fromCache }`. Money always `{amountMinor, currency}`; never a float (conventions §7).

## Testing

- **Recurrence determinism:** same `(t0, rules, assumptions, scenario)` → identical
  `forecast_point` path (property test over random rule sets and horizons).
- **Cache = recompute:** memoized run and a from-scratch recompute produce byte-identical
  points; dropping cache changes nothing (conventions §8 determinism property).
- **Baseline sanity:** with zero recurring rules, projection is flat at `t0` balances
  (investment growth aside); a single `MONTHLY` income rule adds exactly `amount·t` at
  month `t` before growth.
- **Growth math:** step-up applies once per completed year; monthly-factor compounding of
  an investment account matches closed-form `B0·(1+r)^(t/12)` within one paise (rounding).
- **Scenario correctness:** job-loss `DISABLE_RULE` zeroes the salary flow from month M;
  home-purchase one-off + EMI reproduce expected dip-then-drain; runway = first month
  liquid balance < 0.
- **Cache invalidation:** a `TransactionPosted` for the owner marks runs stale and the
  next `project` reseeds `t0` from the new ledger balance.
- **Import handoff:** `RecurringPatternDetected` creates exactly one `CANDIDATE` rule;
  redelivery is a no-op (idempotent on `origin_pattern_id`).
- **API contract tests:** each endpoint — happy path + validation (422) + stable error
  `code` (e.g. `FORECAST_INVALID_HORIZON`, `FORECAST_UNKNOWN_ASSUMPTION_SET`).
- **Never-truth guard:** no forecasting code path writes to `ledger_transaction` /
  `posting`; forecasting owns only its own tables.
