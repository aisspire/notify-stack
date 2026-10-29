"""Authentication, bounded request buffering and safe correlation logging."""

import hmac
import logging
import time
import uuid

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

logger = logging.getLogger("notify.gateway")


def error_body(code: str, message: str, request_id: str, **extra) -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, **extra},
        "request_id": request_id,
    }


class RequestGuard:
    def __init__(self, app, state):
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500
        response_started = False

        async def safe_send(event):
            nonlocal status, response_started
            if event["type"] == "http.response.start":
                status = event["status"]
                response_started = True
                event.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(event)

        async def reject(status_code, code, message, headers=None):
            response = JSONResponse(error_body(code, message, request_id), status_code, headers=headers)
            await response(scope, receive, safe_send)

        try:
            headers = Headers(scope=scope)
            protected = scope["path"].rstrip("/") in {"/v1/notify", "/ready"}
            if protected:
                auth = headers.getlist("authorization")
                scheme, _, provided = auth[0].partition(" ") if len(auth) == 1 else ("", "", "")
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    provided.encode(), self.state.settings.api_token.encode()
                ):
                    return await reject(
                        401, "unauthorized", "A valid Bearer token is required", {"WWW-Authenticate": "Bearer"}
                    )

            if scope["path"].rstrip("/") == "/v1/notify" and scope["method"] == "POST":
                if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return await reject(415, "unsupported_media_type", "Content-Type must be application/json")
                if headers.get("content-encoding", "identity").lower() != "identity":
                    return await reject(415, "unsupported_content_encoding", "Compressed request bodies are not supported")
                limit = self.state.settings.max_body_bytes
                try:
                    content_length = int(headers.get("content-length", "0"))
                    if content_length < 0:
                        raise ValueError
                except ValueError:
                    return await reject(400, "invalid_content_length", "Invalid Content-Length")
                if content_length > limit:
                    return await reject(413, "request_too_large", "Request body is too large")
                chunks = []
                length = 0
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    length += len(chunk)
                    if length > limit:
                        return await reject(413, "request_too_large", "Request body is too large")
                    chunks.append(chunk)
                    if not event.get("more_body", False):
                        break
                body = b"".join(chunks)
                replayed = False
                original_receive = receive

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()

                receive = replay
            await self.app(scope, receive, safe_send)
        except Exception:
            # No traceback / exception text: third-party failures can carry request credentials.
            logger.error("unexpected_error request_id=%s", request_id)
            if not response_started:
                await reject(500, "internal_error", "Internal server error")
        finally:
            logger.info(
                "request_complete request_id=%s status=%d duration_ms=%.1f",
                request_id, status, (time.monotonic() - started) * 1000,
            )
