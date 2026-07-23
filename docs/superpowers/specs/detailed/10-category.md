# 10 — Category Context (Detailed Design)

**Created 2026-07-23** (audit remediation, Tier 0 — Part E #1)

**Inherits:** `00-conventions.md`. **Parent:** the design spec. The **one** owner of the
category taxonomy. Every context that needs a category — analytics, budgeting, forecasting,
import — reads it from here through a single `CategoryResolver` port. No context defines its
own category notion (conventions §12).

## Why this context exists (the audit finding it closes)

The audit found **category modeled four incompatible ways** with no shared source of truth:

- **Analytics (05):** `analytics_category_month.category` was a **`TEXT` key** (`'FOOD'`,
  `'RENT'`) derived from the leaf **account-name path** (ASSUMPTION-CAT). Rollups keyed on a
  *string*, so a rename re-buckets history and two owners' `'FOOD'` are indistinguishable.
- **Budgeting (06):** `budget_envelope.category_id` was a **`UUID`** referencing a "shared
  category concept (Analytics)" that Analytics never actually exposed as ids.
- **Import (03):** `import_merchant.category_hint` and `import_proposed_posting
  .suggested_account_hint` were **advisory `TEXT`** with a third vocabulary.
- A fourth, implicit notion: the account-name-path convention (`Expenses:Food:Groceries`)
  treated as a category in several places.

Four vocabularies, three storage types (`TEXT` key, `UUID`, hint string), zero shared
resolution. This context replaces all four with **one owner-scoped taxonomy keyed on
`category_id`** and **one `CategoryResolver`**. After this doc:

- Analytics `analytics_category_month` keys on **`category_id UUID`**, never the name string.
- Budgeting `budget_envelope.category_id` FKs **this** `category` table.
- Import stops carrying category strings; it feeds a **`category_merchant_rule`** and reads
  the resolver like everyone else.
- A **`CategoryReassigned`** event lets analytics/budgeting re-home historical rollups when a
  tag/mapping changes; a **rename never moves a rollup** because rollups key on the immutable
  `category_id`, not the display name.

## Responsibilities

- Own a **hierarchical, user-editable taxonomy** per owner (`category` tree) with a seeded,
  India-friendly starter tree, `kind ∈ {EXPENSE, INCOME, TRANSFER}`.
- Expose a deterministic **`CategoryResolver`** port that maps a ledger posting to a
  `category_id` via a fixed precedence (explicit tag → account map → merchant rule →
  uncategorized fallback).
- Own the two mapping tables — **account→category** and **merchant→category** — owner-scoped,
  with a deterministic tie-break.
- Emit **`CategoryReassigned`** so downstream rollups re-home when resolution changes; emit
  create/rename/archive events for UI caches.
- Guarantee **stable ids**: renames and moves change display/parentage, never the `id`, so no
  downstream fold re-buckets on a rename.

**Non-responsibilities:** no money, no postings, no truth. Category holds a dimension and its
resolution rules only. It reads `LedgerApi` for account **metadata** (type, leaf-ness) and
`ImportApi`/`import_merchant` ids for merchant rules — never their tables directly
(conventions §11).

## Schema

Owner-scoped, `category_*`/`category` tables. PKs are UUID v7; owner_id on every row; time is
`TIMESTAMPTZ` UTC (conventions §1/§3/§4). No money columns.

```sql
-- The taxonomy. A per-owner forest of exactly three roots (one per kind), each with a
-- user-editable subtree. name is a DISPLAY label only — rollups everywhere key on id, so a
-- rename is metadata (conventions §12). archived hides a node from pickers but keeps its id
-- valid so historical rollups that reference it stay intact.
CREATE TABLE category (
    id          UUID PRIMARY KEY,               -- v7
    owner_id    UUID NOT NULL,
    parent_id   UUID REFERENCES category(id),   -- NULL only for the three kind-roots
    name        TEXT NOT NULL,                  -- display label; NEVER a key
    kind        TEXT NOT NULL,                  -- EXPENSE|INCOME|TRANSFER (== root's kind)
    is_system   BOOLEAN NOT NULL DEFAULT FALSE, -- seeded/starter node: cannot be deleted
    system_slug TEXT,                           -- stable handle for the seams below; see index
    archived    BOOLEAN NOT NULL DEFAULT FALSE, -- hidden from new resolution; id stays valid
    sort_order  INT NOT NULL DEFAULT 100,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- sibling names unique within a parent
    UNIQUE (owner_id, parent_id, name),
    -- composite target so mapping FKs can also pin owner (defence in depth)
    UNIQUE (owner_id, id)
);
CREATE INDEX category_owner_kind_idx ON category (owner_id, kind, archived);
CREATE INDEX category_owner_parent_idx ON category (owner_id, parent_id);
-- Exactly one node per (owner, system_slug): the three roots + the uncategorized fallbacks
-- (ROOT_EXPENSE, ROOT_INCOME, ROOT_TRANSFER, UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME).
-- Mirrors ledger's account_system_kind_idx pattern (02-ledger) so the resolver can find the
-- fallback deterministically.
CREATE UNIQUE INDEX category_system_slug_idx ON category (owner_id, system_slug)
    WHERE system_slug IS NOT NULL;

-- account -> default category. One mapping per account. This is precedence level (b): the
-- account a posting hits (e.g. a dedicated "Rent" expense account) implies its category.
CREATE TABLE category_account_map (
    owner_id    UUID NOT NULL,
    account_id  UUID NOT NULL,                  -- ledger account id (cross-context: not FK)
    category_id UUID NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, account_id),
    FOREIGN KEY (owner_id, category_id) REFERENCES category (owner_id, id)
);

-- merchant -> category rule. Precedence level (c). Fed by Import (a normalized merchant gets
-- a category rule); owner-scoped. Multiple rules per merchant are allowed but resolution is
-- deterministic: lowest priority wins, id ASC breaks ties.
CREATE TABLE category_merchant_rule (
    id          UUID PRIMARY KEY,               -- v7
    owner_id    UUID NOT NULL,
    merchant_id UUID NOT NULL,                  -- import_merchant id (cross-context: not FK)
    category_id UUID NOT NULL,
    priority    INT NOT NULL DEFAULT 100,       -- lower = evaluated first
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    source      TEXT NOT NULL DEFAULT 'USER',   -- USER|IMPORT_SEED|LEARNED
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, merchant_id, category_id),
    FOREIGN KEY (owner_id, category_id) REFERENCES category (owner_id, id)
);
CREATE INDEX cmr_lookup_idx
    ON category_merchant_rule (owner_id, merchant_id, enabled, priority, id);

-- explicit user tag on a ledger transaction (optionally a specific posting). Precedence
-- level (a) — the highest. A posting-level tag overrides a whole-txn tag.
CREATE TABLE category_tag (
    id          UUID PRIMARY KEY,               -- v7
    owner_id    UUID NOT NULL,
    txn_id      UUID NOT NULL,                  -- ledger transaction id (cross-context)
    posting_id  UUID,                           -- NULL = whole-txn tag; else posting-scoped
    category_id UUID NOT NULL,
    tagged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, category_id) REFERENCES category (owner_id, id)
);
-- One tag per (txn, posting) — NULL posting is its own slot (COALESCE sentinel so the
-- whole-txn tag is a single distinct row, not many NULL-distinct rows).
CREATE UNIQUE INDEX category_tag_scope_idx
    ON category_tag (owner_id, txn_id, COALESCE(posting_id, '00000000-0000-0000-0000-000000000000'));
```

**Kind consistency (DB-enforced).** A child's `kind` MUST equal its parent's `kind`, and a
root (`parent_id IS NULL`) MUST carry one of the three `system_slug` root handles. A plain FK
cannot reach the parent's `kind` column, so an **AFTER-INSERT/UPDATE trigger** on `category`
asserts `child.kind = parent.kind` and `child.owner_id = parent.owner_id`, and forbids cycles
(a moved node may not become its own ancestor). Mapping/tag targets are additionally validated
in the domain to be **non-archived and of the expected kind** at write time (see resolution).

**Stable-id invariant (the core fix).** Nothing downstream keys on `category.name`. Rename and
move update `name`/`parent_id`/`updated_at` only; `id` is immutable. Therefore a rename emits
`CategoryRenamed` (a display-cache signal) but **never** a `CategoryReassigned`, and no rollup
moves — analytics/budgeting rows already key on `category_id` (conventions §12).

## Seeded starter tree (India-friendly)

On owner provisioning (platform bootstrap; idempotent — re-seeding is a no-op guarded by
`system_slug` / `(owner, parent, name)`), each owner gets three `is_system` roots and a
starter subtree. Analytics' reporting grain is the **immediate child of a root** (see
"Consumers"); deeper nodes roll up to their top bucket. All are `is_system=true` (protected
from deletion; archivable/renamable by the user).

