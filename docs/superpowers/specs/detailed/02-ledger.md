# 02 — Ledger Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. The spine — the source of
financial truth. Every number anywhere in Midas ultimately derives from here.

## Responsibilities

- Immutable double-entry truth: accounts, transactions, postings.
- Balance queries + point-in-time reconstruction.
- Reversal-based corrections, transfers.
- Emits `TransactionPosted` / `TransactionReversed` to the outbox.

## Schema

```sql
CREATE TABLE account (
    id                UUID PRIMARY KEY,
    owner_id          UUID NOT NULL,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL,      -- ASSET|LIABILITY|EQUITY|INCOME|EXPENSE
    currency          CHAR(3) NOT NULL,
    parent_account_id UUID REFERENCES account(id),
    institution_id    UUID REFERENCES institution(id),
    status            TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE|ARCHIVED
    system_kind       TEXT,               -- NULL for user accounts; e.g. IN_TRANSIT (seeded, see transfers)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name, parent_account_id)
);
CREATE INDEX account_owner_type_idx ON account (owner_id, type);
-- Exactly one clearing account of each system kind per owner.
CREATE UNIQUE INDEX account_system_kind_idx ON account (owner_id, system_kind)
    WHERE system_kind IS NOT NULL;

CREATE TABLE institution (
    id       UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    name     TEXT NOT NULL,               -- HDFC, ICICI, Zerodha
    kind     TEXT NOT NULL DEFAULT 'BANK' -- BANK|BROKER|WALLET|OTHER
);

-- Append-only. No UPDATE/DELETE in app code.
CREATE TABLE ledger_transaction (
    id              UUID PRIMARY KEY,           -- v7
    owner_id        UUID NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,       -- business/effective time
    description     TEXT NOT NULL,
    reference       TEXT,
    source          TEXT NOT NULL,              -- MANUAL|IMPORT|REVERSAL|SYSTEM
    idempotency_key TEXT,
    status          TEXT NOT NULL DEFAULT 'POSTED', -- POSTED|REVERSED
    reverses_txn_id UUID REFERENCES ledger_transaction(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX txn_owner_time_idx ON ledger_transaction (owner_id, timestamp);

-- Append-only. Sum per transaction MUST equal zero.
CREATE TABLE posting (
    id              UUID PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES ledger_transaction(id),
    account_id      UUID NOT NULL REFERENCES account(id),
    amount_minor    BIGINT NOT NULL,     -- signed; debit +, credit - (see convention)
    currency        CHAR(3) NOT NULL,
    sequence_number INT NOT NULL,
    UNIQUE (transaction_id, sequence_number)
);
CREATE INDEX posting_account_idx ON posting (account_id);
CREATE INDEX posting_txn_idx ON posting (transaction_id);

-- Balance snapshots accelerate point-in-time; rebuildable from postings.
CREATE TABLE balance_snapshot (
    id           UUID PRIMARY KEY,
    owner_id     UUID NOT NULL,
    account_id   UUID NOT NULL REFERENCES account(id),
    as_of        TIMESTAMPTZ NOT NULL,        -- snapshot boundary (e.g. month end)
    balance_minor BIGINT NOT NULL,
    currency     CHAR(3) NOT NULL,
    UNIQUE (account_id, as_of)
);
```

**Sign convention:** postings signed; a balanced transaction sums to zero. Account
"natural balance" is presented per type (assets/expenses debit-normal, etc.) at the
query layer, not stored.

**DB-level invariant:** a deferred CHECK/trigger asserts `SUM(amount_minor)=0` per
`transaction_id` at commit, backing the domain invariant.

## Core operations (`LedgerApi`)

- **`postTransaction(cmd)`** — validates ≥2 postings, single currency, sum-zero, accounts
  exist and belong to owner. Inserts txn + postings + `TransactionPosted` outbox row in
  one DB transaction. Idempotent on `idempotency_key`. Rejects with
  `LEDGER_UNBALANCED_TRANSACTION` on sum≠0.
- **`reverseTransaction(txnId, reason)`** — creates a new txn with negated postings,
  `source=REVERSAL`, `reverses_txn_id` set; marks original `REVERSED`. Never mutates
  original postings. Emits `TransactionReversed`.
