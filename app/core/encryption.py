"""Encryption for personal data stored in the database (KYC name, date of birth, address,
PAN).

- AES-256-GCM with a random 96-bit nonce per value. Tampering is detected on decrypt.
- Each value is bound to a context such as "kyc:<user id>:name". A value copied into
  another row or column fails to decrypt instead of showing someone else's data.
- lookup_hash() gives a keyed hash for finding duplicates (the same PAN on two
  accounts) without storing the value in a searchable form.
- Both keys are derived from one 32-byte master key (KYC_ENCRYPTION_KEY). Stored values
  start with "v1:", so a future key can be added without breaking old values.
"""

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32
_NONCE_BYTES = 12
_VERSION = "v1"


class DecryptionError(Exception):
    """The value was tampered with, belongs to another context or uses an unknown key."""


def decode_master_key(value: str) -> bytes:
    """KYC_ENCRYPTION_KEY is 32 random bytes, URL-safe base64 encoded."""
    try:
        key = base64.urlsafe_b64decode(value.encode())
    except (binascii.Error, ValueError) as error:
        msg = "must be URL-safe base64"
        raise ValueError(msg) from error
    if len(key) != KEY_BYTES:
        msg = f"must decode to {KEY_BYTES} bytes"
        raise ValueError(msg)
    return key


def _derive(master_key: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=purpose).derive(
        master_key
    )


class FieldCipher:
    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != KEY_BYTES:
            msg = f"master key must be {KEY_BYTES} bytes"
            raise ValueError(msg)
        self._aead = AESGCM(_derive(master_key, b"dhan-setu/field-encryption/v1"))
        self._lookup_key = _derive(master_key, b"dhan-setu/lookup-hash/v1")

    def __repr__(self) -> str:
        return "FieldCipher(<keys hidden>)"

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{_VERSION}:" + base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, token: str, *, context: str) -> str:
        version, _, payload = token.partition(":")
        if version != _VERSION or not payload:
            raise DecryptionError
        try:
            raw = base64.urlsafe_b64decode(payload.encode())
            nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
            return self._aead.decrypt(nonce, ciphertext, context.encode()).decode()
        except (binascii.Error, ValueError, InvalidTag) as error:
            raise DecryptionError from error

    def lookup_hash(self, value: str, *, purpose: str) -> str:
        """Same value and purpose -> same hash; can't be reversed without the key."""
        message = f"{purpose}:{value}".encode()
        return hmac.new(self._lookup_key, message, hashlib.sha256).hexdigest()
