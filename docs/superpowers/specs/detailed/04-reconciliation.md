# 04 — Reconciliation Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. The bridge between the
outside world (imported `StatementEntry` rows) and the source of truth (`ledger`). It
never writes truth itself — it *proposes*, a human *confirms*, and only then does it call
`LedgerApi.postTransaction`. Its own history is append-only and its every match is
**explainable**.

This context spans **Phase 2** (basic match → confirm into ledger) and deepens later
(auto-clearing, bulk reconciliation, reconciliation reports). Everything below is designed
so the Phase 2 slice and the later slices share one schema.

> **Dependency note (RECONCILED with `03-import.md`).** Import's real contract is the
> authority. Reconciliation reads it only through `ImportApi`, never its tables
> (conventions §11). The concrete seam:
> - **Trigger event is `TransactionProposed`** (emitted by import per proposal), NOT a
>   raw per-entry event. Each `TransactionProposed` carries the proposed balanced postings
>   plus the originating statement-entry linkage — enough to both (a) attempt a match
>   against an existing ledger posting and (b) offer confirm-import. Import does **not**
>   emit `StatementEntryStaged`; earlier drafts of this doc assumed one — removed.
> - **Statement-entry fields** map to `import_statement_entry`: `booked_at` (not
>   `posted_date`), `raw_description` (not `description_raw`), `merchant_id` → resolved
>   canonical merchant name via `ImportApi` (not a `merchant_normalized` string column),
>   `batch_id` (not `import_batch_id`), `amount_minor` in **statement sign** (inflow +) —
>   reconciliation converts to ledger sign for comparison. `external_ref`, `currency`,
>   `status` match as-is.
> - A proposal's postings already sum to zero in **ledger sign** (import did the
>   translation), so `confirmImportedTransaction` can pass them straight to
>   `LedgerApi.postTransaction` using the proposal's deterministic `idempotency_key`.

## Responsibilities

- **Match** ledger postings against imported `StatementEntry` rows for the same account.
- Maintain the reconciliation state of each statement entry: **pending → cleared →
  reconciled** (plus terminal `discrepancy`).
- **Explainable, confidence-scored matching**: candidate generation (amount + date window
  + account), a score in `[0,1]` with a persisted, human-readable breakdown of *why* a
  match was proposed.
- **Confirm** a proposed import transaction into the ledger — call
  `LedgerApi.postTransaction`, then record the match linking the new ledger transaction to
  the statement entry.
- **Discrepancy detection**: statement entries with no ledger match, and ledger postings
  with no statement entry (both directions), surfaced for manual workflow.
- **Manual reconciliation workflows**: accept a proposal, reject it, force a manual match,
  unmatch, or mark a discrepancy resolved.
- **Immutable reconciliation history**: `recon_match` is append-only; corrections are new
  rows that supersede prior ones, never mutations. Reconciliation **reports** read this
  history.
- Consumes `TransactionPosted` from ledger and `StatementEntry` availability from import.
  Emits `MatchProposed`, `MatchConfirmed`, `MatchRejected`, `DiscrepancyFlagged`.

## Schema

