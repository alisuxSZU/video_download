"""OpenAI 兼容 LLM 客户端：字幕翻译 + 学习型结构化摘要（章节/时间戳/思维导图）+ AI 问答。

用已安装的 httpx 发起请求，不引入额外依赖。API Key 仅存服务端环境变量，绝不下发前端。
支持 DeepSeek / Zhipu(智谱) / 星火 / Qwen 等任何 OpenAI 兼容端点。

学习型摘要 v2 要点：字幕先解析为带时间戳的 segments，再
  - 短视频（时长 < AI_CHAPTER_GOAL_SECONDS(约10分钟) 且 字符 ≤ AI_SINGLE_SHOT_CHARS，
    `_should_split` 为假）→ 单次直出 主题/总览/要点/关键词（`chapters=[]`）；
  - 长视频（时长或字符达标，`_should_split` 为真）→ 自动分块成章节，每章独立摘要(map），
    再汇总(reduce)，从而形成「章节 + 时间戳」的时间轴，并派生思维导图。
  （⚠️ 仅按字符分档会把「每分钟字数少」的口述访谈误判为短片并合成整片时长的伪章节。）
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx
from fastapi import HTTPException

from .config import settings


class LLMError(Exception):
    """LLM 调用失败（友好中文）。"""


def _ensure_configured() -> tuple[str, str]:
    if not settings.openai_api_key:
        raise LLMError("AI 功能未配置，请在服务器设置 OPENAI_API_KEY")
    return settings.openai_api_key, settings.openai_model


def _status_error(status_code: int) -> str:
    if status_code == 401:
        return "AI 接口鉴权失败，请检查 OPENAI_API_KEY"
    if status_code == 429:
        return "AI 接口限流，请稍后再试"
    return f"AI 接口错误({status_code})，请稍后再试"


async def _chat(messages: list[dict]) -> str:
    api_key, model = _ensure_configured()
    url = f"{settings.openai_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # 自动重试一次：LLM 端偶发 5xx / 429 / 网络抖动会让长视频 map-reduce 其中一路失败
    # 导致整个摘要/章节/导图端点 502。仅对可重试错误重试；401/400 等确定性错误直接失败。
    last: LLMError | None = None
    for attempt in range(2):
        retryable = False
        resp = None
        try:
            async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            last = LLMError("AI 请求超时，请稍后再试")
            retryable = True
        except httpx.HTTPError as exc:
            last = LLMError("AI 服务暂时不可用，请稍后再试")
            retryable = True

        if resp is not None:
            if resp.status_code == 200:
                data = resp.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise LLMError("AI 返回格式异常，请稍后再试") from exc
            last = LLMError(_status_error(resp.status_code))
            retryable = resp.status_code == 429 or resp.status_code >= 500

        if attempt == 0 and retryable:
            await asyncio.sleep(1.0)
            continue
        assert last is not None
        raise last


async def translate(text: str, target_lang: str) -> str:
    """把字幕纯文本翻译成 target_lang（如「简体中文」「English」）。

    v0.2 重写时误删了本函数，但 `/api/subtitles` 翻译路径仍在调用 -> 任何带
    `target_lang` 的翻译请求都会 500。现按调用签名 `translate(text, target_lang)`
    复用 `_chat` 重建：超过 `AI_MAX_CHARS` 自动分块逐块翻译后按行拼接。
    """
    text = (text or "").strip()
    if not text:
        return ""

    chunk = max(2000, int(settings.ai_max_chars))
    parts = [text[i : i + chunk] for i in range(0, len(text), chunk)]
    out: list[str] = []

    for part in parts:
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是一名专业翻译。把用户提供的字幕文本翻译成{target_lang}。"
                    f"只输出译文本身，不要任何解释、序号或原文；若文本已经是{target_lang}，原样返回。"
                ),
            },
            {"role": "user", "content": part},
        ]
        # translate 本身在 /api/subtitles 已受 ai 限流，无需再在此限流
        try:
            t = await _chat(messages)
        except LLMError:
            # 某一块失败即整体失败，如实抛给前端（路由捕获后返回 llm 错误码）
            raise
        out.append(t.strip())

    return "\n".join(out)


def _as_list(value) -> list:
    """把 LLM 返回的某个字段规整为 list；非 list（字符串/数字/字典/None）一律回退空列表。

    防止形如 `[str(x) for x in (data.get("key_points") or [])]` 在模型把该字段填成
    `5`、`true`、`"a,b"` 等真值非列表时抛出 `TypeError: 'int' object is not iterable`，
    从而逃逸成 500。LLM 的 JSON 字段形状不可信，读取数组一律走这里。
    """
    return value if isinstance(value, list) else []


def _normalize_nodes(value) -> list:
    """把 LLM 返回的 children 规整为 [{title, children:[...]}, ...]（递归、类型安全）。

    容忍几种模型误填：
      * children 是字符串列表（["要点1","要点2"]）→ 包装成 {title: 字符串}；
      * children 某元素是 dict 但缺 title 却有 children → 用占位标题，避免前端拿到无标题节点；
      * children 某元素是纯空对象/数字 → 丢弃；
      * children 整体不是 list → 回退空列表。
    递归把 title 压成字符串，保证前端 buildMindmap 一定拿到 str，不会因 undefined 渲染成「…」。
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, dict):
            title = item.get("title")
            if title is None or str(title).strip() == "":
                if item.get("children"):
                    title = "要点"  # 有子节点但缺标题 → 占位
                else:
                    continue  # 纯空对象，丢弃
            node: dict = {"title": str(title)}
            kids = _normalize_nodes(item.get("children"))
            if kids:
                node["children"] = kids
            out.append(node)
        elif isinstance(item, str) and item.strip():
            out.append({"title": item.strip()})
        # 其它类型（数字 / None 等）忽略，宁缺勿乱
    return out


