"""Secrets at rest: Fernet encryption keyed by /config/secret.key (plex-safety rule 9)."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


class SecretBox:
    """Encrypt/decrypt strings with an instance key created on first boot (chmod 600)."""

    def __init__(self, config_dir: Path):
        key_path = config_dir / "secret.key"
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        self._fernet = Fernet(key_path.read_bytes())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
