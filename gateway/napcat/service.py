import asyncio
import hashlib
import logging
import time
from pathlib import Path

import httpx

from gateway.napcat.client import NapCatClient, NapCatError
from gateway.napcat.state import Links, MonitorState

logger = logging.getLogger("notify.napcat")


def qr_hash(value: str):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


class Watcher:
    def __init__(self, settings, client: httpx.AsyncClient, napcat=None):
        self.settings = settings
        self.client = client
        self.napcat = napcat or NapCatClient(client, settings.napcat_url, settings.napcat_token)
        self.monitor = MonitorState(Path(settings.state_dir) / "state.json", settings)
        self.links = Links(settings.link_secret, settings.link_ttl_seconds)
        self.snapshot = None
        self.last_probe_at = 0.0
        self.last_tick_at = time.time()
        self.last_refresh_at = None
        self.retry_after = 0.0
        self.refresh_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.png_cache = None
        self.png_cache_key = None
        self.task = None

    async def probe(self):
        snapshot = await self.napcat.status()
        self.snapshot = snapshot
        self.last_probe_at = time.time()
        if snapshot.online:
            self.png_cache = self.png_cache_key = None
        return snapshot

    def valid(self, token):
        return self.links.valid(token, self.monitor.state.incident_id, time.time())

    async def refresh(self):
        async with self.refresh_lock:
            now = time.time()
            if self.last_refresh_at is not None and now - self.last_refresh_at < self.settings.refresh_cooldown_seconds:
                raise NapCatError("refresh_cooldown")
            # Always recheck login state immediately before a mutating login operation.
            snapshot = await self.probe()
            if snapshot.online:
                raise NapCatError("already_online")
            self.last_refresh_at = now
            snapshot = await self.napcat.refresh(snapshot)
            self.snapshot = snapshot
            self.png_cache = self.png_cache_key = None
            return snapshot

    async def deliver(self, action, *, refresh=True):
        async with self.delivery_lock:
            state = self.monitor.state
            if action == "recovery":
                payload = {
                    "source": "napcat-qq", "title": "QQ 已恢复在线",
                    "message": "NapCat 已连续确认 QQ 在线，旧登录链接和二维码入口已失效。",
                    "severity": "info", "tags": ["napcat", "recovery"],
                }
            else:
                incident = state.incident_id
                if not incident:
                    raise NapCatError("no_active_incident")
                snapshot = None
                try:
                    snapshot = await self.probe()
                    if snapshot.online:
                        return False  # Recovered while preparing an alert: never send a stale offline claim.
                    if refresh and snapshot.phase in {"waiting_qrcode", "offline", ""}:
                        snapshot = await self.refresh()
                except NapCatError:
                    snapshot = None  # Text + login page still work when QR generation is unavailable.
                token = self.links.issue(incident, time.time())
                action_url = self.settings.public_url + "/napcat/login/" + token
                unavailable = state.confirmed == "unreachable"
                payload = {
                    "source": "napcat-qq",
                    "title": "NapCat 状态接口无法连接" if unavailable else "QQ 已离线或未登录",
                    "message": (
                        "无法读取 NapCat 登录状态，请检查服务与 WebUI 连接。"
                        if unavailable else "检测到 QQ 持续未登录或离线，消息收发可能不可用。"
                    ) + f"\n点击通知打开登录页；二维码过期时可点击刷新。链接有效期约 {max(1, self.settings.link_ttl_seconds // 60)} 分钟。",
                    "severity": "critical", "tags": ["napcat", "offline"],
                    "dedupe_key": "napcat-offline-" + incident,
                    "action_url": action_url,
                }
                if snapshot and snapshot.qr_url:
                    payload["image_url"] = action_url + "/qr/" + qr_hash(snapshot.qr_url) + ".png"
                else:
                    payload["message"] += "\n当前暂未取得新二维码，请稍后在登录页刷新。"
            try:
                response = await self.client.post(
                    self.settings.gateway_url + "/v1/notify", json=payload,
                    headers={"Authorization": "Bearer " + self.settings.gateway_token},
                )
                receipt = response.json()
                if response.status_code != 200 or receipt.get("success") is not True or type(receipt.get("message_id")) is not int:
                    raise NapCatError("delivery_failed")
            except (httpx.RequestError, ValueError, AttributeError):
                raise NapCatError("delivery_failed") from None
            self.monitor.sent(action, time.time())
            logger.info("notice_accepted action=%s message_id=%d", action, receipt["message_id"])
            return True

    async def tick(self):
        try:
            snapshot = await self.probe()
            sample = "online" if snapshot.online else "offline"
        except NapCatError:
            self.snapshot = None
            sample = "unreachable"
        action = self.monitor.observe(sample, time.time())
        if action and time.time() >= self.retry_after:
            try:
                await self.deliver(action)
                self.retry_after = 0.0
            except NapCatError:
                self.retry_after = time.time() + self.settings.retry_seconds
                logger.warning("notice_failed action=%s retry_seconds=%s", action, self.settings.retry_seconds)
        self.last_tick_at = time.time()

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # Do not log raw third-party exceptions, URLs or QR tickets.
                logger.error("monitor_cycle_failed")
            await asyncio.sleep(self.settings.poll_seconds)

    def status(self):
        s = self.monitor.state
        return {
            "confirmed": s.confirmed,
            "phase": self.snapshot.phase if self.snapshot else "unavailable",
            "observed_online": self.snapshot.online if self.snapshot else None,
            "incident_active": bool(s.incident_id),
            "last_probe_at": self.last_probe_at,
            "last_notice_at": s.last_notice_at,
            "poll_seconds": self.settings.poll_seconds,
            "offline_seconds": self.settings.offline_seconds,
            "reminder_seconds": self.settings.reminder_seconds,
        }
