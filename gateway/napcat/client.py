import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class NapCatError(Exception):
    """Only safe error codes; never retain login credentials or QR tickets."""


@dataclass(frozen=True)
class Snapshot:
    online: bool
    phase: str = ""
    qr_url: str = field(default="", repr=False)


class NapCatClient:
    def __init__(self, client: httpx.AsyncClient, url: str, token: str):
        self.client = client
        self.url = url.rstrip("/")
        self.token = token
        self.credential = None
        self.auth_lock = asyncio.Lock()
        self.auth_blocked_until = 0.0

    async def _auth(self):
        async with self.auth_lock:
            if self.credential:
                return
            if time.monotonic() < self.auth_blocked_until:
                raise NapCatError("auth_backoff")
            try:
                digest = hashlib.sha256((self.token + ".napcat").encode()).hexdigest()
                response = await self.client.post(self.url + "/api/auth/login", json={"hash": digest})
                data = response.json()
                body = data.get("data") or {}
                if response.status_code != 200 or data.get("code") != 0 or not isinstance(body.get("Credential"), str) or not body["Credential"] or body.get("require2FA"):
                    raise NapCatError("auth_failed")
                self.credential = body["Credential"]
            except (httpx.RequestError, ValueError, AttributeError, NapCatError):
                self.auth_blocked_until = time.monotonic() + 60
                raise NapCatError("auth_failed") from None

    async def post(self, path):
        for attempt in range(2):
            await self._auth()
            try:
                response = await self.client.post(
                    self.url + "/api/QQLogin/" + path,
                    json={}, headers={"Authorization": "Bearer " + self.credential},
                )
                data = response.json()
                unauthorized = response.status_code in {401, 403} or data.get("message") in {"Unauthorized", "token expired"}
                if unauthorized and attempt == 0:
                    self.credential = None
                    continue
                if response.status_code != 200 or data.get("code") != 0 or not isinstance(data.get("data"), dict):
                    raise NapCatError("api_failed")
                return data["data"]
            except (httpx.RequestError, ValueError, AttributeError):
                raise NapCatError("api_failed") from None
        raise NapCatError("auth_failed")

    async def status(self):
        d = await self.post("CheckLoginStatus")
        online = d.get("isLogin") is True and d.get("coreReady", True) is True and d.get("isOffline") is not True
        phase = d.get("loginPhase", "")
        if not isinstance(phase, str):
            phase = ""
        qr = d.get("qrcodeurl", "")
        if not isinstance(qr, str) or len(qr) > 4096:
            qr = ""
        if qr:
            try:
                u = urlsplit(qr)
                if u.scheme not in {"https", "http"} or not u.hostname:
                    qr = ""
            except ValueError:
                qr = ""
        return Snapshot(online, phase, "" if online or phase in {"initializing", "qrcode_scanned", "reconnecting"} else qr)

    async def refresh(self, old: Snapshot):
        if old.online or old.phase not in {"waiting_qrcode", "offline", ""}:
            raise NapCatError("login_in_progress")
        await self.post("RefreshQRcode")
        # The refresh API acknowledges a request; the new QR arrives asynchronously.
        for _ in range(8):
            await asyncio.sleep(0.5)
            snapshot = await self.status()
            if snapshot.online:
                raise NapCatError("already_online")
            if snapshot.qr_url and snapshot.qr_url != old.qr_url:
                return snapshot
        raise NapCatError("qr_not_refreshed")
