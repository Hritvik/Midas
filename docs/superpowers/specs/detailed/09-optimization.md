# 09 — Optimization Context (Detailed Design)

**Amended 2026-07-23** (audit remediation, Tier 0/1 — see "Audit fixes applied" below).

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release Phase 6. The
decision-**augmentation** layer: it surfaces explainable, data-grounded insights about
where the owner's own money is working against them. It never invents advice — every
insight is a rule fired on the owner's real numbers, with those numbers attached.

## Responsibilities

- A unified **insight / recommendation** model: typed records with a severity, an
  explainable rationale (the exact numbers that triggered the rule), a suggested action,
  and a lifecycle (`OPEN → ACKNOWLEDGED | DISMISSED | ACCEPTED | RESOLVED`). `ACCEPTED` is
  a terminal "this is intentional, stop flagging it unless it materially worsens" state
  (see detector 4 and `acceptInsight`).
- A **detector framework**: each detector is a named rule that reads projections from
  other contexts **via their public APIs only** (Analytics, Budgeting, Investment, Ledger)
  — never their tables — on a schedule and/or on relevant outbox events, and emits/updates
  insights **idempotently** (re-running must not create a duplicate `OPEN` insight for the
  same subject).
- Six detectors: idle cash, subscription leakage, liquidity risk, portfolio concentration,
  debt optimization, emergency reserve.
- Owns **only** its own insight tables. Reads everything else through context APIs.
- Emits `InsightRaised` / `InsightResolved` to the outbox. **Delivery flows to the
  `notification/` context** (conventions §12) — the sole consumer that ranks, dedups,
  digests, and channels insight events (quiet hours, etc.). Optimization produces records;
  notification delivers them. UI rendering remains downstream.

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
    status         TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN|ACKNOWLEDGED|DISMISSED|ACCEPTED|RESOLVED
                                                  -- ACCEPTED = terminal, intentional; re-raise
                                                  -- only if subject materially worsens (see below)
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
    accepted_at     TIMESTAMPTZ,                  -- when marked intentional (terminal)
    accepted_at_level NUMERIC,                    -- the breach magnitude at acceptance (e.g.
                                                  -- concentrationPct); re-raise only if the
                                                  -- current level exceeds this by materialDeltaPct
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
                                                  -- ACCEPTED|RESOLVED|SEVERITY_CHANGED|SNOOZED
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
impossible at the DB level, backing the domain rule. `ACCEPTED`/`DISMISSED`/`RESOLVED` are
terminal and do **not** occupy the live slot; re-raise suppression for `ACCEPTED` is enforced
by the upsert algorithm (a most-recent `ACCEPTED` row with `accepted_at_level` gates re-raise —
see below), not by the index.

**Rationale is self-contained.** `rationale` stores the actual figures read at detection
time (balances, ratios, thresholds used) so an insight is explainable forever, even if the
underlying projection later changes. Nothing in `rationale` requires re-querying another
context to render.

## Detector framework

A **`Detector`** is `{ String key(); Set<Class<? extends Event>> triggers(); DetectorResult
run(DetectorContext ctx); }`. The framework guarantees idempotent upsert; a detector only
decides *whether* a subject currently breaches and *with what numbers*.

- **Inputs — read via context APIs only.** `DetectorContext` exposes typed read ports:
  `AnalyticsApi` (liquid balance, **`avgBurn(W)` trailing burn**, `runwayMonths()`, cash-flow),
  `BudgetingApi` (recurring commitments), `InvestmentApi` (holdings, portfolio value,
  allocation), `LedgerApi` (account balances, liability balances, financial-product metadata),
  and an **`ObligationsApi`** read port (new — see below). No detector holds a repository or
  entity from another context — only its API interface (conventions §11).
