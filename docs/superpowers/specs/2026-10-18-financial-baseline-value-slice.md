# Financial Baseline Value Slice

**Status:** Approved for prototype validation  
**Date:** 2026-10-18

## Capability

For the owner, turn the existing private statement corpus into explainable answers about
near-term commitments, emergency resilience, safe discretionary spending, safe investment
capacity, and trade-offs between long-term goals. This is a product-shaped validation slice,
not ledger truth and not a replacement for the planned Import/Reconciliation contexts.

## Current goals and policies

| Goal | Target | Horizon | Policy |
|---|---:|---:|---|
| Trip | INR 200,000 | 3 months | Hard near-term reserve; do not expose to market risk |
| Wedding | INR 2,000,000 | 4 months | Hard near-term reserve; do not expose to market risk |
| Emergency reserve | 6 months of essential spending | Continuous | Protected; not available for dip buying |
| Business | INR 10,000,000 | Open | Scenario-only until target date and inflation basis are supplied |
| Own home | Open | Future | Lifestyle/security goal; evaluate affordability and rent-vs-buy |
| Investment property | Open | Future | Must pass cash-flow, concentration, liquidity, and alternative-investment tests |

Dip buying uses a separate opportunity reserve funded only after the hard near-term goals,
operating buffer, emergency reserve, and known obligations are protected.

All discovered financial accounts belong to the owner. Transfers between them must not count
as income or spending. A cash withdrawal enters review as `UNRESOLVED_CASH`; the user can mark
it as spending, transfer, cash still held, or split. After a configurable reminder period it
may default to spending, retaining an inferred/not-confirmed marker and remaining correctable.

## First questions the slice must answer

1. What dates, institutions, accounts, and statement periods are covered or missing?
2. What is normal monthly income, essential spending, discretionary spending, and surplus?
3. Which flows are internal transfers, card repayments, refunds, or reversals?
4. What recurring obligations and irregular-but-predictable costs exist?
5. What must be reserved now for the trip and wedding, and what funding gap remains?
6. What is the six-month essential-expense reserve and current runway?
7. After protected amounts, what is safe to spend and safe to invest?
8. What happens to the business/home/property timelines under alternative priorities?
9. What is the highest-impact next action, with evidence and confidence?

## Trust constraints

- Every number retains source-document and parser-version provenance.
- Parser output is staging evidence, never ledger truth.
- Ambiguous classifications remain visible and correctable.
- Account identity must be stable across statements without exposing raw account numbers.
- Exact source duplicates are collapsed; overlapping statement entries are not silently
  collapsed without account identity and a defensible match.
- Reports distinguish observed facts, user-confirmed facts, inferred values, and scenarios.
- No recommendation may allocate protected goal or emergency money to investing.

## First visual surfaces

- Coverage and confidence timeline
- Four-month cash calendar for trip and wedding commitments
- Goal-funding waterfall
- Monthly income/spending/surplus trend
- Transfer and card-payment review graph
- Essential and current-lifestyle emergency-runway gauges
- Goal-competition simulator, including home and investment-property scenarios

## Current corpus readiness

The clean v5 rebuild contains 201 corpus-backed parsed statements and 3,260 transactions,
covering 2023-06-02 through 2026-10-01. It intentionally excludes one stale HDFC output that
was counted by the earlier incremental scan. Sixty-eight statements provide a complete balance
reconciliation; 133 layouts do not expose both opening and closing balances; none report a
failed reconciliation. Ten parser issues are explicitly recorded.

The account-identity blocker is resolved for this corpus. All 201 statements and all 3,260
transactions carry an opaque account reference, representing 13 account/subaccount identities.
Raw account numbers are scrubbed by default. Identity scopes are private random UUIDs, so
credential aliases, last-four values, and passwords cannot be recovered from emitted refs.
The private outputs and scope map are owner-only and ignored by Git.

The first conservative economic-flow pass reduced the evidence to 3,219 canonical transaction
occurrences by collapsing 41 cross-statement overlaps. It now finds 170 transaction legs across
92 candidate pairs: 58 legs form 29 same-day pairs with a unique shared 12-digit narration
reference, while 112 legs participate in amount/date-only candidates with insufficient semantic
evidence. The earlier 11 “ambiguous” groups were an artefact of presenting 14 weak amount/date
collisions as equal to their reference-correlated alternatives. Evidence ranking now recommends
the stronger pair without hiding the weaker choices.
No transfer or card-payment pair is auto-accepted yet. Raw narration references rank review
evidence but remain untrusted until an institution parser emits a typed, validated transfer
reference. All matcher invariants pass. User confirmation or stronger institution-specific
evidence is the next blocker before computing monthly spending, surplus, reserves, or goal
capacity.

A private throwaway review prototype now presents the evidence as 78 review questions covering
92 candidate pairs. Eleven questions show one recommended reference-correlated match alongside
lower-confidence alternatives; none currently contain equally supported ambiguous choices. It
offers three switchable structures: a sequential decision inbox,
a batch evidence table, and an account-flow map. Decisions are deliberately in-memory only;
the prototype is for choosing the interaction model before implementing durable corrections.
Account labels now use institution/product names, with masked statement suffixes where needed
to distinguish multiple Axis savings accounts. The same YES card and Axis accounts are unified
across differently protected files and statement layouts rather than shown as duplicate accounts.

## Open decisions

- Business target date and whether INR 10,000,000 is nominal or in today's purchasing power
- Own-home target horizon, geography, and acceptable rent-versus-buy lifestyle assumptions
- Investment-property horizon, target geography, and maximum real-estate concentration
- Essential/discretionary corrections learned from the first classification review
- Reminder period before unresolved cash defaults to inferred spending

## Handoff

Build transfer/card-payment matching next, review uncertain matches, and then produce the
coverage, monthly-flow, and four-month goal-funding analyses in that order. Promote only
validated rules into the production Phase 2/3 architecture.
