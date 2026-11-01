from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import Mock, patch

from statement_parser.pdf_layout import extract_layout_pages


class _Page:
    def extract_text(self, **_kwargs):
        return (
            "HSBC Composite Statement Details of Your Accounts "
            "SAVINGS ACCOUNT-RES 000-111111-222"
        )

    def extract_words(self, **_kwargs):
        return [
            {"text": "01Oct2026", "x0": 36.0, "top": 261.0},
            {"text": "COFFEE", "x0": 102.0, "top": 261.0},
            {"text": "80.00", "x0": 432.0, "top": 261.0},
            {"text": "2,044.97", "x0": 510.0, "top": 261.0},
        ]


class _Pdf:
    pages = (_Page(),)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PdfLayoutTest(unittest.TestCase):
    def test_maps_current_hsbc_column_coordinates(self) -> None:
        fake_pdfplumber = types.SimpleNamespace(open=lambda _path: _Pdf())
        with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
            pages = extract_layout_pages("synthetic.pdf")

        self.assertEqual("000-111111-222", pages[0].account_number)
        self.assertEqual("01Oct2026", pages[0].lines[0].date_text)
        self.assertEqual("COFFEE", pages[0].lines[0].description)
        self.assertEqual("80.00", pages[0].lines[0].withdrawal)
        self.assertEqual("2,044.97", pages[0].lines[0].balance)

    def test_passes_password_to_pdfplumber(self) -> None:
        open_pdf = Mock(return_value=_Pdf())
        fake_pdfplumber = types.SimpleNamespace(open=open_pdf)

        with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
            extract_layout_pages("locked.pdf", password="statement secret")

        open_pdf.assert_called_once_with("locked.pdf", password="statement secret")


if __name__ == "__main__":
    unittest.main()