```sql
-- Per-(account, statement-entry) reconciliation state. One row per statement entry.
-- The current reconciliation *status* of the outside world's view of an account.
CREATE TABLE recon_entry_state (
    id                 UUID PRIMARY KEY,               -- v7
    owner_id           UUID NOT NULL,
    account_id         UUID NOT NULL REFERENCES account(id),
    statement_entry_id UUID NOT NULL,                  -- FK to import.StatementEntry (logical)
    status             TEXT NOT NULL DEFAULT 'PENDING',-- PENDING|CLEARED|RECONCILED|DISCREPANCY
    current_match_id   UUID,                           -- REFERENCES recon_match(id); the live match, if any
    posted_date        DATE NOT NULL,                  -- denormalized from StatementEntry for windowing
    amount_minor       BIGINT NOT NULL,                -- denormalized; signed, ledger sign convention
    currency           CHAR(3) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, statement_entry_id)
);
CREATE INDEX recon_entry_state_candidacy_idx
    ON recon_entry_state (owner_id, account_id, posted_date, status);

-- Append-only. Every proposed / confirmed / rejected / manual match is a row here.
-- Corrections are NEW rows (superseding), never UPDATEs. This is the reconciliation
-- history and the audit trail. No UPDATE/DELETE grants in app code (conventions §5),
-- except the narrow supersede/decision columns set once at decision time (see note).
CREATE TABLE recon_match (
    id                  UUID PRIMARY KEY,              -- v7 = time order
    owner_id            UUID NOT NULL,
    account_id          UUID NOT NULL REFERENCES account(id),
    statement_entry_id  UUID NOT NULL,                 -- the outside event
    ledger_txn_id       UUID REFERENCES ledger_transaction(id), -- NULL for a pure proposal not yet posted
    posting_id          UUID REFERENCES posting(id),   -- the specific matched line, when known
    match_kind          TEXT NOT NULL,                 -- AUTO|MANUAL|CONFIRMED_IMPORT|SYSTEM
    state               TEXT NOT NULL,                 -- PROPOSED|CONFIRMED|REJECTED|SUPERSEDED
    confidence          NUMERIC(4,3) NOT NULL,         -- [0.000, 1.000]
    score_breakdown     JSONB NOT NULL,                -- explainable component scores + weights (see below)
    explanation         TEXT NOT NULL,                 -- rendered human sentence ("Amount exact; date +1d; merchant 0.92")
    supersedes_match_id UUID REFERENCES recon_match(id), -- the row this correction replaces
    decided_by          TEXT,                          -- owner principal / 'SYSTEM' at decision time
    decided_at          TIMESTAMPTZ,                   -- when CONFIRMED/REJECTED
    decision_reason     TEXT,                          -- free text for MANUAL/REJECTED
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE INDEX recon_match_entry_idx  ON recon_match (statement_entry_id, id);
CREATE INDEX recon_match_txn_idx    ON recon_match (ledger_txn_id);
CREATE INDEX recon_match_owner_state_idx ON recon_match (owner_id, state, id);

-- Discrepancies: an entry (or a ledger posting) with no acceptable match. Append-only;
-- resolution is a new superseding row or a link to the match that cleared it.
CREATE TABLE recon_discrepancy (
    id                 UUID PRIMARY KEY,               -- v7
    owner_id           UUID NOT NULL,
    account_id         UUID NOT NULL REFERENCES account(id),
    kind               TEXT NOT NULL,                  -- UNMATCHED_STATEMENT|UNMATCHED_LEDGER|AMOUNT_MISMATCH|DUPLICATE|STALE_IN_TRANSIT
    statement_entry_id UUID,                           -- set for UNMATCHED_STATEMENT / AMOUNT_MISMATCH
    posting_id         UUID REFERENCES posting(id),    -- set for UNMATCHED_LEDGER / AMOUNT_MISMATCH
    detail             JSONB NOT NULL,                 -- amounts, dates, nearest-candidate info
    status             TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN|RESOLVED|IGNORED
    resolved_by_match_id UUID REFERENCES recon_match(id),
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX recon_discrepancy_open_idx ON recon_discrepancy (owner_id, account_id, status);

-- Tunable scoring config, owner-scoped so weights/thresholds can be tuned without a deploy.
-- Seeded with the defaults in "Matching algorithm" below.
CREATE TABLE recon_scoring_config (
    id                  UUID PRIMARY KEY,              -- v7
    owner_id            UUID NOT NULL,
    date_window_days    INT  NOT NULL DEFAULT 3,       -- ± candidate window (ASSUMPTION)
    weight_amount       NUMERIC(4,3) NOT NULL DEFAULT 0.500,  -- ASSUMPTION
    weight_date         NUMERIC(4,3) NOT NULL DEFAULT 0.200,  -- ASSUMPTION
    weight_description  NUMERIC(4,3) NOT NULL DEFAULT 0.300,  -- ASSUMPTION
    auto_confirm_min    NUMERIC(4,3) NOT NULL DEFAULT 0.950,  -- ≥ → eligible for auto-clear (ASSUMPTION)
    propose_min         NUMERIC(4,3) NOT NULL DEFAULT 0.600,  -- ≥ → surface as a proposal (ASSUMPTION)
    effective_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, effective_from)
);
```