- **Obligations read port (`ObligationsApi`) — earmarked cash is not deployable.** Backed by
  the reserved **recurring-obligation registry** seam (conventions §12.3) plus
  forecasting's one-time dated obligations, this port answers **"how much liquid cash is
  already committed to a dated, near-term obligation?"** — `committedOutflowsMinor(horizon)`
  returns the sum of committed-but-undrawn / dated obligations (down-payment, advance-tax,
  tuition, insurance premium due) within `horizon`. Every detector that reasons about
  "excess"/"deployable" cash (idle cash, debt-opt) **subtracts committed obligations first**,
  and **never** proposes an irreversible action (debt prepay) from cash earmarked against a
  dated obligation. Until the registry is fully built, the port returns the obligations that
  budgeting envelopes / forecasting can already ground; it never fabricates a zero — an
  absent obligation feed means "no known commitments", but a *present* dated obligation is
  always honoured.
- **Financial-product metadata (yield / rate) — suppress, never default.** Per-account
  `yield_bps` and per-liability `rate_bps` come from the reserved **account
  financial-product metadata** seam (conventions §12.3), read via `LedgerApi`. When the
  number is **absent (null)**, the dependent detector **SUPPRESSES** for that subject
  (raises nothing) — it never coerces null to 0. This upholds "grounded — every number from
  the owner's own data": a missing yield must not fabricate an idle-cash insight, and a
  missing rate must not fabricate (or mis-ground) a debt-opt insight.
- **Trailing-burn standardization.** Every detector that needs a monthly-burn figure reads
  `AnalyticsApi.avgBurn(W)` (W = 3 complete owner-local months, §00 periods) — **never** a
  raw single month and **never** a self-computed division. Runway uses
  `AnalyticsApi.runwayMonths()`. If `avgBurn` has insufficient history or `runwayMonths()`
  is `∞`/null (zero burn), the detector treats it as **"insufficient data → do not raise"**
  (no crash, no false negative masquerading as healthy).
- **Triggers.**
  - *Scheduled:* a `@Scheduled` `DetectorScheduler` (default daily, `optimization.schedule.cron`)
    runs every enabled detector for every owner.
  - *Event-driven (debounced):* an `EventConsumer` (`name = "optimization"`, dedup via
    platform `processed_event`) maps relevant outbox events to the detectors that care, and
    runs just those for just the affected owner/subject. **A `TransactionPosted` does NOT
    synchronously re-run a detector** — high-frequency triggers are **coalesced**: the
    consumer records a pending `(owner, detector)` re-run request and a short debounce window
    (`optimization.debounce.window`, default 15 min) collapses a burst of transactions into a
    single re-run, so a normal day of spending produces at most one re-evaluation per
    detector, not one per transaction. Mapping:
    - `TransactionPosted` / `TransactionReversed` → idle cash, liquidity risk, emergency reserve, debt optimization (debounced/coalesced).
    - `HoldingValued` / `HoldingBought` / `HoldingSold` (Investment) → concentration, debt optimization.
    - `RecurringSeriesDetected` / `RecurringChargePosted` (Import/Budgeting) → subscription leakage.
    - `BudgetPeriodClosed` (Budgeting) → emergency reserve, liquidity risk.
- **Hysteresis (anti-flap).** Breach is a **two-threshold** decision, not one line. A
  detector raises when the metric crosses its *raise* threshold and auto-resolves only when
  it crosses back past a *resolve* threshold separated by a margin `ε`
  (`optimization.hysteresis.epsilon`, default 0.1): e.g. liquidity risk raises at
  `runway < minRunwayMonths` but auto-resolves only at `runway > minRunwayMonths × (1 + ε)`.
  A metric oscillating inside the band keeps the single existing insight (RESEEN), emitting
  no resolve/raise churn. Severity-ladder boundaries use the same margin so severity doesn't
  flap either. Additionally a **minimum-open duration** (`optimization.minOpenDuration`,
  default 24 h) prevents auto-resolve of an insight younger than that window — a same-day
  salary credit can't instantly resolve a genuine breach.
