"""yt-dlp 封装层：probe(不下载) / download(后台执行) / 格式启发式 / 进度回调 / 字幕提取。

原则：纯封装调用 yt-dlp 库，尽量不改动开源代码；所有面向用户的文案为友好中文。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import settings

logger = logging.getLogger("app.downloader")


def _is_douyin(url: str) -> bool:
    """是否抖音系链接(用无头浏览器方案走独立分支)。"""
    from . import douyin

    return douyin.is_douyin_url(url)


def _is_bilibili(url: str) -> bool:
    """是否 B 站系链接（字幕需登录态时才下发）。"""
    lower = url.lower()
    return "bilibili.com" in lower or "b23.tv" in lower


# B 站「有字幕但需登录态才下发」的引导语。加在「该视频暂无可用字幕」之后，让用户知道真相与出路。
_BILI_SUBTITLE_LOGIN_HINT = (
    "（B 站该视频确实带字幕，但字幕需登录态才下发；"
    "请按 .env 的 COOKIES_FILE 填入 B 站登录 cookie（Netscape 格式，宜含 SESSDATA）后重试）"
)


def bilibili_subtitle_login_hint(url: str) -> str:
    """B 站视频若「有字幕但需登录态才下发(need_login_subtitle)」，返回引导语；否则 ''。

    仅在取字幕失败、疑似无字幕时调用（多 1~2 次轻量 API 查询，可接受）。
    用于把「该视频暂无可用字幕」升级为准确、可操作的消息：避免用户误以为真无字幕，
    也避免把 B 站登录态的缺席归因成项目 bug。
    对「真无字幕」(need_login_subtitle=False, subs_count=0) 或非 B 站返回空，不误报。
    """
    if not _is_bilibili(url):
        return ""
    try:
        m = re.search(r"(BV[0-9A-Za-z]+)", url)
        if not m:
            return ""
        bvid = m.group(1)
        headers = {"User-Agent": settings.user_agent, "Referer": "https://www.bilibili.com/"}
        view = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers=headers,
            timeout=12,
        )
        view.raise_for_status()
        vd = view.json().get("data") or {}
        aid, cid = vd.get("aid"), vd.get("cid")
        if not aid or not cid:
            return ""
        player = httpx.get(
            "https://api.bilibili.com/x/player/wbi/v2",
            params={"bvid": bvid, "cid": cid, "aid": aid},
            headers=headers,
            timeout=12,
        )
        player.raise_for_status()
        pd = player.json().get("data") or {}
        if pd.get("need_login_subtitle"):
            return _BILI_SUBTITLE_LOGIN_HINT
        return ""
    except Exception:
        return ""


class DownloadCancelled(Exception):
    """主动取消。"""


# 平台风控/限速/连接抖动特征字符串 —— 命中即重试（B站等的 412/SSL 重置是间歇性抖动，重试可自愈）
_ANTIBOT_MARKERS = (
    "http error 412",
    "http error 429",
    "http error 403",
    "precondition failed",
    "too many requests",
    "forbidden",
    # 连接级瞬时抖动：SSL 重置 / EOF / 连接被重置 / 读超时 —— 重试通常即成功，不属确定性错误
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "connection reset",
    "connection reset by peer",
    "remote end closed",
    "connection closed",
    "read operation timed out",
    "temporary failure in name resolution",
)


def _retry_antibot(fn, max_attempts: int = 4, base_sleep: float = 2.5):
    """在遇到平台风控/限速(412/429/403)时重试，带退避。

    B站、部分平台会间歇性返回 412/429，初次尝试失败不代表失败，重试可自愈。
    确定性错误(如 404、不支持链接)不重试，直接抛出。
    """
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except DownloadError as exc:
            marker = str(exc).lower()
            if any(m in marker for m in _ANTIBOT_MARKERS) and attempt < max_attempts - 1:
                last = exc
                delay = base_sleep * (attempt + 1)
                logger.warning("遇到平台风控(%s)，%.1fs 后重试(%d/%d)", marker[:30], delay, attempt + 1, max_attempts)
                time.sleep(delay)
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("unreachable")


# =========================================================================
# 工具
# =========================================================================
def ffmpeg_available() -> bool:
    """是否可用的 ffmpeg。"""
    if settings.ffmpeg_location:
        for name in ("ffmpeg.exe", "ffmpeg"):
            if os.path.exists(os.path.join(settings.ffmpeg_location, name)):
                return True
        return False
    return shutil.which("ffmpeg") is not None


def _fmt_ext(f: dict) -> str:
    return f.get("ext") or "mp4"


def _is_audio_only(f: dict) -> bool:
    return f.get("vcodec") == "none" and f.get("acodec") != "none"


def _is_video_only(f: dict) -> bool:
    return f.get("vcodec") != "none" and f.get("acodec") == "none"


def _is_progressive(f: dict) -> bool:
    """单文件(无需合并)。平台不返回 codec 信息(如 Archive.org)时视为单文件。"""
    return not _is_video_only(f) and not _is_audio_only(f)


def _is_storyboard(f: dict) -> bool:
    """是否非视频「伪格式」（如 yt-dlp 的 sb0/sb1 故事板缩略图，ext=mhtml）。"""
    ext = str(f.get("ext") or "").lower()
    proto = str(f.get("protocol") or "").lower()
    return ext == "mhtml" or proto == "mhtml"


def _height(f: dict) -> int:
    return f.get("height") or f.get("width") or 0


def _filesize(f: dict) -> int:
    return f.get("filesize") or f.get("filesize_approx") or 0


def _build_base_params(extra: dict | None = None) -> dict:
    p: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # 关掉 stderr 噪音，进度全靠 progress_hook
        "noplaylist": True,
        "nocheckcertificate": False,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "http_headers": {"User-Agent": settings.user_agent},
    }
    if settings.ffmpeg_location:
        p["ffmpeg_location"] = settings.ffmpeg_location
    # ⚠️ Cookiefile 是运营者账号凭证,绝不能转发给字节(B站/YouTube 等正常平台才下发)。
    # 抖音同理为特殊分支,但若走到这里(如字幕提取误用 yt-dlp),跳过 cookiefile 更稳。
    is_bytedance = bool(extra and extra.get("_bytedance"))
    if settings.cookies_file and not is_bytedance:
        p["cookiefile"] = settings.cookies_file
    if settings.proxy and not is_bytedance:
        p["proxy"] = settings.proxy
    if extra:
        p.update(extra)
        p.pop("_bytedance", None)  # 私有标记不传给 yt-dlp
    return p


# =========================================================================
# 解析（不下载），供 /api/parse 使用
# =========================================================================
def probe(url: str) -> dict:
    """解析视频元数据 + 可用格式 + 字幕列表，返回可公开 payload。

    对平台风控(412/429/403)做退避重试。
    """
    info = _retry_antibot(lambda: _extract_info(url, download=False))
    return _build_payload(info)


def _extract_info(url: str, download: bool):
    # 抖音需要 a_bogus 签名(绑定浏览器环境指纹),yt-dlp 无签名器直跑必败;
    # 转交服务端无头浏览器解析(见 app/douyin.py)。仅为特殊情况,不影响其他平台。
    if _is_douyin(url):
        from . import douyin

        return douyin.resolve(url)
    with YoutubeDL(_build_base_params()) as ydl:
        return ydl.extract_info(url, download=download)


def fetch_image(url: str) -> tuple[bytes, str]:
    """服务端抓取封面图，返回 (bytes, content_type)。

    用真实 UA + 合理 Referer，规避部分 CDN 防盗链；供 /api/thumbnail 使用。
    """
    headers = {"User-Agent": settings.user_agent}
    # 对已知平台补 Referer，降低被防盗链/风控拒绝的概率
    lower = url.lower()
    if any(h in lower for h in ("bilibili.com", "hdslb.com")):
        headers["Referer"] = "https://www.bilibili.com/"
    elif any(h in lower for h in ("youtube.com", "ytimg.com", "googlevideo.com")):
        headers["Referer"] = "https://www.youtube.com/"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "image/jpeg")


def _build_payload(info: dict) -> dict:
    formats = _clean_formats(info.get("formats") or [])
    thumbnail = info.get("thumbnail") or ""
    # 若缩略图为 data: 或空，尝试从第一个带缩略图的格式取
    if not thumbnail or thumbnail.startswith("data:"):
        for f in (info.get("formats") or []):
            if f.get("thumbnail") and not str(f["thumbnail"]).startswith("data:"):
                thumbnail = f["thumbnail"]
                break
    return {
        "title": info.get("title") or "未命名视频",
        "thumbnail": thumbnail,
        "duration": info.get("duration") or 0,
        "extractor": info.get("extractor_key") or "",
        "webpage_url": info.get("webpage_url") or "",
        "ffmpeg": ffmpeg_available(),
        "formats": formats,
        "subtitles": _subtitles_list(info),
    }


def _clean_formats(raw: list[dict]) -> list[dict]:
    """过滤/去重/排序格式列表，标注 needs_merge；单文件渐进档优先展示。"""
    cleaned: dict[tuple, dict] = {}
    for f in raw:
        if _is_storyboard(f):
            continue  # 剔除 yt-dlp 的 mhtml 故事板(sb*)伪格式，只留真实可下视频
        if _is_audio_only(f):
            continue  # 不单独展示音频流，避免噪杂
        if not _height(f) and not f.get("resolution"):
            continue
        height = _height(f)
        ext = _fmt_ext(f)
        video_only = _is_video_only(f)
        key = (height, ext, video_only)
        fsize = _filesize(f)
        exists = cleaned.get(key)
        if exists is None or fsize > exists.get("filesize", 0):
            cleaned[key] = {
                "format_id": f.get("format_id") or str(f.get("format")),
                "ext": ext,
                "resolution": f"{height}p" if height else str(f.get("resolution") or ext),
                "height": height,
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": fsize,
                "needs_merge": video_only,
                "progressive": _is_progressive(f),
                "note": "高清" if height >= 1080 else ("高清" if height >= 720 else "流畅"),
            }

    out = list(cleaned.values())
    # 每个分辨率只保留一个「最佳」，先按分辨率降序，再按是否渐进(渐进优先,免合并)
    out.sort(key=lambda x: (x["height"] or 0, x["progressive"]), reverse=True)

    # 同高度去重：保留渐进档优先；若无渐进档保留 video-only
    dedup: dict[int, dict] = {}
    for x in out:
        h = x["height"] or 0
        if h not in dedup:
            dedup[h] = x
        # 已有的是 video-only 且当前是渐进 -> 替换
        elif not dedup[h]["progressive"] and x["progressive"]:
            dedup[h] = x
    result = sorted(dedup.values(), key=lambda x: (x["height"] or 0), reverse=True)

    # 无 ffmpeg 时：不展示需合并音视频的档位，只保留单文件可播放档，避免选出无法合并的格式
    if not ffmpeg_available():
        result = [x for x in result if x["progressive"]]
    return result


def _subtitles_list(info: dict) -> list[dict]:
    subs: dict[str, dict] = {}
    for lang, arr in (info.get("subtitles") or {}).items():
        arr = arr or []
        # 排除 B 站弹幕 danmaku（及纯 xml 弹幕源）：它是评论，不是可读字幕稿
        if lang.strip().lower() in ("danmaku", "弹幕") or _is_xml_only(arr):
            continue
        if arr:
            subs.setdefault(lang, {"lang": lang, "is_auto": False, "source": "manual"})
    for lang, arr in (info.get("automatic_captions") or {}).items():
        arr = arr or []
        if lang.strip().lower() in ("danmaku", "弹幕") or _is_xml_only(arr) or lang in subs:
            continue
        if arr:
            subs.setdefault(lang, {"lang": lang, "is_auto": True, "source": "auto"})
    return list(subs.values())


def _is_xml_only(arr: list) -> bool:
    """判断字幕条目是否仅含 xml（弹幕源）。"""
    exts = {str((e.get("ext") or "")).lower() for e in arr if isinstance(e, dict)}
    return bool(exts) and exts == {"xml"}


# 回退字幕时的语言优先级（中文/英文）。缺 zh-Hant / ai-zh 会让「简体+英文」之外的
# 中文视频（繁体字幕、YouTube 自动中文 ai-zh）在回退时误选英文。
_LANG_PRIORITY = ("zh-Hans", "zh-CN", "zh-Hant", "zh", "ai-zh", "en")


def _pick_subtitle(available: list[dict], preferred_lang: str | None, is_auto: bool) -> dict | None:
    """从可用字幕列表(由 _subtitles_list 生成)中选一条最优。

    优先级：用户指定(且同类型) > 用户指定(任意类型) > 中文/英文手动 > 中文/英文任意 > 手动 > 任何。
    """
    if not available:
        return None
    if preferred_lang:
        for a in available:
            if a["lang"] == preferred_lang and a["is_auto"] == is_auto:
                return a
        for a in available:
            if a["lang"] == preferred_lang:
                return a
        # 语言族近似：zh -> zh-Hans/zh-Hant 等（精确无匹配时，用相近语言优先）
        for a in available:
            if a["lang"].startswith(preferred_lang) and a["is_auto"] == is_auto:
                return a
        for a in available:
            if a["lang"].startswith(preferred_lang):
                return a
    for pl in _LANG_PRIORITY:
        for a in available:
            if a["lang"] == pl and not a["is_auto"]:
                return a
    for pl in _LANG_PRIORITY:
        for a in available:
            if a["lang"] == pl:
                return a
    manual = [a for a in available if not a["is_auto"]]
    return manual[0] if manual else available[0]


# =========================================================================
# 下载（后台线程执行）
# =========================================================================
def build_format_string(info: dict, format_id: str | None, needs_merge: bool) -> str:
    """根据用户选择的 format 构建 yt-dlp 的 format 选择表达式。"""
    formats = info.get("formats") or []
    chosen = None
    if format_id:
        for f in formats:
            if str(f.get("format_id")) == str(format_id):
                chosen = f
                break

    if chosen is None:
        # 未选择或找不到 -> 有 ffmpeg 用最佳，否则最佳渐进 mp4
        if ffmpeg_available():
            return "bestvideo+bestaudio/best"
        return "best[ext=mp4]/best"

    if _is_audio_only(chosen):
        return str(chosen.get("format_id"))

    if _is_progressive(chosen):
        return str(chosen.get("format_id"))

    # video-only -> 需合并音频
    if not ffmpeg_available():
        # 无 ffmpeg：降级到渐进档，否则允许纯视频(无音频)
        progressive = (
            f"{c.get('format_id')}" for c in formats if _is_progressive(c) and _height(c) == _height(chosen)
        )
        any_progressive = next(
            (c.get("format_id") for c in formats if _is_progressive(c)), None
        )
        if any_progressive:
            return str(any_progressive)
        return f"{chosen.get('format_id')}+bestaudio/best"

    base = str(chosen.get("format_id"))
    # 优先同分辨率渐进作为音频兜底，否则 bestaudio；最后兜底 best
    same_prog = next(
        (c.get("format_id") for c in formats if _is_progressive(c) and _height(c) == _height(chosen)),
        None,
    )
    tail = same_prog if same_prog else "bestaudio"
    return f"{base}+{tail}/{base}+bestaudio/best"


def run_download(job, info: dict):
    """阻塞式下载；在后台线程中运行。info 为已 probe 的元数据。"""
    # 抖音: 走服务端无头浏览器拿到的直链,httpx 流式下载(而非 yt-dlp)
    if info.get("_play_url"):
        from . import douyin

        job_dir = Path(settings.temp_dir) / job.id
        douyin.download(job, info, job_dir)
        _resolve_output(job, job_dir)
        return

    format_str = build_format_string(info, job.format_id, job.needs_merge)
    job.set_progress(status="downloading", ext=job.ext or info.get("ext") or "mp4")

    job_dir = Path(settings.temp_dir) / job.id
    job_dir.mkdir(parents=True, exist_ok=True)

    params = _build_base_params(
        {
            "format": format_str,
            "outtmpl": str(job_dir / "%(title).100s.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [_make_progress_hook(job)],
        }
    )
    try:
        # 下载对平台风控(412/429/403)做退避重试 —— 风控多发生在下载前的抓取阶段，重试可自愈
        _retry_antibot(lambda: _download_once(params, job.url))
    except DownloadCancelled:
        job.set_progress(status="closed")
        _cleanup_dir(job_dir)
        return

    _resolve_output(job, job_dir)


def _download_once(params: dict, url: str):
    with YoutubeDL(params) as ydl:
        ydl.download([url])


def _make_progress_hook(job):
    def hook(d: dict):
        if job._cancel.is_set():
            raise DownloadCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100) if total else job.progress
            speed = d.get("speed")
            speed_str = human_speed(speed) if speed else ""
            if d.get("ext"):
                job.set_progress(ext=d["ext"])
            job.set_progress(
                status="downloading",
                downloaded_bytes=downloaded,
                total_bytes=total,
                progress=min(pct, 99.9),
                speed=speed_str,
            )
        elif d.get("status") == "finished":
            job.set_progress(status="downloading", progress=100)

    return hook


def _resolve_output(job, job_dir: Path):
    try:
        files = [
            p
            for p in job_dir.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
        ]
    except OSError:
        files = []
    if not files:
        job.set_progress(status="error", error="下载完成但未找到输出文件")
        return
    filepath = max(files, key=lambda p: p.stat().st_size)
    job.filepath = str(filepath)
    job.filesize = filepath.stat().st_size
    job.set_progress(
        status="done",
        progress=100,
        filename=f"{safe_stem(filepath.stem)}{filepath.suffix}",
        ext=filepath.suffix.lstrip("."),
        filesize=filepath.stat().st_size,
    )


def _cleanup_dir(job_dir: Path):
    try:
        for p in job_dir.iterdir():
            p.unlink(missing_ok=True)
        job_dir.rmdir()
    except OSError:
        pass


def human_speed(bytes_per_sec: float) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:.1f}{unit}"
        bytes_per_sec /= 1024
    return f"{bytes_per_sec:.1f}TB/s"


def safe_stem(name: str) -> str:
    """对外文件名清洗（去扩展名的 stem，交给 security.sanitize 更彻底，这里仅兜底）。"""
    from .security import sanitize_filename

    return sanitize_filename(name, default="video")


# =========================================================================
# 字幕提取
# =========================================================================
def extract_subtitle(url: str, lang: str, is_auto: bool, out_dir: Path) -> dict:
    """提取指定语言字幕并返回 {lang, source, format, content}。

    若用户指定的语言/类型在该视频上不存在（很多视频只有自动字幕、或只有英文），
    则从解析到的可用字幕里自动回退一条，保证「能下就下」。

    抖音暂无公开可读字幕(yt-dlp 在其上无签名器),直接返回空文案而非报错。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if _is_douyin(url):
        raise _SubtitleError("抖音视频暂不支持字幕提取/摘要")

    def _extract(use_auto: bool, use_lang: str) -> dict:
        langs = [use_lang] if use_lang else []
        params = _build_base_params(
            {
                "writesubtitles": not use_auto or None,
                "writeautomaticsub": use_auto or None,
                "subtitleslangs": langs,
                "subtitlesformat": "srt",
                "skip_download": True,
                "noplaylist": True,
                "outtmpl": str(out_dir / "%(title).100s.%(ext)s"),
            }
        )
        with YoutubeDL(params) as ydl:
            try:
                # 风控(412/429/403)退避重试
                def _sub_extract():
                    return ydl.extract_info(url, download=True)  # skip_download 使其只写字幕

                return _retry_antibot(_sub_extract)
            except DownloadError as exc:
                raise _SubtitleError(friendly_subtitle_error(exc)) from exc

    def _finalize(use_auto: bool, use_lang: str) -> dict | None:
        files = list(out_dir.glob("*"))
        if not files:
            return None
        # 排除弹幕 xml（B 站会恒产出体积很大的 danmaku.xml，非字幕稿，会盖掉真字幕）。
        # 只剩 xml 时视为无字幕（避免把弹幕当字幕喂给 LLM/前端）。
        candidates = [p for p in files if p.suffix.lower() != ".xml"]
        if candidates:
            target = max(candidates, key=lambda p: p.stat().st_size)
        else:
            return None
        ext = target.suffix.lstrip(".").lower()
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "lang": use_lang,
            "source": "auto" if use_auto else "manual",
            "format": ext,
            "content": content,
        }

    # 1) 按用户指定先取一次（拿到的 info 本身含可用字幕清单，供后续回退复用）
    info = None
    try:
        info = _extract(is_auto, lang)
        result = _finalize(is_auto, lang)
        if result:
            return result
    except _SubtitleError:
        info = None

    # 2) 未产出或失败 -> 从可用字幕清单里按优先级逐条回退，直到成功
    avail = _subtitles_list(info) if info is not None else _subtitles_list(_extract_info(url, download=False))
    last: _SubtitleError | None = None
    for cand in _ordered_sub_candidates(avail, lang, is_auto):
        # 已尝试过的组合跳过
        if cand["lang"] == lang and cand["is_auto"] == is_auto:
            continue
        try:
            _extract(cand["is_auto"], cand["lang"])
            result = _finalize(cand["is_auto"], cand["lang"])
            if result:
                return result
        except _SubtitleError as exc:
            last = exc
            logger.warning("字幕回退候选 %s(auto=%s) 失败: %s", cand["lang"], cand["is_auto"], exc)
            continue
    if last is not None:
        raise _SubtitleError(str(last) + bilibili_subtitle_login_hint(url))
    raise _SubtitleError("该视频暂无可用字幕" + bilibili_subtitle_login_hint(url))


