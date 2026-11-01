from __future__ import annotations

from datetime import datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_PERIOD = re.compile(
    r"For The Period Of\s+(?P<start>\d{2}-[A-Za-z]{3}-\d{4})\s+to\s+"
    r"(?P<end>\d{2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)
_SUMMARY = re.compile(
    r"Opening Balance\s*:\s*(?P<opening>[\d,]+\.\d{2})\s+"
    r"Total Withdrawals\s*:\s*(?P<withdrawals>[\d,]+\.\d{2})\s+"
    r"Total Deposits\s*:\s*(?P<deposits>[\d,]+\.\d{2})\s+"
    r"Closing Balance\s*:\s*(?P<closing>[\d,]+\.\d{2})",
    re.IGNORECASE,
)
_ROW = re.compile(
    r"^\s*(?P<transaction_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<value_date>\d{2}/\d{2}/\d{4})\s+(?P<description>.*?)\s*"
    r"(?P<withdrawal>[\d,]+\.\d{2})\s+(?P<deposit>[\d,]+\.\d{2})\s+"
    r"(?P<balance>[\d,]+\.\d{2})\s*$"
)


def is_yes_bank_savings_statement(text: str) -> bool:
    return (
        "YOUR CONSOLIDATED STATEMENT" in text
        and "Statement Of Transactions In Saving Account" in text
    )


class YesBankSavingsParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _PERIOD.search(text)
        summary = _SUMMARY.search(text)
        if period is None or summary is None:
            raise ValueError("YES Bank statement period or balance summary not found")
        opening = parse_minor_units(summary.group("opening"))
        closing = parse_minor_units(summary.group("closing"))
        declared_withdrawals = parse_minor_units(summary.group("withdrawals"))
        declared_deposits = parse_minor_units(summary.group("deposits"))
        if opening - declared_withdrawals + declared_deposits != closing:
            raise ValueError("YES Bank summary closing balance does not reconcile")
        transactions: tuple[StatementTransaction, ...] = ()
        previous_content = ""
        previous_balance = opening
        for source_line, line in enumerate(text.splitlines(), start=1):
            row = _ROW.match(line)
            if row is None:
                stripped = line.strip()
                if stripped:
                    previous_content = stripped
                continue
            withdrawal = parse_minor_units(row.group("withdrawal"))
            deposit = parse_minor_units(row.group("deposit"))
            amount = deposit - withdrawal
            running_balance = parse_minor_units(row.group("balance"))
            if previous_balance + amount != running_balance:
                raise ValueError(f"YES Bank row balance does not reconcile at line {source_line}")
            description = row.group("description").strip() or previous_content
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=datetime.strptime(
                        row.group("transaction_date"), "%d/%m/%Y"
                    ).date(),
                    description=" ".join(description.split()),
                    amount_minor=amount,
                    currency="INR",
                    running_balance_minor=running_balance,
                    external_ref=None,
                    source_page=0,
                    source_row=source_line,
                ),
            )
            previous_balance = running_balance
            previous_content = ""
        if not transactions and (declared_withdrawals != 0 or declared_deposits != 0):
            raise ValueError("YES Bank statement contains no transaction rows")
        if previous_balance != closing:
            raise ValueError("YES Bank final running balance disagrees with closing balance")
        return ParseResult(
            adapter_id="yes.savings-account.pdf.v0",
            statement_date=datetime.strptime(period.group("end"), "%d-%b-%Y").date(),
            account_number="",
            currency="INR",
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            transactions=transactions,
            issues=(),
        )