- **Idempotent upsert (the core algorithm),** per subject, in one DB transaction:
  1. Compute `breach?` (with hysteresis vs. current live/terminal state) + `rationale` +
     `severity` from the read ports. If a required grounded input is **absent** (null yield /
     null rate) or burn is **insufficient/∞**, **suppress** — treat as no-breach and skip
     (do not raise, do not resolve a live row into a false "fixed").
  2. `SELECT ... FOR UPDATE` the live insight for `(owner_id, dedup_key)`, and the most-recent
     terminal `ACCEPTED` row (if any) for the same key.
  3. **breach & an `ACCEPTED` row exists** → re-raise **only if** the current breach level
     exceeds `accepted_at_level` by `materialDeltaPct` (materially worse). Otherwise it stays
     accepted — no raise, no nag.
  4. **breach & no live row** → `INSERT ... ON CONFLICT (owner_id, dedup_key) WHERE status IN
     ('OPEN','ACKNOWLEDGED') DO NOTHING`, then if no row was inserted (the first-raise race:
     a concurrent scheduled + event run both reached here), **catch the conflict / 23505,
     re-read the now-existing live row and fall through to the RESEEN branch (step 5)**. On a
     genuine insert: append `insight_event RAISED`; `OutboxWriter.append(InsightRaised)`.
     (`snooze_until` in the future ⇒ skip raise.)
  5. **breach & live row exists** → bump `last_seen_at`, refresh `rationale`/`title`/
     `estimated_impact`; if computed severity differs (beyond the hysteresis margin), update
     it + append `SEVERITY_CHANGED`; append `RESEEN`. **No new row.**
  6. **no breach (past the resolve threshold) & live row exists & older than
     `minOpenDuration`** → transition to `RESOLVED` (`resolved_at=now`), append `RESOLVED`,
     `OutboxWriter.append(InsightResolved)`. Auto-resolution closes the loop when the owner
     fixes the situation.
  7. **no breach & no live row** → no-op.
  A `DISMISSED` insight stays terminal; a fresh breach after dismissal may raise a new
  insight only once `snooze_until` (set from the dismissal) has passed — so "not now"
  is respected without silencing forever. An `ACCEPTED` insight is terminal and suppresses
  re-raise indefinitely until the subject materially worsens (step 3).
- **Dangling-subject resolution (scheduled full run only).** The scheduled `DetectorScheduler`
  run, after evaluating each detector, **resolves any live insight whose subject no longer
  appears in the source context's current API listing** (e.g. a `SUB_LEAKAGE` subject whose
  recurring series was merged/deleted, so no triggering event ever fires for the dead id).
  These transition to `RESOLVED` with resolution `AUTO` so an insight never sits OPEN
  pointing at a phantom subject. (Event-driven runs skip this sweep — it needs the full
  current-subject set.)
- **Determinism / explainability.** Given the same projection readings and the same
  `detector_config.params`, a detector always produces the same breach decision, severity,
  and rationale. Detectors are pure functions of (readings, params); the framework owns all
  persistence and event emission.

## The six detectors

Every threshold below is a **tunable assumption** stored in `detector_config.params`
(JSONB), overridable per owner. Defaults are the seed values; none are advice.

