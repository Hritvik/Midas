# Midas — Personal Treasury OS: Design Spec

**Date:** 2026-07-19
**Status:** Approved (brainstorming complete)
**Source PRD:** `personal_treasury_os_prd.md`

---

> **Detailed design:** per-context detailed design (schema, API contracts, algorithms)
> lives in `detailed/` — start with `detailed/README.md`. This document is the north star;
> the `detailed/` docs are the buildable specification.

## 1. Summary

Midas is a personal financial **decision-support system** — not an expense tracker. Its
job is to answer questions like *"how much can I safely spend?"*, *"am I building
wealth?"*, and *"what happens if I change X?"* on top of an immutable, double-entry
ledger.

This spec captures the **full target architecture** (all 8 bounded contexts from the
PRD) and a **phased release plan**. Each phase is implemented under its own
implementation plan; this document is the shared north star.

## 2. Requirements locked during brainstorming

1. **Audience:** Single-user first ("me first, then launch for users"). The domain
   carries an owner/tenant seam from day one; real auth/tenancy is deferred to a later
   phase, never retrofitted.
2. **Stack:** Spring Boot + PostgreSQL backend, React/Next.js frontend (as the PRD
   recommends). Java-native, matches the existing IntelliJ project.
3. **Priority after the core ledger:** Import automation (PRD's Import & Aggregation
   context) is release priority #2.

## 3. Ledger core — the spine

**Decision: Classic double-entry, relational (Option A) as the truth model, plus a
domain-event outbox seam (Option C) built in from the start.** Full event sourcing
(Option B) is explicitly rejected as YAGNI for a single-user app.

- Two append-only tables: `ledger_transaction` (immutable header) and `posting`
  (immutable lines). **Invariant: postings within a transaction sum to zero.**
- Balances are **derived** — `SUM(postings)` up to a point in time — never mutable
  columns. Corrections are **reversal transactions**, never mutations.
- **Point-in-time reconstruction** = sum postings with `timestamp <= T`, accelerated by
  period **snapshot tables**.
- Every committed transaction also writes an **outbox** row inside the same DB
  transaction. Import, reconciliation, analytics, budgeting, and forecasting consume
  this stream. Later this outbox becomes the Kafka bridge the PRD notes as "optional
  later" — with zero change to the truth model.

This satisfies the PRD's ledger NFRs directly: immutable history, deterministic replay,
point-in-time reconstruction, full audit trail, ACID (one Postgres transaction wraps the
whole posting set), idempotent writes.

## 4. Target architecture

**Deployment shape:** Modular monolith. One Spring Boot app, one Postgres DB. Bounded
contexts are internal modules with clean interface boundaries — **not** microservices.
The outbox seam means any module can be extracted later if it ever earns it. React/Next
frontend talks to a REST API.

### Module map

```
midas/
├─ ledger/          Truth. Accounts, transactions, postings, balances,
│                   point-in-time reconstruction, snapshots. Owns the outbox.
├─ import/          Ingest external data → proposed transactions.
│                   CSV/statement parsers, dedup, merchant normalization,
│                   recurring & transfer inference. (Priority #2.)
├─ reconciliation/  Match ledger vs. imported StatementEntries.
│                   pending→cleared→reconciled, confidence scoring, discrepancies.
├─ analytics/       Read-side projections: net worth, burn, runway, cash flow,
│                   category/liquidity analysis. Consumes the outbox.
├─ budgeting/       Envelopes, allocations, safe-to-spend, overspend alerts.
├─ forecasting/     Recurring projection, scenario simulation (FIRE, job loss…).
├─ investment/      Holdings, cost basis, XIRR, gains, SIPs, tax lots.
├─ optimization/    Idle cash, subscription leakage, concentration, reserves.
├─ category/        Shared category taxonomy + resolver (added 2026-07-23).
├─ notification/    Delivers alerts/insights/reminders (added 2026-07-23).
└─ platform/        Cross-cutting: owner/tenant seam, idempotency store,
                    outbox dispatcher, config, auth (stub now, real later).

Roadmap contexts (seams reserved 2026-07-23, full design later): liabilities (loans/
amortization), goals, tax, copilot. See detailed/README.md and the audit doc.
```

### Data flow

```
External data ──▶ import ──▶ StatementEntry (staging, NOT truth)
                                   │
                                   ▼
                          reconciliation ──▶ proposes/matches
                                   │
                                   ▼
      user confirms ──▶ ledger.postTransaction() ──▶ postings (truth) + outbox row
                                                              │
                       ┌──────────────────────────────────────┤ (outbox dispatcher)
                       ▼                     ▼                 ▼
                  analytics            budgeting          forecasting
                 (projections)      (envelope spend)     (recompute)
```

