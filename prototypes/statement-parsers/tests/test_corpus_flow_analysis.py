from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from statement_parser.corpus_flow_analysis import analyze_owned_flows


def _statement(adapter: str, account: str, transactions: list[dict]) -> dict:
    return {
        "adapterId": adapter,
        "statementDate": "2026-09-30",
        "accountNumber": "",
        "accountRef": account,
        "currency": "INR",
        "openingBalanceMinor": None,
        "closingBalanceMinor": None,
        "isBalanceReconciled": None,
        "transactions": transactions,
        "issues": [],
    }


def _transaction(
    *, account: str, amount: int, description: str, row: int = 1
) -> dict:
    return {
        "bookedDate": "2026-09-10",
        "description": description,
        "amountMinor": amount,
        "currency": "INR",
        "runningBalanceMinor": None,
        "externalRef": None,
        "sourcePage": 1,
        "sourceRow": row,
        "accountRef": account,
    }


class CorpusFlowAnalysisTest(unittest.TestCase):
    def test_emits_only_aggregate_flow_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bank = root / "bank" / "one.json"
            card = root / "card" / "two.json"
            bank.parent.mkdir()
            card.parent.mkdir()
            bank.write_text(
                json.dumps(
                    _statement(
                        "hdfc.savings-account.pdf.v0",
                        "acct_aaaaaaaaaaaaaaaaaaaaaaaa",
                        [_transaction(account="acct_aaaaaaaaaaaaaaaaaaaaaaaa", amount=-25_000, description="card bill")],
                    )
                )
            )
            card.write_text(
                json.dumps(
                    _statement(
                        "axis.credit-card.pdf.v0",
                        "acct_bbbbbbbbbbbbbbbbbbbbbbbb",
                        [
                            _transaction(
                                account="acct_bbbbbbbbbbbbbbbbbbbbbbbb",
                                amount=25_000,
                                description="payment received thank you",
                            )
                        ],
                    )
                )
            )

            report = analyze_owned_flows(root)

        self.assertEqual(2, report["sourceTransactionCount"])
        self.assertEqual(1, report["acceptedCardPaymentCount"])
        self.assertEqual(0, report["reviewItemCount"])
        self.assertEqual(0, report["invariantViolationCount"])
        serialized = json.dumps(report)
        self.assertNotIn("card bill", serialized)
        self.assertNotIn("acct_aaaaaaaaaaaaaaaaaaaaaaaa", serialized)
        self.assertNotIn("25000", serialized)

    def test_unknown_adapter_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "unknown" / "one.json"
            output.parent.mkdir()
            output.write_text(json.dumps(_statement("new.adapter.v1", "acct_a", [])))

            with self.assertRaisesRegex(ValueError, "Unknown adapter"):
                analyze_owned_flows(root)

    def test_rejects_non_opaque_account_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "bank" / "one.json"
            output.parent.mkdir()
            output.write_text(
                json.dumps(
                    _statement(
                        "hdfc.savings-account.pdf.v0",
                        "acct_aaaaaaaaaaaaaaaaaaaaaaaa",
                        [
                            _transaction(
                                account="acct_1234567890123456",
                                amount=-25_000,
                                description="card bill",
                            )
                        ],
                    )
                )
            )

            with self.assertRaisesRegex(ValueError, "opaque account reference"):
                analyze_owned_flows(root)

    def test_rejects_oversized_statement_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "bank" / "one.json"
            output.parent.mkdir()
            with output.open("wb") as stream:
                stream.seek(10 * 1024 * 1024)
                stream.write(b"x")

            with self.assertRaisesRegex(ValueError, "size limit"):
                analyze_owned_flows(root)

    def test_rejects_corpus_over_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "bank" / "one.json"
            output.parent.mkdir()
            output.write_text(json.dumps(_statement("hdfc.savings-account.pdf.v0", "", [])))

            with patch("statement_parser.corpus_flow_analysis.MAX_CORPUS_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "corpus byte limit"):
                    analyze_owned_flows(root)

    def test_duplicate_source_ids_across_directories_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = json.dumps(_statement("hdfc.savings-account.pdf.v0", "", []))
            for directory in ("bank-a", "bank-b"):
                output = root / directory / "same-source.json"
                output.parent.mkdir()
                output.write_text(payload)

            with self.assertRaisesRegex(ValueError, "Duplicate source ID"):
                analyze_owned_flows(root)


if __name__ == "__main__":
    unittest.main()
