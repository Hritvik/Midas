# 03 — Import & Aggregation Context (Detailed Design)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 2** (priority
#2). Turns messy external financial data into clean, deduplicated, normalized **proposed
transactions** — but never writes ledger truth. Its output is staging + proposals; the
Reconciliation context confirms them into the ledger.

**Hard boundary (from the parent spec):** imports NEVER call anything that mutates truth.
This context ends at a `proposed_transaction` row. Confirmation, matching against existing
ledger rows, and the eventual `LedgerApi.postTransaction` call are the **Reconciliation
context's** job — referenced here, not implemented here.

## Responsibilities

- **Statement ingestion** via pluggable **source adapters** behind one interface. Phase 2
  ships the **CSV adapter only**; bank-statement / SMS / email / Account-Aggregator /
  broker adapters are designed as deferred implementations of the same interface.
- **Import batch/job lifecycle**: `upload → parse → stage → dedup → normalize → infer →
  propose`, resumable and idempotent at every step.
- **StatementEntry staging**: one immutable-after-parse row per external financial event.
- **Duplicate detection**: content-hash based, both within a batch and against prior
  batches — re-uploading the same file must not double-import.
- **Merchant normalization**: raw descriptor → canonical merchant, via a rule table + a
  normalized-merchant table.
- **Recurring-payment detection**: heuristic grouping of entries into recurring series.
- **Transfer inference**: match opposite-signed entries across the owner's accounts into a
  single proposed transfer.
- **Fault tolerance**: a bad row **quarantines** and never aborts the batch. Every stage is
  retry-safe and resumable.
- Emits `ImportBatchCompleted` / `TransactionProposed` to the outbox for Reconciliation.

**Out of scope (this context):** confirming a proposal, matching a proposal to an existing
ledger transaction, and posting to the ledger. Those are Reconciliation.

> **Tunable assumptions.** Every heuristic threshold below is tagged
> `[ASSUMPTION: <name> = <default>]`. All live in a single `import_tuning` config
> (platform config, owner-scopable later), not in code, so they can be tuned without a
> migration.

## Schema

