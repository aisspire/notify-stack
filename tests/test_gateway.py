import asyncio
import json
import logging
import unittest

import httpx

from gateway.config import Settings
from gateway.main import create_app

logging.getLogger("notify.gateway").setLevel(logging.CRITICAL)
API_TOKEN = "a" * 48
GOTIFY_TOKEN = "gotify-application-test-token"
SETTINGS = Settings(api_token=API_TOKEN, gotify_token=GOTIFY_TOKEN)
PAYLOAD = {"source": "nas-backup", "title": "备份失败", "message": "磁盘空间不足"}
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.reply = httpx.Response(200, json={"id": 42})

        async def upstream(request):
            self.calls.append(request)
            if isinstance(self.reply, Exception):
                raise self.reply
            if callable(self.reply):
                return await self.reply(request)
            return self.reply

        self.app = create_app(SETTINGS, transport=httpx.MockTransport(upstream))
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gateway")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def notify(self, payload=None, **kwargs):
        return await self.client.post("/v1/notify", json=PAYLOAD if payload is None else payload,
                                      headers=AUTH, **kwargs)

    async def test_acceptance_priority_and_no_secret_exposure(self):
        for severity, expected in [("info", 2), ("warning", 5), ("critical", 9)]:
            with self.subTest(severity=severity):
                response = await self.notify({**PAYLOAD, "severity": severity, "tags": ["nas"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["message_id"], 42)
                self.assertEqual(response.json()["status"], "accepted")
                self.assertFalse(response.json()["dedupe_applied"])
                self.assertEqual(response.headers["x-request-id"], response.json()["request_id"])
                req = self.calls[-1]
                self.assertEqual(req.url.path, "/message")
                self.assertFalse(req.url.query)
                self.assertEqual(req.headers["x-gotify-key"], GOTIFY_TOKEN)
                self.assertNotIn("authorization", req.headers)
                body = json.loads(req.content)
                self.assertEqual(body["priority"], expected)
                self.assertEqual(body["title"], "[nas-backup] 备份失败")
                self.assertEqual(body["extras"]["client::display"]["contentType"], "text/plain")
                self.assertNotIn(GOTIFY_TOKEN, response.text)

    async def test_image_and_action_mapping_is_explicit(self):
        image = "https://gotify.example.com/napcat/login/capability/qr/test.png"
        action = "https://gotify.example.com/napcat/login/capability"
        result = await self.notify({**PAYLOAD, "message": "![untrusted](https://other.example/x)",
                                    "image_url": image, "action_url": action})
        self.assertEqual(result.status_code, 200)
        data = json.loads(self.calls[-1].content)
        self.assertEqual(data["extras"]["client::notification"], {"bigImageUrl": image, "click": {"url": action}})
        self.assertEqual(data["extras"]["client::display"]["contentType"], "text/markdown")
        self.assertIn("![图片](" + image + ")", data["message"])
        self.assertIn("[打开操作页面](" + action + ")", data["message"])
        self.assertTrue(data["message"].startswith("[打开操作页面]("))
        self.assertNotIn("![untrusted](", data["message"])

    async def test_action_link_is_also_visible_when_no_image_is_available(self):
        action = "https://gotify.example.com/napcat/login/capability"
        result = await self.notify({**PAYLOAD, "action_url": action})
        self.assertEqual(result.status_code, 200)
        data = json.loads(self.calls[-1].content)
        self.assertEqual(data["extras"]["client::display"]["contentType"], "text/markdown")
        self.assertIn("[打开操作页面](" + action + ")", data["message"])

    async def test_attachment_url_validation(self):
        for url in ["http://example.com/x", "https://user:password@example.com/x", "https://example.com/#token", "https://example.com:99999", "https://bad host/x"]:
            response = await self.notify({**PAYLOAD, "image_url": url})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, [])

    async def test_missing_wrong_and_duplicate_auth(self):
        for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"},
                        [("Authorization", f"Bearer {API_TOKEN}"), ("Authorization", "Bearer wrong")]]:
            response = await self.client.post("/v1/notify", json=PAYLOAD, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.calls, [])

    async def test_auth_precedes_json_validation(self):
        response = await self.client.post("/v1/notify", content=b"not json")
        self.assertEqual(response.status_code, 401)

    async def test_parameter_validation_and_redaction(self):
        for update in [{"source": "bad source"}, {"title": " "}, {"message": 12},
                       {"severity": "emergency"}, {"tags": ["tag"] * 17},
                       {"tags": ["t" * 33]}, {"title": "t" * 257},
                       {"message": "m" * 16385}, {"unexpected": GOTIFY_TOKEN},
                       {GOTIFY_TOKEN: "unexpected field"}]:
            response = await self.notify({**PAYLOAD, **update})
            self.assertEqual(response.status_code, 422, update)
            self.assertNotIn(GOTIFY_TOKEN, response.text)
            self.assertNotIn('"input"', response.text)
        self.assertEqual(self.calls, [])

    async def test_invalid_json_and_content_type(self):
        response = await self.client.post("/v1/notify", content="{broken", headers={**AUTH, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        response = await self.client.post("/v1/notify", content="{}", headers=AUTH)
        self.assertEqual(response.status_code, 415)

    async def test_compression_rejected(self):
        response = await self.client.post("/v1/notify", json=PAYLOAD, headers={**AUTH, "Content-Encoding": "gzip"})
        self.assertEqual(response.status_code, 415)

    async def test_body_limit_declared_and_chunked(self):
        response = await self.client.post("/v1/notify", content=b"x" * 65537,
                                         headers={**AUTH, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

        async def stream():
            yield b"x" * 40000
            yield b"x" * 40000

        response = await self.client.post("/v1/notify", content=stream(),
                                         headers={**AUTH, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])

    async def test_downstream_timeouts_and_network_errors(self):
        for exc, status, code in [
            (httpx.ReadTimeout("secret " + GOTIFY_TOKEN), 504, "channel_timeout"),
            (httpx.ConnectError("secret " + GOTIFY_TOKEN), 503, "channel_unavailable"),
            (httpx.PoolTimeout("busy"), 503, "channel_busy"),
        ]:
            self.reply = exc
            response = await self.notify()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn(GOTIFY_TOKEN, response.text)

    async def test_downstream_status_codes(self):
        for upstream, expected in [(401, 502), (403, 502), (429, 503), (500, 503),
                                   (503, 503), (400, 502), (302, 502)]:
            self.reply = httpx.Response(upstream, text="private " + GOTIFY_TOKEN)
            response = await self.notify()
            self.assertEqual(response.status_code, expected, upstream)
            self.assertNotIn(GOTIFY_TOKEN, response.text)

    async def test_bad_receipt_is_not_success(self):
        for data in [{}, {"id": True}, {"id": "42"}, {"id": -1}, []]:
            self.reply = httpx.Response(200, json=data)
            response = await self.notify()
            self.assertEqual(response.status_code, 502)
        self.reply = httpx.Response(200, text="not json")
        self.assertEqual((await self.notify()).status_code, 502)

    async def test_no_automatic_retries_or_deduplication(self):
        data = {**PAYLOAD, "dedupe_key": "same-event"}
        await self.notify(data)
        await self.notify(data)
        self.assertEqual(len(self.calls), 2)
        self.reply = httpx.ReadTimeout("unknown")
        await self.notify(data)
        self.assertEqual(len(self.calls), 3)

    async def test_health_is_local_and_ready_is_authenticated(self):
        self.reply = httpx.ConnectError("offline")
        self.assertEqual((await self.client.get("/health")).status_code, 200)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual((await self.client.get("/ready")).status_code, 401)
        self.assertEqual((await self.client.get("/ready", headers=AUTH)).status_code, 503)
        self.reply = httpx.Response(200, json={"health": "green", "database": "green"})
        self.assertEqual((await self.client.get("/ready", headers=AUTH)).status_code, 200)
        self.assertNotIn("x-gotify-key", self.calls[-1].headers)

    async def test_ready_rejects_html_or_unhealthy_database(self):
        for response in [httpx.Response(200, text="<html>Login</html>"),
                         httpx.Response(200, json={"health": "green", "database": "red"}),
                         httpx.Response(200, json=[]), httpx.Response(302)]:
            self.reply = response
            result = await self.client.get("/ready", headers=AUTH)
            self.assertEqual(result.status_code, 503)

    async def test_internal_error_is_sanitized(self):
        self.reply = RuntimeError("secret " + GOTIFY_TOKEN)
        with self.assertLogs("notify.gateway", level="ERROR") as logs:
            response = await self.notify()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn(GOTIFY_TOKEN, response.text + "\n".join(logs.output))

    async def test_logs_do_not_include_secrets_or_message_content(self):
        self.reply = httpx.ReadTimeout("private " + GOTIFY_TOKEN)
        with self.assertLogs("notify.gateway", level="INFO") as logs:
            await self.notify({**PAYLOAD, "message": "PRIVATE_MESSAGE"})
        text = "\n".join(logs.output)
        for secret in [API_TOKEN, GOTIFY_TOKEN, "PRIVATE_MESSAGE"]:
            self.assertNotIn(secret, text)

    async def test_total_deadline(self):
        async def slow(request):
            await asyncio.sleep(0.3)
            return httpx.Response(200, json={"id": 1})

        config = Settings(api_token=API_TOKEN, gotify_token=GOTIFY_TOKEN, timeout_seconds=0.1)
        app = create_app(config, transport=httpx.MockTransport(slow))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                response = await client.post("/v1/notify", json=PAYLOAD, headers=AUTH)
                self.assertEqual(response.status_code, 504)


class SettingsTests(unittest.TestCase):
    def test_reject_placeholder_missing_or_shared_tokens(self):
        for token in ["", "short", "replace_" + "a" * 40, "a" * 32 + "\n"]:
            with self.assertRaises(ValueError):
                Settings(api_token=token, gotify_token=GOTIFY_TOKEN)
        with self.assertRaises(ValueError):
            Settings(api_token=API_TOKEN, gotify_token=API_TOKEN)

    def test_reject_unsafe_urls_and_invalid_limits(self):
        for url in ["file:///etc/passwd", "http://user:secret@gotify", "http://gotify?token=x", "http://gotify:99999"]:
            with self.assertRaises(ValueError):
                Settings(api_token=API_TOKEN, gotify_token=GOTIFY_TOKEN, gotify_url=url)
        for timeout in [0, 61, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                Settings(api_token=API_TOKEN, gotify_token=GOTIFY_TOKEN, timeout_seconds=timeout)

    def test_settings_repr_redacts_tokens(self):
        for token in [API_TOKEN, GOTIFY_TOKEN]:
            self.assertNotIn(token, repr(SETTINGS))


if __name__ == "__main__":
    unittest.main()
