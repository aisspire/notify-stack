import json
import logging
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import png

from gateway.napcat.client import NapCatClient, NapCatError, Snapshot
from gateway.napcat.config import WatchSettings
from gateway.napcat.main import create_app
from gateway.napcat.service import Watcher, qr_hash
from gateway.napcat.state import Links, MonitorState

logging.getLogger("notify.napcat").setLevel(logging.CRITICAL)
TOKEN = "gateway-" + "a" * 40
SECRET = "qr-link-" + "b" * 40
QR1 = "https://txz.qq.com/p?k=first-test-ticket"
QR2 = "https://txz.qq.com/p?k=second-test-ticket"


def settings(directory, **extra):
    return WatchSettings(napcat_token="napcat-test-secret", gateway_token=TOKEN,
                         link_secret=SECRET, state_dir=str(directory), **extra)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = settings(self.tmp.name)
        self.path = Path(self.tmp.name) / "state.json"
        self.monitor = MonitorState(self.path, self.config)

    def test_cold_offline_and_transient_failures(self):
        self.assertIsNone(self.monitor.observe("offline", 100))
        self.assertIsNone(self.monitor.observe("offline", 159))
        self.assertEqual(self.monitor.observe("offline", 160), "offline")
        self.assertTrue(self.monitor.state.incident_id)
        self.monitor.sent("offline", 160)
        self.assertIsNone(self.monitor.observe("offline", 170))
        self.assertEqual(self.monitor.observe("offline", 1960), "offline")
        text = self.path.read_text()
        self.assertNotIn(QR1, text)
        self.assertNotIn(TOKEN, text)

    def test_recovery_is_debounced_and_revokes_old_links(self):
        self.monitor.observe("offline", 0)
        self.monitor.observe("offline", 60)
        old = self.monitor.state.incident_id
        self.monitor.sent("offline", 60)
        self.assertIsNone(self.monitor.observe("online", 70))
        self.assertIsNone(self.monitor.observe("online", 99))
        self.assertEqual(self.monitor.observe("online", 100), "recovery")
        self.assertIsNone(self.monitor.state.incident_id)
        self.monitor.sent("recovery", 100)
        self.assertIsNone(self.monitor.observe("online", 110))
        self.monitor.observe("offline", 120)
        self.monitor.observe("offline", 180)
        self.assertNotEqual(self.monitor.state.incident_id, old)

    def test_alternating_down_reasons_do_not_reset_offline_timer(self):
        self.monitor.observe("unreachable", 100)
        self.monitor.observe("offline", 130)
        self.assertEqual(self.monitor.observe("unreachable", 160), "offline")

    def test_initial_online_is_a_baseline_not_a_recovery(self):
        self.assertIsNone(self.monitor.observe("online", 100))
        self.assertEqual(self.monitor.state.confirmed, "online")
        self.monitor.observe("offline", 110)
        self.assertIsNone(self.monitor.observe("online", 120))
        self.assertIsNone(self.monitor.state.incident_id)

    def test_restart_preserves_incident_and_notification_cooldown(self):
        self.monitor.observe("offline", 100)
        self.monitor.observe("offline", 160)
        self.monitor.sent("offline", 160)
        restored = MonitorState(self.path, self.config)
        self.assertEqual(restored.state.incident_id, self.monitor.state.incident_id)
        restored.observe("offline", 200)
        self.assertIsNone(restored.observe("offline", 260))

    def test_failed_delivery_is_not_marked_successful(self):
        self.monitor.observe("offline", 100)
        self.assertEqual(self.monitor.observe("offline", 160), "offline")
        self.assertEqual(self.monitor.observe("offline", 175), "offline")
        self.assertIsNone(self.monitor.state.last_notice_at)

    def test_capabilities_expire_reject_tampering_and_bind_incident(self):
        links = Links(SECRET, 60)
        token = links.issue("incident-one", 100)
        self.assertTrue(links.valid(token, "incident-one", 159))
        self.assertFalse(links.valid(token, "incident-one", 160))
        self.assertFalse(links.valid(token, "incident-two", 100))
        self.assertFalse(links.valid(token, None, 100))
        self.assertFalse(links.valid(token[:-1] + ("0" if token[-1] != "0" else "1"), "incident-one", 100))
        self.assertFalse(links.valid("malformed", "incident-one", 100))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_is_cached_and_authoritative_status_wins(self):
        calls = []

        async def upstream(req):
            calls.append(req.url.path)
            if req.url.path.endswith("auth/login"):
                return httpx.Response(200, json={"code": 0, "data": {"Credential": "session"}})
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "coreReady": False, "loginPhase": "initializing", "qrcodeurl": QR1}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            napcat = NapCatClient(client, "http://napcat", "secret")
            self.assertFalse((await napcat.status()).online)
            self.assertFalse((await napcat.status()).online)
        self.assertEqual(calls.count("/api/auth/login"), 1)

    async def test_unauthorized_session_is_reauthenticated_once(self):
        attempts = 0

        async def upstream(req):
            nonlocal attempts
            if req.url.path.endswith("auth/login"):
                return httpx.Response(200, json={"code": 0, "data": {"Credential": "new-session"}})
            attempts += 1
            if attempts == 1:
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "coreReady": True}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            napcat = NapCatClient(client, "http://napcat", "secret")
            napcat.credential = "expired"
            self.assertTrue((await napcat.status()).online)
            self.assertEqual(attempts, 2)

    async def test_auth_failure_backs_off_instead_of_hammering_login(self):
        count = 0

        async def upstream(req):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"code": -1, "message": "secret error"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            napcat = NapCatClient(client, "http://napcat", "secret")
            for _ in range(2):
                with self.assertRaises(NapCatError):
                    await napcat.status()
            self.assertEqual(count, 1)

    async def test_refresh_waits_for_a_changed_qr_and_never_touches_online_account(self):
        async with httpx.AsyncClient() as client:
            napcat = NapCatClient(client, "http://napcat", "secret")
            from unittest.mock import AsyncMock
            napcat.post = AsyncMock(return_value={})
            napcat.status = AsyncMock(side_effect=[Snapshot(False, "waiting_qrcode", QR1), Snapshot(False, "waiting_qrcode", QR2)])
            with patch("gateway.napcat.client.asyncio.sleep", new=AsyncMock()):
                result = await napcat.refresh(Snapshot(False, "waiting_qrcode", QR1))
            self.assertEqual(result.qr_url, QR2)
            before = napcat.post.call_count
            for snapshot in [Snapshot(True), Snapshot(False, "initializing"), Snapshot(False, "qrcode_scanned")]:
                with self.assertRaises(NapCatError):
                    await napcat.refresh(snapshot)
            self.assertEqual(napcat.post.call_count, before)


