"""Symmetric encryption for profile credentials and env values.

The key is derived from the daemon's bearer token via HKDF-SHA256. This
keeps the key inside the daemon process (never written to disk) and ties
encrypted blobs to the active bearer token: rotating the bearer token
invalidates all encrypted profile data, which is documented as the v1
tradeoff. Users re-enter credentials after a rotation.

Storage layout: each encrypted column is a base64 Fernet token (`str`),
which is JSON-safe and round-trips through SQLite TEXT columns.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_HKDF_SALT = b"nightdesk.profile_secrets.v1"
_HKDF_INFO = b"fernet-key"


def _derive_key(bearer_token: str) -> bytes:
    if not bearer_token:
        raise ValueError("bearer_token must be non-empty to derive a key")
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(bearer_token.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class ProfileSecretBox:
    """Encrypts/decrypts JSON-serializable values bound to a bearer token."""

    def __init__(self, bearer_token: str):
        self._fernet = Fernet(_derive_key(bearer_token))

    def encrypt(self, value: Any) -> str:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, token: str | None) -> Any:
        if token is None or token == "":
            return None
        try:
            payload = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise ValueError("encrypted profile value is unreadable; "
                             "bearer token may have rotated") from exc
        return json.loads(payload.decode("utf-8"))
