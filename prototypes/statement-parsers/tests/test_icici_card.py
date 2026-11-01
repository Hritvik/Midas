import unittest

from statement_parser.icici_card import IciciCreditCardParser


ICICI_TEXT = """
ICICI Bank Credit Card GST Number: 00SYNTHETIC
Statement period : December 16, 2025 to January 15, 2026
Date SerNo. Transaction Details Reward Intl.# Amount (in`)
22/12/2025 14032508817 SAMPLE GROCER BANGALORE IN 25 504.00
29/12/2025 14072946863 SAMPLE REFUND BANGALORE -56 1,127.92 CR
02/01/2026 14048757794 SAMPLE SERVICE 0 N 204.50
"""


class IciciCreditCardParserTest(unittest.TestCase):
    def test_parses_transactions_and_owner_relative_signs(self) -> None:
        result = IciciCreditCardParser().parse_text(ICICI_TEXT)

        self.assertEqual("icici.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual("2026-01-15", result.statement_date.isoformat())
        self.assertEqual((-50_400, 112_792, -20_450), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual(
            ("SAMPLE GROCER BANGALORE IN", "SAMPLE REFUND BANGALORE", "SAMPLE SERVICE"),
            tuple(transaction.description for transaction in result.transactions),
        )

    def test_rejects_a_statement_without_transaction_rows(self) -> None:
        text = """
ICICI Bank Credit Card GST Number: 00SYNTHETIC
Statement period : August 16, 2026 to September 15, 2026
Date SerNo. Transaction Details Reward Intl.# Amount (in`)
"""

        with self.assertRaisesRegex(ValueError, "no transaction rows"):
            IciciCreditCardParser().parse_text(text)


if __name__ == "__main__":
    unittest.main()
