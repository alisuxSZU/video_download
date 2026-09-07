"""OpenAI 兼容 LLM 客户端：字幕翻译 + 视频摘要。

用已安装的 httpx 发起请求，不引入额外依赖。API Key 仅存服务端环境变量，绝不下发前端。
支持 DeepSeek / Zhipu(智谱) / 星火 / Qwen 等任何 OpenAI 兼容端点。
"""
from __future__ import annotations

import httpx
from fastapi import HTTPException

from .config import settings


class LLMError(Exception):
    """LLM 调用失败（友好中文）。"""


def _ensure_configured() -> tuple[str, str]:
    if not settings.openai_api_key:
        raise LLMError("AI 功能未配置，请在服务器设置 OPENAI_API_KEY")
    return settings.openai_api_key, settings.openai_model


async def _chat(messages: list[dict]) -> str:
    api_key, model = _ensure_configured()
    url = f"{settings.openai_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMError("AI 请求超时，请稍后再试") from exc
    except httpx.HTTPError as exc:
        raise LLMError("AI 服务暂时不可用，请稍后再试") from exc

    if resp.status_code == 401:
        raise LLMError("AI 接口鉴权失败，请检查 OPENAI_API_KEY")
    if resp.status_code == 429:
        raise LLMError("AI 接口限流，请稍后再试")
    if resp.status_code >= 400:
        raise LLMError(f"AI 接口错误({resp.status_code})，请稍后再试")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("AI 返回格式异常，请稍后再试") from exc


async def summarize(transcript: str) -> dict:
    """生成结构化中文摘要。transcript 为纯文本字幕。"""
    if not transcript or not transcript.strip():
        raise LLMError("没有可用的字幕文本，无法生成摘要")

    text = transcript[: settings.ai_max_chars]
    sys_prompt = (
        "你是一位视频内容分析师。请根据用户提供的视频字幕，输出一份精炼、结构化、中文的摘要。"
        "要求：1) 用一句话概括视频主题；2) 列出 3-6 条关键要点(分点)；3) 给出 3-5 个关键词；"
        "4) 若字幕含明显时间段可标注。文本越口语化越要抓核心。只输出摘要正文，不要额外寒暄。"
    )
    content = await _chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"视频字幕如下：\n\n{text}"},
        ]
    )
    return {"summary": content.strip(), "lang": "zh", "model": settings.openai_model, "used_source": "transcript"}


async def translate(text: str, target_lang: str) -> str:
    """把字幕翻译成目标语言(如 zh-CN / en)。"""
    if not text or not text.strip():
        raise LLMError("没有可翻译的字幕内容")
    text = text[: settings.ai_max_chars]
    sys_prompt = (
        f"请把用户提供的视频字幕翻译成{target_lang}。保留原意与语气，输出流畅自然，"
        "不要添加任何额外解释或格式标记，只输出翻译结果。"
    )
    content = await _chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ]
    )
    return content.strip()


def as_http_error(exc: LLMError, status: int = 502) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": str(exc)})
