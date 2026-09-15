"""任务调度：内存 job store + 线程池并发 + 后台过期清理。

yt-dlp 是同步阻塞调用，放到有界线程池执行(天然限制并发下载数)；
进度通过 progress_hook 写回 job，前端轮询读取。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .config import settings
from .downloader import (
    DownloadCancelled,
    ProRequiredDownloadError,
    _extract_info,
    _is_progressive,
    _is_audio_only,
    _is_video_only,
    _height,
    _retry_antibot,
    ffmpeg_available,
)
from .models import Job, JobStatus
from .security import friendly_error, sanitize_filename

logger = logging.getLogger("app.tasks")

# ---------------- 全局状态 ----------------
JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()

MAX_ACTIVE_JOBS = 100  # 防失控：在跑+排队总量上限
_thread_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.max_concurrent_downloads),
    thread_name_prefix="vdl",
)

_background: set[asyncio.Task] = set()  # 阻止后台任务被 GC


# ---------------- Job 生命周期 ----------------
def create_job(url: str, format_id: str | None = None, max_height: int = 0) -> Job:
    job = Job(url=url, format_id=format_id or "", needs_merge=False, max_height=max_height)
    job.expires_at = time.time() + settings.file_ttl_seconds
    with _JOBS_LOCK:
        JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def active_or_queued_count() -> int:
    with _JOBS_LOCK:
        return sum(
            1
            for j in JOBS.values()
            if j.status in (JobStatus.QUEUED, JobStatus.PROBING, JobStatus.DOWNLOADING)
        )


def start_download(job: Job) -> None:
    """空闲即从线程池取 worker 执行下载；池满则排队(waiting status)。"""
    # 建 job 目录
    (Path(settings.temp_dir) / job.id).mkdir(parents=True, exist_ok=True)
    _thread_pool.submit(_blocking_run, job.id)


def delete_job(job_id: str) -> bool:
    job = JOBS.get(job_id)
    if not job:
        return False
    job._cancel.set()
    # 清理目录(尽力而为)
    shutil.rmtree(Path(settings.temp_dir) / job_id, ignore_errors=True)
    with _JOBS_LOCK:
        JOBS.pop(job_id, None)
    return True


# ---------------- 阻塞后台任务 ----------------
def _blocking_run(job_id: str) -> None:
    job = JOBS.get(job_id)
    if job is None:
        return
    try:
        # 1) probe 元数据
        job.set_progress(status=JobStatus.PROBING)
        info = _probe(job.url)
        job.set_progress(
            title=info.get("title") or "未命名视频",
            thumbnail=info.get("thumbnail") or "",
            duration=info.get("duration") or 0,
            extractor=info.get("extractor_key") or "",
        )
        # 2) 依据所选 format 决定合并标记与扩展名
        _apply_format(job, info)
        # 3) 真正下载
        from .downloader import run_download

        run_download(job, info)
    except DownloadCancelled:
        job.set_progress(status=JobStatus.CLOSED)
    except ProRequiredDownloadError as exc:
        # 免费用户选了 PRO 专属清晰度：probe 之后下载之前拦截，错误带机器码供前端弹升级
        logger.info("pro required for job_id=%s: %s", job_id, exc)
        job.set_progress(status=JobStatus.ERROR, error=str(exc), error_code="pro_required")
    except Exception as exc:  # noqa: BLE001
        logger.exception("download failed job_id=%s", job_id)
        msg = friendly_error(exc, job.url)
        job.set_progress(status=JobStatus.ERROR, error=msg.get("error", "下载失败"))


def _probe(url: str) -> dict:
    # 走 downloader 的带重试提取：平台风控(412/429/403)自愈
    return _retry_antibot(lambda: _extract_info(url, download=False))


def _apply_format(job: Job, info: dict) -> None:
    formats = info.get("formats") or []
    if not job.format_id:
        # 默认选择最佳
        job.set_progress(ext="mp4")
        return
    chosen = next((f for f in formats if str(f.get("format_id")) == str(job.format_id)), None)
    if chosen is None:
        return
    video_only = _is_video_only(chosen)
    needs_merge = video_only and not _is_audio_only(chosen)
    job.needs_merge = needs_merge
    job.set_progress(ext=chosen.get("ext") or "mp4")


# ---------------- 后台清理 ----------------
async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(max(5, settings.cleanup_interval_seconds))
        now = time.time()
        temp_root = Path(settings.temp_dir)
        # 清理超 TTL 的 job 目录
        if temp_root.exists():
            for d in temp_root.iterdir():
                if not d.is_dir():
                    continue
                try:
                    latest = max((p.stat().st_mtime for p in d.iterdir()), default=0)
                except OSError:
                    continue
                if now - latest > settings.file_ttl_seconds:
                    shutil.rmtree(d, ignore_errors=True)
                    with _JOBS_LOCK:
                        JOBS.pop(d.name, None)
        # 清理内存中超期的 job（即便目录已被外部处理）
        with _JOBS_LOCK:
            stale = [
                jid
                for jid, j in JOBS.items()
                if j.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CLOSED)
                and now - j.created_at > settings.file_ttl_seconds
            ]
            for jid in stale:
                JOBS.pop(jid, None)


def start_cleanup_task() -> None:
    async def _runner():
        await cleanup_loop()

    task = asyncio.create_task(_runner())
    _background.add(task)
    task.add_done_callback(_background.discard)
