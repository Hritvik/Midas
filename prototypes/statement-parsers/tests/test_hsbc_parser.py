from __future__ import annotations

import unittest
from datetime import date

from statement_parser.hsbc_india import HsbcIndiaCompositeParser
from statement_parser.pdf_layout import LayoutLine, LayoutPage


def line(
    text: str,
    *,
    top: float,
    date_text: str = "",
    description: str = "",
    deposit: str = "",
    withdrawal: str = "",
    balance: str = "",
) -> LayoutLine:
    return LayoutLine(
        top=top,
        text=text,
        date_text=date_text,
        description=description,
        deposit=deposit,
        withdrawal=withdrawal,
        balance=balance,
    )


class HsbcIndiaCompositeParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = HsbcIndiaCompositeParser()

    def test_parses_wrapped_rows_and_inherits_date_for_a_second_same_day_row(self) -> None:
        page = LayoutPage(
            page_number=2,
            text="HSBC Composite Statement Details of Your Accounts SAVINGS ACCOUNT-RES",
            account_number="000-111111-222",
            lines=(
                line(
                    "11Sep2026 BALANCE BROUGHT FORWARD 2,124.97",
                    top=261,
                    date_text="11Sep2026",
                    description="BALANCE BROUGHT FORWARD",
                    balance="2,124.97",
                ),
                line(
                    "14Sep2026 UPI20260914000065301",
                    top=274,
                    date_text="14Sep2026",
                    description="UPI20260914000065301",
                ),
                line("662101308880", top=287, description="662101308880"),
                line(
                    "CAFE 80.00 2,044.97",
                    top=300,
                    description="CAFE",
                    withdrawal="80.00",
                    balance="2,044.97",
                ),
                line(
                    "15Sep2026 UPI20260915000087998",
                    top=313,
                    date_text="15Sep2026",
                    description="UPI20260915000087998",
                ),
                line("129629552604", top=326, description="129629552604"),
                line(
                    "CLIENT 6,037.00 8,081.97",
                    top=339,
                    description="CLIENT",
                    deposit="6,037.00",
                    balance="8,081.97",
                ),
                line(
                    "UPI20260915000069882",
                    top=352,
                    description="UPI20260915000069882",
                ),
                line("995361830666", top=365, description="995361830666"),
                line(
                    "LANDLORD 6,000.00 2,081.97",
                    top=378,
                    description="LANDLORD",
                    withdrawal="6,000.00",
                    balance="2,081.97",
                ),
                line(
                    "CLOSING BALANCE 2,081.97",
                    top=391,
                    description="CLOSING BALANCE",
                    balance="2,081.97",
                ),
            ),
        )

        result = self.parser.parse_pages((page,), statement_date=date(2026, 10, 9))

        self.assertEqual("", result.account_number)
        self.assertEqual(3, len(result.transactions))
        self.assertEqual(-8000, result.transactions[0].amount_minor)
        self.assertEqual(603700, result.transactions[1].amount_minor)
        self.assertEqual(-600000, result.transactions[2].amount_minor)
        self.assertEqual(date(2026, 9, 15), result.transactions[2].booked_date)
        self.assertEqual(
            "UPI20260915000069882 995361830666 LANDLORD",
            result.transactions[2].description,
        )
        self.assertEqual("UPI20260915000069882", result.transactions[2].external_ref)
        self.assertEqual(208197, result.closing_balance_minor)
        self.assertTrue(result.is_balance_reconciled)

    def test_reports_row_with_both_deposit_and_withdrawal_without_aborting(self) -> None:
        page = LayoutPage(
            page_number=2,
            text="HSBC Composite Statement Details of Your Accounts SAVINGS ACCOUNT-RES",
            account_number="000-111111-222",
            lines=(
                line(
                    "01Oct2026 BALANCE BROUGHT FORWARD 100.00",
                    top=100,
                    date_text="01Oct2026",
                    description="BALANCE BROUGHT FORWARD",
                    balance="100.00",
                ),
                line(
                    "02Oct2026 BAD 10.00 20.00 110.00",
                    top=110,
                    date_text="02Oct2026",
                    description="BAD",
                    deposit="10.00",
                    withdrawal="20.00",
                    balance="110.00",
                ),
                line(
                    "03Oct2026 GOOD 5.00 105.00",
                    top=120,
                    date_text="03Oct2026",
                    description="GOOD",
                    withdrawal="5.00",
                    balance="105.00",
                ),
                line(
                    "CLOSING BALANCE 105.00",
                    top=130,
                    description="CLOSING BALANCE",
                    balance="105.00",
                ),
            ),
        )

        result = self.parser.parse_pages((page,), statement_date=date(2026, 10, 9))

        self.assertEqual(1, len(result.transactions))
        self.assertEqual("GOOD", result.transactions[0].description)
        self.assertEqual("AMBIGUOUS_AMOUNT", result.issues[0].code)
        self.assertEqual(2, result.issues[0].source_row)

    def test_rejects_an_unsupported_pdf_layout(self) -> None:
        page = LayoutPage(page_number=1, text="Some other bank", account_number=None, lines=())

        with self.assertRaisesRegex(ValueError, "Unsupported statement layout"):
            self.parser.parse_pages((page,), statement_date=date(2026, 10, 9))

    def test_quarantines_bad_money_and_continues_with_the_next_row(self) -> None:
        page = LayoutPage(
            page_number=2,
            text="HSBC Composite Statement Details of Your Accounts SAVINGS ACCOUNT-RES",
            account_number="000-111111-222",
            lines=(
                line(
                    "01Oct2026 BALANCE BROUGHT FORWARD 100.00",
                    top=100,
                    date_text="01Oct2026",
                    description="BALANCE BROUGHT FORWARD",
                    balance="100.00",
                ),
                line(
                    "02Oct2026 BAD 1O.00 90.00",
                    top=110,
                    date_text="02Oct2026",
                    description="BAD",
                    withdrawal="1O.00",
                    balance="90.00",
                ),
                line(
                    "03Oct2026 GOOD 5.00 85.00",
                    top=120,
                    date_text="03Oct2026",
                    description="GOOD",
                    withdrawal="5.00",
                    balance="85.00",
                ),
                line(
                    "CLOSING BALANCE 85.00",
                    top=130,
                    description="CLOSING BALANCE",
                    balance="85.00",
                ),
            ),
        )

        result = self.parser.parse_pages((page,), statement_date=date(2026, 10, 9))

        self.assertEqual(("GOOD",), tuple(item.description for item in result.transactions))
        self.assertEqual("BAD_AMOUNT", result.issues[0].code)


if __name__ == "__main__":
    unittest.main()
