"""数据模型：Pydantic 请求/响应体 + 内存 Job 数据类。

Job 用 dataclass + threading.Lock，因为 yt-dlp 的进度回调运行在后台工作线程，
需跨线程安全地更新字段；前端通过轮询读取，无需 WebSocket。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


# ---------- 请求体 ----------
class AuthRequest(BaseModel):
    email: str
    password: str


class CheckoutRequest(BaseModel):
    plan: str  # month | year（服务端套餐表校验）


class ParseRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None


class BatchItem(BaseModel):
    url: str
    format_id: str | None = None


class BatchRequest(BaseModel):
    items: list[BatchItem]


class SubtitleRequest(BaseModel):
    url: str
    lang: str = "zh"
    is_auto: bool = False
    target_lang: str | None = None
    bili_sessdata: str | None = None  # 前端从已登录浏览器粘贴的 SESSDATA，优先于 COOKIES_FILE


class SummaryRequest(BaseModel):
    url: str
    bili_sessdata: str | None = None


class ChaptersRequest(BaseModel):
    """独立「章节·时间轴」请求（单独调用 LLM，与摘要解耦）。"""
    url: str
    bili_sessdata: str | None = None


class MindmapRequest(BaseModel):
    """独立「思维导图」请求（单独调用 LLM，与摘要解耦）。"""
    url: str
    bili_sessdata: str | None = None


class AskRequest(BaseModel):
    """AI 问答（对视频内容追问）。history 为 [{role, content}, ...]（前端维护）。"""
    url: str
    question: str
    history: list[dict[str, str]] | None = None
    bili_sessdata: str | None = None


# ---------- Job ----------
class JobStatus:
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"
    CLOSED = "closed"  # 主动取消 / 已清理


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    url: str = ""
    status: str = JobStatus.QUEUED
    title: str = ""
    thumbnail: str = ""
    duration: float = 0.0
    extractor: str = ""
    format_id: str = ""
    ext: str = ""
    needs_merge: bool = False
    progress: float = 0.0  # 0~100
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: str = ""
    filename: str = ""  # 清洗后的对外文件名(用于 Content-Disposition)
    filepath: str = ""  # 服务端绝对路径(绝不下发前端)
    filesize: int = 0
    error: str = ""  # 友好中文错误
    error_code: str = ""  # 机器码（如 pro_required），前端据此弹升级
    max_height: int = 0  # 免费用户清晰度封顶（0=不限制，PRO）
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    # 线程锁(进度回调在工作线程更新) + 取消标志
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def set_progress(self, **kwargs: Any) -> None:
        """线程安全地更新进度相关字段。"""
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict[str, Any]:
        """返回可公开给前端的字段，绝不包含 filepath/绝对路径。"""
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "status": self.status,
                "title": self.title,
                "thumbnail": self.thumbnail,
                "duration": self.duration,
                "extractor": self.extractor,
                "format_id": self.format_id,
                "ext": self.ext,
                "needs_merge": self.needs_merge,
                "progress": round(self.progress, 1),
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "speed": self.speed,
                "filename": self.filename,
                "filesize": self.filesize,
                "error": self.error,
                "error_code": self.error_code,
                "subtitles": self.subtitles,
            }
