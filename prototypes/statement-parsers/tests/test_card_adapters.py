import unittest

from statement_parser.card_adapters import parse_extracted_text


AXIS_TEXT = """
Airtel Axis Bank Mastercard Credit Card Statement
Statement Period Payment Due Date Statement Generation Date
14/04/2026 - 12/05/2026 01/06/2026 12/05/2026
DATE TRANSACTION DETAILS MERCHANT CATEGORY AMOUNT (Rs.)
25/04/2026 CARD PAYMENT #SYNTHETIC                        1,898.60 Cr
26/04/2026 SAMPLE UTILITIES MERCHANT                      619.00 Dr
10/05/2026 SAMPLE REVERSAL                                151.00 Cr
10/05/2026 SAMPLE REVERSAL                                151.00 Dr
**** End of Statement ****
Schedule of charges
25/09/2026 Purchase Db 5,000.00 Dr
"""

SBI_TEXT = """
CASHBACK SBI CARD Monthly Statement
Statement Date 11 May 2026
Date Transaction Details Amount (Rs.)
for Statement Period: 12 Apr 26 to 11 May 26
11 Apr 26 CARD CASHBACK CREDIT                         992.00 C
11 May 26 ANNUAL FEE CHARGED                           999.00 D
       IGST DB @ 18.00%                                179.82 D
Important Messages
Transaction Details
Date                                             Amount (Rs.)
for Statement Period: 12 Apr 26 to 11 May 26
08 May 26 SAMPLE RAIL MERCHANT                         385.72 D
10 May 26 SAMPLE TELECOM                                51.00 D
10 May 26 SAMPLE TELECOM                                51.00 C
Important Messages
2nd June Total Amount Due 1200
"""

SLICE_TEXT = """
CUSTOMER's
Credit card statement
slice credit card MITC
X X X X X X X X X X X X 3 5 7 8
21 AUG - 20 SEP
Statement summary
Spends ₹2,777.00
Refunds & repayments ₹500.00
Cashback ₹725.00
Total amount due ₹1,552.00
Spends
SAMPLE GROCER ₹306
17 Sep '26 • UPI
SAMPLE APPAREL ₹1,079.50
10 Sep '26 • UPI
Refunds & repayments
SAMPLE REFUND ₹500
11 Sep '26
Cashback
SAMPLE GROCER ₹50
17 Sep '26
"""

ICICI_TEXT = """
ICICI Bank Credit Card Statement
Date SerNo. Transaction Details Reward Intl.# Amount (in`)
Points amount
4315XXXXXXXX8007
18/07/2026 1234 BBPS PAYMENT RECEIVED 0 10,000.00 CR
02/08/2026 1235 SAMPLE ONLINE MERCHANT 27 799.00
IN
05/08/2026 1237 SAMPLE FLAGGED MERCHANT -12 N 345.00 DR
12/08/2026 1236 INFINITY PAYMENT RECEIVED, THANK YOU 0 1,000.00 CR
IMPORTANT MESSAGES
Date SerNo. Transaction Details Reward Intl.# Amount (in`)
13/08/2026 1238 SAMPLE SECOND CARD MERCHANT 2 100.00
Statement period : July 16, 2026 to August 15, 2026
"""

YES_TEXT = """
Credit Card Statement
Statement for YES BANK Credit Card 3561XXXXXXXX5361
Statement Period: Credit Limit:
15/06/2026 To 14/07/2026 Rs. 5,00,000
Date Transaction Details Merchant Category Amount (Rs.)
17/06/2026 SAMPLE STORE - Ref No: SYNTHETIC Miscellaneous Stores 534.00 Dr
20/06/2026 FUEL SURCHARGE WAIVER - Ref No: SYNTHETIC 10.00 Cr
23/06/2026 PAYMENT RECEIVED BBPS - Ref No: SYNTHETIC 7,000.00 Cr
Reward Points Summary
"""

IDFC_CREDIT_TEXT = """
IDFC FIRST Bank
Credit Card Statement
21/Jun/2025 - 20/Jul/2025
Transaction Transaction Details EMI FX Amount
Date Eligibility Transactions (In INR)
Card Number: XXXX 1234
Purchases, EMIs & Other Debits
14 Jul 25 SAMPLE MOVIE TICKET, GURUGRAM 590.00 DR
Payments & Other Credits
BillDesk BBPS CC
28 Jun 25 9,500.00 CR
Payment/SYNTHETIC
Share your credit limit
"""

HSBC_CREDIT_TEXT = """
HSBC LIVE+ CREDIT CARD
Payment due date Minimal payment due
29 JUL 2026 100.00
Statement period Total payment due
15 JUN 2026 To 14 JUL 2026 1,000.00
DATE TRANSACTION DETAILS AMOUNTS ( )
OPENING BALANCE 6,686.71 CR
PURCHASES & INSTALLMENTS
14JUN SAMPLE MARKET BANGALORE IN 500.00
15JUN CASHBACK CREDIT 20.00 CR
12JUL SAMPLE STORE BANGALORE IN 300.00
TOTAL PURCHASE OUTSTANDING 800.00
"""


