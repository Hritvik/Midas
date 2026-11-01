from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Sequence


MAX_PDF_PAGES = 100


@dataclass(frozen=True)
class LayoutLine:
    top: float
    text: str
    date_text: str = ""
    description: str = ""
    deposit: str = ""
    withdrawal: str = ""
    balance: str = ""


@dataclass(frozen=True)
class LayoutPage:
    page_number: int
    text: str
    account_number: str | None
    lines: tuple[LayoutLine, ...]


def _group_words_by_line(
    words: Sequence[Mapping[str, object]], tolerance: float = 2.0
) -> tuple[tuple[Mapping[str, object], ...], ...]:
    groups: tuple[tuple[Mapping[str, object], ...], ...] = ()
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not groups or abs(float(word["top"]) - float(groups[-1][0]["top"])) > tolerance:
            groups = (*groups, (word,))
        else:
            groups = (*groups[:-1], (*groups[-1], word))
    return groups


def _column_text(words: Iterable[Mapping[str, object]], start: float, end: float) -> str:
    selected = [str(word["text"]) for word in words if start <= float(word["x0"]) < end]
    return " ".join(selected).strip()


def extract_layout_pages(
    pdf_path: str,
    *,
    password: str | None = None,
) -> tuple[LayoutPage, ...]:
    try:
        import pdfplumber
    except ImportError as error:
        raise RuntimeError(
            "pdfplumber is required to read PDFs; install the project dependencies"
        ) from error

    pages: tuple[LayoutPage, ...] = ()
    open_kwargs = {"password": password} if password is not None else {}
    with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise ValueError("Statement exceeds the 100-page prototype limit")
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            if "Details of Your Accounts" not in text:
                pages = (*pages, LayoutPage(page_number, text, None, ()))
                continue

            account_match = re.search(r"SAVINGS ACCOUNT-RES\s+(\d{3}-\d{6}-\d{3})", text)
            words = page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False)
            table_words = [
                word
                for word in words
                if 250 <= float(word["top"]) <= 505
            ]
            lines = tuple(
                LayoutLine(
                    top=float(group[0]["top"]),
                    text=" ".join(str(word["text"]) for word in group),
                    date_text=_column_text(group, 0, 90),
                    description=_column_text(group, 90, 320),
                    deposit=_column_text(group, 320, 390),
                    withdrawal=_column_text(group, 390, 480),
                    balance=_column_text(group, 480, 580),
                )
                for group in _group_words_by_line(table_words)
            )
            pages = (
                *pages,
                LayoutPage(
                    page_number=page_number,
                    text=text,
                    account_number=account_match.group(1) if account_match else None,
                    lines=lines,
                ),
            )
    return pages
