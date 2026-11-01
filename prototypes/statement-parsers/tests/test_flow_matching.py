from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

from statement_parser.flow_matching import (
    FlowTransaction,
    deduplicate_transactions,
    match_owned_flows,
)


def _transaction(
    transaction_id: str,
    *,
    account: str = "acct_a",
    kind: str = "ASSET",
    booked_date: str = "2026-09-10",
    amount: int = -10_000,
    description: str = "own account transfer",
    external_ref: str | None = None,
    source: str = "statement-a",
    row: int = 1,
) -> FlowTransaction:
    return FlowTransaction(
        transaction_id=transaction_id,
        account_ref=account,
        account_kind=kind,
        booked_date=date.fromisoformat(booked_date),
        amount_minor=amount,
        currency="INR",
        description=description,
        external_ref=external_ref,
        source_id=source,
        source_row=row,
    )


class DeduplicationTest(unittest.TestCase):
    def test_collapses_same_external_reference_across_statements(self) -> None:
        first = _transaction("one", external_ref="bank-ref", source="statement-a")
        overlap = _transaction("two", external_ref="bank-ref", source="statement-b")

        result = deduplicate_transactions((first, overlap))

        self.assertEqual(1, len(result))
        self.assertEqual(("one", "two"), result[0].provenance_ids)

    def test_fallback_key_collapses_overlap_but_preserves_identical_repeats(self) -> None:
        transactions = (
            _transaction("a1", source="statement-a", row=1),
            _transaction("a2", source="statement-a", row=2),
            _transaction("b1", source="statement-b", row=1),
            _transaction("b2", source="statement-b", row=2),
        )

        result = deduplicate_transactions(transactions)

        self.assertEqual(2, len(result))
        self.assertEqual({2}, {len(item.provenance_ids) for item in result})

    def test_never_collapses_across_accounts(self) -> None:
        result = deduplicate_transactions(
            (
                _transaction("one", account="acct_a"),
                _transaction("two", account="acct_b"),
            )
        )

        self.assertEqual(2, len(result))

    def test_external_reference_preserves_repeats_and_conflicting_facts(self) -> None:
        transactions = (
            _transaction("a1", external_ref="reused", source="statement-a", row=1),
            _transaction("a2", external_ref="reused", source="statement-a", row=2),
            _transaction("b1", external_ref="reused", source="statement-b", row=1),
            _transaction("b2", external_ref="reused", source="statement-b", row=2),
            _transaction(
                "different",
                external_ref="reused",
                source="statement-b",
                row=3,
                amount=-20_000,
            ),
        )

        result = deduplicate_transactions(transactions)

        self.assertEqual(3, len(result))
        self.assertEqual([1, 2, 2], sorted(len(item.provenance_ids) for item in result))


