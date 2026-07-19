# 09 — Optimization Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release Phase 6. The
decision-**augmentation** layer: it surfaces explainable, data-grounded insights about
where the owner's own money is working against them. It never invents advice — every
insight is a rule fired on the owner's real numbers, with those numbers attached.

## Responsibilities

- A unified **insight / recommendation** model: typed records with a severity, an
  explainable rationale (the exact numbers that triggered the rule), a suggested action,
  and a lifecycle (`OPEN → ACKNOWLEDGED | DISMISSED | RESOLVED`).
- A **detector framework**: each detector is a named rule that reads projections from
  other contexts **via their public APIs only** (Analytics, Budgeting, Investment, Ledger)
  — never their tables — on a schedule and/or on relevant outbox events, and emits/updates
  insights **idempotently** (re-running must not create a duplicate `OPEN` insight for the
  same subject).
- Six detectors: idle cash, subscription leakage, liquidity risk, portfolio concentration,
  debt optimization, emergency reserve.
- Owns **only** its own insight tables. Reads everything else through context APIs.
- Emits `InsightRaised` / `InsightResolved` to the outbox for downstream (notification /
  UI) consumers. **Delivery and UI are out of scope** — Optimization produces records.

**Non-goals:** generic financial advice, product recommendations, tax advice, market
calls. If a number to justify an insight cannot be pulled from the owner's own data, the
insight is not raised. INR only (Phase 6 keeps the single-currency assumption).

## Schema

All money is `BIGINT` minor units (paise) + `currency CHAR(3)`. All PKs are app-side
UUID v7. All aggregate roots carry `owner_id`. Detector reads are external; only these
tables are owned here.

```sql
-- One row per active detector rule. Config lives in DB so thresholds are tunable
-- without a redeploy. Seeded by Flyway; editable per owner (owner_id NULL = default).
CREATE TABLE detector_config (
    id            UUID PRIMARY KEY,               -- v7
    owner_id      UUID,                           -- NULL = system default row
    detector_key  TEXT NOT NULL,                  -- IDLE_CASH|SUB_LEAKAGE|LIQUIDITY_RISK|
                                                  -- CONCENTRATION|DEBT_OPT|EMERGENCY_RESERVE
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    params        JSONB NOT NULL,                 -- tunable thresholds (see each detector)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, detector_key)               -- one row per owner per detector
);

-- The unified insight record.
CREATE TABLE insight (
    id             UUID PRIMARY KEY,              -- v7 = time order
    owner_id       UUID NOT NULL,
    detector_key   TEXT NOT NULL,                 -- which rule produced it
    subject_type   TEXT NOT NULL,                 -- ACCOUNT|HOLDING|SUBSCRIPTION|PORTFOLIO|
                                                  -- LIABILITY|OWNER  (what the insight is about)
    subject_id     UUID,                          -- FK-by-value into the owning context
                                                  -- (NULL for owner-level, e.g. reserve)
    dedup_key      TEXT NOT NULL,                 -- stable key: detector_key + subject (see below)
    severity       TEXT NOT NULL,                 -- INFO|LOW|MEDIUM|HIGH|CRITICAL
    status         TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN|ACKNOWLEDGED|DISMISSED|RESOLVED
    title          TEXT NOT NULL,                 -- short human summary, filled from data
    rationale      JSONB NOT NULL,                -- the numbers that triggered it (explainability)
    suggested_action JSONB NOT NULL,              -- {action, params} — descriptive, not executable
    estimated_impact_minor BIGINT,                -- optional ₹ upside/risk, minor units
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    detected_at    TIMESTAMPTZ NOT NULL,          -- business time of the reading
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(), -- bumped each re-detection while OPEN
    acknowledged_at TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    dismiss_reason  TEXT,
    resolved_at     TIMESTAMPTZ,
    snooze_until    TIMESTAMPTZ,                  -- suppress re-raise until this instant
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The idempotency spine: at most ONE non-terminal (OPEN/ACKNOWLEDGED) insight per
-- (owner, dedup_key). Terminal rows (DISMISSED/RESOLVED) don't block; a partial unique
-- index lets history accumulate while guaranteeing no duplicate live insight.
CREATE UNIQUE INDEX insight_live_dedup_idx
    ON insight (owner_id, dedup_key)
    WHERE status IN ('OPEN', 'ACKNOWLEDGED');
CREATE INDEX insight_owner_status_idx ON insight (owner_id, status, severity);
CREATE INDEX insight_detector_idx     ON insight (owner_id, detector_key, status);

-- Append-only audit of every lifecycle transition (open, ack, dismiss, resolve, re-seen).
-- No UPDATE/DELETE in app code (append-only per conventions §5).
CREATE TABLE insight_event (
    id          UUID PRIMARY KEY,                 -- v7
    insight_id  UUID NOT NULL REFERENCES insight(id),
    owner_id    UUID NOT NULL,
    event_type  TEXT NOT NULL,                    -- RAISED|RESEEN|ACKNOWLEDGED|DISMISSED|
                                                  -- RESOLVED|SEVERITY_CHANGED|SNOOZED
    from_status TEXT,
    to_status   TEXT,
    detail      JSONB,                            -- e.g. rationale snapshot at transition
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX insight_event_insight_idx ON insight_event (insight_id, occurred_at);

-- Bookkeeping per detector run, so runs are observable and re-entrant-safe.
CREATE TABLE detector_run (
    id            UUID PRIMARY KEY,               -- v7
    owner_id      UUID NOT NULL,
    detector_key  TEXT NOT NULL,
    trigger       TEXT NOT NULL,                  -- SCHEDULE|EVENT|MANUAL
    trigger_ref   TEXT,                           -- event_id or cron token
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'RUNNING',-- RUNNING|OK|FAILED
    raised        INT NOT NULL DEFAULT 0,
    reseen        INT NOT NULL DEFAULT 0,
    resolved      INT NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX detector_run_owner_idx ON detector_run (owner_id, detector_key, started_at);
```