```sql
-- Raw uploaded file + batch header. One row per upload attempt.
-- content_sha256 is the idempotency anchor: same bytes = same logical import.
CREATE TABLE import_batch (
    id                UUID PRIMARY KEY,             -- v7
    owner_id          UUID NOT NULL,
    source_type       TEXT NOT NULL,                -- CSV|BANK_STATEMENT|SMS|EMAIL|AA|BROKER
    adapter_id        TEXT NOT NULL,                -- e.g. 'csv.hdfc.savings.v1'
    account_id        UUID REFERENCES account(id),  -- target account, if known at upload
    filename          TEXT,
    content_sha256    TEXT NOT NULL,                -- SHA-256 of raw bytes
    byte_size         BIGINT NOT NULL,
    idempotency_key   TEXT,                         -- from Idempotency-Key header
    status            TEXT NOT NULL DEFAULT 'UPLOADED',
        -- UPLOADED|PARSING|PARSED|STAGED|DEDUPED|NORMALIZED|INFERRED|PROPOSED
        -- |COMPLETED|FAILED
    last_stage        TEXT,                         -- resumability cursor: last stage DONE
    total_rows        INT  NOT NULL DEFAULT 0,
    staged_rows       INT  NOT NULL DEFAULT 0,
    duplicate_rows    INT  NOT NULL DEFAULT 0,
    quarantined_rows  INT  NOT NULL DEFAULT 0,
    proposed_txns     INT  NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Re-uploading identical bytes for the same owner is a no-op replay, not a new import.
    UNIQUE (owner_id, content_sha256)
);
CREATE INDEX import_batch_owner_status_idx ON import_batch (owner_id, status);

-- Raw bytes kept out of the hot table (may be large). One row per batch.
CREATE TABLE import_raw_file (
    batch_id     UUID PRIMARY KEY REFERENCES import_batch(id),
    owner_id     UUID NOT NULL,
    content      BYTEA NOT NULL,                    -- or an object-store URI in later phases
    media_type   TEXT NOT NULL,                     -- text/csv, application/pdf, ...
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One external financial event in staging. NOT ledger truth.
-- Frozen after PARSED except for stage-owned columns (dedup/normalize/infer results).
CREATE TABLE import_statement_entry (
    id                UUID PRIMARY KEY,             -- v7
    owner_id          UUID NOT NULL,
    batch_id          UUID NOT NULL REFERENCES import_batch(id),
    account_id        UUID REFERENCES account(id),  -- resolved target account
    row_number        INT  NOT NULL,                -- 1-based position in source file
    -- Parsed canonical fields:
    booked_at         TIMESTAMPTZ NOT NULL,         -- value/transaction date (business time)
    amount_minor      BIGINT NOT NULL,              -- signed: credit +, debit - (inflow +)
    currency          CHAR(3) NOT NULL DEFAULT 'INR',
    raw_description    TEXT NOT NULL,               -- verbatim descriptor from source
    counterparty_ref   TEXT,                        -- UPI VPA / account tail / ref no if present
    external_ref       TEXT,                        -- bank's own txn/reference id if present
    running_balance_minor BIGINT,                   -- statement's stated balance, if present
    -- Dedup:
    dedup_hash        TEXT NOT NULL,                -- see "Duplicate detection"
    status            TEXT NOT NULL DEFAULT 'STAGED',
        -- STAGED|DUPLICATE|QUARANTINED|NORMALIZED|INFERRED|PROPOSED
    -- Normalization result:
    merchant_id       UUID REFERENCES import_merchant(id),
    -- Inference grouping:
    recurring_series_id UUID REFERENCES import_recurring_series(id),
    transfer_group_id UUID,                         -- links the two legs of an inferred transfer
    proposed_txn_id   UUID REFERENCES import_proposed_transaction(id),
    raw_columns       JSONB NOT NULL,               -- original cells, for audit/quarantine replay
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, dedup_hash)                   -- DB-level duplicate guard (see note)
);
CREATE INDEX ise_batch_idx      ON import_statement_entry (batch_id);
CREATE INDEX ise_owner_acct_idx ON import_statement_entry (owner_id, account_id, booked_at);
CREATE INDEX ise_merchant_idx   ON import_statement_entry (merchant_id);

-- A row that failed parse/validation. The batch continues; this is the audit trail.
CREATE TABLE import_quarantine (
    id           UUID PRIMARY KEY,                  -- v7
    owner_id     UUID NOT NULL,
    batch_id     UUID NOT NULL REFERENCES import_batch(id),
    row_number   INT  NOT NULL,
    raw_columns  JSONB NOT NULL,                    -- verbatim, so it can be fixed & replayed
    reason_code  TEXT NOT NULL,                     -- UNPARSEABLE_DATE|BAD_AMOUNT|MISSING_FIELD|...
    reason_detail TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX quarantine_batch_idx ON import_quarantine (batch_id);

-- Canonical merchant. Normalized descriptors collapse to one of these.
CREATE TABLE import_merchant (
    id             UUID PRIMARY KEY,                -- v7
    owner_id       UUID NOT NULL,
    canonical_name TEXT NOT NULL,                   -- 'Amazon', 'Swiggy', 'HDFC Bank'
    category_hint  TEXT,                            -- 'SHOPPING','FOOD','TRANSFER' (advisory only)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, canonical_name)
);

-- Normalization rules: raw descriptor pattern -> canonical merchant.
-- Ordered by priority; first match wins. Owner-scoped user rules + shared seed rules.
CREATE TABLE import_merchant_rule (
    id           UUID PRIMARY KEY,                  -- v7
    owner_id     UUID NOT NULL,
    match_type   TEXT NOT NULL,                     -- EXACT|CONTAINS|REGEX|PREFIX
    pattern      TEXT NOT NULL,                     -- matched against normalized raw_description
    merchant_id  UUID NOT NULL REFERENCES import_merchant(id),
    priority     INT  NOT NULL DEFAULT 100,         -- lower = evaluated first
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX merchant_rule_lookup_idx ON import_merchant_rule (owner_id, enabled, priority);

-- A detected recurring series (rent, SIP, Netflix). Advisory grouping over entries.
CREATE TABLE import_recurring_series (
    id             UUID PRIMARY KEY,                -- v7
    owner_id       UUID NOT NULL,
    merchant_id    UUID REFERENCES import_merchant(id),
    account_id     UUID REFERENCES account(id),
    cadence        TEXT NOT NULL,                   -- WEEKLY|MONTHLY|QUARTERLY|YEARLY|IRREGULAR
    typical_amount_minor BIGINT NOT NULL,
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    amount_tolerance_bp  INT NOT NULL,              -- band used to group (basis points)
    last_seen_at   TIMESTAMPTZ,
    confidence     NUMERIC(4,3) NOT NULL DEFAULT 0, -- 0..1
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX recurring_owner_merchant_idx ON import_recurring_series (owner_id, merchant_id);

-- A proposed double-entry transaction. This is the context's OUTPUT.
-- It is NOT posted. Reconciliation reads these, matches, and (on user confirm)
-- calls LedgerApi.postTransaction. proposed_txn is never itself ledger truth.
CREATE TABLE import_proposed_transaction (
    id              UUID PRIMARY KEY,               -- v7
    owner_id        UUID NOT NULL,
    batch_id        UUID NOT NULL REFERENCES import_batch(id),
    kind            TEXT NOT NULL,                  -- SINGLE|TRANSFER|HALF_TRANSFER
    timestamp       TIMESTAMPTZ NOT NULL,           -- effective business time
    description     TEXT NOT NULL,                  -- normalized (merchant) description
    merchant_id     UUID REFERENCES import_merchant(id),
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0,-- 0..1, drives review ordering
    status          TEXT NOT NULL DEFAULT 'PROPOSED',
        -- PROPOSED  (handed to reconciliation; terminal for THIS context)
    idempotency_key TEXT NOT NULL,                  -- deterministic; see idempotency section
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX proposed_batch_idx ON import_proposed_transaction (batch_id);

-- Proposed legs (mirror of ledger posting shape, so reconciliation can post as-is).
-- Sum per proposed_txn MUST equal zero, same invariant as ledger.
CREATE TABLE import_proposed_posting (
    id                UUID PRIMARY KEY,             -- v7
    proposed_txn_id   UUID NOT NULL REFERENCES import_proposed_transaction(id),
    account_id        UUID REFERENCES account(id),  -- NULL = "suggest a category account"
    suggested_account_hint TEXT,                    -- e.g. 'Expense:Food' when account_id NULL
    amount_minor      BIGINT NOT NULL,              -- signed; debit +, credit -
    currency          CHAR(3) NOT NULL DEFAULT 'INR',
    sequence_number   INT NOT NULL,
    UNIQUE (proposed_txn_id, sequence_number)
);
```

