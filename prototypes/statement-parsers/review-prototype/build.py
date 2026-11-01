"""PROTOTYPE ONLY: build a private, standalone flow-review UI.

Question: which of three review structures makes transfer/card-payment decisions fastest
without hiding uncertainty? Variants are switchable with ``?variant=A|B|C``.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from statement_parser.corpus_flow_analysis import (  # noqa: E402
    _account_kind,
    _required_account_ref,
    _source_transaction_id,
    _validate_transaction,
    iter_validated_statements,
)
from statement_parser.flow_matching import (  # noqa: E402
    FlowTransaction,
    _Candidate,
    _has_hard_contradiction,
    _transaction_references,
    deduplicate_transactions,
    match_owned_flows,
)


_ACCOUNT_PRODUCT_NAMES = {
    "axis.one-glance.savings.pdf.v0": "Axis Savings · One Glance",
    "axis.quick-view.savings.pdf.v0": "Axis Savings · Quick View",
    "axis.credit-card.pdf.v0": "Axis Credit Card",
    "hdfc.savings-account.pdf.v0": "HDFC Savings",
    "bank-statement.hsbc.india.composite.v1": "HSBC Bank Account",
    "hsbc.credit-card.pdf.v0": "HSBC Credit Card",
    "icici.credit-card.pdf.v0": "ICICI Credit Card",
    "idfc-first.credit-card.pdf.v0": "IDFC FIRST Credit Card",
    "kotak.savings-account.pdf.v0": "Kotak Savings",
    "sbi.credit-card.pdf.v0": "SBI Credit Card",
    "slice.credit-card.pdf.v0": "Slice Credit Card",
    "slice.savings-account.pdf.v0": "Slice Savings",
    "yes-bank.credit-card.pdf.v0": "YES BANK Credit Card",
    "yes.savings-account.pdf.v0": "YES BANK Savings",
}


def _load_review_data(parsed_root: Path) -> dict[str, object]:
    source_transactions: list[FlowTransaction] = []
    label_by_account: dict[str, str] = {}
    for output, payload in iter_validated_statements(parsed_root):
        adapter_id = payload["adapterId"]
        account_kind = _account_kind(adapter_id)
        product_name = _ACCOUNT_PRODUCT_NAMES[adapter_id]
        for index, transaction in enumerate(payload["transactions"]):
            _validate_transaction(transaction)
            account_ref = _required_account_ref(transaction)
            display_label = transaction.get("accountLabel") or product_name
            existing_label = label_by_account.setdefault(account_ref, display_label)
            if existing_label != display_label:
                raise ValueError("One account identity maps to multiple product labels")
            source_transactions.append(
                FlowTransaction(
                    transaction_id=_source_transaction_id(output.stem, transaction, index),
                    account_ref=account_ref,
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
    result = match_owned_flows(canonical)
    by_id = {item.transaction_id: item for item in canonical}
    transaction_ids_by_reference: dict[str, set[str]] = {}
    for transaction in canonical:
        for reference in _transaction_references(transaction.description):
            transaction_ids_by_reference.setdefault(reference, set()).add(
                transaction.transaction_id
            )
    reason_by_id = {item.transaction_id: item.reason for item in result.review_items}
    pairs: set[tuple[str, str]] = set()
    for item in result.review_items:
        for candidate_id in item.candidate_transaction_ids:
            pairs.add(tuple(sorted((item.transaction_id, candidate_id))))

    refs_by_product: dict[str, list[str]] = {}
    for account_ref, product_name in label_by_account.items():
        refs_by_product.setdefault(product_name, []).append(account_ref)
    account_names: dict[str, str] = {}
    for product_name, account_refs in sorted(refs_by_product.items()):
        ordered_refs = sorted(account_refs)
        for index, account_ref in enumerate(ordered_refs, start=1):
            suffix = f" #{index}" if len(ordered_refs) > 1 else ""
            account_names[account_ref] = f"{product_name}{suffix}"
    candidates = []
    for first_id, second_id in sorted(pairs):
        first = by_id[first_id]
        second = by_id[second_id]
        debit, credit = (
            (first, second) if first.amount_minor < 0 else (second, first)
        )
        candidate_type = (
            "CARD_PAYMENT" if credit.account_kind == "LIABILITY" else "INTERNAL_TRANSFER"
        )
        reasons = {reason_by_id.get(first_id), reason_by_id.get(second_id)}
        shared_references = _transaction_references(
            debit.description
        ) & _transaction_references(credit.description)
        reference_correlated = (
            reasons == {"REFERENCE_CORRELATED_CANDIDATE"}
            and not _has_hard_contradiction(
                _Candidate(debit=debit, credit=credit, match_type=candidate_type)
            )
            and any(
                transaction_ids_by_reference[reference] == {first_id, second_id}
                for reference in shared_references
            )
        )
        reason = (
            "AMBIGUOUS_MULTIPLE_CANDIDATES"
            if "AMBIGUOUS_MULTIPLE_CANDIDATES" in reasons
            else "REFERENCE_CORRELATED_CANDIDATE"
            if reference_correlated
            else "INSUFFICIENT_SEMANTIC_EVIDENCE"
        )
        candidate_id = hashlib.sha256(
            f"{first_id}\0{second_id}".encode("utf-8")
        ).hexdigest()[:16]
        candidates.append(
            {
                "id": candidate_id,
                "debitId": debit.transaction_id,
                "creditId": credit.transaction_id,
                "type": candidate_type,
                "reason": reason,
                "amountMinor": abs(debit.amount_minor),
                "currency": debit.currency,
                "dayGap": abs((debit.booked_date - credit.booked_date).days),
                "debit": {
                    "account": account_names[debit.account_ref],
                    "date": debit.booked_date.isoformat(),
                    "description": debit.description,
                },
                "credit": {
                    "account": account_names[credit.account_ref],
                    "date": credit.booked_date.isoformat(),
                    "description": credit.description,
                },
            }
        )
    candidates.sort(
        key=lambda item: (
            {
                "AMBIGUOUS_MULTIPLE_CANDIDATES": 0,
                "REFERENCE_CORRELATED_CANDIDATE": 1,
                "INSUFFICIENT_SEMANTIC_EVIDENCE": 2,
            }[item["reason"]],
            item["debit"]["date"],
            item["id"],
        )
    )
    candidates_by_id = {item["id"]: item for item in candidates}
    candidate_ids_by_leg: dict[str, set[str]] = {}
    for item in candidates:
        for leg_id in (item["debitId"], item["creditId"]):
            candidate_ids_by_leg.setdefault(leg_id, set()).add(item["id"])
    groups = []
    unvisited = set(candidates_by_id)
    while unvisited:
        seed = min(unvisited)
        component: set[str] = set()
        frontier = [seed]
        while frontier:
            candidate_id = frontier.pop()
            if candidate_id in component:
                continue
            component.add(candidate_id)
            candidate = candidates_by_id[candidate_id]
            for leg_id in (candidate["debitId"], candidate["creditId"]):
                frontier.extend(candidate_ids_by_leg[leg_id] - component)
        unvisited -= component
        options = [candidates_by_id[candidate_id] for candidate_id in sorted(component)]
        options.sort(
            key=lambda option: (
                option["reason"] != "REFERENCE_CORRELATED_CANDIDATE",
                option["dayGap"],
                option["id"],
            )
        )
        recommended_options = [
            option
            for option in options
            if option["reason"] == "REFERENCE_CORRELATED_CANDIDATE"
        ]
        leg_sets = [{option["debitId"], option["creditId"]} for option in options]
        shared_ids = set.intersection(*leg_sets) if leg_sets else set()
        shared_leg_id = min(shared_ids) if shared_ids else None
        if len(options) > 1 and shared_leg_id is None:
            raise ValueError(
                "Review prototype cannot represent a non-star candidate component"
            )
        shared_leg = None
        if shared_leg_id is not None:
            sample = options[0]
            side = "debit" if sample["debitId"] == shared_leg_id else "credit"
            shared_leg = {"id": shared_leg_id, **sample[side]}
        group_id = hashlib.sha256(
            "\0".join(sorted(component)).encode("utf-8")
        ).hexdigest()[:16]
        groups.append(
            {
                "id": group_id,
                "ambiguous": len(recommended_options) > 1
                or (not recommended_options and len(options) > 1),
                "recommendedOptionId": (
                    recommended_options[0]["id"]
                    if len(recommended_options) == 1
                    else None
                ),
                "sharedLeg": shared_leg,
                "options": options,
            }
        )
    groups.sort(key=lambda group: (not group["ambiguous"], group["id"]))
    return {
        "candidates": candidates,
        "groups": groups,
        "accounts": len(account_names),
        "transactionCount": len(canonical),
    }


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        stream.write(content)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build private review UI prototype")
    parser.add_argument("parsed_root", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    data = json.dumps(_load_review_data(arguments.parsed_root), separators=(",", ":"))
    safe_data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    template = (Path(__file__).parent / "index.template.html").read_text(encoding="utf-8")
    rendered = template.replace("__REVIEW_DATA__", safe_data)
    _write_private(arguments.output, rendered)
    print(json.dumps({"output": str(arguments.output), "mode": "0600"}))


if __name__ == "__main__":
    main()