def _parse_json(text: str) -> dict:
    """从 LLM 返回里尽力解析出一段 JSON 对象（容忍代码块包裹/前后杂文本）。

    先整体解析（对顶层对象/数组最稳妥），失败再退化为「截取首个 { 到最后一个 } 的对象」，
    容忍模型多套一层数组（`[{"title":..}]`）或前后有多余文字。

    两条防线，避免把不可信输出漏给上层：
      1. 顶层若真的是 JSON 数组（模型把单对象包成数组 / 数组里多个对象），整体 json.loads
         能解析；单个元素的数组将其元素作为对象返回，多个元素的数组按格式异常处理。
      2. 兜底：`json.loads` 成功但结果不是 dict（裸数组/字符串/数字）→ 直接判格式异常，
         交给路由层做友好 502，而不是让下游 `.get()` 抛 AttributeError 变 500。

    DeepSeek/智谱等往往在数组/对象末尾逗号后紧接 `}`/`]`（`["a",]`、`{"k":1,}`），
    `json.loads` 会因尾逗号抛出 JSONDecodeError 而使整次摘要失败。这里先去尾逗号
    （`,}`→`}`、`,]`→`]`）再解析，多一次重试。
    """
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[^\n]*\n", "", s)
        s = s.rstrip("`").strip()

    # 候选：先原始文本（含整体数组），再截取对象区间（容忍前后杂文本）。
    candidates = [s]
    cleaned = re.sub(r",\s*([}\]])", r"\1", s)
    if cleaned != s:
        candidates.append(cleaned)

    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        obj = s[start : end + 1]
        if obj not in candidates:
            candidates.append(obj)
            obj_cleaned = re.sub(r",\s*([}\]])", r"\1", obj)
            if obj_cleaned != obj:
                candidates.append(obj_cleaned)

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        # 模型误把单个对象套了一层数组：`[{"title":..}]` → 取那个对象。
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        # 其它非对象（裸数组/字符串/数字）→ 格式异常，交上层做友好 502。
        raise LLMError("AI 返回格式异常，请稍后再试")
    raise LLMError("AI 返回格式异常，请稍后再试")


