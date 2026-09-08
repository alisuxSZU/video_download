"""安全模块：URL/SSRF 校验、滑窗限流、安全响应头、统一异常处理、文件名清洗。

面向公开上线，遵循「最小信任、不泄露内部信息」原则。
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("app.security")


# =========================================================================
# 1) URL / SSRF 校验
# =========================================================================
# 拒绝的 scheme 之外的方案一律拦截；仅允许 http/https
_ALLOWED_SCHEMES = {"http", "https"}

# 明确禁止的私网/回环/保留段 -> 命中即拒 (169.254.169.254 元数据重点拦)
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),  # 覆盖所有 IPv4 映射地址(含 127/8、169.254)
]


class SSRFError(Exception):
    """URL 校验失败。"""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


class InvalidURL(SSRFError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message, status)


def _is_private(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        # 解析不出合法 IP -> 视为不可信，保守拒绝
        return True
    for net in _PRIVATE_NETS:
        if ip in net:
            return True
    if ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return True
    return False


def validate_url(url: str) -> str:
    """校验用户提供的下载/解析 URL，防 SSRF 与协议注入。

    返回清理后的 url；失败抛 SSRFError/InvalidURL。
    """
    url = (url or "").strip()
    if not url:
        raise InvalidURL("链接不能为空")
    if len(url) > 2048:
        raise InvalidURL("链接过长")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidURL("仅支持 http/https 链接", status=400)

    host = parsed.hostname
    if not host:
        raise InvalidURL("链接缺少有效主机名")

    # 公共上线：解析出真实 IP 判断是否为私网/回环/保留段
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidURL("无法解析该域名，请检查链接") from exc

    for info in infos:
        ip_str = info[4][0]
        if _is_private(ip_str):
            raise SSRFError("不允许访问内网地址", status=422)

    # 可选提取器白名单(ie_key)，开启后非白名单站点在解析阶段被拒
    return url


# =========================================================================
# 2) 限流（内存滑窗，按 IP + 端点分组）
# =========================================================================
class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        """返回 (是否允许, 距下次可用的秒数)。"""
        now = time.time()
        dq = self._buckets[key]
        while dq and now - dq[0] > window_seconds:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = max(1, int(window_seconds - (now - dq[0])) + 1)
            return False, retry_after
        dq.append(now)
        return True, 0


async def check_rate(request: Request, group: str, limit: int, window: int = 60) -> None:
    """按客户端 IP + 端点分组限流；超限抛 httpx.HTTPStatusError 语义(由调用方转 429)。

    这里直接返回响应由调用方处理更清晰；本函数抛出 RateError 供 route 捕获。
    """
    client_ip = request.client.host if request.client else "unknown"
    key = f"{group}:{client_ip}"
    ok, retry_after = request.app.state.rate_limiter.allow(key, limit, window)
    if not ok:
        raise RateError(retry_after)


class RateError(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limited")
        self.retry_after = retry_after


# =========================================================================
# 3) 安全响应头中间件
# =========================================================================
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data: http: https:; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self' https:; "
        )
        return response


# =========================================================================
# 4) 文件名清洗
# =========================================================================
def sanitize_filename(name: str, default: str = "download") -> str:
    """清洗文件名，剔除 Windows 非法字符与路径穿越，保证 Content-Disposition 合法。"""
    if not name:
        return default
    # 去非法字符: /\:*?"<>| 控制字符
    name = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]+', "_", name)
    name = name.strip(". ")
    if len(name) > 150:
        name = name[:150]
    return name or default


# =========================================================================
# 5) 统一异常处理（friendly errors）
# =========================================================================
def friendly_error(exc: Exception, url: str = "") -> dict[str, str]:
    """把异常映射为友好中文错误文案，绝不泄露堆栈/绝对路径/完整 URL。"""
    # 限流
    if isinstance(exc, RateError):
        return {"error": "操作过于频繁，请稍后再试", "code": "rate_limited"}
    # InvalidURL 是 SSRFError 子类，必须先判断
    if isinstance(exc, InvalidURL):
        return {"error": exc.message, "code": "invalid_url"}
    if isinstance(exc, SSRFError):
        return {"error": exc.message, "code": "ssrf"}

    # 抖音(服务端无头浏览器)
    try:
        from .douyin import DouyinBlockedError, DouyinUnsupportedError
    except ImportError:  # 兜底: 模块加载失败时按普通错误走
        DouyinBlockedError = DouyinUnsupportedError = ()  # type: ignore[assignment]
    if isinstance(exc, DouyinUnsupportedError):
        return {"error": str(exc) or "该视频不支持下载", "code": "unsupported"}
    if isinstance(exc, DouyinBlockedError):
        return {"error": str(exc) or "抖音风控拦截,请稍后再试", "code": "forbidden"}

    # yt-dlp 相关
    exc_str = str(exc).lower()
    if "unsupported url" in exc_str:
        return {"error": "暂不支持该平台或链接格式，请换一个试试", "code": "unsupported"}
    if "http error 404" in exc_str or "not found" in exc_str:
        return {"error": "视频不存在或已失效", "code": "not_found"}
    if "http error 403" in exc_str:
        return {"error": "该平台拒绝访问（可能需登录或受地区/版权限制）", "code": "forbidden"}
    if "http error 429" in exc_str or "too many requests" in exc_str:
        return {"error": "平台限速，请稍后再试", "code": "rate_limited_by_platform"}
    if "private ip" in exc_str or "internal" in exc_str:
        return {"error": "不允许访问内网地址", "code": "ssrf"}
    if "ffmpeg" in exc_str and "not found" in exc_str:
        return {"error": "服务器缺少 ffmpeg，高清合并暂不可用", "code": "no_ffmpeg"}
    if "no subtitles" in exc_str or "subtitles not available" in exc_str:
        return {"error": "该视频暂无可用字幕", "code": "no_subtitles"}
    if "login required" in exc_str or "sign in" in exc_str:
        return {"error": "该视频需要登录后才能访问，暂不支持", "code": "login_required"}
    if "timed out" in exc_str or "timeout" in exc_str:
        return {"error": "请求超时，请检查网络或稍后再试", "code": "timeout"}
    if "unable to download" in exc_str or "unable to extract" in exc_str:
        return {"error": "无法获取该视频，可能是平台反爬或链接已失效", "code": "extract_failed"}

    # 默认兜底
    logger.exception("Unhandled error url=%s", _safe_url(url))
    return {"error": "解析失败，请稍后重试或换一个链接", "code": "unknown"}


# 错误码 -> 建议的 HTTP 状态码
_STATUS_BY_CODE: dict[str, int] = {
    "invalid_url": 400,
    "ssrf": 422,
    "unsupported": 400,
    "not_found": 404,
    "forbidden": 502,
    "login_required": 403,
    "extract_failed": 502,
    "no_ffmpeg": 500,
    "no_subtitles": 422,
    "rate_limited": 429,
    "rate_limited_by_platform": 429,
    "timeout": 504,
    "unknown": 502,
    "internal": 500,
}


def error_status(code: str) -> int:
    return _STATUS_BY_CODE.get(code, 500)


def _safe_url(url: str) -> str:
    """日志脱敏：只保留 host + 尾部路径片段，避免完整 URL(含敏感参数)进日志。"""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return "<url>"


def register_exception_handlers(app) -> None:
    """注册全局异常处理：返回 JSON 而非 HTML，且不泄露堆栈。"""

    @app.exception_handler(SSRFError)
    @app.exception_handler(InvalidURL)
    async def _handler(request: Request, exc):
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    @app.exception_handler(Exception)
    async def _fallback(request: Request, exc):
        logger.exception("Unhandled exception path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "服务器开小差了，请稍后重试", "code": "internal"},
        )