**`dedup_key` construction (the idempotency contract):** `detector_key` + the stable
identity of what the insight is about — *not* the triggering numbers. So a re-run finds
the existing live insight and updates it in place instead of creating a second one.
Examples: `IDLE_CASH:{accountId}`, `SUB_LEAKAGE:{subscriptionId}`,
`CONCENTRATION:{holdingId}`, `DEBT_OPT:{liabilityId}`, `LIQUIDITY_RISK:{ownerId}`,
`EMERGENCY_RESERVE:{ownerId}`. The partial unique index makes a duplicate live insight
impossible at the DB level, backing the domain rule.

**Rationale is self-contained.** `rationale` stores the actual figures read at detection
time (balances, ratios, thresholds used) so an insight is explainable forever, even if the
underlying projection later changes. Nothing in `rationale` requires re-querying another
context to render.

## Detector framework

A **`Detector`** is `{ String key(); Set<Class<? extends Event>> triggers(); DetectorResult
run(DetectorContext ctx); }`. The framework guarantees idempotent upsert; a detector only
decides *whether* a subject currently breaches and *with what numbers*.

- **Inputs — read via context APIs only.** `DetectorContext` exposes typed read ports:
  `AnalyticsApi` (liquid balance, monthly burn, runway, cash-flow), `BudgetingApi`
  (recurring commitments), `InvestmentApi` (holdings, portfolio value, allocation),
  `LedgerApi` (account balances, liability balances, interest metadata). No detector holds
  a repository or entity from another context — only its API interface (conventions §11).
- **Triggers.**
  - *Scheduled:* a `@Scheduled` `DetectorScheduler` (default daily, `optimization.schedule.cron`)
    runs every enabled detector for every owner.
  - *Event-driven:* an `EventConsumer` (`name = "optimization"`, dedup via platform
    `processed_event`) maps relevant outbox events to the detectors that care, and runs
    just those for just the affected owner/subject. Mapping:
    - `TransactionPosted` / `TransactionReversed` → idle cash, liquidity risk, emergency reserve, debt optimization.
    - `HoldingValued` / `HoldingBought` / `HoldingSold` (Investment) → concentration, debt optimization.
    - `RecurringSeriesDetected` / `RecurringChargePosted` (Import/Budgeting) → subscription leakage.
    - `BudgetPeriodClosed` (Budgeting) → emergency reserve, liquidity risk.
- **Idempotent upsert (the core algorithm),** per subject, in one DB transaction:
  1. Compute `breach?` + `rationale` + `severity` from the read ports.
  2. `SELECT ... FOR UPDATE` the live insight for `(owner_id, dedup_key)`.
  3. **breach & no live row** → `INSERT` insight `OPEN`; append `insight_event RAISED`;
     `OutboxWriter.append(InsightRaised)`. (`snooze_until` in the future ⇒ skip raise.)
  4. **breach & live row exists** → bump `last_seen_at`, refresh `rationale`/`title`/
     `estimated_impact`; if computed severity differs, update it + append `SEVERITY_CHANGED`;
     append `RESEEN`. **No new row** (the partial unique index would reject it anyway).
  5. **no breach & live row exists** → transition to `RESOLVED` (`resolved_at=now`),
     append `RESOLVED`, `OutboxWriter.append(InsightResolved)`. Auto-resolution closes the
     loop when the owner fixes the situation.
  6. **no breach & no live row** → no-op.
  A `DISMISSED` insight stays terminal; a fresh breach after dismissal may raise a new
  insight only once `snooze_until` (set from the dismissal) has passed — so "not now"
  is respected without silencing forever.
