"""Authentication, abuse controls and integrity helpers."""

from __future__ import annotations

import base64
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import secrets
from threading import Lock
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .config import Settings


@dataclass(frozen=True)
class APIPrincipal:
    key_id: str
    scopes: frozenset[str]


class APIKeyAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._keys = settings.parsed_api_keys
        self._demo_keys: dict[str, tuple[str, float, str]] = {}
        self._lock = Lock()

    def authenticate(self, supplied_key: str | None) -> APIPrincipal:
        if not supplied_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-API-Key header.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        for key_id, expected in self._keys.items():
            if hmac.compare_digest(supplied_key, expected):
                return APIPrincipal(key_id=key_id, scopes=frozenset({"*"}))
        now = time.monotonic()
        with self._lock:
            expired = [key for key, (_, expires_at, _) in self._demo_keys.items() if expires_at <= now]
            for key in expired:
                del self._demo_keys[key]
            demo_record = self._demo_keys.get(supplied_key)
            if demo_record and hmac.compare_digest(supplied_key, demo_record[0]):
                return APIPrincipal(
                    key_id=demo_record[2],
                    scopes=frozenset({"deliveries:read", "deliveries:write"}),
                )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    def issue_demo_key(self, *, ttl_seconds: int) -> str:
        key = "demo_crr_" + secrets.token_urlsafe(24)
        key_id = "demo_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        with self._lock:
            self._demo_keys[key] = (key, time.monotonic() + ttl_seconds, key_id)
        return key


class SlidingWindowRateLimiter:
    """An in-process safety limit. Replace with Redis for horizontally scaled deployments."""

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            window = self._hits[key]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.max_requests:
                retry_after = max(1, int(60 - (now - window[0])))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded.",
                    headers={"Retry-After": str(retry_after)},
                )
            window.append(now)


async def require_api_key(request: Request) -> APIPrincipal:
    supplied_key = request.headers.get("X-API-Key")
    authorization = request.headers.get("Authorization", "")
    if not supplied_key and authorization.lower().startswith("bearer "):
        supplied_key = authorization[7:].strip()
    principal = request.app.state.authenticator.authenticate(supplied_key)
    if "*" not in principal.scopes:
        action = "read" if request.method in {"GET", "HEAD"} else "write"
        required_scope = "deliveries" if request.url.path.startswith("/v1/deliveries") else "admin"
        if f"{required_scope}:{action}" not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key does not have the required scope.",
            )
    request.app.state.rate_limiter.check(principal.key_id)
    request.state.actor = principal.key_id
    return principal


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_fingerprint(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_idempotency_key(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required.")
    if not 8 <= len(value) <= 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key must contain 8 to 255 characters.")
    return value


def encode_cursor(created_at: str, shipment_id: str) -> str:
    raw = canonical_json({"created_at": created_at, "id": shipment_id}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at, shipment_id = decoded["created_at"], decoded["id"]
        if not isinstance(created_at, str) or not isinstance(shipment_id, str):
            raise ValueError
        return created_at, shipment_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from error


def sign_webhook_payload(*, secret: str, timestamp: str, payload: bytes) -> str:
    signed = timestamp.encode("utf-8") + b"." + payload
    return "v1=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def validate_webhook_url(value: str, *, allow_insecure: bool) -> str:
    """Perform a fast SSRF guard before an outbound webhook is registered."""
    parsed = urlsplit(value)
    if parsed.scheme not in ({"http", "https"} if allow_insecure else {"https"}):
        raise HTTPException(status_code=422, detail="Webhook URLs must use HTTPS.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Webhook URL is malformed.")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=422, detail="Local webhook targets are not allowed.")
    try:
        address = ipaddress.ip_address(hostname)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise HTTPException(status_code=422, detail="Private webhook targets are not allowed.")
    except ValueError:
        # Hostname resolution is intentionally left to the delivery layer. In a multi-tenant
        # deployment, pair this check with egress filtering to close DNS rebinding paths.
        pass
    return value