**Sign convention.** `import_statement_entry.amount_minor` uses **statement sign** (inflow
`+`, outflow `−`) — the natural reading of a bank statement. `import_proposed_posting` uses
the **ledger posting sign** (debit `+`, credit `−`, sum-zero) so Reconciliation can post it
without re-deriving. The propose stage does this translation once.

**DB-level guards.** `UNIQUE (owner_id, dedup_hash)` makes cross-batch duplicate insertion
impossible at the storage layer (the app also checks first, to route duplicates to
`status=DUPLICATE` gracefully instead of erroring). A deferred CHECK asserts
`SUM(amount_minor)=0` per `proposed_txn_id`, mirroring the ledger invariant so no unbalanced
proposal can be created.

**Staging is not truth.** None of these tables are append-only ledger tables; they carry no
ledger grants. They can be re-derived by re-running stages from `import_raw_file`, which is
the resumability contract.

## Source adapters (the pluggable seam)

One interface, many implementations. **Phase 2 implements `CsvSourceAdapter` only.**

```
SourceAdapter                       // package com.midas.import.adapter
  String adapterId()                // 'csv.hdfc.savings.v1'
  SourceType type()                 // CSV | BANK_STATEMENT | SMS | EMAIL | AA | BROKER
  boolean supports(RawFile file)    // sniff media type / shape
  Stream<ParsedRow> parse(RawFile)  // lazy; one ParsedRow per source record
                                    // ParsedRow = canonical fields OR a ParseError
```

