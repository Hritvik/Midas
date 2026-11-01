from __future__ import annotations

from datetime import date, datetime
import re

from .models import ParseResult, StatementTransaction
from .money import parse_minor_units


_AXIS_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>Dr|Cr)\s*$",
    re.IGNORECASE,
)
_SBI_ROW = re.compile(
    r"^\s*(?P<date>\d{2}\s+[A-Za-z]{3}\s+\d{2})\s+(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>[CD])\s*$",
    re.IGNORECASE,
)
_SBI_CONTINUATION = re.compile(
    r"^\s+(?P<description>\S.+?)\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<polarity>[CD])\s*$",
    re.IGNORECASE,
)
_ICICI_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<serial>\d+)\s+"
    r"(?P<description>.+?)\s+(?P<reward>-?\d+)\s+"
    r"(?:(?:Y|N|YES|NO)\s+)?"
    r"(?P<amount>[\d,]+\.\d{2})(?:\s+(?P<polarity>CR|DR))?\s*$",
    re.IGNORECASE,
)
_ICICI_PERIOD = re.compile(
    r"Statement\s+period\s*:\s*"
    r"(?P<start_month>[A-Za-z]+)\s+(?P<start_day>\d{1,2}),\s+(?P<start_year>\d{4})"
    r"\s+to\s+"
    r"(?P<end_month>[A-Za-z]+)\s+(?P<end_day>\d{1,2}),\s+(?P<end_year>\d{4})",
    re.IGNORECASE,
)
_YES_PERIOD = re.compile(
    r"(?P<start>\d{2}/\d{2}/\d{4})\s+To\s+(?P<end>\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
_YES_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<description>.+?)\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<polarity>Dr|Cr)\s*$",
    re.IGNORECASE,
)
_IDFC_CREDIT_PERIOD = re.compile(
    r"(?P<start>\d{1,2}/[A-Za-z]{3}/\d{4})\s*-\s*"
    r"(?P<end>\d{1,2}/[A-Za-z]{3}/\d{4})",
    re.IGNORECASE,
)
_IDFC_CREDIT_ROW = re.compile(
    r"^\s*(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+\d{2})\s+"
    r"(?P<description>.+?)\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<polarity>DR|CR)\s*$",
    re.IGNORECASE,
)
_IDFC_CREDIT_SPLIT_AMOUNT = re.compile(
    r"^\s*(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+\d{2})\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>DR|CR)\s*$",
    re.IGNORECASE,
)
_HSBC_CREDIT_PERIOD = re.compile(
    r"(?P<start>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+To\s+"
    r"(?P<end>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
    re.IGNORECASE,
)
_HSBC_CREDIT_ROW = re.compile(
    r"^\s*(?P<date>\d{2}[A-Za-z]{3})\s+(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})(?:\s+(?P<polarity>CR))?\s*$",
    re.IGNORECASE,
)
_SLICE_PERIOD = re.compile(
    r"^\s*(?P<start_day>\d{1,2})\s+(?P<start_month>[A-Za-z]{3})\s*-\s*"
    r"(?P<end_day>\d{1,2})\s+(?P<end_month>[A-Za-z]{3})\s*$",
    re.IGNORECASE,
)
_SLICE_ITEM = re.compile(
    r"^\s*(?P<description>.+?)\s+₹\s*(?P<amount>[\d,]+(?:\.\d{1,2})?)\s*$"
)
_SLICE_SPLIT_ITEM = re.compile(
    r"^\s*(?P<description>.+?)\s+(?P<amount>[\d,]+(?:\.\d{1,2})?)\s*$"
)
_SLICE_DATE = re.compile(
    r"^\s*(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+'\d{2})(?:\s*•.*)?\s*$",
    re.IGNORECASE,
)
_SLICE_SECTIONS = {
    "Spends": -1,
    "Bank transfers": -1,
    "Refunds & repayments": 1,
    "Cashback": 1,
    "Interest": -1,
    "Surcharge": -1,
    "Flat fee": -1,
    "Late fee": -1,
    "Foreclosure charge": -1,
    "Ongoing EMIs": -1,
    "Carry forward": -1,
}


def parse_extracted_text(text: str) -> ParseResult:
    if "Axis Bank Mastercard Credit Card Statement" in text:
        return _parse_axis(text)
    if "SBI CARD" in text.upper() and "MONTHLY STATEMENT" in text.upper():
        return _parse_sbi(text)
    if is_slice_credit_statement(text):
        return _parse_slice(text)
    if is_icici_credit_statement(text):
        return _parse_icici(text)
    if is_yes_bank_credit_statement(text):
        return _parse_yes_bank(text)
    if is_idfc_credit_statement(text):
        return _parse_idfc_credit(text)
    if is_hsbc_credit_statement(text):
        return _parse_hsbc_credit(text)
    raise ValueError("unsupported statement layout")


