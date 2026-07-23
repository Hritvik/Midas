# 11 — Notification Context (Detailed Design)

**Created 2026-07-23**

**Inherits:** `00-conventions.md`. **Parent:** the design spec. Release **Phase 3**
(in-app inbox baseline; email/push channels grow in later phases).

The delivery layer. Producers across Midas already **mint** alert / insight / discrepancy /
reminder records but nothing turns them into something the owner actually sees — the audit's
#1 product gap (flagged by four agents). Notification closes it: it is the **sole idempotent
outbox consumer** that ranks, dedups, digests, quiet-hours-gates, and channels those events
into delivered messages. It owns no financial truth and never writes any — it only reads the
events producers emit and materializes an owner-facing inbox from them (conventions §8: a
projection-style read side, rebuildable from the outbox).

## Why this context exists (audit remediation)

- **Part B #1 (delivery gap):** `BudgetOverspent` / `SafeToSpendChanged` / `budget_alert`
  (Budgeting), `InsightRaised` / `InsightResolved` (Optimization), `DiscrepancyFlagged`
  (Reconciliation), review-ready `TransactionProposed` / `ImportBatchCompleted` (Import), and
  due-date reminders (Forecasting recurring rules / the obligation registry) all previously
  went nowhere. Notification is the one context that delivers them. Producers keep saying
  "delivery is out of scope" — this doc **is** that scope.
- **Oscillation must not spam (coordinated with Optimization hysteresis, conventions §12):** a
  metric that flaps across a threshold must not fire a message each way. Notification's dedup +
  cooldown collapses re-raises; it relies on and reinforces Optimization's own
  `dedup_key` / `snooze_until` hysteresis (a `RESEEN` never emits `InsightRaised` again).
- **At-least-once + idempotent (conventions §6):** a redelivered event must not double-notify.
  Dedup is on `(consumer_name, event_id)` in `processed_event` (platform) **and** a
  `UNIQUE (owner_id, source_event_id)` guard on the notification row.

## Responsibilities

- **Consume producer events** through the platform dispatcher (idempotent on
  `(consumer_name, event_id)`, conventions §6) and turn each into at most one owner-facing
  `notification` row.
- **Rank / prioritize:** compute a delivery priority from `severity` + type weight + recency so
  the inbox and any digest lead with what matters (a `SAFE_TO_SPEND_NEGATIVE` outranks an
  informational insight).
- **Dedup & collapse:** a stable `dedup_key` per logical subject means a re-raise, a redelivery,
  or an oscillating metric updates the existing live notification instead of minting a new one.
- **Digest batching:** low-urgency items can be held and rolled into a weekly / monthly
  "money moment" digest per the owner's `digest_freq` preference, instead of interrupting.
- **Quiet hours:** hold non-critical deliveries during the owner's configured quiet window
  (owner-local time, conventions §3) and release them after.
- **Per-type channel preferences & unsubscribe:** the owner chooses which `type`s reach which
  `channel` (in-app / email / push), can disable a type entirely, and can set quiet hours and
  digest frequency per type.
- **Read / dismissed tracking:** the inbox tracks `PENDING → SENT → READ → DISMISSED`; resolve
  events (`InsightResolved`, an alert clearing) auto-dismiss the corresponding live notification.
- **Channel abstraction:** in-app inbox is the baseline channel (Phase 3); email and push are
  pluggable `NotificationChannel` implementations behind the same delivery pipeline.
- **Optional Claude enrichment:** turn a dry record `rationale` into one human sentence,
  grounded in the record's own numbers, behind the same pipeline (see §Claude enrichment).

**Non-goals:** Notification never mutates truth, never re-computes a producer's numbers, and
never invents content — a message's title/body derive from the source event payload (and, for
enrichment, only from numbers already in that payload). INR only (Phases 0–5).

## Schema

All tables are owner-scoped. PKs are UUID v7 (conventions §1); money, where shown in a body,
is carried as the source event's `{amountMinor, currency}` and never recomputed here.

