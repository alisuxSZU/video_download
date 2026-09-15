"""API 路由：解析 / 下载 / 批量 / 轮询 / 成品 / 字幕 / AI / 账户 / 支付。

连接前端与后端能力，统一做限流、URL 校验、友好错误映射。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import ai, auth, billing, db, downloader, tasks
from .config import settings
from .models import (
    AskRequest,
    AuthRequest,
    BatchRequest,
    ChaptersRequest,
    CheckoutRequest,
    DownloadRequest,
    MindmapRequest,
    ParseRequest,
    SubtitleRequest,
    SummaryRequest,
    JobStatus,
)
from .security import friendly_error, RateError, validate_url, error_status

logger = logging.getLogger("app.routes")

router = APIRouter(prefix="/api")


# ---------------- 工具 ----------------
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _rate(request: Request, group: str, limit: int, window: int = 60) -> None:
    try:
        await _rate_impl(request, group, limit, window)
    except RateError as exc:
        raise HTTPException(
            status_code=429,
            detail={"ok": False, "error": "操作过于频繁，请稍后再试", "code": "rate_limited"},
            headers={"Retry-After": str(exc.retry_after)},
        )


async def _rate_impl(request: Request, group: str, limit: int, window: int) -> None:
    limiter = request.app.state.rate_limiter
    key = f"{group}:{_client_ip(request)}"
    ok, retry_after = limiter.allow(key, limit, window)
    if not ok:
        raise RateError(retry_after)


def _resolve_job(job_id: str):
    job = tasks.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "任务不存在或已过期"})
    return job


def _pro_required(message: str, *, used: int | None = None) -> JSONResponse:
    detail = {"ok": False, "error": message, "code": "pro_required"}
    if used is not None:
        detail["ai_used_today"] = used
        detail["ai_daily_limit"] = settings.ai_free_daily_limit
    return JSONResponse(status_code=403, content=detail)


async def _ai_gate(request: Request) -> dict | None:
    """AI 端点统一闸门：PRO 走更高分钟级限流；非 PRO 先查每日配额（成功后才计数）。

    返回当前用户（可能为 None=游客）；配额超限抛 403 pro_required。
    """
    user = auth.user_from_request(request)
    limit = settings.ai_pro_rate_per_min if auth.is_pro(user) else settings.ai_rate_per_min
    await _rate(request, "ai", limit)
    if not auth.is_pro(user):
        subject = auth.quota_subject(user, _client_ip(request))
        used = auth.ai_used_today(subject)
        if used >= settings.ai_free_daily_limit:
            raise HTTPException(
                status_code=403,
                detail={
                    "ok": False,
                    "error": f"免费额度已用完（每日 {settings.ai_free_daily_limit} 次 AI 功能），升级 PRO 不限次",
                    "code": "pro_required",
                    "ai_used_today": used,
                    "ai_daily_limit": settings.ai_free_daily_limit,
                },
            )
    return user


def _consume_ai(user: dict | None, request: Request) -> None:
    """AI 调用成功后计数（PRO 不计数）。"""
    if not auth.is_pro(user):
        auth.consume_ai_call(auth.quota_subject(user, _client_ip(request)))


# ---------------- 账户：注册 / 登录 / 退出 / 当前用户 ----------------
@router.post("/auth/register")
async def register(req: AuthRequest, request: Request):
    await _rate(request, "auth", settings.auth_rate_per_min)
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: auth.create_user(req.email, req.password)
        )
    except auth.AuthError as exc:
        return JSONResponse(
            status_code=exc.status, content={"ok": False, "error": exc.message, "code": exc.code}
        )
    return {
        "ok": True,
        "token": result["token"],
        "user": auth.public_user(result["user"], _client_ip(request)),
    }


@router.post("/auth/login")
async def login(req: AuthRequest, request: Request):
    await _rate(request, "auth", settings.auth_rate_per_min)
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: auth.authenticate(req.email, req.password)
        )
    except auth.AuthError as exc:
        return JSONResponse(
            status_code=exc.status, content={"ok": False, "error": exc.message, "code": exc.code}
        )
    return {
        "ok": True,
        "token": result["token"],
        "user": auth.public_user(result["user"], _client_ip(request)),
    }


@router.post("/auth/logout")
async def logout(request: Request):
    token = auth.bearer_token(request)
    if token:
        await asyncio.get_running_loop().run_in_executor(None, auth.revoke_token, token)
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request):
    """可选登录；未登录返回 user=null（不报错）。附 AI 配额，供前端渲染状态。"""
    user = auth.user_from_request(request)
    ip = _client_ip(request)
    if user is None:
        used = auth.ai_used_today(auth.quota_subject(None, ip))
        return {
            "ok": True,
            "user": None,
            "is_pro": False,
            "ai_used_today": used,
            "ai_daily_limit": settings.ai_free_daily_limit,
            # 免费用户清晰度封顶（前端据此渲染格式加锁 UI；0 表示不封顶）
            "free_max_height": settings.free_max_height,
        }
    pub = auth.public_user(user, ip)
    pro = pub["is_pro"]
    return {
        "ok": True,
        "user": pub,
        "is_pro": pro,
        "free_max_height": 0 if pro else settings.free_max_height,
    }


# ---------------- 支付：创建 Checkout / Stripe Webhook / 订单记录 ----------------
@router.post("/billing/checkout")
async def billing_checkout(req: CheckoutRequest, request: Request):
    user = auth.require_user(request)
    await _rate(request, "billing", settings.billing_rate_per_min)
    if not settings.billing_enabled:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "支付功能即将上线，敬请期待", "code": "billing_disabled"},
        )
    base = settings.site_base_url or str(request.base_url).rstrip("/")
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: billing.create_checkout(user, req.plan, base)
        )
    except billing.BillingError as exc:
        return JSONResponse(
            status_code=exc.status, content={"ok": False, "error": exc.message, "code": exc.code}
        )
    return {"ok": True, **result}


@router.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe 回调：必须用原始字节体验签（不能先 JSON 解析）。

    幂等：event_id 去重 + 订单 pending→paid 条件更新（见 billing.fulfill_order）。
    只有返回 2xx，Stripe 才停止重发；5xx/400 会按策略重试。
    """
    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = await asyncio.get_running_loop().run_in_executor(
            None, billing.construct_event, raw, sig
        )
    except billing.BillingError as exc:
        return JSONResponse(
            status_code=exc.status, content={"ok": False, "error": exc.message, "code": exc.code}
        )
    except Exception as exc:
        logger.warning("stripe webhook signature verification failed: %s", exc)
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "invalid signature"}
        )

    event_id = str(event.id)
    event_type = str(event.type)
    if billing.is_duplicate_event(event_id, event_type):
        logger.info("duplicate stripe event ignored: %s", event_id)
        return {"ok": True, "received": True, "duplicate": True}

    try:
        await asyncio.get_running_loop().run_in_executor(None, billing.handle_event, event)
    except billing.BillingError as exc:
        # 可修复的服务端错误（如库异常）：移除事件占位并返回 500，让 Stripe 重试；
        # 数据本身非法（4xx，重试无意义）：记录后回 200，避免无意义重发。
        billing.forget_event(event_id)
        if exc.status >= 500:
            logger.exception("stripe event handling failed (retryable): %s", event_id)
            return JSONResponse(status_code=500, content={"ok": False, "error": "retry"})
        logger.warning("stripe event bad payload %s: %s", event_id, exc.message)
    except Exception:
        billing.forget_event(event_id)
        logger.exception("stripe event handling failed (retryable): %s", event_id)
        return JSONResponse(status_code=500, content={"ok": False, "error": "retry"})
    return {"ok": True, "received": True}


