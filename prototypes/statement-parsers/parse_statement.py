#!/usr/bin/env python3
"""Run a PROTOTYPE statement parser and emit canonical JSON locally."""

from __future__ import annotations

from dataclasses import asdict
import argparse
import json
from pathlib import Path

import pdfplumber

from statement_parsers import parse_extracted_text

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 100


def extract_pdf_text(path: Path) -> str:
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("statement must be an existing PDF file")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError("statement exceeds the 25 MiB prototype limit")
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise ValueError("statement exceeds the 100-page prototype limit")
        return "\n--- PAGE ---\n".join(
            page.extract_text(layout=True) or "" for page in pdf.pages
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("statement", type=Path)
    args = parser.parse_args()

    statement = parse_extracted_text(extract_pdf_text(args.statement))
    print(json.dumps(asdict(statement), default=str, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
