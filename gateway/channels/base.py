from dataclasses import dataclass
from typing import Protocol

from gateway.schemas import Notification


@dataclass(frozen=True)
class Receipt:
    message_id: int


class DeliveryError(Exception):
    """A safe public error: never retain URLs, tokens, or upstream response bodies."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(code)


class Channel(Protocol):
    async def send(self, notification: Notification, *, priority: int) -> Receipt: ...

    async def ready(self) -> bool: ...