- `ParsedRow` is either a canonical entry (booked_at, amount_minor, currency,
  raw_description, refs) or a `ParseError(rowNumber, reasonCode, rawColumns)` → quarantine.
- The parser is a **lazy stream**: a bad row yields a `ParseError` and parsing continues.
  Nothing aborts the batch.
- **CSV adapter specifics:** column mapping is driven by a per-`adapter_id` profile (which
  column is date / description / debit / credit / balance, the date format, the decimal
  format). Rupee decimals are parsed to paise via `Money` (never float). Separate
  debit/credit columns collapse to one signed `amount_minor`.
- **Deferred adapters** (designed, not built): `BankStatementAdapter` (PDF/table
  extraction), `SmsAdapter` (regex over bank SMS templates), `EmailAdapter` (parse
  transaction-alert emails), `AccountAggregatorAdapter` (RBI AA FIP JSON), `BrokerAdapter`
  (contract-note / holdings statements). Each is a drop-in `SourceAdapter`; adding one is a
  new class + profile, **no schema change** — that is the point of this seam.

## The import pipeline

Stages are ordered, each idempotent, each advancing `import_batch.last_stage`. A crashed or
retried batch **resumes from `last_stage`**, never restarts from zero. Each stage reads its
inputs from the DB (not from memory), so a fresh process can pick up any in-flight batch.

1. **upload** — store raw bytes (`import_raw_file`) + `import_batch` header, compute
   `content_sha256`. If `(owner_id, content_sha256)` already exists → return the existing
   batch (idempotent replay, HTTP 200 with the original batch). `status=UPLOADED`.
2. **parse** — pick the adapter (explicit `adapter_id` or `supports()` sniff), stream
   `ParsedRow`s. Canonical rows → next stage buffer; `ParseError`s → `import_quarantine`.
   `status=PARSED`, `total_rows`/`quarantined_rows` set.
3. **stage** — insert `import_statement_entry` rows (compute `dedup_hash`). `status=STAGED`.
4. **dedup** — mark entries whose `dedup_hash` collides (within batch or against any prior
   entry for the owner) as `status=DUPLICATE`; they carry forward for audit but are excluded
   from inference/propose. `status=DEDUPED`, `duplicate_rows` set.
5. **normalize** — resolve `merchant_id` for each non-duplicate entry via the rule table.
   `status=NORMALIZED`.
6. **infer** — run **transfer inference** then **recurring detection** over the batch's
   non-duplicate entries, setting `transfer_group_id` / `recurring_series_id`.
   `status=INFERRED`.
7. **propose** — build `import_proposed_transaction` + `import_proposed_posting` (sum-zero),
   translate sign, set deterministic `idempotency_key`, compute `confidence`. Emit one
   `TransactionProposed` outbox event per proposal. `status=PROPOSED`.
8. **complete** — emit `ImportBatchCompleted` (summary counts). `status=COMPLETED`.

Each stage runs in its own DB transaction and writes an outbox row / advances `last_stage`
in that same transaction, so partial progress is durable and re-entrant. Re-running a
completed stage is a no-op (rows already have the target status).

## Duplicate detection

**`dedup_hash` = SHA-256 of a canonical tuple:**
`owner_id | account_id | booked_at (date, day precision) | amount_minor | currency |
normalized(raw_description) | external_ref`.

- If the source provides a bank `external_ref` (many bank CSVs do), it dominates — two rows
  with the same `external_ref` for the same account are the same event.
- When `external_ref` is absent, the `(date, amount, normalized description)` triple is the
  identity. `booked_at` is truncated to **day precision** `[ASSUMPTION: dedup_date_precision
  = DAY]` because the same transaction can carry slightly different timestamps across
  re-exports.
- `normalized(raw_description)`: uppercase, collapse whitespace, strip volatile tokens
  (trailing reference numbers, dates embedded in the descriptor) before hashing.
- **Scope:** the `UNIQUE (owner_id, dedup_hash)` constraint makes duplicates impossible to
  insert twice across the owner's entire history, so re-uploading last month's overlapping
  statement only stages the genuinely-new rows. The app pre-checks and routes collisions to
  `status=DUPLICATE` rather than relying on the constraint throwing.
- **Whole-file idempotency** is a separate, cheaper guard: `import_batch.content_sha256`
  short-circuits an identical re-upload before any parsing.