- **Expenses** (`ROOT_EXPENSE`, kind EXPENSE)
  - Food → Groceries, Dining Out, Food Delivery
  - Rent
  - Utilities → Electricity, Water, Gas (LPG/PNG), Mobile, Internet/Broadband, DTH
  - Transport → Fuel, Cab/Auto, Public Transit (Metro/Bus), Parking/Tolls (FASTag), Vehicle Maintenance
  - Shopping → Clothing, Electronics, Household, Online (Amazon/Flipkart)
  - Health → Pharmacy, Doctor/Hospital, Insurance Premium
  - Education → Tuition/Fees, Books, Courses
  - Entertainment → OTT/Subscriptions, Movies, Events
  - Travel
  - EMI & Loans → Home Loan, Personal Loan, Credit-Card Interest
  - Taxes & Charges → Income Tax, GST, Bank Charges
  - Personal Care
  - Gifts & Donations
  - Miscellaneous
  - **Uncategorized** (`UNCATEGORIZED_EXPENSE`) — the expense fallback (§Resolution)
- **Income** (`ROOT_INCOME`, kind INCOME)
  - Salary
  - Business/Freelance
  - Interest (Savings/FD)
  - Dividends
  - Capital Gains
  - Rental Income
  - Refunds & Cashback
  - Gifts Received
  - Other Income
  - **Uncategorized** (`UNCATEGORIZED_INCOME`) — the income fallback