async def _chat_json(messages: list[dict]) -> dict:
    """调用 LLM 并解析出 JSON 对象。

    解析失败（LLM 偶发给非严格 JSON）自动**重新生成一次**——长视频 map-reduce 多路
    并发时某一路格式异常会让整个导图/章节端点 502（网络类错误已由 `_chat` 内部重试）。
    """
    last: LLMError | None = None
    for attempt in range(2):
        try:
            text = await _chat(messages)
            return _parse_json(text)
        except LLMError as exc:
            last = exc
            if attempt == 0 and "格式异常" in str(exc):
                await asyncio.sleep(1.0)
                continue
            raise
    assert last is not None
    raise last


async def _chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    """流式调用 LLM，逐 token 产出 content（供 SSE 问答）。"""
    api_key, model = _ensure_configured()
    url = f"{settings.openai_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise LLMError(_status_error(resp.status_code))
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta
    except httpx.TimeoutException as exc:
        raise LLMError("AI 请求超时，请稍后再试") from exc
    except httpx.HTTPError as exc:
        raise LLMError("AI 服务暂时不可用，请稍后再试") from exc


# =========================================================================
# 时间与分段工具
# =========================================================================
def fmt_time(seconds: float) -> str:
    """秒 → 'MM:SS' / 'HH:MM:SS'。"""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _segments_lines(segments: list[dict]) -> str:
    return "\n".join(f"[{fmt_time(s['start'])} - {fmt_time(s['end'])}] {s['text']}" for s in segments)


def segment_chapters(
    segments: list[dict],
    max_chars: int | None = None,
    max_chapters: int | None = None,
    goal_seconds: int | None = None,
) -> list[dict]:
    """把带时间戳的分段聚合为「章节」[{start, end, text, title?, ...}]。

    按「时间目标」切块：某章累计时长 ≥ goal_seconds（默认约 10 分钟）**或**累计字符
    达 max_chars，先到先切一刀。每章起止时间取该章首尾分段的时间，保证时间轴连续、确定。
    非时间敏感场景（纯粹为了控制 prompt 体积的上下文裁切）可仍用字符预算。

    原实现用 `len(chapters) < max_chapters - 1` 作为切分门槛：一旦章节数到达
    max_chapters-1 就不再切，后续全部并入**末章**，使末章无界——超长视频下某个
    map 步骤的 prompt 会顶爆 LLM 上下文。这里改为在到达硬上限前**始终在字符预算
    达标处切分**（硬上限远大于软目标 max_chapters，实际几乎不会触达），从而保证
    每一章（含末章）都被 `max_chars + 单段长度` 严格约束。
    """
    if not segments:
        return []
    max_chars = max_chars or settings.ai_chapter_max_chars
    max_chapters = max_chapters or settings.ai_max_chapters
    goal_seconds = goal_seconds or settings.ai_chapter_goal_seconds
    # 硬上限：软目标 max_chapters 只是章节数倾向，不以此让末章无界。
    hard_max_chapters = max_chapters * 3 + 10
    chapters: list[dict] = []
    cur_start = segments[0]["start"]
    cur_end = segments[0]["end"]
    cur_texts = [segments[0]["text"]]
    cur_chars = len(segments[0]["text"])
    for seg in segments[1:]:
        hit_goal = (seg["end"] - cur_start) >= goal_seconds
        hit_chars = cur_chars >= max_chars
        if (hit_goal or hit_chars) and len(chapters) < hard_max_chapters - 1:
            chapters.append({"start": cur_start, "end": cur_end, "text": "\n".join(cur_texts)})
            cur_start = seg["start"]
            cur_end = seg["end"]
            cur_texts = [seg["text"]]
            cur_chars = len(seg["text"])
        else:
            cur_end = seg["end"]
            cur_texts.append(seg["text"])
            cur_chars += len(seg["text"])
    final_text = "\n".join(cur_texts)
    # 兜底：万一仍落到硬上限（超长视频），把末章保守截断到 max_chars 的 2 倍，
    # 确保任何一章的 prompt 都不会顶爆上下文。截断仅发生在极端场景（> hard_max_chapters×max_chars）。
    bound = max(max_chars * 2, max_chars + 1)
    if len(final_text) > bound:
        final_text = final_text[:bound]
    chapters.append({"start": cur_start, "end": cur_end, "text": final_text})
    return chapters


