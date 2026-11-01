from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


_MONEY_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d?(?:,\d{2})*,\d{3})\.\d{2}$"
)


def parse_minor_units(raw_amount: str) -> int:
    """Parse a two-decimal rupee amount into paise without floating-point math."""
    value = raw_amount.strip()
    if not _MONEY_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid monetary amount: {raw_amount!r}")
    try:
        return int(Decimal(value.replace(",", "")) * 100)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid monetary amount: {raw_amount!r}") from error
