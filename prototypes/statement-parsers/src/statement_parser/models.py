from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


DateValue = date | str


def _iso(value: DateValue) -> str:
    return value.isoformat() if isinstance(value, date) else value


@dataclass(frozen=True)
class StatementTransaction:
    booked_date: DateValue
    description: str
    amount_minor: int
    currency: str
    running_balance_minor: int | None
    external_ref: str | None
    source_page: int
    source_row: int
    account_ref: str | None = None
    account_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bookedDate": _iso(self.booked_date),
            "description": self.description,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "runningBalanceMinor": self.running_balance_minor,
            "externalRef": self.external_ref,
            "sourcePage": self.source_page,
            "sourceRow": self.source_row,
            "accountRef": self.account_ref,
            "accountLabel": self.account_label,
        }


@dataclass(frozen=True)
class ParseIssue:
    code: str
    detail: str
    source_page: int
    source_row: int
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "sourcePage": self.source_page,
            "sourceRow": self.source_row,
            "rawText": self.raw_text,
        }


@dataclass(frozen=True)
class ParseResult:
    adapter_id: str
    statement_date: DateValue
    account_number: str
    currency: str
    opening_balance_minor: int | None
    closing_balance_minor: int | None
    transactions: tuple[StatementTransaction, ...]
    issues: tuple[ParseIssue, ...]
    account_ref: str | None = None

    @property
    def is_balance_reconciled(self) -> bool | None:
        if self.opening_balance_minor is None or self.closing_balance_minor is None:
            return None
        expected = self.opening_balance_minor + sum(
            transaction.amount_minor for transaction in self.transactions
        )
        return expected == self.closing_balance_minor

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapterId": self.adapter_id,
            "statementDate": _iso(self.statement_date),
            "accountNumber": self.account_number,
            "accountRef": self.account_ref,
            "currency": self.currency,
            "openingBalanceMinor": self.opening_balance_minor,
            "closingBalanceMinor": self.closing_balance_minor,
            "isBalanceReconciled": self.is_balance_reconciled,
            "transactions": [transaction.to_dict() for transaction in self.transactions],
            "issues": [issue.to_dict() for issue in self.issues],
        }
