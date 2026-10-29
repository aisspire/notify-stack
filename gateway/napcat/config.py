import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WatchSettings:
    napcat_token: str = field(repr=False)
    gateway_token: str = field(repr=False)
    link_secret: str = field(repr=False)
    napcat_url: str = "http://napcat:6099"
    gateway_url: str = "http://gateway:8090"
    public_url: str = "https://gotify.example.com"
    state_dir: str = "/app/state"
    poll_seconds: float = 15
    offline_seconds: float = 60
    recovery_seconds: float = 30
    reminder_seconds: float = 1800
    link_ttl_seconds: int = 3600
    refresh_cooldown_seconds: float = 30
    retry_seconds: float = 60

    def __post_init__(self):
        for value in (self.napcat_token, self.gateway_token, self.link_secret):
            if not value or value.lower().startswith("replace_"):
                raise ValueError("NapCat watcher credentials must be configured")
        if len(self.gateway_token) < 32 or len(self.link_secret) < 32:
            raise ValueError("Gateway token and QR link secret must be at least 32 characters")
        if self.link_secret in {self.gateway_token, self.napcat_token}:
            raise ValueError("QR link secret must be separate from API credentials")
        for value in (self.napcat_url, self.gateway_url, self.public_url):
            try:
                u = urlsplit(value)
                valid = u.scheme in {"http", "https"} and u.hostname and not u.username and not u.password
                valid = valid and not u.query and not u.fragment and not any(c.isspace() for c in value)
                _ = u.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Watcher service URLs must not include credentials or queries")
        u = urlsplit(self.public_url)
        if u.scheme != "https" or u.path not in {"", "/"}:
            raise ValueError("Public QR URL must be an HTTPS origin")
        if not 1 <= self.poll_seconds <= 300 or not 0 <= self.offline_seconds <= 600:
            raise ValueError("Invalid probe interval or offline threshold")
        if not 0 <= self.recovery_seconds <= 600 or not 60 <= self.reminder_seconds <= 86400:
            raise ValueError("Invalid recovery threshold or reminder interval")
        if not 60 <= self.link_ttl_seconds <= 86400 or self.refresh_cooldown_seconds < 10 or self.retry_seconds < 1:
            raise ValueError("Invalid link expiry or retry / refresh interval")

    @classmethod
    def from_env(cls):
        token_file = os.getenv("NAPCAT_TOKEN_FILE", "/run/secrets/napcat-webui-token")
        return cls(
            napcat_token=Path(token_file).read_text().strip(),
            gateway_token=os.environ["GATEWAY_API_TOKEN"],
            link_secret=os.environ["NAPCAT_LINK_SECRET"],
            napcat_url=os.getenv("NAPCAT_URL", "http://napcat:6099").rstrip("/"),
            gateway_url=os.getenv("GATEWAY_URL", "http://gateway:8090").rstrip("/"),
            public_url=os.getenv("NAPCAT_PUBLIC_URL", "https://gotify.example.com").rstrip("/"),
            state_dir=os.getenv("NAPCAT_STATE_DIR", "/app/state"),
            poll_seconds=float(os.getenv("NAPCAT_POLL_SECONDS", "15")),
            offline_seconds=float(os.getenv("NAPCAT_OFFLINE_SECONDS", "60")),
            recovery_seconds=float(os.getenv("NAPCAT_RECOVERY_SECONDS", "30")),
            reminder_seconds=float(os.getenv("NAPCAT_REMINDER_SECONDS", "1800")),
            link_ttl_seconds=int(os.getenv("NAPCAT_LINK_TTL_SECONDS", "3600")),
        )
