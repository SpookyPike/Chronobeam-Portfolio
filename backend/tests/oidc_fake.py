"""Socket-free OIDC provider used by production-boundary tests.

It deliberately replaces only ``app.auth.oidc.build_opener``. Discovery, JWKS
caching, HTTPS URL checks, PKCE construction and token validation therefore
remain production code in every test which uses it.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from urllib.request import Request

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeIdP:
    issuer = "https://idp.test"
    audience = "chronobeam"
    client_id = "chronobeam"
    discovery_url = "https://idp.test/.well-known/openid-configuration"
    authorization_endpoint = "https://idp.test/authorize"
    token_endpoint = "https://idp.test/token"  # noqa: S105 - endpoint, not a secret
    jwks_uri = "https://idp.test/jwks"

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.active_kid = "key-1"
        self.published_kids: set[str] = set()
        self.add_key(self.active_kid, publish=True)
        self.codes: dict[str, tuple[str, str, str]] = {}
        self.fetches: dict[str, int] = {"discovery": 0, "jwks": 0, "token": 0}
        self.fail_jwks = False
        self.discovery_issuer: str = self.issuer

    def add_key(self, kid: str, *, publish: bool = False) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if publish:
            self.published_kids.add(kid)

    def rotate(self, kid: str = "key-2") -> None:
        self.add_key(kid, publish=True)
        self.active_kid = kid

    def retire(self, kid: str) -> None:
        self.published_kids.discard(kid)

    def _jwk(self, kid: str) -> dict[str, str]:
        public = self.keys[kid].public_key().public_numbers()

        def _value(number: int) -> str:
            raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return {
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": _value(public.n),
            "e": _value(public.e),
        }

    def mint(
        self,
        sub: str = "employee",
        *,
        iss: str | None = None,
        aud: str | list[str] | None = None,
        exp: datetime | None = None,
        iat: datetime | None = None,
        nonce: str | None = None,
        azp: str | None = None,
        kid: str | None = None,
        signing_kid: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        header_kid = kid or self.active_kid
        key_kid = signing_kid or header_kid
        if key_kid not in self.keys:
            self.add_key(key_kid)
        payload: dict[str, Any] = {
            "iss": self.issuer if iss is None else iss,
            "aud": self.audience if aud is None else aud,
            "exp": exp or now + timedelta(minutes=5),
            "iat": iat or now - timedelta(seconds=1),
            "sub": sub,
        }
        if nonce is not None:
            payload["nonce"] = nonce
        if azp is not None:
            payload["azp"] = azp
        private_pem = self.keys[key_kid].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": header_kid})

    def issue_code(self, *, subject: str = "employee", verifier: str, nonce: str) -> str:
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = (subject, verifier, nonce)
        return code

    def open(self, request: Request, timeout: int = 5) -> _Response:
        url = request.full_url
        if url == self.discovery_url:
            self.fetches["discovery"] += 1
            return _Response(
                {
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": self.authorization_endpoint,
                    "token_endpoint": self.token_endpoint,
                    "jwks_uri": self.jwks_uri,
                }
            )
        if url == self.jwks_uri:
            self.fetches["jwks"] += 1
            if self.fail_jwks:
                raise OSError("fake jwks unavailable")
            return _Response({"keys": [self._jwk(kid) for kid in sorted(self.published_kids)]})
        if url == self.token_endpoint:
            self.fetches["token"] += 1
            raw_data = request.data
            body = parse_qs(raw_data.decode() if isinstance(raw_data, bytes) else "")
            code = body.get("code", [""])[0]
            verifier = body.get("code_verifier", [""])[0]
            entry = self.codes.pop(code, None)
            if entry is None or entry[1] != verifier:
                raise OSError("invalid code or verifier")
            return _Response({"id_token": self.mint(entry[0], nonce=entry[2])})
        raise OSError(f"unexpected fake URL {url}")
