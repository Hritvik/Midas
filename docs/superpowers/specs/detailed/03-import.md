# 03 — Import & Aggregation Context (Detailed Design)

**Amended 2026-07-23** (audit remediation, Tier 0/1 — see "Audit fixes applied" at end).

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
    amount_minor      BIGINT NOT NULL,              -- signed owner-cash-flow: inflow +, outflow -
                                                    -- (account-type-aware: card charge = outflow -)
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
-- (priority, id) tail gives a deterministic tie-break for equal-priority matches (audit MEDIUM).
CREATE INDEX merchant_rule_lookup_idx ON import_merchant_rule (owner_id, enabled, priority, id);

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
    idempotency_key TEXT NOT NULL,                  -- deterministic; INCLUDES source-entry
                                                    -- occurrence identity (see idempotency section)
    source_entry_ids UUID[] NOT NULL,               -- the import_statement_entry row(s) this
                                                    -- proposal derives from (occurrence identity)
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

**Sign convention (account-type-aware — audit HIGH).** `import_statement_entry.amount_minor`
is normalized to a **cash-flow sign relative to the OWNER** (money into the owner's net
position `+`, money out `−`), NOT to the raw column sign of the source. `import_proposed_posting`
uses the **ledger posting sign** (debit `+`, credit `−`, sum-zero) so Reconciliation can post
it without re-deriving. The propose stage does this translation once — but it is **not one
global rule**; it is keyed on the **target account's type (asset vs liability)**:

- **Asset accounts (bank/savings):** statement is asset-centric. A credit column is an
  inflow (`+`), a debit column an outflow (`−`) — the natural bank reading.
- **Liability accounts (credit cards):** statement is liability-centric and **inverted**. A
  **charge/purchase increases what you owe and typically prints as a positive "Amount"**, yet
  it is money *out* of the owner's net position (`−`); a **payment/refund reduces the balance**
  and is money *in* (`+`). A card purchase MUST propose as `Expense +2000 / Card(liability)
  −2000` (you owe more), never as phantom income.

Adapter profiles therefore declare their **column polarity per account type**: a
liability/credit-card profile marks its charge column and payment column so the parser emits
the correct owner-cash-flow sign at parse time. An implementer following a single global
sign rule would mis-sign every card import — this is called out explicitly (see §CSV adapter
specifics).

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
  - **Column polarity (account-type-aware sign — audit HIGH):** the profile declares the
    target account type (`ASSET|LIABILITY`) and, for the amount column(s), which is
    charge/outflow vs payment/inflow. **Liability/credit-card profiles invert** the raw
    column sign so a positive "Amount" charge becomes an owner outflow (`−`). See §Sign
    convention.
  - **RFC-4180 reader + continuation lines (audit MEDIUM):** the CSV reader is strict
    RFC-4180 (honours quoted fields with embedded newlines), so a wrapped RTGS/NEFT narration
    is one record, not two. For hand-massaged exports that break quoting, the profile may
    declare a **continuation-line rule** — a physical line with no date/amount is merged into
    the prior record's descriptor before staging, instead of quarantining the tail as
    `MISSING_FIELD`.
  - **Balance/summary row filter (audit MEDIUM):** the profile declares a **row filter** that
    recognises Opening/Closing/`B/F`/summary rows (a date + `running_balance` but empty
    debit/credit). These are **non-transaction rows**: they are skipped (their
    `running_balance_minor` may still seed reconciliation) and are **never staged as
    proposable entries**. A zero-`amount_minor` balance row is treated as non-proposable, not
    a degenerate all-zero proposal and not `BAD_AMOUNT` quarantine noise on every file.
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
   `ParsedRow`s. The profile's **row filter drops non-transaction rows** (opening/closing/
   summary balance rows) and its RFC-4180 reader / continuation-line rule keeps wrapped
   narrations as one record. Canonical rows → next stage buffer; `ParseError`s →
   `import_quarantine`. `status=PARSED`, `total_rows`/`quarantined_rows` set.
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
   translate sign **account-type-aware** (asset vs liability), record `source_entry_ids`, set
   deterministic `idempotency_key` **including the source-entry occurrence identity**, resolve
   the category account hint via the shared `CategoryResolver`, compute `confidence`.
   Zero-amount / non-transaction rows are **non-proposable** (never emitted). Emit one
   `TransactionProposed` outbox event per proposal. `status=PROPOSED`.
8. **complete** — emit `ImportBatchCompleted` (summary counts). `status=COMPLETED`.

Each stage runs in its own DB transaction and writes an outbox row / advances `last_stage`
in that same transaction, so partial progress is durable and re-entrant. Re-running a
completed stage is a no-op (rows already have the target status). **Exception:** a row
introduced *after* the batch reached `COMPLETED` (a requeued quarantine fix) must not be
stranded by this no-op — `requeueQuarantine` rewinds `last_stage` or runs the per-entry
stages inline (see §Core operations).

