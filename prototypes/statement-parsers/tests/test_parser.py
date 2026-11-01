from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from statement_parser.models import ParseResult, StatementTransaction
from statement_parser.parser import parse_pdf
from statement_parser.pdf_layout import LayoutPage


SCOPE_ID = "8d50bd46-35f4-4aed-88fd-0f0627ac48c3"


def _parse_result(adapter_id: str = "test-adapter") -> ParseResult:
    return ParseResult(
        adapter_id=adapter_id,
        statement_date="2026-09-30",
        account_number="raw-account-number",
        currency="INR",
        opening_balance_minor=None,
        closing_balance_minor=None,
        transactions=(),
        issues=(),
    )


class ParserTest(unittest.TestCase):
    def test_assigns_account_identity_after_parsing_without_exposing_the_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "axis.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            card_result = ParseResult(
                adapter_id="axis-credit-card-v1",
                statement_date="2026-09-30",
                account_number="123456789",
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
                    ),
                ),
                issues=(),
            )
            with (
                patch(
                    "statement_parser.parser.extract_layout_pages",
                    return_value=(
                        LayoutPage(1, "Axis Bank Mastercard Credit Card Statement", None, ()),
                    ),
                ),
                patch(
                    "statement_parser.parser.parse_extracted_text",
                    return_value=card_result,
                ),
            ):
                result = parse_pdf(pdf_path, account_scope_id=SCOPE_ID)

        self.assertEqual("", result.account_number)
        self.assertRegex(result.transactions[0].account_ref or "", r"^acct_[0-9a-f]{24}$")
        self.assertNotIn(SCOPE_ID, result.transactions[0].account_ref or "")

    def test_rejects_invalid_account_scope_before_reading_the_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "axis.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            with patch("statement_parser.parser.extract_layout_pages") as extract:
                with self.assertRaisesRegex(ValueError, "account scope"):
                    parse_pdf(pdf_path, account_scope_id="5361")

            extract.assert_not_called()

    def test_rejects_files_over_the_prototype_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "oversized.pdf"
            with pdf_path.open("wb") as stream:
                stream.seek(25 * 1024 * 1024)
                stream.write(b"x")

            with self.assertRaisesRegex(ValueError, "25 MiB"):
                parse_pdf(pdf_path)

    def test_forwards_pdf_password_to_layout_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "locked.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            parse_result = _parse_result()
            parser = Mock()
            parser.parse_pages.return_value = parse_result

            with (
                patch(
                    "statement_parser.parser.extract_layout_pages",
                    return_value=(
                        LayoutPage(
                            page_number=1,
                            text="Statement Date 09OCT2026 HSBC Composite Statement",
                            account_number=None,
                            lines=(),
                        ),
                    ),
                ) as extract_layout_pages,
                patch("statement_parser.parser.HsbcIndiaCompositeParser", return_value=parser),
            ):
                result = parse_pdf(pdf_path, password="statement secret")

            self.assertEqual("", result.account_number)
            extract_layout_pages.assert_called_once_with(
                str(pdf_path),
                password="statement secret",
            )

    def test_routes_axis_text_to_the_card_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "axis.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            card_result = _parse_result("axis.credit-card.pdf.v0")
            with (
                patch(
                    "statement_parser.parser.extract_layout_pages",
                    return_value=(
                        LayoutPage(
                            page_number=1,
                            text="Axis Bank Mastercard Credit Card Statement",
                            account_number=None,
                            lines=(),
                        ),
                    ),
                ),
                patch(
                    "statement_parser.parser.parse_extracted_text",
                    return_value=card_result,
                ) as parse_text,
            ):
                result = parse_pdf(pdf_path)

        self.assertEqual("", result.account_number)
        parse_text.assert_called_once_with("Axis Bank Mastercard Credit Card Statement")

    def test_routes_icici_text_to_the_continuation_aware_card_adapter(self) -> None:
        text = (
            "ICICI Bank Credit Card Statement\n"
            "Statement period : September 1, 2026 to September 30, 2026\n"
            "Date SerNo. Transaction Details Reward Amount"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "icici.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            card_result = _parse_result("icici.credit-card.pdf.v0")
            with (
                patch(
                    "statement_parser.parser.extract_layout_pages",
                    return_value=(LayoutPage(1, text, None, ()),),
                ),
                patch(
                    "statement_parser.parser.parse_extracted_text",
                    return_value=card_result,
                ) as parse_text,
            ):
                result = parse_pdf(pdf_path)

        self.assertEqual("", result.account_number)
        parse_text.assert_called_once_with(text)


if __name__ == "__main__":
    unittest.main()