- **Transfers** (`ROOT_TRANSFER`, kind TRANSFER)
  - Investments (SIP/Stocks/MF buys), Savings/Sweep, Credit-Card Payment, Self/Family Transfer,
    In-Transit
  - Transfers are **not** spend or income; they exist so a user can label an
    asset↔asset / asset↔liability movement, but the resolver never rolls them into
    category burn/income (see Resolution step 2).

## Resolution rules — the `CategoryResolver` port

The single entry point every other context uses. Given a posting, it returns a `category_id`
deterministically. **Input** (`CategoryResolutionRequest`):

```
{ ownerId, txnId, postingId, accountId, accountType,   // ASSET|LIABILITY|EQUITY|INCOME|EXPENSE
  merchantId? }                                          // resolved import merchant, if any
```

**Algorithm** (`resolve(request) -> CategoryRef`):

1. **Derive the target kind from the account type:** `EXPENSE → EXPENSE`, `INCOME → INCOME`,
   everything else (`ASSET`/`LIABILITY`/`EQUITY`) → `TRANSFER`.
2. **Transfers are not categorized as spend/income.** If kind is `TRANSFER`, return
   `CategoryRef{ categoryId = null, kind = TRANSFER, source = NONE }` unless an explicit
   `TRANSFER`-kind tag exists (then step 3a). Analytics/budgeting **skip** null-category refs
   for burn/income rollups (matches 05's "pure transfers contribute to net worth, not category
   rollups"). This is why paying a card bill or an SIP never inflates spend.
3. **Precedence — first non-empty wins**, each constrained to the target kind and to
   non-archived targets:
   - **(a) Explicit tag (highest).** `category_tag` for `(owner, txn, posting)`. A
     **posting-level** tag (`posting_id = request.postingId`) beats a **whole-txn** tag
     (`posting_id IS NULL`). `source = TAG`.
   - **(b) Account map.** `category_account_map` for `(owner, accountId)`. `source = ACCOUNT`.
   - **(c) Merchant rule.** `category_merchant_rule` for `(owner, merchantId)`, `enabled`,
     ordered **`priority ASC, id ASC`** — the deterministic tie-break. First row wins.
     `source = MERCHANT`.
   - **(d) Uncategorized fallback (lowest).** The system `UNCATEGORIZED_<kind>` node resolved
     by `system_slug`. Always exists (seeded). `source = FALLBACK`.
4. **Validation / skip.** If a candidate at a level points to a category whose `kind` ≠ target
   kind, or that is `archived`, that candidate is **skipped** (it does not short-circuit),
   resolution falls through to the next level, and the anomaly is logged. This makes "archived
   a category that a stale mapping still points at" degrade to the next rule, not error.

**Output** (`CategoryRef`): `{ categoryId, kind, source: TAG|ACCOUNT|MERCHANT|FALLBACK|NONE }`.
`source` drives both explainability (a "why this category?" UI) and `CategoryReassigned`
scoping.

**Determinism (replay-critical, conventions §8).** For fixed mapping/tag state and inputs,
`resolve` is a pure function — same output every time, including tie-broken merchant rules.
Consumers that fold resolved categories into projections therefore replay identically. When
the *mapping state itself* changes, that is a `CategoryReassigned` event (below), not
hidden non-determinism.

Companion helpers on the port:

- **`resolveBatch(requests)`** — one round-trip for a whole transaction's postings.
- **`rollupOf(categoryId) -> categoryId`** — the **immediate child of the root** ancestor of a
  category (the analytics reporting grain). `Groceries → Food`; `Food → Food`; a root →
  itself.
- **`kindOf(categoryId)` / `displayPath(categoryId)`** — kind and `Root:Parent:Leaf` label for
  display. Never used as a key.

## Core operations (`CategoryApi`)

Owner-scoped (conventions §4); package-private impl, public interface + DTOs only (§11).

- **`tree(kind?)`** — the owner's taxonomy as a forest (or one kind's subtree), archived nodes
  flagged.
