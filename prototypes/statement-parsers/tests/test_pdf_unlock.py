from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from statement_parser.pdf_unlock import InvalidPdfPasswordError, unlock_pdf


class PdfUnlockTest(unittest.TestCase):
    def _create_encrypted_pdf(self, path: Path, password: str) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        writer.add_metadata({"/Title": "Synthetic private statement"})
        writer.encrypt(password)
        with path.open("wb") as stream:
            writer.write(stream)

    def test_writes_a_valid_unencrypted_copy_without_changing_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "statement.pdf"
            output = Path(temp_dir) / "statement_unlocked.pdf"
            self._create_encrypted_pdf(source, "correct horse")

            result = unlock_pdf(source, output, password="correct horse")

            source_reader = PdfReader(source)
            output_reader = PdfReader(output)
            self.assertEqual(output, result)
            self.assertTrue(source_reader.is_encrypted)
            self.assertFalse(output_reader.is_encrypted)
            self.assertEqual(1, len(output_reader.pages))
            self.assertEqual("Synthetic private statement", output_reader.metadata.title)

    def test_rejects_a_wrong_password_and_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "statement.pdf"
            output = Path(temp_dir) / "statement_unlocked.pdf"
            self._create_encrypted_pdf(source, "correct horse")

            with self.assertRaises(InvalidPdfPasswordError):
                unlock_pdf(source, output, password="wrong password")

            self.assertFalse(output.exists())

    def test_rejects_overwriting_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "statement.pdf"
            self._create_encrypted_pdf(source, "correct horse")

            with self.assertRaisesRegex(ValueError, "must be different"):
                unlock_pdf(source, source, password="correct horse")

    def test_rejects_overwriting_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "statement.pdf"
            output = Path(temp_dir) / "existing.pdf"
            self._create_encrypted_pdf(source, "correct horse")
            output.write_bytes(b"keep me")

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                unlock_pdf(source, output, password="correct horse")

            self.assertEqual(b"keep me", output.read_bytes())

    def test_copies_an_already_unencrypted_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "statement.pdf"
            output = Path(temp_dir) / "statement_copy.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)

            unlock_pdf(source, output, password="unused")

            self.assertFalse(PdfReader(output).is_encrypted)
            self.assertEqual(1, len(PdfReader(output).pages))


if __name__ == "__main__":
    unittest.main()
