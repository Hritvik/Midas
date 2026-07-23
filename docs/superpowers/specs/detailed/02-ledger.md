# 02 — Ledger Context (Detailed Design)

**Amended 2026-07-23**

**Inherits:** `00-conventions.md`. **Parent:** the design spec. The spine — the source of
financial truth. Every number anywhere in Midas ultimately derives from here.

## Audit fixes applied

- **[CRITICAL] Reversal UPDATE on append-only table** → removed mutable `status` column from
  `ledger_transaction`; `REVERSED` is now derived from the existence of a row with
  `reverses_txn_id = id` (§Schema, §reverseTransaction). Conventions §5.
- **[CRITICAL] Reversal timestamp** → a reversal's business `timestamp` = correction time
  (now), never the original's; `TransactionReversed` carries the correction timestamp
  (§reverseTransaction, §Events). Conventions §10.
- **[HIGH] "Deferred CHECK" not implementable** → replaced with a `DEFERRABLE INITIALLY
  DEFERRED CONSTRAINT TRIGGER` asserting `SUM(amount_minor)=0`; empty/single-posting
  transactions forbidden at the DB layer (§DB-level invariant). Conventions §5.
- **[HIGH] Reversing an already-reversed txn / a reversal** → `UNIQUE(owner_id,
  reverses_txn_id)` + guard: only a POSTED, non-reversal, not-already-reversed txn may be
  reversed (§Schema, §reverseTransaction). Conventions §10.
- **[HIGH] Parent/child rollup ambiguity** → postings hit **leaf** accounts only; parents are
  pure aggregation; `balances`/`netPosition` iterate leaves (§Schema, §Core operations).
  Conventions §9.
- **[MEDIUM] `netPosition`/`/net-worth` "all accounts"** → type-scoped to the canonical
  net-worth formula (conventions §9) (§Core operations, §REST).
- **[MEDIUM] `posting.currency` vs `account.currency`** → enforced equal; sum-zero invariant
  is now per `(transaction_id, currency)` (§Schema, §DB-level invariant).
- **[MEDIUM] Concurrent same-key `postTransaction`** → unique-violation is treated as replay:
  look up and return the existing txn (§postTransaction). Conventions §7.
- **[MEDIUM] Archived accounts** → require balance=0 to archive **and** forbid new postings to
  ARCHIVED accounts; they stay in `balances` (§Schema, §Core operations).
- **Reserved seams (conventions §12)** → nullable account financial-product metadata columns
  and a `manual_valuation` mark seam added (design now, full build later) (§Schema, §Reserved
  seams).
- **[HIGH] Snapshot half-open boundary + backdated/lagging invalidation (audit 02-HIGH, known
  #2)** → snapshot set is `timestamp ≤ as_of`, gap is `(as_of, target]`; snapshots record
  `captured_max_created_at` and a `stale` flag; a backdated or late-committed posting marks
  affected snapshots stale for lazy recompute (§Schema, §Point-in-time reconstruction).

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
    -- Reserved seams (conventions §12) — nullable financial-product metadata. Design now,
    -- full build in the liabilities/illiquid work. Detectors MUST suppress (not default-to-0)
    -- when a value is absent.
    rate_bps          INT,                -- interest rate, basis points (loans/deposits)
    tenure_months     INT,                -- product tenure
    credit_limit_minor BIGINT,            -- card/credit limit, minor units
    yield_bps         INT,                -- expected/quoted yield, basis points
    statement_day     SMALLINT,           -- day-of-month statement cuts (cards)
    due_day           SMALLINT,           -- day-of-month payment due (cards/loans)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name, parent_account_id)
);
CREATE INDEX account_owner_type_idx ON account (owner_id, type);
-- Exactly one clearing account of each system kind per owner.
CREATE UNIQUE INDEX account_system_kind_idx ON account (owner_id, system_kind)
    WHERE system_kind IS NOT NULL;

