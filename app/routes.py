"""API 路由：解析 / 下载 / 批量 / 轮询 / 成品 / 字幕 / AI 摘要。

连接前端与后端能力，统一做限流、URL 校验、友好错误映射。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import ai, downloader, tasks
from .config import settings
from .models import (
    AskRequest,
    BatchRequest,
    ChaptersRequest,
    DownloadRequest,
    MindmapRequest,
    ParseRequest,
    SubtitleRequest,
    SummaryRequest,
    JobStatus,
)
from .security import friendly_error, RateError, validate_url, error_status

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

    job = tasks.create_job(url, req.format_id)
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

    jobs_payload = []
    for item in req.items:
        try:
            url = validate_url(item.url)
        except Exception as exc:
            jobs_payload.append({"url": item.url, "job_id": None, "status": "invalid", "error": friendly_error(exc)["error"]})
            continue
        job = tasks.create_job(url, item.format_id)
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
    await _rate(request, "ai", settings.ai_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    out_dir = Path(settings.temp_dir) / "sub" / uuid.uuid4().hex
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _extract_subtitle_sync(url, req.lang, req.is_auto, out_dir),
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


def _extract_subtitle_sync(url: str, lang: str, is_auto: bool, out_dir: Path) -> dict:
    return downloader.extract_subtitle(url, lang, is_auto, out_dir)


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


def _collect_segments_sync(url: str) -> tuple[list[dict], dict]:
    """提取带时间戳的字幕分段（优先手动字幕，其次自动）并缓存。

    返回 (segments, meta)。segments 为 [{start, end, text}, ...]（保留时间轴，
    供章节/思维导图/问答使用）；meta 附带 lang / is_auto / model / used_source。
    """
    cached = _cache_get(url)
    if cached:
        return cached["segments"], cached["meta"]

    out_dir = Path(settings.temp_dir) / "sub" / uuid.uuid4().hex
    try:
        payload = _probe_blocking(url)

        # 多数平台在解析期就对 probe 暴露字幕清单，走「优先手动」的常规路径。
        subs = payload.get("subtitles") or []
        if subs:
            chosen = downloader._pick_subtitle(subs, None, False) or subs[0]
            result = downloader.extract_subtitle(url, chosen["lang"], chosen["is_auto"], out_dir)
        else:
            # B 站等平台解析期不暴露字幕（字幕仅在写字幕时出现），probe 会误判「无字幕」。
            # 此时改走与 /api/subtitles 相同的「写字幕」路径：手动桶 + 不限语言
            # （extract_subtitle 内部逐条回退），保证「有真字幕就拿到」。仅弹幕(xml)时
            # extract_subtitle 会因 _finalize 排除 xml 而返回无字幕，诚实降级。
            result = downloader.extract_subtitle(url, "", False, out_dir)

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
        _cache_set(url, {"segments": segments, "meta": meta, "ts": time.time()})
        return segments, meta
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


@router.post("/ai/summary")
async def summary(req: SummaryRequest, request: Request):
    await _rate(request, "ai", settings.ai_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url)
        )
        result = await ai.summarize(segments, meta)
        return {"ok": True, **result}
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except ai.LLMError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "llm"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})


@router.post("/ai/chapters")
async def chapters(req: ChaptersRequest, request: Request):
    """独立生成「章节·时间轴」（单独调用 LLM，不复用摘要）。"""
    await _rate(request, "ai", settings.ai_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url)
        )
        chapters = await ai.generate_chapters(segments, meta)
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
    await _rate(request, "ai", settings.ai_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url)
        )
        mm = await ai.mindmap(segments, meta)
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
    await _rate(request, "ai", settings.ai_rate_per_min)
    try:
        url = validate_url(req.url)
    except Exception as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, **friendly_error(exc)})

    try:
        segments, _meta = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _collect_segments_sync(url)
        )
    except downloader._SubtitleError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc), "code": "no_subtitles"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, **friendly_error(exc, req.url)})

    async def event_stream():
        # 手写 SSE 帧（data: <json>\\n\\n），用原生 StreamingResponse —— 不依赖
        # fastapi.sse.EventSourceResponse（本版本对其 ServerSentEvent 的 .encode 处理异常）。
        # 前端用 fetch + ReadableStream 逐帧解析；json.dumps 会把 token 内换行转义，保证单帧单行。
        try:
            async for token in ai.generate_answer(segments, req.question, req.history):
                yield f"data: {json.dumps({'delta': token})}\n\n"
        except ai.LLMError as exc:
            # 出错时以 error 帧终止，不补发 done：否则前端会用 done 覆盖「出错」状态，把失败显示成无回答。
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