**Sign & money:** `amount_minor` follows the ledger's signed convention (conventions §2),
`currency CHAR(3)` present everywhere; INR-only in Phase 2 but multi-currency-additive.
Matching compares magnitudes in the account's own currency; a currency mismatch can never
score above `propose_min` (it is a hard candidate filter).

**Append-only posture (conventions §5, §9):** `recon_match` and `recon_discrepancy` are
append-only in spirit. Rows are inserted at proposal time; the **decision** columns
(`state → CONFIRMED/REJECTED/SUPERSEDED`, `decided_*`) are the one narrow, once-only
transition allowed, and `recon_entry_state.status`/`current_match_id`/`updated_at` are the
mutable *projection* of that history (rebuildable by replaying `recon_match` in `id`
order). Truth-style immutability lives in `recon_match`: a "correction" (e.g. re-matching)
is a **new** row with `supersedes_match_id` set and the old row transitioned to
`SUPERSEDED`. History is never rewritten.

## Matching algorithm (explainable, confidence-scored)

Two-stage: cheap **candidate generation** (SQL, index-backed), then **scoring** (in-domain,
deterministic, persisted).

### Stage 1 — Candidate generation

For a `StatementEntry e` on `account A`, candidates are ledger `posting`s `p` such that:

- `p.account_id = A` (same account — hard filter),
- `p.currency = e.currency` (hard filter),
- `sign(p.amount_minor) = sign(e.amount_minor)` (a debit matches a debit),
- ledger `timestamp::date ∈ [e.posted_date − W, e.posted_date + W]` where
  `W = recon_scoring_config.date_window_days` (**ASSUMPTION: default 3 days**), and
- the posting is not already the `posting_id` of a live `CONFIRMED` match (no double-claim).

Amount is deliberately **not** required to be exact at candidate stage (statements can
round or split), but is bounded: `|p.amount_minor − e.amount_minor| ≤ amount_tolerance`
where tolerance is `max(fixed_paise, pct * |e.amount_minor|)` — **ASSUMPTION: `fixed =
0`, `pct = 0` for Phase 2 (exact amount only); relaxed later.** The index
`recon_entry_state_candidacy_idx` and ledger's `posting_account_idx` back this query.

### Stage 2 — Scoring → confidence ∈ [0,1]

Each candidate gets three component sub-scores in `[0,1]`, combined by config weights:

| Component | Sub-score `s_i ∈ [0,1]` | Rationale |
|---|---|---|
| **Amount exactness** `s_amt` | `1.0` if `p.amount == e.amount`; else `max(0, 1 − |Δ| / (tol==0 ? |e.amount| : tol))` | Exact amount is the strongest signal. |
| **Date proximity** `s_date` | `max(0, 1 − |days(p) − days(e)| / (W + 1))` | Same-day = 1; decays to 0 at the window edge. |
| **Description similarity** `s_desc` | token/Jaro-Winkler similarity of `e.merchant_normalized` (fallback `description_raw`) vs `p`'s transaction `description` | Merchant text disambiguates same-amount collisions. |

```
confidence = clamp01( w_amt·s_amt + w_date·s_date + w_desc·s_desc )
             with  w_amt + w_date + w_desc = 1
```

**ASSUMPTION (tunable, seeded in `recon_scoring_config`):** `w_amt = 0.50`, `w_date =
0.20`, `w_desc = 0.30`. Weights are owner-scoped config so they can be tuned without a
code change; the config row `id` in force is stamped into every `score_breakdown` for
reproducibility.

**Explainability — persisted, not recomputed.** For the chosen candidate the full
breakdown is stored in `recon_match.score_breakdown`:

```json
{
  "configId": "018f...-v7",
  "weights":  { "amount": 0.50, "date": 0.20, "description": 0.30 },
  "components": {
    "amount":      { "sub": 1.000, "detail": "exact: 245000 == 245000" },
    "date":        { "sub": 0.667, "detail": "ledger 2026-07-03 vs stmt 2026-07-04, +1d, window ±3" },
    "description": { "sub": 0.920, "detail": "'SWIGGY' ~ 'Swiggy Instamart' jaro-winkler 0.92" }
  },
  "confidence": 0.906,
  "candidatesConsidered": 2,
  "runnerUp": { "postingId": "018f...", "confidence": 0.31 }
}
```