1. **Idle cash — `IDLE_CASH`** (subject: `ACCOUNT`).
   *Rule:* a liquid account whose balance exceeds the protected buffer **and** whose account
   yield is below `minYieldPct`, where the excess above the buffer **minus committed
   obligations** is "idle".
   *Reads:* `AnalyticsApi.liquidBalance(accountId)`, `AnalyticsApi.avgBurn(W)`,
   `LedgerApi` account `yield_bps` (financial-product metadata seam),
   `ObligationsApi.committedOutflowsMinor(horizon)`.
   *Buffer reconciliation:* the protected buffer uses the **shared `bufferMultiple`** (see
   below), which is **≥ `EMERGENCY_RESERVE.targetMonths`** — so cash needed for the emergency
   reserve is **never** labelled idle. The idle-cash and reserve detectors can no longer give
   opposite advice on the same rupees.
   *Breach:* `yield_bps` present (else **suppress** — never default to 0) **and**
   `accountYieldPct < minYieldPct` **and**
   `excessMinor = balance − bufferMultiple × avgBurn − committedOutflowsMinor > minExcessMinor`.
   *Rationale:* `{balanceMinor, avgBurnMinor, bufferMultiple, bufferMinor,
   committedOutflowsMinor, thresholdMinor, excessMinor, accountYieldPct, minYieldPct}`.
   *Impact:* `excessMinor × (benchmarkYieldPct − accountYieldPct)` annualized. *Suggested
   action:* move `excessMinor` to a higher-yield liquid instrument.
   *Tunable defaults:* `bufferMultiple = 6` (shared with debt-opt and ≥ reserve target;
   × trailing burn kept liquid), `minYieldPct = 4.0`, `benchmarkYieldPct = 7.0`,
   `minExcessMinor = 5000000` (₹50,000 floor to avoid noise).

2. **Subscription leakage — `SUB_LEAKAGE`** (subject: `SUBSCRIPTION`, i.e. a recurring
   debit series).
   *Rule:* a recurring debit flagged **duplicate** (two active recurring series to the same
   normalized merchant / overlapping category). **`DUPLICATE` is the only reason raised** —
   it is groundable purely from charge data. **`UNUSED` is suppressed:** Midas observes
   *charges*, not benefit/usage, so "days since meaningful activity" collapses to "days since
   last charge", which for any >90-day cadence (annual policy, domain renewal, quarterly
   membership) falsely reads as unused for most of the cycle. Until a real usage signal
   exists, UNUSED is not raised. **Guard (if UNUSED is ever re-enabled):** it MUST gate on
   `unusedDays > cadenceDays` — never flag a series within one billing cycle.
   *Reads:* `BudgetingApi.recurringCommitments()` / `ImportApi`-derived recurring series
   (merchant-normalized, with detected `cadenceDays`), last-charge dates.
   *Breach:* series still charging **and** `duplicateOfSeriesId != null` (overlapping active
   series to the same normalized merchant within `duplicateWindowDays`).
   *Rationale:* `{merchant, cadence, cadenceDays, amountMinor, lastChargedAt, chargesInWindow,
   annualizedMinor, reason: DUPLICATE, duplicateOfSeriesId}`. *Impact:*
   `annualizedMinor` (recoverable if the duplicate is cancelled). *Suggested action:*
   review/cancel the redundant subscription (descriptive only).
   *Tunable defaults:* `minMonthlyMinor = 10000` (₹100 floor), `duplicateWindowDays = 35`.
   (`unusedDays` retained in params but inert while UNUSED is suppressed.)

3. **Liquidity risk — `LIQUIDITY_RISK`** (subject: `OWNER`).
   *Rule:* projected cash runway falls below `minRunwayMonths`.
   *Reads:* **`AnalyticsApi.runwayMonths()` only** (liquid assets ÷ trailing `avgBurn(W)`) —
   never a self-computed `liquidAssets / monthlyBurn` division. If `runwayMonths()` is
   `∞`/null (zero or insufficient-history burn), **do not raise** (insufficient data, not
   "healthy").
   *Breach (with hysteresis):* raise at `runwayMonths < minRunwayMonths`; auto-resolve only
   at `runwayMonths > minRunwayMonths × (1 + ε)`.
   *Rationale:* `{liquidAssetsMinor, avgBurnMinor, runwayMonths, minRunwayMonths,
   shortfallMonths}`. *Severity ladder (tunable, hysteretic):* `< criticalRunwayMonths` →
   CRITICAL, else HIGH.  *Suggested action:* rebuild liquid buffer / reduce burn by
   `shortfall`.
   *Tunable defaults:* `minRunwayMonths = 3`, `criticalRunwayMonths = 1`.

