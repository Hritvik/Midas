from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import re

from .models import ParseIssue, ParseResult, StatementTransaction
from .money import parse_minor_units
from .pdf_layout import LayoutLine, LayoutPage


ADAPTER_ID = "bank-statement.hsbc.india.composite.v1"
_DATE_PATTERN = re.compile(r"^\d{2}[A-Za-z]{3}\d{4}$")
_REFERENCE_PATTERN = re.compile(r"^(?:UPI\d{17}|[A-Z]{2,}\d{8,})$")


@dataclass(frozen=True)
class _PendingRow:
    booked_date: date
    description_parts: tuple[str, ...]
    deposit: str
    withdrawal: str
    balance: str
    page_number: int
    source_row: int
    raw_lines: tuple[str, ...]

    @property
    def has_amount(self) -> bool:
        return bool(self.deposit or self.withdrawal)


class HsbcIndiaCompositeParser:
    def supports(self, pages: tuple[LayoutPage, ...]) -> bool:
        combined = "\n".join(page.text for page in pages)
        return all(
            marker in combined
            for marker in ("HSBC", "Composite Statement", "Details of Your Accounts")
        )

    def parse_pages(
        self, pages: tuple[LayoutPage, ...], *, statement_date: date
    ) -> ParseResult:
        if not self.supports(pages):
            raise ValueError("Unsupported statement layout: expected HSBC India composite statement")

        account_page = next(
            (page for page in pages if page.account_number and page.lines), None
        )
        if account_page is None:
            raise ValueError("Unsupported statement layout: savings-account table not found")

        transactions: tuple[StatementTransaction, ...] = ()
        issues: tuple[ParseIssue, ...] = ()
        pending: _PendingRow | None = None
        current_date: date | None = None
        opening_balance: int | None = None
        closing_balance: int | None = None
        source_row = 0

        for layout_line in account_page.lines:
            normalized_description = " ".join(layout_line.description.split())
            parsed_date = self._parse_date(layout_line.date_text)

            if normalized_description.startswith("BALANCE BROUGHT FORWARD"):
                opening_balance = self._optional_money(layout_line.balance)
                current_date = parsed_date
                source_row += 1
                continue

            if normalized_description.startswith("CLOSING BALANCE"):
                transactions, issues = self._finalize(pending, transactions, issues)
                pending = None
                closing_balance = self._optional_money(layout_line.balance)
                break

            starts_reference_row = bool(
                normalized_description
                and _REFERENCE_PATTERN.fullmatch(normalized_description.split()[0])
            )
            starts_new = parsed_date is not None or (
                starts_reference_row and pending is not None and pending.has_amount
            )

            if starts_new:
                transactions, issues = self._finalize(pending, transactions, issues)
                current_date = parsed_date or current_date
                source_row += 1
                if current_date is None:
                    issues = (
                        *issues,
                        ParseIssue(
                            code="MISSING_DATE",
                            detail="Transaction appeared before any parseable date",
                            source_page=account_page.page_number,
                            source_row=source_row,
                            raw_text=layout_line.text,
                        ),
                    )
                    pending = None
                    continue
                pending = _PendingRow(
                    booked_date=current_date,
                    description_parts=(normalized_description,) if normalized_description else (),
                    deposit=layout_line.deposit,
                    withdrawal=layout_line.withdrawal,
                    balance=layout_line.balance,
                    page_number=account_page.page_number,
                    source_row=source_row,
                    raw_lines=(layout_line.text,),
                )
                continue

            if pending is not None:
                pending = replace(
                    pending,
                    description_parts=(
                        *pending.description_parts,
                        *((normalized_description,) if normalized_description else ()),
                    ),
                    deposit=layout_line.deposit or pending.deposit,
                    withdrawal=layout_line.withdrawal or pending.withdrawal,
                    balance=layout_line.balance or pending.balance,
                    raw_lines=(*pending.raw_lines, layout_line.text),
                )

        transactions, issues = self._finalize(pending, transactions, issues)
        return ParseResult(
            adapter_id=ADAPTER_ID,
            statement_date=statement_date,
            account_number="",
            currency="INR",
            opening_balance_minor=opening_balance,
            closing_balance_minor=closing_balance,
            transactions=transactions,
            issues=issues,
        )

    @staticmethod
    def _parse_date(raw_date: str) -> date | None:
        value = raw_date.strip()
        if not _DATE_PATTERN.fullmatch(value):
            return None
        return datetime.strptime(value.title(), "%d%b%Y").date()

    @staticmethod
    def _optional_money(raw_amount: str) -> int | None:
        return parse_minor_units(raw_amount) if raw_amount else None

    def _finalize(
        self,
        pending: _PendingRow | None,
        transactions: tuple[StatementTransaction, ...],
        issues: tuple[ParseIssue, ...],
    ) -> tuple[tuple[StatementTransaction, ...], tuple[ParseIssue, ...]]:
        if pending is None:
            return transactions, issues
        if bool(pending.deposit) == bool(pending.withdrawal):
            code = "AMBIGUOUS_AMOUNT" if pending.deposit else "MISSING_AMOUNT"
            detail = (
                "Row has both deposit and withdrawal amounts"
                if pending.deposit
                else "Row has neither a deposit nor a withdrawal amount"
            )
            return transactions, (
                *issues,
                ParseIssue(
                    code=code,
                    detail=detail,
                    source_page=pending.page_number,
                    source_row=pending.source_row,
                    raw_text=" | ".join(pending.raw_lines),
                ),
            )

        try:
            amount_minor = parse_minor_units(pending.deposit or pending.withdrawal)
            running_balance_minor = self._optional_money(pending.balance)
        except ValueError as error:
            return transactions, (
                *issues,
                ParseIssue(
                    code="BAD_AMOUNT",
                    detail=str(error),
                    source_page=pending.page_number,
                    source_row=pending.source_row,
                    raw_text=" | ".join(pending.raw_lines),
                ),
            )
        if pending.withdrawal:
            amount_minor = -amount_minor
        description = " ".join(pending.description_parts)
        first_token = description.split()[0] if description else ""
        external_ref = first_token if _REFERENCE_PATTERN.fullmatch(first_token) else None
        transaction = StatementTransaction(
            booked_date=pending.booked_date,
            description=description,
            amount_minor=amount_minor,
            currency="INR",
            running_balance_minor=running_balance_minor,
            external_ref=external_ref,
            source_page=pending.page_number,
            source_row=pending.source_row,
        )
        return (*transactions, transaction), issues
