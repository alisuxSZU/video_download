"""抖音视频解析/下载 —— 服务端无头浏览器方案。

为什么不能让 yt-dlp 直跑抖音? 抖音 Web 业务 API(详情/列表/评论)被 a_bogus + msToken 签名墙覆盖,且 a_bogus 已绑定
  浏览器环境指纹(UA/版本/设备参数须在签名时一致),纯 Python 逆向模拟不出真实浏览器,
  f2/yt-dlp 直跑一律失败(缺 cookie/签名)。免 Cookie 直连(video_id→aweme.snssdk.com/v1/play)
  的 SSR 通道已于 2026-08-30 关闭(share 页不再渲染 play_addr.uri)。

方案(成熟开源,非自造): 服务器内置无头 Chrome(Playwright 复用系统 Chrome,`channel="chrome"`,
无需下载 Chromium),用**浏览器自己的匿名游客会话**加载抖音页 —— 页内真实 JS 现场计算 a_bogus,
我们只拦截它对 /aweme/v1/web/aweme/detail 的**真实网络请求响应**(expect_response),拿到
play_addr.url_list[0](去掉 playwm 水印后缀)。全程:
  - 不需要用户 Cookie(浏览器匿名游客会话,不碰用户隐私)
  - 不需要浏览器插件/油猴(浏览器跑在服务器上,不是用户端扩展)
  - 不转发票据给外部(本模块只用 httpx 加 UA+Referer 直连 CDN,绝不把 settings.cookies_file 转发给字节)

本模块只做「解析」;下载的字节流由 downloader.run_download 依 _play_url 走一次 httpx 直连。
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from .config import settings

logger = logging.getLogger("app.douyin")

# 匹配抖音系链接: www.douyin.com / douyin.com / v.douyin.com 短链 / iesdouyin.com
_DOUYIN_URL_RE = re.compile(r"^https?://([^/]*\.)?(douyin\.com|iesdouyin\.com)/", re.I)


class DouyinError(Exception):
    """抖音解析失败(友好中文文案由 security.friendly_error 映射)。"""


class DouyinBlockedError(DouyinError):
    """IP 被风控 / 触发验证码 / 游客会话被弹。"""


class DouyinUnsupportedError(DouyinError):
    """图文 / 直播 / 私密 / 已删除等不可下载情形。"""


def is_douyin_url(url: str) -> bool:
    return bool(url and _DOUYIN_URL_RE.match(url))


# ---------------------------------------------------------------------------
# 浏览器: 单浏览器线程独占 Playwright 实例
#
# Playwright sync API 的 greenlet 绑定到"创建它的线程"。绝不可在多线程间复用
# 同一个 context/page(否则抛 greenlet.error: Cannot switch to a different thread)。
# 而 resolve() 会被两类线程调用: /api/parse 走 FastAPI 默认执行器线程,下载 job 走
# vdl 线程池 —— 所以必须让一个**专用线程**独占浏览器,其余线程只提交请求、阻塞等结果。
# ---------------------------------------------------------------------------
import threading
import queue
from concurrent.futures import Future

_browser_state: dict = {}
_req_queue: "queue.Queue | None" = None
_worker: "threading.Thread | None" = None
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    """确保浏览器线程已启动(惰性,首次调用解析时拉起)。"""
    global _worker, _req_queue

    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _req_queue = queue.Queue()
        _worker = threading.Thread(
            target=_worker_main, name="douyin-browser", daemon=True
        )
        _worker.start()


def _worker_main() -> None:
    """浏览器线程主循环: 独占启动 Playwright,顺序处理解析请求。

    启动/预热失败会记录为 start_error,后续所有请求都转为友好 DouyinError,
    而不让浏览器半损坏状态继续被使用。
    """
    start_error = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        b = pw.chromium.launch(
            channel="chrome",  # 复用已装好的系统 Chrome,免下载 Chromium
            headless=settings.douyin_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = b.new_context(
            user_agent=settings.user_agent,
            locale="zh-CN",
            viewport={"width": 390, "height": 844},
        )
        # 首次访问 douyin.com,让站点种下游客 cookie(升温),降低被风控概率
        page = ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        page.close()
        _browser_state["pw"] = pw
        _browser_state["browser"] = b
        _browser_state["context"] = ctx
        logger.info("douyin headless browser ready (channel=chrome, headless=%s)", settings.douyin_headless)
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to start douyin browser (thread)")
        start_error = DouyinError("抖音解析环境启动失败,请检查服务器是否已安装 Chrome")

    # —— 常驻循环: 只在本线程内做 Playwright 调用 ——
    while True:
        item = _req_queue.get()
        if item is None:  # 关停哨兵,退出线程
            break
        future, url = item
        if start_error is not None:
            future.set_exception(start_error)
            continue
        page = None
        try:
            page = _browser_state["context"].new_page()
            detail = _fetch_aweme_detail(page, url)
            future.set_result(detail)
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# 解析: 返回 yt-dlp 形状的 info dict(供 probe/_build_payload/_apply_format 复用)
# ---------------------------------------------------------------------------
def resolve(url: str) -> dict:
    if not settings.douyin_enabled:
        raise DouyinBlockedError("抖音下载暂不可用")

    _ensure_worker()
    future: "Future[dict]" = Future()
    _req_queue.put((future, url))
    # 限时等待,避免浏览器线程卡死时调用方无限阻塞
    try:
        detail = future.result(timeout=settings.douyin_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — Future 内抛出的异常原样透传
        if isinstance(exc, TimeoutError):
            raise DouyinBlockedError("抖音未返回视频详情(可能被风控或触发验证码)") from exc
        raise
    return _build_info(detail, url)


def _fetch_aweme_detail(page, url: str) -> dict:
    """加载 url 并抓到真实签名的 aweme/detail 响应体(aweme_detail dict)。

    关键: 不自己发 fetch(会因缺 a_bogus 拿到空 body),而是让页面自己发;
    用 expect_response 拦截页面对该 detail API 的响应 —— 这是页内 JS 现场签名的真实结果。
    """
    timeout_ms = int(settings.douyin_timeout_seconds * 1000)
    try:
        with page.expect_response(
            lambda r: "aweme/v1/web/aweme/detail" in r.url, timeout=timeout_ms
        ) as ri:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        resp = ri.value
    except Exception as exc:  # noqa: BLE001
        if _is_timeout(exc):
            raise DouyinBlockedError("抖音未返回视频详情(可能被风控或触发验证码)") from exc
        raise DouyinError("无法打开该抖音链接,请确认链接有效") from exc

    if resp.status != 200:
        raise DouyinBlockedError("抖音风控拦截,请稍后再试")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise DouyinError("抖音返回数据异常") from exc
    return data.get("aweme_detail") or {}


def _build_info(detail: dict, url: str) -> dict:
    if not detail:
        raise DouyinUnsupportedError("抖音未返回视频信息(可能是图文/直播/私密/已删除)")
    desc = detail.get("desc") or "抖音视频"
    aweme_id = str(detail.get("aweme_id") or "")
    video = detail.get("video") or {}
    play_addr = video.get("play_addr") or {}

    urls = [u.replace("playwm", "play") for u in (play_addr.get("url_list") or [])]
    play_url = next((u for u in urls if u), None)
    if not play_url:
        raise DouyinUnsupportedError("该视频无可用播放地址(可能是图文/直播/MSE 流)")

    cover = ""
    for key in ("cover", "origin_cover", "dynamic_cover"):
        u = (video.get(key) or {}).get("url_list") or []
        if u:
            cover = u[0]
            break

    duration_ms = detail.get("duration") or 0
    height = video.get("height") or 0
    width = video.get("width") or 0
    note = "高清" if height >= 720 else "流畅"

    fmt = {
        "format_id": "0",
        "ext": "mp4",
        "resolution": f"{height}p" if height else "mp4",
        "height": height or None,
        "width": width or None,
        "fps": None,
        "vcodec": "h264",  # 抖音主流 h264
        "acodec": "aac",
        "filesize": _filesize_from(detail),
        "needs_merge": False,
        "progressive": True,  # 单文件(音视频合一),免 ffmpeg 合并
        "note": note,
        "url": play_url,
    }

    return {
        "id": aweme_id,
        "title": desc,
        "thumbnail": cover,
        "duration": (duration_ms / 1000.0) if duration_ms else 0,
        "extractor_key": "Douyin",
        "extractor": "Douyin",
        "webpage_url": url,
        "ext": "mp4",
        "formats": [fmt],
        "subtitles": {},
        "automatic_captions": {},
        # 私有标记: 供 run_download 走 httpx 直连(而非再走 yt-dlp)
        "_play_url": play_url,
        "_play_headers": {
            "User-Agent": settings.user_agent,
            "Referer": "https://www.douyin.com/",
        },
    }


def _filesize_from(detail: dict) -> int:
    try:
        size = 0
        for key in ("download_addr", "play_addr"):
            v = (detail.get(key) or {}).get("data_size")
            if v:
                size = max(size, int(v))
        return size or 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 下载(字节流): 依 _play_url 用 httpx 流式拉,更新 job 进度;产物落盘后由 _resolve_output 收尾
# ---------------------------------------------------------------------------
def download(job, info: dict, job_dir: Path) -> None:
    """流式下载(替代 yt-dlp)。info 必须含 _play_url/_play_headers。"""
    import httpx

    play_url = info.get("_play_url")
    headers = info.get("_play_headers") or {}
    if not play_url:
        raise DouyinUnsupportedError("缺少可下载的播放地址")

    job_dir.mkdir(parents=True, exist_ok=True)
    out_path = job_dir / f"{_safe_stem(info.get('title') or 'douyin')}.mp4"
    tmp_path = job_dir / f"{out_path.name}.part"

    total = 0
    done = 0
    now = time.monotonic()
    chunk_t = now
    chunk_bytes = 0
    speed = 0.0

    with httpx.Client(headers=headers, follow_redirects=True, timeout=settings.douyin_timeout_seconds) as client:
        with client.stream("GET", play_url) as resp:
            if resp.status_code != 200:
                resp.read()
                raise DouyinError("下载被抖音拒绝,请稍后重试")
            total = int(resp.headers.get("content-length") or 0)
            job.set_progress(status="downloading", ext="mp4", total_bytes=total)
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=128 * 1024):
                    if job._cancel.is_set():
                        tmp_path.unlink(missing_ok=True)
                        from .downloader import DownloadCancelled

                        raise DownloadCancelled()
                    fh.write(chunk)
                    done += len(chunk)
                    chunk_bytes += len(chunk)
                    # 每 ~0.5s 采样一次速度,避免每 chunk 结算一次
                    now = time.monotonic()
                    if now - chunk_t >= 0.5:
                        speed = chunk_bytes / (now - chunk_t)
                        chunk_bytes = 0
                        chunk_t = now
                        if total > 0:
                            job.set_progress(
                                status="downloading",
                                downloaded_bytes=done,
                                total_bytes=total,
                                progress=min(done / total * 100, 99.9),
                                speed=_human_speed(speed),
                            )

    tmp_path.replace(out_path)
    # 收尾写回精确字节数与 100%:下载中每 ~0.5s 采样的 done 是近似值,结束时校正
    job.set_progress(
        status="downloading",
        downloaded_bytes=done,
        total_bytes=total,
        progress=100,
        filesize=os.path.getsize(out_path),
    )


def _human_speed(bytes_per_sec: float) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:.1f}{unit}"
        bytes_per_sec /= 1024
    return f"{bytes_per_sec:.1f}TB/s"


def _safe_stem(name: str) -> str:
    from .security import sanitize_filename

    return sanitize_filename(name, default="douyin")


def _is_timeout(exc: Exception) -> bool:
    s = str(exc).lower()
    return "timeout" in s or "timed out" in s or "exceeded" in s
