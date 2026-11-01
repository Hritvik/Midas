"""Privacy-safe corpus readiness analysis for the Midas value-slice prototype."""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path
from typing import Iterable


def analyze_corpus(
    parsed_root: Path,
    *,
    source_roots: Iterable[Path],
    output_directories: Iterable[str] | None = None,
) -> dict[str, object]:
    """Return aggregate readiness metrics without transaction descriptions or amounts."""
    allowed_source_ids = {
        path.stem
        for source_root in source_roots
        for path in source_root.rglob("*.pdf")
        if path.is_file()
    }
    selected_directories = set(output_directories) if output_directories is not None else None
    statements: dict[str, dict] = {}
    excluded_output_count = 0
    for output in sorted(parsed_root.glob("*/*.json")):
        if output.name == "summary.json":
            continue
        if selected_directories is not None and output.parent.name not in selected_directories:
            continue
        if output.stem not in allowed_source_ids:
            excluded_output_count += 1
            continue
        payload = _read_statement(output)
        existing = statements.get(output.stem)
        if existing is not None and existing != payload:
            raise ValueError(f"Conflicting parsed outputs for source {output.stem}")
        statements[output.stem] = payload

    adapters: Counter[str] = Counter()
    balance_checks: Counter[str] = Counter()
    transaction_dates: list[date] = []
    transaction_count = 0
    missing_account_ref = 0
    issue_count = 0
    for statement in statements.values():
        adapters[statement["adapterId"]] += 1
        reconciled = statement.get("isBalanceReconciled")
        balance_checks[
            "unknown" if reconciled is None else "reconciled" if reconciled else "unreconciled"
        ] += 1
        issue_count += len(statement.get("issues", ()))
        for transaction in statement["transactions"]:
            transaction_count += 1
            transaction_dates.append(date.fromisoformat(transaction["bookedDate"]))
            if not transaction.get("accountRef"):
                missing_account_ref += 1

    return {
        "statementCount": len(statements),
        "transactionCount": transaction_count,
        "transactionDateRange": {
            "from": min(transaction_dates).isoformat() if transaction_dates else None,
            "to": max(transaction_dates).isoformat() if transaction_dates else None,
        },
        "statementsByAdapter": dict(sorted(adapters.items())),
        "balanceChecks": {
            key: balance_checks[key]
            for key in ("reconciled", "unknown", "unreconciled")
        },
        "transactionsMissingAccountRef": missing_account_ref,
        "issueCount": issue_count,
        "excludedOutputCount": excluded_output_count,
    }


def _read_statement(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid parsed statement output: {path}") from error
    required = ("adapterId", "statementDate", "transactions")
    if not isinstance(payload, dict) or any(key not in payload for key in required):
        raise ValueError(f"Invalid parsed statement output: {path}")
    if not isinstance(payload["transactions"], list):
        raise ValueError(f"Invalid parsed statement output: {path}")
    return payload
