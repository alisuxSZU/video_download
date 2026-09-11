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
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
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
    return HTMLResponse(html.replace("{{SITE_URL}}", settings.site_base_url))


# ---- SEO：robots.txt / sitemap.xml / 教程内容页（/guides）----
# pages/ 存放服务端渲染的 HTML 模板（不走 /static 静态挂载，避免出现重复 URL）。
# {{SITE_URL}} 占位符渲染时替换为生产域名（SITE_BASE_URL）；未配置时为空串，
# canonical/og:url 退化为根相对路径（按当前访问域名解析，不影响本地开发）。
PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"

# 教程 slug 白名单：同时用于路由校验（防路径穿越）与 sitemap 生成。
GUIDE_SLUGS = (
    "bilibili-video-download",
    "douyin-no-watermark-download",
    "ai-video-summary",
    "subtitle-extract-translate",
)


def _render_page(file: Path) -> str:
    return file.read_text(encoding="utf-8").replace("{{SITE_URL}}", settings.site_base_url)


def _404_response() -> HTMLResponse:
    file = PAGES_DIR / "404.html"
    if file.exists():
        return HTMLResponse(_render_page(file), status_code=404)
    return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    # GEO：显式声明主流 AI 引擎爬虫可抓取全部页面（豆包/ChatGPT/Claude/Perplexity 等内容收录的前提）
    ai_bots = (
        "GPTBot", "OAI-SearchBot", "ChatGPT-User",  # OpenAI / ChatGPT
        "ClaudeBot", "anthropic-ai",                # Anthropic / Claude
        "PerplexityBot", "Google-Extended", "Applebot-Extended",
        "Amazonbot", "meta-externalagent",          # Meta AI
        "Bytespider",                               # 字节跳动 / 豆包
        "PetalBot",                                 # 华为 / 小艺
    )
    lines: list[str] = []
    for bot in ai_bots:
        lines += [f"User-agent: {bot}", "Allow: /", ""]
    lines += ["User-agent: *", "Disallow: /api/"]
    if settings.site_base_url:
        lines.append(f"Sitemap: {settings.site_base_url}/sitemap.xml")
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    # 未配置 SITE_BASE_URL 时退化为本次请求的 scheme+host（本地开发友好；反代部署需转发 X-Forwarded-* 头）
    base = settings.site_base_url or str(request.base_url).rstrip("/")
    today = date.today().isoformat()
    entries = [
        ("/", "daily", "1.0"),
        ("/guides", "monthly", "0.7"),
        *((f"/guides/{slug}", "monthly", "0.8") for slug in GUIDE_SLUGS),
    ]
    urls = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in entries
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/guides", include_in_schema=False)
async def guides_index():
    file = PAGES_DIR / "guides" / "index.html"
    if not file.exists():
        return _404_response()
    return HTMLResponse(_render_page(file))


@app.get("/guides/{slug}", include_in_schema=False)
async def guide_page(slug: str):
    # 白名单校验：未知 slug 直接 404（同时杜绝 ../ 路径穿越）
    if slug not in GUIDE_SLUGS:
        return _404_response()
    file = PAGES_DIR / "guides" / f"{slug}.html"
    if not file.exists():
        return _404_response()
    return HTMLResponse(_render_page(file))


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request):
    """GEO：面向 AI 引擎的站点说明（llmstxt.org 约定），链接用绝对 URL。"""
    base = settings.site_base_url or str(request.base_url).rstrip("/")
    file = PAGES_DIR / "llms.txt"
    if not file.exists():
        return PlainTextResponse("", status_code=404)
    # 注意：不用 _render_page（其会先用 site_base_url 替换占位符），此处需用 request 兜底域名
    return PlainTextResponse(file.read_text(encoding="utf-8").replace("{{SITE_URL}}", base), media_type="text/plain; charset=utf-8")


@app.get("/llms-full.txt", include_in_schema=False)
async def llms_full_txt(request: Request):
    """GEO：站点全文 Markdown 版（首页 + 全部教程），供 AI 引擎整站引用。"""
    base = settings.site_base_url or str(request.base_url).rstrip("/")
    file = PAGES_DIR / "llms-full.txt"
    if not file.exists():
        return PlainTextResponse("", status_code=404)
    return PlainTextResponse(file.read_text(encoding="utf-8").replace("{{SITE_URL}}", base), media_type="text/plain; charset=utf-8")


@app.get("/{keyfile}.txt", include_in_schema=False)
async def indexnow_key_file(keyfile: str):
    """IndexNow 验证文件：GET /{INDEXNOW_KEY}.txt 返回 key 本身；未配置或 key 不符一律 404。

    注意：必须注册在 robots.txt / llms.txt 等精确 .txt 路由之后，避免遮蔽。
    """
    if not settings.indexnow_key or keyfile != settings.indexnow_key:
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse(settings.indexnow_key, media_type="text/plain; charset=utf-8")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    run()