4. **Portfolio concentration — `CONCENTRATION`** (subject: `HOLDING`).
   *Rule:* a single holding exceeds `maxSingleHoldingPct` of total portfolio value.
   *Reads:* `InvestmentApi.portfolioValue()`, `InvestmentApi.holdings()` (per-holding
   market value).
   *Breach:* `holdingValue / portfolioValue > maxSingleHoldingPct`.
   *Deliberate concentration:* an owner who intentionally holds a concentrated position
   (employer ESOPs, a chosen index fund) marks the insight **`ACCEPTED`** (via
   `acceptInsight`), which records `accepted_at_level = concentrationPct`. The detector then
   **stops re-raising** until concentration rises materially past that level
   (`concentrationPct > accepted_at_level + materialDeltaPct`) — no 30-day dismissal nag loop
   for a considered decision.
   *Rationale:* `{holdingLabel, holdingValueMinor, portfolioValueMinor, concentrationPct,
   maxSingleHoldingPct, excessValueMinor}`. *Severity (tunable, hysteretic):* `> criticalPct`
   → HIGH, else MEDIUM. *Suggested action:* consider trimming toward the target weight
   (descriptive).
   *Tunable defaults:* `maxSingleHoldingPct = 20.0`, `criticalPct = 40.0`,
   `minPortfolioMinor = 10000000` (₹1,00,000 floor so a tiny portfolio isn't flagged),
   `materialDeltaPct = 10.0` (re-raise an ACCEPTED concentration only if it worsens by this).

5. **Debt optimization — `DEBT_OPT`** (subject: `LIABILITY`).
   *Rule:* the owner carries a liability whose interest rate exceeds `minDebtRatePct`
   **while** deployable liquid cash (above the emergency buffer **and** clear of earmarked
   obligations) sits earning less — paying down the debt beats holding the cash.
   *Reads:* `LedgerApi` liability `rate_bps` (financial-product metadata seam) + balance,
   `AnalyticsApi.liquidBalance()`, `AnalyticsApi.avgBurn(W)` (to size the protected buffer),
   `ObligationsApi.committedOutflowsMinor(horizon)`.
   *Deployable (obligation-aware):*
   `deployableMinor = liquidBalance − bufferMultiple × avgBurn − committedOutflowsMinor`.
   Cash earmarked against a dated obligation is **never** deployable, and this detector — the
   only one proposing an **irreversible** action (prepay) — must **never** suggest deploying
   such cash.
   *Breach:* `rate_bps` present (else **suppress** — never default absent rate to a value)
   **and** `liabilityRatePct > minDebtRatePct` **and** `deployableMinor > 0` **and**
   `liabilityRatePct − idleCashYieldPct > rateGapPct`.
   *Rationale:* `{liabilityLabel, balanceMinor, liabilityRatePct, idleCashMinor,
   protectedBufferMinor, committedOutflowsMinor, deployableMinor, idleCashYieldPct,
   rateGapPct, annualInterestSavedMinor}`.
   *Impact:* `min(deployableMinor, balanceMinor) × (liabilityRatePct − idleCashYieldPct)`
   annualized. *Suggested action:* deploy `deployableMinor` toward the higher-rate debt,
   preserving the emergency buffer and all earmarked obligations. *Tunable defaults:*
   `minDebtRatePct = 10.0`, `rateGapPct = 3.0`, `bufferMultiple = 6` (shared with idle cash;
   × trailing burn kept before any prepay).

6. **Emergency reserve — `EMERGENCY_RESERVE`** (subject: `OWNER`).
   *Rule:* the liquid emergency buffer is below `targetMonths` of essential expenses.
   *Reads:* `AnalyticsApi.liquidBalance()`, `BudgetingApi.essentialMonthlyExpense()` (falls
   back to `AnalyticsApi.avgBurn(W)` if budgeting isn't configured; if that is
   `∞`/insufficient-history, **do not raise**).
   *Buffer reconciliation:* `targetMonths` is the **canonical safe-buffer figure**; idle-cash
   and debt-opt keep `bufferMultiple ≥ targetMonths`, so no detector labels reserve cash as
   idle/deployable. One safe-buffer number across the three cash detectors.
   *Breach (with hysteresis):* raise at `liquidBuffer < targetMonths × essentialMonthlyExpense`;
   auto-resolve only above the same target × `(1 + ε)`.
   *Rationale:* `{liquidBufferMinor, essentialMonthlyExpenseMinor, targetMonths,
   targetReserveMinor, shortfallMinor, currentMonthsCovered}`. *Severity (tunable, hysteretic):*
   `currentMonthsCovered < 1` → HIGH, else MEDIUM. *Suggested action:* build the reserve by
   `shortfallMinor` before other deployments. *Tunable defaults:* `targetMonths = 6`
   (= idle-cash/debt-opt `bufferMultiple`), `minReserveMonths = 3` (below this → HIGH).

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
- **`acceptInsight(insightId)`** — `OPEN|ACKNOWLEDGED → ACCEPTED` (terminal); records
  `accepted_at = now` and `accepted_at_level` = the current breach magnitude (e.g.
  `concentrationPct`); appends `ACCEPTED`. Marks the position/situation intentional: the
  detector will not re-raise until the subject materially worsens past `accepted_at_level`
  (by `materialDeltaPct`). Idempotent.
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

Consumed by the **`notification/` context** (conventions §12), which owns ranking, dedup,
digest, quiet hours, and channel delivery of `InsightRaised` — delivery is no longer out of
scope, it is that context's responsibility. UI rendering is downstream. Written to the
outbox in the same DB transaction as the insight change (transactional outbox, §6). The
hysteresis + debounce above ensure this stream is stable (no raise/resolve churn), so the
notification consumer is not flooded.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/insights` | list (owner-scoped, filter by status/severity/detector, cursor) |
| GET | `/api/v1/insights/{id}` | full insight incl. rationale + event timeline |
| POST | `/api/v1/insights/{id}/acknowledge` | OPEN → ACKNOWLEDGED |
| POST | `/api/v1/insights/{id}/dismiss` | dismiss + optional snooze (Idempotency-Key) |
| POST | `/api/v1/insights/{id}/accept` | mark intentional → ACCEPTED (Idempotency-Key) |
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
- **Lifecycle:** `OPEN → ACKNOWLEDGED → RESOLVED`, `OPEN → DISMISSED (+snooze)`,
  `OPEN|ACKNOWLEDGED → ACCEPTED`, and auto-resolve when the breach clears; every transition
  appends exactly one `insight_event`; invalid transitions return
  `OPTIMIZATION_INVALID_TRANSITION`.
- **Snooze:** a dismissed-with-snooze insight does not re-raise before `snooze_until`; it
  may raise a fresh insight after it passes.
- **Accepted (intentional):** an `ACCEPTED` concentration does not re-raise after any snooze
  window; it re-raises **only** when `concentrationPct` rises past `accepted_at_level +
  materialDeltaPct`, never merely because time elapsed.
- **Hysteresis / anti-flap:** a metric oscillating inside the `[min, min×(1+ε)]` band
  produces exactly one insight and **zero** resolve/raise churn (no `InsightResolved`/
  `InsightRaised` pair per crossing); auto-resolve is suppressed below `minOpenDuration`.
- **Obligation-awareness:** cash earmarked by `ObligationsApi.committedOutflowsMinor` is
  excluded from idle-cash `excess` and debt-opt `deployable`; a dated obligation covering the
  surplus suppresses both insights — debt prepay is never suggested against earmarked cash.
- **Buffer reconciliation:** no liquid balance produces a simultaneous `IDLE_CASH` and
  `EMERGENCY_RESERVE` breach on the same rupees — with `bufferMultiple ≥ targetMonths` the
  old conflicting `(3×, 6×)` band is gone; a balance below the reserve target has zero idle
  excess by construction.
- **Grounding / suppression:** absent `yield_bps` suppresses idle cash; absent `rate_bps`
  suppresses debt-opt (never defaulted to 0); `avgBurn` insufficient / `runwayMonths()` = ∞
  suppresses runway & reserve — no fabricated insight.
- **SUB_LEAKAGE:** an annual/quarterly (>90-day-cadence) fully-used subscription raises
  **nothing** (UNUSED suppressed); only a genuine DUPLICATE series raises.
- **First-raise race:** two concurrent runs (scheduled + event) for a not-yet-existing
  insight converge to exactly one RAISED (via `ON CONFLICT`/23505 → RESEEN), never a
  unique-violation failure.
- **Dangling subject:** after a recurring series is deleted/merged, the scheduled full run
  auto-resolves the orphaned live `SUB_LEAKAGE` insight.
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

## Audit fixes applied

**Amended 2026-07-23** — remediation of the adversarial review
(`audit/09-optimization.md`, HIGH 4 · MEDIUM 4 · LOW 1). Mapping finding → change:

| Audit finding | Sev | Fix applied |
|---|---|---|
| Idle-cash & debt-opt treat earmarked cash as deployable (no obligation port) | HIGH | Added `ObligationsApi.committedOutflowsMinor(horizon)` read port (recurring-obligation registry seam + forecasting one-time obligations, §12); detectors 1 & 5 subtract committed obligations from `excess`/`deployable` before breach; debt-opt (irreversible prepay) never proposes deploying earmarked cash. |
| Subscription "unused" has no usage signal — every annual/quarterly sub breaches | HIGH | Detector 2 suppresses UNUSED (no real usage signal exists); only the groundable DUPLICATE reason is raised. If UNUSED is ever re-enabled it MUST gate on `unusedDays > cadenceDays` (never flag within one billing cycle). |
| Idle-cash (3×) contradicts emergency-reserve (6×) | HIGH | Buffers reconciled: idle-cash & debt-opt share `bufferMultiple = 6 ≥ EMERGENCY_RESERVE.targetMonths`; reserve cash is never labelled idle; no opposite advice on the same rupees. |
| Account yield / liability rate have no source → idle-cash inverts | HIGH | Yield (`yield_bps`) / rate (`rate_bps`) now sourced from the reserved account financial-product metadata seam (§12) via `LedgerApi`; a detector **suppresses** (never defaults null → 0) when the number is absent. |
| No hysteresis + per-transaction triggers → flapping & event storm | MED | Per-detector two-threshold hysteresis (raise `<min`, resolve `>min×(1+ε)`) + `minOpenDuration` before auto-resolve; event-driven re-runs debounced/coalesced (no synchronous re-run per `TransactionPosted`). |
| `monthlyBurn()` underspecified — single-month volatility + zero-burn blind spot | MED | Standardized every detector on `AnalyticsApi.avgBurn(W)` / `runwayMonths()` (never a raw single month, never self-divided); `∞`/insufficient-history burn ⇒ "do not raise". |
| Concurrent scheduled + event run → unique-violation on first raise | MED | Upsert uses `INSERT ... ON CONFLICT ... DO NOTHING`; on conflict / 23505 re-read the live row and fall through to the RESEEN branch. |
| Deliberate concentration re-nags forever — no accepted state | MED | Added `ACCEPTED` terminal state + `acceptInsight` (records `accepted_at_level`); detector 4 re-raises only if the position materially worsens (`materialDeltaPct`), not on snooze elapse. |
| Dangling subject after a series is deleted/merged — OPEN insight never resolves | LOW | Scheduled full run resolves any live insight whose subject no longer appears in the source context's current API listing. |
| Delivery/UI "out of scope" | — | Insights flow to the new `notification/` context (§12) for ranking/dedup/digest/quiet-hours/channel delivery; delivery is no longer out of scope. |
