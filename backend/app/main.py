"""FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import _init_db
from .routers import admin, auth, custom_proxies, external_subs, public, tags
from .scheduler import scheduler, start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== 安全检查 =====
    if not settings.jwt_secret_valid:
        log.warning(
            "⚠️  JWT_SECRET 使用默认值或长度不足 32 字符！"
            "请在 .env 配置: JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
        )
    origins = settings.cors_origins_list
    if not origins:
        log.info("CORS: 未配置白名单，仅同源可访问")
    else:
        log.info("CORS 白名单: %s", origins)

    _init_db()
    from .routers.auth import ensure_default_admin
    try:
        ensure_default_admin()
        log.info("default admin ensured")
    except Exception:
        log.exception("failed to ensure default admin")
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Mihomo Probe Dashboard", lifespan=lifespan)

# ===== CORS 白名单（生产环境务必配置 cors_origins） =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list or ["http://localhost", "http://localhost:19001"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(custom_proxies.router)
app.include_router(tags.router)
app.include_router(external_subs.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# 静态前端
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/admin")
def admin_page():
    return RedirectResponse(url="/static/admin.html")


@app.get("/admin/login")
def admin_login_page():
    return RedirectResponse(url="/static/login.html")


@app.get("/admin/logout")
def admin_logout_page():
    return RedirectResponse(url="/static/login.html")