def _ordered_sub_candidates(avail: list[dict], lang: str | None, is_auto: bool) -> list[dict]:
    """按优先级排出字幕候选（去重），供 extract_subtitle 逐条尝试。

    顺序：精确(语言+类型) > 精确语言 > 语言族近似(zh->zh-Hans/zh-Hant) > 中/英手动 > 中/英任意 > 手动 > 任意。
    """
    if not avail:
        return []
    seen: set[tuple[str, bool]] = set()
    out: list[dict] = []

    def add(a: dict) -> None:
        if not a.get("lang"):
            return
        key = (a["lang"], a["is_auto"])
        if key not in seen:
            seen.add(key)
            out.append(a)

    if lang:
        for a in avail:
            if a["lang"] == lang and a["is_auto"] == is_auto:
                add(a)
        for a in avail:
            if a["lang"] == lang:
                add(a)
        for a in avail:
            if a["lang"].startswith(lang) and a["is_auto"] == is_auto:
                add(a)
        for a in avail:
            if a["lang"].startswith(lang):
                add(a)
    for pl in _LANG_PRIORITY:
        for a in avail:
            if a["lang"] == pl and not a["is_auto"]:
                add(a)
    for pl in _LANG_PRIORITY:
        for a in avail:
            if a["lang"] == pl:
                add(a)
    for a in avail:
        if not a["is_auto"]:
            add(a)
    for a in avail:
        add(a)
    return out


