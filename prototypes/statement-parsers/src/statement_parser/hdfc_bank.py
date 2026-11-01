from __future__ import annotations

from datetime import datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_PERIOD = re.compile(
    r"Statement\s+From\s*:\s*(?P<start>\d{2}/\d{2}/\d{2,4})\s+"
    r"To\s+(?P<end>\d{2}/\d{2}/\d{2,4})",
    re.IGNORECASE,
)
_OPENING = re.compile(r"Opening\s+Balance\s*:\s*(?P<amount>[\d,]+\.\d{2})", re.IGNORECASE)
_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{2,4})\s+(?P<description>.+?)\s+"
    r"(?P<amounts>[\d,]+\.\d{2}(?:\s+[\d,]+\.\d{2}){1,2})\s*$"
)
_ROW_WITHOUT_DESCRIPTION = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{2,4})\s+"
    r"(?P<amounts>[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2})\s*$"
)
_SUMMARY_VALUES = re.compile(
    r"^\s*[\d,]+\.\d{2}(?:\s+[\d,]+\.\d{2}){3}\s*$"
)
_SUMMARY = re.compile(
    r"Opening\s+Balance\s+Debit\s+Amount\s+Credit\s+Amount\s+Closing\s+Balance\s+"
    r"(?P<opening>[\d,]+\.\d{2})\s+(?P<debits>[\d,]+\.\d{2})\s+"
    r"(?P<credits>[\d,]+\.\d{2})\s+(?P<closing>[\d,]+\.\d{2})",
    re.IGNORECASE,
)


class HdfcBankStatementParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _PERIOD.search(text)
        opening_match = _OPENING.search(text)
        if period is None or opening_match is None:
            raise ValueError("HDFC statement period or opening balance not found")
        opening = parse_minor_units(opening_match.group("amount"))
        previous_balance = opening
        transactions: tuple[StatementTransaction, ...] = ()
        for source_line, line in enumerate(text.splitlines(), start=1):
            missing_description = _ROW_WITHOUT_DESCRIPTION.match(line)
            row = missing_description or _ROW.match(line)
            if row is None:
                if transactions and _is_continuation(line.strip()):
                    previous = transactions[-1]
                    transactions = (
                        *transactions[:-1],
                        StatementTransaction(
                            booked_date=previous.booked_date,
                            description=f"{previous.description} {' '.join(line.split())}",
                            amount_minor=previous.amount_minor,
                            currency=previous.currency,
                            running_balance_minor=previous.running_balance_minor,
                            external_ref=previous.external_ref,
                            source_page=previous.source_page,
                            source_row=previous.source_row,
                        ),
                    )
                continue
            values = tuple(parse_minor_units(value) for value in row.group("amounts").split())
            running_balance = values[-1]
            amount = running_balance - previous_balance
            if abs(amount) not in set(values[:-1]):
                raise ValueError(f"HDFC row amount disagrees with balance delta at line {source_line}")
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=_date(row.group("date")),
                    description=(
                        "Unlabelled transaction"
                        if missing_description
                        else " ".join(row.group("description").split())
                    ),
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
            raise ValueError("HDFC statement contains no transaction rows")
        summary = _SUMMARY.search(text)
        closing = previous_balance
        if summary is not None:
            declared_opening = parse_minor_units(summary.group("opening"))
            declared_debits = parse_minor_units(summary.group("debits"))
            declared_credits = parse_minor_units(summary.group("credits"))
            declared_closing = parse_minor_units(summary.group("closing"))
            parsed_debits = -sum(min(transaction.amount_minor, 0) for transaction in transactions)
            parsed_credits = sum(max(transaction.amount_minor, 0) for transaction in transactions)
            if (
                declared_opening != opening
                or declared_debits != parsed_debits
                or declared_credits != parsed_credits
                or declared_closing != previous_balance
                or declared_opening - declared_debits + declared_credits != declared_closing
            ):
                raise ValueError("HDFC statement summary does not reconcile")
            closing = declared_closing
        return ParseResult(
            adapter_id="hdfc.savings-account.pdf.v0",
            statement_date=_date(period.group("end")),
            account_number="",
            currency="INR",
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            transactions=transactions,
            issues=(),
        )


def is_hdfc_bank_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "hdfc bank" in lowered
        and "txn date narration withdrawals deposits closing balance" in lowered
    )


HdfcSavingsParser = HdfcBankStatementParser
is_hdfc_savings_statement = is_hdfc_bank_statement


def _is_continuation(line: str) -> bool:
    lowered = line.lower()
    if not line or "generated by hdfc bank" in lowered:
        return False
    if (
        _ROW.match(line)
        or _ROW_WITHOUT_DESCRIPTION.match(line)
        or _SUMMARY_VALUES.match(line)
        or "txn date narration" in lowered
    ):
        return False
    if lowered.startswith(("summary", "opening balance", "debit count", "total withdrawal")):
        return False
    if "end of statement" in lowered:
        return False
    return True


def _date(raw: str):
    date_format = "%d/%m/%Y" if len(raw.rsplit("/", 1)[-1]) == 4 else "%d/%m/%y"
    try:
        return datetime.strptime(raw, date_format).date()
    except ValueError as error:
        raise ValueError(f"invalid date: {raw}") from error
