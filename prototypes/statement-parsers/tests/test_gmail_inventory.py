from __future__ import annotations

import unittest

from statement_parser.gmail_inventory import GmailInventoryService


class FakeGateway:
    def __init__(self) -> None:
        self.list_calls: list[str | None] = []
        self.metadata_calls: list[str] = []

    def list_messages(self, *, query: str, page_token: str | None) -> dict:
        self.list_calls.append(page_token)
        if page_token is None:
            return {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "next"}
        return {"messages": [{"id": "m3"}]}

    def get_message_metadata(self, message_id: str) -> dict:
        self.metadata_calls.append(message_id)
        messages = {
            "m1": {
                "id": "m1",
                "threadId": "t1",
                "internalDate": "1791234567000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "statements@hsbc.co.in"},
                        {"name": "Subject", "value": "Your monthly e-statement"},
                    ],
                    "parts": [
                        {
                            "mimeType": "multipart/mixed",
                            "parts": [
                                {
                                    "filename": "account-statement.pdf",
                                    "mimeType": "application/pdf",
                                    "body": {"attachmentId": "a1", "size": 1234},
                                }
                            ],
                        }
                    ],
                },
            },
            "m2": {
                "id": "m2",
                "threadId": "t2",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "newsletter@example.com"},
                        {"name": "Subject", "value": "Monthly product statement"},
                    ],
                    "parts": [
                        {
                            "filename": "brochure.png",
                            "mimeType": "image/png",
                            "body": {"attachmentId": "a2", "size": 999},
                        }
                    ],
                },
            },
            "m3": {
                "id": "m3",
                "threadId": "t3",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "cards@axisbank.com"},
                        {"name": "Subject", "value": "Credit Card Statement"},
                    ],
                    "body": {"attachmentId": "a3", "size": 4321},
                    "filename": "card.csv",
                    "mimeType": "text/csv",
                },
            },
        }
        return messages[message_id]


class GmailInventoryServiceTest(unittest.TestCase):
    def test_recognizes_all_supported_statement_provider_hints(self) -> None:
        expected = {
            "alerts@yesbank.in": "yes",
            "statements@idfcfirstbank.com": "idfc",
            "cards@kotak.com": "kotak",
            "statements@sliceit.com": "slice",
        }
        for index, (sender, institution) in enumerate(expected.items(), start=1):
            gateway = FakeGateway()
            message = gateway.get_message_metadata("m1")
            message["id"] = f"provider-{index}"
            message["payload"]["headers"][0]["value"] = sender
            gateway.list_messages = lambda **_: {"messages": [{"id": "m1"}]}
            gateway.get_message_metadata = lambda _message_id, value=message: value

            inventory = GmailInventoryService(gateway).discover(
                query="has:attachment",
                max_messages=1,
            )

            self.assertEqual(institution, inventory.candidates[0].institution_hint)
    def test_discovers_supported_attachments_across_pages_without_downloading_them(self) -> None:
        gateway = FakeGateway()

        inventory = GmailInventoryService(gateway).discover(
            query="has:attachment subject:statement",
            max_messages=10,
        )

        self.assertEqual((None, "next"), tuple(gateway.list_calls))
        self.assertEqual(("m1", "m2", "m3"), tuple(gateway.metadata_calls))
        self.assertEqual(("m1", "m3"), tuple(item.message_id for item in inventory.candidates))
        self.assertEqual("hsbc", inventory.candidates[0].institution_hint)
        self.assertEqual("axis", inventory.candidates[1].institution_hint)
        self.assertEqual("a1", inventory.candidates[0].attachments[0].attachment_id)

    def test_stops_at_the_configured_message_limit(self) -> None:
        gateway = FakeGateway()

        inventory = GmailInventoryService(gateway).discover(
            query="has:attachment",
            max_messages=1,
        )

        self.assertEqual((None,), tuple(gateway.list_calls))
        self.assertEqual(("m1",), tuple(gateway.metadata_calls))
        self.assertEqual(1, inventory.scanned_messages)

    def test_rejects_an_unbounded_or_empty_inventory_request(self) -> None:
        service = GmailInventoryService(FakeGateway())

        with self.assertRaisesRegex(ValueError, "query"):
            service.discover(query=" ", max_messages=10)
        with self.assertRaisesRegex(ValueError, "between 1 and 5000"):
            service.discover(query="has:attachment", max_messages=0)


if __name__ == "__main__":
    unittest.main()
