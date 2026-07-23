# Midas — Design Audit & Product Opportunities (Consolidated)

**Date:** 2026-07-23
**Method:** 14 parallel adversarial/product agents — 9 correctness auditors (one per context)
+ 5 product lenses (requirements completeness, India-first, digital-twin elevation,
adoption/trust, domain-modeling gaps). Findings below are deduped and ranked; the 5
pre-known findings (#1–#5) from the earlier manual pass are folded in.

**Headline:** ~12 CRITICAL + ~35 HIGH correctness findings, but they collapse into **six
cross-cutting themes** — fixing the theme fixes many findings at once. Separately, the
product review converges hard on **one missing piece (notification delivery)** and a clear
signature direction (**grounded AI copilot + scenario studio**).

> Nothing in the design docs has been changed yet. This is the decision input.

---

# Part A — Correctness (must-fix; several are truth-corrupting)

## Theme 1 — Corrections: reversals & refunds are under-modeled (the #1 cluster)

Surfaced independently by **ledger, analytics, reconciliation, budgeting**. This single
theme accounts for 3 CRITICALs + several HIGHs.

- **[CRITICAL] `reverseTransaction` UPDATEs an append-only table** (ledger). Convention §5
  grants only INSERT/SELECT; the POSTED→REVERSED write is impossible under the stated grants,
  or silently voids immutability. → Derive `REVERSED` from the existence of a reversing row;
  never store mutable status.
- **[CRITICAL] Reversal timestamp unspecified → rewrites point-in-time history** (ledger,
  analytics). If a reversal inherits the original's business date, `balanceAt(past)` retro-
  actively lies and old snapshots silently invalidate. → Reversal business-time = correction
  time; carry a distinct `reversalTimestamp`.
- **[CRITICAL] Analytics reversal double-counts category spend** — the category fold stores an
  absolute magnitude, so post+reverse = 2× spend, and **replay reproduces the corruption**. →
  Fold the *signed* natural value; add a post-then-reverse-nets-to-zero test.
- **[HIGH] Reversal booked in the wrong month drives burn & runway negative** (analytics). No
  `avgBurn < 0` guard. → Pin reversal to original month or clamp/ў treat negative avg as
  "insufficient data".
- **[HIGH] `TransactionReversed` is not consumed by reconciliation** → a reversed txn's
  statement entry stays falsely `RECONCILED`. → Consume it; supersede the match, return entry
  to PENDING/DISCREPANCY.
- **[HIGH] Refunds have no convention and are mis-handled** (budgeting, +known #3). A refund is
  NOT a reversal (original really happened); it's a new txn crediting the original expense
  account. Sign-based attribution skips the credit → envelope never credited back, safe-to-
  spend overstated. → State the refund convention; attribute by `account.type==EXPENSE` with
  signed amount, not by posting sign.
- **[HIGH] No guard against double-reversal / reversing a reversal** (ledger). → `UNIQUE(owner,
  reverses_txn_id)`; only reverse a POSTED non-reversal txn.

## Theme 2 — Net worth is defined inconsistently and computed at cost

Surfaced by **analytics, investment, ledger, domain-gaps, india-first**.

- **[HIGH] Investment double-count / contradiction:** 05 sums the ledger asset account (cost)
  AND 08 says analytics folds `HoldingRevalued` market value on top → net worth = cost+market.
  The two docs contradict; every reading is wrong. → Pick ONE: net worth = ledger net-position
  (investments at cost) + Σ unrealized gain, OR exclude broker asset accounts and add market
  value. Reconcile 05↔08.
- **[HIGH] Illiquid / manually-valued assets unmodeled** (domain-gaps, india-first): real
  estate, physical gold, **EPF/PPF/NPS** have no transaction stream and appreciate/accrue with
  no posting. Net worth (`SUM(postings)` at cost) is silently wrong — understated by lakhs for
  the target Indian user. → A `manual_valuation`/mark mechanism on any asset account + a
  revaluation posting convention; fold into net worth **before Phase 3 ossifies cost-basis**.
- **[HIGH] Parent/child hierarchy rollup unspecified** (ledger) → either useless parent
  balances or double-counted net worth. → Postings hit leaf accounts only; parents aggregate.
- **[MEDIUM] Ledger `netPosition`/`/net-worth` says "all accounts"** — contradicts the
  type-scoped `assets − liabilities` in analytics. → Reference one formula.

## Theme 3 — Outbox delivery & projection consistency (foundational, Phase 0)

Surfaced by **platform, analytics** (+known #4). Multiple CRITICALs at the spine.

- **[CRITICAL] One outbox `status` can't express per-consumer delivery** → a single poison
  consumer starves/drops the event for healthy consumers. → Per-`(event,consumer)` delivery
  rows; scope backoff/DEAD per consumer.
- **[CRITICAL] Backoff/multi-instance delivers out of `id` order; a max-applied high-water
  offset skips a stuck lower id → silent permanent event loss** (analytics projections
  understate forever). → Offset advances only over a contiguous prefix; never past an un-SENT
  lower id.
- **[CRITICAL] Owner seam absent on the async dispatch path** — `OwnerContext` is request-
  scoped; the scheduled dispatcher runs with none. Masked in Phases 0–5 by the single hard-
  coded owner; corrupts cross-owner projections at the Phase-6 flip. → Dispatcher opens an
  owner scope from `event.owner_id` around each `handle()`.
- **[HIGH] "Mark SENT" vs the consumer's `processed_event` write is a separate tx** → crash
  window loses events. → SENT derived from all-consumers-acked, or stay PENDING until acked.
- **[MEDIUM] outbox is both queue and genesis event-log with no retention policy** (+known #4
  DEAD event has no replay path). → Declare it the durable log; pruning invalidates rebuild;
  define DEAD-event catch-up.

## Theme 4 — Idempotency under concurrency & across modules

Surfaced by **platform, ledger, investment**.

- **[HIGH] Idempotency store is check-then-act with no lock** → concurrent same-key requests
  double-execute; only ledger is saved by its own unique constraint (import/others double-post).
  → Reserve-first: INSERT a PENDING record as the lock.
- **[MEDIUM] Idempotency-record write vs business-commit boundary unspecified** → crash-in-gap
  re-executes. → Write the record in the same tx as the effect + outbox row.
- **[MEDIUM] Cross-module atomicity** (investment→ledger mirror; reinvestment = 2 legs under 1
  key) → partial-failure orphans. → Same physical tx across the mirror; per-leg sub-keys.

## Theme 5 — Time: owner-local calendar + Financial-Year period (foundational)

Surfaced by **analytics, budgeting, forecasting, import, india-first** (+known #1).

- **[HIGH/known] UTC month bucketing mis-attributes** month-edge (post-23:30 IST) transactions
  across burn, budgets, rollups — and (import) **double-imports** a re-export that flips the UTC
  day. → Bucket by owner-local (Asia/Kolkata) civil day/month; truncate dedup day locally.
- **[REQ-GAP] Financial Year (Apr–Mar) as a first-class period** (india-first). Tax, 80C,
  capital-gains, advance-tax all run on FY. → Add an FY period primitive alongside calendar
  month; decide the seam before projections harden.

## Theme 6 — Integer-money representation in investment (3 CRITICALs, one root cause)

- **[CRITICAL] `cost_per_unit_minor` as integer paise/unit leaks basis vs. the ledger** on
  every fractional-unit buy (a ₹10k MF buy of 12.3456 units is already 17 paise off). Root cause
  of two more CRITICALs (**SPLIT double-rounding**, **partial-SELL cost drift**). → Store per-lot
  **total** cost as BIGINT paise as truth; derive per-unit for display; never round per-unit and
  multiply back.

## Per-context correctness findings not covered by a theme (condensed)

**Ledger:** deferred sum-zero CHECK is not implementable as a CHECK in PG → use a DEFERRABLE
`CONSTRAINT TRIGGER`; forbid empty/1-posting txns; posting.currency vs account.currency
unchecked + sum-zero ignores currency; archived-account posting/balance behavior undefined.

**Import:** [CRITICAL] proposal `idempotency_key` omits the occurrence-salt → genuine same-day
repeats (two ₹500 coffees, two identical HALF_TRANSFERs) silently dropped, no audit trail;
[CRITICAL] `reparse` re-imports over already-confirmed ledger txns; [HIGH] `external_ref` does
NOT dominate dedup (whole tuple hashed) → amended descriptor/amount double-imports; [HIGH]
coincidental equal-and-opposite flows inferred as TRANSFER → erases a real expense + income;
[HIGH] credit-card CSV sign convention inverts → spend imported as income; [HIGH] requeued
quarantine row on a COMPLETED batch never normalized/proposed → silent loss; merchant-rule ties
nondeterministic; opening/closing balance rows unmodeled; multi-line txns split.

**Reconciliation:** [CRITICAL] `confirmImportedTransaction` has no already-matched precondition
→ double-post with a different idempotency key the ledger can't dedup; two-tx confirm window
races the `TransactionPosted` consumer / orphans on retry; double-claim guard is TOCTOU; In-
Transit pairing links the WRONG leg when two equal transfers are in flight; `STALE_IN_TRANSIT`
(10d) fires wider than the pairing window (9d) → false stale; CLEARED/RECONCILED not
reconstructible from history (rebuild under-determined); no many:1 match primitive; liability
sign-conversion undefined → zero candidates for card accounts.

**Analytics:** avg-burn divides by fixed window incl. zero-activity months → runway overstated
~50%; liquidity ratio unguarded when net worth ≤ 0; In-Transit account has no liquidity bucket
→ runway understated mid-transfer; "mark dirty/recompute if stale" has no dirty column; category
grain (leaf vs parent) self-contradictory.

**Budgeting:** [HIGH] safe-to-spend subtracts one period of commitments from the **entire
liquid stock** (stock-vs-flow) → tells the user nearly all savings is spendable → overspend;
essentials + committed-obligations double-subtracted (rent/EMI twice); zero-funded envelope
trips CRITICAL overspent + divide-by-zero; category_id divergence strands essential spend in
Unbudgeted.

**Forecasting:** sub-monthly cadence collapses (biweekly salary halved); `cash_yield_bps` never
applied (idle cash earns nothing); runway sums liabilities → mortgage forces runway=0; growth
step-up keyed to absolute month not rule age → mid-horizon rules over-inflated / overrides reset
instead of freeze; import-detected + manual rule double-counts; FIRE withdrawal as %-of-portfolio
can never show ruin; `ADD_RULE at_month` vs body `start_date` ambiguous; fingerprint omits engine
version + can seed from a lagging projection.

**Optimization:** earmarked cash treated as deployable (no obligations port) → recommends
irreversible debt prepay on committed cash; "unused subscription" has no usage signal → flags
every annual renewal; idle-cash (3×) contradicts emergency-reserve (6×) → opposite advice on the
same rupees; account yield / liability rate have no source in the model → idle-cash inverts; no
hysteresis + per-txn trigger → raise/resolve flapping & event storm; deliberate concentration
re-nags forever (no ACCEPTED state); first-raise race → unique violation.

---

# Part B — Requirements gaps (block "usable" / "launchable")

Ranked. R = needed to be usable day-to-day; L = needed before launching to users.

1. **[R, headline] Notification / delivery layer — nobody ships the alert.** Flagged
   independently by **4 agents**. Budgeting, optimization, reconciliation, and the platform
   DEAD path all mint alert/insight/discrepancy *records*; **no context delivers them**. The
   product's most valuable moments hit the floor; there is no re-engagement loop. → NEW
   `notification/` context: idempotent outbox consumer, ranking/dedup, digest, quiet hours,
   channels (in-app/email/push), read/dismissed. The events already exist and are tested.
2. **[R, correctness-blocking] First-run onboarding + opening balances.** The ledger sums from
   genesis assuming every account started at ₹0; the `EQUITY` type is defined but never used.
   Net worth & every derived number are wrong for any real user until opening balances (via an
   Opening-Balances equity account) exist. → Guided setup: accounts, institutions, balances
   as-of-date.
3. **[R] Everyday review loop: recategorize + edit-before-confirm + split.** The most-repeated
   money-app interaction has no home — import review can only accept/reject, recategorization
   is deferred, no split-across-categories. → `CategoryReassigned` write path + edit-proposal +
   split primitive + merchant-rule learning loop.
4. **[R] Manual quick-entry for cash / un-imported spend** — India runs on cash; the only write
   path demands a balanced posting set. → A single-sided expense/income form that auto-builds
   the counter-posting.
5. **[R] Global search & filter over transactions** — today only per-account statements +
   monthly rollups. "I can't find a transaction" is a day-one failure.
6. **[R] Low-effort bulk review / one-tap confirm** — 120 proposals one-by-one kills the confirm
   step (and nothing enters the ledger until confirmed). → batch-confirm + auto-confirm-above-
   threshold opt-in (already a flagged open decision).
7. **[L] Auth / sessions / MFA** — one sentence today. Not launchable without a real spec.
8. **[L] Encryption-at-rest** — raw bank statements sit as plaintext `BYTEA`. Baseline before
   launch; **local-first / E2EE is a marketable differentiator** ("we can't read your data").
9. **[L] Data export / portability, deletion (DPDP), backup/DR, user-facing audit** — export &
   backup fit cleanly; deletion tensions with the append-only ledger and needs a deliberate
   design (crypto-shredding).
10. **[R] Bill/EMI due reminders & upcoming-obligations view** — forecasting knows every due
    date; nothing surfaces a forward calendar. Depends on #1.
11. **[R] Onboard an unrecognized bank CSV** — self-serve column mapping; without it, import
    (the #2 feature) silently fails for unsupported formats.
12. **[R] Account lifecycle** — rename/close/merge with history intact (collides with
    category-from-account-name and duplicate-account-on-reimport).

---

# Part C — Elevation (what could make Midas exceptional)

**The architectural asset to exploit:** every user-visible number is already forced through a
narrow, deterministic, owner-scoped context API, and insights already carry the exact numbers
that produced them. That is the substrate that lets intelligence be **grounded, not
hallucinated** — the "why us".

**Signature bet — AI Financial Copilot (grounded, tool-calling, ledger-truth only).** Ask
finances anything in plain language; every numeric claim must trace to a context-API tool call
in the same turn or be suppressed. The audited APIs map ~1:1 onto tool definitions; owner-
scoping is inherited. Competitors bolt a chatbot onto raw rows; Midas can ship one whose every
figure is provably double-entry truth. (NEW `copilot/`, Phase 4.5+.)

**Co-signature — What-If Scenario Studio + "Can I afford X?" + Monte Carlo.** The deterministic,
cached, fingerprinted forecast engine already exists; add authoring/side-by-side compare, the
one-tap affordability question (**cheapest high-impact — build first as the proof point**), and
probabilistic bands (seeded, so determinism holds) to make "digital twin" honest.

**Highest-leverage cheap wins (reuse existing projections, no new truth):**
- **Proactive nudge delivery** (= requirements #1) with Claude turning dry `rationale` JSONB into
  one well-timed human sentence — the cleanest records→product upgrade.
- **Financial Health Score** — one 0–100 trended number composed from existing
  runway/savings-rate/debt/reserve/concentration readings; Claude explains the one action that
  moves it most.
- **Self-benchmarking** ("23% more on dining than your 6-mo avg") — trivial over existing
  monthly projections.
- **Cashflow calendar** — daily projected balance + danger-days from `recurring_rule` +
  `safeToSpendPerDay`.

**Structural elevation:**
- **Goal-based planning** — target/date/funding/progress + on-track verdict; a goal is a named
  forecast target + funding source (NEW `goals/`, reads analytics+forecasting).
- **Automation rules** — auto-categorize, payday auto-allocate, auto-confirm high-confidence
  matches; outbox-triggered idempotent consumers, money-moves still go through confirm.
- **Spending anomaly detection** & **renewal/price-hike radar** — new detectors in the existing
  framework.

---

# Part D — India-first (must-have vs differentiator)

1. **[HIGH] Capital-gains tax + loss-harvesting engine.** Investment already stores lot-level
   `acquired_date`+`term` and throws the tax away. FY-aware LTCG (₹1.25L exemption)/STCG/set-off
   + the Mar-31 harvest ritual is the single highest-value feature for an Indian investor.
2. **[HIGH] EPF/PPF/NPS accrual instruments.** Largest asset for most salaried Indians; needs an
   accrual-valuation path (declared rate, employer match, lock-in), distinct from market-price.
   Without it net worth is permanently understated.
3. **[HIGH] Account Aggregator (RBI AA) ingestion.** The adapter seam exists but is parked; AA is
   what makes "always-fresh, zero-manual-entry" real and feeds #1/#2 clean MF/FD/PF data.
   (Partner + consent lifecycle; L effort, zero schema change.)
4. **[MED-HIGH, foundational] FY + IST calendar** (= Theme 5).
5. **[HIGH] EMI amortization + prepay-vs-invest** (= domain Loans, below).
6. **[HIGH] 80C/80D headroom + old-vs-new regime + Mar-31 nudge** — perfect fit for the grounded
   insight model.
7. **[MED-HIGH] Advance-tax / TDS / AIS reconciliation** — lumpy quarterly outflow that breaks
   naive safe-to-spend.
8. **[MED] UPI-native identity** — P2P vs P2M (P2P must not count as spend), multi-VPA collapse,
   autopay-mandate grouping, failed-debit↔auto-reversal pairing. Accuracy floor for burn.
9. Sinking funds for lumpy cashflow; real estate + gold; reimbursable/shared tagging.

---

# Part E — Domain modeling to settle NOW (cheap now, ugly later)

1. **Category as a shared dimension** — modeled 4 incompatible ways across analytics/budgeting/
   forecasting/import; README leaves ownership open; `budget_envelope.category_id` FKs a concept
   that doesn't formally exist. → One owned taxonomy (hierarchical, user-editable) + resolver
   port, before budgeting/analytics harden.
2. **Loans & Liabilities** — amortization, per-EMI interest/principal split, prepayment, +
   account rate/tenure/credit-limit metadata (which `IDLE_CASH`/`DEBT_OPT` already read but the
   schema lacks). EMI-as-flat-expense corrupts both burn and net worth. → NEW `liabilities/` +
   extend ledger account metadata, before Phase 3/4 bakes in the wrong split.
3. **Illiquid / manually-valued assets + mark-to-market net worth** (= Theme 2).
4. **Recurring-Obligation registry** — `CommittedObligationsPort` and `SUBSCRIPTION:{id}` are
   load-bearing in the specs but backed by no entity; import detects, forecasting projects,
   budgeting reserves, optimization audits — all off one canonical obligation.
5. **Financial Goals** (structural; see Part C).
- *Defer (seam only): Taxes engine, Insurance, People/Counterparties, Documents/attachments.*

---

# Proposed remediation plan

**Tier 0 — amend the design before ANY build (spine correctness + cheap-now modeling):**
- Theme 1 (reversal/refund model), Theme 3 (outbox per-consumer delivery + contiguous offset +
  owner-on-dispatch), Theme 4 (reserve-first idempotency, same-tx record), Theme 5 (owner-local
  calendar + FY seam), Theme 6 (per-lot total cost), Theme 2 net-worth definition (pick one
  model; add mark-to-market seam), ledger sum-zero trigger + leaf-only postings, reconciliation
  confirm guard + consume `TransactionReversed`, import proposal-key + external_ref dedup fixes.
- Part E #1 (category shared dimension) and reserve seams for #2–#4.

**Tier 1 — fold into the relevant phase's plan as first-class scope:**
- Notification layer (Phase 3), onboarding+opening balances (Phase 1), everyday review loop
  (Phase 2/3), safe-to-spend stock-vs-flow rewrite (Phase 4), forecasting cadence/growth/runway
  fixes (Phase 4), optimization obligations-port + hysteresis + buffer reconciliation (Phase 6).

**Tier 2 — new phases / contexts on the roadmap:**
- Loans/liabilities, illiquid assets, goals, AA ingestion, capital-gains tax, EPF/PPF/NPS,
  AI copilot, scenario studio, auth/encryption/export-deletion-backup.

**Tier 3 — deferred with seams reserved:** taxes engine depth, insurance, counterparties,
attachments, household sharing, cohort benchmarking, Monte Carlo.