```sql
-- The owner-facing notification (one per delivered logical event). A read-side projection
-- built from the outbox; rebuildable by replaying producer events (conventions §8).
CREATE TABLE notification (
    id              UUID PRIMARY KEY,               -- v7 = time order
    owner_id        UUID NOT NULL,
    source_event_id UUID NOT NULL,                  -- the outbox event.id that produced this
    source_type     TEXT NOT NULL,                  -- originating event_type (BudgetOverspent, InsightRaised, …)
    type            TEXT NOT NULL,                  -- notification taxonomy (see §Types) e.g.
                                                    --   BUDGET_OVERSPENT|SAFE_TO_SPEND|INSIGHT|
                                                    --   DISCREPANCY|REVIEW_READY|DUE_REMINDER|SYSTEM
    severity        TEXT NOT NULL,                  -- INFO|LOW|MEDIUM|HIGH|CRITICAL
    priority_rank   INT  NOT NULL,                  -- derived ordering key (see §Ranking); higher = more urgent
    title           TEXT NOT NULL,                  -- short human summary, filled from the event payload
    body            TEXT NOT NULL,                  -- longer human text (dry render, or Claude-enriched sentence)
    body_source     TEXT NOT NULL DEFAULT 'TEMPLATE', -- TEMPLATE|CLAUDE (provenance of body; §Claude enrichment)
    dedup_key       TEXT NOT NULL,                  -- stable logical-subject key (see §Dedup); collapses re-raise/oscillation
    detail          JSONB NOT NULL DEFAULT '{}',    -- machine-readable context copied from the event (numbers, subject ids)
    status          TEXT NOT NULL DEFAULT 'PENDING',-- PENDING|SENT|READ|DISMISSED
    channel         TEXT NOT NULL DEFAULT 'IN_APP', -- IN_APP|EMAIL|PUSH — the channel this row is destined for
    deliver_after   TIMESTAMPTZ NOT NULL DEFAULT now(), -- quiet-hours / digest release time; PENDING until now() >= this
    digest_id       UUID,                           -- set when folded into a digest batch (§Digest)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,                    -- when the channel accepted it (SENT)
    read_at         TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    -- Idempotent apply: a redelivered event never mints a second row (conventions §6).
    UNIQUE (owner_id, source_event_id, channel)
);
CREATE INDEX notification_inbox_idx    ON notification (owner_id, status, priority_rank DESC, created_at DESC);
CREATE INDEX notification_due_idx      ON notification (channel, status, deliver_after);
-- At most one LIVE (PENDING|SENT) notification per (owner, dedup_key, channel): the DB-level
-- backstop for dedup / oscillation collapse. READ/DISMISSED rows drop out so history accrues.
CREATE UNIQUE INDEX notification_live_dedup_idx
    ON notification (owner_id, dedup_key, channel)
    WHERE status IN ('PENDING', 'SENT');

-- Per-(owner, type, channel) preference. A missing row means the seeded default for that type.
CREATE TABLE notification_pref (
    id           UUID PRIMARY KEY,                  -- v7
    owner_id     UUID NOT NULL,
    type         TEXT NOT NULL,                     -- matches notification.type; '*' = all types
    channel      TEXT NOT NULL,                     -- IN_APP|EMAIL|PUSH
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,     -- FALSE = unsubscribed for this (type, channel)
    quiet_hours  JSONB,                             -- {start:"22:00", end:"07:00", tz:"Asia/Kolkata"} (owner-local, §3)
    digest_freq  TEXT NOT NULL DEFAULT 'IMMEDIATE', -- IMMEDIATE|DAILY|WEEKLY|MONTHLY|OFF
    min_severity TEXT,                              -- optional floor: below this severity is suppressed for this channel
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, type, channel)
);
CREATE INDEX notification_pref_owner_idx ON notification_pref (owner_id);

-- A digest batch: the "money moment" roll-up. Low-urgency notifications are held (deliver_after
-- in the future) and swept into a digest at the cadence boundary in owner-local time.
CREATE TABLE notification_digest (
    id            UUID PRIMARY KEY,                 -- v7
    owner_id      UUID NOT NULL,
    channel       TEXT NOT NULL,
    freq          TEXT NOT NULL,                    -- WEEKLY|MONTHLY (matches the pref that produced it)
    period_start  TIMESTAMPTZ NOT NULL,             -- owner-local period boundaries
    period_end    TIMESTAMPTZ NOT NULL,
    item_count    INT NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'OPEN',     -- OPEN (accumulating) | SENT
    sent_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, channel, freq, period_start)
);
```