class CardAdaptersTest(unittest.TestCase):
    def test_parses_axis_liability_signs_and_ignores_boilerplate(self) -> None:
        result = parse_extracted_text(AXIS_TEXT)

        self.assertEqual("axis.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual((189_860, -61_900, 15_100, -15_100), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertNotIn("Purchase Db", [item.description for item in result.transactions])

    def test_parses_sbi_across_sections_and_inherits_continuation_date(self) -> None:
        result = parse_extracted_text(SBI_TEXT)

        self.assertEqual("sbi.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual(6, len(result.transactions))
        tax = result.transactions[2]
        self.assertEqual("2026-05-11", tax.booked_date.isoformat())
        self.assertEqual("IGST DB @ 18.00%", tax.description)
        self.assertEqual(-17_982, tax.amount_minor)
        self.assertEqual((-5_100, 5_100), tuple(
            transaction.amount_minor for transaction in result.transactions[-2:]
        ))

    def test_parses_slice_sections_with_liability_signs(self) -> None:
        result = parse_extracted_text(SLICE_TEXT)

        self.assertEqual("slice.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual("2026-09-20", result.statement_date.isoformat())
        self.assertEqual(
            (-30_600, -107_950, 50_000, 5_000),
            tuple(transaction.amount_minor for transaction in result.transactions),
        )
        self.assertEqual(
            ("SAMPLE GROCER", "SAMPLE APPAREL", "SAMPLE REFUND", "SAMPLE GROCER"),
            tuple(transaction.description for transaction in result.transactions),
        )

    def test_parses_icici_credit_card_rows_and_wrapped_descriptions(self) -> None:
        result = parse_extracted_text(ICICI_TEXT)

        self.assertEqual("icici.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual(5, len(result.transactions))
        self.assertEqual("2026-08-15", result.statement_date.isoformat())
        self.assertEqual((1_000_000, -79_900, -34_500, 100_000, -10_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual(
            "SAMPLE ONLINE MERCHANT IN",
            result.transactions[1].description,
        )

    def test_parses_yes_bank_credit_card_rows(self) -> None:
        result = parse_extracted_text(YES_TEXT)

        self.assertEqual("yes-bank.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual("2026-07-14", result.statement_date.isoformat())
        self.assertEqual((-53_400, 1_000, 700_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))

    def test_parses_idfc_credit_card_dated_and_split_credit_rows(self) -> None:
        result = parse_extracted_text(IDFC_CREDIT_TEXT)

        self.assertEqual("idfc-first.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual("2025-07-20", result.statement_date.isoformat())
        self.assertEqual((-59_000, 950_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual(
            "BillDesk BBPS CC Payment/SYNTHETIC",
            result.transactions[1].description,
        )

    def test_parses_hsbc_credit_card_compact_dates(self) -> None:
        result = parse_extracted_text(HSBC_CREDIT_TEXT)

        self.assertEqual("hsbc.credit-card.pdf.v0", result.adapter_id)
        self.assertEqual("2026-07-14", result.statement_date.isoformat())
        self.assertEqual((-50_000, 2_000, -30_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual("2026-06-14", result.transactions[0].booked_date.isoformat())

    def test_parses_slice_split_currency_glyph_layout(self) -> None:
        text = """
Credit card statement
slice credit card MITC
21 JUN - 20 JUL
Statement summary
Spends
₹
477.00
Spends
SAMPLE MARKET 367
₹
12 Jul '26 • UPI
SAMPLE CAFE 110
₹
11 Jul '26 • UPI
"""

        result = parse_extracted_text(text)

        self.assertEqual((-36_700, -11_000), tuple(
            transaction.amount_minor for transaction in result.transactions
        ))
        self.assertEqual(("SAMPLE MARKET", "SAMPLE CAFE"), tuple(
            transaction.description for transaction in result.transactions
        ))

    def test_rejects_a_slice_statement_when_no_transactions_are_extracted(self) -> None:
        text = """
Credit card statement
slice credit card MITC
21 AUG - 20 SEP
Statement summary
Spends ₹100.00
"""

        with self.assertRaisesRegex(ValueError, "no transaction rows"):
            parse_extracted_text(text)

    def test_uses_the_end_year_for_a_slice_cycle_crossing_new_year(self) -> None:
        text = """
Credit card statement
slice credit card MITC
21 DEC - 20 JAN
Statement summary
Spends ₹100.00
Spends
SAMPLE MARKET ₹100
29 Dec '25 • UPI
"""

        result = parse_extracted_text(text)

        self.assertEqual("2026-01-20", result.statement_date.isoformat())

    def test_does_not_claim_a_generic_credit_card_statement(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported statement layout"):
            parse_extracted_text("Credit card statement\nStatement summary")

    def test_rejects_an_unknown_text_layout(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported statement layout"):
            parse_extracted_text("An unrelated PDF document")


if __name__ == "__main__":
    unittest.main()