Edge case — genuine same-day same-amount repeats (two identical ₹50 coffees, no
`external_ref`): these would collide. Mitigation: include `row_number` **only** as a
tie-break in a `dedup_hash_v2` when the source has no `external_ref` and the same triple
appears N times in one file `[ASSUMPTION: allow_intraday_repeats = true]`; the Nth
occurrence gets an occurrence-index salt so legitimate repeats survive while cross-file
re-imports still collapse.

## Merchant normalization

Two-table approach: **rules** (`import_merchant_rule`) map raw descriptors to **canonical
merchants** (`import_merchant`).

- Input is `normalized(raw_description)` (same normalization as dedup).
- Rules are evaluated in ascending `priority`; **first match wins**. `match_type` supports
  `EXACT`, `PREFIX`, `CONTAINS`, `REGEX`.
- Seed rules ship for common Indian descriptors (UPI/`@`-VPA patterns, `NEFT`/`IMPS`/`ACH`
  prefixes, big merchants). User-created rules are owner-scoped and take priority over seed
  rules `[ASSUMPTION: user_rule_priority_offset = -50]` (lower number = earlier).
- **No match** → `merchant_id` stays NULL; the entry still proposes, using `raw_description`
  as the description and lower `confidence`. Normalization never blocks a proposal.
- Learning loop (deferred): when a user renames a proposal's merchant in Reconciliation, we
  can auto-suggest a new `import_merchant_rule` — designed for, not built in Phase 2.

## Recurring-payment detection

Heuristic, advisory only (sets `recurring_series_id`; drives UI grouping and forecasting
later — never gates a proposal).

Group non-duplicate entries by `(merchant_id [or normalized descriptor if NULL],
sign-of-amount)`. Within a group, a **recurring series** is detected when:

