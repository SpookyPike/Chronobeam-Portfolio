#!/usr/bin/env python3
"""Small TLS-only fake OIDC provider for the isolated pilot smoke test.

This is smoke infrastructure, not a Chronobeam runtime component.  It exposes
only the discovery, JWKS, authorization-code, and token endpoints needed to
exercise the real production OIDC boundary over localhost.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SMOKE_SUBJECT = "smoke-subject-001"
KEY_ID = "chronobeam-smoke-key-1"


def _b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True)
class AuthorizationCode:
    code_challenge: str
    nonce: str


class FakeIdP:
    def __init__(self, issuer: str, client_id: str, audience: str, redirect_uri: str) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.audience = audience
        self.redirect_uri = redirect_uri
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, AuthorizationCode] = {}

    def metadata(self) -> dict[str, str]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
        }

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        public = self.private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": KEY_ID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url(public.n),
                    "e": _b64url(public.e),
                }
            ]
        }

    def authorize(self, query: dict[str, list[str]]) -> tuple[str, str] | None:
        required = ("client_id", "redirect_uri", "state", "nonce", "code_challenge")
        if any(not query.get(name, [""])[0] for name in required):
            return None
        if query["client_id"][0] != self.client_id or query["redirect_uri"][0] != self.redirect_uri:
            return None
        if query.get("response_type", [""])[0] != "code":
            return None
        if query.get("code_challenge_method", [""])[0] != "S256":
            return None
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code_challenge=query["code_challenge"][0], nonce=query["nonce"][0]
        )
        return code, query["state"][0]

    def exchange(self, values: dict[str, list[str]]) -> str | None:
        if values.get("grant_type", [""])[0] != "authorization_code":
            return None
        if values.get("client_id", [""])[0] != self.client_id:
            return None
        if values.get("redirect_uri", [""])[0] != self.redirect_uri:
            return None
        code = values.get("code", [""])[0]
        verifier = values.get("code_verifier", [""])[0]
        entry = self.codes.pop(code, None)
        if entry is None or not verifier or _s256(verifier) != entry.code_challenge:
            return None
        now = datetime.now(UTC)
        pem = self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "exp": now + timedelta(minutes=5),
                "iat": now,
                "sub": SMOKE_SUBJECT,
                "nonce": entry.nonce,
            },
            pem,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )


def _redirect_with_code(redirect_uri: str, code: str, state: str) -> str:
    parsed = urlparse(redirect_uri)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.update({"code": [code], "state": [state]})
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def make_handler(provider: FakeIdP) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ChronobeamSmokeIdP/1"

        def log_message(self, _format: str, *_args: object) -> None:
            """Never log authorization codes, state, nonce, or token requests."""

        def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
            elif parsed.path == "/.well-known/openid-configuration":
                self._json(HTTPStatus.OK, provider.metadata())
            elif parsed.path == "/jwks":
                self._json(HTTPStatus.OK, provider.jwks())
            elif parsed.path == "/authorize":
                result = provider.authorize(parse_qs(parsed.query, keep_blank_values=True))
                if result is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                    return
                code, state = result
                self.send_response(HTTPStatus.FOUND)
                self.send_header(
                    "Location", _redirect_with_code(provider.redirect_uri, code, state)
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/token":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            token = provider.exchange(parse_qs(body, keep_blank_values=True))
            if token is None:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                return
            self._json(HTTPStatus.OK, {"id_token": token, "token_type": "Bearer"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="TLS fake IdP for Chronobeam's pilot smoke")
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--redirect-uri", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()

    provider = FakeIdP(args.issuer, args.client_id, args.audience, args.redirect_uri)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(provider))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"fake IdP listening on {args.issuer}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