def is_slice_credit_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "credit card statement" in lowered
        and "statement summary" in lowered
        and re.search(r"slice\s+(?:upi\s+)?credit\s+card\s+mitc", lowered) is not None
    )


def is_icici_credit_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "icici bank" in lowered
        and "credit card statement" in lowered
        and "date serno. transaction details" in lowered
    )


def is_yes_bank_credit_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "yes bank" in lowered
        and "credit card statement" in lowered
        and "date transaction details" in lowered
    )


def is_idfc_credit_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "idfc first bank" in lowered
        and "credit card statement" in lowered
        and "transaction transaction details" in lowered
    )


def is_hsbc_credit_statement(text: str) -> bool:
    lowered = text.lower()
    return (
        "hsbc" in lowered
        and "credit card" in lowered
        and "date transaction details amounts" in lowered
    )


def _parse_axis(text: str) -> ParseResult:
    period = re.search(
        r"(?P<start>\d{2}/\d{2}/\d{4})\s*-\s*(?P<end>\d{2}/\d{2}/\d{4})",
        text,
    )
    if period is None:
        raise ValueError("Axis statement period not found")
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "DATE" in line and "TRANSACTION DETAILS" in line and "AMOUNT" in line:
            table_active = True
            continue
        if "End of Statement" in line:
            break
        match = _AXIS_ROW.match(line) if table_active else None
        if match:
            transactions = (*transactions, _transaction(match, source_line, "%d/%m/%Y"))
    return _result("axis.credit-card.pdf.v0", _date(period.group("end"), "%d/%m/%Y"), transactions)


def _parse_sbi(text: str) -> ParseResult:
    period = re.search(
        r"Statement Period:\s*(?P<start>\d{2}\s+[A-Za-z]{3}\s+\d{2})"
        r"\s+to\s+(?P<end>\d{2}\s+[A-Za-z]{3}\s+\d{2})",
        text,
        re.IGNORECASE,
    )
    if period is None:
        raise ValueError("SBI statement period not found")
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    header_pending = False
    last_date: date | None = None
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "Transaction Details" in line and "Amount" in line:
            table_active, header_pending = True, False
            continue
        if "Transaction Details" in line:
            header_pending = True
            continue
        if header_pending and "Date" in line and "Amount" in line:
            table_active, header_pending = True, False
            continue
        if "Important Messages" in line:
            table_active, header_pending = False, False
            continue
        if not table_active or "Statement Period:" in line:
            continue
        dated = _SBI_ROW.match(line)
        if dated:
            transaction = _transaction(dated, source_line, "%d %b %y")
            transactions = (*transactions, transaction)
            last_date = transaction.booked_date
            continue
        continuation = _SBI_CONTINUATION.match(line)
        if continuation and last_date is not None:
            transactions = (
                *transactions,
                _transaction(continuation, source_line, None, booked_date=last_date),
            )
    return _result("sbi.credit-card.pdf.v0", _date(period.group("end"), "%d %b %y"), transactions)


def _parse_slice(text: str) -> ParseResult:
    lines = text.splitlines()
    period = next((_SLICE_PERIOD.match(line) for line in lines if _SLICE_PERIOD.match(line)), None)
    dated_lines = tuple(
        match for line in lines if (match := _SLICE_DATE.match(line)) is not None
    )
    if period is None:
        raise ValueError("Slice statement period not found")
    if not dated_lines:
        raise ValueError("Slice credit-card statement contains no transaction rows")
    first_transaction_date = _date(dated_lines[0].group("date"), "%d %b '%y")
    start_month = datetime.strptime(period.group("start_month"), "%b").month
    end_month = datetime.strptime(period.group("end_month"), "%b").month
    year = first_transaction_date.year
    if end_month < start_month and first_transaction_date.month >= start_month:
        year += 1
    statement_date = _date(
        f"{period.group('end_day')} {period.group('end_month')} {year}", "%d %b %Y"
    )
    transactions: tuple[StatementTransaction, ...] = ()
    polarity: int | None = None
    pending: tuple[re.Match[str], int] | None = None
    for source_line, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped in _SLICE_SECTIONS:
            polarity = _SLICE_SECTIONS[stripped]
            pending = None
            continue
        item = _SLICE_ITEM.match(line) if polarity is not None else None
        if item is None and polarity is not None:
            item = _SLICE_SPLIT_ITEM.match(line)
        if item:
            pending = (item, source_line)
            continue
        dated = _SLICE_DATE.match(line)
        if dated is None or pending is None or polarity is None:
            continue
        item, item_line = pending
        transactions = (
            *transactions,
            StatementTransaction(
                booked_date=_date(dated.group("date"), "%d %b '%y"),
                description=" ".join(item.group("description").split()),
                amount_minor=polarity * _slice_minor_units(item.group("amount")),
                currency="INR",
                running_balance_minor=None,
                external_ref=None,
                source_page=0,
                source_row=item_line,
            ),
        )
        pending = None
    if not transactions:
        raise ValueError("Slice credit-card statement contains no transaction rows")
    return _result("slice.credit-card.pdf.v0", statement_date, transactions)


