from __future__ import annotations

from pathlib import Path
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError, PdfReadError


class InvalidPdfPasswordError(ValueError):
    """Raised when an encrypted PDF cannot be opened with the supplied password."""


def unlock_pdf(
    source_path: str | Path,
    output_path: str | Path,
    *,
    password: str,
) -> Path:
    """Write a verified, unencrypted copy while preserving the source PDF."""
    source = Path(source_path).expanduser()
    output = Path(output_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"PDF not found: {source}")
    if source.resolve() == output.resolve():
        raise ValueError("Input and output PDF paths must be different")
    if output.exists():
        raise FileExistsError(f"Output PDF already exists: {output}")

    try:
        reader = PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(password):
            raise InvalidPdfPasswordError("The supplied PDF password is incorrect")
        expected_page_count = len(reader.pages)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
    except InvalidPdfPasswordError:
        raise
    except (FileNotDecryptedError, PdfReadError) as error:
        raise InvalidPdfPasswordError("Unable to decrypt the PDF with that password") from error

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.stem}-",
            suffix=".pdf",
            dir=output.parent,
            delete=False,
        ) as temporary_stream:
            temporary_path = Path(temporary_stream.name)
            writer.write(temporary_stream)

        verification_reader = PdfReader(temporary_path)
        if verification_reader.is_encrypted:
            raise RuntimeError("Unlocked PDF verification failed: output is still encrypted")
        if len(verification_reader.pages) != expected_page_count:
            raise RuntimeError("Unlocked PDF verification failed: page count changed")
        temporary_path.replace(output)
        return output
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