class _SubtitleError(Exception):
    pass


def friendly_subtitle_error(exc: Exception) -> str:
    s = str(exc).lower()
    if "no subtitles" in s or "not available" in s or "subtitles" in s and "not found" in s:
        return "该视频暂无可用字幕"
    return "字幕提取失败，可能是平台限制或不支持该语言"


def subtitle_to_text(content: str, fmt: str) -> str:
    """把 srt/vtt 字幕转成纯文本（去掉时间戳与序号），供 AI 摘要使用。"""
    if fmt == "vtt":
        content = content.split("WEBVTT", 1)[-1]
        lines = content.splitlines()
    else:  # srt
        lines = content.splitlines()
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if "-->" in ln:  # 时间轴行
            continue
        if ln.isdigit():  # 序号行
            continue
        if ln.lower().startswith(("www.", "http")) and "://" in ln:
            continue
        out.append(ln)
    text = "\n".join(out)
    return text.strip()


def _parse_timestamp(ts: str) -> float:
    """把 SRT/VTT 时间戳 'HH:MM:SS,mmm' / 'HH:MM:SS.mmm' / 'MM:SS' 转成秒(float)。"""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
    except ValueError:
        return 0.0
    return 0.0


def subtitle_to_segments(content: str, fmt: str) -> list[dict]:
    """把 srt/vtt 字幕解析为带时间戳的分段 [{start, end, text}]。

    与 ``subtitle_to_text`` 不同：它**保留时间轴**（start/end 为秒），供「章节 + 时间戳」
    类摘要使用。已有字幕但分段为空时返回 []，由调用方决定报错或兜底。
    """
    if fmt == "vtt":
        content = content.split("WEBVTT", 1)[-1]
    if not content or not content.strip():
        return []

    # 每条字幕以空行分隔的一个块为单位
    blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]
    segments: list[dict] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines()]
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        start_s, _, end_s = lines[timing_idx].partition("-->")
        start, end = _parse_timestamp(start_s), _parse_timestamp(end_s)
        body = []
        for ln in lines[timing_idx + 1:]:
            ln = ln.strip()
            if not ln or ln.isdigit():
                continue
            if "://" in ln and ln.lower().startswith(("http", "www.")):
                continue
            body.append(ln)
        text_body = "\n".join(body).strip()
        if not text_body:
            continue
        segments.append({"start": start, "end": end, "text": text_body})
    return segments
