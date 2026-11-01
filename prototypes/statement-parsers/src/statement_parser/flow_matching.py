"""Conservative matching of owned-account transfers and card repayments."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import re
from typing import Iterable


_OWN_ACCOUNT_EVIDENCE = re.compile(
    r"\b(?:self[ -]?transfer|own[ -]?account|transfer to self|transfer from self)\b",
    re.IGNORECASE,
)
_CARD_PAYMENT_EVIDENCE = re.compile(
    r"\b(?:payment received|card payment|credit card payment|bbps)\b",
    re.IGNORECASE,
)
_BANK_CARD_PAYMENT_EVIDENCE = re.compile(
    r"\b(?:credit card|card bill|cc payment|bill payment|autopay|bbps)\b",
    re.IGNORECASE,
)
_CARD_PAYMENT_EXCLUSION = re.compile(
    r"\b(?:refund|reversal|reversed|cashback|chargeback)\b",
    re.IGNORECASE,
)
_TRANSACTION_REFERENCE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
MAX_MATCH_CANDIDATES = 1_000_000
MAX_MATCH_COMPARISONS = 2_000_000


@dataclass(frozen=True)
class FlowTransaction:
    transaction_id: str
    account_ref: str
    account_kind: str
    booked_date: date
    amount_minor: int
    currency: str
    description: str
    external_ref: str | None
    source_id: str
    source_row: int


@dataclass(frozen=True)
class CanonicalTransaction:
    transaction_id: str
    account_ref: str
    account_kind: str
    booked_date: date
    amount_minor: int
    currency: str
    description: str
    external_ref: str | None
    provenance_ids: tuple[str, ...]


@dataclass(frozen=True)
class AcceptedFlow:
    match_id: str
    match_type: str
    debit_transaction_id: str
    credit_transaction_id: str


@dataclass(frozen=True)
class FlowReviewItem:
    transaction_id: str
    reason: str
    candidate_transaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class FlowMatchResult:
    accepted: tuple[AcceptedFlow, ...]
    review_items: tuple[FlowReviewItem, ...]
    unmatched_transaction_ids: tuple[str, ...]
    invariant_violation_count: int


@dataclass(frozen=True)
class _Candidate:
    debit: CanonicalTransaction
    credit: CanonicalTransaction
    match_type: str


def deduplicate_transactions(
    transactions: Iterable[FlowTransaction],
) -> tuple[CanonicalTransaction, ...]:
    """Collapse cross-statement overlap without collapsing real same-statement repeats."""
    by_base_and_rank: dict[tuple[object, ...], list[FlowTransaction]] = defaultdict(list)
    occurrences_by_source: dict[
        tuple[tuple[object, ...], str], list[FlowTransaction]
    ] = defaultdict(list)

    for transaction in transactions:
        base = _deduplication_base(transaction)
        occurrences_by_source[(base, transaction.source_id)].append(transaction)

    for (base, _source_id), occurrences in occurrences_by_source.items():
        for rank, transaction in enumerate(
            sorted(occurrences, key=lambda item: (item.source_row, item.transaction_id))
        ):
            by_base_and_rank[(*base, rank)].append(transaction)

    canonical = tuple(
        _canonicalize(key, occurrences)
        for key, occurrences in sorted(
            by_base_and_rank.items(), key=lambda item: repr(item[0])
        )
    )
    return tuple(sorted(canonical, key=lambda item: item.transaction_id))


def match_owned_flows(
    transactions: Iterable[CanonicalTransaction],
    *,
    maximum_day_gap: int = 3,
) -> FlowMatchResult:
    if (
        isinstance(maximum_day_gap, bool)
        or not isinstance(maximum_day_gap, int)
        or not 0 <= maximum_day_gap <= 31
    ):
        raise ValueError("Maximum day gap must be an integer from 0 to 31")
    ordered = tuple(sorted(transactions, key=lambda item: item.transaction_id))
    debits = tuple(item for item in ordered if item.amount_minor < 0)
    credits = tuple(item for item in ordered if item.amount_minor > 0)
    credits_by_amount_and_date: dict[
        tuple[str, int, date], list[CanonicalTransaction]
    ] = defaultdict(list)
    for credit in credits:
        credits_by_amount_and_date[
            (credit.currency, credit.amount_minor, credit.booked_date)
        ].append(credit)
    structural_candidates: list[_Candidate] = []
    comparison_count = 0
    for debit in debits:
        for day_offset in range(-maximum_day_gap, maximum_day_gap + 1):
            try:
                candidate_date = debit.booked_date + timedelta(days=day_offset)
            except OverflowError:
                continue
            matching_credits = credits_by_amount_and_date.get(
                (debit.currency, -debit.amount_minor, candidate_date), ()
            )
            for credit in matching_credits:
                comparison_count += 1
                if comparison_count > MAX_MATCH_COMPARISONS:
                    raise ValueError("Owned-flow analysis exceeds the comparison limit")
                candidate = _structural_candidate(
                    debit,
                    credit,
                    maximum_day_gap=maximum_day_gap,
                )
                if candidate is None:
                    continue
                structural_candidates.append(candidate)
                if len(structural_candidates) > MAX_MATCH_CANDIDATES:
                    raise ValueError("Owned-flow analysis exceeds the candidate limit")
    transaction_ids_by_reference: dict[str, set[str]] = defaultdict(set)
    for transaction in ordered:
        for reference in _transaction_references(transaction.description):
            transaction_ids_by_reference[reference].add(transaction.transaction_id)
    semantic_candidates = tuple(
        candidate
        for candidate in structural_candidates
        if _has_semantic_evidence(candidate)
    )
    reference_candidates = tuple(
        candidate
        for candidate in structural_candidates
        if not _has_hard_contradiction(candidate)
        and _has_unique_shared_reference(candidate, transaction_ids_by_reference)
    )
    preferred_candidates = tuple(
        {
            _candidate_key(candidate): candidate
            for candidate in (*semantic_candidates, *reference_candidates)
        }.values()
    )
    preferred_by_transaction: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in preferred_candidates:
        preferred_by_transaction[candidate.debit.transaction_id].append(candidate)
        preferred_by_transaction[candidate.credit.transaction_id].append(candidate)

    accepted: list[AcceptedFlow] = []
    consumed: set[str] = set()
    for candidate in sorted(
        semantic_candidates,
        key=lambda item: (item.debit.transaction_id, item.credit.transaction_id),
    ):
        debit_id = candidate.debit.transaction_id
        credit_id = candidate.credit.transaction_id
        if (
            len(preferred_by_transaction[debit_id]) != 1
            or len(preferred_by_transaction[credit_id]) != 1
        ):
            continue
        if debit_id in consumed or credit_id in consumed:
            continue
        accepted.append(
            AcceptedFlow(
                match_id=_digest("match", candidate.match_type, debit_id, credit_id),
                match_type=candidate.match_type,
                debit_transaction_id=debit_id,
                credit_transaction_id=credit_id,
            )
        )
        consumed.update((debit_id, credit_id))

    remaining_preferred = tuple(
        candidate
        for candidate in preferred_candidates
        if candidate.debit.transaction_id not in consumed
        and candidate.credit.transaction_id not in consumed
    )
    review_candidates = tuple(
        candidate
        for candidate in structural_candidates
        if candidate.debit.transaction_id not in consumed
        and candidate.credit.transaction_id not in consumed
    )
    review_by_transaction: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in review_candidates:
        review_by_transaction[candidate.debit.transaction_id].append(candidate)
        review_by_transaction[candidate.credit.transaction_id].append(candidate)
    review_preferred_by_transaction: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in remaining_preferred:
        review_preferred_by_transaction[candidate.debit.transaction_id].append(candidate)
        review_preferred_by_transaction[candidate.credit.transaction_id].append(candidate)
    reference_keys = {_candidate_key(candidate) for candidate in reference_candidates}

    review_items = tuple(
        FlowReviewItem(
            transaction_id=transaction_id,
            reason=(
                "AMBIGUOUS_MULTIPLE_CANDIDATES"
                if transaction_id in review_preferred_by_transaction
                and _is_ambiguous(
                    transaction_id,
                    review_preferred_by_transaction[transaction_id],
                    review_preferred_by_transaction,
                )
                else "REFERENCE_CORRELATED_CANDIDATE"
                if any(
                    _candidate_key(candidate) in reference_keys
                    for candidate in transaction_candidates
                )
                else "INSUFFICIENT_SEMANTIC_EVIDENCE"
            ),
            candidate_transaction_ids=tuple(
                sorted(
                    {
                        candidate.credit.transaction_id
                        if candidate.debit.transaction_id == transaction_id
                        else candidate.debit.transaction_id
                        for candidate in transaction_candidates
                    }
                )
            ),
        )
        for transaction_id, transaction_candidates in sorted(
            review_by_transaction.items()
        )
    )
    matched_ids = {
        transaction_id
        for match in accepted
        for transaction_id in (match.debit_transaction_id, match.credit_transaction_id)
    }
    unmatched = tuple(
        item.transaction_id for item in ordered if item.transaction_id not in matched_ids
    )
    by_id = {item.transaction_id: item for item in ordered}
    violations = sum(
        1
        for match in accepted
        if not _match_invariants_hold(
            match, by_id, maximum_day_gap=maximum_day_gap
        )
    )
    return FlowMatchResult(
        accepted=tuple(accepted),
        review_items=review_items,
        unmatched_transaction_ids=unmatched,
        invariant_violation_count=violations,
    )


def _deduplication_base(transaction: FlowTransaction) -> tuple[object, ...]:
    if transaction.external_ref:
        return (
            "external",
            transaction.account_ref,
            transaction.external_ref,
            transaction.booked_date.isoformat(),
            transaction.amount_minor,
            transaction.currency,
        )
    description = " ".join(transaction.description.casefold().split())
    return (
        "fallback",
        transaction.account_ref,
        transaction.booked_date.isoformat(),
        transaction.amount_minor,
        transaction.currency,
        description,
    )


def _canonicalize(
    key: tuple[object, ...], occurrences: list[FlowTransaction]
) -> CanonicalTransaction:
    representative = min(
        occurrences,
        key=lambda item: (item.source_id, item.source_row, item.transaction_id),
    )
    canonical_id = _digest("transaction", *(str(part) for part in key))
    return CanonicalTransaction(
        transaction_id=canonical_id,
        account_ref=representative.account_ref,
        account_kind=representative.account_kind,
        booked_date=representative.booked_date,
        amount_minor=representative.amount_minor,
        currency=representative.currency,
        description=representative.description,
        external_ref=representative.external_ref,
        provenance_ids=tuple(sorted(item.transaction_id for item in occurrences)),
    )


def _eligible_candidate(
    debit: CanonicalTransaction,
    credit: CanonicalTransaction,
    *,
    maximum_day_gap: int,
) -> _Candidate | None:
    candidate = _structural_candidate(
        debit, credit, maximum_day_gap=maximum_day_gap
    )
    if candidate is None or not _has_semantic_evidence(candidate):
        return None
    return candidate


def _structural_candidate(
    debit: CanonicalTransaction,
    credit: CanonicalTransaction,
    *,
    maximum_day_gap: int,
) -> _Candidate | None:
    if debit.account_ref == credit.account_ref:
        return None
    if debit.currency != credit.currency or debit.amount_minor + credit.amount_minor != 0:
        return None
    if abs((debit.booked_date - credit.booked_date).days) > maximum_day_gap:
        return None
    if debit.account_kind == "ASSET" and credit.account_kind == "LIABILITY":
        return _Candidate(debit=debit, credit=credit, match_type="CARD_PAYMENT")
    if debit.account_kind == credit.account_kind == "ASSET":
        return _Candidate(debit=debit, credit=credit, match_type="INTERNAL_TRANSFER")
    return None


def _has_semantic_evidence(candidate: _Candidate) -> bool:
    if candidate.match_type == "CARD_PAYMENT":
        return bool(
            not _CARD_PAYMENT_EXCLUSION.search(candidate.credit.description)
            and _CARD_PAYMENT_EVIDENCE.search(candidate.credit.description)
            and _BANK_CARD_PAYMENT_EVIDENCE.search(candidate.debit.description)
        )
    return bool(
        _OWN_ACCOUNT_EVIDENCE.search(candidate.debit.description)
        or _OWN_ACCOUNT_EVIDENCE.search(candidate.credit.description)
    )


def _has_hard_contradiction(candidate: _Candidate) -> bool:
    return bool(
        candidate.match_type == "CARD_PAYMENT"
        and _CARD_PAYMENT_EXCLUSION.search(candidate.credit.description)
    )


def _transaction_references(description: str) -> frozenset[str]:
    return frozenset(_TRANSACTION_REFERENCE.findall(description))


def _has_unique_shared_reference(
    candidate: _Candidate,
    transaction_ids_by_reference: dict[str, set[str]],
) -> bool:
    shared_references = _transaction_references(
        candidate.debit.description
    ) & _transaction_references(candidate.credit.description)
    candidate_ids = {
        candidate.debit.transaction_id,
        candidate.credit.transaction_id,
    }
    return any(
        transaction_ids_by_reference[reference] == candidate_ids
        for reference in shared_references
    )


def _candidate_key(candidate: _Candidate) -> tuple[str, str]:
    return (candidate.debit.transaction_id, candidate.credit.transaction_id)


def _match_invariants_hold(
    match: AcceptedFlow,
    by_id: dict[str, CanonicalTransaction],
    *,
    maximum_day_gap: int,
) -> bool:
    debit = by_id[match.debit_transaction_id]
    credit = by_id[match.credit_transaction_id]
    candidate = _eligible_candidate(
        debit, credit, maximum_day_gap=maximum_day_gap
    )
    return candidate is not None and candidate.match_type == match.match_type


def _is_ambiguous(
    transaction_id: str,
    candidates: list[_Candidate],
    by_transaction: dict[str, list[_Candidate]],
) -> bool:
    if len(candidates) > 1:
        return True
    candidate = candidates[0]
    counterpart = (
        candidate.credit.transaction_id
        if candidate.debit.transaction_id == transaction_id
        else candidate.debit.transaction_id
    )
    return len(by_transaction[counterpart]) > 1


def _digest(*parts: str) -> str:
    material = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]
