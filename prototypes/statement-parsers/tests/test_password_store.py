from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock

from statement_parser.password_store import KeychainPasswordStore


class KeychainPasswordStoreTest(unittest.TestCase):
    def test_saves_password_using_keychains_own_interactive_prompt(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        store = KeychainPasswordStore(runner=runner)

        store.set("axis-credit-card")

        arguments = runner.call_args.args[0]
        self.assertEqual("-w", arguments[-1])
        self.assertNotIn("input", runner.call_args.kwargs)
        self.assertNotIn("capture_output", runner.call_args.kwargs)

    def test_reads_password_without_logging_or_transforming_it(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, "very secret\n", "")
        )
        store = KeychainPasswordStore(runner=runner)

        self.assertEqual("very secret", store.get("axis-credit-card"))
        self.assertIn("-w", runner.call_args.args[0])

    def test_reports_a_missing_password_without_exposing_command_errors(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 44, "", "keychain detail")
        )
        store = KeychainPasswordStore(runner=runner)

        with self.assertRaisesRegex(KeyError, "axis-credit-card"):
            store.get("axis-credit-card")

    def test_rejects_unsafe_key_names_before_running_security(self) -> None:
        runner = Mock()
        store = KeychainPasswordStore(runner=runner)

        with self.assertRaisesRegex(ValueError, "password key"):
            store.get("../../secrets")

        runner.assert_not_called()

    def test_converts_keychain_timeout_to_safe_error(self) -> None:
        runner = Mock(side_effect=subprocess.TimeoutExpired("security", 120))
        store = KeychainPasswordStore(runner=runner)

        with self.assertRaisesRegex(RuntimeError, "Unable to save"):
            store.set("axis-credit-card")


if __name__ == "__main__":
    unittest.main()