**No truth mutation.** None of these tables feed the ledger or any producer's tables.
Notification holds no INSERT/UPDATE grant on `ledger_transaction`, `posting`, `budget_alert`,
`insight`, or `recon_*` — it reads only the events those contexts emit (enforced statically,
see §Testing). Marking a notification `READ`/`DISMISSED` is a purely local inbox state; it does
**not** acknowledge or resolve the source record (acknowledging an insight is still
`OptimizationApi.acknowledgeInsight`).

## Notification types & the source-event mapping

Each consumed event maps to exactly one notification `type` and a `dedup_key` derived from the
source's own stable subject identity — never from the triggering numbers, so a re-raise or an
oscillating value collapses onto the same row.

| Source event | Producer | `type` | `dedup_key` | Baseline severity |
|---|---|---|---|---|
| `BudgetOverspent` | Budgeting | `BUDGET_OVERSPENT` | `BUDGET_OVERSPENT:{envelopeId}:{periodId}` | CRITICAL |
| `budget_alert` (`ENVELOPE_NEAR_LIMIT`) | Budgeting | `BUDGET_NEAR_LIMIT` | `BUDGET_NEAR_LIMIT:{envelopeId}:{periodId}` | WARNING→MEDIUM |
| `SafeToSpendChanged` (crosses < 0) | Budgeting | `SAFE_TO_SPEND` | `SAFE_TO_SPEND:{periodId}` | HIGH |
| `InsightRaised` | Optimization | `INSIGHT` | `INSIGHT:{detectorKey}:{subjectId}` (= source `dedup_key`) | from event `severity` |
| `InsightResolved` | Optimization | — (auto-dismiss) | same `INSIGHT:…` key | — |
| `DiscrepancyFlagged` | Reconciliation | `DISCREPANCY` | `DISCREPANCY:{discrepancyId}` | MEDIUM (HIGH for `STALE_IN_TRANSIT`) |
| `TransactionProposed` | Import | `REVIEW_READY` | `REVIEW_READY:{batchId}` (coalesced, see below) | LOW |
| `ImportBatchCompleted` | Import | `REVIEW_READY` | `REVIEW_READY:{batchId}` | LOW |
| due-date reminder | Notification scheduler | `DUE_REMINDER` | `DUE_REMINDER:{ruleId|obligationId}:{dueDate}` | MEDIUM |
| `DeliveryDead` (DEAD-letter alert) | Platform | `SYSTEM` | `SYSTEM:DEAD:{consumerName}:{eventSeq}` | CRITICAL |

**Review-ready coalescing.** A batch import fires many `TransactionProposed` events; the
`dedup_key` keys on `batchId`, so the first proposal opens one `REVIEW_READY` notification and
subsequent proposals in the same batch bump its `detail.proposedCount` rather than minting N
messages. `ImportBatchCompleted` finalizes the same row ("42 transactions ready to review").

**`SafeToSpendChanged` is edge-triggered, not level-triggered.** Budgeting throttles the event
already; Notification only raises a `SAFE_TO_SPEND` notification when the payload crosses into
negative territory and auto-dismisses when a later event shows it non-negative — so a number
wobbling around zero produces one live notification, not a stream. This is the concrete
oscillation guard the audit calls for, coordinated with the producer's own throttle.

## Consuming the outbox (`notification` consumer)

A single platform `EventConsumer` `name = "notification"`. The dispatcher runs `handle()` under
the event's `owner_id` scope (conventions §4) and dedups on `(consumer_name, event_id)` via
`processed_event`. `handle(event)` is a pure function of the event payload plus current prefs:

1. **Map** `event.event_type` → `(type, dedup_key, baseSeverity)` per the table above. Unknown
   event types are a no-op (tolerate-and-ignore; consumers tolerate unknown fields, §6).
2. **Resolve preferences** for `(owner, type)`: for each enabled `channel`, decide whether this
   event is deliverable (channel enabled, `severity ≥ min_severity`). A disabled `(type,
   channel)` yields no row for that channel. If *all* channels are disabled the event is
   recorded as processed and dropped (unsubscribed — not an error).
