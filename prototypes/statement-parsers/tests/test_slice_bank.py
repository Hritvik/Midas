import unittest

from statement_parser.slice_bank import SliceBankStatementParser


SLICE_BANK_TEXT = """
01 Sep '26 - 30 Sep '26
Customer ID 123456 Account SAVING
Opening balance Total credits Interest earned Total debits Closing balance
+ + - =
₹104.82 ₹42.12 ₹0.02 ₹8.60 ₹138.34
DATE DETAILS REF NO. AMOUNT BALANCE
01 Sep '26 Interest Cr. for 31-Aug-2026 123456789 ₹0.02 ₹104.84
10 Sep '26 Account Transfer-Credit-REF 987654321 ₹42.10 ₹146.94
17 Sep '26 Account Transfer-Debit-REF 111222333 -₹8.60 ₹138.34
"""


class SliceBankStatementParserTest(unittest.TestCase):
    def test_parses_running_balance_deltas_and_reconciles(self) -> None:
        result = SliceBankStatementParser().parse_text(SLICE_BANK_TEXT)

        self.assertEqual("slice.savings-account.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-30", result.statement_date.isoformat())
        self.assertEqual(10_482, result.opening_balance_minor)
        self.assertEqual(13_834, result.closing_balance_minor)
        self.assertEqual((2, 4_210, -860), tuple(
            item.amount_minor for item in result.transactions
        ))
        self.assertEqual((10_484, 14_694, 13_834), tuple(
            item.running_balance_minor for item in result.transactions
        ))
        self.assertTrue(result.is_balance_reconciled)

    def test_rejects_a_row_whose_amount_disagrees_with_balance_delta(self) -> None:
        invalid = SLICE_BANK_TEXT.replace("-₹8.60 ₹138.34", "-₹9.60 ₹138.34")

        with self.assertRaisesRegex(ValueError, "balance delta"):
            SliceBankStatementParser().parse_text(invalid)

    def test_accepts_an_explicit_empty_statement(self) -> None:
        text = """
01 Jun '26 - 30 Jun '26
Opening balance Total credits Interest earned Total debits Closing balance
+ + - =
₹7.84 ₹0.00 ₹0.00 ₹0.00 ₹7.84
No transactions found
slice small finance bank
"""

        result = SliceBankStatementParser().parse_text(text)

        self.assertEqual((), result.transactions)
        self.assertTrue(result.is_balance_reconciled)


if __name__ == "__main__":
    unittest.main()
