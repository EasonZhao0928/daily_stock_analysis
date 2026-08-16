"""Small credential-store seam for personal channel adapters.

Secrets are kept outside the DSA database and logs.  macOS uses the Keychain
when the ``security`` utility is available; the fallback is an atomic 0600
JSON file suitable for a single-user local installation.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Protocol


class CredentialStore(Protocol):
    def read(self, ref: str) -> Optional[str]: ...

    def write(self, ref: str, value: str) -> None: ...

    def delete(self, ref: str) -> None: ...


class FileCredentialStore:
    """Atomic local secret file with directory/file permission checks."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _read_all(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def read(self, ref: str) -> Optional[str]:
        return self._read_all().get(str(ref))

    def _atomic_write(self, payload: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def write(self, ref: str, value: str) -> None:
        payload = self._read_all()
        payload[str(ref)] = str(value)
        self._atomic_write(payload)

    def delete(self, ref: str) -> None:
        payload = self._read_all()
        if str(ref) not in payload:
            return
        payload.pop(str(ref), None)
        if payload:
            self._atomic_write(payload)
        else:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class MacOSKeychainStore:
    """Keychain-backed store; raises when the platform utility is unavailable."""

    def __init__(self, service: str = "dsa-personal-channel"):
        if platform.system() != "Darwin" or shutil.which("security") is None:
            raise RuntimeError("macOS security utility is unavailable")
        self.service = service

    def read(self, ref: str) -> Optional[str]:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", self.service, "-a", str(ref), "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.rstrip("\n") if result.returncode == 0 else None

    def write(self, ref: str, value: str) -> None:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", self.service, "-a", str(ref), "-w", str(value)],
            capture_output=True,
            text=True,
            check=True,
        )

    def delete(self, ref: str) -> None:
        subprocess.run(
            ["security", "delete-generic-password", "-s", self.service, "-a", str(ref)],
            capture_output=True,
            text=True,
            check=False,
        )


def default_credential_store(path: str | Path | None = None) -> CredentialStore:
    """Choose Keychain first on macOS, otherwise a 0600 local file."""
    if path is None and platform.system() == "Darwin":
        try:
            return MacOSKeychainStore()
        except RuntimeError:
            pass
    fallback = Path(path).expanduser() if path is not None else Path.home() / ".dsa" / "credentials.json"
    return FileCredentialStore(fallback)


__all__ = ["CredentialStore", "FileCredentialStore", "MacOSKeychainStore", "default_credential_store"]
