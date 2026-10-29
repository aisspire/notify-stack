import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class State:
    confirmed: str = "unknown"
    incident_id: str | None = None
    last_notice_at: float | None = None
    pending_recovery: bool = False


class MonitorState:
    def __init__(self, path: Path, settings):
        self.path = path
        self.settings = settings
        self.state = State()
        if path.exists():
            data = json.loads(path.read_text())
            self.state = State(**data)
            if self.state.confirmed not in {"unknown", "online", "offline", "unreachable"}:
                raise ValueError("Invalid persisted monitor state")
        self.candidate = None
        self.candidate_since = 0.0

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state)))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def observe(self, sample: str, now: float):
        group = "online" if sample == "online" else "down"
        if group != self.candidate:
            self.candidate = group
            self.candidate_since = now
        delay = self.settings.recovery_seconds if sample == "online" else self.settings.offline_seconds
        if self.state.confirmed == "unknown" and sample == "online":
            delay = 0
        if now - self.candidate_since < delay:
            return None
        if sample != self.state.confirmed:
            was_down = self.state.confirmed in {"offline", "unreachable"}
            self.state.confirmed = sample
            if sample == "online":
                self.state.incident_id = None  # Immediately revoke every old capability link.
                self.state.last_notice_at = None
                self.state.pending_recovery = was_down
            else:
                if not self.state.incident_id:
                    self.state.incident_id = secrets.token_urlsafe(16)
                    self.state.last_notice_at = None
                self.state.pending_recovery = False
            self.save()
        if sample == "online":
            return "recovery" if self.state.pending_recovery else None
        last = self.state.last_notice_at
        return "offline" if last is None or now - last >= self.settings.reminder_seconds else None

    def sent(self, action, now):
        if action == "recovery":
            self.state.pending_recovery = False
        else:
            self.state.last_notice_at = now
        self.save()


class Links:
    def __init__(self, secret: str, ttl: int):
        self.secret = secret.encode()
        self.ttl = ttl

    def issue(self, incident: str, now: float):
        value = f"{int(now) + self.ttl}.{incident}"
        return value + "." + hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def valid(self, token: str, incident: str | None, now: float):
        if not incident or len(token) > 160 or not re.fullmatch(r"[0-9]+\.[A-Za-z0-9_-]+\.[a-f0-9]{64}", token):
            return False
        expires, event, signature = token.split(".")
        if int(expires) <= now or event != incident:
            return False
        expected = hmac.new(self.secret, f"{expires}.{event}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