3. **Upsert by `dedup_key`** (one DB tx, per channel): `SELECT ... FOR UPDATE` the live row for
   `(owner, dedup_key, channel)`.
   - **no live row** → INSERT `PENDING`, compute `priority_rank` (§Ranking) and `deliver_after`
     (§Quiet hours / §Digest), fill `title`/`body` (§Rendering).
   - **live row exists** → refresh `detail`/`title`/`priority_rank` in place (re-raise,
     oscillation, batch bump). No second row — the partial unique index enforces it. A row
     already `SENT` is *not* re-sent for a cosmetic refresh; a severity escalation may re-arm it.
   - **resolve event** (`InsightResolved`, alert cleared, discrepancy resolved) → transition the
     live row to `DISMISSED` (auto-dismiss); nothing new is delivered.
4. **Write `processed_event`** as the ack — the dispatcher marks the delivery `SENT` only after
   this commits (conventions §6, mark-SENT boundary). Redelivery before ack is a dedup no-op;
   redelivery after ack hits the `UNIQUE (owner_id, source_event_id, channel)` guard and is
   absorbed. **Delivery is at-least-once and the apply is idempotent, so a redelivered event
   never double-notifies.**

Actual channel send (in-app is trivially "the row exists"; email/push call an external gateway)
is driven by a separate **delivery sweep** so it can respect `deliver_after` and retry
independently of event consumption — see §Delivery pipeline.

## Ranking / prioritization

`priority_rank` is a deterministic integer derived at apply time (injected `Clock`, §3):

```
priority_rank = severityWeight(severity) * 1000
              + typeWeight(type)              -- SAFE_TO_SPEND/DISCREPANCY lead, REVIEW_READY trails
              + recencyBoost(now - created_at) -- small, decays; keeps a fresh item above a stale equal
```

`severityWeight`: `CRITICAL=5, HIGH=4, MEDIUM=3, LOW=2, INFO=1`. The inbox lists by
`priority_rank DESC, created_at DESC` (index `notification_inbox_idx`); a digest orders its items
the same way and leads with the single highest. Weights live in config so they are tunable
without a redeploy; ranking never changes *which* notifications exist, only their order.

## Dedup, oscillation & cooldown

Three layers, so a flapping metric or a redelivery cannot spam:

1. **`processed_event`** — the same outbox event is never handled twice.
2. **`UNIQUE (owner_id, source_event_id, channel)`** — even across a dispatcher replay, one event
   yields one row per channel.
