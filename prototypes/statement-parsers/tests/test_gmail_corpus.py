from __future__ import annotations

import base64
from email.message import EmailMessage
import json
import tempfile
import unittest
from pathlib import Path

from statement_parser.gmail_corpus import GmailCorpusDownloader
from statement_parser.gmail_inventory import (
    AttachmentReference,
    GmailInventory,
    StatementCandidate,
)


def _raw_message(*attachments: tuple[str, bytes, str]) -> str:
    message = EmailMessage()
    message["From"] = "statements@example.bank"
    message["To"] = "me@example.com"
    message["Subject"] = "Statement"
    message.set_content("Please find attached.")
    for filename, payload, mime_type in attachments:
        maintype, subtype = mime_type.split("/", 1)
        message.add_attachment(
            payload,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


class FakeGateway:
    def __init__(self, raw_by_message_id: dict[str, str]) -> None:
        self.raw_by_message_id = raw_by_message_id
        self.raw_calls: list[str] = []

    def get_message_raw(self, message_id: str) -> dict:
        self.raw_calls.append(message_id)
        return {
            "id": message_id,
            "internalDate": "1791234567000",
            "raw": self.raw_by_message_id[message_id],
        }


class FakeInventoryService:
    def __init__(self, inventory: GmailInventory) -> None:
        self.inventory = inventory
        self.calls: list[tuple[str, int]] = []

    def discover(self, *, query: str, max_messages: int) -> GmailInventory:
        self.calls.append((query, max_messages))
        return self.inventory


def _candidate(message_id: str) -> StatementCandidate:
    return StatementCandidate(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="statements@example.bank",
        subject="Monthly statement",
        received_at_ms=1791234567000,
        institution_hint="axis",
        attachments=(
            AttachmentReference(
                attachment_id=f"attachment-{message_id}",
                filename="statement.pdf",
                mime_type="application/pdf",
                size=12,
            ),
        ),
    )


class GmailCorpusDownloaderTest(unittest.TestCase):
    def test_downloads_statement_attachments_with_private_hash_manifest(self) -> None:
        inventory = GmailInventory(
            query="has:attachment",
            scanned_messages=1,
            candidates=(_candidate("m1"),),
        )
        gateway = FakeGateway(
            {
                "m1": _raw_message(
                    ("statement.pdf", b"%PDF-statement", "application/pdf"),
                    ("Most Important Terms & Conditions.pdf", b"%PDF-terms", "application/pdf"),
                    ("logo.png", b"png", "image/png"),
                )
            }
        )
        service = FakeInventoryService(inventory)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = GmailCorpusDownloader(gateway, service).download(
                query="has:attachment",
                max_messages=50,
                output_dir=Path(temp_dir),
            )
            files = sorted((Path(temp_dir) / "files").iterdir())
            manifest_path = Path(temp_dir) / "manifest.jsonl"
            manifest = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
            ]
            manifest_mode = manifest_path.stat().st_mode & 0o777

        self.assertEqual(1, result.saved_attachments)
        self.assertEqual(1, len(files))
        self.assertEqual("statement.pdf", manifest[0]["original_filename"])
        self.assertEqual(files[0].name, manifest[0]["stored_filename"])
        self.assertEqual(0o600, manifest_mode)
        self.assertEqual(("has:attachment", 50), service.calls[0])

    def test_deduplicates_exact_attachment_bytes(self) -> None:
        inventory = GmailInventory(
            query="has:attachment",
            scanned_messages=2,
            candidates=(_candidate("m1"), _candidate("m2")),
        )
        raw = _raw_message(("statement.pdf", b"%PDF-same", "application/pdf"))
        gateway = FakeGateway({"m1": raw, "m2": raw})

        with tempfile.TemporaryDirectory() as temp_dir:
            result = GmailCorpusDownloader(
                gateway,
                FakeInventoryService(inventory),
            ).download(
                query="has:attachment",
                max_messages=2,
                output_dir=Path(temp_dir),
            )
            files = list((Path(temp_dir) / "files").iterdir())

        self.assertEqual(2, result.saved_attachments)
        self.assertEqual(1, result.unique_files)
        self.assertEqual(1, len(files))

    def test_rejects_invalid_raw_gmail_response(self) -> None:
        inventory = GmailInventory(
            query="has:attachment",
            scanned_messages=1,
            candidates=(_candidate("m1"),),
        )
        gateway = FakeGateway({"m1": "not base64"})

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "invalid raw message"):
                GmailCorpusDownloader(gateway, FakeInventoryService(inventory)).download(
                    query="has:attachment",
                    max_messages=1,
                    output_dir=Path(temp_dir),
                )


if __name__ == "__main__":
    unittest.main()
