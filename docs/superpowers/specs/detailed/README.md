# Midas — Detailed Design (Index)

Broad-first detailed design for every bounded context. Read `00-conventions.md` first —
it is the shared contract all other docs inherit and must not restate.

| Doc | Context | Phase | Owns truth? |
|---|---|---|---|
| `00-conventions.md` | Shared design contract | — | — |
| `01-platform.md` | Owner seam, idempotency, outbox + dispatcher | 0 | infra |
| `02-ledger.md` | Double-entry truth, balances, point-in-time | 1 | **YES (the only truth)** |
| `03-import.md` | Ingest → staged entries → proposed txns | 2 | no (staging) |
| `04-reconciliation.md` | Match/confirm proposals into the ledger | 2 | no (proposes) |
| `05-analytics.md` | Net worth, burn, runway projections | 3 | no (read model) |
| `06-budgeting.md` | Envelopes, safe-to-spend | 4 | no (read model) |
| `07-forecasting.md` | Recurring projection, scenarios | 4 | no (derived) |
| `08-investment.md` | Holdings, XIRR, tax lots | 5 | no (mirrors cash to ledger) |
| `09-optimization.md` | Insights / recommendations | 6 | no (read model) |

## Invariants that hold across every doc (verified in the reconciliation pass)

1. **One source of truth.** Only `ledger` owns financial truth. The only contexts that
   write it are (a) `reconciliation` (confirming proposals) and (b) `investment`
   (mirroring investment cash legs) — both exclusively via `LedgerApi.postTransaction` /
   `reverseTransaction`. No context writes ledger tables directly. Analytics, budgeting,
   forecasting, and optimization only *read* via `LedgerApi` and their own projections.
2. **Money is always `BIGINT` minor units + currency.** No floats anywhere. Investment
   quantities are integers scaled by 1e8.
3. **Projections are rebuildable** from the outbox (deterministic replay) — a tested
   property in analytics, budgeting, reconciliation.
4. **Idempotency end-to-end** — a single source row can never become two ledger
   transactions (import's deterministic proposal key flows through reconciliation into the
   ledger's idempotency key).

## Cross-context seams (reconciled)

- **import → reconciliation.** Reconciliation's trigger is import's real
  **`TransactionProposed`** event (plus `ImportBatchCompleted` as a UI signal). An earlier
  draft assumed a `StatementEntryStaged` event and different `StatementEntry` field names;
  those were reconciled to import's actual `import_statement_entry` columns (`booked_at`,
  `raw_description`, `merchant_id`, `batch_id`) and its `import_proposed_transaction`
  handoff. See the dependency note in `04-reconciliation.md`.
- **ledger → analytics / budgeting / forecasting / reconciliation.** All consume
  `TransactionPosted` / `TransactionReversed` from the outbox.
- **Category dimension** is owned by **analytics** (`05`): account-mapping primary, optional
  transaction tag. Budgeting and forecasting consume that same dimension — they do not
  define their own.
- **optimization** reads everything via other contexts' public APIs
  (`AnalyticsApi`, `BudgetingApi`, `InvestmentApi`, `LedgerApi`), never their internals.
- **Internal transfers (bank↔bank, bank↔card).** A transfer is a balanced ledger
  transaction touching only asset/liability accounts — never income/expense — so net worth
  and burn are unchanged, and card bill payments never double-count card purchases. When
  both legs are known together, it's one `TRANSFER` transaction; when legs arrive in
  separate imports, each posts as a `HALF_TRANSFER` against a per-owner
  `Transfers:In-Transit` clearing account (system `ASSET`), which **reconciliation** pairs
  and clears (stale legs → `STALE_IN_TRANSIT` discrepancy). Spans `02-ledger`, `03-import`,
  `04-reconciliation`. In-Transit is a system asset, so it is included in net worth while
  money is in flight.

## Open decisions to firm up before implementing later phases

- **Category ownership vs. tagging.** Analytics derives category from account mapping now;
  if user-level transaction tagging is wanted, decide whether the tag lives on the ledger
  transaction (affects `02`) or stays a projection-side concern. Deferred to Phase 3.
- **Auto-confirm reconciliation.** Phase 2 surfaces high-confidence matches but does not
  silently post them; auto-clear is a Phase-2+ opt-in.
- **External price feeds** (investment) and **non-CSV import adapters** are designed but
  deferred; each is additive (no schema change).
