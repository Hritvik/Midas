from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from statement_parser.financial_baseline import analyze_corpus


def _statement(
    *,
    adapter: str,
    statement_date: str,
    transactions: list[dict],
    reconciled: bool | None,
    issues: list[dict] | None = None,
) -> dict:
    return {
        "adapterId": adapter,
        "statementDate": statement_date,
        "accountNumber": "",
        "currency": "INR",
        "openingBalanceMinor": None,
        "closingBalanceMinor": None,
        "isBalanceReconciled": reconciled,
        "transactions": transactions,
        "issues": issues or [],
    }


def _transaction(date: str, amount: int, *, account_ref: str | None = None) -> dict:
    return {
        "bookedDate": date,
        "description": "SYNTHETIC",
        "amountMinor": amount,
        "currency": "INR",
        "runningBalanceMinor": None,
        "externalRef": None,
        "sourcePage": 0,
        "sourceRow": 1,
        "accountRef": account_ref,
    }


class FinancialBaselineTest(unittest.TestCase):
    def test_reports_coverage_without_exposing_transaction_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed = root / "parsed"
            source = root / "corpus"
            (parsed / "bank").mkdir(parents=True)
            (parsed / "card").mkdir(parents=True)
            source.mkdir()
            (source / "a.pdf").write_bytes(b"a")
            (source / "b.pdf").write_bytes(b"b")
            (parsed / "bank" / "a.json").write_text(
                json.dumps(
                    _statement(
                        adapter="bank.v0",
                        statement_date="2026-02-28",
                        transactions=[_transaction("2026-02-10", 10_000, account_ref="bank-1")],
                        reconciled=True,
                    )
                )
            )
            (parsed / "card" / "b.json").write_text(
                json.dumps(
                    _statement(
                        adapter="card.v0",
                        statement_date="2026-03-31",
                        transactions=[_transaction("2026-03-12", -5_000)],
                        reconciled=None,
                        issues=[{"code": "REVIEW"}],
                    )
                )
            )
            (parsed / "card" / "summary.json").write_text("{}")

            report = analyze_corpus(parsed, source_roots=(source,))

        self.assertEqual(2, report["statementCount"])
        self.assertEqual(2, report["transactionCount"])
        self.assertEqual("2026-02-10", report["transactionDateRange"]["from"])
        self.assertEqual("2026-03-12", report["transactionDateRange"]["to"])
        self.assertEqual({"bank.v0": 1, "card.v0": 1}, report["statementsByAdapter"])
        self.assertEqual({"reconciled": 1, "unknown": 1, "unreconciled": 0}, report["balanceChecks"])
        self.assertEqual(1, report["transactionsMissingAccountRef"])
        self.assertEqual(1, report["issueCount"])
        self.assertNotIn("description", json.dumps(report).lower())
        self.assertNotIn("amount", json.dumps(report).lower())

    def test_ignores_outputs_not_present_in_the_selected_source_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed = root / "parsed" / "bank"
            source = root / "corpus"
            parsed.mkdir(parents=True)
            source.mkdir()
            (source / "kept.pdf").write_bytes(b"source")
            payload = _statement(
                adapter="bank.v0",
                statement_date="2026-01-31",
                transactions=[_transaction("2026-01-01", 1)],
                reconciled=True,
            )
            (parsed / "kept.json").write_text(json.dumps(payload))
            (parsed / "stale.json").write_text(json.dumps(payload))

            report = analyze_corpus(root / "parsed", source_roots=(source,))

        self.assertEqual(1, report["statementCount"])
        self.assertEqual(1, report["excludedOutputCount"])

    def test_rejects_conflicting_outputs_for_the_same_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "corpus"
            source.mkdir()
            (source / "same.pdf").write_bytes(b"source")
            for directory, adapter in (("one", "bank.v0"), ("two", "card.v0")):
                target = root / "parsed" / directory
                target.mkdir(parents=True)
                (target / "same.json").write_text(
                    json.dumps(
                        _statement(
                            adapter=adapter,
                            statement_date="2026-01-31",
                            transactions=[_transaction("2026-01-01", 1)],
                            reconciled=True,
                        )
                    )
                )

            with self.assertRaisesRegex(ValueError, "Conflicting parsed outputs"):
                analyze_corpus(root / "parsed", source_roots=(source,))

    def test_can_select_canonical_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "corpus"
            source.mkdir()
            (source / "same.pdf").write_bytes(b"source")
            for directory, adapter in (("legacy", "old.v0"), ("current", "new.v0")):
                target = root / "parsed" / directory
                target.mkdir(parents=True)
                (target / "same.json").write_text(
                    json.dumps(
                        _statement(
                            adapter=adapter,
                            statement_date="2026-01-31",
                            transactions=[_transaction("2026-01-01", 1)],
                            reconciled=True,
                        )
                    )
                )

            report = analyze_corpus(
                root / "parsed",
                source_roots=(source,),
                output_directories=("current",),
            )

        self.assertEqual({"new.v0": 1}, report["statementsByAdapter"])


if __name__ == "__main__":
    unittest.main()