def derive_mindmap(theme: str, chapters: list[dict], key_points: list[str] | None = None) -> dict:
    """从结构化摘要确定性派生思维导图树：根=主题 → 一级=章节 → 二级=章节要点。

    短视频直出无章节时回退为 根 → 顶层要点，避免空导图（仅根无分支）。
    """
    if not chapters:
        return {"title": theme, "children": [{"title": str(p)} for p in _as_list(key_points)]}
    return {
        "title": theme,
        "children": [
            {"title": str(c.get("title") or "章节"), "children": [{"title": p} for p in _as_list(c.get("key_points"))]}
            for c in chapters
        ],
    }


def _markdown(d: dict) -> str:
    """由结构化摘要渲染 Markdown 全文（供复制/下载），确定性、无额外 LLM 调用。

    摘要 = **主题 + 总览 + 要点 + 关键词**，纯叙述，**不内嵌「章节·时间轴」**：
    时间轴由独立的「章节·时间轴」面板承载。此前把摘要章节渲染成
    `### [MM:SS - MM:SS] title`，短视频会合成「仅一条覆盖整片时长的伪章节」（title=主题、
    summary=总览），导致摘要顶部与章节区各出现一遍（主人反馈「内容重复、不应该有时间轴」）。
    故摘要不再含章节段；顶层 `key_points` 直接摊平列要点。
    """
    lines = [f"# {d['theme']}", "", d.get("overview", "") or ""]
    kps = _as_list(d.get("key_points"))
    if kps:
        lines += ["", "## 要点"]
        lines += [f"- {p}" for p in kps]
    kws = _as_list(d.get("keywords"))
    if kws:
        lines += ["", "## 关键词", " ".join(str(k) for k in kws)]
    return "\n".join(lines)


# =========================================================================
# LLM 步骤（单次直出短摘要 / 每章 map / reduce）
# =========================================================================
_SYSTEM_ANALYST = (
    "你是一位视频内容分析师。用户会给你一段带时间戳的视频字幕，请提炼清晰、准确、中文的结构化知识。"
    "只输出 JSON，不要额外寒暄或代码块标记。"
)


async def _short_summary(segments: list[dict]) -> dict:
    """短视频：一次直出 主题/总览/要点/关键词。"""
    sys_prompt = _SYSTEM_ANALYST + (
        " 请输出严格 JSON：{\"theme\":\"一句话概括视频主题\",\"overview\":\"一段总览\","
        "\"key_points\":[\"要点1\",\"要点2\",...],\"keywords\":[\"关键词\",...]}"
    )
    user = f"视频字幕（含时间戳）如下：\n\n{_segments_lines(segments)}"
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return {
        "theme": str(data.get("theme") or "视频主题"),
        "overview": str(data.get("overview") or ""),
        "key_points": [str(x) for x in _as_list(data.get("key_points"))][:10],
        "keywords": [str(x) for x in _as_list(data.get("keywords"))][:10],
    }