**Key rule:** imports never write truth directly. They produce `StatementEntry` staging
rows; only a confirmed, balanced double-entry transaction hits the ledger. This keeps
the PRD's "external event ≠ internal truth" distinction real.

### Cross-cutting decisions

- **Currency:** INR-only to start, but `currency` stored on every account and posting →
  multi-currency is additive, never a migration.
- **Money type:** `BIGINT` minor units (paise) + currency code. Never floating point.
- **Idempotency:** central `idempotency_key` store in `platform/`, used by both import
  and transaction APIs.
- **Owner seam:** `owner_id` on every aggregate root now, hard-coded to one user;
  becomes real auth in the multi-user phase.
- **Point-in-time:** derived by summing postings ≤ T, accelerated by period snapshot
  tables in `ledger/`.

## 5. Domain model (from PRD, refined)

- **Account** — `id, owner_id, name, type, currency, parent_account_id, institution_id,
  status`. Types: Asset, Liability, Equity, Income, Expense.
- **LedgerTransaction** — `id, owner_id, timestamp, description, reference, source,
  idempotency_key, status`. Immutable.
- **Posting** — `id, transaction_id, account_id, amount (minor units), currency,
  sequence_number`. Immutable. **Sum per transaction = 0.**
- **Institution** — e.g. HDFC, ICICI, Zerodha.
- **StatementEntry** — external financial event in staging; **not** internal ledger
  truth.

## 6. Release phases

Each phase ships something usable on its own. Each phase gets its own implementation plan.

| Phase | Name | Scope | Done when (usable outcome) |
|---|---|---|---|
| **0** | Foundation | Spring Boot + Postgres skeleton, Flyway migrations, `platform/` module: owner seam, idempotency store, outbox table + dispatcher, health check, git + CI | App boots, health endpoint green, outbox plumbing tested end-to-end |
| **1** | Ledger core (MVP truth) | Accounts (5 types, hierarchy), transactions, postings w/ sum-zero invariant, reversals, transfers, balance query, point-in-time reconstruction + snapshots, REST API w/ idempotency | Keep real double-entry books via API: post transactions, get balances, reconstruct any past date |
| **2** | Import automation *(#2)* | CSV import → `StatementEntry` staging, dedup, merchant normalization, recurring & transfer inference, proposes transactions; basic reconciliation (match → confirm into ledger) | Upload a bank CSV → review proposed transactions → confirm into the ledger |
| **3** | Analytics + first UI | Outbox-driven projections: net worth, burn, runway, cash flow, categories, liquidity. First React/Next frontend: timeline + dashboards | Dashboard showing where money goes, net worth trajectory, cash runway |
| **4** | Budgeting + Forecasting | Envelopes, allocations, safe-to-spend, overspend alerts; recurring projection + scenario simulation (FIRE, job loss, home purchase) | "How much can I safely spend this month" + future-state simulation |
| **5** | Investment | Holdings, cost basis, XIRR, realized/unrealized gains, SIPs, dividends, tax lots, allocation | Portfolio tracking + returns |
| **6** | Optimization + Launch-ready | Idle cash, subscription leakage, concentration & liquidity alerts, reserve recs; flip owner seam → real auth/tenancy; polish | Recommendations live; app is multi-user launchable |

### Deliberate sequencing calls

1. **API-first for Phases 1–2; first real UI in Phase 3.** Building UI for raw ledger
   entry before analytics is low-value, and imports reduce manual entry anyway. The UI
   arrives when there's something worth seeing.
2. **This spec covers the full design + all 7 phases, but each phase gets its own
   implementation plan.** The next step is the Phase 0 + 1 plan only.

## 7. Testing strategy

- **Ledger invariants** (sum-zero, immutability, point-in-time correctness) covered by
  unit + property-style tests — the ledger is the highest-risk surface.
- **Idempotency** tested by replaying the same request/import and asserting no duplicate
  effects.
- **Outbox** tested end-to-end: committing a transaction produces exactly one outbox row
  in the same DB transaction; dispatcher delivers at-least-once with idempotent consumers.
- **Import pipeline** tested against real sample bank CSVs (dedup, recurring/transfer
  inference).

## 8. Explicitly out of scope initially (per PRD)

AI recommendations, tax optimization, brokerage integrations, real-time sync,
multi-currency, complex budgeting. These land in later phases or beyond.

## 9. Success metrics (from PRD)

Higher financial clarity, spending confidence, and future visibility; lower financial
anxiety and manual bookkeeping effort.