@router.get("/billing/orders")
async def billing_orders(request: Request):
    user = auth.require_user(request)
    return {"ok": True, "orders": billing.list_orders(user["user_id"])}


# ---------------- 健康检查 ----------------
@router.get("/health")
async def health():
    return {"ok": True, "version": settings.version, "ffmpeg": downloader.ffmpeg_available()}


# ---------------- 封面图代理 ----------------
@router.get("/thumbnail")
async def thumbnail(url: str):
    """为跨站封面图做服务端代理。

    规避两件事：
      1. 部分 CDN(如 B站 hdslb)防盗链，浏览器直接 <img> 会被挡；
      2. 封面多为 http://，若站点走 https 会被浏览器按混合内容拦截，代理后走本站 https。
    校验 URL 防 SSRF；图片来自 yt-dlp 解析结果，但接口仍按用户输入对待。
    """
    try:
        safe_url = validate_url(url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})
    try:
        content, ctype = await asyncio.get_running_loop().run_in_executor(
            None, lambda: downloader.fetch_image(safe_url)
        )
    except Exception:
        return JSONResponse(status_code=502, content={"ok": False, "error": "封面图加载失败", "code": "thumbnail"})
    return Response(
        content=content,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------- 解析 ----------------
@router.post("/parse")
async def parse(req: ParseRequest, request: Request):
    await _rate(request, "parse", settings.parse_rate_per_min)
    try:
        url = validate_url(req.url)
        payload = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _probe_blocking(url)
        )
        return {"ok": True, **payload}
    except HTTPException:
        raise
    except Exception as exc:
        info = friendly_error(exc, req.url)
        return JSONResponse(status_code=error_status(info.get("code", "unknown")), content={"ok": False, **info})