async def _chapter_json(chapter: dict) -> dict:
    """长视频 map：单章摘要。"""
    sys_prompt = _SYSTEM_ANALYST + (
        " 请输出严格 JSON：{\"title\":\"本章标题\",\"summary\":\"本章摘要\","
        "\"key_points\":[\"要点...\",...],\"keywords\":[\"关键词\",...]}"
    )
    user = f"时间范围 [{fmt_time(chapter['start'])} - {fmt_time(chapter['end'])}] 的字幕：\n\n{chapter['text']}"
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return {
        "title": str(data.get("title") or fmt_time(chapter["start"])),
        "summary": str(data.get("summary") or ""),
        "key_points": [str(x) for x in _as_list(data.get("key_points"))][:10],
        "keywords": [str(x) for x in _as_list(data.get("keywords"))][:10],
    }


async def _map_chapters(chapters: list[dict]) -> list[dict]:
    """长视频 map：并发（受 AI_MAP_CONCURRENCY 限制）生成各章摘要。"""
    sem = asyncio.Semaphore(max(1, settings.ai_map_concurrency))

    async def run(ch: dict):
        async with sem:
            return await _chapter_json(ch)

    return await asyncio.gather(*[run(ch) for ch in chapters])


async def _reduce(chapters: list[dict]) -> dict:
    """长视频 reduce：汇总各章摘要 → 主题/总览/关键词。"""
    sys_prompt = _SYSTEM_ANALYST + (
        " 你已经得到一个个分章摘要，把它们合并成整体。请输出严格 JSON："
        "{\"theme\":\"一句话概括整段视频主题\",\"overview\":\"总体概述\",\"keywords\":[\"关键词\",...]}"
    )
    user = "各章节摘要 JSON 数组：\n\n" + json.dumps(chapters, ensure_ascii=False)
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return {
        "theme": str(data.get("theme") or "视频主题"),
        "overview": str(data.get("overview") or ""),
        "keywords": [str(x) for x in _as_list(data.get("keywords"))][:15],
        # 长视频的「要点」由各章要点聚合，保证顶层 key_points 存在（列表），
        # 否则前端摘要面板该栏会渲染为空。短路径由 _short_summary 自带。
        "key_points": _flatten_key_points(chapters),
    }


def _flatten_key_points(chapters: list[dict]) -> list[str]:
    """长视频 reduce：把各章 key_points 摊平为前若干条要点（列表）。

    前端摘要面板的「要点」在长视频路径下曾因顶层缺 key_points 而渲染为空。
    这里从分章要点取前若干条，作为一份列表供前端渲染，并保证与 key_points 列表语义一致。
    """
    pts: list[str] = []
    for c in chapters:
        for p in _as_list(c.get("key_points")):
            pts.append(str(p))
            if len(pts) >= 15:
                return pts
    return pts


# =========================================================================
# 对外：结构化摘要
# =========================================================================
def _should_split(segments: list[dict]) -> bool:
    """时间/字符双判据决定是否分块（map-reduce）。

    此前仅以字符预算 `ai_single_shot_chars` 判断「长/短视频」——但口述访谈/播客这类
    【每分钟字数少】的内容，整段 60+ 分钟的转录也可能 <30000 字，于是被误判为「短视频」
    走单次直出，再被合成一条覆盖[整片时长]的伪章节（`[00:00 - 01:02:52] 标题=主题、摘要=总览`），
    这正是主人反馈的「章节时段莫名其妙 / 前后内容重复」的根因。

    章节粒度本质由【时长】决定（既定目标 ~10 分钟一段），故以时长为主判据、字符作上下文
    体积兜底：任一触发即分块，避免上述伪章节。
    """
    if not segments:
        return False
    dur = segments[-1]["end"] - segments[0]["start"]
    chars = sum(len(s["text"]) for s in segments)
    return dur >= settings.ai_chapter_goal_seconds or chars >= settings.ai_single_shot_chars


