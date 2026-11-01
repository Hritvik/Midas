import unittest

from statement_parser.kotak_bank import KotakSavingsParser


KOTAK_TEXT = """
Kotak Mahindra Bank Ltd.
Savings Account Transactions
# Date Description Chq/Ref. No. Withdrawal (Dr.) Deposit (Cr.) Balance
- - Opening Balance - - - 1,000.00
1 14 Sep 2026 SYNTHETIC CREDIT REF001 200.00 1,200.00
2 18 Sep 2026 SYNTHETIC PURCHASE REF002 50.00 1,150.00
continuation detail
Account Statement 01 Sep 2026 - 30 Sep 2026
Account Summary
Particulars Opening Balance Closing Balance
Savings Account (SA): 1,000.00 1,150.00
End of Statement
"""


class KotakSavingsParserTest(unittest.TestCase):
    def test_parses_signs_from_running_balance_deltas(self) -> None:
        result = KotakSavingsParser().parse_text(KOTAK_TEXT)

        self.assertEqual("kotak.savings-account.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-30", result.statement_date.isoformat())
        self.assertEqual((20_000, -5_000), tuple(x.amount_minor for x in result.transactions))
        self.assertEqual("SYNTHETIC PURCHASE REF002 continuation detail", result.transactions[1].description)
        self.assertTrue(result.is_balance_reconciled)

    def test_rejects_a_final_balance_mismatch(self) -> None:
        invalid = KOTAK_TEXT.replace("Savings Account (SA): 1,000.00 1,150.00", "Savings Account (SA): 1,000.00 1,151.00")

        with self.assertRaisesRegex(ValueError, "closing balance"):
            KotakSavingsParser().parse_text(invalid)


if __name__ == "__main__":
    unittest.main()
