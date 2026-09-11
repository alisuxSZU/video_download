# -*- coding: utf-8 -*-
"""B 站字幕直调官方 API 提取（绕过 yt-dlp，稳定拿 AI 字幕 ai-zh）。

============================================================
为什么有这个模块
============================================================
yt-dlp 对 B 站 AI 字幕（lan=ai-zh）支持不稳：部分视频（need_login_subtitle=True）
在无登录态时 yt-dlp 拿不到真实字幕 URL，只产出弹幕 XML；长视频 AI 字幕被分段时
yt-dlp 也只拿到开头一小段。本模块直调 B 站官方 Web 接口，稳定获取完整字幕（含
AI 字幕），并把 body 数组转成标准 srt（带时间戳），下游 subtitle_to_segments /
章节时间轴 / AI 问答分钟级定位全兼容复用，零改动。

------------------------------------------------------------
接口流程（共 3 步，2026-09 实测有效，参考 bilibili-API-collect/docs/video/player.md）
------------------------------------------------------------
1. GET https://api.bilibili.com/x/web-interface/view?bvid=xxx        （免登录）
   → 拿 cid / aid / title / desc
2. GET https://api.bilibili.com/x/player/v2?aid=&cid=&bvid=          （需 SESSDATA）
   → 拿 data.subtitle.subtitles[]（每项含 lan/lan_doc/subtitle_url/ai_status）
   → data.need_login_subtitle：字幕是否必须登录才能查看
   ⚠️ 用 /x/player/v2（非 wbi/v2）！wbi/v2 带 SESSDATA 必 412 风控，
      player/v2 带 SESSDATA 稳定返回完整字幕列表 + 有效 subtitle_url。
3. GET https:{subtitle_url}                                          （免 cookie）
   → 字幕 JSON 本体 {"body": [{"from": 0.04, "to": 0.58, "content": "你好"}, ...]}
   ⚠️ subtitle_url 是协议相对地址（//aisubtitle.hdslb.com/...），需补 "https:" 前缀；
     URL 自带 auth_key 时效签名，下载这步不需要登录态。

------------------------------------------------------------
几个关键的实测结论（踩坑记录）
------------------------------------------------------------
a. 字幕列表必须登录：不带 SESSDATA 时 subtitles 恒为 []，游客拿不到。
   try_look=1 只对 playurl(视频流)接口有效，对字幕无效。
b. 必须用 /x/player/v2（非 wbi/v2）：wbi/v2 带 SESSDATA 必触发 412 风控
   （request was banned），哪怕加完整指纹头、完整 cookie 集、WBI 签名也不行。
   而 player/v2 带 SESSDATA 稳定返回，subtitle_url 也有效（含 AI 字幕）。
c. 字幕挑选优先级：人工中文(zh-Hans/zh-Hant 等) > AI 中文(ai-zh) > 英文(en) > 列表第一条。
d. 长视频 AI 字幕会被分段返回：subtitles 列表里同 lan 可能有多条，必须下载全部
   并按 from 时间排序、去重拼接，否则只拿到开头一小段。
e. 限流假残缺：批量时 subtitle_url 可能空串、或 body 为空 —— 不是真无字幕，
   需递增重试，且每次重取 player/v2 拿新 url（旧 url 带 auth_key 会过期）。

------------------------------------------------------------
SESSDATA 来源
------------------------------------------------------------
从 settings.cookies_file（Netscape cookies.txt，与 yt-dlp 共用）解析 SESSDATA。
未配置 / 无 SESSDATA 时抛 LoginRequiredError，上层返回带登录引导语的 no_subtitles。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("app.bili_subtitle")

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"       # 视频信息（拿 cid，免登录）
PLAYER_URL = "https://api.bilibili.com/x/player/v2"                # 字幕列表（需 SESSDATA；wbi/v2 带 SESSDATA 必 412 风控，v2 稳定）
TIMEOUT = 15  # 秒

# B 站「有字幕但需登录态才下发」的引导语。加在「该视频暂无可用字幕」之后，让用户知道真相与出路。
# 前端在 B 站视频点摘要/字幕且收到 no_subtitles 时，会自动弹出 SESSDATA 粘贴模态框。
_LOGIN_HINT = (
    "（B 站该视频确实带字幕，但字幕需登录态才下发；"
    "请在弹出的窗口粘贴 B 站 SESSDATA（浏览器 DevTools → Application → Cookies → "
    "bilibili.com → SESSDATA 复制值；一次粘贴即可），"
    "或按 .env 的 COOKIES_FILE 填入含 SESSDATA 的 B 站登录 cookie 后重试。"
    "若刚已登录仍失败，可能是触发了 B 站临时风控，请稍后再试）"
)


class BiliSubtitleError(Exception):
    """B 站字幕提取失败（可回退 yt-dlp）。"""


class LoginRequiredError(BiliSubtitleError):
    """字幕需登录态才能查看（未配 SESSDATA 或失效），上层应返回登录引导语。"""


# =========================================================================
# 工具
# =========================================================================
def _is_bilibili(url: str) -> bool:
    """是否 B 站系链接（含 b23.tv 短链）。"""
    lower = url.lower()
    return "bilibili.com" in lower or "b23.tv" in lower


def _headers(bvid: str = "") -> dict:
    """完整浏览器指纹头。

    B 站带 SESSDATA 时风控严格——缺头会触发 412 Precondition Failed / -352。
    补齐 Accept / Sec-Fetch-* / Sec-Ch-Ua 等 Chromium 指纹头可绕过。
    """
    ua = settings.user_agent
    referer = f"https://www.bilibili.com/video/{bvid}/" if bvid else "https://www.bilibili.com/"
    return {
        "User-Agent": ua,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "X-Requested-With": "XMLHttpRequest",
    }


def _extract_bvid(url: str) -> str | None:
    """从 URL 提取 BV 号。

    普通链接直接正则匹配；b23.tv 短链需跟随重定向拿真实 URL 再匹配。
    """
    m = re.search(r"(BV[0-9A-Za-z]+)", url)
    if m:
        return m.group(1)
    # b23.tv 短链：跟随重定向拿真实 bilibili.com/video/BVxxx
    if "b23.tv" in url.lower():
        try:
            r = httpx.get(url, headers=_headers(), timeout=TIMEOUT, follow_redirects=True)
            m = re.search(r"(BV[0-9A-Za-z]+)", str(r.url))
            if m:
                return m.group(1)
        except Exception as exc:
            logger.warning("解析 b23.tv 短链失败: %s", exc)
    return None


def _load_sessdata() -> str:
    """从 settings.cookies_file（Netscape cookies.txt）解析 SESSDATA。

    Netscape 格式每行 tab 分隔：domain  flag  path  secure  expiration  name  value
    返回 SESSDATA 值；无 cookies 文件 / 无 SESSDATA 返回空串。
    """
    path = settings.cookies_file
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5].strip() == "SESSDATA":
                    return parts[6].strip()
    except Exception as exc:
        logger.warning("读取 cookies 文件 %s 失败: %s", path, exc)
    return ""


def _seconds_to_srt_time(s: float) -> str:
    """秒(float) → SRT 时间戳 HH:MM:SS,mmm。"""
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# =========================================================================
# 第 1 步：view 接口拿 cid / aid / title（免登录）
# =========================================================================
def _view_info(bvid: str) -> dict:
    """GET /x/web-interface/view?bvid= → {aid, cid, title, desc, bvid}。

    无字幕也拿得到（这是视频基础信息接口）。
    """
    r = httpx.get(
        VIEW_URL,
        params={"bvid": bvid},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise BiliSubtitleError(f"获取视频信息失败: code={data.get('code')} msg={data.get('message')}")
    vdata = data["data"]
    return {
        "bvid": vdata.get("bvid", bvid),
        "aid": vdata.get("aid", 0),
        "cid": vdata.get("cid", 0),
        "title": vdata.get("title", ""),
        "desc": vdata.get("desc", ""),
        "duration": int(vdata.get("duration") or 0),
    }


# =========================================================================
# 第 2 步：player/v2 拿字幕列表（需 SESSDATA，完整浏览器会话 + 递增重试）
# =========================================================================
def _player_subtitle(bvid: str, cid: int, aid: int, sessdata: str) -> dict:
    """GET /x/player/v2?aid=&cid=&bvid= → {subtitles[], need_login_subtitle}。

    ⚠️ 关键选型：用 /x/player/v2（非 wbi/v2）。
    wbi/v2 接口带 SESSDATA 会触发 B 站 412 风控（request was banned），
    而 player/v2 带 SESSDATA 稳定返回完整字幕列表 + 有效 subtitle_url。

    ⚠️ 必须用完整浏览器会话：先访问首页 + 视频页拿 buvid3/b_nut，再加 SESSDATA。
    直接调 player/v2 可能拿到 subtitle_url 全空（B 站限流假残缺），
    完整会话模拟真实浏览器行为，大幅降低限流概率。

    ⚠️ subtitle_url 全空时递增重试（最多 5 次，间隔递增）。
    """
    def _fetch_once(client: httpx.Client) -> dict:
        r = client.get(
            PLAYER_URL,
            params={"aid": aid, "cid": cid, "bvid": bvid},
            headers=_headers(bvid),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise BiliSubtitleError(f"获取字幕列表失败: code={data.get('code')} msg={data.get('message')}")
        pd = data.get("data") or {}
        sub_obj = pd.get("subtitle") or {}
        subs = sub_obj.get("subtitles") or []
        return {
            "subtitles": subs,
            "need_login_subtitle": bool(pd.get("need_login_subtitle")),
        }

    # 完整浏览器会话：先访问首页 + 视频页 → 拿 buvid3/b_nut → 加 SESSDATA
    client = httpx.Client(headers=_headers(bvid), follow_redirects=True, timeout=TIMEOUT)
    try:
        # 访问首页（拿 buvid3/b_nut）
        client.get("https://www.bilibili.com/")
        # 访问视频页（可能拿更多 cookie）
        client.get(f"https://www.bilibili.com/video/{bvid}/")
        # 加 SESSDATA
        if sessdata:
            client.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")

        # 递增重试：subtitle_url 全空时重取。克制次数（最多 3 次）——
        # 限流时疯狂重取只会火上浇油，且实测强风控期重取也只拿到跨视频脏资源。
        for attempt in range(3):
            result = _fetch_once(client)
            subs = result["subtitles"]
            # 有字幕且至少有一条带 URL → 成功
            if subs and any(s.get("subtitle_url") for s in subs):
                return result
            # 无字幕：不需要重试（游客状态就是空）
            if not subs:
                return result
            # 有字幕但 URL 全空 → 限流假残缺，递增退避后重试
            logger.warning("player/v2 字幕 URL 全空(疑似限流)，第 %d 次重试", attempt + 1)
            time.sleep(2 + attempt * 3)  # 2/5s
        # 重试后仍空，返回最后一次结果（下游 _span_plausible 会拦截脏内容）
        return result
    finally:
        client.close()


# =========================================================================
# 字幕选源优先级
# =========================================================================
def _pick_subtitle(subtitles: list[dict], preferred_lang: str | None, is_auto: bool) -> dict | None:
    """按优先级从字幕列表挑一条最优（并返回同 lan 的全部分段条目）。

    优先级：用户指定(同类型) > 用户指定(任意类型) > 人工中文(zh-*) > AI 中文(ai-zh)
            > 英文(en) > 列表第一条。
    返回选中条目；subtitles 为空返回 None。
    """
    if not subtitles:
        return None

    def pick(lang_pred) -> dict | None:
        for s in subtitles:
            if lang_pred(s.get("lan", "")):
                return s
        return None

    if preferred_lang:
        pl = preferred_lang
        # 精确语言 + 同类型
        for s in subtitles:
            if s.get("lan") == pl and _is_auto_track(s) == is_auto:
                return s
        # 精确语言
        c = pick(lambda l: l == pl)
        if c:
            return c
        # 语言族近似（zh -> zh-Hans/zh-Hant/ai-zh）
        c = pick(lambda l: l.startswith(pl))
        if c:
            return c
    # 人工中文（zh 开头但非 ai-）
    c = pick(lambda l: l.startswith("zh") and not l.startswith("ai-"))
    if c:
        return c
    # AI 中文
    c = pick(lambda l: l.startswith("ai-zh"))
    if c:
        return c
    # 英文
    c = pick(lambda l: l == "en" or l.startswith("en-"))
    if c:
        return c
    # 兜底：第一条
    return subtitles[0]


def _is_auto_track(sub: dict) -> bool:
    """是否 AI/自动字幕（lan 以 ai- 开头，或 ai_type 存在）。"""
    lan = (sub.get("lan") or "").lower()
    return lan.startswith("ai-") or bool(sub.get("ai_type"))


def _same_lang_tracks(subtitles: list, chosen: dict) -> list[dict]:
    """取出与 chosen 同 lan 的全部分段条目（长视频 AI 字幕会被分段，需全部下载合并）。"""
    lan = chosen.get("lan", "")
    same = [s for s in subtitles if s.get("lan") == lan]
    return same or [chosen]


def _ordered_candidates(subtitles: list[dict], preferred_lang: str | None, is_auto: bool) -> list[dict]:
    """按选源优先级返回「去重 lan」的候选代表轨（人工中文 > AI 中文 > 英文 > 其他）。

    供 fetch_subtitle 严格按优先级逐语言尝试，避免在重试中跨 lan 偷换——
    此前 `同lan条目 or 全量列表` 的兜底会把人工中文静默换成限流期的脏 AI 轨
    （实测拿到过别的视频内容）。
    """
    remaining = list(subtitles)
    ordered: list[dict] = []
    seen: set[str] = set()
    while remaining:
        pick = _pick_subtitle(remaining, preferred_lang, is_auto)
        if not pick:
            break
        lan = pick.get("lan", "")
        if lan and lan not in seen:
            seen.add(lan)
            ordered.append(pick)
        remaining = [s for s in remaining if s.get("lan", "") != lan]
    return ordered


def _span_plausible(merged: list[dict], duration: int) -> bool:
    """字幕时间跨度是否与视频时长大体匹配（识别限流期下发的「别的视频」脏资源）。"""
    if not merged or duration <= 0:
        return True  # 无时长信息时不拦截
    if duration < 30:  # 超短视频不校验
        return True
    span = max(float(it.get("to", 0) or 0) for it in merged)
    return duration * 0.35 <= span <= duration * 1.6


def _download_lang_body(
    bvid: str, cid: int, aid: int, sessdata: str, lan: str, subs: list[dict]
) -> list[list[dict]]:
    """严格只下载指定 lan 的全部分段 body，返回 list[body]；拿不到返回 []。

    重取 player/v2 时也严格限定同 lan，绝不跨 lan 降级（防止静默换成脏 AI 轨）。
    同 lan 多分段共享重试计数，避免无限重试。
    """
    tracks = [s for s in subs if s.get("lan") == lan]
    if not tracks:
        return []
    bodies: list[list[dict]] = []
    retry = 0
    for track in tracks:
        sub_url = track.get("subtitle_url", "")
        body = _download_body(sub_url) if sub_url else []
        while not body and retry < 2:
            retry += 1
            time.sleep(2 + retry * 3)  # 5/8s
            try:
                p2 = _player_subtitle(bvid, cid, aid, sessdata)
                same = [s for s in p2["subtitles"] if s.get("lan") == lan]
                if same:
                    u = same[0].get("subtitle_url", "")
                    body = _download_body(u) if u else []
            except Exception as exc:
                logger.warning("重取 %s 字幕列表失败(第 %d 次): %s", lan, retry, exc)
        if body:
            bodies.append(body)
    return bodies


# =========================================================================
# 第 3 步：下载字幕 body（免 cookie，带重试处理限流假残缺）
# =========================================================================
def _download_body(subtitle_url: str) -> list[dict]:
    """GET https:{subtitle_url} → body 数组 [{from, to, content}, ...]。

    subtitle_url 是协议相对地址(//开头)，补 https: 前缀；
    URL 自带 auth_key 时效签名，下载这步不需要登录态。
    body 为空时视为限流假残缺，重试（递增退避）。
    """
    if not subtitle_url:
        return []
    if subtitle_url.startswith("//"):
        subtitle_url = f"https:{subtitle_url}"
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.get(subtitle_url, headers=_headers(), timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json().get("body") or []
            if body:
                return body
            # body 为空 → 限流假残缺，递增退避后重试
            logger.warning("字幕 body 为空(疑似限流)，第 %d 次重试", attempt + 1)
        except Exception as exc:
            last_exc = exc
            logger.warning("下载字幕 body 失败(第 %d 次): %s", attempt + 1, exc)
        time.sleep(3 + attempt * 3)  # 递增：3/6/9
    if last_exc:
        raise BiliSubtitleError(f"下载字幕 body 失败: {last_exc}")
    return []  # 重试后仍空


def _merge_bodies(bodies: list[list[dict]]) -> list[dict]:
    """合并多分段 body：按 from 时间排序、去重（相同 from+to+content 视为重复）。

    长视频 AI 字幕会被 B 站分段返回，需把同 lan 各分段的 body 合并为一条完整时间轴。
    """
    seen: set[tuple[float, float, str]] = set()
    merged: list[dict] = []
    for body in bodies:
        for item in body:
            try:
                key = (round(float(item.get("from", 0)), 3),
                       round(float(item.get("to", 0)), 3),
                       str(item.get("content", "")).strip())
            except (TypeError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "from": float(item.get("from", 0)),
                "to": float(item.get("to", 0)),
                "content": str(item.get("content", "")).strip(),
            })
    merged.sort(key=lambda x: x["from"])
    return merged


def _body_to_srt(body: list[dict]) -> str:
    """body 数组 → 标准 SRT 文本（带时间戳），下游 subtitle_to_segments 直接用。

    格式：
        1
        00:00:00,040 --> 00:00:00,580
        你好

        2
        ...
    """
    blocks: list[str] = []
    for idx, item in enumerate(body, 1):
        start = _seconds_to_srt_time(float(item.get("from", 0)))
        end = _seconds_to_srt_time(float(item.get("to", 0)))
        content = str(item.get("content", "")).strip()
        blocks.append(f"{idx}\n{start} --> {end}\n{content}")
    return "\n\n".join(blocks)


# =========================================================================
# 主流程
# =========================================================================
def fetch_subtitle(url: str, lang: str, is_auto: bool, sessdata_override: str | None = None) -> dict | None:
    """B 站字幕直调主流程：URL → cid → 字幕列表 → 选源 → 下载合并 → 转 srt。

    返回 {lang, source, format, content}（与 downloader.extract_subtitle 同结构，零改动兼容）；
    无字幕返回 None（真无字幕）；需登录态抛 LoginRequiredError。

    sessdata_override：前端从已登录浏览器粘贴的 SESSDATA，优先于 COOKIES_FILE。
    """
    bvid = _extract_bvid(url)
    if not bvid:
        raise BiliSubtitleError("无法从链接提取 B 站 BV 号")

    info = _view_info(bvid)
    cid, aid = info.get("cid"), info.get("aid")
    if not cid or not aid:
        raise BiliSubtitleError(f"未取到 cid/aid: {info}")

    # SESSDATA 优先级：前端传入 > COOKIES_FILE
    sessdata = sessdata_override or _load_sessdata()
    player = _player_subtitle(bvid, cid, aid, sessdata)
    subs = player["subtitles"]
    need_login = player["need_login_subtitle"]

    if not subs:
        # 字幕列表空：需登录态(未配/失效 SESSDATA) → 引导配置；否则真无字幕
        if need_login or not sessdata:
            raise LoginRequiredError("字幕需登录态才下发")
        return None

    # 按优先级构造「去重 lan」候选轨（人工中文 > AI 中文 > 英文 > 其他）
    candidates = _ordered_candidates(subs, lang or None, is_auto)
    duration = int(info.get("duration") or 0)

    # 严格按候选语言逐个尝试：某语言 URL 空 / body 空才降级到下一语言，绝不在重试中
    # 跨语言偷换。下载到内容但时间跨度与视频明显不符（限流期可能下发「别的视频」脏资源，
    # 实测出现过帕尼尼减肥视频字幕串到出海视频上）则跳过该语言。
    for cand in candidates:
        cand_lan = cand.get("lan", "")
        bodies = _download_lang_body(bvid, cid, aid, sessdata, cand_lan, subs)
        if not bodies:
            continue
        merged = _merge_bodies(bodies)
        if not merged:
            continue
        if not _span_plausible(merged, duration):
            logger.warning(
                "字幕轨 %s 时间跨度与视频(%ss)不符，疑似限流脏数据，尝试下一候选",
                cand_lan, duration,
            )
            continue
        srt = _body_to_srt(merged)
        return {
            "lang": cand.get("lan", lang or ""),
            "source": "auto" if _is_auto_track(cand) else "manual",
            "format": "srt",
            "content": srt,
        }

    # 所有候选都拿不到 body（限流）或全是脏数据 → 抛错触发上层重试/回退；
    # 宁可提示重试，也不返回跨视频的错误内容。
    raise BiliSubtitleError("字幕 body 全部为空或与视频不符（疑似限流，稍后重试）")


def login_hint(url: str) -> str:
    """B 站视频若「有字幕但需登录态才下发(need_login_subtitle)」，返回引导语；否则 ''。

    下沉自原 downloader.bilibili_subtitle_login_hint，逻辑一致：
    仅在取字幕失败、疑似无字幕时调用（多 1~2 次轻量 API 查询，可接受）。
    对「真无字幕」(need_login_subtitle=False, subs_count=0) 或非 B 站返回空，不误报。
    """
    if not _is_bilibili(url):
        return ""
    try:
        bvid = _extract_bvid(url)
        if not bvid:
            return ""
        info = _view_info(bvid)
        cid, aid = info.get("cid"), info.get("aid")
        if not cid or not aid:
            return ""
        # 免 SESSDATA 诊断：只读 need_login_subtitle 标志（游客也可读）
        player = _player_subtitle(bvid, cid, aid, "")
        if player["need_login_subtitle"]:
            return _LOGIN_HINT
        return ""
    except Exception:
        return ""