- **≥ N occurrences** across the owner's history `[ASSUMPTION: recurring_min_occurrences =
  3]`.
- **amounts within a band**: each within `amount_tolerance_bp` of the group median
  `[ASSUMPTION: recurring_amount_tolerance_bp = 500]` (±5%). Fixed-amount series (rent, SIP)
  land inside; variable bills (utilities) are marked `IRREGULAR` cadence but still grouped.
- **regular interval**: median gap between consecutive occurrences maps to a cadence bucket
  (WEEKLY≈7d, MONTHLY≈30d, QUARTERLY≈91d, YEARLY≈365d) within a tolerance
  `[ASSUMPTION: recurring_interval_tolerance_days = 4]`. Gaps that don't fit → `IRREGULAR`.

`confidence` = fraction of the interval/amount checks that pass, blended
`[ASSUMPTION: recurring_confidence_weights = {interval:0.5, amount:0.3, count:0.2}]`. Series
are persistent across batches (matched by merchant+account), so each new statement extends
an existing series rather than re-detecting from scratch.

## Transfer inference

Detect when one entry is the **other leg** of a movement between two of the **owner's own
accounts** (e.g. Savings → Credit-card payment), so it proposes as a single balanced
`TRANSFER` instead of two unrelated single-sided entries.

For each pair of non-duplicate entries `(a, b)` across **different** `account_id`s of the
same owner, they are inferred as a transfer when **all** hold:

- **Opposite signs**, **equal magnitude**: `a.amount_minor == -b.amount_minor`
  `[ASSUMPTION: transfer_amount_exact = true]` (allow a small band only if a known
  FX/fee-free assumption is relaxed later).
- **Close in time**: `|a.booked_at − b.booked_at| ≤ transfer_window`
  `[ASSUMPTION: transfer_window_days = 3]` (settlement lag between debit and credit legs).
- **Both accounts belong to the owner** (asset/liability), resolved via `LedgerApi` account
  lookup (read-only) — never inferring a transfer to an external merchant.
- **Descriptor corroboration (optional booster)**: matching counterparty ref / account tail
  raises confidence but is not required `[ASSUMPTION: transfer_ref_match_bonus = 0.2]`.

On a match: both entries get a shared `transfer_group_id`, and the propose stage emits **one**
`kind=TRANSFER` proposal with two postings (debit one account, credit the other) instead of
two singles. If more than two entries could pair, choose the closest-in-time exact-magnitude
partner (greedy, nearest first).

**Cross-batch / cross-statement legs (the common real case).** The two legs of a transfer
often arrive in *different* imports (HDFC statement today, ICICI statement next week), so
in-batch pairing finds only one leg. An unpaired entry is classified two ways:

- **Confidently a transfer leg** — the descriptor/counterparty indicates an own-account
  movement (UPI/IMPS/NEFT to another of the owner's accounts, matching account tail/VPA, or
  a user rule tagging it as a transfer) but no in-batch partner exists → propose
  `kind=HALF_TRANSFER`: two postings, one against the real account and one against the
  owner's `Transfers:In-Transit` clearing account (`LedgerApi.systemAccount(IN_TRANSIT)`),
  sum-zero. Net worth stays correct while the money is "in flight".
- **Not identifiable as a transfer** → `kind=SINGLE` as before (an expense/income proposal
  the user can still reclassify as a transfer).

When the *other* leg arrives in a later batch, it is proposed the same way (its own
`HALF_TRANSFER` against In-Transit). **Reconciliation** then matches the two In-Transit
postings and clears the clearing account to zero — see `04-reconciliation.md`. This keeps
every intermediate state balanced without stateful cross-batch pairing inside import.
`[ASSUMPTION: transfer_leg_evidence = descriptor/counterparty/user-rule]`.

## Idempotency

Three layers, so no path can double-import:

1. **File level:** `UNIQUE (owner_id, content_sha256)` on `import_batch` — identical bytes
   re-uploaded return the existing batch, no re-parse.
2. **Request level:** the upload POST honors the platform `Idempotency-Key` header via
   `IdempotencyService` (scope `import`) — a retried HTTP call replays the original response.
3. **Entry / proposal level:** `UNIQUE (owner_id, dedup_hash)` on entries, and a
   **deterministic** `import_proposed_transaction.idempotency_key` =
   `hash(owner_id | sorted proposed posting tuples | timestamp-day)`. Re-running the propose
   stage produces the same key → no duplicate proposal. Crucially, this same key is what
   Reconciliation ultimately passes to `LedgerApi.postTransaction`, so even the final ledger
   post is idempotent end-to-end: **the same source row can never become two ledger
   transactions.**

## Core operations (`ImportApi`)

- **`upload(cmd)`** — `cmd = {sourceType, adapterId?, accountId?, filename, bytes}`. Stores
  raw file + batch, computes `content_sha256`, returns `ImportBatchView`. Idempotent on
  content hash and on `Idempotency-Key`. Kicks off (or resumes) the pipeline.
- **`runPipeline(batchId)`** — advances the batch through stages from `last_stage`;
  resumable, retry-safe. Invoked inline (small files) or by a `@Scheduled` worker. Each
  stage isolated; a stage failure marks the batch `FAILED` with a reason but leaves prior
  stages intact for retry — it never rolls back staged data.
- **`getBatch(batchId)`** — status + counts (`total/staged/duplicate/quarantined/proposed`).
- **`listEntries(batchId, cursor, limit)`** — staged entries, incl. duplicates &
  quarantines, for review.
- **`listProposals(batchId, cursor, limit)`** — proposed transactions with postings,
  merchant, confidence — the handoff surface Reconciliation reads.
- **`requeueQuarantine(quarantineId, correctedRow)`** — user fixes a bad row; it is
  re-parsed and staged into the same batch. Idempotent (dedup still applies).
- **`reparse(batchId, adapterId)`** — re-run parse with a corrected adapter profile (e.g.
  wrong column mapping) without a new upload; supersedes prior staged entries for the batch.

All write operations are owner-scoped via `OwnerScopedRepository`. `ImportApi` is the only
public surface; parsers, dedup, normalization, and inference are package-private.

## Events consumed / emitted (via the outbox)

**Consumed:** none required for the Phase 2 happy path. (Optionally consumes
`TransactionPosted` later to strengthen dedup against already-posted ledger rows and to feed
the merchant-rule learning loop — deferred.)

**Emitted** (transactional outbox, same DB tx as the state change; `schemaVersion` on every
payload):

- **`TransactionProposed`** — `{proposedTxnId, batchId, kind, timestamp, description,
  merchantId, confidence, postings:[{accountId, suggestedAccountHint, amountMinor,
  currency}], idempotencyKey}`. Consumed by **Reconciliation** to match & confirm.
- **`ImportBatchCompleted`** — `{batchId, sourceType, totals:{rows, staged, duplicates,
  quarantined, proposed}}`. Consumed by Reconciliation / UI to signal a batch is ready for
  review.

Import emits proposals; it never emits anything that mutates the ledger. The `TransactionPosted`
event stays owned by the Ledger and is produced only after a user confirms in Reconciliation.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/imports` | upload a file (multipart), `Idempotency-Key` required → batch |
| GET | `/api/v1/imports/{batchId}` | batch status + counts |
| POST | `/api/v1/imports/{batchId}/run` | resume/advance the pipeline (retry-safe) |
| GET | `/api/v1/imports/{batchId}/entries?cursor=&limit=` | staged entries (incl. dup/quarantine) |
| GET | `/api/v1/imports/{batchId}/proposals?cursor=&limit=` | proposed transactions |
| GET | `/api/v1/imports/{batchId}/quarantine?cursor=&limit=` | quarantined rows |
| POST | `/api/v1/imports/{batchId}/quarantine/{id}/requeue` | fix + re-stage a bad row |
| GET | `/api/v1/merchants` | list canonical merchants (owner-scoped) |
| POST | `/api/v1/merchant-rules` | create a normalization rule |