def _parse_icici(text: str) -> ParseResult:
    period = _ICICI_PERIOD.search(text)
    if period is None:
        raise ValueError("ICICI statement period not found")
    statement_date = _date(
        f"{period.group('end_day')} {period.group('end_month')} {period.group('end_year')}",
        "%d %B %Y",
    )
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "Date SerNo. Transaction Details" in line:
            table_active = True
            continue
        if not table_active:
            continue
        if "IMPORTANT MESSAGES" in line:
            table_active = False
            continue
        row = _ICICI_ROW.match(line)
        if row:
            amount_minor = parse_minor_units(row.group("amount"))
            amount_minor = amount_minor if row.group("polarity") == "CR" else -amount_minor
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=_date(row.group("date"), "%d/%m/%Y"),
                    description=" ".join(row.group("description").split()),
                    amount_minor=amount_minor,
                    currency="INR",
                    running_balance_minor=None,
                    external_ref=row.group("serial"),
                    source_page=0,
                    source_row=source_line,
                ),
            )
            continue
        continuation = line.strip()
        if transactions and _is_icici_continuation(continuation):
            previous = transactions[-1]
            transactions = (
                *transactions[:-1],
                StatementTransaction(
                    booked_date=previous.booked_date,
                    description=f"{previous.description} {continuation}",
                    amount_minor=previous.amount_minor,
                    currency=previous.currency,
                    running_balance_minor=previous.running_balance_minor,
                    external_ref=previous.external_ref,
                    source_page=previous.source_page,
                    source_row=previous.source_row,
                ),
            )
    if not transactions:
        raise ValueError("ICICI credit-card statement contains no transaction rows")
    return _result("icici.credit-card.pdf.v0", statement_date, transactions)


def _parse_yes_bank(text: str) -> ParseResult:
    period = _YES_PERIOD.search(text)
    if period is None:
        raise ValueError("YES Bank statement period not found")
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "Date Transaction Details" in line and "Amount" in line:
            table_active = True
            continue
        if not table_active:
            continue
        row = _YES_ROW.match(line)
        if row is None:
            continue
        amount_minor = parse_minor_units(row.group("amount"))
        if row.group("polarity").upper() == "DR":
            amount_minor = -amount_minor
        transactions = (
            *transactions,
            StatementTransaction(
                booked_date=_date(row.group("date"), "%d/%m/%Y"),
                description=" ".join(row.group("description").split()),
                amount_minor=amount_minor,
                currency="INR",
                running_balance_minor=None,
                external_ref=None,
                source_page=0,
                source_row=source_line,
            ),
        )
    if not transactions:
        raise ValueError("YES Bank credit-card statement contains no transaction rows")
    return _result(
        "yes-bank.credit-card.pdf.v0",
        _date(period.group("end"), "%d/%m/%Y"),
        transactions,
    )


def _parse_idfc_credit(text: str) -> ParseResult:
    period = _IDFC_CREDIT_PERIOD.search(text)
    if period is None:
        raise ValueError("IDFC FIRST statement period not found")
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    pending_description: str | None = None
    pending_split_index: int | None = None
    for source_line, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if "Transaction Transaction Details" in line:
            table_active = True
            pending_description = None
            pending_split_index = None
            continue
        if not table_active:
            continue
        if "Share your credit limit" in line:
            pending_description = None
            pending_split_index = None
            continue
        row = _IDFC_CREDIT_ROW.match(line)
        if row:
            transactions = (
                *transactions,
                _transaction(row, source_line, "%d %b %y"),
            )
            pending_description = None
            pending_split_index = None
            continue
        split = _IDFC_CREDIT_SPLIT_AMOUNT.match(line)
        if split and pending_description:
            amount_minor = parse_minor_units(split.group("amount"))
            if split.group("polarity").upper() == "DR":
                amount_minor = -amount_minor
            transactions = (
                *transactions,
                StatementTransaction(
                    booked_date=_date(split.group("date"), "%d %b %y"),
                    description=" ".join(pending_description.split()),
                    amount_minor=amount_minor,
                    currency="INR",
                    running_balance_minor=None,
                    external_ref=None,
                    source_page=0,
                    source_row=source_line,
                ),
            )
            pending_split_index = len(transactions) - 1
            continue
        if pending_split_index is not None and _is_idfc_credit_continuation(stripped):
            previous = transactions[pending_split_index]
            transactions = (
                *transactions[:pending_split_index],
                StatementTransaction(
                    booked_date=previous.booked_date,
                    description=f"{previous.description} {stripped}",
                    amount_minor=previous.amount_minor,
                    currency=previous.currency,
                    running_balance_minor=previous.running_balance_minor,
                    external_ref=previous.external_ref,
                    source_page=previous.source_page,
                    source_row=previous.source_row,
                ),
                *transactions[pending_split_index + 1 :],
            )
            pending_split_index = None
            pending_description = None
            continue
        if _is_idfc_credit_pending_description(stripped):
            pending_description = stripped
            pending_split_index = None
    if not transactions:
        raise ValueError("IDFC FIRST credit-card statement contains no transaction rows")
    return _result(
        "idfc-first.credit-card.pdf.v0",
        _date(period.group("end"), "%d/%b/%Y"),
        transactions,
    )


