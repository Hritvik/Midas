"""Download a private local corpus of Gmail statement attachments."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import email
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from .gmail_inventory import GmailInventoryService


ALLOWED_ATTACHMENT_EXTENSIONS = frozenset((".pdf", ".csv", ".zip"))
AUXILIARY_ATTACHMENT_NAME = re.compile(
    r"\b(?:"
    r"mitc|most[\s_-]*important|terms?(?:[\s_-]*(?:and|&)[\s_-]*conditions?)?|"
    r"key[\s_-]*fact|schedule[\s_-]*(?:of[\s_-]*)?(?:charges?|fees?)|"
    r"charges?|fees?|welcome[\s_-]*kit|insurance|rewards?|benefits?|privacy|tariff"
    r")\b",
    re.IGNORECASE,
)
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
_BASE64_URLSAFE = re.compile(r"^[A-Za-z0-9_-]*$")


class GmailRawGateway(Protocol):
    def get_message_raw(self, message_id: str) -> dict: ...


@dataclass(frozen=True)
class GmailCorpusDownloadResult:
    scanned_messages: int
    candidate_messages: int
    saved_attachments: int
    unique_files: int
    manifest_path: Path
    files_dir: Path


class GmailCorpusDownloader:
    def __init__(
        self,
        gateway: GmailRawGateway,
        inventory_service: GmailInventoryService,
    ) -> None:
        self._gateway = gateway
        self._inventory_service = inventory_service

    def download(
        self,
        *,
        query: str,
        max_messages: int,
        output_dir: Path,
    ) -> GmailCorpusDownloadResult:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        files_dir = output_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(files_dir, 0o700)

        manifest_path = output_dir / "manifest.jsonl"
        seen_hashes = _read_seen_hashes(manifest_path)
        saved_attachments = 0
        unique_files = len(seen_hashes)

        inventory = self._inventory_service.discover(
            query=query,
            max_messages=max_messages,
        )
        with manifest_path.open("a", encoding="utf-8") as manifest:
            os.chmod(manifest_path, 0o600)
            for candidate in inventory.candidates:
                response = self._gateway.get_message_raw(candidate.message_id)
                raw_message = _decode_raw_message(response)
                for attachment in _statement_attachments(raw_message):
                    digest = hashlib.sha256(attachment.payload).hexdigest()
                    suffix = Path(attachment.filename).suffix.lower()
                    stored_filename = f"{digest}{suffix}"
                    target = files_dir / stored_filename
                    duplicate = digest in seen_hashes
                    if not duplicate:
                        with target.open("xb") as stream:
                            stream.write(attachment.payload)
                        os.chmod(target, 0o600)
                        seen_hashes.add(digest)
                        unique_files += 1
                    manifest.write(
                        json.dumps(
                            {
                                "message_id": candidate.message_id,
                                "thread_id": candidate.thread_id,
                                "received_at_ms": candidate.received_at_ms,
                                "institution_hint": candidate.institution_hint,
                                "original_filename": attachment.filename,
                                "stored_filename": stored_filename,
                                "mime_type": attachment.mime_type,
                                "size_bytes": len(attachment.payload),
                                "sha256": digest,
                                "duplicate": duplicate,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    manifest.flush()
                    saved_attachments += 1

        return GmailCorpusDownloadResult(
            scanned_messages=inventory.scanned_messages,
            candidate_messages=len(inventory.candidates),
            saved_attachments=saved_attachments,
            unique_files=unique_files,
            manifest_path=manifest_path,
            files_dir=files_dir,
        )


@dataclass(frozen=True)
class _AttachmentPayload:
    filename: str
    mime_type: str
    payload: bytes


def _read_seen_hashes(manifest_path: Path) -> set[str]:
    if not manifest_path.exists():
        return set()
    hashes: set[str] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping) and isinstance(record.get("sha256"), str):
            hashes.add(record["sha256"])
    return hashes


def _decode_raw_message(response: Mapping[str, object]) -> email.message.EmailMessage:
    raw = response.get("raw")
    if not isinstance(raw, str) or not _BASE64_URLSAFE.fullmatch(raw):
        raise RuntimeError("Gmail API returned an invalid raw message")
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError as error:
        raise RuntimeError("Gmail API returned an invalid raw message") from error
    return email.message_from_bytes(payload, policy=email.policy.default)


def _statement_attachments(
    message: email.message.EmailMessage,
) -> tuple[_AttachmentPayload, ...]:
    attachments: tuple[_AttachmentPayload, ...] = ()
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        safe_filename = Path(filename).name
        suffix = Path(safe_filename).suffix.lower()
        normalized_name = safe_filename.replace(".", " ")
        if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
            continue
        if AUXILIARY_ATTACHMENT_NAME.search(normalized_name):
            continue
        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        attachments = (
            *attachments,
            _AttachmentPayload(
                filename=safe_filename,
                mime_type=part.get_content_type(),
                payload=payload,
            ),
        )
    return attachments