- **Determinism / explainability.** Given the same projection readings and the same
  `detector_config.params`, a detector always produces the same breach decision, severity,
  and rationale. Detectors are pure functions of (readings, params); the framework owns all
  persistence and event emission.

## The six detectors

Every threshold below is a **tunable assumption** stored in `detector_config.params`
(JSONB), overridable per owner. Defaults are the seed values; none are advice.

1. **Idle cash — `IDLE_CASH`** (subject: `ACCOUNT`).
   *Rule:* a liquid account whose balance exceeds `idleMultiple` × monthly burn **and**
   whose account yield is below `minYieldPct`, where the excess above the buffer is "idle".
   *Reads:* `AnalyticsApi.liquidBalance(accountId)`, `AnalyticsApi.monthlyBurn()`,
   account interest metadata from `LedgerApi`.
   *Breach:* `balance > idleMultiple × monthlyBurn` **and** `accountYieldPct < minYieldPct`.
   *Rationale:* `{balanceMinor, monthlyBurnMinor, idleMultiple, thresholdMinor, excessMinor,
   accountYieldPct, minYieldPct}`. *Impact:* `excessMinor × (benchmarkYieldPct −
   accountYieldPct)` annualized. *Suggested action:* move `excessMinor` to a
   higher-yield liquid instrument.
   *Tunable defaults:* `idleMultiple = 3` (× monthly burn kept liquid), `minYieldPct = 4.0`,
   `benchmarkYieldPct = 7.0`, `minExcessMinor = 5000000` (₹50,000 floor to avoid noise).

2. **Subscription leakage — `SUB_LEAKAGE`** (subject: `SUBSCRIPTION`, i.e. a recurring
   debit series).
   *Rule:* a recurring debit flagged **unused** (no corresponding usage/benefit signal for
   `unusedDays`) **or duplicate** (two active recurring series to the same normalized
   merchant / overlapping category).
   *Reads:* `BudgetingApi.recurringCommitments()` / `ImportApi`-derived recurring series
   (merchant-normalized), last-charge dates.
   *Breach:* series still charging **and** (`daysSinceMeaningfulActivity > unusedDays`) **or**
   (`duplicateOfSeriesId != null`).
   *Rationale:* `{merchant, cadence, amountMinor, lastChargedAt, chargesInWindow,
   annualizedMinor, reason: UNUSED|DUPLICATE, duplicateOfSeriesId?}`. *Impact:*
   `annualizedMinor` (recoverable if cancelled). *Suggested action:* review/cancel the
   subscription (descriptive only).
   *Tunable defaults:* `unusedDays = 90`, `minMonthlyMinor = 10000` (₹100 floor),
   `duplicateWindowDays = 35`.

3. **Liquidity risk — `LIQUIDITY_RISK`** (subject: `OWNER`).
   *Rule:* projected cash runway falls below `minRunwayMonths`.
   *Reads:* `AnalyticsApi.runwayMonths()` (liquid assets ÷ monthly burn), or
   `liquidAssets / monthlyBurn` composed from `AnalyticsApi.liquidBalance()` +
   `monthlyBurn()`.
   *Breach:* `runwayMonths < minRunwayMonths`.
   *Rationale:* `{liquidAssetsMinor, monthlyBurnMinor, runwayMonths, minRunwayMonths,
   shortfallMonths}`. *Severity ladder (tunable):* `< criticalRunwayMonths` → CRITICAL,
   else HIGH. *Suggested action:* rebuild liquid buffer / reduce burn by `shortfall`.
   *Tunable defaults:* `minRunwayMonths = 3`, `criticalRunwayMonths = 1`.