## Duplicate detection

**`dedup_hash` has two mutually-exclusive forms** (a `dedup_variant` discriminator is stored
alongside so the two never collide):

- **`external_ref` present → hash on `(owner_id, account_id, external_ref)` ALONE.** The bank
  reference genuinely dominates: description and amount are **dropped** from the tuple so a
  re-export with a corrected/cleaned descriptor (`"UPI/PAYTM/REF98213/QR"` →
  `"UPI-Paytm Payments-REF98213"`) or a corrected amount (₹1,000 → ₹1,050) for the same
  `external_ref` still collapses to the one event (audit HIGH — the old formula concatenated
  all fields, so `external_ref` did *not* dominate).
- **`external_ref` absent → hash the `(owner_id, account_id, civil-day, amount_minor,
  currency, normalized(raw_description))` tuple** — the descriptor/amount/date identity is the
  only signal available.
- **Civil-day truncation is OWNER-LOCAL, not UTC (conventions §3).** `booked_at` is truncated
  to the owner's local civil day (`Asia/Kolkata`) before hashing `[ASSUMPTION:
  dedup_date_precision = DAY]`. A 2026-07-01 00:20 IST txn stored as `2026-06-30T18:50Z` and a
  date-only re-export parsed as `2026-07-01T00:00` must both bucket to the **IST** day
  2026-07-01; truncating in UTC lands them in different buckets and double-imports the same
  event (audit HIGH). Only the no-`external_ref` path uses the day at all.
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
re-imports still collapse. **This occurrence salt (the same `row_number`/occurrence-index
identity) is the entry's "occurrence identity" and MUST also flow into the proposal
`idempotency_key`** (see §Idempotency layer 3) — otherwise dedup preserves both repeats but
the proposal key collapses them again.

## Merchant normalization

Two-table approach: **rules** (`import_merchant_rule`) map raw descriptors to **canonical
merchants** (`import_merchant`).

- Input is `normalized(raw_description)` (same normalization as dedup).
- Rules are evaluated in ascending `priority`; **first match wins**. Priority ties are broken
  **deterministically by `(priority, id)`** (audit MEDIUM) — never storage/plan order — so
  normalization output (and hence `merchant_id`, proposal `description`, `confidence`) is
  reproducible, preserving the byte-identical resumability guarantee. `match_type` supports
  `EXACT`, `PREFIX`, `CONTAINS`, `REGEX`.
- Seed rules ship for common Indian descriptors (UPI/`@`-VPA patterns, `NEFT`/`IMPS`/`ACH`
  prefixes, big merchants). User-created rules are owner-scoped and take priority over seed
  rules `[ASSUMPTION: user_rule_priority_offset = -50]` (lower number = earlier).
- **No match** → `merchant_id` stays NULL; the entry still proposes, using `raw_description`
  as the description and lower `confidence`. Normalization never blocks a proposal.
- Learning loop (deferred): when a user renames a proposal's merchant in Reconciliation, we
  can auto-suggest a new `import_merchant_rule` — designed for, not built in Phase 2.

**Category is not a local notion (conventions §12).** Import owns *merchant*, never
*category*. When the propose stage needs a category account hint (the `suggested_account_hint`
on a proposed posting, e.g. `Expense:Food`), it calls the shared **`CategoryResolver`** port
owned by the `category/` context — passing the resolved merchant / descriptor — rather than
inventing categories here. `import_merchant.category_hint` is retained only as an **advisory
input to that resolver**, not an authoritative category; there is no per-context `category
TEXT`. A `CategoryReassigned` event in the shared context re-homes historical rollups without
import doing anything.

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

**Recurring-obligation registry seam (conventions §12, reserved seam #3).** A detected
`import_recurring_series` is *import's detection of* an obligation, not a private notion.
Import **detects into** the canonical `obligation` interface (cadence, amount, counterparty,
next-due, usage signal) that forecasting projects, budgeting reserves, and optimization
audits — replacing the four partial notions the audit called out. Import owns detection and
publishes the series; it does not re-model the registry.

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
- **Descriptor / counterparty corroboration is REQUIRED (audit HIGH), not an optional
  booster.** At least one of the paired entries must carry own-account evidence — a
  counterparty VPA / account tail that resolves to the *other* leg's account, an
  own-account IMPS/NEFT/UPI marker, or a user rule tagging it as a transfer
  `[ASSUMPTION: transfer_ref_match_required = true; transfer_ref_match_bonus = 0.2]`.
  **Without corroboration the pair is NOT inferred as a transfer** — propose two `SINGLE`s
  (the user can still reclassify). Rationale: opposite-sign + equal-magnitude + within-window
  alone coincidentally matches a real ₹10k expense out of HDFC against an unrelated ₹10k debt
  repayment into ICICI; auto-collapsing them to a TRANSFER erases a real expense *and* a real
  income from P&L while net worth stays coincidentally correct, and the user rubber-stamps it.
  The same guard prevents a refund landing in a different account from being mis-booked as a
  transfer with the original expense never reversed.

On a match: both entries get a shared `transfer_group_id`, and the propose stage emits **one**
`kind=TRANSFER` proposal with two postings (debit one account, credit the other) instead of
two singles. If more than two entries could pair, choose the closest-in-time exact-magnitude
partner **that also corroborates** (greedy, nearest first).

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
   `hash(owner_id | source-entry occurrence identity | sorted proposed posting tuples |
   timestamp-day)`. The **source-entry occurrence identity** is the same occurrence-salt / row
   identity (the `source_entry_ids`, i.e. the `dedup_hash_v2` occurrence-index) that dedup uses
   to keep genuine same-day repeats distinct (audit CRITICAL). Without it, two legitimate ₹500
   coffees — or two identical ₹10,000 HALF_TRANSFERs on one day — have identical posting tuples
   and identical `timestamp-day`, compute the **same** key, and the second proposal is silently
   collapsed by `UNIQUE (owner_id, idempotency_key)` with no quarantine, no event, and a ledger
   permanently short. Including the occurrence identity makes genuinely-distinct entries yield
   distinct keys while a *re-run of the same entry* stays stable. Crucially, this same key is
   what Reconciliation ultimately passes to `LedgerApi.postTransaction`, so even the final
   ledger post is idempotent end-to-end: **the same source row can never become two ledger
   transactions, and two genuine same-day repeats are never collapsed into one.**

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
  re-parsed and staged into the same batch. Idempotent (dedup still applies). **On a
  `COMPLETED` batch it MUST drive the fixed row through the remaining per-entry stages
  (audit HIGH):** because the pipeline resumes from `last_stage` and "re-running a completed
  stage is a no-op", a freshly-staged row would otherwise sit in `STAGED` forever and never
  be proposed — silent loss under a misleadingly "COMPLETED" batch. So `requeueQuarantine`
  either **rewinds `last_stage`/batch status** (to `STAGED`, so `runPipeline` re-drives
  dedup→normalize→infer→propose) **or runs the per-entry stages inline** for just that entry
  (normalize→infer→propose + emit `TransactionProposed`). Batch counts are updated
  accordingly.
- **`reparse(batchId, adapterId)`** — re-run parse with a corrected adapter profile (e.g.
  wrong column mapping) without a new upload; supersedes prior staged entries for the batch.
  **Guard (audit CRITICAL — no double-import over confirmed ledger truth):** `reparse` is
  **forbidden once any proposal in the batch has been emitted or confirmed** — it returns
  `409 IMPORT_REPARSE_FORBIDDEN`. Reparse changes `amount_minor`/descriptors, so both the
  `dedup_hash` and the proposal `idempotency_key` change; nothing suppresses the new
  proposals, and the ledger txns from the first (wrong) confirmation are immutable — so a
  naive reparse would leave the ledger holding both the wrong-signed and right-signed
  transactions. Reparse is therefore only allowed **before any proposal is emitted**; to
  correct an already-proposed/confirmed batch, the fix must instead **emit explicit
  supersession events that Reconciliation acts on** (reverse the mis-posted txns, then
  re-propose) — never a silent re-import. See handoff to `04-reconciliation.md`.

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
failure), `IMPORT_REPARSE_FORBIDDEN` (409 — `reparse` attempted after a proposal was
emitted/confirmed; see §Core operations). Confirming/posting a proposal is **not** an
endpoint here — it lives in the Reconciliation context.

## Testing

- **Idempotency (the headline NFR):** re-uploading the identical file → same batch, zero new
  entries, zero new proposals. Re-running any stage → no duplicate rows. Same source row can
  never yield two proposals (deterministic proposal key), and thus never two ledger txns.
  **Same-day repeats not collapsed (audit CRITICAL):** two genuine ₹500 coffees (and two
  identical same-day ₹10k HALF_TRANSFERs), no `external_ref`, produce **two** distinct
  proposals with distinct `idempotency_key`s (occurrence identity in the key), both reaching
  Reconciliation — never silently collapsed.
- **`reparse` guard (audit CRITICAL):** `reparse` after any proposal is emitted/confirmed →
  `409 IMPORT_REPARSE_FORBIDDEN`; allowed only pre-proposal. No double-import over confirmed
  ledger txns.
- **Fault tolerance:** a batch with N good + M malformed rows stages exactly N entries,
  quarantines exactly M, and reaches `COMPLETED` — the bad rows never abort the batch.
- **Resumability:** kill the process mid-pipeline (after each stage); a fresh run resumes
  from `last_stage` and produces byte-identical output to an uninterrupted run.
- **Dedup:** overlapping statements (month N and N+1 sharing days) stage only the net-new
  rows; genuine intraday repeats survive per the occurrence-salt rule. **`external_ref`
  dominates (audit HIGH):** a re-export with a corrected descriptor or amount but the same
  `external_ref` collapses (hash on `(owner, account, external_ref)` alone). **Owner-local
  civil day (audit HIGH):** a full-timestamp export and a date-only re-export of a
  post-midnight-UTC / pre-midnight-IST event dedup to the same IST day and collapse.
- **Merchant normalization:** rule precedence (user > seed, priority order, first-match),
  **deterministic `(priority, id)` tie-break** on equal priority (audit MEDIUM),
  no-match leaves `merchant_id` NULL and still proposes.
- **Non-transaction rows (audit MEDIUM):** opening/closing/summary balance rows are filtered
  at parse (not staged, not quarantined, not proposed); wrapped RFC-4180 / continuation-line
  narrations parse as one record.
- **Account-type-aware sign (audit HIGH):** a credit-card CSV (liability profile) imports a
  positive-column purchase as `Expense +2000 / Card −2000` (you owe more), never as phantom
  income; a bank CSV keeps the asset-centric reading.
- **Requeue on a COMPLETED batch (audit HIGH):** a fixed quarantine row flows through
  normalize→infer→propose and emits `TransactionProposed` — never left `STAGED` forever.
- **Recurring detection:** property tests over synthetic series (rent, SIP, Netflix, noisy
  utility) assert correct cadence/confidence at the default thresholds; below-threshold
  series are not falsely grouped.
- **Transfer inference:** exact opposite-leg pairs within the window **with own-account
  corroboration** infer one `TRANSFER` proposal (sum-zero); near-misses (amount off, out of
  window, external counterparty) **and uncorroborated equal-and-opposite coincidences**
  (audit HIGH — real expense out of one account vs unrelated income into another) fall back
  to two `SINGLE` proposals so neither the expense nor the income is erased.
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

## Audit fixes applied

**Amended 2026-07-23** — remediation of `2026-07-23-audit-and-opportunities.md` / audit
`03-import.md`. Each finding maps to the sections changed:

| # | Sev | Finding | Fix location |
|---|-----|---------|--------------|
| 1 | CRITICAL | Proposal `idempotency_key` collapsed legitimate same-day repeats (two ₹500 coffees, two identical HALF_TRANSFERs) | §Idempotency layer 3 (key now includes source-entry occurrence identity); §Schema (`source_entry_ids`, key comment); §Duplicate detection edge case; §propose stage; §Testing |
| 2 | CRITICAL | `reparse` re-imported over already-confirmed ledger truth (no coordination) | §Core operations `reparse` (forbidden once a proposal is emitted/confirmed → `409`; else emit supersession events); §REST error codes (`IMPORT_REPARSE_FORBIDDEN`); §Testing |
| 3 | HIGH | Dedup: `external_ref` did not actually dominate (hash concatenated all fields); day truncated in UTC | §Duplicate detection (two mutually-exclusive hash forms — `external_ref` alone vs the no-ref triple; owner-local civil-day truncation per conventions §3); §Testing |
| 4 | HIGH | Transfer inference erased a real expense + income on coincidental equal-and-opposite flows | §Transfer inference (descriptor/counterparty corroboration now REQUIRED; else two `SINGLE`s); §Testing |
| 5 | HIGH | Credit-card CSV sign inversion → spend imported as income | §Sign convention (account-type-aware, asset vs liability); §CSV adapter specifics (column polarity per profile); §Schema (`amount_minor` comment); §propose stage; §Testing |
| 6 | HIGH | `requeueQuarantine` on a COMPLETED batch left the fixed row STAGED forever | §Core operations `requeueQuarantine` (rewind `last_stage` / run per-entry stages inline); §pipeline resumability note; §Testing |
| 7 | MEDIUM | Merchant-rule ties nondeterministic; balance/summary rows unmodeled; wrapped narrations split | §Merchant normalization (`(priority, id)` tie-break) + `merchant_rule_lookup_idx`; §CSV adapter specifics (RFC-4180 reader / continuation-line rule; per-profile balance-row filter, zero-amount non-proposable); §parse & §propose stages; §Testing |
| 8 | — | Category must be the shared dimension; recurrences feed the reserved obligation registry | §Merchant normalization (consume shared `CategoryResolver`, no local category notion — conventions §12); §Recurring-payment detection (detects into the reserved recurring-obligation registry seam) |