3. **`notification_live_dedup_idx`** on `(owner_id, dedup_key, channel) WHERE status IN
   ('PENDING','SENT')** — the logical-subject collapse. A re-raise from a *different* event id
   but the same subject (Optimization emitting a fresh `InsightRaised` after a snooze lapses, a
   budget re-tripping the same envelope) updates the live row instead of adding one. A
   **cooldown** (`notification.cooldownMinutes`, default 60) suppresses re-arming an already-SENT
   row for the same `dedup_key` unless severity escalates — so an oscillating value that keeps
   re-crossing a threshold produces one message, then quiet. This is the delivery-side complement
   to Optimization's `dedup_key` / `snooze_until` hysteresis: the producer avoids re-emitting;
   Notification absorbs whatever still slips through.

## Quiet hours

Per `(type, channel)` `quiet_hours = {start, end, tz}`, evaluated in owner-local time
(conventions §3, default `Asia/Kolkata`). On apply, if `now` (owner-local) falls in the quiet
window **and** `severity < CRITICAL`, `deliver_after` is set to the window's next end instant;
the row stays `PENDING` and the delivery sweep releases it then. `CRITICAL` bypasses quiet hours
(a negative safe-to-spend or a DEAD-letter alert is always allowed through). Quiet hours only
delay in-app/push/email *delivery*; the notification still exists immediately in the inbox with
its true `created_at`.

## Digest batching (the "money moment")

When `notification_pref.digest_freq` is `WEEKLY` or `MONTHLY` for a `(type, channel)`, a matching
low/medium notification is not delivered individually: `deliver_after` is set to the period
boundary (owner-local Monday 08:00 for weekly, 1st 08:00 for monthly), and the row is attached to
the OPEN `notification_digest` for that `(owner, channel, freq, period_start)` (auto-created;
`item_count++`). A `@Scheduled` **digest sweep** at each boundary marks the digest `SENT`,
renders one roll-up message ("Your week in money: 3 near-limit envelopes, 1 idle-cash insight, ₹X
recoverable"), and flips its items to `SENT`. `CRITICAL`/`HIGH` items are never digested — they
deliver immediately regardless of `digest_freq`. `IMMEDIATE`/`DAILY` freqs deliver per-item (daily
= a same-day quiet-hours-style hold, no batch row).

## Due-date reminders (scheduler, not an event)

Forecasting emits no reminder event (it only projects), so Notification **generates** due
reminders itself from the shared recurring-obligation registry (conventions §12.3, reserved seam
#3) read through a `DueObligationsPort` (backed today by `recurring_rule` via forecasting, and by
the obligation registry as it lands). A `@Scheduled` daily job (owner-local morning) computes, for
each active obligation, the next due date; when it falls within `notification.reminderLeadDays`
(default 3), it applies a synthetic `DUE_REMINDER` through the *same pipeline* (rank/dedup/quiet/
digest) with `dedup_key = DUE_REMINDER:{ruleId}:{dueDate}` so a rerun of the job the next day does
not re-remind for the same due date. This is a generated notification, not a consumed event — it
uses the same idempotent upsert path, so re-running the scheduler is safe.

## Claude enrichment (optional, behind the same pipeline)

An optional `BodyEnricher` port. When enabled for a `type`, after the template body is built the
enricher may replace it with **one human sentence** generated by Claude, grounded strictly in the
numbers already present in `detail` (the source event's own figures — e.g. an insight's
`rationale`, an overspend's `overMinor`/`fundedMinor`). The prompt is constrained to
rephrase-only: no new facts, no advice beyond the source's `suggested_action`, numbers passed
through verbatim. `body_source` records `CLAUDE` vs `TEMPLATE` for provenance and A/B. Enrichment
is **best-effort and non-blocking**: a timeout or failure falls back to the deterministic template
body (the notification is never dropped for want of enrichment), and it happens on the delivery
sweep, not in the event `handle()` path, so the consumer stays fast and deterministic. Because it
only ever rewrites numbers the record already contains, it introduces no new source of truth and
survives a projection rebuild (the template body is always reproducible; the enriched body is a
presentation layer).

## Delivery pipeline (channels)

The **delivery sweep** is a `@Scheduled` poller (default 5s) that selects `PENDING` rows with
`deliver_after <= now()` (index `notification_due_idx`), grouped by channel, `FOR UPDATE SKIP
LOCKED` (multi-instance safe, Phase 6). For each:

- `IN_APP` — the baseline channel; "delivery" is just flipping `status = SENT`, `sent_at = now`
  so it surfaces in the inbox query. No external call.
- `EMAIL` / `PUSH` — a pluggable `NotificationChannel { boolean deliver(Notification n) }`
  implementation calls the gateway; on success → `SENT`; on transient failure it stays `PENDING`
  with a backed-off `deliver_after` (bounded retries; a permanently failing send is logged and
  left for the next sweep, never lost). Channel sends are idempotent on `notification.id` so a
  retried sweep can't double-send.

Channels are additive: Phase 3 ships `IN_APP` only; `EMAIL`/`PUSH` register later with no change
to the consumer, schema, or ranking/dedup logic.

## Core operations (`NotificationApi`)

Read-and-inbox-management API (creation is event/schedule-driven, never by a caller):

- **`listInbox(filter, cursor, limit)`** — owner-scoped, cursor-paginated (opaque `seq`),
  ordered by `priority_rank DESC, created_at DESC`. Filter by `status`, `type`, `severity`,
  `channel`. Default excludes `DISMISSED`.
- **`getNotification(id)`** — full row incl. `detail` and `body_source`.
- **`markRead(id)`** — `SENT|PENDING → READ`, sets `read_at`. Idempotent. Local inbox state only
  — does not touch the source record.
- **`markAllRead(filter?)`** — bulk read within a filter; idempotent.
- **`dismiss(id)`** — `→ DISMISSED`, sets `dismissed_at`; drops out of the live-dedup index so a
  later genuine re-raise of the same subject can surface again. Idempotent.
- **`unreadCount()`** — badge count of `PENDING|SENT` (owner-scoped), for the UI.
- **`getPreferences()` / `updatePreference(type, channel, cmd)`** — read/patch
  `enabled`, `quiet_hours`, `digest_freq`, `min_severity` for a `(type, channel)`. Writes
  `notification_pref` (owner-scoped). `enabled=false` is the unsubscribe.
- **`getDigest(digestId)`** — the rendered roll-up + its item list (for the digest view / email
  body inspection).

`NotificationApi` is the only cross-context entry point (conventions §11). It never calls into
another context's repositories; it reads producers only via the events they already emit (and the
`DueObligationsPort` for reminders). Rendering (§Rendering) pulls a canonical merchant/label
through the relevant read API when a human string is needed, never a table.

## Rendering

`title`/`body` templates are per `type`, filled from the event payload's own numbers (money as
`{amountMinor,currency}`, formatted at presentation with HALF_EVEN, §2). Examples:

- `BUDGET_OVERSPENT` → *"Dining out is ₹1,240 over its ₹8,000 budget this month."*
- `INSIGHT` (idle cash) → *"₹1,20,000 is sitting idle in HDFC Savings earning 3% — moving it
  could earn ~₹4,800/yr."* (template) or the Claude-enriched one-liner (same numbers).
- `DUE_REMINDER` → *"Rent of ₹35,000 is due in 3 days (25 Jul)."*
- `SYSTEM` DEAD-letter → *"A background job stalled (consumer `analytics`, event 90412) — some
  data may be delayed."*

Rendering is deterministic given the event payload; the enriched variant is the only
non-deterministic body and is always falling-back-able to the template.

## Events consumed

Via the platform dispatcher; consumer `name = "notification"`, idempotent on
`(consumer_name, event_id)` (conventions §6). Notification is a pure consumer — it **emits
nothing to the outbox** (an optional internal `NotificationDelivered` for UI cache-busting only,
never consumed by another context).

| Event | Source | Effect |
|---|---|---|
| `BudgetOverspent` | Budgeting | raise/refresh `BUDGET_OVERSPENT` (CRITICAL) |
| `SafeToSpendChanged` | Budgeting | raise `SAFE_TO_SPEND` on cross < 0; auto-dismiss when ≥ 0 |
| `budget_alert` events (`ENVELOPE_NEAR_LIMIT`, `SAFE_TO_SPEND_NEGATIVE`) | Budgeting | raise/refresh per type |
| `InsightRaised` | Optimization | raise/refresh `INSIGHT` at the event's severity |
| `InsightResolved` | Optimization | auto-dismiss the live `INSIGHT` for that `dedup_key` |
| `DiscrepancyFlagged` | Reconciliation | raise `DISCREPANCY` (HIGH for `STALE_IN_TRANSIT`) |
| `TransactionProposed` | Import | open/bump coalesced `REVIEW_READY` for the batch |
| `ImportBatchCompleted` | Import | finalize the batch's `REVIEW_READY` ("N ready to review") |
| `DeliveryDead` | Platform | raise `SYSTEM` CRITICAL (the DEAD-letter operational alert) |

Redelivery of any of the above is a no-op (dedup + the `source_event_id` guard). Unknown event
types and unknown payload fields are tolerated (conventions §6). Payloads carry `schemaVersion`.

> **Producer note:** the platform DEAD-letter alert (`01-platform.md`: "after N attempts → DEAD …
> + alert") is emitted as a `DeliveryDead` outbox event so it flows through this same delivery
> pipeline rather than a side-channel — a small, additive change to platform's alert step.

## Phasing

- **Phase 3 (launch):** in-app inbox only. At launch the live producers are Reconciliation
  (`DiscrepancyFlagged`, Phase 2), Import review-ready, due reminders, and the platform
  DEAD-letter alert. Budgeting events (`BudgetOverspent`, `SafeToSpendChanged`, Phase 4) and
  Optimization insight events (Phase 6) begin delivering automatically as those contexts ship — no
  Notification change, since it keys off event types, not producers.
- **Later:** `EMAIL` / `PUSH` channels register behind `NotificationChannel`; Claude enrichment
  enables per type; digest cadences and ranking weights tune via config.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/notifications` | inbox (owner-scoped, filter status/type/severity/channel, cursor) |
