from pathlib import Path
import sys
import unittest


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTOTYPE_ROOT))

from statement_parsers import ParseError, parse_extracted_text  # noqa: E402


AXIS_TEXT = """
Airtel Axis Bank Mastercard Credit Card Statement
Statement Period Payment Due Date Statement Generation Date
14/04/2026 - 12/05/2026 01/06/2026 12/05/2026
DATE TRANSACTION DETAILS MERCHANT CATEGORY AMOUNT (Rs.)
25/04/2026 CARD PAYMENT #SYNTHETIC                               1,898.60 Cr
26/04/2026 SAMPLE UTILITIES MERCHANT,GURGAON UTILITIES              619.00 Dr
10/05/2026 SAMPLE REVERSAL,GURGAON UTILITIES                        151.00 Cr
10/05/2026 SAMPLE REVERSAL,GURGAON UTILITIES                        151.00 Dr
**** End of Statement ****
Schedule of charges
25th Sep Purchase Db 5000
"""


SBI_TEXT = """
CASHBACK SBI CARD Monthly Statement
Statement Date 11 May 2026
Payment Due Date 31 May 2026
Date Transaction Details Amount (Rs.)
for Statement Period: 12 Apr 26 to 11 May 26
11 Apr 26 CARD CASHBACK CREDIT                         992.00 C
11 May 26 ANNUAL FEE CHARGED                           999.00 D
       IGST DB @ 18.00%                                179.82 D
TRANSACTIONS FOR SAMPLE PERSON
11 Apr 26 SAMPLE SHOP THANE IN (Pay in EMIs)         4,134.00 D
29 Apr 26 SAMPLE MERCHANT MUMBAI IN                    149.00 C
Important Messages
--- PAGE ---
Transaction Details
Date                                             Amount (Rs.)
for Statement Period: 12 Apr 26 to 11 May 26
08 May 26 SAMPLE RAIL MERCHANT                         385.72 D
10 May 26 SAMPLE TELECOM                               51.00 D
10 May 26 SAMPLE TELECOM                               51.00 C
Important Messages
2nd June Total Amount Due 1200
"""


class AxisParserTest(unittest.TestCase):
    def test_parses_axis_rows_with_liability_cashflow_signs(self):
        statement = parse_extracted_text(AXIS_TEXT)

        self.assertEqual("axis.credit-card.pdf.v0", statement.adapter_id)
        self.assertEqual("2026-04-14", statement.period_start.isoformat())
        self.assertEqual("2026-05-12", statement.period_end.isoformat())
        self.assertEqual(4, len(statement.entries))
        self.assertEqual(189_860, statement.entries[0].amount_minor)
        self.assertEqual(-61_900, statement.entries[1].amount_minor)
        self.assertEqual(15_100, statement.entries[2].amount_minor)
        self.assertEqual(-15_100, statement.entries[3].amount_minor)

    def test_ignores_axis_schedule_examples_after_end_marker(self):
        statement = parse_extracted_text(AXIS_TEXT)

        self.assertNotIn("Purchase Db", [entry.description for entry in statement.entries])


class SbiParserTest(unittest.TestCase):
    def test_parses_sbi_rows_across_pages_and_keeps_real_repeats(self):
        statement = parse_extracted_text(SBI_TEXT)

        self.assertEqual("sbi.credit-card.pdf.v0", statement.adapter_id)
        self.assertEqual("2026-04-12", statement.period_start.isoformat())
        self.assertEqual("2026-05-11", statement.period_end.isoformat())
        self.assertEqual(8, len(statement.entries))
        self.assertEqual(99_200, statement.entries[0].amount_minor)
        self.assertEqual(-99_900, statement.entries[1].amount_minor)
        self.assertEqual(-17_982, statement.entries[2].amount_minor)
        self.assertEqual(-5_100, statement.entries[-2].amount_minor)
        self.assertEqual(5_100, statement.entries[-1].amount_minor)

    def test_inherits_date_for_sbi_tax_continuation_row(self):
        statement = parse_extracted_text(SBI_TEXT)

        tax = statement.entries[2]
        self.assertEqual("2026-05-11", tax.booked_on.isoformat())
        self.assertEqual("IGST DB @ 18.00%", tax.description)

    def test_ignores_sbi_regulatory_examples_after_transaction_section(self):
        statement = parse_extracted_text(SBI_TEXT)

        self.assertNotIn("Total Amount Due", [entry.description for entry in statement.entries])


class DetectionAndErrorsTest(unittest.TestCase):
    def test_rejects_unknown_statement_layout(self):
        with self.assertRaises(ParseError):
            parse_extracted_text("An unrelated PDF document")


if __name__ == "__main__":
    unittest.main()
