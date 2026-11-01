from __future__ import annotations

from datetime import date, datetime
import re

from .models import ParseIssue, ParseResult, StatementTransaction
from .money import parse_minor_units


_SECTION = re.compile(
    r"Statement for account no\.\s*(?P<account>\S+).*?for the period from\s+"
    r"(?P<start>\d{2}-\d{2}-\d{4})\s+to\s+(?P<end>\d{2}-\d{2}-\d{4})",
    re.IGNORECASE,
)
_OPENING = re.compile(r"^\s*Opening Balance\s+(?P<amount>[\d,]+\.\d{2})\s*$", re.I | re.M)
_CLOSING = re.compile(r"^\s*Closing Balance\s+(?P<amount>[\d,]+\.\d{2})\s*$", re.I | re.M)
_ROW = re.compile(
    r"^\s*(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<description>.+?)\s+"
    r"(?P<withdrawal>[\d,]+\.\d{2})\s+(?P<deposit>[\d,]+\.\d{2})\s+"
    r"(?P<balance>[\d,]+\.\d{2})\s*$"
)
_QUICK_PERIOD = re.compile(
    r"Detailed Statement for a/c no\.\s*(?P<account>\S+).*?between\s+(?P<start>\d{2}-\d{2}-\d{4})"
    r"\s+to\s+(?P<end>\d{2}-\d{2}-\d{4})",
    re.IGNORECASE,
)
_QUICK_ROW = re.compile(
    r"^\s*(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<description>.+?)\s+"
    r"(?P<amounts>[\d,]+\.\d{2}(?:\s+[\d,]+\.\d{2}){1,2})\s*$"
)


def is_axis_one_glance_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "statement for account no." in lowered
        and "date transaction details chq no. withdrawal deposits balance" in lowered
    )


def is_axis_quick_view_statement(text: str) -> bool:
    return (
        "Detailed Statement for a/c no." in text
        and "Txn Date Transaction Withdrawals Deposits Balance" in text
    )


class AxisOneGlanceParser:
    def parse_text(self, text: str) -> ParseResult:
        transactions: tuple[StatementTransaction, ...] = ()
        issues: tuple[ParseIssue, ...] = ()
        statement_dates: tuple[date, ...] = ()
        section_balances: tuple[tuple[int, int], ...] = ()
        current_opening: int | None = None
        previous_balance: int | None = None
        section_transaction_start = 0
        table_active = False
        section_index = 0
        current_account_ref: str | None = None
        current_account_label: str | None = None
        seen_account_suffixes: set[str] = set()
        for source_line, line in enumerate(text.splitlines(), start=1):
            section = _SECTION.search(line)
            if section:
                section_index += 1
                suffix = _safe_account_suffix(section.group("account"))
                if suffix in seen_account_suffixes:
                    raise ValueError("Axis statement contains a duplicate account suffix")
                seen_account_suffixes.add(suffix)
                current_account_ref = f"axis-account-{suffix}"
                current_account_label = f"Axis Savings • {suffix}"
                statement_dates = (
                    *statement_dates,
                    datetime.strptime(section.group("end"), "%d-%m-%Y").date(),
                )
                current_opening = None
                previous_balance = None
                section_transaction_start = len(transactions)
                table_active = False
                continue
            if "Date Transaction details Chq no. Withdrawal Deposits Balance" in line:
                table_active = True
                continue
            opening = _OPENING.match(line) if table_active else None
            if opening:
                current_opening = parse_minor_units(opening.group("amount"))
                previous_balance = current_opening
                continue
            row = _ROW.match(line) if table_active and previous_balance is not None else None
            if row:
                withdrawal = parse_minor_units(row.group("withdrawal"))
                deposit = parse_minor_units(row.group("deposit"))
                amount = deposit - withdrawal
                running_balance = parse_minor_units(row.group("balance"))
                difference = running_balance - (previous_balance + amount)
                if abs(difference) > 1:
                    raise ValueError(f"Axis row balance does not reconcile at line {source_line}")
                if difference:
                    issues = (
                        *issues,
                        ParseIssue(
                            code="RUNNING_BALANCE_ROUNDING",
                            detail="Statement running balance differs by one paise",
                            source_page=0,
                            source_row=source_line,
                            raw_text="",
                        ),
                    )
                transactions = (
                    *transactions,
                    StatementTransaction(
                        booked_date=datetime.strptime(row.group("date"), "%d-%m-%Y").date(),
                        description=" ".join(row.group("description").split()),
                        amount_minor=amount,
                        currency="INR",
                        running_balance_minor=running_balance,
                        external_ref=None,
                        source_page=0,
                        source_row=source_line,
                        account_ref=current_account_ref,
                        account_label=current_account_label,
                    ),
                )
                previous_balance = running_balance
                continue
            closing = _CLOSING.match(line) if table_active else None
            if closing:
                declared_closing = parse_minor_units(closing.group("amount"))
                if current_opening is None or previous_balance is None:
                    raise ValueError("Axis account-section closing balance does not reconcile")
                closing_difference = declared_closing - previous_balance
                if abs(closing_difference) > 1:
                    raise ValueError("Axis account-section closing balance does not reconcile")
                if closing_difference:
                    issues = (
                        *issues,
                        ParseIssue(
                            code="CLOSING_BALANCE_ROUNDING",
                            detail="Statement closing balance differs by one paise",
                            source_page=0,
                            source_row=source_line,
                            raw_text="",
                        ),
                    )
                section_balances = (*section_balances, (current_opening, declared_closing))
                table_active = False
                continue
            if (
                table_active
                and len(transactions) > section_transaction_start
                and _is_continuation(line.strip())
            ):
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
                        account_ref=previous.account_ref,
                        account_label=previous.account_label,
                    ),
                )
        if not statement_dates or not section_balances:
            raise ValueError("Axis One Glance account sections not found")
        single_section = len(section_balances) == 1
        return ParseResult(
            adapter_id="axis.one-glance.savings.pdf.v0",
            statement_date=max(statement_dates),
            account_number="",
            currency="INR",
            opening_balance_minor=section_balances[0][0] if single_section else None,
            closing_balance_minor=section_balances[0][1] if single_section else None,
            transactions=transactions,
            issues=issues,
        )


