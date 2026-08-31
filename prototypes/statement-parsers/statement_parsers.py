"""PROTOTYPE parsers for two supplied credit-card statement layouts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import re


class ParseError(ValueError):
    """The document is unsupported or its statement metadata is malformed."""


@dataclass(frozen=True)
class StatementEntry:
    row_number: int
    booked_on: date
    amount_minor: int
    currency: str
    description: str
    raw_polarity: str


@dataclass(frozen=True)
class ParsedStatement:
    adapter_id: str
    period_start: date
    period_end: date
    entries: tuple[StatementEntry, ...]


_AXIS_ROW = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>Dr|Cr)\s*$",
    re.IGNORECASE,
)
_SBI_ROW = re.compile(
    r"^\s*(?P<date>\d{2}\s+[A-Za-z]{3}\s+\d{2})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>[CD])\s*$",
    re.IGNORECASE,
)
_SBI_CONTINUATION = re.compile(
    r"^\s+(?P<description>\S.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s+(?P<polarity>[CD])\s*$",
    re.IGNORECASE,
)


def parse_extracted_text(text: str) -> ParsedStatement:
    """Detect one prototype layout and canonicalize its transaction rows."""
    if "Axis Bank Mastercard Credit Card Statement" in text:
        return _parse_axis(text)
    if "SBI CARD" in text.upper() and "MONTHLY STATEMENT" in text.upper():
        return _parse_sbi(text)
    raise ParseError("unsupported statement layout")


def _parse_axis(text: str) -> ParsedStatement:
    period = re.search(
        r"(?P<start>\d{2}/\d{2}/\d{4})\s*-\s*(?P<end>\d{2}/\d{2}/\d{4})",
        text,
    )
    if period is None:
        raise ParseError("Axis statement period not found")

    entries: list[StatementEntry] = []
    table_active = False
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "DATE" in line and "TRANSACTION DETAILS" in line and "AMOUNT" in line:
            table_active = True
            continue
        if "End of Statement" in line:
            break
        if not table_active:
            continue
        match = _AXIS_ROW.match(line)
        if match:
            entries.append(_entry_from_match(match, source_line, "%d/%m/%Y"))

    return ParsedStatement(
        adapter_id="axis.credit-card.pdf.v0",
        period_start=_date(period.group("start"), "%d/%m/%Y"),
        period_end=_date(period.group("end"), "%d/%m/%Y"),
        entries=tuple(entries),
    )


def _parse_sbi(text: str) -> ParsedStatement:
    period = re.search(
        r"Statement Period:\s*(?P<start>\d{2}\s+[A-Za-z]{3}\s+\d{2})"
        r"\s+to\s+(?P<end>\d{2}\s+[A-Za-z]{3}\s+\d{2})",
        text,
        re.IGNORECASE,
    )
    if period is None:
        raise ParseError("SBI statement period not found")

    entries: list[StatementEntry] = []
    table_active = False
    header_pending = False
    last_date: date | None = None
    for source_line, line in enumerate(text.splitlines(), start=1):
        if "Transaction Details" in line and "Amount" in line:
            table_active = True
            header_pending = False
            continue
        if "Transaction Details" in line:
            header_pending = True
            continue
        if header_pending and "Date" in line and "Amount" in line:
            table_active = True
            header_pending = False
            continue
        if "Important Messages" in line:
            table_active = False
            header_pending = False
            continue
        if not table_active or "Statement Period:" in line:
            continue

        dated_match = _SBI_ROW.match(line)
        if dated_match:
            entry = _entry_from_match(dated_match, source_line, "%d %b %y")
            entries.append(entry)
            last_date = entry.booked_on
            continue

        continuation = _SBI_CONTINUATION.match(line)
        if continuation and last_date is not None:
            entries.append(
                _entry(
                    source_line=source_line,
                    booked_on=last_date,
                    description=continuation.group("description"),
                    amount=continuation.group("amount"),
                    polarity=continuation.group("polarity"),
                )
            )

    return ParsedStatement(
        adapter_id="sbi.credit-card.pdf.v0",
        period_start=_date(period.group("start"), "%d %b %y"),
        period_end=_date(period.group("end"), "%d %b %y"),
        entries=tuple(entries),
    )


def _entry_from_match(match: re.Match[str], source_line: int, date_format: str) -> StatementEntry:
    return _entry(
        source_line=source_line,
        booked_on=_date(match.group("date"), date_format),
        description=match.group("description"),
        amount=match.group("amount"),
        polarity=match.group("polarity"),
    )


def _entry(
    *, source_line: int, booked_on: date, description: str, amount: str, polarity: str
) -> StatementEntry:
    normalized_polarity = polarity.upper()
    amount_minor = _minor_units(amount)
    owner_inflow = normalized_polarity in {"CR", "C"}
    return StatementEntry(
        row_number=source_line,
        booked_on=booked_on,
        amount_minor=amount_minor if owner_inflow else -amount_minor,
        currency="INR",
        description=" ".join(description.split()),
        raw_polarity=normalized_polarity,
    )


def _minor_units(raw: str) -> int:
    try:
        amount = Decimal(raw.replace(",", ""))
    except InvalidOperation as error:
        raise ParseError(f"invalid amount: {raw}") from error
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _date(raw: str, date_format: str) -> date:
    try:
        return datetime.strptime(raw, date_format).date()
    except ValueError as error:
        raise ParseError(f"invalid date: {raw}") from error
