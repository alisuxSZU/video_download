"""FastAPI 应用入口。

- 配置 CORS、安全响应头、统一异常处理
- 挂载 /static 托管前端单页
- 启动时清空旧临时文件、启动后台清理任务
- 注意：job 存内存，必须单进程单 worker 运行(--workers 1)
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import tasks
from .config import settings
from .routes import router
from .security import RateLimiter, register_exception_handlers, SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("app")


def _wipe_expired_temp() -> None:
    """启动时清空 TEMP_DIR 下所有旧临时文件（上次运行的残留）。"""
    root = Path(settings.temp_dir)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return
    for child in root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _wipe_expired_temp()
    tasks.start_cleanup_task()
    logger.info("vdl app started, temp_dir=%s", settings.temp_dir)
    yield
    logger.info("vdl app stopped")


app = FastAPI(title="万能视频下载", version=settings.version, lifespan=lifespan)

# ---- 安全响应头中间件 ----
app.add_middleware(SecurityHeadersMiddleware)

# ---- CORS（默认同源，仅当配置了 ALLOWED_ORIGIN 才放行） ----
if settings.allowed_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.allowed_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ---- 状态/限流器 ----
app.state.rate_limiter = RateLimiter()
app.state.temp_dir = Path(settings.temp_dir)

# ---- 路由与异常处理 ----
app.include_router(router)
register_exception_handlers(app)

# ---- 静态前端单页 ----
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def index():
    from fastapi.responses import HTMLResponse

    file = _static_dir / "index.html"
    html = file.read_text(encoding="utf-8") if file.exists() else "前端尚未就绪"
    return HTMLResponse(html)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    run()