`recon_match.explanation` is the one-line human render of the above
(*"Amount exact; date +1d; merchant 0.92 → 91% confidence"*). This satisfies the NFR:
every match can be explained to the user *after the fact* from stored data, with no
recomputation and no dependence on the current (possibly-retuned) weights.

**Selection & thresholds (ASSUMPTION, tunable):**

- Best candidate `confidence ≥ auto_confirm_min` (**0.95**) → eligible for auto-clear
  (Phase 2: still surfaced, not silently posted; auto-confirm is a later opt-in).
- `propose_min ≤ confidence < auto_confirm_min` (**0.60–0.95**) → `MatchProposed` for the
  user to accept/reject.
- `confidence < propose_min`, or zero candidates → no proposal; the entry stays `PENDING`
  and, after the account's statement period is closed, becomes a `recon_discrepancy` of
  kind `UNMATCHED_STATEMENT`.
- Ambiguity guard: if the top two candidates are within `ambiguity_margin` (**ASSUMPTION:
  0.05**), do not auto-confirm; force a manual choice (both stored, `runnerUp` recorded).

## Match states & transitions

**`recon_entry_state.status`** (the entry's reconciliation lifecycle):

```
                 propose+accept / manual match         confirm into ledger
   PENDING ───────────────────────────────▶ CLEARED ───────────────────────▶ RECONCILED
      │  (matched to an existing/late posting)   │ (matched posting exists)      (posting is
      │                                           │                               ledger truth,
      │  no candidate ≥ propose_min               │  unmatch / reject             match CONFIRMED)
      ▼                                           ▼
  DISCREPANCY  ◀────────────────────────────── (back to PENDING on unmatch)
```

- **PENDING** — imported, not yet matched.
- **CLEARED** — a proposed/manual match is *accepted* and links to a real ledger posting,
  but reconciliation is not finalized (e.g. awaiting statement-period close, or the
  posting itself is a to-be-confirmed import proposal).
- **RECONCILED** — terminal-happy: the statement entry is tied to a `CONFIRMED`
  `recon_match` whose `ledger_txn_id`/`posting_id` is committed ledger truth.
- **DISCREPANCY** — no acceptable match; a `recon_discrepancy` row exists.

**`recon_match.state`** (per-match, append-only): `PROPOSED → CONFIRMED`,
`PROPOSED → REJECTED`, and any live match `→ SUPERSEDED` when replaced. A match never goes
backward; reversing a decision means inserting a new superseding row.

Every transition is driven by inserting/one-time-transitioning `recon_match` rows;
`recon_entry_state` is the derived projection and is rebuildable by folding `recon_match`
history in `id` (v7 time) order — a tested determinism property (conventions §8).

## Core operations (`ReconciliationApi`)

- **`proposeMatches(accountId, window)`** — runs candidate-gen + scoring for all `PENDING`
  entries on the account, inserts `recon_match` rows in `PROPOSED` state for candidates
  `≥ propose_min`, emits one `MatchProposed` per proposal. Idempotent per
  `(statement_entry_id, posting_id)` — re-running does not duplicate a live proposal;
  a changed score supersedes the prior proposal. Fault-tolerant (conventions §9): a bad
  entry is quarantined as a discrepancy, never aborts the batch.
- **`confirmMatch(matchId, reason?)`** — accepts an existing proposal against an
  already-posted ledger posting. Marks the `recon_match` `CONFIRMED` (`decided_*` set),
  moves `recon_entry_state` to `RECONCILED` (or `CLEARED` if the posting is itself an
  unconfirmed import proposal), sets `current_match_id`, emits `MatchConfirmed`. One DB
  transaction; the state change + outbox row are co-committed.
- **`confirmImportedTransaction(cmd)`** — *the Phase 2 headline flow.* For a proposed
  import transaction that has **no** ledger posting yet: (1) calls
  `LedgerApi.postTransaction(cmd)` (single currency, sum-zero, owner-scoped — ledger
  enforces its own invariants and idempotency), then (2) records a `recon_match`
  (`match_kind=CONFIRMED_IMPORT`, `state=CONFIRMED`, `confidence=1.000`, breakdown =
  *"user-confirmed import"*) linking the new `ledger_txn_id`/`posting_id` to the
  `statement_entry_id`, and (3) sets `recon_entry_state=RECONCILED`. Emits `MatchConfirmed`.
  Idempotent via the caller's `Idempotency-Key` (conventions §7) *and* the ledger's own
  `idempotency_key`, so a retry never double-posts and never creates a second match. See
  the flow detail below.
- **`rejectMatch(matchId, reason)`** — marks the proposal `REJECTED`, leaves the entry
  `PENDING`, emits `MatchRejected`. The rejected row stays (history).
- **`manualMatch(statementEntryId, postingId, reason)`** — user forces a link the scorer
  missed/under-scored. Inserts a `CONFIRMED` `recon_match` (`match_kind=MANUAL`,
  `confidence` recorded as computed but decision is human), supersedes any live proposal,
  moves the entry to `CLEARED`/`RECONCILED`.
- **`unmatch(statementEntryId, reason)`** — supersedes the live match (new
  `SUPERSEDED`-marking row), returns the entry to `PENDING`. Never deletes the prior match.
- **`detectDiscrepancies(accountId, periodEnd)`** — for a closed statement period, opens
  `recon_discrepancy` rows: `UNMATCHED_STATEMENT` (entries still `PENDING`),
  `UNMATCHED_LEDGER` (postings in the period with no `CONFIRMED` match),
  `AMOUNT_MISMATCH`/`DUPLICATE` where detectable. Emits `DiscrepancyFlagged`. Idempotent
  per `(kind, statement_entry_id|posting_id)` while `OPEN`.
- **`resolveDiscrepancy(discrepancyId, resolution)`** — links to the match that cleared it
  or marks `IGNORED`; append-only (sets `resolved_*` once).
- **`reconciliationReport(accountId, from, to)`** — read-side over `recon_match` /
  `recon_entry_state` / `recon_discrepancy`: counts and amounts by status
  (pending/cleared/reconciled), open discrepancies, and — because history is immutable —
  a fully auditable "how did this account get reconciled" trail. Read-only; touches no
  truth.

### `confirmImportedTransaction` flow (Phase 2)

```
user reviews proposed import txn for StatementEntry e
        │
        ▼
ReconciliationApi.confirmImportedTransaction(cmd, Idempotency-Key)
        │   (one @Transactional boundary in reconciliation)
        ├─▶ LedgerApi.postTransaction(cmd)            // ledger's OWN tx: postings (truth) + TransactionPosted outbox row
        │        └─ returns ledgerTxnId, postingIds   //   (ledger enforces sum-zero, single-currency, idempotency)
        ├─▶ INSERT recon_match(state=CONFIRMED, match_kind=CONFIRMED_IMPORT,
        │        confidence=1.000, ledger_txn_id, posting_id, statement_entry_id, explanation='user-confirmed import')
        ├─▶ UPDATE recon_entry_state → RECONCILED, current_match_id=<new>
        └─▶ OutboxWriter.append(MatchConfirmed)       // same reconciliation DB tx
```

**Idempotency & failure posture:** the outer `Idempotency-Key` (platform store) makes the
whole confirm replay-safe; the ledger's `idempotency_key` guarantees the *posting* side is
posted at-most-once even if reconciliation retries after a partial failure. If
`postTransaction` succeeds but the reconciliation commit fails, the retry finds the ledger
txn already exists (idempotent) and only (re)writes the match — no duplicate posting. Fail
closed on the ledger call, fault-tolerant on the batch around it (conventions §9).

## Discrepancy handling

Two directions, both first-class:

1. **Statement entry with no ledger match** (`UNMATCHED_STATEMENT`): a real-world event we
   have no book entry for → surfaced so the user confirms it into the ledger (which turns
   it into `confirmImportedTransaction`) or marks it ignorable.
2. **Ledger posting with no statement entry** (`UNMATCHED_LEDGER`): the books claim money
   moved but the statement doesn't show it → surfaced for review (manual entry, timing gap
   spanning the period, or an error to reverse in the ledger — reconciliation never mutates
   the ledger, it only flags).
