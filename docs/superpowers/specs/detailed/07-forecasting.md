# 07 — Forecasting Context (Detailed Design)

**Amended 2026-07-23** (audit remediation — see "## Audit fixes applied" at the end).

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 4**
(alongside budgeting). The forward-looking read side: *"what happens to my money if
nothing changes — and what if X changes?"* Forecasts are **derived and ephemeral (or
cached), never truth.** Truth lives in `ledger`; this context only projects it.

## Responsibilities

- **Recurring model:** a `recurring_rule` table the user defines (cadence, amount,
  account, category, start/end). Rules can be seeded from `import`'s detected
  recurrences (candidate → confirmed), or entered by hand. An import-detected rule and a
  pre-existing manual rule that cover the **same obligation** must be **deduped** (merge or
  replace), never coexist — otherwise the same flow is double-counted (see §"Import/manual
  rule dedup"). This is the forecasting projection of the shared **recurring-obligation
  registry** seam (conventions §12, reserved seam #3): forecasting *projects from* the
  registry; a single obligation resolves to a single active rule.
- **Forecast engine:** project balances forward from *current* balances (read from the
  strongly-consistent `LedgerApi.balances(t0)` — never the eventually-consistent analytics
  projection) + active recurring rules + assumptions, over a horizon at monthly granularity.
  Deterministic.
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
                                                  --   WEEKLY/biweekly fire >1×/month bucket via
                                                  --   due(r,t) occurrence-count (see engine)
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
    cash_yield_bps        INT  NOT NULL DEFAULT 300,  -- 3.00% p.a.; applied to cash-asset (K) accts
    safe_withdrawal_bps   INT  NOT NULL DEFAULT 350,  -- 3.50% p.a.; dynamic-withdrawal mode only
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
  **The rule's `ruleStartMonth` is derived from the event's `at_month` (t0 + M months);** any
  `start_date`/`end_date` in the body are **overridden** by the event offset (start = t0+M,
  end = t0+M+tenure), so the body dates never conflict with the offset. This resolves the
  DATE-vs-offset ambiguity: the scenario always speaks in month offsets from t0.
- `ONE_OFF_FLOW` — `{ "accountId": …, "amountMinor": …, "direction": "OUT" }` (home
  down-payment; retirement corpus lump withdrawal).
- `OVERRIDE_ASSUMPTION` — `{ "field": "salary_growth_bps", "value": 0 }` (startup: no raises).

## Forecast engine — the projection recurrence

Monthly granularity. Let `t = 0..H` months from anchor `t0`. **Month buckets are
owner-local** (conventions §3): month `t` is the owner-local calendar month `t` months
after the owner-local month containing `t0`; a DATE maps to a bucket by its owner-local
month. Accounts partition into:

- **`I` — investment** accounts (returns compound at `returnRate(a)`),
- **`K` — cash-asset** accounts (`account.type = ASSET`, non-investment: savings, current,
  wallet), which accrue at `cash_yield_bps` and accumulate flows,
- **`L` — liability** accounts (loans, credit cards), which accumulate flows but do **not**
  accrue a yield.

`C := K ∪ L` is the union of non-investment accounts referenced by the recurrence below.
**Runway is measured over `K` only** (see "Runway", never `C`) — liabilities must never be
summed into a "liquid" balance.

All rates are annual bps; the monthly factor is derived once:

```
monthlyFactor(rate_bps) = (1 + rate_bps/10000) ^ (1/12)
```

**Base recurrence** (per account `a`, month `t ≥ 1`):

```
B_a(0)  = balanceAt(a, t0)   -- seeded ONLY from LedgerApi.balances(t0) (strongly consistent,
                             --   conventions §8); NEVER the eventually-consistent projection

for cash-asset a ∈ K:                              -- savings/current/wallet
    B_a(t) = B_a(t-1) · monthlyFactor(cash_yield_bps)  -- idle cash accrues the seeded yield
           + Σ_r  due(r, t) · inflow(r, a, t)          -- rules crediting a
           − Σ_r  due(r, t) · outflow(r, a, t)         -- rules debiting a
           ± oneOff(a, t)                               -- scenario ONE_OFF_FLOW at month t

for liability a ∈ L:                               -- loans/credit cards (no yield term)
    B_a(t) = B_a(t-1)
           + Σ_r  due(r, t) · inflow(r, a, t)
           − Σ_r  due(r, t) · outflow(r, a, t)
           ± oneOff(a, t)

for investment a ∈ I:
    B_a(t) = B_a(t-1) · monthlyFactor(returnRate(a))  -- growth (equity/debt per account tag)
           + Σ_r  due(r, t) · contribution(r, a, t)   -- SIP/investment inflows
```

> Liability interest accrual (loan `rate_bps`) is modelled by the loan/liabilities context via
> its own recurring rules feeding `L`; the cash-yield factor above applies to `K` only.

**Rule amount with growth binding** (annual step-up keyed to **rule age**, tracked
cumulatively per rule — not to the absolute projection month):

```
stepYears(r, t) = floor( (t − ruleStartMonth(r)) / 12 )      -- 0 while t < ruleStartMonth(r)
amount(r, t)    = base(r) · Π_{y=1..stepYears(r,t)} (1 + g(r, ruleStartMonth(r)+12·(y-1)) / 10000)

g(r, m) = the rule's effective growth bps as of month m (see freeze rule below):
          inflation_bps     if growth_binding = INFLATION  (or AUTO & kind=EXPENSE)
          salary_growth_bps if growth_binding = SALARY     (or AUTO & kind=INCOME)
          0                 if growth_binding = NONE
```

- `ruleStartMonth(r)` is the month bucket in which rule `r` first becomes active (§Base rule:
  the owner-local month of `start_date`; for an `ADD_RULE` rule it is the event's `at_month`,
  see below). A freshly added rule therefore starts at `base(r)` in its first month and steps
  up annually from *its own* start — never pre-inflated by the projection anchor.
- **Cumulative, freeze-not-reset:** raises are accumulated as the product above. A
  mid-projection `OVERRIDE_ASSUMPTION` that changes the bound rate (e.g. startup sets
  `salary_growth_bps → 0` at month M) applies **from month M forward only** — `g(r, m)` for
  years already completed keeps its historical rate, so the already-banked raises are
  **frozen at the current level**, not discarded back to `base(r)`. Implementation carries a
  per-rule running `amount(r, t)` accumulator across months rather than re-evaluating a single
  closed form, making the step-up path-dependent and correct under overrides.

**`due(r, t)` — occurrence COUNT per month bucket** (not a 0/1 boolean). It is the number of
times rule `r`'s cadence fires inside owner-local month `t`, respecting `interval_count`,
`start_date`/`ruleStartMonth`, and `end_date`:

- `MONTHLY`, `QUARTERLY`, `YEARLY` (interval ≥ 1 month): `due ∈ {0, 1}` — fires in the months
  the cadence lands on, else 0. (A `QUARTERLY` rule contributes its full amount once, in its
  quarter months.)
- **Sub-monthly cadences** (`WEEKLY`, biweekly via `interval_count=2` weeks): `due(r, t)` is
  the **integer count of occurrences that fall within month `t`** — typically 4 or 5 for
  WEEKLY (long-run ≈ 4.33/mo) and 2 or 3 for biweekly (≈ 2.17/mo), computed from the actual
  owner-local calendar dates so a biweekly salary contributes ~26 pays/year, not 12. The
  recurrence multiplies `due(r, t) · amount(r, t)`, so a count > 1 fires the amount that many
  times. Growth step-up (`amount(r, t)`) applies per-bucket, uniformly to all occurrences in
  that bucket.
- `ONE_OFF` rules: `due = 1` in exactly their start bucket, else 0.

Alternatively a rule may be created with a **monthly-equivalent amount** (WEEKLY forbidden at
rule creation) — but if sub-monthly cadences are stored, `due` MUST use the count semantics
above. The engine supports the count form; it is the default.

**Scenario application:** before evaluating month `t`, apply every `scenario_event` with
`at_month = t` in `sequence_number` order — disabling rules, mutating amounts, adding
rules, injecting one-offs, or overriding an assumption field from that month forward. This
makes each scenario a pure function of `(t0 balances, ruleset, assumption_set, events)`.

**Rounding:** all growth/compounding math is done in integer paise with banker's rounding
(HALF_EVEN) applied **only** at each month boundary when writing `forecast_point` — the
one documented derivation boundary (per conventions §2). Same inputs → same rounded path.

**Runway (liquidity, cash-asset accounts only):** `runway = first t where Σ_{a ∈ K} B_a(t)
< 0` — the sum over **cash-asset accounts `K`** (`type=ASSET`, non-investment) **only**.
Liability accounts (`L`) and investment accounts (`I`) are **excluded** from the runway sum:
a home loan of ₹30L must not drive runway to 0 while the user holds ₹5L cash. (Whether
investments can be liquidated to extend runway is a separate, explicit mode, not the default.)

**Scenario semantics (worked):**
- *Home purchase* — `ONE_OFF_FLOW` (down payment out of a cash-asset account at month M)
  **plus** `ADD_RULE` (EMI: `EXPENSE`, `MONTHLY`, `ruleStartMonth = M` derived from the
  event's `at_month`, ending at month M+tenure).
- *Reduced income / job loss* — `SET_RULE_AMOUNT` lowers, or `DISABLE_RULE` zeroes, the
  salary rule from month M. Read runway from `K` per the definition above.
- *Startup attempt* — `SET_RULE_AMOUNT` (founder draw) + `OVERRIDE_ASSUMPTION` salary
  growth → 0 (freezes accumulated raises, per the freeze rule) + optional one-off capital
  injection.
- *Retirement / FIRE* — disable income rules and model a **constant REAL withdrawal**:
  the withdrawal per month `t` is `baseMonthlyExpense · (1 + inflation_bps/10000) ^ floor(t/12)`
  (the user's real standard of living, inflated), drawn from the portfolio each month.
  **FLAG RUIN** at the first month where the portfolio (Σ over `I` plus liquid `K`) is less
  than the needed draw — a percentage-of-balance draw can never go negative and so hides ruin;
  a constant real draw surfaces depletion under poor returns, which is the point of the stress
  test. FIRE target = smallest corpus at which the constant real draw is sustained to the
  horizon **without RUIN** under the assumption set.
  - *Dynamic-withdrawal mode (reserved, explicit opt-in):* the `safe_withdrawal_bps ·
    portfolio / 12` **percentage-of-portfolio** draw is retained only as an explicitly labelled
    alternative mode (guardrails / dynamic spending), never the default retirement model. It is
    self-correcting by construction and therefore must not be used to assess ruin.

## Import/manual rule dedup (no double-count)

An import-detected `RecurringPatternDetected` and a pre-existing manual rule can describe the
**same real obligation** (e.g. the user hand-entered "Salary" and import later detects the
same salary). Two `ACTIVE` rules would both satisfy `due(r, t)` and **double-count** the flow.
Idempotency on `origin_pattern_id` only prevents duplicate *candidates on redelivery* — it
does not detect overlap with a manual rule.

**Overlap detection** runs on both candidate insert and `confirmRule`:

- Two rules overlap when, for the same `owner_id`, they share `account_id` **and** an
  equivalent cadence (`cadence` + `interval_count`, normalised to occurrences/month) **and**
  their amounts fall inside a tolerance window (`|a − b| ≤ max(₹ tolerance, x% of a)`) with
  overlapping active date ranges.
- On overlap, the two rules are **merged/replaced, never coexisting**: the import-detected
  candidate is attached to the existing active rule (carrying `origin_pattern_id` and any
  refined amount/cadence for user review) rather than promoted to a second `ACTIVE` rule.
  Confirming resolves to a **single active rule** per obligation. A genuine conflict (amounts
  outside tolerance) is surfaced to the user to reconcile, not silently added.
- This is the forecasting enforcement of the shared **recurring-obligation registry** seam
  (conventions §12, reserved seam #3): one obligation ⇒ one active rule, so `import` detecting,
  `forecasting` projecting, and `budgeting` reserving all reference the same canonical flow.

## Core operations (`ForecastingApi`)

- **`upsertRecurringRule(cmd)`** — create/edit a rule (owner-scoped, validated cadence &
  account). Import-detected rules arrive as `status=CANDIDATE`; `confirmRule(id)` promotes
  to `ACTIVE`. Both candidate insert and `confirmRule` run the **overlap check** (§"Import/
  manual rule dedup") and merge/replace rather than create a coexisting duplicate. Mutating
  any rule invalidates dependent cached runs.
- **`upsertAssumptionSet(cmd)`** — never edits in place; writes `version+1`. Exactly one
  `is_default` per owner.
- **`createScenario(cmd)` / `addScenarioEvent(scenarioId, event)`** — build a stored
  scenario from a base assumption set + ordered events.
- **`project(scenarioId?, horizonMonths?, asOf?)`** — the engine. Seeds `t0` balances
  **only** from the strongly-consistent `LedgerApi.balances(t0)` (conventions §8) — **never**
  from the eventually-consistent analytics net-worth projection, which can lag the ledger and
  memoize a stale balance under a fresh fingerprint. Runs the recurrence, returns a
  `ForecastResult { points[], summary }`. Pure/deterministic; no ledger writes.
- **`getOrComputeRun(scenarioId)`** — returns a fresh `forecast_run` from cache if its
  `input_fingerprint` matches; else recomputes and memoizes. Never returns stale numbers.
- **`compareScenarios(ids[])`** — projects several scenarios over a shared horizon for
  side-by-side deltas (net worth, runway, FIRE date).
- **`invalidate(ownerId)`** — marks runs `is_stale` when the ledger moves (event consumer).

## Caching & determinism

`input_fingerprint = SHA-256(` canonical JSON of `{ engineVersion, t0, latest ledger txn id
at t0, sorted active rule set + versions, assumption_set version, scenario version + event
set, horizon, granularity }` `)`. **`engineVersion`** identifies the recurrence/rounding
algorithm; bumping it on any engine or rounding change invalidates every prior cached run so a
redeploy can never return old numbers under a matching fingerprint (a hit is provably the same
computation *by the same code*). Combined with seeding `t0` only from `LedgerApi.balances(t0)`
(never the lagging projection), a cache hit is provably identical to a fresh correct
computation. Cache is derived state (conventions §8): drop `forecast_run`/`forecast_point`,
recompute, and the numbers must be identical — a tested property.

## Events consumed

Forecasting is a pure read/consumer context — it **emits nothing to the ledger** (it may
emit an internal `ForecastRecomputed` for UI cache-busting only).

| Event | Source | Reaction |
|---|---|---|
| `TransactionPosted` | ledger | new `t0` reality → mark owner's runs `is_stale`; next `project` reseeds balances |
| `TransactionReversed` | ledger | same as above (baseline moved) |
| `RecurringPatternDetected` | import | run the overlap check (§"Import/manual rule dedup"); if it matches an existing active rule, attach to that rule for review rather than adding a second; else insert a `recurring_rule` with `status=CANDIDATE`, `source=IMPORT_DETECTED`, `origin_pattern_id` set — awaits user confirm |
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
- **Growth math:** step-up applies once per completed year of **rule age**; monthly-factor
  compounding of an investment account matches closed-form `B0·(1+r)^(t/12)` within one paise
  (rounding). A rule with `start_date > t0` (or an `ADD_RULE` at month M) starts at `base(r)`
  in its first active month — no anchor-driven pre-inflation.
- **Sub-monthly cadence count:** a WEEKLY rule contributes 4 or 5 occurrences per month bucket
  (long-run ≈ 4.33/mo); a biweekly (`interval_count=2` weeks) salary contributes ~26 pays/year
  — not 12. Assert `Σ due(r,t)` over a year ≈ 52 (weekly) / 26 (biweekly).
- **Cash yield applied:** a cash-asset account with no flows and `cash_yield_bps=300` grows at
  ~3% p.a. — ₹20L → ~₹26.9L over 10y (within rounding); with `cash_yield_bps=0` it stays flat.
- **Override freezes, not resets:** an `OVERRIDE_ASSUMPTION salary_growth_bps→0` at month M
  freezes the salary at its month-M level (accumulated raises retained), never drops it back to
  `base(r)`.
- **FIRE ruin surfaces:** a constant-real-withdrawal retirement scenario with returns below the
  draw FLAGS RUIN at corpus depletion (the percentage-of-portfolio dynamic mode, by contrast,
  never goes negative — so it is not used for ruin assessment).
- **Rule dedup:** a manual salary rule + a `RecurringPatternDetected` for the same
  account/cadence/amount resolves to a **single** active rule; the projection counts the salary
  once, not twice.
- **Scenario correctness:** job-loss `DISABLE_RULE` zeroes the salary flow from month M;
  home-purchase one-off + EMI reproduce expected dip-then-drain.
- **Runway excludes liabilities:** an owner with ₹5L cash + a ₹30L loan liability reports
  runway over cash-asset accounts `K` only (many months), never 0 at t0.
- **Fingerprint covers engine:** two runs with identical inputs but different `engineVersion`
  produce distinct fingerprints; bumping `engineVersion` forces recompute on next fetch.
- **Cache invalidation:** a `TransactionPosted` for the owner marks runs stale and the
  next `project` reseeds `t0` from the new ledger balance.
- **Import handoff:** `RecurringPatternDetected` creates exactly one `CANDIDATE` rule;
  redelivery is a no-op (idempotent on `origin_pattern_id`).
- **API contract tests:** each endpoint — happy path + validation (422) + stable error
  `code` (e.g. `FORECAST_INVALID_HORIZON`, `FORECAST_UNKNOWN_ASSUMPTION_SET`).
- **Never-truth guard:** no forecasting code path writes to `ledger_transaction` /
  `posting`; forecasting owns only its own tables.

## Audit fixes applied

**Amended 2026-07-23** — remediation of the adversarial design review
(`../2026-07-23-audit-and-opportunities.md`, forecasting findings). Mapping:

| Audit finding (severity) | Fix in this doc |
|---|---|
| Sub-monthly cadence collapses to one hit/month (HIGH) | `due(r,t)` redefined as an **occurrence COUNT** per owner-local month bucket; WEEKLY ≈ 4.33/mo, biweekly ≈ 2.17/mo; count semantics documented in §engine; cadence schema note + count test added. |
| `cash_yield_bps` never applied (HIGH) | New **cash-asset partition `K`** accrues `· monthlyFactor(cash_yield_bps)` in the recurrence; schema comment updated; "cash grows at seeded yield" test added. |
| Runway sums liabilities → runway 0 (HIGH) | Runway defined over **cash-asset accounts `K` only** (`type=ASSET`, non-investment); liabilities (`L`) and investments (`I`) excluded; partition `C` no longer reused for liquidity; explicit account-set stated + test. |
| Growth step-up keyed to absolute month (HIGH) | Step-up keyed to **rule age `floor((t − ruleStartMonth)/12)`**, tracked **cumulatively per rule**; `ADD_RULE` rules start at `base(r)`; mid-projection `OVERRIDE_ASSUMPTION` **freezes** (not resets) accumulated raises; tests added. |
| Import-detected + manual rule double-count (HIGH) | New §"Import/manual rule dedup": overlap check (account + cadence + amount window) on candidate insert **and** `confirmRule`; merge/replace → single active rule; ties into the recurring-obligation registry seam (conventions §12); dedup test added. |
| FIRE withdrawal %-of-portfolio hides ruin (HIGH) | Retirement modelled as a **constant REAL withdrawal** (base expense × inflation) drawn from the portfolio; **FLAG RUIN** when portfolio < needed draw; %-of-portfolio reserved for an explicit **dynamic-withdrawal mode**; `safe_withdrawal_bps` comment updated; ruin test added. |
| `ADD_RULE` at_month vs body dates (MEDIUM) | `ADD_RULE` derives `ruleStartMonth` from `at_month` (t0+M), **overriding** body dates; DATE→month-bucket mapping pinned as owner-local (conventions §3). |
| Fingerprint omits engine version; lagging seed (MEDIUM) | `engineVersion` added to `input_fingerprint`; `t0` balances seeded **only** from strongly-consistent `LedgerApi.balances(t0)` (conventions §8), never the eventually-consistent projection; test added. |
