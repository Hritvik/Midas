"""Opaque, deterministic account identities for parsed statement transactions."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from uuid import UUID

from .models import ParseResult


_REFERENCE_VERSION = "midas-account-ref-v1"


def assign_account_refs(result: ParseResult, account_scope: str | None) -> ParseResult:
    """Return a copy with opaque account refs derived from a non-secret local alias.

    The scope is deliberately constrained to a random UUID stored in a private local
    mapping. Raw account numbers, credential aliases and passwords must never be supplied.
    Existing per-section refs are included so composite statements retain distinct
    account identities.
    """
    if account_scope is None:
        return replace(result, account_number="")
    validate_account_scope_id(account_scope)

    statement_ref = _opaque_reference(
        account_scope=account_scope,
        subaccount_ref=None,
    )
    transactions = tuple(
        replace(
            transaction,
            account_ref=_opaque_reference(
                account_scope=account_scope,
                subaccount_ref=transaction.account_ref,
            ),
        )
        for transaction in result.transactions
    )
    return replace(
        result,
        account_number="",
        account_ref=statement_ref,
        transactions=transactions,
    )


def validate_account_scope_id(account_scope_id: str) -> None:
    """Accept only canonical random UUIDs, never human-chosen account data or secrets."""
    try:
        parsed = UUID(account_scope_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Invalid account scope ID: expected a random UUID") from error
    if parsed.version != 4 or str(parsed) != account_scope_id:
        raise ValueError("Invalid account scope ID: expected a canonical random UUID")


def _opaque_reference(
    *,
    account_scope: str,
    subaccount_ref: str | None,
) -> str:
    material = "\0".join(
        (_REFERENCE_VERSION, account_scope, subaccount_ref or "primary")
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"acct_{digest}"