def _parse_hsbc_credit(text: str) -> ParseResult:
    period = _HSBC_CREDIT_PERIOD.search(text)
    if period is None:
        raise ValueError("HSBC credit-card statement period not found")
    statement_date = _date(period.group("end"), "%d %b %Y")
    start_date = _date(period.group("start"), "%d %b %Y")
    transactions: tuple[StatementTransaction, ...] = ()
    table_active = False
    for source_line, line in enumerate(text.splitlines(), start=1):
        normalized = " ".join(line.replace("(", "").replace(")", "").split())
        if "DATE TRANSACTION DETAILS AMOUNTS" in normalized.upper():
            table_active = True
            continue
        if not table_active:
            continue
        row = _HSBC_CREDIT_ROW.match(normalized)
        if row is None:
            continue
        transaction_date = _hsbc_credit_date(
            row.group("date"),
            start_year=start_date.year,
            statement_year=statement_date.year,
            start_month=start_date.month,
        )
        amount_minor = parse_minor_units(row.group("amount"))
        if not row.group("polarity"):
            amount_minor = -amount_minor
        transactions = (
            *transactions,
            StatementTransaction(
                booked_date=transaction_date,
                description=" ".join(row.group("description").split()),
                amount_minor=amount_minor,
                currency="INR",
                running_balance_minor=None,
                external_ref=None,
                source_page=0,
                source_row=source_line,
            ),
        )
    if not transactions:
        raise ValueError("HSBC credit-card statement contains no transaction rows")
    return _result("hsbc.credit-card.pdf.v0", statement_date, transactions)


def _hsbc_credit_date(
    raw: str,
    *,
    start_year: int,
    statement_year: int,
    start_month: int,
):
    month = datetime.strptime(raw[-3:].title(), "%b").month
    year = start_year if month >= start_month else statement_year
    return _date(f"{raw[:2]} {raw[-3:]} {year}", "%d %b %Y")


def _is_idfc_credit_pending_description(line: str) -> bool:
    if not line or len(line) > 80:
        return False
    if re.search(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{2}|[\d,]+\.\d{2}", line):
        return False
    ignored = {
        "DATE ELIGIBILITY TRANSACTIONS (IN INR)",
        "PURCHASES, EMIS & OTHER DEBITS",
        "PAYMENTS & OTHER CREDITS",
    }
    return line.upper() not in ignored and not line.startswith("Card Number:")


def _is_idfc_credit_continuation(line: str) -> bool:
    return bool(line) and len(line) <= 80 and not re.search(r"[\d,]+\.\d{2}", line)


def _is_icici_continuation(line: str) -> bool:
    if not line or len(line) > 40:
        return False
    if re.search(r"\d{2}/\d{2}/\d{4}|[\d,]+\.\d{2}", line):
        return False
    ignored = {"POINTS AMOUNT", "IMPORTANT MESSAGES", "# INTERNATIONAL SPENDS"}
    return line.upper() not in ignored


def _slice_minor_units(raw: str) -> int:
    return parse_minor_units(raw if "." in raw else f"{raw}.00")


def _transaction(match, source_line: int, date_format: str | None, *, booked_date=None):
    polarity = match.group("polarity").upper()
    amount_minor = parse_minor_units(match.group("amount"))
    return StatementTransaction(
        booked_date=booked_date or _date(match.group("date"), date_format),
        description=" ".join(match.group("description").split()),
        amount_minor=amount_minor if polarity in {"CR", "C"} else -amount_minor,
        currency="INR",
        running_balance_minor=None,
        external_ref=None,
        source_page=0,
        source_row=source_line,
    )


def _result(adapter_id, statement_date, transactions):
    return ParseResult(
        adapter_id=adapter_id,
        statement_date=statement_date,
        account_number="",
        currency="INR",
        opening_balance_minor=None,
        closing_balance_minor=None,
        transactions=transactions,
        issues=(),
    )


def _date(raw: str, date_format: str | None) -> date:
    try:
        return datetime.strptime(raw, date_format).date()
    except ValueError as error:
        raise ValueError(f"invalid date: {raw}") from error
