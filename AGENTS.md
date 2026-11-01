# Midas — Agent Orientation

You are picking up **Midas**, a personal financial **decision-support system** (ledger-first
double-entry; NOT an expense tracker). Single-user first, architected to launch multi-user
later. The design is complete and **Phase 0 implementation has begun**. Production application
code lives under `src/`; the statement-parser research remains isolated under `prototypes/`.

**Read `docs/superpowers/HANDOFF.md` first** — it is the point-in-time status (what's done,
what's held, open decisions, next steps). This file is the durable orientation.

## What to read, in order

1. `docs/superpowers/specs/2026-07-19-midas-personal-treasury-os-design.md` — north-star spec (vision, 8→11 contexts, phase plan).
2. `docs/superpowers/specs/2026-07-23-hld.md` — **High-Level Design**: C4 diagrams, key sequence flows, NFRs, security, deployment, and **ADR-001…015** (decisions + rationale). Read the ADRs.
3. `docs/superpowers/specs/detailed/00-conventions.md` — **the binding shared contract.** Every context obeys it; do not restate or violate it.
4. `docs/superpowers/specs/detailed/README.md` — index of all context docs + cross-cutting invariants + reserved seams.
5. `docs/superpowers/specs/detailed/01`…`11` — per-context detailed design (LLD-ish). Each has an "Audit fixes applied" table near the top.
6. `docs/superpowers/specs/2026-07-23-audit-and-opportunities.md` — the adversarial audit + product roadmap that hardened the design.
7. `docs/superpowers/plans/2026-07-23-phase-0-foundation.md` — the first build plan (TDD, ready to execute).

## Non-negotiable conventions (full detail in `detailed/00-conventions.md`)

- **One source of truth:** only `ledger` owns financial truth (append-only double-entry, postings sum to zero). Only `reconciliation` and `investment` write it, via `LedgerApi`, in the **same physical DB transaction**. Everything else reads projections rebuilt from the outbox.
- **Money:** `BIGINT` minor units + currency; aggregate cost stored as a **total**, never rounded-per-unit-multiplied-back. Quantities are integers scaled `1e8`. No floats.
- **Net worth** (one canonical formula): `Σ assets − Σ liabilities + Σ unrealized gain`, leaf accounts, investments at cost + gain once.
- **Corrections:** reversals are correction-time and derive `REVERSED` from a reversing row (no mutable status); refunds are ordinary expense-credit transactions. Signed folds net them out.
- **Outbox:** transactional; delivery is per-`(event, consumer)` with a contiguous low-water offset; async dispatch runs under the event's owner scope. Idempotency is **reserve-first**.
- **Time:** store UTC; bucket all day/week/month/FY periods in **owner-local** time (Asia/Kolkata); Apr–Mar Financial Year is first-class.
- **Owner seam:** `owner_id` on every aggregate; `OwnerContext` returns a fixed system owner in Phases 0–5, flips to auth in Phase 6 with no schema change.

## Tech stack (ratified — ADR-012…015 in the HLD)

- Java 21, Spring Boot 3.3, **Gradle** (Groovy DSL).
- **Spring Data JDBC + `JdbcTemplate`** — NOT JPA/Hibernate (append-only ledger + Postgres-specific mechanics: deferrable constraint triggers, `FOR UPDATE SKIP LOCKED`, `RETURNING`, JSONB).
- **PostgreSQL 16**, **Flyway** migrations (forward-only, one SQL file per change).
- Tests: **JUnit 5 + Testcontainers (real Postgres)** + AssertJ + Awaitility. Do NOT use H2 — it can't do the DB features the design relies on.

## Build workflow (do not skip stages)

The design process is: **requirements → HLD/ADRs → per-phase LLD → per-phase build plan → build.**
- LLD is authored **just-in-time per phase** (not one big upfront doc). Phase 0's plan is already LLD-grade; Phase 1 onward needs an LLD before its build plan.
- Build **phase by phase**, in the order in the north-star spec (Phase 0 foundation → Phase 1 ledger → …).
- **TDD:** every task is write-failing-test → run-it-fails → minimal-impl → run-it-passes → commit. Frequent commits.
- Plans live in `docs/superpowers/plans/`. Execute with the superpowers executing-plans / subagent-driven-development workflow if available.

## Repo conventions

- Branch off `main` for work; do not commit directly to `main` for feature work.
- Conventional-commit style messages (`feat(ledger): …`, `docs: …`).
- Keep files focused and small; follow the module boundaries in the HLD component view — cross-context calls go through the public API interface only, never another context's internals.

## Project Discovery

- Production layout: `src/main` (Spring application); `src/test` (JUnit/Testcontainers tests).
- Production commands: `./gradlew test` (tests); `./gradlew build` (build); `./gradlew bootRun` (local application).

### Statement-parser prototype

- Root: `prototypes/statement-parsers`
- Language: Python 3.11+
- Packaging: setuptools via `pyproject.toml`; runtime dependencies are pdfplumber and pypdf.
- Test: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
- Run: `PYTHONPATH=src python3 -m statement_parser.cli ...`

## Planning location

Active maps, tickets, specs, designs and implementation plans live in
`~/repositories/portfolio-planning`. Do not create new planning directories or planning documents
in this repository.

- Read `~/repositories/portfolio-planning/AGENTS.md` before planning.
- Run `~/repositories/portfolio-planning/capabilities/work/task status` before claiming work.
- Promote only accepted specs/ADRs here, under `docs/`; active planning stays central.