async def summarize(segments: list[dict], meta: dict) -> dict:
    """由带时间戳的字幕分段生成结构化摘要。

    返回：{theme, overview, chapters[], keywords[], mindmap{}, summary(全文md), **meta}
    meta 通常为 {lang, model, used_source}。
    """
    if not segments:
        raise LLMError("没有可用的字幕文本，无法生成摘要")

    if not _should_split(segments):
        # 短视频（时间与字符都短）：单次直出，**不合成伪章节**——只给 主题/总览/要点/关键词，
        # 修「整片时长[00:00 - 01:02:52] + 标题=主题 + 摘要=总览」的重复伪章节。
        short = await _short_summary(segments)
        data = {**short, "chapters": []}
    else:
        # 长视频（时长或字符达阈值）：分块 map-reduce，得到 ~10 分钟一段的真实时间轴
        chapters = segment_chapters(segments)
        mapped = await _map_chapters(chapters)
        reduced = await _reduce([{"start": c["start"], "end": c["end"], **m} for c, m in zip(chapters, mapped)])
        data = {
            **reduced,
            "chapters": [
                {
                    "start": c["start"],
                    "end": c["end"],
                    **m,
                }
                for c, m in zip(chapters, mapped)
            ],
        }

    data["mindmap"] = derive_mindmap(data.get("theme", ""), data.get("chapters", []), data.get("key_points"))
    data["summary"] = _markdown(data)
    data.update(meta)
    return data


# =========================================================================
# 对外：独立「章节·时间轴」与「思维导图」（各自单独调用 LLM，不复用摘要）
# =========================================================================
async def generate_chapters(segments: list[dict], meta: dict) -> list[dict]:
    """独立生成「章节·时间轴」：按字符预算分块，每个时间块单独调用 LLM
    生成 标题/摘要/要点，得到 [{start, end, title, summary, key_points, keywords}, ...]。

    与 summarize 解耦：前端「章节·时间轴」面板可单独触发，不复用摘要，不额外耦合。
    """
    if not segments:
        raise LLMError("没有可用的字幕文本，无法生成章节时间轴")
    blocks = segment_chapters(segments)
    mapped = await _map_chapters(blocks)
    return [{"start": c["start"], "end": c["end"], **m} for c, m in zip(blocks, mapped)]


async def mindmap(segments: list[dict], meta: dict) -> dict:
    """独立调用 LLM 生成「思维导图」树 {title, children:[{title, children:[...]}]}。

    专供前端「思维导图」面板，与摘要解耦。短字幕一次直出；超长字幕先分块逐块生成
    子树（map），再做一次根节点标题的汇总（reduce），保证任意章节 prompt 均受控。
    """
    if not segments:
        raise LLMError("没有可用的字幕文本，无法生成思维导图")
    if not _should_split(segments):
        tree = await _mindmap_shot(segments)
    else:
        blocks = segment_chapters(segments)
        subtrees = await _map_mindmap(blocks)
        root = await _mindmap_root_title(subtrees)
        tree = {"title": root, "children": subtrees}
    return tree


async def _mindmap_shot(segments: list[dict]) -> dict:
    """短视频：从整段字幕一次生成一棵导图树。"""
    sys_prompt = _SYSTEM_ANALYST + (
        " 请输出严格 JSON 思维导图（不要代码块、不要解释）："
        "{\"title\":\"根节点(一句话主题)\",\"children\":[{\"title\":\"一级分支\","
        "\"children\":[{\"title\":\"二级要点\"},...]},...]}"
        " 根下不超过 6 个一级分支，层级不超过 3 层，节点用短语不要长句。"
    )
    user = f"视频字幕（含时间戳）如下：\n\n{_segments_lines(segments)}"
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return {"title": str(data.get("title") or "视频主题"), "children": _normalize_nodes(data.get("children"))}


async def _mindmap_block(block: dict) -> dict:
    """长视频 map：单章生成一棵子树。"""
    sys_prompt = _SYSTEM_ANALYST + (
        " 请输出严格 JSON 思维导图子树（不要代码块、不要解释）："
        "{\"title\":\"本段主题\",\"children\":[{\"title\":\"要点\"},...]}"
        " 仅一层 children，节点用短语。"
    )
    user = f"时间范围 [{fmt_time(block['start'])} - {fmt_time(block['end'])}] 的字幕：\n\n{block['text']}"
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return {"title": str(data.get("title") or fmt_time(block["start"])), "children": _normalize_nodes(data.get("children"))}