- **`createCategory(cmd)`** — `{parentId, name, kind}`. `kind` must match the parent; rejects a
  duplicate sibling name (`CATEGORY_NAME_TAKEN`). Emits `CategoryCreated`.
- **`renameCategory(id, name)`** — display only. Emits `CategoryRenamed`. **No**
  `CategoryReassigned` (rollups key on id).
- **`moveCategory(id, newParentId)`** — reparents within the **same kind**; rejects cycles and
  cross-kind moves (`CATEGORY_KIND_MISMATCH`). Emits `CategoryMoved`. Rollup grain of
  descendants may shift, so `rollupOf` changes are advertised in the event; the leaf ids are
  unchanged, so per-leaf rollups do not move.
- **`archiveCategory(id)`** — hides from pickers. **Forbidden** for the three roots and the two
  `UNCATEGORIZED_*` fallbacks (`CATEGORY_PROTECTED`). Archiving is preferred over delete
  because historical rollups reference the id; a delete is only allowed for a **user** category
  with **no** references and **no** children. Emits `CategoryArchived`.
- **`mapAccount(accountId, categoryId)`** — upsert level-(b) mapping. If the effective mapping
  for the account **changes**, emits `CategoryReassigned{ scope=ACCOUNT }`.
- **`setMerchantRule(cmd)`** — `{merchantId, categoryId, priority?}`; upsert level-(c) rule.
  If the *winning* rule for the merchant changes, emits `CategoryReassigned{ scope=MERCHANT }`.
- **`tagTransaction(cmd)`** — `{txnId, postingId?, categoryId}`; upsert level-(a) tag
  (Idempotency-Key). If the effective category for the txn/posting changes, emits
  `CategoryReassigned{ scope=TRANSACTION }`.
