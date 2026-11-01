"""Aggregate, privacy-safe owned-flow analysis over parsed statement outputs."""

from __future__ import annotations

from datetime import date
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from .flow_matching import FlowTransaction, deduplicate_transactions, match_owned_flows


_ASSET_ADAPTERS = frozenset(
    {
        "axis.one-glance.savings.pdf.v0",
        "axis.quick-view.savings.pdf.v0",
        "hdfc.savings-account.pdf.v0",
        "bank-statement.hsbc.india.composite.v1",
        "kotak.savings-account.pdf.v0",
        "slice.savings-account.pdf.v0",
        "yes.savings-account.pdf.v0",
    }
)
_LIABILITY_ADAPTERS = frozenset(
    {
        "axis.credit-card.pdf.v0",
        "hsbc.credit-card.pdf.v0",
        "icici.credit-card.pdf.v0",
        "idfc-first.credit-card.pdf.v0",
        "sbi.credit-card.pdf.v0",
        "slice.credit-card.pdf.v0",
        "yes-bank.credit-card.pdf.v0",
    }
)
_OPAQUE_ACCOUNT_REF = re.compile(r"^acct_[0-9a-f]{24}$")
MAX_PARSED_JSON_BYTES = 10 * 1024 * 1024
MAX_STATEMENTS = 5_000
MAX_TRANSACTIONS = 100_000
MAX_CORPUS_BYTES = 512 * 1024 * 1024


def analyze_owned_flows(parsed_root: Path) -> dict[str, object]:
    source_transactions: list[FlowTransaction] = []
    statement_count = 0
    for output, payload in iter_validated_statements(parsed_root):
        account_kind = _account_kind(payload["adapterId"])
        statement_count += 1
        for index, transaction in enumerate(payload["transactions"]):
            _validate_transaction(transaction)
            source_transactions.append(
                FlowTransaction(
                    transaction_id=_source_transaction_id(output.stem, transaction, index),
                    account_ref=_required_account_ref(transaction),
                    account_kind=account_kind,
                    booked_date=date.fromisoformat(transaction["bookedDate"]),
                    amount_minor=transaction["amountMinor"],
                    currency=transaction["currency"],
                    description=transaction["description"],
                    external_ref=transaction.get("externalRef"),
                    source_id=output.stem,
                    source_row=transaction["sourceRow"],
                )
            )

    canonical = deduplicate_transactions(source_transactions)
    matches = match_owned_flows(canonical)
    accepted_internal = sum(
        item.match_type == "INTERNAL_TRANSFER" for item in matches.accepted
    )
    accepted_card = sum(item.match_type == "CARD_PAYMENT" for item in matches.accepted)
    reviews_by_reason = Counter(item.reason for item in matches.review_items)
    dates = [item.booked_date for item in canonical]
    return {
        "statementCount": statement_count,
        "sourceTransactionCount": len(source_transactions),
        "canonicalTransactionCount": len(canonical),
        "duplicateOccurrenceCount": len(source_transactions) - len(canonical),
        "acceptedInternalTransferCount": accepted_internal,
        "acceptedCardPaymentCount": accepted_card,
        "acceptedTransactionCount": 2 * len(matches.accepted),
        "reviewItemCount": len(matches.review_items),
        "reviewItemsByReason": dict(sorted(reviews_by_reason.items())),
        "unmatchedTransactionCount": len(matches.unmatched_transaction_ids),
        "transactionDateRange": {
            "from": min(dates).isoformat() if dates else None,
            "to": max(dates).isoformat() if dates else None,
        },
        "invariantViolationCount": matches.invariant_violation_count,
    }


def iter_validated_statements(parsed_root: Path):
    """Yield a corpus only after enforcing shared resource and identity boundaries."""
    corpus_bytes = 0
    transaction_count = 0
    source_ids: set[str] = set()
    for statement_count, output in enumerate(
        sorted(parsed_root.glob("*/*.json")), start=1
    ):
        if statement_count > MAX_STATEMENTS:
            raise ValueError("Parsed corpus exceeds the statement count limit")
        if output.is_symlink() or not output.is_file():
            raise ValueError("Parsed corpus contains a non-regular statement output")
        corpus_bytes += output.stat().st_size
        if corpus_bytes > MAX_CORPUS_BYTES:
            raise ValueError("Parsed corpus exceeds the corpus byte limit")
        if output.stem in source_ids:
            raise ValueError(f"Duplicate source ID in parsed corpus: {output.stem}")
        source_ids.add(output.stem)
        payload = _read_statement(output)
        transaction_count += len(payload["transactions"])
        if transaction_count > MAX_TRANSACTIONS:
            raise ValueError("Parsed corpus exceeds the transaction count limit")
        yield output, payload


def _account_kind(adapter_id: str) -> str:
    if adapter_id in _ASSET_ADAPTERS:
        return "ASSET"
    if adapter_id in _LIABILITY_ADAPTERS:
        return "LIABILITY"
    raise ValueError(f"Unknown adapter for owned-flow analysis: {adapter_id}")


def _source_transaction_id(source_id: str, transaction: dict, index: int) -> str:
    material = "\0".join(
        (
            source_id,
            str(transaction["sourcePage"]),
            str(transaction["sourceRow"]),
            str(index),
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _required_account_ref(transaction: dict) -> str:
    account_ref = transaction.get("accountRef")
    if not isinstance(account_ref, str) or _OPAQUE_ACCOUNT_REF.fullmatch(account_ref) is None:
        raise ValueError("Transaction is missing an opaque account reference")
    return account_ref


def _read_statement(path: Path) -> dict:
    if path.stat().st_size > MAX_PARSED_JSON_BYTES:
        raise ValueError(f"Parsed statement exceeds the size limit: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid parsed statement output: {path.name}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("transactions"), list):
        raise ValueError(f"Invalid parsed statement output: {path.name}")
    return payload


def _validate_transaction(transaction: object) -> None:
    if not isinstance(transaction, dict):
        raise ValueError("Invalid transaction in parsed statement")
    try:
        date.fromisoformat(transaction["bookedDate"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid transaction date in parsed statement") from error
    amount = transaction.get("amountMinor")
    if isinstance(amount, bool) or not isinstance(amount, int) or abs(amount) > 10**15:
        raise ValueError("Invalid transaction amount in parsed statement")
    currency = transaction.get("currency")
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("Invalid transaction currency in parsed statement")
    description = transaction.get("description")
    if not isinstance(description, str) or len(description) > 4_096:
        raise ValueError("Invalid transaction description in parsed statement")
    external_ref = transaction.get("externalRef")
    if external_ref is not None and (
        not isinstance(external_ref, str) or len(external_ref) > 512
    ):
        raise ValueError("Invalid external reference in parsed statement")
    for field in ("sourcePage", "sourceRow"):
        value = transaction.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid source position in parsed statement")