| GET | `/api/v1/notifications/unread-count` | badge count of PENDING\|SENT |
| GET | `/api/v1/notifications/{id}` | one notification incl. detail |
| POST | `/api/v1/notifications/{id}/read` | mark read |
| POST | `/api/v1/notifications/read-all` | bulk mark read (optional filter) |
| POST | `/api/v1/notifications/{id}/dismiss` | dismiss |
| GET | `/api/v1/notifications/preferences` | list per-(type,channel) prefs |
| PUT | `/api/v1/notifications/preferences/{type}/{channel}` | set enabled/quiet_hours/digest_freq/min_severity |
| GET | `/api/v1/notifications/digests/{id}` | rendered digest roll-up |

Conventions apply (§11): `/api/v1`, JSON, cursor pagination, ISO-8601 UTC, money as
`{amountMinor,currency}`, RFC 9457 `application/problem+json` with a stable `code` enum, e.g.
`NOTIFICATION_NOT_FOUND`, `NOTIFICATION_INVALID_TRANSITION` (dismiss/read on a bad state),
`NOTIFICATION_UNKNOWN_TYPE`, `NOTIFICATION_PREF_INVALID` (bad quiet-hours / digest_freq). No POST
here creates financial state, so no `Idempotency-Key` is required on these read/inbox operations;
the idempotency that matters is on the *consume* path (per-event, §Consuming).