- **`resolve(request)` / `resolveBatch` / `rollupOf`** — the `CategoryResolver` port surface,
  called in-process by other contexts (never over HTTP on the hot path).

## `CategoryReassigned` — re-homing historical rollups

Emitted **only** when resolution output for some scope actually changes (a rename/move never
does — ids are stable). It is the signal analytics and budgeting use to move already-folded
spend/income from the previous category to the new one, keeping rollups keyed on id.

Payload:

```json
{
  "scope": "TRANSACTION | ACCOUNT | MERCHANT",
  "key":   { "txnId": "…", "postingId": "…" },   // or { "accountId": … } / { "merchantId": … }
  "previousCategoryId": "…",
  "newCategoryId": "…",
  "kind": "EXPENSE | INCOME",
  "sinceBusinessTime": null,   // null = all history; else re-home only postings at/after this
  "schemaVersion": 1
}
```

**How consumers re-home (each in its own idempotent projection tx):**

- **`scope=TRANSACTION`** — the tightest: exactly the identified txn's (or posting's) attributed
  amount moves. Budgeting looks up its `budget_spend_attribution` rows for `txn_id`
  (posting-scoped if `postingId` set), subtracts them from the `previousCategoryId`'s envelope
  and re-attributes to the `newCategoryId`'s envelope. Analytics moves the same signed amount
  between `analytics_category_month` rows for the affected periods.
- **`scope=ACCOUNT` / `scope=MERCHANT`** — a **broad** reassignment: every historical posting
  whose *winning* resolution came from that account/merchant re-homes. The consumer re-drives
  resolution over the affected posting set (identified via its own lineage — budgeting has
  per-posting `budget_spend_attribution`; analytics scopes the recompute by re-reading the
  relevant ledger postings for that account/merchant through `LedgerApi`), moving signed amounts
  from previous to new category per period. Because folds are **signed and additive**
  (conventions §8), the move is exact: `−amount` on the old key, `+amount` on the new.
- Worst case (a consumer cannot scope the delta) it triggers a **scoped rebuild** of the
  affected category rows from the outbox — still deterministic, since resolution is pure given
  the *new* mapping state.

Consumers are **idempotent** on `(consumer, event_id)` (conventions §6): a redelivered
`CategoryReassigned` re-homes to the same end state (moving already-moved rows is a no-op
because the previous key no longer holds those amounts).

## Events emitted

Transactional outbox, same DB tx as the state change, `schemaVersion` on every payload
(conventions §6):

- **`CategoryCreated`** — `{categoryId, parentId, name, kind, isSystem}`.
- **`CategoryRenamed`** — `{categoryId, name}` (display-cache invalidation only).
- **`CategoryMoved`** — `{categoryId, oldParentId, newParentId, rollupChanged}`.
- **`CategoryArchived`** — `{categoryId}`.
- **`CategoryReassigned`** — the re-home signal above. The one event with rollup arithmetic
  consequences.

Category **consumes** nothing on the hot path (resolution is a synchronous port call). It may
optionally consume `TransactionProposed` later to auto-seed a `category_merchant_rule` from an
import's `category_hint` (the learning loop 03 defers) — designed, not required in this phase.

## How every context consumes this one resolver (no per-context category)

- **Analytics (05):** `analytics_category_month` keys on **`category_id`** (replacing the old
  `category TEXT`). Its `AnalyticsProjector`, per income/expense posting, calls
  `CategoryResolver.resolve(...)` then `rollupOf(...)` to bucket at the **immediate-child-of-root
  grain**, folding a **signed** natural-value accumulator. On `CategoryReassigned` it moves the
  signed amount between category rows. Rename → no change (id-keyed). This removes ASSUMPTION-CAT
  entirely — category is no longer inferred from the account-name path.
- **Budgeting (06):** `budget_envelope.category_id` FKs **this** `category(owner_id, id)`. The
  spend-projector resolves each expense posting via the resolver, finds the envelope for that
  `category_id`, and attributes. The doc-06 "mirror projection until the resolver ships" and its
  "re-attribution pass on mapping change" are now served directly: the mirror *is* this context,
  and the re-attribution trigger *is* `CategoryReassigned`.
