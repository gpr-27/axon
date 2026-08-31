"""
Session encryption and authenticated keystream cipher for Axon session transcripts.
Protects stored conversations and secrets at rest in ~/.axon/sessions/*.jsonl.
Uses PBKDF2-HMAC-SHA256 key derivation with authenticated AES/HMAC payload envelopes.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
from typing import Any


def _derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """Derive encryption key (32 bytes) and HMAC authentication key (32 bytes) via PBKDF2."""
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        iterations=100_000,
        dklen=64,
    )
    return derived[:32], derived[32:]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate cryptographically secure keystream from key and nonce."""
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    return bytes(stream[:length])


def encrypt_session_record(data: str, passphrase: str) -> str:
    """
    Encrypt a plaintext session record string.
    Envelope format: ENC:v1:<base64(salt + nonce + ciphertext + tag)>
    """
    if not passphrase or not data:
        return data

    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, auth_key = _derive_keys(passphrase, salt)

    raw_bytes = data.encode("utf-8")
    stream = _keystream(enc_key, nonce, len(raw_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(raw_bytes, stream))

    tag = hmac.new(auth_key, salt + nonce + ciphertext, hashlib.sha256).digest()
    payload = salt + nonce + ciphertext + tag
    b64 = base64.b64encode(payload).decode("ascii")
    return f"ENC:v1:{b64}"


def decrypt_session_record(record_str: str, passphrase: str) -> str:
    """
    Decrypt an encrypted session record.
    Returns original plaintext or raises ValueError on authentication failure.
    """
    if not record_str.startswith("ENC:v1:"):
        return record_str  # Unencrypted record

    if not passphrase:
        return "[Encrypted Session Record — Passphrase required to decrypt]"

    try:
        b64_payload = record_str[len("ENC:v1:"):]
        payload = base64.b64decode(b64_payload)
        if len(payload) < 64:  # salt(16) + nonce(16) + tag(32)
            return "[Corrupt Encrypted Record]"

        salt = payload[:16]
        nonce = payload[16:32]
        tag = payload[-32:]
        ciphertext = payload[32:-32]

        enc_key, auth_key = _derive_keys(passphrase, salt)
        expected_tag = hmac.new(auth_key, salt + nonce + ciphertext, hashlib.sha256).digest()

        if not hmac.compare_digest(tag, expected_tag):
            return "[Decryption Failed: Invalid Passphrase or Corrupt Data]"

        stream = _keystream(enc_key, nonce, len(ciphertext))
        plaintext_bytes = bytes(a ^ b for a, b in zip(ciphertext, stream))
        return plaintext_bytes.decode("utf-8")
    except Exception as e:
        return f"[Decryption Error: {e}]"
