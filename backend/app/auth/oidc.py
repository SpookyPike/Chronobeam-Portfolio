"""OIDC discovery/JWKS validation for production; static PEM only in dev/test."""

import base64
import hashlib
import json
import time
from typing import Any, cast
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import Settings, get_settings

REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub", "nonce")
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class InvalidOidcToken(Exception):
    """Token validation failed; callers expose only a generic error."""


class OidcBoundaryNotConfigured(Exception):
    """The OIDC boundary this call needs is not configured.

    Deliberately NOT mapped to an HTTP status: production cannot start with an
    unconfigured boundary (``validate_production_settings`` raises at startup),
    so the request paths that call into here (``/auth/login``, ``/auth/callback``
    — both production-only) can never raise this. It guards direct/offline
    callers, including the dev/test static-key seam.
    """


def _https_url(value: object) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.netloc:
        raise InvalidOidcToken("OIDC endpoint is not HTTPS")
    return str(value)


class _HttpsOnlyRedirectHandler(HTTPRedirectHandler):
    """Keep HTTPS OIDC requests from following a redirect to cleartext HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects for credential-bearing OIDC token requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_https(request: Request, *, allow_redirects: bool = True):
    _https_url(request.full_url)
    redirect_handler = _HttpsOnlyRedirectHandler() if allow_redirects else _NoRedirectHandler()
    return build_opener(redirect_handler).open(request, timeout=5)


def _fetch_json(url: str) -> dict[str, Any]:
    # `_open_https` validates the initial URL and every redirect before opening it.
    request = Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with _open_https(request) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise InvalidOidcToken("invalid OIDC response")
    return cast(dict[str, Any], payload)


def discovery(settings: Settings) -> dict[str, Any]:
    if not settings.oidc_discovery_url:
        raise OidcBoundaryNotConfigured("OIDC discovery is not configured")
    metadata = _fetch_json(_https_url(settings.oidc_discovery_url))
    if metadata.get("issuer") != settings.oidc_issuer:
        raise InvalidOidcToken("discovery issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        _https_url(metadata.get(key))
    return metadata


def _jwks(settings: Settings, metadata: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    url = _https_url(metadata["jwks_uri"])
    cached = _jwks_cache.get(url)
    if not refresh and cached and cached[0] > time.monotonic():
        return cached[1]
    payload = _fetch_json(url)
    if not isinstance(payload.get("keys"), list):
        raise InvalidOidcToken("invalid JWKS")
    _jwks_cache[url] = (time.monotonic() + settings.jwks_cache_ttl_seconds, payload)
    return payload


def _key_for_token(token: str, settings: Settings, metadata: dict[str, Any]) -> Any:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidOidcToken("invalid token") from exc
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise InvalidOidcToken("unsupported signing key")
    for refresh in (False, True):  # unknown kid gets exactly one refresh
        keys = _jwks(settings, metadata, refresh).get("keys", [])
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
        if len(matches) == 1:
            try:
                return RSAAlgorithm.from_jwk(json.dumps(matches[0]))
            except (TypeError, ValueError, jwt.PyJWTError) as exc:
                raise InvalidOidcToken("invalid JWKS key") from exc
    raise InvalidOidcToken("unknown signing key")


def _decode(token: str, settings: Settings, nonce: str | None = None) -> dict[str, Any]:
    metadata = discovery(settings)
    try:
        payload = jwt.decode(
            token,
            _key_for_token(token, settings, metadata),
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_clock_skew_seconds,
            options=cast(
                Any, {"require": list(REQUIRED_CLAIMS if nonce else REQUIRED_CLAIMS[:-1])}
            ),
        )
    except jwt.PyJWTError as exc:
        raise InvalidOidcToken("Invalid or expired token.") from exc
    # OIDC requires ``azp`` to name this client whenever it is present, and
    # REQUIRES it when ``aud`` has more than one value: a multi-audience token
    # without ``azp`` does not say which party it was issued to, so accepting
    # it would let a token minted for another client authenticate here.
    # PyJWT validates ``aud`` above, but it cannot know this client-id
    # relationship. A single-valued ``aud`` without ``azp`` stays acceptable
    # (the pilot IdP emits exactly that).
    azp = payload.get("azp")
    audience = payload.get("aud")
    if azp is not None and azp != settings.oidc_client_id:
        raise InvalidOidcToken("invalid authorized party")
    if azp is None and isinstance(audience, list) and len(audience) > 1:
        raise InvalidOidcToken("multi-audience token without azp")
    if nonce is not None and payload.get("nonce") != nonce:
        raise InvalidOidcToken("invalid nonce")
    return cast(dict[str, Any], payload)


def validate_production_token(token: str, *, nonce: str | None = None) -> str:
    settings = get_settings()
    if not settings.oidc_configured:
        raise OidcBoundaryNotConfigured("OIDC boundary not configured")
    return str(_decode(token, settings, nonce)["sub"])


def validate_id_token(token: str) -> str:
    """Legacy static-key validation, intentionally unavailable in production."""
    settings = get_settings()
    if settings.is_production:
        return validate_production_token(token)
    if not (settings.oidc_issuer and settings.oidc_audience and settings.oidc_public_key):
        raise OidcBoundaryNotConfigured("OIDC boundary not configured")
    try:
        payload = jwt.decode(
            token,
            settings.oidc_public_key,
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options=cast(Any, {"require": ["iss", "aud", "exp", "iat", "sub"]}),
        )
    except jwt.PyJWTError as exc:
        raise InvalidOidcToken("Invalid or expired token.") from exc
    return str(payload["sub"])


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(
    settings: Settings, metadata: dict[str, Any], *, state: str, nonce: str, verifier: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": f"{settings.public_base_url.rstrip('/')}/api/v1/auth/callback",
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{metadata['authorization_endpoint']}?{query}"


def exchange_code(settings: Settings, metadata: dict[str, Any], *, code: str, verifier: str) -> str:
    values = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.oidc_client_id,
        "redirect_uri": f"{settings.public_base_url.rstrip('/')}/api/v1/auth/callback",
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if settings.oidc_client_auth_method == "basic":
        pair = f"{settings.oidc_client_id}:{settings.oidc_client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
    elif settings.oidc_client_auth_method == "post":
        values["client_secret"] = settings.oidc_client_secret
    elif settings.oidc_client_auth_method == "private_key_jwt":
        raise InvalidOidcToken("private_key_jwt profile not approved")
    try:
        request = Request(  # noqa: S310 - discovery verified the HTTPS endpoint
            _https_url(metadata["token_endpoint"]),
            data=urlencode(values).encode(),
            headers=headers,
            method="POST",
        )
        # The request contains the client secret (Basic header or form field).
        # Never forward it through a token endpoint redirect, even to HTTPS.
        with _open_https(request, allow_redirects=False) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise InvalidOidcToken("code exchange failed") from exc
    token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(token, str):
        raise InvalidOidcToken("code exchange returned no ID token")
    return token
