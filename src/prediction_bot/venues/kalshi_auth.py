from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

PrivateKey = RSAPrivateKey | Ed25519PrivateKey


def load_private_key(path: Path) -> PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey | Ed25519PrivateKey):
        raise TypeError(f"Unsupported key type {type(key).__name__}; use RSA or Ed25519")
    return key


def sign_message(private_key: PrivateKey, message: bytes) -> str:
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(message)
    else:
        sig = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode()


def sign_request(private_key: PrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """Kalshi signs `timestamp + METHOD + path` where path excludes the query string
    but includes the `/trade-api/v2` prefix."""
    path_without_query = path.split("?", 1)[0]
    return sign_message(private_key, f"{timestamp_ms}{method.upper()}{path_without_query}".encode())


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key: PrivateKey) -> None:
        self.api_key_id = api_key_id
        self.private_key = private_key

    @classmethod
    def from_file(cls, api_key_id: str, key_path: Path) -> KalshiSigner:
        return cls(api_key_id, load_private_key(key_path))

    def headers(self, method: str, url: str, now_ms: int | None = None) -> dict[str, str]:
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        path = urlparse(url).path
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign_request(self.private_key, ts, method, path),
        }
