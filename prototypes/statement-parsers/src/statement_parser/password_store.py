"""macOS Keychain-backed storage for statement PDF passwords."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence


KEYCHAIN_SERVICE = "com.distracteddev.midas.statement-pdf"
_VALID_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_Runner = Callable[..., subprocess.CompletedProcess[str]]


class KeychainPasswordStore:
    def __init__(
        self,
        *,
        runner: _Runner = subprocess.run,
    ) -> None:
        self._runner = runner

    def set(self, key: str) -> None:
        _validate_key(key)
        arguments = (
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            key,
            "-s",
            KEYCHAIN_SERVICE,
            "-l",
            f"Midas statement PDF: {key}",
            "-w",
        )
        try:
            result = self._runner(list(arguments), check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "Unable to save the PDF password in macOS Keychain"
            ) from error
        if result.returncode != 0:
            raise RuntimeError("Unable to save the PDF password in macOS Keychain")

    def get(self, key: str) -> str:
        _validate_key(key)
        result = self._run(
            (
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                key,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            )
        )
        if result.returncode != 0:
            raise KeyError(f"No stored PDF password for key: {key}")
        password = result.stdout.rstrip("\r\n")
        if not password:
            raise RuntimeError("Stored PDF password is empty")
        return password

    def delete(self, key: str) -> None:
        _validate_key(key)
        result = self._run(
            (
                "/usr/bin/security",
                "delete-generic-password",
                "-a",
                key,
                "-s",
                KEYCHAIN_SERVICE,
            )
        )
        if result.returncode != 0:
            raise KeyError(f"No stored PDF password for key: {key}")

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(
            list(arguments),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
def _validate_key(key: str) -> None:
    if not _VALID_KEY.fullmatch(key):
        raise ValueError(
            "Invalid password key; use 1-64 lowercase letters, digits, '.', '_' or '-'"
        )
