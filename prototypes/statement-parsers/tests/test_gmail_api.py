from __future__ import annotations

import io
import json
import unittest

from statement_parser.gmail_inventory import GmailReadOnlyClient


class Response:
    def __init__(self, payload: dict) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))
        self.headers = {"Content-Length": str(len(self._body.getvalue()))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class GmailReadOnlyClientTest(unittest.TestCase):
    def test_lists_messages_with_get_and_bearer_authorization(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response({"messages": [{"id": "abc_123"}]})

        client = GmailReadOnlyClient("secret-token", opener=opener)
        result = client.list_messages(query="has:attachment subject:statement", page_token=None)

        self.assertEqual("abc_123", result["messages"][0]["id"])
        request, timeout = requests[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("Bearer secret-token", request.get_header("Authorization"))
        self.assertNotIn("secret-token", request.full_url)
        self.assertEqual(20, timeout)

    def test_rejects_invalid_message_ids_before_building_a_request(self) -> None:
        client = GmailReadOnlyClient("secret-token", opener=lambda *_args, **_kwargs: None)

        with self.assertRaisesRegex(ValueError, "message ID"):
            client.get_message_metadata("../unsafe")
        with self.assertRaisesRegex(ValueError, "message ID"):
            client.get_message_raw("../unsafe")

    def test_reads_raw_messages_with_a_larger_response_cap(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response({"id": "abc_123", "raw": "UmF3"})

        client = GmailReadOnlyClient("secret-token", opener=opener)
        result = client.get_message_raw("abc_123")

        self.assertEqual("UmF3", result["raw"])
        request, _timeout = requests[0]
        self.assertIn("format=raw", request.full_url)
        self.assertEqual("Bearer secret-token", request.get_header("Authorization"))

    def test_rejects_empty_access_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, "access token"):
            GmailReadOnlyClient(" ")


if __name__ == "__main__":
    unittest.main()