async def _map_mindmap(blocks: list[dict]) -> list[dict]:
    """长视频 map：并发（受 AI_MAP_CONCURRENCY 限制）生成各章导图子树。"""
    sem = asyncio.Semaphore(max(1, settings.ai_map_concurrency))

    async def run(b: dict):
        async with sem:
            return await _mindmap_block(b)

    return await asyncio.gather(*[run(b) for b in blocks])


async def _mindmap_root_title(subtrees: list[dict]) -> str:
    """长视频 reduce：从各章子树合成一个根节点标题。"""
    sys_prompt = (
        "你是一位视频内容分析师。下面是某视频各时间段的思维导图子树，"
        "请给出一个能整体概括该视频的根节点标题（一句短语）。只输出严格 JSON："
        "{\"title\":\"根节点标题\"}，不要代码块、不要解释。"
    )
    user = "各段子树 JSON：\n\n" + json.dumps(subtrees, ensure_ascii=False)
    data = await _chat_json([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ])
    return str(data.get("title") or "视频主题")




# =========================================================================
# 对外：AI 问答（SSE 流式）
# =========================================================================
def _ask_messages(question: str, context: str, history: list[dict] | None) -> list[dict]:
    system = (
        "你是一位视频内容分析助手。下面是该视频的字幕片段，逐条带时间戳（[MM:SS - MM:SS]）。"
        "请用中文简明准确地回答用户针对视频内容的提问。\n"
        "若问题是在问「哪一段 / 哪几分钟 / 什么时候 / 在哪里讲到某事」这类**定位题**：请依据字幕里给出的"
        "时间戳回答具体时间段（如 12:30–15:20、约第 18 分钟、3:05 前后），可把相邻多条时间戳合成一个区间，"
        "并尽量给出起止时间。\n"
        "若你拿到的字幕片段没有覆盖问题所指内容，请如实说明「该片段未覆盖到所述内容」，**不要编造时间**。\n"
        "视频字幕片段：\n" + context
    )
    msgs = [{"role": "system", "content": system}]
    for m in (history or [])[-6:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])})
    msgs.append({"role": "user", "content": question})
    return msgs


_CJK_RE = re.compile(r"[一-鿿]+")


def _question_tokens(question: str) -> list[str]:
    r"""把问题拆成可用来与字幕正文匹配的关键词。

    英文按 ``\W+`` 切（足够）；但中文是**无空格连续串**，``\W+`` 不会拆 CJK，导致
    ``re.split(r"\W+", question)`` 把整句中文当作一个 token，几乎匹配不上任何章节，
    于是每次问答都退化成取前 3 章。这里对连续中文额外产出 2-gram（`结尾说什么`
    → 结尾/尾说/说了/什么），使中文提问也能量化匹配。
    """
    q = (question or "").lower()
    words = [t for t in re.split(r"\W+", q) if len(t) >= 2]
    cjk: list[str] = []
    for chunk in _CJK_RE.findall(q):
        if len(chunk) >= 2:
            cjk += [chunk[i : i + 2] for i in range(len(chunk) - 1)]
        else:
            cjk.append(chunk)
    seen: set[str] = set()
    out: list[str] = []
    for t in words + cjk:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _retrieve_context(question: str, chapters: list[dict]) -> str:
    """按「问题关键词 × 章节文本/标题」的简单打分，取 top-k 章节作为上下文。"""
    tokens = _question_tokens(question)

    def score(ch: dict) -> int:
        hay = f"{ch.get('title', '')} {ch['text']}".lower()
        return sum(1 for t in tokens if t in hay)

    ranked = sorted(chapters, key=score, reverse=True)
    budget = settings.ai_chat_context_chars
    used = 0
    parts: list[str] = []
    for ch in ranked[:3]:
        snippet = ch["text"]
        if used + len(snippet) > budget:
            snippet = snippet[: max(0, budget - used)]
        if not snippet:
            continue
        parts.append(f"[{fmt_time(ch['start'])} - {fmt_time(ch['end'])}] {snippet}")
        used += len(snippet)
        if used >= budget:
            break
    return "\n\n".join(parts) or _segments_lines([])


