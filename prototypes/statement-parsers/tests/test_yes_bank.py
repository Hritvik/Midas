import unittest

from statement_parser.yes_bank import YesBankSavingsParser


YES_TEXT = """
YOUR CONSOLIDATED STATEMENT FOR SEP' 26
Statement Of Transactions In Saving Account No: 0000 For The Period Of 01-Sep-2026 to 30-Sep-2026
Value Date Description Withdrawals Deposits Running Balance
Date No/Reference No.
01/09/2026 01/09/2026 B/F ... 1,000.00
SYNTHETIC SERVICE CHARGE
01/09/2026 01/09/2026 200.00 0.00 800.00
02/09/2026 02/09/2026 SYNTHETIC CREDIT 0.00 50.00 850.00
Opening Balance : 1,000.00 Total Withdrawals : 200.00 Total Deposits: 50.00 Closing Balance : 850.00
"""


class YesBankSavingsParserTest(unittest.TestCase):
    def test_parses_and_reconciles_savings_transactions(self) -> None:
        result = YesBankSavingsParser().parse_text(YES_TEXT)

        self.assertEqual("yes.savings-account.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-30", result.statement_date.isoformat())
        self.assertEqual((-20_000, 5_000), tuple(
            item.amount_minor for item in result.transactions
        ))
        self.assertEqual((80_000, 85_000), tuple(
            item.running_balance_minor for item in result.transactions
        ))
        self.assertEqual("SYNTHETIC SERVICE CHARGE", result.transactions[0].description)
        self.assertTrue(result.is_balance_reconciled)

    def test_rejects_summary_mismatch(self) -> None:
        invalid = YES_TEXT.replace("Closing Balance : 850.00", "Closing Balance : 851.00")

        with self.assertRaisesRegex(ValueError, "closing balance"):
            YesBankSavingsParser().parse_text(invalid)

    def test_accepts_a_reconciled_statement_with_no_activity(self) -> None:
        text = """
YOUR CONSOLIDATED STATEMENT FOR AUG' 26
Statement Of Transactions In Saving Account No: 0000 For The Period Of 01-Aug-2026 to 31-Aug-2026
Value Date Description Withdrawals Deposits Running Balance
01/08/2026 01/08/2026 B/F ... 1,000.00
Opening Balance : 1,000.00 Total Withdrawals : 0.00 Total Deposits: 0.00 Closing Balance : 1,000.00
"""

        result = YesBankSavingsParser().parse_text(text)

        self.assertEqual((), result.transactions)
        self.assertTrue(result.is_balance_reconciled)


if __name__ == "__main__":
    unittest.main()
