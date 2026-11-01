from __future__ import annotations

from datetime import datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_PERIOD = re.compile(
    r"(?P<start>\d{1,2}\s+[A-Za-z]{3}\s+'\d{2})\s*-\s*"
    r"(?P<end>\d{1,2}\s+[A-Za-z]{3}\s+'\d{2})"
)
_SUMMARY = re.compile(
    r"Opening balance\s+Total credits\s+Interest earned\s+Total debits\s+Closing balance"
    r".*?₹\s*(?P<opening>[\d,]+\.\d{2})\s+"
    r"₹\s*[\d,]+\.\d{2}\s+₹\s*[\d,]+\.\d{2}\s+"
    r"₹\s*[\d,]+\.\d{2}\s+₹\s*(?P<closing>[\d,]+\.\d{2})",
    re.DOTALL | re.IGNORECASE,
)
_ROW = re.compile(
    r"^\s*(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+'\d{2})\s+"
    r"(?P<description>.+?)\s+-?₹\s*(?P<amount>[\d,]+\.\d{2})\s+"
    r"₹\s*(?P<balance>[\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)


class SliceBankStatementParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _PERIOD.search(text)
        summary = _SUMMARY.search(text)
        if period is None or summary is None:
            raise ValueError("Slice savings statement period or balance summary not found")
        opening = parse_minor_units(summary.group("opening"))
        declared_closing = parse_minor_units(summary.group("closing"))
        previous_balance = opening
        transactions: tuple[StatementTransaction, ...] = ()
        for source_line, line in enumerate(text.splitlines(), start=1):
            row = _ROW.match(line)
            if row is None:
                continue
            stated_amount = parse_minor_units(row.group("amount"))
            running_balance = parse_minor_units(row.group("balance"))
            amount = running_balance - previous_balance
            if abs(amount) != stated_amount:
                raise ValueError(f"Slice row amount disagrees with balance delta at line {source_line}")
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=datetime.strptime(row.group("date"), "%d %b '%y").date(),
                    description=" ".join(row.group("description").split()),
                    amount_minor=amount,
                    currency="INR",
                    running_balance_minor=running_balance,
                    external_ref=None,
                    source_page=0,
                    source_row=source_line,
                ),
            )
            previous_balance = running_balance
        if not transactions and "No transactions found" not in text:
            raise ValueError("Slice savings statement contains no transaction rows")
        if previous_balance != declared_closing:
            raise ValueError("Slice final running balance disagrees with closing balance")
        return ParseResult(
            adapter_id="slice.savings-account.pdf.v0",
            statement_date=datetime.strptime(period.group("end"), "%d %b '%y").date(),
            account_number="",
            currency="INR",
            opening_balance_minor=opening,
            closing_balance_minor=declared_closing,
            transactions=transactions,
            issues=(),
        )
