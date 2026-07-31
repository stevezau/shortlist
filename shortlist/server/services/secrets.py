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
            # Created with the mode already set, rather than write-then-chmod: between those two calls
            # the key that decrypts every Plex token and LLM key on this instance is readable at
            # whatever the umask allows (rule 9). Same shape as `main._instance_secret`.
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        self._fernet = Fernet(key_path.read_bytes())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
