"""OIDC boundary unit tests  — locally generated RSA keys.

The boundary is config-driven and stateless: RS256 pinned, iss/aud/exp/iat/sub
required, static public-key PEM. No JWKS/discovery (``OPEN-11``: the real IdP
protocol is unknown).
"""

import uuid
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from io import BytesIO
from typing import Any
from urllib.request import Request

import jwt
import pytest
from app.auth import oidc
from app.auth.oidc import InvalidOidcToken, OidcBoundaryNotConfigured, validate_id_token
from app.config import get_settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.invalid/issuer"
AUDIENCE = "chronobeam-api"


@pytest.fixture()
def oidc_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[bytes, bytes]:
    """Configure the OIDC boundary and return (private_key_pem, public_key_pem)."""
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("CHRONOBEAM_OIDC_PUBLIC_KEY", public_pem.decode())
    get_settings.cache_clear()
    return private_pem, public_pem


def _sign(private_pem: bytes, **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(minutes=10),
        "iat": datetime.now(UTC) - timedelta(minutes=1),
        "sub": f"sub-{uuid.uuid4()}",
    }
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="RS256")


def test_valid_token_returns_subject(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    subject = "emp-subject-1"
    token = _sign(private_pem, sub=subject)
    assert validate_id_token(token) == subject


def test_wrong_audience_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    with pytest.raises(InvalidOidcToken):
        validate_id_token(_sign(private_pem, aud="other-app"))


def test_wrong_issuer_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    with pytest.raises(InvalidOidcToken):
        validate_id_token(_sign(private_pem, iss="https://evil.invalid/issuer"))


def test_expired_token_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    with pytest.raises(InvalidOidcToken):
        validate_id_token(_sign(private_pem, exp=datetime.now(UTC) - timedelta(minutes=5)))


def test_missing_required_claim_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    with pytest.raises(InvalidOidcToken):
        validate_id_token(_sign(private_pem, iat=None))  # iat required by options
    with pytest.raises(InvalidOidcToken):
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": datetime.now(UTC) + timedelta(minutes=10),
                "iat": datetime.now(UTC) - timedelta(minutes=1),
            },
            private_pem,
            algorithm="RS256",
        )
        validate_id_token(token)  # no sub -> rejected


def test_alg_none_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=10),
            "iat": datetime.now(UTC) - timedelta(minutes=1),
            "sub": "x",
        },
        None,  # type: ignore[arg-type]  # alg="none" requires key=None in PyJWT
        algorithm="none",
    )
    with pytest.raises(InvalidOidcToken):
        validate_id_token(token)


def test_hs256_confusion_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    """A token signed with HS256 must not be accepted: algorithms are pinned
    to RS256, so any non-RS256 alg is rejected at decode time regardless of
    the key material."""
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=10),
            "iat": datetime.now(UTC) - timedelta(minutes=1),
            "sub": "x",
        },
        b"a-symmetric-hmac-secret-0123456789",
        algorithm="HS256",
    )
    with pytest.raises(InvalidOidcToken):
        validate_id_token(token)


def test_tampered_signature_rejected(oidc_env: tuple[bytes, bytes]) -> None:
    private_pem, _ = oidc_env
    token = _sign(private_pem, sub="a")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidOidcToken):
        validate_id_token(tampered)


def test_unconfigured_boundary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRONOBEAM_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("CHRONOBEAM_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("CHRONOBEAM_OIDC_PUBLIC_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(OidcBoundaryNotConfigured):
        validate_id_token("any.token.value")


def test_oidc_transport_rejects_cleartext_url_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_opened(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an insecure OIDC URL must be refused before opening a connection")

    monkeypatch.setattr(oidc, "build_opener", fail_if_opened)
    with pytest.raises(InvalidOidcToken, match="not HTTPS"):
        oidc._open_https(Request("http://idp.example.invalid/metadata"))


def test_oidc_redirect_handler_rejects_downgrade() -> None:
    handler = oidc._HttpsOnlyRedirectHandler()
    request = Request("https://idp.example.invalid/metadata")

    with pytest.raises(InvalidOidcToken, match="not HTTPS"):
        handler.redirect_request(
            request,
            BytesIO(),
            302,
            "Found",
            HTTPMessage(),
            "http://idp.example.invalid/metadata",
        )


def test_oidc_redirect_handler_allows_https_redirect() -> None:
    handler = oidc._HttpsOnlyRedirectHandler()
    request = Request("https://idp.example.invalid/metadata")

    redirected = handler.redirect_request(
        request,
        BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://idp.example.invalid/metadata-v2",
    )

    assert redirected is not None
    assert redirected.full_url == "https://idp.example.invalid/metadata-v2"


def test_oidc_token_request_does_not_forward_credentials_on_redirect() -> None:
    handler = oidc._NoRedirectHandler()
    request = Request(
        "https://idp.example.invalid/token",
        data=b"grant_type=authorization_code&client_secret=secret",
        headers={"Authorization": "Basic c2VjcmV0"},
        method="POST",
    )

    redirected = handler.redirect_request(
        request,
        BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://attacker.example.invalid/collect",
    )

    assert redirected is None
