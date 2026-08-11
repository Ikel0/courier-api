"""Runtime configuration kept deliberately small and explicit."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/courier.db"
    api_keys: str = "local:local_demo_key_change_me"
    allow_insecure_webhooks: bool = False
    webhook_timeout_seconds: float = 4.0
    rate_limit_per_minute: int = 120
    enable_demo_keys: bool = True
    seed_demo_data: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("COURIER_DATABASE_URL", cls.database_url),
            api_keys=os.getenv("COURIER_API_KEYS", cls.api_keys),
            allow_insecure_webhooks=_as_bool(
                os.getenv("COURIER_ALLOW_INSECURE_WEBHOOKS", "false")
            ),
            webhook_timeout_seconds=float(
                os.getenv("COURIER_WEBHOOK_TIMEOUT_SECONDS", "4")
            ),
            rate_limit_per_minute=int(
                os.getenv("COURIER_RATE_LIMIT_PER_MINUTE", "120")
            ),
            enable_demo_keys=_as_bool(os.getenv("COURIER_ENABLE_DEMO_KEYS", "true")),
            seed_demo_data=_as_bool(os.getenv("COURIER_SEED_DEMO_DATA", "false")),
        )

    @property
    def database_path(self) -> str:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Courier API currently supports sqlite:/// database URLs only.")
        raw_path = self.database_url.removeprefix(prefix)
        path = Path(raw_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    @property
    def parsed_api_keys(self) -> dict[str, str]:
        """Parse `key_id:secret,key_id:secret` without leaking secrets in errors.

        A raw single secret is also accepted as ``render:<secret>``. That keeps Render's
        ``generateValue`` convention safe while retaining labelled local keys.
        """
        parsed: dict[str, str] = {}
        pairs = [pair.strip() for pair in self.api_keys.split(",") if pair.strip()]
        if len(pairs) == 1 and ":" not in pairs[0]:
            return {"render": pairs[0]}
        for pair in pairs:
            key_id, separator, secret = pair.strip().partition(":")
            if not separator or not key_id or not secret:
                raise ValueError("COURIER_API_KEYS must contain key_id:secret pairs.")
            parsed[key_id] = secret
        if not parsed:
            raise ValueError("At least one Courier API key must be configured.")
        return parsed