- **Forecasting:** projects per-category future flows by reading the resolver for historical
  categorization and `rollupOf` for the grain; recurring-obligation categorization uses the same
  ids. No separate forecasting category table.
- **Import (03):** stops emitting category **strings**. When a user categorizes/renames a
  merchant's proposal, Import (or Reconciliation) calls `CategoryApi.setMerchantRule(merchantId,
  categoryId)` — creating a level-(c) rule other contexts then honor. `import_merchant
  .category_hint` degrades to a **display-only advisory** (or is dropped); it is never a key and
  never competes with the resolver. Proposed postings reference accounts, not category strings;
  categorization happens at resolve time.

One taxonomy, one resolver, one id space. There is no other category notion anywhere.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/categories?kind=` | the owner's taxonomy tree |
| POST | `/api/v1/categories` | create a category (Idempotency-Key) |
| PATCH | `/api/v1/categories/{id}` | rename / move / archive |
| PUT | `/api/v1/accounts/{id}/category` | set account→category mapping |
| PUT | `/api/v1/merchants/{id}/category` | set merchant→category rule |
| PUT | `/api/v1/transactions/{id}/category` | tag a transaction (Idempotency-Key) |
| GET | `/api/v1/transactions/{id}/category?postingId=` | resolved category + `source` (explainability) |

Conventions apply: `/api/v1`, JSON, cursor pagination on the tree if large, Idempotency-Key on
state-changing POST/PUT (conventions §7/§11), RFC 9457 `problem+json` with a stable `CATEGORY_`
`code` enum — e.g. `CATEGORY_NAME_TAKEN`, `CATEGORY_KIND_MISMATCH`, `CATEGORY_PROTECTED`
(root/uncategorized), `CATEGORY_NOT_FOUND`, `CATEGORY_CYCLE`. The resolve endpoint is read-only
(no key). Cross-context callers use the in-process `CategoryResolver`/`CategoryApi`, not HTTP.

## Testing

- **Resolution precedence (property):** for generated tag/account/merchant/fallback states,
  `resolve` returns the highest-precedence non-empty match; a posting-level tag beats a whole-txn
  tag; merchant ties break deterministically by `priority ASC, id ASC`.
- **Kind derivation & transfers:** EXPENSE/INCOME accounts resolve to their kind; asset/liability
  legs return `NONE` (null category) and never enter burn/income rollups (post + card-payment
  transfer → spend unchanged).
- **Kind-consistency invariant:** the trigger rejects a child whose kind ≠ parent's, a
  cross-owner parent, and a move that forms a cycle (Testcontainers Postgres, real trigger).
- **Stable id / rename:** renaming a category emits `CategoryRenamed` and **no**
  `CategoryReassigned`; a downstream category rollup keyed on the id is byte-identical before and
  after the rename.
- **Reassignment re-homes exactly:** changing an account map / merchant rule / txn tag emits
  `CategoryReassigned`; a consuming projection moves the signed amount from previous to new
  category and nets to zero on the old key; redelivery is a no-op.
- **Archived fallthrough:** archiving a category that a stale mapping still targets makes
  `resolve` skip it and fall to the next level (ultimately `UNCATEGORIZED_<kind>`), never errors;
  historical rollups referencing the archived id remain intact.
- **Seeding idempotency:** provisioning an owner twice yields exactly one starter tree; the
  three roots and two `UNCATEGORIZED_*` fallbacks always exist and are `CATEGORY_PROTECTED`.
- **Owner scoping:** every query is owner-scoped; two owners' identically-named categories have
  distinct ids and never resolve across owners.
- **API contract:** per endpoint happy + validation + error `code` (`CATEGORY_NAME_TAKEN`,
  `CATEGORY_KIND_MISMATCH`, `CATEGORY_PROTECTED`, `CATEGORY_CYCLE`); Idempotency-Key replay
  returns the original result.
