"""Credential vault — the execution-boundary store for secret material.

Phase 1's credential-non-exposure rule, made concrete: secrets (OAuth tokens,
refresh tokens) live in a directory OUTSIDE the repo (`settings.vault_dir`, which is
git-ignored regardless). The orchestrating/agent code path never holds raw secret
bytes — it holds a `SecretHandle`, an opaque reference that knows *which* secret but
not its value. Only code at the execution boundary (e.g. building the Gmail service)
resolves a handle through the vault.

A handle is safe to pass around and safe to log: its `repr` carries the key name, never
the secret. Nothing here ever prints or logs the secret payload.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import settings


class SecretHandle:
    """An opaque reference to a secret in a vault. Carries the vault and a key name;
    structurally does not expose the secret value. Pass this to the orchestrator;
    resolve it only at the execution boundary via the vault."""

    __slots__ = ("_vault", "key")

    def __init__(self, vault: "CredentialVault", key: str) -> None:
        self._vault = vault
        self.key = key

    def __repr__(self) -> str:  # never the value
        return f"SecretHandle(key={self.key!r})"

    __str__ = __repr__


class CredentialVault:
    """A directory of secret files outside the repo. Reads/writes raw bytes by handle.

    The only object that touches secret bytes; callers above the execution boundary
    receive handles from `handle()` and never see the payload."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.vault_dir)

    def handle(self, key: str) -> SecretHandle:
        return SecretHandle(self, key)

    def _file(self, key: str) -> Path:
        return self.path / key

    def exists(self, handle: SecretHandle) -> bool:
        return self._file(handle.key).exists()

    def read_bytes(self, handle: SecretHandle) -> Optional[bytes]:
        p = self._file(handle.key)
        return p.read_bytes() if p.exists() else None

    def write_bytes(self, handle: SecretHandle, data: bytes) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        p = self._file(handle.key)
        p.write_bytes(data)
        # Best-effort owner-only perms; harmless no-op where unsupported.
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