-- Reserved seam (conventions §12): illiquid-asset marks feeding §9's unrealized-gain term.
-- Design now, full build in the illiquid/liabilities work. NOT yet wired into balanceAt
-- (which stays cost-basis truth); §9 net worth reads the latest mark ≤ asOf per account.
CREATE TABLE manual_valuation (
    id          UUID PRIMARY KEY,
    owner_id    UUID NOT NULL,
    account_id  UUID NOT NULL REFERENCES account(id),
    as_of       TIMESTAMPTZ NOT NULL,     -- mark date (business time)
    value_minor BIGINT NOT NULL,          -- marked value, minor units
    currency    CHAR(3) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, as_of)
);

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
    -- No mutable status column (conventions §5: append-only rows are never UPDATEd).
    -- A txn is REVERSED iff a row exists with reverses_txn_id = this id.
    reverses_txn_id UUID REFERENCES ledger_transaction(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, idempotency_key),
    -- At most one reversal per original (blocks double-negation / two concurrent reversals).
    UNIQUE (owner_id, reverses_txn_id)
);
CREATE INDEX txn_owner_time_idx ON ledger_transaction (owner_id, timestamp);

-- Append-only. Sum per transaction MUST equal zero.
CREATE TABLE posting (
    id              UUID PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES ledger_transaction(id),
    account_id      UUID NOT NULL REFERENCES account(id),
    amount_minor    BIGINT NOT NULL,     -- signed; debit +, credit - (see convention)
    currency        CHAR(3) NOT NULL,    -- MUST equal account.currency (enforced, see below)
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
    as_of        TIMESTAMPTZ NOT NULL,        -- snapshot boundary (owner-local month end)
    balance_minor BIGINT NOT NULL,
    currency     CHAR(3) NOT NULL,
    captured_max_created_at TIMESTAMPTZ NOT NULL, -- max(posting.created_at) summed into this
                                                  -- snapshot; a later-created posting with
                                                  -- timestamp <= as_of makes it stale (see
                                                  -- Point-in-time reconstruction)
    stale        BOOLEAN NOT NULL DEFAULT FALSE,  -- set on a backdated/lagging insert; lazily recomputed
    UNIQUE (account_id, as_of)
);
```

**Sign convention:** postings signed; a balanced transaction sums to zero. Account
"natural balance" is presented per type (assets/expenses debit-normal, etc.) at the
query layer, not stored.

**Leaf-only postings (audit 02-HIGH, conventions §9):** postings may hit **leaf** accounts
only — an account that is the `parent_account_id` of another account MUST NOT be posted to.
Parent accounts are pure aggregation nodes; their balance is the sum over their leaf subtree,
computed at the query layer. This makes per-type sums (balances, net worth) iterate leaves and
never double-count a parent that also carries direct postings. `postTransaction` rejects a
posting to a non-leaf account with `LEDGER_POSTING_TO_PARENT`.

**DB-level invariant (audit 02-HIGH / 02-MEDIUM, conventions §5):** a `DEFERRABLE INITIALLY
DEFERRED CONSTRAINT TRIGGER` asserts, at commit, that `SUM(amount_minor) = 0` per
`(transaction_id, currency)` — deferred because postings are inserted one row at a time and
only the committed set must balance. This is a `CONSTRAINT TRIGGER`, **not** a CHECK: Postgres
CHECKs cannot defer and cannot aggregate across sibling rows. The same trigger rejects a
transaction with **fewer than two** postings (an empty or single-posting txn header would
otherwise trivially satisfy or never fire the sum check), so degenerate transactions fail at
the DB layer, not only in the domain. Because the invariant is grouped by currency, a
mixed-currency set that nets to zero numerically (+100 USD, −100 INR) is rejected rather than
silently accepted.

**Currency enforcement (audit 02-MEDIUM):** `posting.currency` MUST equal its
`account.currency`. Enforced by an AFTER-INSERT trigger on `posting` (a plain FK can't reach
the referenced account's column). Combined with the per-currency sum-zero above, a single
transaction is single-currency and each posting matches its account.

## Core operations (`LedgerApi`)

- **`postTransaction(cmd)`** — validates ≥2 postings, single currency, sum-zero, each
  `posting.currency = account.currency`, accounts exist, belong to owner, are **ACTIVE**
  (not ARCHIVED) and are **leaf** accounts (not a parent). Inserts txn + postings +
  `TransactionPosted` outbox row in one DB transaction. Idempotent on `idempotency_key`.
  Rejects with `LEDGER_UNBALANCED_TRANSACTION` on sum≠0, `LEDGER_POSTING_TO_PARENT` on a
  non-leaf account, `LEDGER_ACCOUNT_ARCHIVED` on an archived account.
  **Concurrent same-key (audit 02-MEDIUM):** two near-simultaneous calls with the same
  `Idempotency-Key` both attempt the INSERT; `UNIQUE(owner_id, idempotency_key)` lets exactly
  one commit. The loser catches the unique violation (23505) and treats it as a **replay** —
  it looks up the winning txn by `(owner_id, idempotency_key)` and returns it (retrying the
  lookup briefly until the winner's commit is visible), never surfacing a raw 23505 or
  double-posting. (Conventions §7 reserve-first covers the platform-level lock.)
- **`reverseTransaction(txnId, reason)`** — creates a **new** txn with negated postings,
  `source=REVERSAL`, `reverses_txn_id = txnId`, and business `timestamp = correction time
  (now)` — **never** the original's, so point-in-time history before the correction is
  unchanged (conventions §10). Never mutates the original txn or its postings; `REVERSED` is
  derived from this new row's existence, not stored. **Guard:** the target must be `POSTED`
  (i.e. not itself already reversed — no row has `reverses_txn_id = txnId`) and
  non-reversal (`source <> 'REVERSAL'`); otherwise reject with `LEDGER_NOT_REVERSIBLE`. The
  `UNIQUE(owner_id, reverses_txn_id)` constraint is the hard backstop against two concurrent
  reversals of the same original. Emits `TransactionReversed` carrying the correction
  timestamp.
- **`transfer(fromAccount, toAccount, amount, at)`** — convenience that builds a 2-posting
  balanced transaction between two of the owner's own asset/liability accounts. No
  income/expense account is touched, so net worth and burn are unchanged (see "Internal
  transfers" below).
- **`systemAccount(kind)`** — resolves (lazily seeding on first use) the owner's system
  clearing account for a given `system_kind` (currently `IN_TRANSIT`). Used by the split
  transfer path.
- **`balanceAt(accountId, asOf)`** — nearest snapshot ≤ asOf + `SUM(postings)` in the gap.
  O(postings since last snapshot), not O(all history).
- **`balances(asOf)`** — per-leaf-account balance at a point in time (parents rolled up by
  summing their leaf subtree). ARCHIVED accounts remain included (their balance is 0 by the
  archive rule below, so they neither vanish from nor distort net worth).
- **`netPosition(asOf)`** — the canonical net worth of conventions §9, **not** a naive sum of
  all accounts (which is identically 0 since every txn sums to zero, and would fold in
  INCOME/EXPENSE flows and the EQUITY plug):
  > **net worth(asOf) = Σ balance(ASSET) − Σ balance(LIABILITY) + Σ unrealizedGain(holdings)**

  computed over **leaf** ASSET/LIABILITY accounts. Investment assets sit at **cost basis** in
  the ledger; market appreciation is added exactly once as `Σ unrealizedGain` (from
  `InvestmentApi`; illiquid/manually-valued assets contribute `mark − cost` via the
  `manual_valuation` seam). INCOME/EXPENSE/EQUITY are excluded. See conventions §9.
- **`ledgerFor(accountId, from, to)`** — running-balance statement for an account.

**Archived accounts (audit 02-MEDIUM):** an account may be archived (`status=ARCHIVED`) **only
when its balance is 0**; archival is rejected otherwise (`LEDGER_ARCHIVE_NONZERO_BALANCE`).
ARCHIVED accounts accept **no new postings** (`postTransaction` rejects them). Because their
balance is 0, keeping them in `balances`/`netPosition` neither drops net worth on archive nor
lets an archived account carry hidden value. Correcting an old txn is still done by posting a
reversal against the still-ACTIVE accounts before archiving.

## Point-in-time reconstruction

`balanceAt(account, target)` = the `balance_snapshot` at the greatest `as_of ≤ target`
**plus** the sum of postings with business `timestamp ∈ (snapshot.as_of, target]`. A snapshot
captures postings with `timestamp ≤ as_of`; the gap interval is half-open on the left so the
two never overlap or leave a hole. O(postings since last snapshot), not O(all history). A
monthly job (outbox consumer or scheduled) writes owner-local month-end snapshots.

**Snapshots are a cache and MUST be invalidated by out-of-order writes (audit 02-HIGH, known
#2).** A snapshot at `as_of` is only valid if every posting with business `timestamp ≤ as_of`
was already committed when it was computed. Two everyday cases violate that:

- **Backdated posting** — today you record a transaction with a past business `timestamp`
  (forgot last month's rent), landing *before* existing snapshots.
- **Normal entry lag** — a posting for `2026-01-31` commits on `Feb 1`, after the Jan-31
  month-end snapshot job already read its inputs.

Both are detected by comparing a posting's business `timestamp` against existing snapshot
`as_of`s, **not** by trusting snapshot recency. Each snapshot row therefore records the
`max(created_at)` (system time) of the postings it summed; `balanceAt` treats a snapshot as
**stale** — and recomputes from the previous valid snapshot (or genesis) — if any posting with
`timestamp ≤ as_of` has a later `created_at` than the snapshot captured. Concretely: **on any
insert whose business `timestamp` precedes an existing snapshot's `as_of`, mark all snapshots
for that account with `as_of ≥ timestamp` stale**; they are lazily recomputed on next read (or
by the snapshot job). Reversals never trigger this (their business time is correction-time =
now, §10), so only genuine backdated/lagging inserts invalidate.

Correctness tests: (1) `balanceAt` with and without snapshots agrees for random dates; (2) a
backdated insert before an existing snapshot yields the same `balanceAt(≥ that date)` as a
full-scan-from-genesis; (3) a posting committed after its own month-end snapshot still appears
in every later `balanceAt`.

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
- `TransactionReversed` — `{txnId, reversesTxnId, timestamp}` where `txnId` is the new
  reversing transaction and `timestamp` is the **correction time** (the reversal's business
  time, = now), never the original's. Consumers derive the original's REVERSED state from this
  event / the `reverses_txn_id` row, and apply the correction from `timestamp` forward.

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
| GET | `/api/v1/net-worth?asOf=` | net worth = Σ ASSET − Σ LIABILITY + Σ unrealizedGain (conventions §9); not a raw all-accounts sum |

## Reserved seams (conventions §12 — design now, build later)

These exist in the schema so the future **liabilities** and **illiquid-asset** work has a real
source, but the ledger does not yet act on them:

- **Account financial-product metadata** — nullable `rate_bps`, `tenure_months`,
  `credit_limit_minor`, `yield_bps`, `statement_day`, `due_day` on `account`. Consumed later by
  the loan model and the `IDLE_CASH`/`DEBT_OPT` detectors; a detector MUST **suppress** (not
  default-to-zero) when a value is absent.
- **`manual_valuation(account_id, as_of, value_minor)`** — a mark for illiquid / manually
  valued assets (real estate, gold, EPF/PPF/NPS). Feeds the `Σ unrealizedGain` term of the §9
  net-worth formula as `mark − cost`; it does **not** enter `balanceAt`, which stays cost-basis
  truth. Full valuation-vs-cost reconciliation is deferred to the illiquid-asset context.

## Testing

- Property: any generated balanced transaction posts; any unbalanced one rejects.
- Immutability: no code path updates/deletes a posting or posted txn (no `status` to mutate).
- Sum-zero trigger: deferred `CONSTRAINT TRIGGER` accepts a balanced multi-row insert and
  rejects sum≠0, an empty txn, a single-posting txn, and a mixed-currency net-zero set.
- Currency: a posting whose currency ≠ its account's currency is rejected.
- Leaf-only: posting to a parent account rejects with `LEDGER_POSTING_TO_PARENT`.
- Point-in-time: snapshot vs. full-scan agreement (property test over random dates).
- Reversal: original untouched; `REVERSED` derived from the reversing row; reversal `timestamp`
  = correction time so `balanceAt(before-correction)` is unchanged; a second reversal of the
  same txn and reversing a reversal both reject; `UNIQUE(owner_id, reverses_txn_id)` holds.
- Net worth: `netPosition` = ASSET − LIABILITY + unrealizedGain over leaves; excludes
  INCOME/EXPENSE/EQUITY.
- Archived: archiving a non-zero account rejects; an ARCHIVED account rejects new postings and
  still appears (balance 0) in `balances`.
- Idempotency: same key replays, never double-posts; concurrent same-key → one commit, the
  loser returns the winning txn (no raw 23505).
- Outbox: exactly one `TransactionPosted` per successful post, same DB tx.