- **`transfer(fromAccount, toAccount, amount, at)`** — convenience that builds a 2-posting
  balanced transaction between two of the owner's own asset/liability accounts. No
  income/expense account is touched, so net worth and burn are unchanged (see "Internal
  transfers" below).
- **`systemAccount(kind)`** — resolves (lazily seeding on first use) the owner's system
  clearing account for a given `system_kind` (currently `IN_TRANSIT`). Used by the split
  transfer path.
- **`balanceAt(accountId, asOf)`** — nearest snapshot ≤ asOf + `SUM(postings)` in the gap.
  O(postings since last snapshot), not O(all history).
- **`balances(asOf)`** / **`netPosition(asOf)`** — all accounts at a point in time.
- **`ledgerFor(accountId, from, to)`** — running-balance statement for an account.

## Point-in-time reconstruction

`balanceAt` = `balance_snapshot` at greatest `as_of ≤ target` **plus** sum of postings
with `timestamp` in `(snapshot.as_of, target]`. A monthly snapshot job (outbox consumer
or scheduled) writes month-end snapshots. Correctness test: `balanceAt` with and without
snapshots must agree for random dates.

## Internal transfers & the In-Transit clearing account

A transfer moves money between two accounts the owner controls (bank↔bank, bank↔credit
card). It must never touch an income/expense account — net worth and burn stay unchanged.
Two shapes:

**1. Both legs known at once** (manual transfer, or both statements imported together) —
a single balanced transaction:
```
transfer HDFC savings → ICICI savings, ₹10,000
  debit  ICICI savings (ASSET)  +10,000
  credit HDFC savings  (ASSET)  −10,000   → sum 0, net worth unchanged
```
Credit-card payment is the same shape with a liability leg: `debit Credit Card (LIABILITY)
+8,000` (owe less), `credit HDFC savings (ASSET) −8,000`. Card *purchases* remain expenses
(booked when swiped); paying the bill is only this transfer — so spend is never
double-counted.

**2. Legs arrive separately** (source bank imported now, destination later) — each leg
posts against the per-owner **`Transfers:In-Transit`** clearing account
(`type=ASSET`, `system_kind=IN_TRANSIT`, seeded via `systemAccount(IN_TRANSIT)`):
```
Monday  — HDFC debit leg lands:
  debit  Transfers:In-Transit (ASSET) +10,000   ← money is "in flight", still an asset
  credit HDFC savings         (ASSET) −10,000   → sum 0, net worth unchanged

Friday  — ICICI credit leg lands:
  debit  ICICI savings        (ASSET) +10,000
  credit Transfers:In-Transit (ASSET) −10,000   → In-Transit back to 0, transfer complete
```
A non-zero In-Transit balance = transfers whose second leg hasn't arrived yet — a useful
"in flight" signal. The two In-Transit postings are matched and cleared by the
**Reconciliation** context; a leg that stays in-transit beyond a threshold is surfaced as a
reconciliation discrepancy (likely a real external payment mis-inferred as a transfer).
Both shapes keep every intermediate state balanced and net-worth-correct, and both are
plain append-only transactions — no special-casing of truth.

## Events emitted

- `TransactionPosted` — `{txnId, timestamp, postings:[{accountId, amountMinor, currency}],
  description, source}`.
- `TransactionReversed` — `{txnId, reversesTxnId, timestamp}`.

Consumed downstream by analytics, budgeting, forecasting, reconciliation.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/accounts` | create account |
| GET | `/api/v1/accounts` | list (owner-scoped) |
| POST | `/api/v1/transactions` | post transaction (Idempotency-Key required) |
| POST | `/api/v1/transactions/{id}/reversal` | reverse |
| GET | `/api/v1/accounts/{id}/balance?asOf=` | point-in-time balance |
| GET | `/api/v1/accounts/{id}/ledger?from=&to=` | running statement |
| GET | `/api/v1/net-worth?asOf=` | net position |

## Testing

- Property: any generated balanced transaction posts; any unbalanced one rejects.
- Immutability: no code path updates/deletes a posting or posted txn.
- Point-in-time: snapshot vs. full-scan agreement (property test over random dates).
- Reversal: original untouched, balances net to zero after reverse.
- Idempotency: same key replays, never double-posts.
- Outbox: exactly one `TransactionPosted` per successful post, same DB tx.