3. **Amount/duplicate** (`AMOUNT_MISMATCH`, `DUPLICATE`): a near-match outside tolerance,
   or the same external event imported twice → flagged with nearest-candidate detail in
   `detail` JSONB so the resolution is explainable.

`recon_discrepancy` is append-only; a discrepancy is *resolved* by pointing at the
`recon_match` that later cleared it (`resolved_by_match_id`) or by an explicit `IGNORED`
decision, never by deletion — the report must always be able to show what was once out of
balance.

## Clearing split transfers (In-Transit)

When the two legs of an internal transfer arrive in different imports, each is confirmed as
a `HALF_TRANSFER` that posts one leg against the owner's `Transfers:In-Transit` clearing
account (see `03-import.md` and `02-ledger.md`). Reconciliation owns closing the loop:

- **Pairing.** On each `TransactionPosted` carrying an `IN_TRANSIT` posting, look for an
  outstanding opposite-sign, equal-magnitude In-Transit posting within
  `date_window_days × [ASSUMPTION: in_transit_multiplier = 3]` (transfer settlement can lag
  more than a single statement). On a match, record a `recon_match`
  (`match_kind=SYSTEM`, `state=CONFIRMED`, explanation *"in-transit transfer legs paired"*)
  linking the two postings as one logical transfer for reporting. The ledger's In-Transit
  balance already nets to zero once both legs exist; this pairing is the *explainable record*
  of which legs cleared which, not a truth mutation.