def _expand_window(segments: list[dict], center: int, budget: int) -> list[dict]:
    """从 center 向两侧扩展，取一段时间连续、字符量 ≤ budget 的字幕切片。"""
    n = len(segments)
    lo = hi = center
    used = len(segments[center].get("text", ""))
    while (lo > 0 or hi < n - 1) and used < budget:
        expanded = False
        if lo > 0:
            cand = segments[lo - 1].get("text", "")
            if used + len(cand) <= budget:
                lo -= 1; used += len(cand); expanded = True
        if hi < n - 1:
            cand = segments[hi + 1].get("text", "")
            if used + len(cand) <= budget:
                hi += 1; used += len(cand); expanded = True
        if not expanded:
            break
    return segments[lo : hi + 1]


def _retrieve_timeline(question: str, segments: list[dict]) -> str:
    """为问答检索「细粒度时间线」，让模型能回答「第几分钟讲了什么」。

    旧实现把分段聚合为章节后按章节整块打分、取 top-3 章，且章节内合并文本丢失了逐条
    时间戳——模型只能凭章节首尾时间猜，难以定位到分钟。这里改为**逐字幕条**打分，
    围绕最相关分段取一段时间连续、逐行带 `[MM:SS - MM:SS]` 的窗口；若问题关键词一个都
    匹配不上则退回章节级上下文（原逻辑），避免只给开头一段。
    """
    if not segments:
        return _segments_lines([])
    tokens = _question_tokens(question)

    def seg_score(s: dict) -> int:
        hay = str(s.get("text", "")).lower()
        return sum(1 for t in tokens if t in hay)

    scores = [seg_score(s) for s in segments]
    best_idx = max(range(len(segments)), key=lambda i: scores[i])
    if scores[best_idx] == 0:
        return _retrieve_context(question, segment_chapters(segments))
    budget = settings.ai_chat_context_chars
    window = _expand_window(segments, best_idx, budget)
    used = sum(len(s.get("text", "")) for s in window)
    w0, w1 = window[0]["start"], window[-1]["start"]
    lines = _segments_lines(window)
    # 预算有余且话题分散出现时，补充窗口外的高分片段作为「另一次出现」，标注时间。
    extra: list[str] = []
    for s, sc in zip(segments, scores):
        if sc > 0 and used < budget and not (w0 <= s["start"] <= w1):
            line = f"[{fmt_time(s['start'])} - {fmt_time(s['end'])}] {s['text']}"
            if used + len(line) <= budget:
                extra.append(line)
                used += len(line)
    if extra:
        lines += "\n\n（以下片段也可能涉及相同话题，时间如下：）\n" + "\n".join(extra[:6])
    return lines


async def generate_answer(segments: list[dict], question: str, history: list[dict] | None) -> AsyncIterator[str]:
    """对视频内容提问，逐 token 产出答案（供前端 SSE 组装）。

    用细粒度时间线上下文（逐条时间戳），使模型能就「第几分钟讲到什么」给出具体时间区间。
    """
    if not question or not question.strip():
        raise LLMError("请输入你的问题")
    if not segments:
        raise LLMError("该视频无可读字幕，无法回答问题")
    context = _retrieve_timeline(question, segments)
    messages = _ask_messages(question, context, history)
    async for token in _chat_stream(messages):
        yield token


def as_http_error(exc: LLMError, status: int = 502) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": str(exc)})
