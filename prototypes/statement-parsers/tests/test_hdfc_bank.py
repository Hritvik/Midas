import unittest

from statement_parser.hdfc_bank import HdfcBankStatementParser


HDFC_TEXT = """
HDFC BANK
Statement From : 01/09/2026 To 30/09/2026
Opening Balance : 1,000.00
Txn Date Narration Withdrawals Deposits Closing Balance
01/09/2026 SAMPLE SALARY CREDIT 200.00 1,200.00
02/09/2026 SAMPLE AUTOPAY 50.00 1,150.00
Value Dt 02/09/2026 Ref SYNTHETIC
SUMMARY
Opening Balance Debit Amount Credit Amount Closing Balance
1,000.00 50.00 200.00 1,150.00
*** End of Statement ***
"""


class HdfcBankStatementParserTest(unittest.TestCase):
    def test_parses_hdfc_rows_from_running_balance_deltas(self) -> None:
        result = HdfcBankStatementParser().parse_text(HDFC_TEXT)

        self.assertEqual("hdfc.savings-account.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-30", result.statement_date.isoformat())
        self.assertEqual(100_000, result.opening_balance_minor)
        self.assertEqual(115_000, result.closing_balance_minor)
        self.assertTrue(result.is_balance_reconciled)
        self.assertEqual((20_000, -5_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual(
            "SAMPLE AUTOPAY Value Dt 02/09/2026 Ref SYNTHETIC",
            result.transactions[1].description,
        )

    def test_parses_a_row_with_missing_extracted_narration(self) -> None:
        text = """
HDFC BANK
Statement From : 01/01/2026 To 31/01/2026
Opening Balance : 1,000.00
Txn Date Narration Withdrawals Deposits Closing Balance
07/01/2026 576.00 0.00 424.00
SUMMARY
Opening Balance Debit Amount Credit Amount Closing Balance
1,000.00 576.00 0.00 424.00
*** End of Statement ***
"""

        result = HdfcBankStatementParser().parse_text(text)

        self.assertEqual((-57_600,), tuple(x.amount_minor for x in result.transactions))
        self.assertEqual("Unlabelled transaction", result.transactions[0].description)
        self.assertNotIn("SUMMARY", result.transactions[0].description)

    def test_rejects_a_declared_summary_that_does_not_reconcile(self) -> None:
        invalid = HDFC_TEXT.replace(
            "1,000.00 50.00 200.00 1,150.00",
            "1,000.00 50.00 200.00 1,151.00",
        )

        with self.assertRaisesRegex(ValueError, "summary"):
            HdfcBankStatementParser().parse_text(invalid)


if __name__ == "__main__":
    unittest.main()
