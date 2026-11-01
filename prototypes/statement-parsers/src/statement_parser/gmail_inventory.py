"""Read-only Gmail statement discovery without downloading attachment bytes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RAW_RESPONSE_BYTES = 75 * 1024 * 1024
SUPPORTED_EXTENSIONS = (".pdf", ".csv", ".zip")
DEFAULT_STATEMENT_QUERY = (
    "has:attachment {filename:pdf filename:csv filename:zip} "
    "{subject:statement subject:e-statement subject:\"account statement\" "
    "subject:\"credit card statement\"}"
)
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_INSTITUTIONS = {
    "axis": ("axisbank", "axis bank"),
    "sbi": ("sbicard", "sbi card", "sbi.co.in"),
    "hsbc": ("hsbc",),
    "hdfc": ("hdfc",),
    "icici": ("icici",),
    "amex": ("american express", "amex"),
    "yes": ("yes bank", "yesbank", "yes.bank.in"),
    "idfc": ("idfc first", "idfcfirstbank"),
    "kotak": ("kotak",),
    "slice": ("slice",),
}


class GmailGateway(Protocol):
    def list_messages(self, *, query: str, page_token: str | None) -> dict: ...

    def get_message_metadata(self, message_id: str) -> dict: ...


@dataclass(frozen=True)
class AttachmentReference:
    attachment_id: str
    filename: str
    mime_type: str
    size: int

    def to_dict(self) -> dict:
        return {
            "attachmentId": self.attachment_id,
            "filename": self.filename,
            "mimeType": self.mime_type,
            "size": self.size,
        }


@dataclass(frozen=True)
class StatementCandidate:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    received_at_ms: int | None
    institution_hint: str | None
    attachments: tuple[AttachmentReference, ...]

    def to_dict(self) -> dict:
        return {
            "messageId": self.message_id,
            "threadId": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "receivedAtMs": self.received_at_ms,
            "institutionHint": self.institution_hint,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }


@dataclass(frozen=True)
class GmailInventory:
    query: str
    scanned_messages: int
    candidates: tuple[StatementCandidate, ...]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "scannedMessages": self.scanned_messages,
            "candidateMessages": len(self.candidates),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class GmailReadOnlyClient:
    """Minimal Gmail REST client that exposes only GET operations."""

    def __init__(
        self,
        access_token: str,
        *,
        opener: Callable = urlopen,
        timeout_seconds: int = 20,
    ) -> None:
        token = access_token.strip()
        if not token:
            raise ValueError("Gmail access token is required")
        self._access_token = token
        self._opener = opener
        self._timeout_seconds = timeout_seconds

    def list_messages(self, *, query: str, page_token: str | None) -> dict:
        parameters: dict[str, str | int] = {
            "q": query,
            "maxResults": 100,
        }
        if page_token:
            parameters["pageToken"] = page_token
        return self._get(f"messages?{urlencode(parameters)}")

    def get_message_metadata(self, message_id: str) -> dict:
        if not _MESSAGE_ID.fullmatch(message_id):
            raise ValueError("Invalid Gmail message ID")
        parameters = [
            ("format", "metadata"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "Subject"),
        ]
        return self._get(f"messages/{quote(message_id)}?{urlencode(parameters)}")

    def get_message_raw(self, message_id: str) -> dict:
        if not _MESSAGE_ID.fullmatch(message_id):
            raise ValueError("Invalid Gmail message ID")
        return self._get(
            f"messages/{quote(message_id)}?{urlencode({'format': 'raw'})}",
            max_response_bytes=MAX_RAW_RESPONSE_BYTES,
        )

    def _get(self, relative_url: str, *, max_response_bytes: int = MAX_RESPONSE_BYTES) -> dict:
        request = Request(
            f"{GMAIL_API_BASE}/{relative_url}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_response_bytes:
                    raise RuntimeError("Gmail response exceeded the safe size limit")
                payload = response.read(max_response_bytes + 1)
        except HTTPError as error:
            raise RuntimeError(f"Gmail API request failed with HTTP {error.code}") from error
        except URLError as error:
            raise RuntimeError("Gmail API request failed") from error
        if len(payload) > max_response_bytes:
            raise RuntimeError("Gmail response exceeded the safe size limit")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Gmail API returned an invalid response") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("Gmail API returned an invalid response")
        return decoded


class GmailInventoryService:
    def __init__(self, gateway: GmailGateway) -> None:
        self._gateway = gateway

    def discover(self, *, query: str, max_messages: int) -> GmailInventory:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Gmail query must not be empty")
        if not 1 <= max_messages <= 5000:
            raise ValueError("max_messages must be between 1 and 5000")

        message_ids: tuple[str, ...] = ()
        page_token: str | None = None
        seen_page_tokens: tuple[str, ...] = ()
        while len(message_ids) < max_messages:
            response = self._gateway.list_messages(
                query=normalized_query,
                page_token=page_token,
            )
            page_ids = tuple(
                str(message["id"])
                for message in response.get("messages", ())
                if isinstance(message, Mapping) and message.get("id")
            )
            message_ids = (*message_ids, *page_ids[: max_messages - len(message_ids)])
            next_page = response.get("nextPageToken")
            if not next_page or len(message_ids) >= max_messages:
                break
            next_page = str(next_page)
            if next_page in seen_page_tokens:
                raise RuntimeError("Gmail pagination repeated a page token")
            seen_page_tokens = (*seen_page_tokens, next_page)
            page_token = next_page

        candidates = tuple(
            candidate
            for message_id in message_ids
            if (candidate := _candidate(self._gateway.get_message_metadata(message_id)))
            is not None
        )
        return GmailInventory(
            query=normalized_query,
            scanned_messages=len(message_ids),
            candidates=candidates,
        )


def _candidate(message: Mapping[str, object]) -> StatementCandidate | None:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return None
    attachments = _attachments(payload)
    if not attachments:
        return None
    headers = _headers(payload)
    sender = headers.get("from", "")
    subject = headers.get("subject", "")
    identifying_text = " ".join(
        (sender, subject, *(attachment.filename for attachment in attachments))
    ).lower()
    institution_hint = next(
        (
            institution
            for institution, markers in _INSTITUTIONS.items()
            if any(marker in identifying_text for marker in markers)
        ),
        None,
    )
    internal_date = message.get("internalDate")
    return StatementCandidate(
        message_id=str(message.get("id", "")),
        thread_id=str(message.get("threadId", "")),
        sender=sender,
        subject=subject,
        received_at_ms=int(internal_date) if str(internal_date or "").isdigit() else None,
        institution_hint=institution_hint,
        attachments=attachments,
    )


def _headers(payload: Mapping[str, object]) -> dict[str, str]:
    headers = payload.get("headers", ())
    if not isinstance(headers, list):
        return {}
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
        if isinstance(header, Mapping)
    }


def _attachments(part: Mapping[str, object]) -> tuple[AttachmentReference, ...]:
    found: tuple[AttachmentReference, ...] = ()
    filename = str(part.get("filename", "")).strip()
    body = part.get("body")
    if filename.lower().endswith(SUPPORTED_EXTENSIONS) and isinstance(body, Mapping):
        attachment_id = str(body.get("attachmentId", "")).strip()
        if attachment_id:
            raw_size = body.get("size", 0)
            found = (
                AttachmentReference(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=str(part.get("mimeType", "application/octet-stream")),
                    size=int(raw_size) if str(raw_size).isdigit() else 0,
                ),
            )
    parts = part.get("parts", ())
    if isinstance(parts, list):
        for child in parts:
            if isinstance(child, Mapping):
                found = (*found, *_attachments(child))
    return found