- **Stale legs.** An In-Transit posting still unpaired after
  `[ASSUMPTION: in_transit_stale_days = 10]` opens a `STALE_IN_TRANSIT` discrepancy — the
  second leg never arrived, which usually means the first leg was **not** actually a
  transfer (e.g. a real external payment mis-inferred). Resolution: the user reclassifies it
  (reverse the In-Transit posting in the ledger and re-book against the correct
  expense/party) or supplies the missing leg. Reconciliation only flags; it never mutates
  the ledger.
- A persistently non-zero In-Transit balance is therefore always explainable as the sum of
  open `STALE_IN_TRANSIT` discrepancies plus genuinely-in-flight (within-window) transfers.

## Immutable reconciliation history

- `recon_match` and `recon_discrepancy` are append-only (conventions §5): no `DELETE`, and
  the only mutations are the single, monotonic decision transitions (`PROPOSED →
  CONFIRMED/REJECTED/SUPERSEDED`, `OPEN → RESOLVED/IGNORED`) each set exactly once. Enforced
  by the append-only DB role (`INSERT, SELECT` + the guarded decision columns) plus domain
  guards.
- A **correction** (re-matching, un-matching, re-scoring) always creates a **new** row with
  `supersedes_match_id` pointing at the row it replaces; the superseded row stays visible.
  The full lineage of any statement entry's reconciliation is `SELECT * FROM recon_match
  WHERE statement_entry_id = ? ORDER BY id` (v7 = chronological).
- `recon_entry_state` is a **projection** of that history and is rebuildable: dropping it
  and folding `recon_match` in `id` order reproduces identical current state — a tested
  determinism property (conventions §8, §10).

## Events consumed / emitted (outbox)

**Consumed** (via the platform dispatcher; consumers idempotent on `(consumer_name,
event_id)`, conventions §6):

- **`TransactionPosted`** (from ledger) — a new posting exists; trigger candidate
  re-evaluation for that account/date so late-arriving ledger entries can clear pending
  statement entries, and to detect `UNMATCHED_LEDGER` discrepancies.
- **`TransactionProposed`** (from import — the real event, per `03-import.md`) — a new
  proposed balanced transaction exists for a staged statement entry. Reconciliation seeds
  a `recon_entry_state` `PENDING` row keyed on the proposal's `statementEntryId`, then runs
  `proposeMatches`: if a strong candidate already exists in the ledger (e.g. the user booked
  it manually), it proposes a **clearing match**; otherwise it surfaces the proposal for
  **confirm-import** (which posts it). One event drives both paths.
- **`ImportBatchCompleted`** (from import) — a batch is ready for review; used only to
  signal the UI/report that a set of proposals has fully landed. No state change on its own.

**Emitted** (transactional outbox, same DB tx as the state change, conventions §6):

- **`MatchProposed`** — `{matchId, statementEntryId, ledgerTxnId?, postingId?, confidence,
  scoreBreakdown, accountId}`.
- **`MatchConfirmed`** — `{matchId, statementEntryId, ledgerTxnId, postingId, matchKind,
  confidence}`.
- **`MatchRejected`** — `{matchId, statementEntryId, reason}`.
- **`DiscrepancyFlagged`** — `{discrepancyId, accountId, kind, statementEntryId?,
  postingId?, detail}`.

All payloads carry `schemaVersion` and tolerate unknown fields (conventions §6). Downstream
(analytics, later a reconciliation dashboard) consumes these; reconciliation itself never
writes truth.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/reconciliation/accounts/{accountId}/propose` | run candidate-gen + scoring, create proposals |
| GET | `/api/v1/reconciliation/accounts/{accountId}/proposals` | list live proposals (cursor-paginated) |
| GET | `/api/v1/reconciliation/matches/{matchId}` | one match + full `scoreBreakdown` (explainability) |
| POST | `/api/v1/reconciliation/matches/{matchId}/confirm` | confirm an existing proposal |
| POST | `/api/v1/reconciliation/matches/{matchId}/reject` | reject a proposal (reason) |
| POST | `/api/v1/reconciliation/confirm-import` | confirm proposed import txn → `LedgerApi.postTransaction` + record match (Idempotency-Key required) |
| POST | `/api/v1/reconciliation/manual-match` | force `{statementEntryId, postingId}` link |
| POST | `/api/v1/reconciliation/entries/{statementEntryId}/unmatch` | supersede live match, back to PENDING |
| GET | `/api/v1/reconciliation/accounts/{accountId}/discrepancies` | list open discrepancies |
| POST | `/api/v1/reconciliation/discrepancies/{id}/resolve` | resolve / ignore a discrepancy |
| GET | `/api/v1/reconciliation/accounts/{accountId}/report?from=&to=` | reconciliation report |

