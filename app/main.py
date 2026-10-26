"""FastAPI application factory + APScheduler hook."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx

# greenlet 3.x + aiosqlite + Windows require SelectorEventLoop instead of the
# default ProactorEventLoop, otherwise SQLAlchemy's cursor.__aenter__() can't
# resolve greenlet_spawn and raises "greenlet_spawn has not been called".
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Application loggers (e.g. the per-turn "model_route" line) need a root
# handler; uvicorn only configures its own namespaced loggers.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """Mirror root + uvicorn.error logs into DATA_DIR/logs/server.log.

    On Android the console IS logcat (adb-only); a file copy in the app's
    private dir is the only pullable evidence when something 500s on a phone.
    Secrets never reach logs (app layer masks keys/tokens by convention).
    """
    android_data = os.environ.get("ANDROID_DATA_DIR", "")
    data_dir = Path(android_data) if android_data else BASE_DIR / "data"
    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "server.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger("uvicorn.error").addHandler(fh)
    except OSError:
        pass  # console logging still works

from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings, BASE_DIR
from app.database import init_db, async_session as async_session_factory
from app.routers import auth, chat, memories, companion, moments, settings as settings_router
from app.security import get_auth_token, require_auth
from app.services.proactive import run_proactive_tick, SCHEDULE_INTERVAL_MINUTES
from app.services.life_engine import tick_all_users
from app.services.moments_engine import maybe_generate_moment


def create_app() -> FastAPI:
    # Scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # File logging must be live before lifespan runs so startup errors land
    # in server.log too.
    _setup_file_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        # Materialise the access token before the first request so it can be
        # read from .auth_token as soon as the server is up.
        get_auth_token()

        # Shared httpx.AsyncClient — connection pool reuse instead of
        # per-call client creation (the hot path does chat_completion +
        # model_failover + Librarian spawn, which used to cost 3–9 client
        # init/close cycles per turn).  Stash on app.state so chat routers
        # can read it without re-importing.
        app.state.llm_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))

        scheduler.add_job(
            run_proactive_tick,
            "interval",
            minutes=SCHEDULE_INTERVAL_MINUTES,
            id="proactive_poke",
            replace_existing=True,
        )

        async def _life_tick():
            try:
                await tick_all_users(async_session_factory)
            except Exception:
                pass

        scheduler.add_job(
            _life_tick,
            "interval",
            hours=1,
            id="life-tick",
            replace_existing=True,
        )

        # 她的说说：每 2 小时补一条（feed 安静满 2h 才发，避免刷屏）。
        async def _moments_tick():
            try:
                await maybe_generate_moment(async_session_factory)
            except Exception:
                pass

        scheduler.add_job(
            _moments_tick,
            "interval",
            hours=2,
            id="moments-bi-hourly",
            replace_existing=True,
        )
        # Boot-time catch-up so a fresh install doesn't wait until tomorrow 14:00.
        async def _moments_boot_catchup():
            await asyncio.sleep(20)
            await _moments_tick()

        asyncio.ensure_future(_moments_boot_catchup())

        scheduler.start()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)
        try:
            await app.state.llm_client.aclose()
        except Exception:
            pass

    app = FastAPI(
        title="AI Friend",
        description="A persistent female AI companion with deterministic emotion kernel, layered memory, and self-updating persona.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Static + templates — BASE_DIR is module-level from app.config, not on Settings.
    static_dir = BASE_DIR / "app" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)  # ensure exists for StaticFiles
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    # PWA root-scope endpoints — service workers must live at the root to
    # intercept fetch events anywhere on the origin. We can't rely on
    # StaticFiles mount alone because it would serve them under /static/*.
    @app.get("/manifest.json")
    async def manifest():
        return FileResponse(
            str(static_dir / "manifest.json"),
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(str(static_dir / "sw.js"), media_type="application/javascript")

    # Routers — every application endpoint requires the access token; only the
    # auth router (login/status/logout) stays public.
    protected = [Depends(require_auth)]
    app.include_router(auth.router)
    app.include_router(chat.router, dependencies=protected)
    app.include_router(memories.router, dependencies=protected)
    app.include_router(companion.router, dependencies=protected)
    app.include_router(moments.router, dependencies=protected)
    app.include_router(settings_router.router, dependencies=protected)
    from app.routers import bridge
    app.include_router(bridge.router)  # OpenAI 兼容桥：自带 Bearer 鉴权

    # ---------------------------------------------------------------
    # App-level rate limiter — simple in-memory sliding window.
    # 60 requests per minute per IP.  Memory is tiny (one tuple per IP),
    # no external dep, auto-cleans stale entries on every request.
    # ---------------------------------------------------------------
    import collections
    import time as _time

    _RL_WINDOW = 60          # seconds
    _RL_MAX = 60             # requests per window
    _RL_BUCKET: dict[str, collections.deque[float]] = {}

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        # Only throttle API routes — static files + HTML page stay open.
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        key = request.client.host if request.client else "unknown"
        now = _time.monotonic()
        bucket = _RL_BUCKET.get(key)
        if bucket is None:
            bucket = collections.deque()
            _RL_BUCKET[key] = bucket

        # Drop timestamps older than the window.
        cutoff = now - _RL_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= _RL_MAX:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(_RL_WINDOW)},
            )

        bucket.append(now)
        # Opportunistic cleanup — prune IPs that haven't hit for 10 min.
        if len(_RL_BUCKET) > 1024:
            stale = [k for k, v in _RL_BUCKET.items() if v and v[-1] < now - 600]
            for k in stale:
                del _RL_BUCKET[k]

        return await call_next(request)

    # ---------------------------------------------------------------
    # Global fallback — any unhandled exception becomes a clear JSON 500
    # with the exception CLASS NAME (safe, no secrets) instead of a blank
    # wall; the full traceback lands in server.log / logcat.
    # ---------------------------------------------------------------
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误（{type(exc).__name__}），详见服务端日志 server.log"},
        )

    # Frontend — minimal Jinja2 page
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "companion_name": settings.COMPANION_NAME,
                "user_name": "小朋友",
            },
        )

    return app


async def _run_proactive_morning() -> None:
    from app.services.proactive import daily_proactive_tick
    await daily_proactive_tick(None, "morning")


async def _run_proactive_evening() -> None:
    from app.services.proactive import daily_proactive_tick
    await daily_proactive_tick(None, "evening")


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