class FlowMatchingTest(unittest.TestCase):
    def test_accepts_semantically_supported_internal_transfer(self) -> None:
        debit = _transaction("debit", account="acct_a", amount=-10_000)
        credit = _transaction(
            "credit", account="acct_b", amount=10_000, description="self transfer received"
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual(1, len(result.accepted))
        self.assertEqual("INTERNAL_TRANSFER", result.accepted[0].match_type)
        self.assertEqual(0, result.invariant_violation_count)

    def test_accepts_card_payment_from_bank_debit_to_card_credit(self) -> None:
        debit = _transaction(
            "debit", account="acct_bank", amount=-25_000, description="credit card bill"
        )
        credit = _transaction(
            "credit",
            account="acct_card",
            kind="LIABILITY",
            amount=25_000,
            description="payment received thank you",
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual("CARD_PAYMENT", result.accepted[0].match_type)

    def test_equal_opposite_amounts_without_semantic_evidence_are_not_accepted(self) -> None:
        debit = _transaction("debit", description="merchant purchase")
        credit = _transaction(
            "credit", account="acct_b", amount=10_000, description="salary adjustment"
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual((), result.accepted)
        self.assertTrue(
            any(item.reason == "INSUFFICIENT_SEMANTIC_EVIDENCE" for item in result.review_items)
        )

    def test_card_refund_is_not_treated_as_a_payment(self) -> None:
        debit = _transaction("debit", account="acct_bank", amount=-25_000)
        credit = _transaction(
            "credit",
            account="acct_card",
            kind="LIABILITY",
            amount=25_000,
            description="payment reversal refund",
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual((), result.accepted)
        self.assertTrue(
            any(item.reason == "INSUFFICIENT_SEMANTIC_EVIDENCE" for item in result.review_items)
        )

    def test_broad_card_side_phrase_without_bank_evidence_is_not_accepted(self) -> None:
        debit = _transaction(
            "debit", account="acct_bank", amount=-25_000, description="merchant purchase"
        )
        credit = _transaction(
            "credit",
            account="acct_card",
            kind="LIABILITY",
            amount=25_000,
            description="thank you",
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual((), result.accepted)

    def test_rejects_same_account_currency_mismatch_and_outside_window(self) -> None:
        debit = _transaction("debit")
        same_account = _transaction("same", amount=10_000)
        other_currency = FlowTransaction(
            **{**_transaction("currency", account="acct_b", amount=10_000).__dict__, "currency": "USD"}
        )
        late = _transaction(
            "late", account="acct_c", amount=10_000, booked_date="2026-09-14"
        )

        result = match_owned_flows(
            deduplicate_transactions((debit, same_account, other_currency, late))
        )

        self.assertEqual((), result.accepted)

    def test_ambiguous_strong_candidates_are_sent_to_review(self) -> None:
        debit = _transaction("debit")
        first = _transaction("first", account="acct_b", amount=10_000)
        second = _transaction("second", account="acct_c", amount=10_000)

        result = match_owned_flows(deduplicate_transactions((debit, first, second)))

        self.assertEqual((), result.accepted)
        self.assertTrue(
            any(item.reason == "AMBIGUOUS_MULTIPLE_CANDIDATES" for item in result.review_items)
        )

    def test_result_is_independent_of_input_order(self) -> None:
        debit = _transaction("debit")
        credit = _transaction("credit", account="acct_b", amount=10_000)

        forward = match_owned_flows(deduplicate_transactions((debit, credit)))
        reverse = match_owned_flows(deduplicate_transactions((credit, debit)))

        self.assertEqual(forward, reverse)

    def test_rejects_negative_matching_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "day gap"):
            match_owned_flows((), maximum_day_gap=-1)
        for invalid in (True, 32, 1.5):
            with self.subTest(day_gap=invalid):
                with self.assertRaisesRegex(ValueError, "day gap"):
                    match_owned_flows((), maximum_day_gap=invalid)  # type: ignore[arg-type]

    def test_date_boundary_does_not_overflow(self) -> None:
        debit = _transaction("debit", booked_date="0001-01-01")

        result = match_owned_flows(deduplicate_transactions((debit,)))

        self.assertEqual((), result.accepted)

    def test_custom_window_is_used_by_invariant_validation(self) -> None:
        debit = _transaction("debit", booked_date="2026-09-10")
        credit = _transaction(
            "credit", account="acct_b", amount=10_000, booked_date="2026-09-14"
        )

        result = match_owned_flows(
            deduplicate_transactions((debit, credit)), maximum_day_gap=4
        )

        self.assertEqual(1, len(result.accepted))
        self.assertEqual(0, result.invariant_violation_count)

    def test_caps_candidate_comparison_work_even_when_pairs_are_ineligible(self) -> None:
        debits = (
            _transaction("debit-1", account="acct_a"),
            _transaction("debit-2", account="acct_a", row=2),
        )
        credit = _transaction("credit", account="acct_a", amount=10_000)

        with patch("statement_parser.flow_matching.MAX_MATCH_COMPARISONS", 1):
            with self.assertRaisesRegex(ValueError, "comparison limit"):
                match_owned_flows(deduplicate_transactions((*debits, credit)))

    def test_counterpart_of_ambiguous_transaction_is_also_marked_ambiguous(self) -> None:
        debit = _transaction("debit")
        first = _transaction("first", account="acct_b", amount=10_000)
        second = _transaction("second", account="acct_c", amount=10_000)

        result = match_owned_flows(deduplicate_transactions((debit, first, second)))

        self.assertEqual(
            {"AMBIGUOUS_MULTIPLE_CANDIDATES"},
            {item.reason for item in result.review_items},
        )

    def test_unique_shared_reference_prioritizes_review_without_auto_accepting(self) -> None:
        debit = _transaction(
            "debit",
            description="transfer reference 123456789012",
        )
        referenced_credit = _transaction(
            "referenced-credit",
            account="acct_b",
            amount=10_000,
            description="received reference 123456789012",
        )
        coincidental_credit = _transaction(
            "coincidental-credit",
            account="acct_c",
            amount=10_000,
            description="cash deposit",
        )

        result = match_owned_flows(
            deduplicate_transactions((debit, referenced_credit, coincidental_credit))
        )

        self.assertEqual((), result.accepted)
        canonical = deduplicate_transactions(
            (debit, referenced_credit, coincidental_credit)
        )
        expected_ids = {item.transaction_id for item in canonical}
        self.assertEqual(
            expected_ids,
            {item.transaction_id for item in result.review_items},
        )
        self.assertEqual(
            {"REFERENCE_CORRELATED_CANDIDATE", "INSUFFICIENT_SEMANTIC_EVIDENCE"},
            {item.reason for item in result.review_items},
        )
        shared_item = next(
            item for item in result.review_items if len(item.candidate_transaction_ids) == 2
        )
        self.assertEqual("REFERENCE_CORRELATED_CANDIDATE", shared_item.reason)

    def test_two_unique_reference_backed_candidates_remain_ambiguous(self) -> None:
        debit = _transaction(
            "debit",
            description="references 123456789012 and 987654321098",
        )
        first = _transaction(
            "first",
            account="acct_b",
            amount=10_000,
            description="received 123456789012",
        )
        second = _transaction(
            "second",
            account="acct_c",
            amount=10_000,
            description="received 987654321098",
        )

        result = match_owned_flows(deduplicate_transactions((debit, first, second)))

        self.assertEqual((), result.accepted)
        self.assertEqual(
            {"AMBIGUOUS_MULTIPLE_CANDIDATES"},
            {item.reason for item in result.review_items},
        )

    def test_reference_repeated_across_three_transactions_is_not_strong_evidence(self) -> None:
        debit = _transaction("debit", description="reference 123456789012")
        first = _transaction(
            "first",
            account="acct_b",
            amount=10_000,
            description="received 123456789012",
        )
        second = _transaction(
            "second",
            account="acct_c",
            amount=10_000,
            description="received 123456789012",
        )

        result = match_owned_flows(deduplicate_transactions((debit, first, second)))

        self.assertEqual((), result.accepted)

    def test_short_shared_number_is_not_transaction_reference_evidence(self) -> None:
        debit = _transaction("debit", description="transfer 123456")
        credit = _transaction(
            "credit",
            account="acct_b",
            amount=10_000,
            description="received 123456",
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual((), result.accepted)

    def test_shared_reference_does_not_override_card_refund_exclusion(self) -> None:
        debit = _transaction(
            "debit",
            account="acct_bank",
            description="card activity reference 123456789012",
            amount=-25_000,
        )
        credit = _transaction(
            "credit",
            account="acct_card",
            kind="LIABILITY",
            amount=25_000,
            description="refund reference 123456789012",
        )

        result = match_owned_flows(deduplicate_transactions((debit, credit)))

        self.assertEqual((), result.accepted)
        self.assertEqual(
            {"INSUFFICIENT_SEMANTIC_EVIDENCE"},
            {item.reason for item in result.review_items},
        )


if __name__ == "__main__":
    unittest.main()
