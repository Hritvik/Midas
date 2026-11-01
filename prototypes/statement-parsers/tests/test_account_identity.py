from __future__ import annotations

import unittest

from statement_parser.account_identity import assign_account_refs
from statement_parser.models import ParseResult, StatementTransaction


SCOPE_ID = "8d50bd46-35f4-4aed-88fd-0f0627ac48c3"
OTHER_SCOPE_ID = "a93b3388-86ca-44a8-85ef-c373f0ff3ee2"


def _result(*, account_ref: str | None = None) -> ParseResult:
    return ParseResult(
        adapter_id="axis-one-glance-v1",
        statement_date="2026-09-30",
        account_number="raw-account-number",
        currency="INR",
        opening_balance_minor=None,
        closing_balance_minor=None,
        transactions=(
            StatementTransaction(
                booked_date="2026-09-01",
                description="Test transaction",
                amount_minor=-100,
                currency="INR",
                running_balance_minor=None,
                external_ref=None,
                source_page=1,
                source_row=1,
                account_ref=account_ref,
            ),
        ),
        issues=(),
    )


class AccountIdentityTest(unittest.TestCase):
    def test_assigns_a_stable_opaque_reference_and_removes_raw_account_number(self) -> None:
        original = _result()

        first = assign_account_refs(original, SCOPE_ID)
        second = assign_account_refs(original, SCOPE_ID)

        self.assertEqual(first.transactions[0].account_ref, second.transactions[0].account_ref)
        self.assertRegex(first.transactions[0].account_ref or "", r"^acct_[0-9a-f]{24}$")
        self.assertNotIn(SCOPE_ID, first.transactions[0].account_ref or "")
        self.assertEqual(first.account_ref, first.transactions[0].account_ref)
        self.assertEqual("", first.account_number)
        self.assertEqual("raw-account-number", original.account_number)
        self.assertIsNone(original.transactions[0].account_ref)

    def test_different_scopes_produce_different_references(self) -> None:
        first = assign_account_refs(_result(), SCOPE_ID)
        second = assign_account_refs(_result(), OTHER_SCOPE_ID)

        self.assertNotEqual(first.transactions[0].account_ref, second.transactions[0].account_ref)

    def test_adapter_upgrade_does_not_change_account_identity(self) -> None:
        original = _result()
        upgraded = ParseResult(
            **{
                **original.__dict__,
                "adapter_id": "axis-one-glance-v2",
            }
        )

        first = assign_account_refs(original, SCOPE_ID)
        second = assign_account_refs(upgraded, SCOPE_ID)

        self.assertEqual(first.account_ref, second.account_ref)
        self.assertEqual(first.transactions[0].account_ref, second.transactions[0].account_ref)

    def test_preserves_distinct_subaccounts_as_distinct_opaque_references(self) -> None:
        first = assign_account_refs(_result(account_ref="axis-section-1"), SCOPE_ID)
        second = assign_account_refs(_result(account_ref="axis-section-2"), SCOPE_ID)

        self.assertNotEqual(first.transactions[0].account_ref, second.transactions[0].account_ref)

    def test_rejects_invalid_scope_instead_of_embedding_arbitrary_input(self) -> None:
        for invalid_scope in (
            "",
            "5361",
            "axis-bank-1234",
            "statementsecret",
            "00000000-0000-0000-0000-000000000000",
            "not-a-random-scope-id",
        ):
            with self.subTest(scope=invalid_scope):
                with self.assertRaisesRegex(ValueError, "account scope"):
                    assign_account_refs(_result(), invalid_scope)

    def test_none_scope_still_scrubs_raw_account_number(self) -> None:
        original = _result(account_ref="axis-section-1")

        scrubbed = assign_account_refs(original, None)

        self.assertEqual("", scrubbed.account_number)
        self.assertEqual("axis-section-1", scrubbed.transactions[0].account_ref)
        self.assertEqual("raw-account-number", original.account_number)

    def test_statement_identity_exists_when_there_are_no_transactions(self) -> None:
        original = ParseResult(
            adapter_id="axis-credit-card-v1",
            statement_date="2026-09-30",
            account_number="raw-account-number",
            currency="INR",
            opening_balance_minor=100,
            closing_balance_minor=100,
            transactions=(),
            issues=(),
        )

        scrubbed = assign_account_refs(original, SCOPE_ID)

        self.assertRegex(scrubbed.account_ref or "", r"^acct_[0-9a-f]{24}$")
        self.assertEqual((), scrubbed.transactions)


if __name__ == "__main__":
    unittest.main()