def _probe_blocking(url: str) -> dict:
    # 使用 downloader.probe：自带平台风控(412/429/403)重试
    return downloader.probe(url)


# ---------------- 下载（单个 / 批量） ----------------
@router.post("/download")
async def download(req: DownloadRequest, request: Request):
    await _rate(request, "download", settings.download_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    if tasks.active_or_queued_count() >= tasks.MAX_ACTIVE_JOBS:
        raise HTTPException(status_code=503, detail={"ok": False, "error": "当前任务较多，请稍后重试"})

    # PRO 不限清晰度；免费用户封顶 free_max_height（默认 720p），任务层二次硬拦截
    user = auth.user_from_request(request)
    max_height = 0 if auth.is_pro(user) else settings.free_max_height
    job = tasks.create_job(url, req.format_id, max_height=max_height)
    tasks.start_download(job)
    return {"ok": True, "job_id": job.id, "status": job.status}


@router.post("/download/batch")
async def download_batch(req: BatchRequest, request: Request):
    await _rate(request, "download", settings.download_rate_per_min)
    if len(req.items) == 0:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "请至少提供一条链接"})
    if len(req.items) > settings.max_batch:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": f"单次最多 {settings.max_batch} 条"},
        )

    # 批量下载对所有人开放；清晰度封顶沿用单条下载同一规则
    user = auth.user_from_request(request)
    max_height = 0 if auth.is_pro(user) else settings.free_max_height
    jobs_payload = []
    for item in req.items:
        try:
            url = validate_url(item.url)
        except Exception as exc:
            jobs_payload.append({"url": item.url, "job_id": None, "status": "invalid", "error": friendly_error(exc)["error"]})
            continue
        job = tasks.create_job(url, item.format_id, max_height=max_height)
        tasks.start_download(job)
        jobs_payload.append({"url": item.url, "job_id": job.id, "status": job.status})

    return {"ok": True, "jobs": jobs_payload}


# ---------------- 任务轮询 / 成品 / 删除 ----------------
@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _resolve_job(job_id)
    return {"ok": True, "job": job.snapshot()}


@router.get("/jobs/{job_id}/file")
async def get_file(job_id: str):
    job = _resolve_job(job_id)
    if job.status != JobStatus.DONE or not job.filepath:
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "error": "文件尚未就绪，请等待进度完成", "code": "not_ready"},
        )
    path = Path(job.filepath)
    if not path.exists():
        raise HTTPException(status_code=410, detail={"ok": False, "error": "文件已过期，请重新下载"})
    media_type, _ = mimetypes.guess_type(job.filename)
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=job.filename,
    )


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    ok = tasks.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "任务不存在"})
    return {"ok": True}


# ---------------- 字幕 提取/翻译 ----------------
@router.post("/subtitles")
async def subtitles(req: SubtitleRequest, request: Request):
    user = auth.user_from_request(request)
    limit = settings.ai_pro_rate_per_min if auth.is_pro(user) else settings.ai_rate_per_min
    await _rate(request, "ai", limit)
    # 字幕翻译为 PRO 专属；仅提取（target_lang 为空）免费开放
    if req.target_lang and not auth.is_pro(user):
        return _pro_required("字幕翻译为 PRO 会员专属功能，升级后可一键翻译成多语言")
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    out_dir = Path(settings.temp_dir) / "sub" / uuid.uuid4().hex
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _extract_subtitle_sync(url, req.lang, req.is_auto, out_dir, req.bili_sessdata),
        )
        # 可选翻译
        if req.target_lang:
            text = downloader.subtitle_to_text(result["content"], result["format"])
            trans = await ai.translate(text, req.target_lang)
            result["translated"] = trans
        return {"ok": True, **result}
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except ai.LLMError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "llm"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _extract_subtitle_sync(url: str, lang: str, is_auto: bool, out_dir: Path,
                            bili_sessdata: str | None = None) -> dict:
    return downloader.extract_subtitle(url, lang, is_auto, out_dir, bili_sessdata=bili_sessdata)


