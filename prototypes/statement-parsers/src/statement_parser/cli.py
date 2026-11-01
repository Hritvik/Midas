from __future__ import annotations

import argparse
import csv
from getpass import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence

from .gmail_inventory import (
    DEFAULT_STATEMENT_QUERY,
    GmailInventoryService,
    GmailReadOnlyClient,
)
from .gmail_corpus import GmailCorpusDownloader
from .parser import parse_pdf
from .password_store import KeychainPasswordStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unlock and parse bank statement PDFs")
    commands = parser.add_subparsers(dest="command", required=True)

    parse_command = commands.add_parser("parse", help="extract statement transactions")
    parse_command.add_argument("pdf", help="path to the statement PDF")
    parse_command.add_argument("--format", choices=("json", "csv"), default="json")
    parse_command.add_argument("--output", help="output path; stdout when omitted")
    parse_command.add_argument(
        "--password-env",
        help="environment variable containing the PDF password",
    )
    parse_command.add_argument(
        "--prompt-password",
        action="store_true",
        help="prompt securely for the PDF password",
    )
    parse_command.add_argument(
        "--password-key",
        help="macOS Keychain key containing the PDF password",
    )

    unlock_command = commands.add_parser("unlock", help="write an unencrypted PDF copy")
    unlock_command.add_argument("pdf", help="path to the encrypted PDF")
    unlock_command.add_argument("--output", required=True, help="path for the unlocked PDF")
    unlock_command.add_argument(
        "--password-env",
        help="environment variable containing the password; prompts securely when omitted",
    )
    unlock_command.add_argument(
        "--password-key",
        help="macOS Keychain key containing the PDF password",
    )

    password_command = commands.add_parser(
        "password", help="manage statement PDF passwords in macOS Keychain"
    )
    password_actions = password_command.add_subparsers(
        dest="password_action", required=True
    )
    password_set = password_actions.add_parser("set", help="save or replace a password")
    password_set.add_argument("key")
    password_delete = password_actions.add_parser("delete", help="delete a password")
    password_delete.add_argument("key")

    inventory_command = commands.add_parser(
        "gmail-inventory",
        help="inventory likely statement attachments without downloading them",
    )
    inventory_command.add_argument(
        "--output",
        required=True,
        help="private JSON inventory output path",
    )
    inventory_command.add_argument(
        "--token-env",
        default="GMAIL_ACCESS_TOKEN",
        help="environment variable holding a Gmail readonly OAuth access token",
    )
    inventory_command.add_argument("--query", default=DEFAULT_STATEMENT_QUERY)
    inventory_command.add_argument("--max-messages", type=int, default=500)

    corpus_command = commands.add_parser(
        "gmail-corpus",
        help="download likely statement attachments into a private local corpus",
    )
    corpus_command.add_argument(
        "--output-dir",
        required=True,
        help="private corpus directory; receives files/ and manifest.jsonl",
    )
    corpus_command.add_argument(
        "--token-env",
        default="GMAIL_ACCESS_TOKEN",
        help="environment variable holding a Gmail readonly OAuth access token",
    )
    corpus_command.add_argument("--query", default=DEFAULT_STATEMENT_QUERY)
    corpus_command.add_argument("--max-messages", type=int, default=500)
    return parser


def _write_csv(result, stream) -> None:
    fieldnames = (
        "bookedDate",
        "description",
        "amountMinor",
        "currency",
        "runningBalanceMinor",
        "externalRef",
        "sourcePage",
        "sourceRow",
    )
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(transaction.to_dict() for transaction in result.transactions)


def _unlock_pdf(source_path: str, output_path: Path, *, password: str) -> Path:
    try:
        from .pdf_unlock import unlock_pdf
    except ImportError as error:
        raise RuntimeError(
            "pypdf is required to unlock PDFs; install the project dependencies"
        ) from error
    return unlock_pdf(source_path, output_path, password=password)


def _resolve_password(arguments, *, prompt_by_default: bool) -> str | None:
    password_key = getattr(arguments, "password_key", None)
    if password_key:
        return KeychainPasswordStore().get(password_key)
    if arguments.password_env:
        password = os.environ.get(arguments.password_env)
        if password is None:
            raise ValueError(
                f"Password environment variable is not set: {arguments.password_env}"
            )
        return password
    if getattr(arguments, "prompt_password", False) or prompt_by_default:
        return getpass("PDF password: ")
    return None


def _write_private_json(output: Path, payload: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}-",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        temporary_path.replace(output)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _write_private_parse_result(output: Path, result, output_format: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output.name}-",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            if output_format == "json":
                json.dump(result.to_dict(), stream, indent=2)
                stream.write("\n")
            else:
                _write_csv(result, stream)
        temporary_path.replace(output)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(argv if argv is not None else sys.argv[1:])
    # Preserve the original `statement.pdf --output ...` parser invocation.
    normalized_arguments = (
        raw_arguments
        if raw_arguments
        and raw_arguments[0]
        in {"parse", "unlock", "password", "gmail-inventory", "gmail-corpus"}
        else ["parse", *raw_arguments]
    )
    arguments = _build_parser().parse_args(normalized_arguments)
    try:
        if arguments.command == "password":
            store = KeychainPasswordStore()
            if arguments.password_action == "set":
                store.set(arguments.key)
                print(f"Stored PDF password key: {arguments.key}")
                return 0
            store.delete(arguments.key)
            print(f"Deleted PDF password key: {arguments.key}")
            return 0

        if arguments.command == "unlock":
            password = _resolve_password(arguments, prompt_by_default=True)
            output = Path(arguments.output)
            unlocked_path = _unlock_pdf(arguments.pdf, output, password=password)
            print(unlocked_path)
            return 0

        if arguments.command in {"gmail-inventory", "gmail-corpus"}:
            access_token = os.environ.get(arguments.token_env, "").strip()
            if not access_token:
                raise ValueError(
                    "Gmail access token environment variable is not set: "
                    f"{arguments.token_env}"
                )
            client = GmailReadOnlyClient(access_token)
            inventory_service = GmailInventoryService(client)
            if arguments.command == "gmail-inventory":
                inventory = inventory_service.discover(
                    query=arguments.query,
                    max_messages=arguments.max_messages,
                )
                _write_private_json(Path(arguments.output), inventory.to_dict())
                return 0
            result = GmailCorpusDownloader(client, inventory_service).download(
                query=arguments.query,
                max_messages=arguments.max_messages,
                output_dir=Path(arguments.output_dir),
            )
            print(
                json.dumps(
                    {
                        "scannedMessages": result.scanned_messages,
                        "candidateMessages": result.candidate_messages,
                        "savedAttachments": result.saved_attachments,
                        "uniqueFiles": result.unique_files,
                        "manifest": str(result.manifest_path),
                        "filesDir": str(result.files_dir),
                    },
                    separators=(",", ":"),
                )
            )
            return 0

        password = _resolve_password(arguments, prompt_by_default=False)
        result = parse_pdf(arguments.pdf, password=password)
        if arguments.output:
            _write_private_parse_result(Path(arguments.output), result, arguments.format)
        elif arguments.format == "json":
            json.dump(result.to_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            _write_csv(result, sys.stdout)
        return 0
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as error:
        message = error.args[0] if isinstance(error, KeyError) else str(error)
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
