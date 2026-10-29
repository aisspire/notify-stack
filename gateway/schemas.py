from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Severity = Literal["info", "warning", "critical"]
Source = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"),
]
Title = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256)]
Message = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=16384)]
Tag = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=32)]
DedupeKey = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)]
AttachmentURL = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Source
    title: Title
    message: Message
    severity: Severity = "info"
    tags: list[Tag] = Field(default_factory=list, max_length=16)
    # Accepted as metadata only. V1 does not suppress duplicate messages.
    dedupe_key: DedupeKey | None = None
    image_url: AttachmentURL | None = None
    action_url: AttachmentURL | None = None

    @field_validator("image_url", "action_url")
    @classmethod
    def public_https_url(cls, value):
        if value is None:
            return value
        try:
            url = urlsplit(value)
            valid = url.scheme == "https" and url.hostname and not url.username and not url.password
            valid = valid and not any(c.isspace() for c in value) and not url.fragment
            if not valid:
                raise ValueError
            _ = url.port
        except ValueError:
            raise ValueError("Attachment URL must be HTTPS without credentials or fragments") from None
        return value


class NotifyResponse(BaseModel):
    success: Literal[True] = True
    message: str = "Notification accepted by Gotify"
    status: Literal["accepted"] = "accepted"
    channel: Literal["gotify"] = "gotify"
    message_id: int
    request_id: str
    dedupe_applied: Literal[False] = False