# ---------------- AI 摘要 + 问答 ----------------
# 字幕稿缓存：AI 问答每轮都需字幕，避免重复走 yt-dlp 提取；按 TTL 过期，超上限丢最旧。
_transcript_cache: dict[str, dict] = {}
_transcript_lock = threading.Lock()


def _cache_get(url: str) -> dict | None:
    with _transcript_lock:
        ent = _transcript_cache.get(url)
        if ent and (time.time() - ent["ts"]) < settings.transcript_cache_ttl_seconds:
            return ent
        return None


def _cache_set(url: str, ent: dict) -> None:
    with _transcript_lock:
        _transcript_cache[url] = ent
        if len(_transcript_cache) > 100:
            oldest = min(_transcript_cache, key=lambda k: _transcript_cache[k]["ts"])
            _transcript_cache.pop(oldest, None)


def _collect_segments_sync(url: str, bili_sessdata: str | None = None) -> tuple[list[dict], dict]:
    """提取带时间戳的字幕分段（优先手动字幕，其次自动）并缓存。

    返回 (segments, meta)。segments 为 [{start, end, text}, ...]（保留时间轴，
    供章节/思维导图/问答使用）；meta 附带 lang / is_auto / model / used_source。

    缓存 key 含 sessdata 前 8 位，避免用户换登录态后命中旧缓存。
    """
    cache_key = url + (f":{bili_sessdata[:8]}" if bili_sessdata else "")
    cached = _cache_get(cache_key)
    if cached:
        return cached["segments"], cached["meta"]

    out_dir = Path(settings.temp_dir) / "sub" / uuid.uuid4().hex
    try:
        if downloader._is_bilibili(url):
            # B 站：跳过 yt-dlp 无 cookie 探测。yt-dlp 不带 SESSDATA 探测 B 站拿到的
            # 字幕清单不可靠（可能只暴露残缺的 ai-zh 分段，甚至仅片头音乐段），会把选源
            # 误导到「纯音乐」之类的残缺口径。直接走 extract_subtitle —— 内部对 B 站分流到
            # bili_subtitle.fetch_subtitle，按「人工中文 > AI 中文」优先级 + 完整浏览器会话 +
            # 限流重试自行选源，稳定拿到完整字幕（实测人工 zh 114 条）。
            result = downloader.extract_subtitle(url, "", False, out_dir, bili_sessdata=bili_sessdata)
        else:
            payload = _probe_blocking(url)

            # 多数平台在解析期就对 probe 暴露字幕清单，走「优先手动」的常规路径。
            subs = payload.get("subtitles") or []
            if subs:
                chosen = downloader._pick_subtitle(subs, None, False) or subs[0]
                result = downloader.extract_subtitle(url, chosen["lang"], chosen["is_auto"], out_dir, bili_sessdata=bili_sessdata)
            else:
                # 解析期不暴露字幕的平台：走与 /api/subtitles 相同的「写字幕」路径：
                # 手动桶 + 不限语言（extract_subtitle 内部逐条回退），保证「有真字幕就拿到」。
                result = downloader.extract_subtitle(url, "", False, out_dir, bili_sessdata=bili_sessdata)

        segments = downloader.subtitle_to_segments(result["content"], result["format"])
        if not segments:
            # 拿到了“字幕”却解析不出任何带时间戳分段（如 B 站仅弹幕 xml、或空字稿）。
            # 此前此处会把空列表写进缓存并让 ai.summarize 走到 `没有可用的字幕文本`
            # 的 LLMError —— 前端收到的是 `llm` 错误码，既误报又污染/缓存空结果。
            # 视为「无可用字幕」，走真正的 no_subtitles。
            raise downloader._SubtitleError(
                "该视频暂无可用于摘要的字幕" + downloader.bilibili_subtitle_login_hint(url)
            )
        meta = {
            "lang": result.get("lang") or None,
            "is_auto": result["source"] == "auto",
            "model": settings.openai_model,
            "used_source": result["source"],
        }
        _cache_set(cache_key, {"segments": segments, "meta": meta, "ts": time.time()})
        return segments, meta
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


@router.post("/ai/summary")
async def summary(req: SummaryRequest, request: Request):
    user = await _ai_gate(request)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url, req.bili_sessdata)
        )
        data = await ai.summarize(segments, meta)
        _consume_ai(user, request)  # 成功才计入免费每日配额
        # ai.summarize 返回完整 dict：含 summary(全文 md 字符串)、theme/overview/
        # key_points/keywords/chapters/mindmap，且已 update(meta)。整体展开返回，
        # 前端 renderSummary 取 d.summary 渲染 Markdown。
        return {"ok": True, **data}
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except ai.LLMError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "llm"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})


