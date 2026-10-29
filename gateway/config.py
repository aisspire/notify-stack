"""Environment-only configuration. Never include secrets in repr or errors."""

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    gotify_token: str = field(repr=False)
    gotify_url: str = "http://gotify:80"
    timeout_seconds: float = 5.0
    max_body_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("GATEWAY_API_TOKEN", self.api_token, 32),
            ("GOTIFY_APP_TOKEN", self.gotify_token, 1),
        ):
            if (
                len(value) < minimum
                or not value.isascii()
                or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value)
                or value.lower().startswith(("replace_", "changeme", "your_"))
            ):
                raise ValueError(f"{name} must contain a real, non-placeholder token")
        if self.api_token == self.gotify_token:
            raise ValueError("Gateway and Gotify tokens must be different")
        try:
            url = urlsplit(self.gotify_url)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(c.isspace() for c in self.gotify_url)
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("GOTIFY_URL must be an HTTP(S) base URL without credentials or query")
        if not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 60:
            raise ValueError("GOTIFY_TIMEOUT_SECONDS must be between 0.1 and 60")
        if not 1024 <= self.max_body_bytes <= 1_048_576:
            raise ValueError("MAX_REQUEST_BYTES must be between 1024 and 1048576")

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            timeout = float(os.getenv("GOTIFY_TIMEOUT_SECONDS", "5"))
            limit = int(os.getenv("MAX_REQUEST_BYTES", "65536"))
        except ValueError:
            raise ValueError("Timeout and request size configuration must be numeric") from None
        return cls(
            api_token=os.getenv("GATEWAY_API_TOKEN", ""),
            gotify_token=os.getenv("GOTIFY_APP_TOKEN", ""),
            gotify_url=os.getenv("GOTIFY_URL", "http://gotify:80").rstrip("/"),
            timeout_seconds=timeout,
            max_body_bytes=limit,
        )