class FakeNapcat:
    def __init__(self):
        self.snapshot = Snapshot(False, "waiting_qrcode", QR1)
        self.refresh_count = 0

    async def status(self):
        return self.snapshot

    async def refresh(self, old):
        self.refresh_count += 1
        self.snapshot = Snapshot(False, "waiting_qrcode", QR2)
        return self.snapshot


class WebAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = settings(self.tmp.name, offline_seconds=0, recovery_seconds=0)
        self.payloads = []
        self.fail_delivery = False

        async def gateway(req):
            self.payloads.append(json.loads(req.content))
            self.assertEqual(req.headers["authorization"], "Bearer " + TOKEN)
            return httpx.Response(503 if self.fail_delivery else 200, json={"success": not self.fail_delivery, "message_id": 123})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(gateway))
        self.fake = FakeNapcat()
        self.watcher = Watcher(self.config, self.upstream, self.fake)
        self.watcher.monitor.observe("offline", time.time())
        self.token = self.watcher.links.issue(self.watcher.monitor.state.incident_id, time.time())
        self.base = "/napcat/login/" + self.token
        self.app = create_app(self.config, watcher=self.watcher, start_worker=False)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.config.public_url)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.upstream.aclose()
        self.tmp.cleanup()

    async def test_login_page_png_and_cache_headers(self):
        page = await self.client.get(self.base)
        self.assertEqual(page.status_code, 200)
        self.assertIn("扫码登录", page.text)
        for secret in [QR1, self.config.napcat_token, TOKEN, SECRET]:
            self.assertNotIn(secret, page.text)
        image = await self.client.get(self.base + "/qr/" + qr_hash(QR1) + ".png")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertEqual(image.content[:8], b"\x89PNG\r\n\x1a\n")
        width, height, rows, meta = png.Reader(bytes=image.content).read()
        self.assertEqual(width, height)
        self.assertGreater(width, 100)
        self.assertIn("no-store", image.headers["cache-control"])
        self.assertEqual(page.headers["referrer-policy"], "strict-origin")
        self.assertIsNotNone(self.watcher.png_cache)

    async def test_invalid_expired_and_recovered_links(self):
        self.assertEqual((await self.client.get("/napcat/login/invalid")).status_code, 410)
        expired = self.watcher.links.issue(self.watcher.monitor.state.incident_id, time.time() - 7200)
        self.assertEqual((await self.client.get("/napcat/login/" + expired)).status_code, 410)
        self.watcher.monitor.observe("online", time.time())
        self.assertEqual((await self.client.get(self.base)).status_code, 410)

    async def test_online_state_blocks_images_and_refresh_before_debounce(self):
        self.fake.snapshot = Snapshot(True, "ready")
        self.assertEqual((await self.client.get(self.base + "/qr/" + qr_hash(QR1) + ".png")).status_code, 410)
        self.assertEqual((await self.client.post(self.base + "/refresh")).status_code, 409)
        self.assertEqual(self.fake.refresh_count, 0)

    async def test_refresh_changes_qr_invalidates_old_image_and_is_rate_limited(self):
        response = await self.client.post(self.base + "/refresh")
        self.assertEqual(response.status_code, 303)
        self.assertEqual((await self.client.get(self.base + "/qr/" + qr_hash(QR1) + ".png")).status_code, 410)
        self.assertEqual((await self.client.post(self.base + "/refresh")).status_code, 429)
        self.assertEqual((await self.client.post(self.base + "/refresh", headers={"Origin": "https://evil.example"})).status_code, 403)

    async def test_browser_form_refresh_preserves_origin_without_disclosing_signed_path(self):
        page = await self.client.get(self.base)
        # no-referrer makes a browser navigation POST send Origin: null. strict-origin
        # preserves the HTTPS origin while stripping the signed path from Referer.
        self.assertEqual(page.headers["referrer-policy"], "strict-origin")
        response = await self.client.post(
            self.base + "/refresh", content=b"",
            headers={"Origin": self.config.public_url,
                     "Referer": self.config.public_url + "/",
                     "Sec-Fetch-Site": "same-origin",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], self.base)
        self.assertEqual(self.fake.refresh_count, 1)
        self.assertEqual((await self.client.get(self.base)).status_code, 200)

    async def test_refresh_still_rejects_null_and_foreign_origins(self):
        for origin in ("null", "https://evil.example", "http://gotify.example.com",
                       "https://other.example.com"):
            with self.subTest(origin=origin):
                response = await self.client.post(self.base + "/refresh", headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)
                self.assertIn("请求来源无效", response.text)
                self.assertEqual(response.headers["referrer-policy"], "strict-origin")
        self.assertEqual(self.fake.refresh_count, 0)

    async def test_internal_status_requires_token_and_never_exposes_qr(self):
        self.assertEqual((await self.client.get("/internal/status")).status_code, 401)
        response = await self.client.get("/internal/status", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 200)
        for value in [QR1, TOKEN, SECRET, self.config.napcat_token]:
            self.assertNotIn(value, response.text)

    async def test_delivery_goes_through_gateway_and_contains_bounded_image_link(self):
        self.assertTrue(await self.watcher.deliver("offline"))
        data = self.payloads[-1]
        self.assertEqual(data["severity"], "critical")
        self.assertIn("/napcat/login/", data["action_url"])
        self.assertTrue(data["image_url"].endswith(qr_hash(QR2) + ".png"))
        self.assertNotIn(QR2, json.dumps(data))
        self.assertIsNotNone(self.watcher.monitor.state.last_notice_at)

    async def test_failed_delivery_is_retried_without_marking_success(self):
        self.fail_delivery = True
        await self.watcher.tick()
        self.assertIsNone(self.watcher.monitor.state.last_notice_at)
        self.assertGreater(self.watcher.retry_after, time.time())
        count = len(self.payloads)
        await self.watcher.tick()
        self.assertEqual(count, len(self.payloads))


if __name__ == "__main__":
    unittest.main()
