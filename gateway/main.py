import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from gateway.channels.base import DeliveryError
from gateway.channels.gotify import GotifyChannel
from gateway.config import Settings
from gateway.middleware import RequestGuard, error_body
from gateway.router import NotificationRouter
from gateway.schemas import Notification, NotifyResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("notify.gateway")


def create_app(settings: Settings | None = None, *, transport=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_env()
        app.state.settings = config
        # Never follow redirects with application credentials. Ignore ambient proxy env vars.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds, pool=1.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            app.state.channel = GotifyChannel(client, config)
            app.state.router = NotificationRouter(app.state.channel)
            logger.info("gateway_started channel=gotify")
            yield

    app = FastAPI(title="Notification Gateway", version="1.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestGuard, state=app.state)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic defaults include the raw input. Do not echo message bodies or credentials.
        known_fields = {"body", *Notification.model_fields}
        details = [
            {"field": ".".join(str(x) if isinstance(x, int) or x in known_fields else "unknown"
                                for x in e["loc"]), "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(error_body("validation_error", "Invalid notification parameters",
                                       request.state.request_id, details=details), 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(error_body("http_error", "Request could not be processed",
                                       request.state.request_id), exc.status_code, headers=exc.headers)

    @app.exception_handler(DeliveryError)
    async def delivery_error(request: Request, exc: DeliveryError):
        logger.warning("delivery_failed request_id=%s code=%s", request.state.request_id, exc.code)
        return JSONResponse(error_body(exc.code, exc.message, request.state.request_id), exc.status)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "notification-gateway"}

    @app.get("/ready")
    async def ready(request: Request):
        if not await request.app.state.channel.ready():
            raise DeliveryError(503, "channel_unavailable", "Gotify health check failed")
        return {"status": "ready", "channel": "gotify"}

    @app.post("/v1/notify", response_model=NotifyResponse)
    async def notify(notification: Notification, request: Request):
        receipt = await request.app.state.router.send(notification)
        logger.info("delivery_accepted request_id=%s channel=gotify message_id=%d severity=%s",
                    request.state.request_id, receipt.message_id, notification.severity)
        return NotifyResponse(message_id=receipt.message_id, request_id=request.state.request_id)

    return app


app = create_app()