4. **Portfolio concentration — `CONCENTRATION`** (subject: `HOLDING`).
   *Rule:* a single holding exceeds `maxSingleHoldingPct` of total portfolio value.
   *Reads:* `InvestmentApi.portfolioValue()`, `InvestmentApi.holdings()` (per-holding
   market value).
   *Breach:* `holdingValue / portfolioValue > maxSingleHoldingPct`.
   *Rationale:* `{holdingLabel, holdingValueMinor, portfolioValueMinor, concentrationPct,
   maxSingleHoldingPct, excessValueMinor}`. *Severity (tunable):* `> criticalPct` → HIGH,
   else MEDIUM. *Suggested action:* consider trimming toward the target weight (descriptive).
   *Tunable defaults:* `maxSingleHoldingPct = 20.0`, `criticalPct = 40.0`,
   `minPortfolioMinor = 10000000` (₹1,00,000 floor so a tiny portfolio isn't flagged).

5. **Debt optimization — `DEBT_OPT`** (subject: `LIABILITY`).
   *Rule:* the owner carries a liability whose interest rate exceeds `minDebtRatePct`
   **while** idle liquid cash (above the emergency buffer) sits earning less — paying down
   the debt beats holding the cash.
   *Reads:* `LedgerApi` liability balance + rate metadata, `AnalyticsApi.liquidBalance()`,
   `AnalyticsApi.monthlyBurn()` (to size the protected buffer).
   *Breach:* `liabilityRatePct > minDebtRatePct` **and** `idleCashAboveBuffer > 0` **and**
   `liabilityRatePct − idleCashYieldPct > rateGapPct`.
   *Rationale:* `{liabilityLabel, balanceMinor, liabilityRatePct, idleCashMinor,
   protectedBufferMinor, deployableMinor, idleCashYieldPct, rateGapPct, annualInterestSavedMinor}`.
   *Impact:* `min(deployableMinor, balanceMinor) × (liabilityRatePct − idleCashYieldPct)`
   annualized. *Suggested action:* deploy `deployableMinor` toward the higher-rate debt,
   preserving the emergency buffer. *Tunable defaults:* `minDebtRatePct = 10.0`,
   `rateGapPct = 3.0`, `bufferMultiple = 6` (× monthly burn kept before any prepay).

6. **Emergency reserve — `EMERGENCY_RESERVE`** (subject: `OWNER`).
   *Rule:* the liquid emergency buffer is below `targetMonths` of essential expenses.
   *Reads:* `AnalyticsApi.liquidBalance()`, `BudgetingApi.essentialMonthlyExpense()` (falls
   back to `AnalyticsApi.monthlyBurn()` if budgeting isn't configured).
   *Breach:* `liquidBuffer < targetMonths × essentialMonthlyExpense`.
   *Rationale:* `{liquidBufferMinor, essentialMonthlyExpenseMinor, targetMonths,
   targetReserveMinor, shortfallMinor, currentMonthsCovered}`. *Severity (tunable):*
   `currentMonthsCovered < 1` → HIGH, else MEDIUM. *Suggested action:* build the reserve by
   `shortfallMinor` before other deployments. *Tunable defaults:* `targetMonths = 6`,
   `minReserveMonths = 3` (below this → HIGH).

## Core operations (`OptimizationApi`)

Read-and-lifecycle API. Detection itself is driven by schedule/events, not by callers.

- **`listInsights(filter, cursor, limit)`** — owner-scoped, cursor paginated (v7 id).
  Filter by `status`, `severity`, `detectorKey`, `subjectType`. Default excludes terminal.
- **`getInsight(insightId)`** — full record incl. `rationale`, `suggestedAction`,
  and the `insight_event` timeline.
- **`acknowledgeInsight(insightId)`** — `OPEN → ACKNOWLEDGED`; appends `ACKNOWLEDGED`.
  Idempotent (already-acknowledged is a no-op).
- **`dismissInsight(insightId, reason, snoozeFor?)`** — `OPEN|ACKNOWLEDGED → DISMISSED`;
  sets `snooze_until = now + snoozeFor` (default `optimization.dismiss.snoozeDays = 30`);
  appends `DISMISSED`. A dismissed insight will not re-raise until the snooze passes.
- **`resolveInsight(insightId)`** — manual `→ RESOLVED` (the detector also auto-resolves);
  appends `RESOLVED`. Idempotent.
- **`runDetector(detectorKey)`** / **`runAllDetectors()`** — manual re-scan for the current
  owner (`trigger=MANUAL`); returns a `DetectorRunSummary` `{raised, reseen, resolved}`.
  Same idempotent upsert — never double-raises.
- **`getDetectorConfig()`** / **`updateDetectorConfig(detectorKey, params, enabled)`** —
  read/tune thresholds per owner; writes `detector_config` (owner-scoped override row).

All financial state changes are owner-scoped; lifecycle transitions append to
`insight_event` in the same DB transaction as the `insight` update.

## Events consumed

Optimization is a pure consumer of other contexts' outbox events (it stores no truth). Via
the platform `EventConsumer` (`name = "optimization"`), redelivery deduped on
`(consumer_name, event_id)`:

| Event | Source | Detectors re-run |
|---|---|---|
| `TransactionPosted` / `TransactionReversed` | Ledger | idle cash, liquidity risk, emergency reserve, debt optimization |
| `HoldingValued` / `HoldingBought` / `HoldingSold` | Investment | concentration, debt optimization |
| `RecurringSeriesDetected` / `RecurringChargePosted` | Import / Budgeting | subscription leakage |
| `BudgetPeriodClosed` | Budgeting | emergency reserve, liquidity risk |

Handling is **idempotent** at two layers: platform `processed_event` (won't process the
same event twice) **and** the detector upsert (won't create a duplicate live insight even
if it does re-run). Consumers tolerate unknown payload fields and `schemaVersion` bumps.

## Events emitted

- `InsightRaised` — `{insightId, detectorKey, subjectType, subjectId, severity, title,
  rationale, estimatedImpactMinor, currency, detectedAt}`.
- `InsightResolved` — `{insightId, detectorKey, subjectId, resolvedAt, resolution:
  AUTO|MANUAL}`.

Consumed by a downstream notification/UI context (out of scope here). Written to the
outbox in the same DB transaction as the insight change (transactional outbox, §6).

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/insights` | list (owner-scoped, filter by status/severity/detector, cursor) |
| GET | `/api/v1/insights/{id}` | full insight incl. rationale + event timeline |
| POST | `/api/v1/insights/{id}/acknowledge` | OPEN → ACKNOWLEDGED |
| POST | `/api/v1/insights/{id}/dismiss` | dismiss + optional snooze (Idempotency-Key) |
| POST | `/api/v1/insights/{id}/resolve` | manual resolve |
| POST | `/api/v1/detectors/{key}/run` | manual re-scan one detector (Idempotency-Key) |
| POST | `/api/v1/detectors/run` | manual re-scan all detectors (Idempotency-Key) |
| GET | `/api/v1/detectors/config` | current thresholds (defaults + owner overrides) |
| PUT | `/api/v1/detectors/{key}/config` | tune thresholds / enable-disable |

Errors use RFC 9457 problem+json with a stable `code` enum, e.g.
`OPTIMIZATION_INSIGHT_NOT_FOUND`, `OPTIMIZATION_INVALID_TRANSITION` (e.g. resolve on a
dismissed insight), `OPTIMIZATION_UNKNOWN_DETECTOR`, `OPTIMIZATION_INVALID_THRESHOLD`.
Money in JSON is `{amountMinor, currency}`; times ISO-8601 UTC.

## Testing

- **Idempotency (the headline invariant):** running any detector twice on the same
  readings raises exactly one `OPEN` insight; the second run is a `RESEEN`, never a
  duplicate. Asserted at the DB level too (partial unique index rejects a second live row).
- **Lifecycle:** `OPEN → ACKNOWLEDGED → RESOLVED`, `OPEN → DISMISSED (+snooze)`, and
  auto-resolve when the breach clears; every transition appends exactly one `insight_event`;
  invalid transitions return `OPTIMIZATION_INVALID_TRANSITION`.
- **Snooze:** a dismissed-with-snooze insight does not re-raise before `snooze_until`; it
  may raise a fresh insight after it passes.
- **Per-detector breach/no-breach property tests:** for each of the six, generate readings
  straddling the configured threshold and assert breach exactly on the correct side, with
  `rationale` carrying the exact triggering numbers and `severity` matching the ladder.
- **Explainability:** every raised insight's `rationale` is self-contained — rendering it
  requires no further calls into other contexts.
- **Boundary isolation:** detectors depend only on context **API interfaces**; a test wiring
  fake `AnalyticsApi`/`InvestmentApi`/`BudgetingApi`/`LedgerApi` drives every detector with
  no other context's persistence present (proves no reach into internals).
- **Event consumption:** redelivery of the same outbox event is a no-op (platform
  `processed_event`) and never produces a second insight (detector upsert).
- **Tunability:** overriding `detector_config.params` changes the breach boundary with no
  code change; a disabled detector raises nothing.
- **API contract tests:** each endpoint — happy path, validation (422), and error `code`.
- **Persistence:** integration tests on real Postgres (Testcontainers); owner scoping —
  one owner never reads another's insights.
