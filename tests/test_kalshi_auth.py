from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from prediction_bot.venues.kalshi_auth import (
    KalshiSigner,
    load_private_key,
    load_private_key_pem,
    sign_request,
)


def test_rsa_pss_signature_verifies_and_strips_query() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sig = sign_request(key, "1700000000000", "GET", "/trade-api/v2/portfolio/balance?x=1")
    key.public_key().verify(
        base64.b64decode(sig),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    sig = sign_request(key, "1", "POST", "/trade-api/v2/portfolio/events/orders")
    key.public_key().verify(base64.b64decode(sig), b"1POST/trade-api/v2/portfolio/events/orders")


def test_inline_pem_with_flattened_newlines_loads() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    flattened = pem.replace("\n", " ")
    loaded = load_private_key_pem(flattened)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    assert loaded.private_numbers() == key.private_numbers()
    assert isinstance(KalshiSigner.from_pem("k", pem).private_key, rsa.RSAPrivateKey)
    with pytest.raises(ValueError):
        load_private_key_pem("not a key")


def test_headers_use_url_path_including_prefix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "k.pem"
    path.write_bytes(pem)
    assert isinstance(load_private_key(path), ed25519.Ed25519PrivateKey)

    signer = KalshiSigner.from_file("key-id", path)
    h = signer.headers(
        "GET", "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance?a=b", now_ms=42
    )
    assert h["KALSHI-ACCESS-KEY"] == "key-id"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "42"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"42GET/trade-api/v2/portfolio/balance"
    )