class AxisQuickViewParser:
    def parse_text(self, text: str) -> ParseResult:
        period = _QUICK_PERIOD.search(text)
        opening_match = _OPENING.search(text)
        closing_match = _CLOSING.search(text)
        if period is None or opening_match is None or closing_match is None:
            raise ValueError("Axis Quick View statement period or balances not found")
        opening = parse_minor_units(opening_match.group("amount"))
        closing = parse_minor_units(closing_match.group("amount"))
        previous_balance = opening
        transactions: tuple[StatementTransaction, ...] = ()
        table_active = False
        suffix = _safe_account_suffix(period.group("account"))
        account_ref = f"axis-account-{suffix}"
        account_label = f"Axis Savings • {suffix}"
        for source_line, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if "Txn Date Transaction Withdrawals Deposits Balance" in line:
                table_active = True
                continue
            if _CLOSING.match(line):
                table_active = False
                continue
            row = _QUICK_ROW.match(line) if table_active else None
            if row is None:
                if transactions and table_active and _is_quick_continuation(stripped):
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
                            account_ref=previous.account_ref,
                            account_label=previous.account_label,
                        ),
                    )
                continue
            values = tuple(parse_minor_units(raw) for raw in row.group("amounts").split())
            running_balance = values[-1]
            amount = running_balance - previous_balance
            if abs(amount) not in set(values[:-1]):
                raise ValueError(f"Axis Quick View row amount disagrees with balance delta at line {source_line}")
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=datetime.strptime(row.group("date"), "%d-%m-%Y").date(),
                    description=" ".join(row.group("description").split()),
                    amount_minor=amount,
                    currency="INR",
                    running_balance_minor=running_balance,
                    external_ref=None,
                    source_page=0,
                    source_row=source_line,
                    account_ref=account_ref,
                    account_label=account_label,
                ),
            )
            previous_balance = running_balance
        if previous_balance != closing:
            raise ValueError("Axis Quick View closing balance does not reconcile")
        return ParseResult(
            adapter_id="axis.quick-view.savings.pdf.v0",
            statement_date=datetime.strptime(period.group("end"), "%d-%m-%Y").date(),
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
    return not lowered.startswith(("closing balance", "total ", "page ", "statement for account"))


def _is_quick_continuation(line: str) -> bool:
    lowered = line.lower()
    if not line or _QUICK_ROW.match(line):
        return False
    return not lowered.startswith(("opening balance", "closing balance", "legends used"))


def _safe_account_suffix(masked_account: str) -> str:
    match = re.search(r"(\d{4})$", masked_account)
    if match is None:
        raise ValueError("Axis statement account suffix not found")
    return match.group(1)
