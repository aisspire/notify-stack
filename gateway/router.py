"""V1 routing: one adapter, one receipt, no queue or implicit retries."""

from gateway.channels.base import Channel, Receipt
from gateway.schemas import Notification

PRIORITY = {"info": 2, "warning": 5, "critical": 9}


class NotificationRouter:
    def __init__(self, channel: Channel):
        self.channel = channel

    async def send(self, notification: Notification) -> Receipt:
        return await self.channel.send(notification, priority=PRIORITY[notification.severity])
