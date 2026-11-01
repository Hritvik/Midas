# Midas work items

Status lines are the central coordination baseline. Source: `AGENTS.md` and
`docs/superpowers/HANDOFF.md`.

## W-1 — Ratify the HLD and ADRs

**Status.** Blocked — awaiting owner review of `docs/superpowers/HANDOFF.md`.
**Summary.** Approve the HLD and ADR-001…015 or apply the owner's requested changes, so Phase 1 design is no longer gated.
**Depends on.** None.

## W-2 — Write the Phase 1 LLD

**Status.** Planned.
**Summary.** Author the ledger-core and opening-balances LLD: class design, method contracts, sequences, data dictionary, error catalog.
**Depends on.** `W-1.`

## W-3 — Write the Phase 1 build plan

**Status.** Planned.
**Summary.** Turn the Phase 1 LLD into a TDD build plan with task-sized increments and evidence gates.
**Depends on.** `W-2.`

## W-4 — Execute Phase 0 and Phase 1

**Status.** Planned.
**Summary.** Run the held Phase 0 foundation plan, then Phase 1, phase by phase with real-Postgres tests.
**Depends on.** `W-3.`

## W-5 — UX foundation pass

**Status.** Ready.
**Summary.** Produce journeys and low-fidelity wireframes for the core screens; validate the Phase 3 read-API shapes.
**Depends on.** None.

## W-6 — Security and launch-readiness phase

**Status.** Planned.
**Summary.** Build auth/MFA, encryption at rest, export/deletion/backup, and resolve crypto-shredding against the append-only ledger.
**Depends on.** `W-4.`

## W-7 — Resolve investment decisioning

**Status.** Blocked — the proposal is intentionally unratified.
**Summary.** Decide the `investmentdecisioning/` boundary and policy questions before changing the north-star map, HLD, or roadmap.
**Depends on.** None.
