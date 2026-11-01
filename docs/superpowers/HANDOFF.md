# Midas — Handoff / Current Status

**As of:** 2026-07-24 · **Stage:** design-complete, pre-build (no app code yet).
**Start here, then read `AGENTS.md` and the docs it lists.**

---

## Where we are

The full design chain is done and committed **except** the last two steps before coding:

| Stage | State |
|---|---|
| Requirements (PRD + north-star spec) | ✅ committed |
| Detailed per-context design (`detailed/00`–`11`) | ✅ committed, audit-amended |
| Adversarial audit + product roadmap | ✅ committed (consolidated) |
| High-Level Design + ADRs (incl. tech stack) | ✅ committed |
| **Phase 1 LLD** (ledger core + opening balances) | ⏳ NOT written — next design step |
| **Phase 0 build plan** | ✅ written, **held** (not executed) |
| Any application code | ❌ none yet |

## How we got here (chronology)

1. Brainstormed the PRD → north-star spec (ledger-first truth + outbox backbone, modular monolith, 7 phases, single-user-first).
2. Broad-first detailed design of all contexts (8 original + amendments).
3. Added the In-Transit clearing account for cross-bank/card transfers.
4. Ran a 14-agent adversarial + product audit (~12 CRITICAL + ~35 HIGH findings).
5. Applied Tier 0 + Tier 1 remediation across every context; added two new contexts — **`category`** (shared taxonomy/resolver) and **`notification`** (delivers alert/insight records that previously went nowhere); reserved seams for loans/illiquid/obligations.
6. Wrote the Phase 0 build plan (held).
7. Authored the consolidated **HLD + ADR-001…015** (fills the architecture gap; ratifies the backend stack).

## Locked decisions (see HLD ADRs for rationale)

- Ledger-first double-entry; modular monolith; transactional outbox with per-consumer delivery.
- Net worth = cost + Σ unrealized gain (single formula).
- Category = dedicated `category/` context. Reserve seams now for loans/illiquid/obligations; full contexts later.
- Money = BIGINT minor units + per-lot total cost. Owner-local time + Apr–Mar FY.
- Stack: Java 21 / Spring Boot 3.3 / Gradle / Spring Data JDBC (no JPA) / Flyway / Testcontainers.
- API-first; first real UI at Phase 3; one responsive Next.js PWA.

## Open threads (decisions awaiting the human)

1. **HLD review** — was sent to the user for review; approving it unblocks Phase 1 LLD. Check whether they returned changes.
2. **UX foundation pass** (journeys + low-fi wireframes for ~6 core screens) — undecided whether to run it **in parallel** with Phase 1 LLD (recommended — it validates the Phase 3 read-API shapes) or sequentially.
3. **Deferred stack deliberations:** frontend stack (state, data-fetching, component/design system, charts) before the UX pass / Phase 3; infra/deploy/observability/auth/secrets before first deploy / Phase 6. The user's environment is GCP + Grafana + ArgoCD — likely relevant then.
4. **Security build-out** (auth/MFA, encryption-at-rest, export/deletion/backup) is architected in HLD §8 but needs its own phase before launch; the deletion-vs-append-only resolution (crypto-shredding) is a candidate ADR-011 to firm up.
5. **Investment Decisioning proposal** — `specs/2026-08-01-investment-decisioning-proposal.md`
   proposes a strategy-neutral `investmentdecisioning/` capability, with reusable market data
   and Dip Radar as its first strategy. It is intentionally unratified: resolve the listed
   boundary and policy decisions before changing the north-star context map, HLD, or roadmap.

## Recommended next steps (in order)

1. Get HLD sign-off (or apply requested changes).
2. Write **Phase 1 LLD** (ledger core + **opening balances** — the correctness-blocking onboarding piece; the ledger has the `EQUITY` type ready to hang it on). Class design, method contracts, per-operation sequence diagrams, data dictionary, error catalog — grounded in `detailed/02-ledger.md`.
3. Write the **Phase 1 build plan**; then execute **Phase 0** (already planned) and **Phase 1**, phase by phase, TDD.
4. (Parallel, optional) UX foundation pass.

## What did NOT survive the session (ephemeral)

- The **raw per-context audit findings** (14 scratchpad files) are gone. Their substance is preserved in `specs/2026-07-23-audit-and-opportunities.md` (consolidated) and in each context doc's "Audit fixes applied" table — nothing actionable is lost, but the verbose per-finding detail is not recoverable.
- Conversation narrative / reasoning not captured in an ADR or doc.
- A Claude "ask questions in plain text" user preference (harmless to Codex).

## Gotchas for the next agent

- The Phase 0 plan hard-codes Gradle + Spring Data JDBC + Flyway + Testcontainers (per the ADRs). If the team standardizes on Maven, that's the one easy pre-execution swap (ADR-012).
- Tests need **Docker** (Testcontainers pulls `postgres:16-alpine`). H2 is explicitly rejected.
- Every context doc has an "Audit fixes applied" table — trust the amended doc, not any pre-audit assumption.
- `detailed/00-conventions.md` is binding and overrides any per-context wording if they ever conflict.
