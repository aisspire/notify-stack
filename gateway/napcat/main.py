import asyncio
import hmac
import html
import io
import logging
import time
from contextlib import asynccontextmanager, suppress

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from qrcode.image.pure import PyPNGImage

from gateway.napcat.client import NapCatError
from gateway.napcat.config import WatchSettings
from gateway.napcat.service import Watcher, qr_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

SAFE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Cloudflare-CDN-Cache-Control": "no-store",
    # no-referrer also turns a browser form POST's Origin into "null", which
    # conflicts with refresh's origin check. Keep the origin, never the signed path.
    "Referrer-Policy": "strict-origin",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def png_bytes(value):
    qr = qrcode.QRCode(box_size=6, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(value)
    qr.make(fit=True)
    output = io.BytesIO()
    qr.make_image(image_factory=PyPNGImage).save(output)
    return output.getvalue()


def page(body, status=200, *, refresh=False):
    meta = '<meta http-equiv="refresh" content="15">' if refresh else ""
    text = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + meta
    text += '<title>QQ 登录</title><style>body{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px;line-height:1.7}img{max-width:100%;height:auto}button,a{font-size:17px}button{padding:10px 20px;cursor:pointer}small{color:#555}</style></head><body>'
    return HTMLResponse(text + body + "</body></html>", status_code=status, headers=SAFE_HEADERS)


def create_app(settings=None, *, watcher=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or WatchSettings.from_env()
        app.state.settings = config
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8, pool=2),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False, trust_env=False,
        ) as client:
            app.state.watcher = watcher or Watcher(config, client)
            if start_worker:
                app.state.watcher.task = asyncio.create_task(app.state.watcher.run())
            try:
                yield
            finally:
                task = app.state.watcher.task
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/internal/"):
            values = request.headers.getlist("authorization")
            scheme, _, token = values[0].partition(" ") if len(values) == 1 else ("", "", "")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), request.app.state.settings.gateway_token.encode()):
                return JSONResponse({"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger("notify.napcat").error("http_request_failed")
            return page("<h1>暂时无法访问</h1><p>请稍后重试。</p>", 503)
        for key, value in SAFE_HEADERS.items():
            response.headers[key] = value
        return response

    def checked(request, token):
        w = request.app.state.watcher
        if not w.valid(token):
            raise HTTPException(410, "登录链接已失效，请打开最新告警通知中的链接。")
        return w

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return page("<h1>登录入口不可用</h1><p>" + html.escape(str(exc.detail)) + "</p>", exc.status_code)

    @app.get("/health")
    async def health(request: Request):
        w = request.app.state.watcher
        healthy = time.time() - w.last_tick_at < max(120, w.settings.poll_seconds * 4)
        if w.task and w.task.done():
            healthy = False
        return JSONResponse({"status": "ok" if healthy else "stale"}, 200 if healthy else 503)

    @app.get("/internal/status")
    async def status(request: Request):
        return request.app.state.watcher.status()

    @app.post("/internal/notify")
    async def notify(request: Request):
        w = request.app.state.watcher
        snapshot = await w.probe()
        if snapshot.online:
            return {"sent": False, "reason": "online"}
        if not w.monitor.state.incident_id:
            raise HTTPException(409, "尚未达到持续离线判定时间。")
        if w.monitor.state.last_notice_at is not None and time.time() - w.monitor.state.last_notice_at < 60:
            raise HTTPException(429, "请等待 60 秒后重试。")
        try:
            return {"sent": await w.deliver("offline")}
        except NapCatError:
            raise HTTPException(503, "暂时无法发送通知，请稍后重试。") from None

    @app.get("/napcat/login/{token}")
    async def login(request: Request, token: str):
        w = checked(request, token)
        try:
            snapshot = await w.probe()
        except NapCatError:
            return page("<h1>NapCat 暂时不可用</h1><p>正在重试连接，请稍后刷新本页。</p>", 503, refresh=True)
        if snapshot.online:
            return page("<h1>QQ 已在线</h1><p>无需再次扫码，旧二维码已停用。</p>")
        base = "/napcat/login/" + token
        body = "<h1>QQ 扫码登录</h1><p>使用手机 QQ 扫描二维码并确认登录。同一部手机可尝试保存图片后从 QQ 扫码相册识别；如果 QQ 不允许，请在电脑打开此页再用手机扫码。</p>"
        if snapshot.qr_url:
            body += '<img alt="QQ 登录二维码" src="' + base + "/qr/" + qr_hash(snapshot.qr_url) + '.png">'
        else:
            body += "<p>当前尚无可用二维码，可能正在重连或初始化。</p>"
        body += '<form method="post" action="' + base + '/refresh"><button type="submit">刷新二维码</button></form>'
        body += "<p><small>二维码过期后请手动刷新；刷新有 30 秒冷却。本页每 15 秒更新登录状态，不自动刷新二维码。</small></p>"
        return page(body, refresh=True)

    @app.get("/napcat/login/{token}/qr/{digest}.png")
    async def image(request: Request, token: str, digest: str):
        w = checked(request, token)
        try:
            snapshot = await w.probe()
        except NapCatError:
            raise HTTPException(503, "暂时无法获取二维码。") from None
        if snapshot.online or not snapshot.qr_url or digest != qr_hash(snapshot.qr_url):
            raise HTTPException(410, "二维码已更新或账号已在线，请打开登录页。")
        if w.png_cache_key != digest:
            w.png_cache = png_bytes(snapshot.qr_url)
            w.png_cache_key = digest
        return Response(w.png_cache, media_type="image/png", headers=SAFE_HEADERS)

    @app.post("/napcat/login/{token}/refresh")
    async def refresh(request: Request, token: str):
        w = checked(request, token)
        origin = request.headers.get("origin")
        if origin and origin != w.settings.public_url:
            raise HTTPException(403, "请求来源无效。")
        try:
            await w.refresh()
        except NapCatError as exc:
            messages = {
                "refresh_cooldown": (429, "刷新过于频繁，请等待 30 秒后再试。"),
                "already_online": (409, "QQ 已在线，无需刷新二维码。"),
                "login_in_progress": (409, "QQ 正在登录或重连，请稍后再试。"),
            }
            code, message = messages.get(str(exc), (503, "尚未取得新二维码，请稍后重试或通过原 NapCat 面板处理。"))
            base = "/napcat/login/" + token
            return page("<h1>刷新提示</h1><p>" + message + '</p><a href="' + base + '">返回登录页</a>', code)
        return RedirectResponse("/napcat/login/" + token, status_code=303, headers=SAFE_HEADERS)

    return app


app = create_app()
