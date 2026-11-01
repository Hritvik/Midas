# Investment Decisioning — Product and Architecture Proposal

**Date:** 2026-08-01  
**Status:** Proposed — not yet ratified into the north-star spec or HLD  
**Initial use case:** Investment Dip Radar

## Decision summary

Investment Dip Radar should be part of Midas, but dip buying must not become the organizing
model for all investment decisions. Midas should introduce a strategy-neutral
`investmentdecisioning/` bounded context and implement dip review as its first strategy.

The user-facing feature may be called **Dip Radar**. Its score should be called **Review
Priority**, not "Opportunity Score": a decline and an allocation gap can justify review, but
do not establish expected return or make a buy recommendation.

This proposal is additive and should not interrupt the current Phase 0/1 sequence. It becomes
eligible for a just-in-time LLD and build plan after the Phase 5 investment foundation defines
the concrete public APIs it consumes.

## Why this belongs in Midas

The useful version of the feature combines market observations with facts Midas already owns:

- canonical instruments and holdings;
- cost basis and current allocation;
- target allocation;
- owner-authored conviction and thesis;
- later, liquidity, goals, obligations, and deployable cash.

A separate product would either lose this context or duplicate and synchronize portfolio
truth. This capability nevertheless needs a distinct boundary so that market-data ingestion
and a single investment style do not distort the existing `investment/` model.

## Proposed bounded contexts

### `investment/` — portfolio and instrument truth

Owns canonical instruments, investment transactions, holdings, tax lots, cost basis, actual
and target allocation, portfolio returns, and ledger integration. It answers: **What do I
own, what did it cost, and how is it allocated?**

The ledger remains the sole financial truth. Investment remains the instrument-level truth.

### `marketdata/` — reusable market observations

Owns:

- provider-specific instrument identifiers and explicit symbol resolution;
- latest prices and historical candles;
- source timestamps, ingestion timestamps, provenance, and freshness;
- trading-session/calendar resolution;
- provider adapters, rate limits, retries, caching, and partial failures;
- adjusted-series policy for splits, distributions, and other corporate actions.

It answers: **What has the market reported?** Market observations are not ledger truth and
must never silently substitute an ambiguously matched instrument.

### `investmentdecisioning/` — owner-configured strategy evaluation

Owns:

- investment universes and membership metadata;
- strategy definitions and versioned owner configurations;
- deterministic evaluation runs and results;
- evidence, warnings, explanations, and review lifecycle;
- a cross-strategy review queue and dashboard projections.

It answers: **What deserves my attention under the policies I chose?** It reads other
contexts only through their public APIs and never owns holdings, transactions, current
allocation, or market observations.

## Investment universes, not one watchlist

A flat conviction watchlist is the first UI, but the domain concept should be an investment
universe. The same instrument may belong to several universes and be evaluated under different
strategies.

Examples include long-term compounders, index funds, retirement candidates, income assets,
and speculative assets with a capped allocation.

```text
InvestmentUniverse
- id
- ownerId
- name
- purpose
- active
- createdAt
- updatedAt

UniverseMember
- universeId
- instrumentId
- conviction
- thesis
- tags
- targetAllocationOverrideBps (optional)
- active
- createdAt
- updatedAt
```

Objective identity belongs to `investment.instrument`. Conviction, thesis, and tags describe
the owner's relationship to an instrument and therefore belong to `UniverseMember`.

## Strategy extension model

Strategies share an execution contract, not a universal formula:

```java
public interface InvestmentStrategy {
    StrategyType type();
    StrategyVersion version();

    EvaluationResult evaluate(
        StrategyConfiguration configuration,
        InstrumentContext instrument,
        PortfolioContext portfolio,
        MarketDataSnapshot marketData,
        EvaluationTime evaluationTime
    );
}
```

Potential implementations include:

