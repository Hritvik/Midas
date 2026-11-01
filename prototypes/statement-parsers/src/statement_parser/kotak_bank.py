from __future__ import annotations

from datetime import datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_PERIOD = re.compile(
    r"Account Statement\s+(?P<start>\d{2}\s+[A-Za-z]{3}\s+\d{4})\s*-\s*"
    r"(?P<end>\d{2}\s+[A-Za-z]{3}\s+\d{4})",
    re.IGNORECASE,
)
_OPENING = re.compile(r"Opening Balance\s+-\s+-\s+-\s+(?P<amount>[\d,]+\.\d{2})")
_SUMMARY = re.compile(
    r"Savings Account \(SA\):\s*(?P<opening>[\d,]+\.\d{2})\s+"
    r"(?P<closing>[\d,]+\.\d{2})",
    re.IGNORECASE,
)
_ROW = re.compile(
    r"^\s*\d+\s+(?P<date>\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"
    r"(?P<description>.+?)\s+(?P<amounts>[\d,]+\.\d{2}(?:\s+[\d,]+\.\d{2}){1,2})\s*$"
)


def is_kotak_savings_statement(text: str) -> bool:
    lowered = text.lower()
    return "kotak mahindra bank" in lowered and "savings account transactions" in lowered


class KotakSavingsParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _PERIOD.search(text)
        opening_match = _OPENING.search(text)
        summary = _SUMMARY.search(text)
        if period is None or opening_match is None or summary is None:
            raise ValueError("Kotak statement period or balance summary not found")
        opening = parse_minor_units(opening_match.group("amount"))
        summary_opening = parse_minor_units(summary.group("opening"))
        closing = parse_minor_units(summary.group("closing"))
        if opening != summary_opening:
            raise ValueError("Kotak opening balances disagree")
        previous_balance = opening
        transactions: tuple[StatementTransaction, ...] = ()
        table_active = False
        for source_line, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if "# Date Description" in line:
                table_active = True
                continue
            if table_active and stripped.startswith("Account Statement"):
                table_active = False
            row = _ROW.match(line) if table_active else None
            if row is None:
                if transactions and table_active and _is_continuation(stripped):
                    previous = transactions[-1]
                    transactions = (
                        *transactions[:-1],
                        StatementTransaction(
                            booked_date=previous.booked_date,
                            description=f"{previous.description} {' '.join(stripped.split())}",
                            amount_minor=previous.amount_minor,
                            currency=previous.currency,
                            running_balance_minor=previous.running_balance_minor,
                            external_ref=previous.external_ref,
                            source_page=previous.source_page,
                            source_row=previous.source_row,
                        ),
                    )
                continue
            values = tuple(parse_minor_units(raw) for raw in row.group("amounts").split())
            running_balance = values[-1]
            amount = running_balance - previous_balance
            if abs(amount) not in set(values[:-1]):
                raise ValueError(f"Kotak row amount disagrees with balance delta at line {source_line}")
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=datetime.strptime(row.group("date"), "%d %b %Y").date(),
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
        if not transactions:
            raise ValueError("Kotak statement contains no transaction rows")
        if previous_balance != closing:
            raise ValueError("Kotak final running balance disagrees with closing balance")
        return ParseResult(
            adapter_id="kotak.savings-account.pdf.v0",
            statement_date=datetime.strptime(period.group("end"), "%d %b %Y").date(),
            account_number="",
            currency="INR",
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            transactions=transactions,
            issues=(),
        )


def _is_continuation(line: str) -> bool:
    lowered = line.lower()
    if not line or _ROW.match(line):
        return False
    return not lowered.startswith(("- - opening balance", "statement generated", "account summary"))