Errors use RFC 9457 `problem+json` with stable `code`s, e.g. `IMPORT_UNKNOWN_ADAPTER`,
`IMPORT_UNSUPPORTED_FILE`, `IMPORT_DUPLICATE_FILE` (surfaced as a 200 replay, not an error),
`IMPORT_BATCH_NOT_FOUND`, `IMPORT_ROW_UNPARSEABLE` (per-row, in quarantine, never a batch
failure). Confirming/posting a proposal is **not** an endpoint here — it lives in the
Reconciliation context.

## Testing

- **Idempotency (the headline NFR):** re-uploading the identical file → same batch, zero new
  entries, zero new proposals. Re-running any stage → no duplicate rows. Same source row can
  never yield two proposals (deterministic proposal key), and thus never two ledger txns.
- **Fault tolerance:** a batch with N good + M malformed rows stages exactly N entries,
  quarantines exactly M, and reaches `COMPLETED` — the bad rows never abort the batch.
- **Resumability:** kill the process mid-pipeline (after each stage); a fresh run resumes
  from `last_stage` and produces byte-identical output to an uninterrupted run.
- **Dedup:** overlapping statements (month N and N+1 sharing days) stage only the net-new
  rows; genuine intraday repeats survive per the occurrence-salt rule.
- **Merchant normalization:** rule precedence (user > seed, priority order, first-match),
  no-match leaves `merchant_id` NULL and still proposes.
- **Recurring detection:** property tests over synthetic series (rent, SIP, Netflix, noisy
  utility) assert correct cadence/confidence at the default thresholds; below-threshold
  series are not falsely grouped.
- **Transfer inference:** exact opposite-leg pairs within the window infer one `TRANSFER`
  proposal (sum-zero); near-misses (amount off, out of window, external counterparty) fall
  back to two `SINGLE` proposals.
- **Proposal shape:** every proposal's postings sum to zero (DB CHECK + domain test); sign
  translation from statement to ledger convention is correct.
- **Persistence:** Testcontainers Postgres — real constraints (`UNIQUE (owner_id,
  dedup_hash)`, sum-zero CHECK) enforced.
- **Outbox:** exactly one `TransactionProposed` per proposal and one `ImportBatchCompleted`
  per batch, each in the same DB transaction as the state change; at-least-once delivery,
  idempotent consumer.
- **Boundary:** no code path in this context calls `LedgerApi.postTransaction` or writes any
  ledger table — asserted by an architecture test (import module cannot reference ledger
  write APIs).
- **Adapter seam:** CSV adapter against real sample bank CSVs (HDFC/ICICI/SBI shapes); a
  stub second adapter proves a new `SourceAdapter` needs no schema change.
