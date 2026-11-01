from __future__ import annotations

from datetime import datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_PERIOD = re.compile(
    r"Statement period\s*:\s*(?P<start>[A-Za-z]+\s+\d{1,2},\s+\d{4})"
    r"\s+to\s+(?P<end>[A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+\d+\s+"
    r"(?P<description>.+?)\s+-?\d+\s+(?:(?:Y|N|YES|NO)\s+)?"
    r"(?P<amount>[\d,]+\.\d{2})(?:\s+(?P<polarity>CR|DR))?\s*$",
    re.IGNORECASE,
)


def is_icici_credit_card_statement(text: str) -> bool:
    lowered = text.lower()
    return "icici bank credit card" in lowered and "statement period" in lowered


class IciciCreditCardParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _PERIOD.search(text)
        if period is None:
            raise ValueError("ICICI credit-card statement period not found")
        transactions: tuple[StatementTransaction, ...] = ()
        for source_line, line in enumerate(text.splitlines(), start=1):
            row = _ROW.match(line)
            if row is None:
                continue
            amount = parse_minor_units(row.group("amount"))
            polarity = (row.group("polarity") or "DR").upper()
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=datetime.strptime(row.group("date"), "%d/%m/%Y").date(),
                    description=" ".join(row.group("description").split()),
                    amount_minor=amount if polarity == "CR" else -amount,
                    currency="INR",
                    running_balance_minor=None,
                    external_ref=None,
                    source_page=0,
                    source_row=source_line,
                ),
            )
        if not transactions:
            raise ValueError("ICICI credit-card statement contains no transaction rows")
        return ParseResult(
            adapter_id="icici.credit-card.pdf.v0",
            statement_date=datetime.strptime(period.group("end"), "%B %d, %Y").date(),
            account_number="",
            currency="INR",
            opening_balance_minor=None,
            closing_balance_minor=None,
            transactions=transactions,
            issues=(),
        )