@router.post("/ai/chapters")
async def chapters(req: ChaptersRequest, request: Request):
    """独立生成「章节·时间轴」（单独调用 LLM，不复用摘要）。"""
    user = await _ai_gate(request)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url, req.bili_sessdata)
        )
        chapters = await ai.generate_chapters(segments, meta)
        _consume_ai(user, request)
        return {"ok": True, "chapters": chapters, **meta}
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except ai.LLMError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "llm"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})


@router.post("/ai/mindmap")
async def mindmap(req: MindmapRequest, request: Request):
    """独立生成「思维导图」树（单独调用 LLM，不复用摘要）。"""
    user = await _ai_gate(request)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url, req.bili_sessdata)
        )
        mm = await ai.mindmap(segments, meta)
        _consume_ai(user, request)
        return {"ok": True, "mindmap": mm, **meta}
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except ai.LLMError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "llm"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})


@router.post("/ai/ask")
async def ask(req: AskRequest, request: Request):
    """对视频内容追问（SSE 流式）。逐 token 推送答案，前端据此拼接显示。"""
    # 问答为 PRO 专属：非 PRO 直接 403，不占用每日免费 AI 配额（摘要/章节/导图仍走 _ai_gate）
    user = auth.user_from_request(request)
    limit = settings.ai_pro_rate_per_min if auth.is_pro(user) else settings.ai_rate_per_min
    await _rate(request, "ai", limit)
    if not auth.is_pro(user):
        return _pro_required("AI 问答为 PRO 会员专属功能，升级后可针对视频内容自由追问")
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    async def event_stream():
        # 手写 SSE 帧（data: <json>\\n\\n），用原生 StreamingResponse —— 不依赖
        # fastapi.sse.EventSourceResponse（本版本对其 ServerSentEvent 的 .encode 处理异常）。
        # 前端用 fetch + ReadableStream 逐帧解析；json.dumps 会把 token 内换行转义，保证单帧单行。
        #
        # 及时性：把「字幕抽取 + 上下文构建」这步（冷路径可能 2s+）也挪进流里，这样请求一进来流就立即可见，
        # 先推一帧 status 让前端立刻有反应，再在生成阶段推一帧 status，最后逐 token 推 delta —— 全程无阻塞等待。
        yield f"data: {json.dumps({'status': 'preparing'})}\n\n"
        try:
            segments, _meta = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _collect_segments_sync(url, req.bili_sessdata)
            )
        except downloader._SubtitleError as exc:
            # 出错以 error 帧终止（HTTP 200，走流式错误；不补发 done，否则前端用 done 覆盖失败态）
            yield f"data: {json.dumps({'error': str(exc), 'code': 'no_subtitles'})}\n\n"
            return
        except Exception as exc:
            yield f"data: {json.dumps({'error': friendly_error(exc, req.url)['error']})}\n\n"
            return
        yield f"data: {json.dumps({'status': 'generating'})}\n\n"
        try:
            async for token in ai.generate_answer(segments, req.question, req.history):
                yield f"data: {json.dumps({'delta': token})}\n\n"
        except ai.LLMError as exc:
            # 出错时以 error 帧终止，不补发 done：否则前端会用 done 覆盖「出错」状态，把失败显示成无回答。
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return
        yield f"data: {json.dumps({'done': True})}\n\n"

    # Cache-Control/X-Accel-Buffering：阻止 nginx 等反向代理把 SSE 缓冲到收尾才一次吐出，确保逐帧流式下发。
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# 保留 v1 的纯文本收集方式（竞品/回归对比用，当前摘要已升级为分段版，此函数未被调用）。
def _collect_transcript_sync(url: str, out_dir: Path) -> str:
    """优先手动内建字幕，其次自动字幕；两者皆无抛 _SubtitleError。

    _probe_blocking() 返回的是已标准化的 payload，其 subtitles 已是 [dict] 列表，
    直接取用即可，勿再套 _subtitles_list（会二次标准化而把列表当字典误判）。
    """
    payload = _probe_blocking(url)
    subs = payload.get("subtitles") or []
    if not subs:
        raise downloader._SubtitleError("该视频无可读字幕，无法生成摘要（暂不支持本地转写）")
    # 摘要优先可读手动字幕；无则任意自动
    chosen = downloader._pick_subtitle(subs, None, False) or subs[0]
    result = downloader.extract_subtitle(url, chosen["lang"], chosen["is_auto"], out_dir)
    return downloader.subtitle_to_text(result["content"], result["format"])