## Testing

- **Idempotent apply (the headline invariant):** redelivering the same producer event (same
  `event_id`) creates no second notification — `processed_event` no-ops, and the
  `UNIQUE (owner_id, source_event_id, channel)` guard holds under a forced replay. At-least-once
  delivery never double-notifies.
- **Oscillation / dedup:** a metric crossing a threshold up-down-up (e.g. `SafeToSpendChanged`
  negative → non-negative → negative, or Optimization re-raising the same `dedup_key`) yields
  exactly one live notification, refreshed in place; the cooldown suppresses re-arming an
  already-SENT row unless severity escalates. Asserted at the DB level too (partial unique index
  rejects a second live row).
- **Auto-dismiss:** `InsightResolved` / a cleared `budget_alert` dismisses the matching live
  notification; a fresh raise after dismissal surfaces a new one.
- **Ranking:** for generated severity/type/recency mixes, the inbox order equals
  `priority_rank DESC, created_at DESC`; a `CRITICAL` always outranks a `LOW`; ties break by
  recency.
- **Quiet hours (owner-local, §3):** a MEDIUM event in the quiet window is held (`deliver_after`
  = window end) and released after; a `CRITICAL` in the same window delivers immediately; a
  post-23:30 IST event evaluates against the IST window, not UTC.
- **Digest:** WEEKLY/MONTHLY items accumulate into one `notification_digest`, the sweep sends one
  roll-up at the owner-local boundary and flips items to SENT; CRITICAL/HIGH are never digested.
- **Preferences / unsubscribe:** disabling `(type, channel)` produces no row for that channel;
  `min_severity` floors suppress below-floor events; a re-enable resumes delivery for future
  events.
- **Review-ready coalescing:** N `TransactionProposed` for one batch produce one `REVIEW_READY`
  with `proposedCount = N`; `ImportBatchCompleted` finalizes it.
- **Due reminders:** the scheduler raises a `DUE_REMINDER` within the lead window and re-running
  the job the next day does not re-remind for the same due date (dedup_key on `dueDate`).
- **Claude enrichment fallback:** with the enricher forced to time out/fail, the notification is
  still delivered with the deterministic template body and `body_source = TEMPLATE`; a successful
  enrichment sets `CLAUDE` and passes through the source numbers verbatim (no fabricated figure).
- **Channel pluggability:** a fake `EMAIL` channel that fails once then succeeds is retried with
  backoff and eventually SENT, never double-sent (idempotent on `notification.id`); IN_APP needs
  no gateway.
- **No-truth-mutation:** static/integration assertion that Notification holds no INSERT/UPDATE
  grant on `ledger_transaction`, `posting`, `budget_alert`, `insight`, or `recon_*`; marking
  read/dismissed touches only `notification`.
- **Owner scoping (conventions §4):** dispatching an owner-B event materializes the notification
  under owner B; one owner never reads another's inbox; async owner scope resolves B's prefs.
- **API contract tests:** each endpoint — happy path, validation (422 on bad quiet-hours /
  digest_freq), and error `code`.
- **Persistence:** integration tests on real Postgres (Testcontainers) exercising the partial
  unique index, the due-delivery index, and the SKIP LOCKED delivery sweep.
```
