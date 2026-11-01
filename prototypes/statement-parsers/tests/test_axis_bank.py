import unittest

from statement_parser.axis_bank import AxisOneGlanceParser, AxisQuickViewParser


AXIS_BANK_TEXT = """
Axis Bank
Statement for account no. XXXXXXXXXXX0001 for the period from 01-09-2026 to 30-09-2026
Date Transaction details Chq no. Withdrawal Deposits Balance
Opening Balance 500.00
Closing Balance 500.00
Total 0.00 0.00
Statement for account no. XXXXXXXXXXX0002 for the period from 01-09-2026 to 30-09-2026
Date Transaction details Chq no. Withdrawal Deposits Balance
Opening Balance 1,000.00
01-09-2026 SYNTHETIC PURCHASE 200.00 0.00 800.00
continuation detail
02-09-2026 SYNTHETIC CREDIT 0.00 50.00 850.00
Closing Balance 850.00
Total 200.00 50.00
"""


class AxisOneGlanceParserTest(unittest.TestCase):
    def test_parses_and_validates_multiple_account_sections(self) -> None:
        result = AxisOneGlanceParser().parse_text(AXIS_BANK_TEXT)

        self.assertEqual("axis.one-glance.savings.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-30", result.statement_date.isoformat())
        self.assertEqual((-20_000, 5_000), tuple(x.amount_minor for x in result.transactions))
        self.assertEqual("SYNTHETIC PURCHASE continuation detail", result.transactions[0].description)
        self.assertIsNone(result.opening_balance_minor)
        self.assertIsNone(result.closing_balance_minor)

    def test_rejects_an_unreconciled_account_section(self) -> None:
        invalid = AXIS_BANK_TEXT.replace("Closing Balance 850.00", "Closing Balance 851.00")

        with self.assertRaisesRegex(ValueError, "closing balance"):
            AxisOneGlanceParser().parse_text(invalid)

    def test_rejects_duplicate_masked_suffixes_in_one_statement(self) -> None:
        duplicate = AXIS_BANK_TEXT.replace("XXXXXXXXXXX0002", "XXXXXXXXXXX0001")

        with self.assertRaisesRegex(ValueError, "duplicate account suffix"):
            AxisOneGlanceParser().parse_text(duplicate)

    def test_records_a_one_paise_bank_rounding_discrepancy(self) -> None:
        text = AXIS_BANK_TEXT.replace("200.00 0.00 800.00", "200.00 0.00 799.99").replace(
            "0.00 50.00 850.00", "0.00 50.00 849.99"
        ).replace("Closing Balance 850.00", "Closing Balance 849.99")

        result = AxisOneGlanceParser().parse_text(text)

        self.assertEqual(1, len(result.issues))
        self.assertEqual("RUNNING_BALANCE_ROUNDING", result.issues[0].code)

    def test_labels_transactions_with_a_safe_masked_account_suffix(self) -> None:
        result = AxisOneGlanceParser().parse_text(AXIS_BANK_TEXT)

        self.assertEqual(
            ("axis-account-0002", "axis-account-0002"),
            tuple(transaction.account_ref for transaction in result.transactions),
        )
        self.assertEqual(
            ("Axis Savings • 0002", "Axis Savings • 0002"),
            tuple(transaction.account_label for transaction in result.transactions),
        )


class AxisQuickViewParserTest(unittest.TestCase):
    def test_parses_quick_view_rows_from_balance_deltas(self) -> None:
        text = """
Axis Bank
Detailed Statement for a/c no. XXXXXXXXXXX0001 between 01-04-2026 to 30-04-2026
Account Statement
Txn Date Transaction Withdrawals Deposits Balance
Opening Balance 1,000.00
10-04-2026 SYNTHETIC PURCHASE 200.00 800.00
Ref#SYNTHETIC
25-04-2026 SYNTHETIC CREDIT 0.00 50.00 850.00
Closing Balance 850.00
"""

        result = AxisQuickViewParser().parse_text(text)

        self.assertEqual("axis.quick-view.savings.pdf.v0", result.adapter_id)
        self.assertEqual((-20_000, 5_000), tuple(x.amount_minor for x in result.transactions))
        self.assertEqual("SYNTHETIC PURCHASE Ref#SYNTHETIC", result.transactions[0].description)
        self.assertEqual("axis-account-0001", result.transactions[0].account_ref)
        self.assertEqual("Axis Savings • 0001", result.transactions[0].account_label)
        self.assertTrue(result.is_balance_reconciled)


if __name__ == "__main__":
    unittest.main()