- `DIP_REVIEW`;
- `SYSTEMATIC_ACCUMULATION`;
- `ALLOCATION_REBALANCE`;
- `VALUATION_BAND`;
- `MOMENTUM`;
- `INCOME_YIELD`;
- `THESIS_MONITORING`;
- `GOAL_BASED_ALLOCATION`;
- `RISK_REDUCTION`.

Midas does not endorse a strategy. It evaluates only strategies explicitly enabled and
configured by the owner.

## Configuration and evaluation model

Keep algorithm identity, owner configuration, execution history, and results separate:

```text
StrategyDefinition
- strategyType
- strategyVersion
- inputContractVersion
- outputContractVersion

StrategyConfiguration
- id
- ownerId
- universeId
- strategyType
- strategyVersion
- parameters
- enabled
- createdAt
- updatedAt

EvaluationRun
- id
- ownerId
- configurationId
- startedAt
- completedAt
- status
- inputCutoff
- failureSummary

StrategyEvaluation
- id
- runId
- configurationId
- instrumentId
- evaluatedAt
- marketDataAsOf
- portfolioDataAsOf
- state
- reviewPriority
- evidence
- constraints
- warnings
- strategySpecificMetrics
- expiresAt
```

Strategy and contract versions make historical results interpretable after a formula changes.
Evaluations are derived, owner-scoped, reproducible from identified inputs, and safe to
recompute. Scheduled evaluation is idempotent; one instrument failure must not fail the run.

## Common result envelope

Different strategies need different calculations but should produce a common presentation
shape:

```text
EvaluationResult
- state: NORMAL | REVIEW | HIGH_PRIORITY | UNAVAILABLE
- reviewPriority: 0..100
- headline
- evidence[]
- constraints[]
- warnings[]
- strategySpecificMetrics
```

`reviewPriority` ranks attention within the review queue. It is not expected return,
probability of profit, suitability, or an instruction to trade. Every material factor must be
visible, and stale or missing inputs must degrade or invalidate the result explicitly.

## Dip Radar as the first strategy

`DipReviewStrategy` may initially evaluate:

- one-week and one-month decline;
- drawdown from the 52-week high;
- optional drawdown from a reliable all-time high;
- owner conviction;
- actual-versus-target allocation gap;
- freshness and completeness of required data;
- simultaneous activation of multiple owner-configured rules.

Thresholds have sensible defaults with per-configuration or per-member overrides. The
strategy emits evidence such as "1M return -12%" or "allocation gap +300 bps," never an
unqualified conclusion that an asset is cheap or should be bought.

## Smallest useful vertical slice

1. One manually maintained investment universe linked to canonical Midas instruments.
2. Daily close history for stocks and ETFs from one provider adapter.
3. Trading-session-aware 1W, 1M, 3M, and 52-week drawdown metrics.
4. Configurable `DIP_REVIEW` thresholds and deterministic Review Priority explanations.
5. A read-only, sortable comparison table with missing/stale states.
6. Allocation gap only when a matching Midas holding and valid mark exist.

Mutual funds, indices, commodities, AI explanations, news, notifications, brokerage actions,
and additional strategies remain outside this first slice.

## Decisions required before ratification

1. Whether `marketdata/` warrants a top-level bounded context or begins as an internal
   `investment/` submodule with an extraction seam.
2. Whether target allocation remains asset-class-only or gains an instrument/universe-level
   policy model.
3. The canonical policy for adjusted versus unadjusted price histories and total-return
   comparisons.
4. Market calendar source and the behavior when calendar metadata is unavailable.
5. Provider coverage, licensing, retention, rate limits, and redistribution constraints.
6. Whether Review Priority is comparable across strategies or only within one strategy.
7. Where owner thesis history and thesis-review lifecycle belong once they exceed simple
   universe-member metadata.

Once these decisions are made, update the north-star context map and phase roadmap, add the
necessary HLD/ADR decisions, and author a just-in-time detailed design before implementation.
