from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from statement_parser.cli import main
from statement_parser.gmail_inventory import GmailInventory
from statement_parser.models import ParseResult, StatementTransaction


class CliTest(unittest.TestCase):
    def test_writes_json_output(self) -> None:
        result = ParseResult(
            adapter_id="bank-statement.hsbc.india.composite.v1",
            statement_date="2026-10-09",
            account_number="000-111111-222",
            currency="INR",
            opening_balance_minor=10000,
            closing_balance_minor=9000,
            transactions=(
                StatementTransaction(
                    booked_date="2026-10-01",
                    description="TEST",
                    amount_minor=-1000,
                    currency="INR",
                    running_balance_minor=9000,
                    external_ref=None,
                    source_page=2,
                    source_row=1,
                ),
            ),
            issues=(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "transactions.json"
            with patch("statement_parser.cli.parse_pdf", return_value=result):
                exit_code = main(["input.pdf", "--output", str(output)])

            payload = json.loads(output.read_text(encoding="utf-8"))
            mode = output.stat().st_mode & 0o777

        self.assertEqual(0, exit_code)
        self.assertEqual(-1000, payload["transactions"][0]["amountMinor"])
        self.assertTrue(payload["isBalanceReconciled"])
        self.assertEqual(0o600, mode)

    def test_unlock_command_reads_password_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "unlocked.pdf"
            with (
                patch.dict("os.environ", {"STATEMENT_PASSWORD": "secret"}),
                patch("statement_parser.cli._unlock_pdf", return_value=output) as unlock,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = main(
                    [
                        "unlock",
                        "locked.pdf",
                        "--output",
                        str(output),
                        "--password-env",
                        "STATEMENT_PASSWORD",
                    ]
                )

        self.assertEqual(0, exit_code)
        unlock.assert_called_once_with("locked.pdf", output, password="secret")

    def test_parse_command_reads_password_from_environment(self) -> None:
        result = ParseResult(
            adapter_id="bank-statement.hsbc.india.composite.v1",
            statement_date="2026-10-09",
            account_number="000-111111-222",
            currency="INR",
            opening_balance_minor=10000,
            closing_balance_minor=10000,
            transactions=(),
            issues=(),
        )

        with (
            patch.dict("os.environ", {"STATEMENT_PASSWORD": "secret"}),
            patch("statement_parser.cli.parse_pdf", return_value=result) as parse_pdf,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = main(
                [
                    "parse",
                    "locked.pdf",
                    "--password-env",
                    "STATEMENT_PASSWORD",
                ]
            )

        self.assertEqual(0, exit_code)
        parse_pdf.assert_called_once_with("locked.pdf", password="secret")

    def test_missing_parse_password_environment_variable_is_a_cli_error(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("statement_parser.cli.parse_pdf") as parse_pdf,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = main(
                [
                    "parse",
                    "locked.pdf",
                    "--password-env",
                    "STATEMENT_PASSWORD",
                ]
            )

        self.assertEqual(2, exit_code)
        parse_pdf.assert_not_called()
        self.assertIn("Password environment variable is not set", stderr.getvalue())

    def test_parse_command_reads_password_from_keychain(self) -> None:
        result = ParseResult(
            adapter_id="axis.credit-card.pdf.v0",
            statement_date="2026-09-01",
            account_number=None,
            currency="INR",
            opening_balance_minor=None,
            closing_balance_minor=None,
            transactions=(),
            issues=(),
        )
        with (
            patch("statement_parser.cli.KeychainPasswordStore") as store_type,
            patch("statement_parser.cli.parse_pdf", return_value=result) as parse_pdf,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            store_type.return_value.get.return_value = "keychain secret"
            exit_code = main(
                [
                    "parse",
                    "locked.pdf",
                    "--password-key",
                    "axis-credit-card",
                ]
            )

        self.assertEqual(0, exit_code)
        store_type.return_value.get.assert_called_once_with("axis-credit-card")
        parse_pdf.assert_called_once_with("locked.pdf", password="keychain secret")

    def test_missing_keychain_password_is_a_cli_error(self) -> None:
        with (
            patch("statement_parser.cli.KeychainPasswordStore") as store_type,
            patch("statement_parser.cli.parse_pdf") as parse_pdf,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            store_type.return_value.get.side_effect = KeyError(
                "No stored PDF password for key: axis-credit-card"
            )
            exit_code = main(
                ["parse", "locked.pdf", "--password-key", "axis-credit-card"]
            )

        self.assertEqual(2, exit_code)
        parse_pdf.assert_not_called()
        self.assertIn("No stored PDF password", stderr.getvalue())

    def test_password_store_set_delegates_prompt_to_keychain(self) -> None:
        with patch("statement_parser.cli.KeychainPasswordStore") as store_type:
            exit_code = main(["password", "set", "axis-credit-card"])

        self.assertEqual(0, exit_code)
        store_type.return_value.set.assert_called_once_with("axis-credit-card")

    def test_writes_a_private_gmail_candidate_inventory_without_downloading(self) -> None:
        inventory = GmailInventory(
            query="has:attachment subject:statement",
            scanned_messages=3,
            candidates=(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "gmail-inventory.json"
            with (
                patch.dict("os.environ", {"GMAIL_ACCESS_TOKEN": "token"}),
                patch("statement_parser.cli.GmailReadOnlyClient") as client,
                patch("statement_parser.cli.GmailInventoryService") as service,
            ):
                service.return_value.discover.return_value = inventory
                exit_code = main(
                    [
                        "gmail-inventory",
                        "--output",
                        str(output),
                        "--max-messages",
                        "50",
                    ]
                )
                payload = json.loads(output.read_text(encoding="utf-8"))
                mode = output.stat().st_mode & 0o777

        self.assertEqual(0, exit_code)
        self.assertEqual(3, payload["scannedMessages"])
        self.assertEqual(0o600, mode)
        client.assert_called_once_with("token")
        service.return_value.discover.assert_called_once()

    def test_gmail_inventory_requires_the_access_token_environment_variable(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = main(
                ["gmail-inventory", "--output", "inventory.json"]
            )

        self.assertEqual(2, exit_code)
        self.assertIn("Gmail access token environment variable", stderr.getvalue())

    def test_gmail_corpus_downloads_to_private_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "corpus"
            with (
                patch.dict("os.environ", {"GMAIL_ACCESS_TOKEN": "token"}),
                patch("statement_parser.cli.GmailReadOnlyClient") as client,
                patch("statement_parser.cli.GmailInventoryService") as service,
                patch("statement_parser.cli.GmailCorpusDownloader") as downloader,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                downloader.return_value.download.return_value.scanned_messages = 3
                downloader.return_value.download.return_value.candidate_messages = 2
                downloader.return_value.download.return_value.saved_attachments = 2
                downloader.return_value.download.return_value.unique_files = 2
                downloader.return_value.download.return_value.manifest_path = (
                    output_dir / "manifest.jsonl"
                )
                downloader.return_value.download.return_value.files_dir = (
                    output_dir / "files"
                )

                exit_code = main(
                    [
                        "gmail-corpus",
                        "--output-dir",
                        str(output_dir),
                        "--max-messages",
                        "50",
                    ]
                )
                payload = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(2, payload["savedAttachments"])
        client.assert_called_once_with("token")
        service.assert_called_once_with(client.return_value)
        downloader.return_value.download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
