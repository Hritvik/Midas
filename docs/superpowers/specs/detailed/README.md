# Midas — Detailed Design (Index)

Broad-first detailed design for every bounded context. Read `00-conventions.md` first —
it is the shared contract all other docs inherit and must not restate.

**Amended 2026-07-23** — Tier 0 + Tier 1 audit remediation applied across all contexts; two
new contexts added (`category`, `notification`). See `../2026-07-23-audit-and-opportunities.md`.

| Doc | Context | Phase | Owns truth? |
|---|---|---|---|
| `00-conventions.md` | Shared design contract | — | — |
| `01-platform.md` | Owner seam, idempotency, outbox + dispatcher | 0 | infra |
| `02-ledger.md` | Double-entry truth, balances, point-in-time | 1 | **YES (the only truth)** |
| `03-import.md` | Ingest → staged entries → proposed txns | 2 | no (staging) |
| `04-reconciliation.md` | Match/confirm proposals into the ledger | 2 | no (proposes) |
| `05-analytics.md` | Net worth, burn, runway projections | 3 | no (read model) |
| `10-category.md` | **Shared category taxonomy + resolver** | 3 | no (dimension) |
| `11-notification.md` | **Delivers alerts/insights/reminders** | 3 | no (consumer) |
| `06-budgeting.md` | Envelopes, safe-to-spend | 4 | no (read model) |
| `07-forecasting.md` | Recurring projection, scenarios | 4 | no (derived) |
| `08-investment.md` | Holdings, XIRR, tax lots | 5 | no (mirrors cash to ledger) |
| `09-optimization.md` | Insights / recommendations | 6 | no (read model) |

**Roadmap contexts (seams reserved in Tier 0, full design in their own phase):**
`liabilities` (loans/amortization), `goals`, `tax` (capital-gains/80C/advance-tax), `copilot`
(grounded AI). See the audit doc's Tier 2/3.

## Invariants that hold across every doc

1. **One source of truth.** Only `ledger` owns financial truth. The only writers are
   `reconciliation` (confirming proposals) and `investment` (mirroring cash legs) — both via
   `LedgerApi.postTransaction`/`reverseTransaction` in the **same physical DB transaction**
   (conventions §7). Everyone else only reads.
2. **Money is `BIGINT` minor units + currency; quantities are integers scaled 1e8.** Aggregate
   cost/valuation is stored as a **total**, never a rounded per-unit multiplied back
   (conventions §2).
3. **Net worth = Σ assets − Σ liabilities + Σ unrealized gain**, over leaf accounts,
   investments at cost + gain added exactly once (conventions §9). One canonical formula in
   ledger/analytics/investment.
4. **Corrections:** reversals are correction-time and derive `REVERSED` from a reversing row
   (no mutable status); refunds are ordinary expense-credit transactions. Signed folds net them
   out (conventions §10, §8).
5. **Projections are rebuildable** from the durable outbox (deterministic replay), incl. a
   post-then-reverse-nets-to-zero test on every signed fold.
6. **Outbox delivery is per-`(event,consumer)`** with a contiguous low-water offset and
   owner-scoped async dispatch; idempotency is **reserve-first** (conventions §6, §7).
7. **Time is owner-local** (Asia/Kolkata) for all day/week/month/FY bucketing; storage UTC
   (conventions §3).

## Cross-context seams (reconciled)

- **import → reconciliation.** Trigger is import's `TransactionProposed`; the confirm flow is a
  single physical tx with an already-matched precondition (no double-post). Proposal
  idempotency key folds occurrence identity; `reparse` forbidden once proposals are emitted.
- **ledger → analytics / budgeting / forecasting / reconciliation.** All consume
  `TransactionPosted` **and `TransactionReversed`** from the outbox.
- **Category** is owned by the dedicated **`category/` context** (`10`): one taxonomy +
  `CategoryResolver` (tag → account map → merchant rule → uncategorized). Analytics, budgeting,
  forecasting, import all consume it; none defines its own. Transfers resolve to null category.
- **Notification** (`11`) is the sole delivery consumer of `budget_alert`/`InsightRaised`/
  `DiscrepancyFlagged`/due-reminders/DEAD-letter — closing the "records go nowhere" gap.
- **optimization** reads peers only via their public APIs (incl. a new `ObligationsApi` so
  earmarked cash isn't flagged deployable); account rate/yield come from the reserved
  financial-product metadata seam (suppress when absent, never default 0).
- **Internal transfers** unchanged in spirit: balanced asset/liability transaction; separate-leg
  transfers use `HALF_TRANSFER` against `Transfers:In-Transit`, paired/cleared by reconciliation
  with counterparty corroboration.

## Reserved seams (design now, full build later — conventions §12)

- **Account financial-product metadata** (`rate_bps`, `tenure_months`, `credit_limit_minor`,
  `yield_bps`, `statement_day`, `due_day`) on `account` — feeds loans + `IDLE_CASH`/`DEBT_OPT`.
- **`manual_valuation`** mark on any asset account — feeds mark-to-market net worth for
  illiquid assets (real estate, gold, EPF/PPF/NPS).
- **Recurring-obligation registry** interface — import detects into it; forecasting projects;
  budgeting reserves; optimization audits. Replaces the four former partial notions.

## Resolved (previously open) decisions

- **Category** → owned by the new `category/` context (was "open").
- **Auto-confirm reconciliation** → still a Phase-2+ opt-in; bulk-confirm UX is a Tier-1
  requirement (audit Part B #6).
- **Net worth model** → cost + Σ unrealized gain (decided 2026-07-23).
- **Timezone/FY** → owner-local Asia/Kolkata; Apr–Mar Financial Year is a first-class period.

## Still open / deferred (roadmap)

- Full **loans/liabilities**, **goals**, **tax engine**, **AI copilot**, **AA ingestion**,
  **auth/MFA + encryption-at-rest + export/deletion/backup** — all in the audit doc's Tier 2/3.
- External price feeds & non-CSV import adapters — designed, deferred, additive.