Conventions apply (conventions §7): `/api/v1`, JSON, cursor pagination, money as
`{amountMinor, currency}`, ISO-8601 UTC, RFC 9457 `application/problem+json` with stable
`code`s — e.g. `RECON_MATCH_NOT_FOUND`, `RECON_MATCH_ALREADY_DECIDED`,
`RECON_POSTING_ALREADY_MATCHED`, `RECON_CURRENCY_MISMATCH`,
`RECON_ENTRY_ALREADY_RECONCILED`. `confirm-import` and any POST creating financial state
require `Idempotency-Key`.

## Testing

- **Scoring determinism & explainability:** for fixed inputs + config, `confidence` and
  `score_breakdown` are exactly reproducible; the stored breakdown re-renders to the same
  `explanation` string. Property test over generated (amount, date-delta, description)
  triples that each sub-score stays in `[0,1]` and `confidence ∈ [0,1]`.
- **Candidate generation:** entries outside the date window / wrong sign / wrong currency
  never appear as candidates; exact-amount same-day entry always tops the ranking.
- **Ambiguity guard:** two near-equal candidates (within `ambiguity_margin`) never
  auto-confirm; both are persisted with a `runnerUp`.
- **`confirmImportedTransaction` (integration, Testcontainers Postgres):** confirms exactly
  one `LedgerApi.postTransaction` call, one posting set, one `recon_match`, one
  `MatchConfirmed` outbox row, all in the co-committed transactions; a forced failure after
  the ledger post leaves no orphan match and the retry does not double-post (idempotency).
- **Immutability / history:** no code path `UPDATE`s a `recon_match` beyond its single
  decision transition or `DELETE`s one; a correction produces a new superseding row and the
  original stays; lineage query returns full chronological history.
- **Projection rebuild (conventions §8):** dropping `recon_entry_state` and folding
  `recon_match` in `id` order reproduces identical current state (deterministic replay).
- **Discrepancy detection:** unmatched-statement and unmatched-ledger are both raised for a
  crafted closed period; resolution links the clearing match and never deletes the row.
- **Consumers idempotent (conventions §6):** redelivered `TransactionPosted` /
  `TransactionProposed` produce no duplicate state (dedup on `(consumer_name, event_id)`).
- **API contract tests:** each endpoint — happy path, validation (422), and error `code`
  (e.g. confirming an already-decided match → `RECON_MATCH_ALREADY_DECIDED`).
```
