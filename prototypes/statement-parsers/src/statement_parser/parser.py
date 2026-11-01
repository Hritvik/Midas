from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .account_identity import assign_account_refs, validate_account_scope_id
from .hsbc_india import HsbcIndiaCompositeParser
from .axis_bank import (
    AxisOneGlanceParser,
    AxisQuickViewParser,
    is_axis_one_glance_statement,
    is_axis_quick_view_statement,
)
from .hdfc_bank import HdfcBankStatementParser, is_hdfc_bank_statement
from .card_adapters import (
    is_hsbc_credit_statement,
    is_idfc_credit_statement,
    is_icici_credit_statement,
    is_slice_credit_statement,
    is_yes_bank_credit_statement,
    parse_extracted_text,
)
from .models import ParseResult
from .kotak_bank import KotakSavingsParser, is_kotak_savings_statement
from .pdf_layout import extract_layout_pages
from .slice_bank import SliceBankStatementParser
from .yes_bank import YesBankSavingsParser, is_yes_bank_savings_statement


MAX_PDF_BYTES = 25 * 1024 * 1024


def parse_pdf(
    pdf_path: str | Path,
    *,
    password: str | None = None,
    account_scope_id: str | None = None,
) -> ParseResult:
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"Statement PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError("Statement must be a PDF file")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError("Statement exceeds the 25 MiB prototype limit")
    if account_scope_id is not None:
        validate_account_scope_id(account_scope_id)
    pages = extract_layout_pages(str(path), password=password)
    combined = "\n".join(page.text for page in pages)
    result = _parse_pages(pages, combined)
    return assign_account_refs(result, account_scope_id)


def _parse_pages(pages, combined: str) -> ParseResult:
    if "slice small finance bank" in combined.lower() and "Opening balance" in combined:
        return SliceBankStatementParser().parse_text(combined)
    if is_axis_one_glance_statement(combined):
        return AxisOneGlanceParser().parse_text(combined)
    if is_axis_quick_view_statement(combined):
        return AxisQuickViewParser().parse_text(combined)
    if is_hdfc_bank_statement(combined):
        return HdfcBankStatementParser().parse_text(combined)
    if is_kotak_savings_statement(combined):
        return KotakSavingsParser().parse_text(combined)
    if is_yes_bank_savings_statement(combined):
        return YesBankSavingsParser().parse_text(combined)
    if "Axis Bank Mastercard Credit Card Statement" in combined or (
        "SBI CARD" in combined.upper() and "MONTHLY STATEMENT" in combined.upper()
    ) or is_slice_credit_statement(combined) or is_icici_credit_statement(
        combined
    ) or is_yes_bank_credit_statement(combined) or is_idfc_credit_statement(
        combined
    ) or is_hsbc_credit_statement(combined):
        return parse_extracted_text(combined)
    match = re.search(r"Statement Date\s+(\d{2}[A-Za-z]{3}\d{4})", combined)
    if match is None:
        raise ValueError("Unsupported statement layout: statement date not found")
    statement_date = datetime.strptime(match.group(1).title(), "%d%b%Y").date()
    return HsbcIndiaCompositeParser().parse_pages(pages, statement_date=statement_date)
