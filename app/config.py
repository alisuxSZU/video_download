"""集中配置：从 .env 读取所有可调参数。

保持轻量：所有配置为模块级常量/单例，无数据库依赖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = 本文件上一级(即 d:\LCP_agent\video_download)
BASE_DIR = Path(__file__).resolve().parent.parent

# 优先加载项目根下的 .env
load_dotenv(BASE_DIR / ".env")


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int(os.getenv("PORT"), 8000)
    version: str = "0.7.0"

    # ---- 下载 / 临时文件 ----
    # 尽量用短路径，规避 Windows 260 长度限制
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "D:/vdl_tmp"))
    file_ttl_seconds: int = _int(os.getenv("FILE_TTL_SECONDS"), 1800)
    max_concurrent_downloads: int = _int(os.getenv("MAX_CONCURRENT_DOWNLOADS"), 2)
    max_batch: int = _int(os.getenv("MAX_BATCH"), 8)
    cleanup_interval_seconds: int = _int(os.getenv("CLEANUP_INTERVAL_SECONDS"), 60)
    ffmpeg_location: str | None = os.getenv("YTDLP_FFMPEG_LOCATION") or None
    cookies_file: str | None = os.getenv("COOKIES_FILE") or None
    proxy: str | None = os.getenv("PROXY") or None

    # ---- 抖音(服务端无头浏览器)----
    # 服务器需装有 Chrome/Edge。关闭后抖音走友好降级提示,不影响其他平台。
    douyin_enabled: bool = _bool(os.getenv("DOUYIN_ENABLED"), True)
    douyin_headless: bool = _bool(os.getenv("DOUYIN_HEADLESS"), True)
    douyin_timeout_seconds: int = _int(os.getenv("DOUYIN_TIMEOUT_SECONDS"), 45)
    douyin_auto_refresh: bool = _bool(os.getenv("DOUYIN_AUTO_REFRESH"), True)

    # ---- SEO ----
    # 生产域名（如 https://example.com，不带末尾斜杠）。用于 canonical / og:url / sitemap 绝对 URL。
    # 未配置时页面内为相对路径占位（按当前域名解析），上线后在此填写即全站生效。
    site_base_url: str = os.getenv("SITE_BASE_URL", "").rstrip("/")

    # ---- GEO（AI 搜索引擎优化）----
    # IndexNow key（Bing/ChatGPT 搜索源主动推送验证）；未配置时不暴露验证路由。
    indexnow_key: str = os.getenv("INDEXNOW_KEY", "").strip()

    # ---- 会员账户 / Stripe 支付（v0.7.0）----
    # SQLite 库文件（持久化订单/会员/令牌；零额外服务，契合单 worker 部署）
    db_path: Path = BASE_DIR / "data" / "vdl.db"
    stripe_secret_key: str | None = os.getenv("STRIPE_SECRET_KEY") or None
    stripe_webhook_secret: str | None = os.getenv("STRIPE_WEBHOOK_SECRET") or None
    stripe_price_month: str | None = os.getenv("STRIPE_PRICE_MONTH") or None
    stripe_price_year: str | None = os.getenv("STRIPE_PRICE_YEAR") or None
    # 注册/登录 与 创建支付会话 的独立限流（每 IP 每分钟，防爆破/刷单）
    auth_rate_per_min: int = _int(os.getenv("RATE_AUTH_PER_MIN"), 5)
    billing_rate_per_min: int = _int(os.getenv("RATE_BILLING_PER_MIN"), 5)
    # 非 PRO 每日 AI 调用上限（游客按 IP、免费账户按 user_id）；PRO 不限日次数
    ai_free_daily_limit: int = _int(os.getenv("AI_FREE_DAILY_LIMIT"), 3)
    # PRO 每分钟 AI 限流（高于免费桶）
    ai_pro_rate_per_min: int = _int(os.getenv("RATE_AI_PRO_PER_MIN"), 60)
    # 免费用户可选的最大视频高度（像素）；1080p/4K 为 PRO 专属
    free_max_height: int = _int(os.getenv("FREE_MAX_HEIGHT"), 720)

    # 套餐表：plan_key -> {天数, Stripe 一次性 Price ID}；__post_init__ 中填充
    plans: dict = field(default_factory=dict)
    # Stripe 是否配置完整（密钥 + 两个 Price）；False 时支付端点 503、不发起任何外呼
    billing_enabled: bool = False

    def __post_init__(self) -> None:
        self.plans = {
            "month": {"days": 30, "price_id": self.stripe_price_month, "label": "月卡"},
            "year": {"days": 365, "price_id": self.stripe_price_year, "label": "年卡"},
        }
        self.billing_enabled = bool(
            self.stripe_secret_key and self.stripe_price_month and self.stripe_price_year
        )

    # ---- 安全 / 限流 ----
    parse_rate_per_min: int = _int(os.getenv("RATE_PARSE_PER_MIN"), 20)
    download_rate_per_min: int = _int(os.getenv("RATE_DOWNLOAD_PER_MIN"), 6)
    # 每分钟每 IP 可调用 AI 功能（共用「ai」限流桶：字幕提取/翻译、摘要、章节、导图、问答）。
    # 默认 30：普通用户一次连点多个功能 + 问几个问题（~10 次/分钟）不被打断，仍保留刷量拦截。
    ai_rate_per_min: int = _int(os.getenv("RATE_AI_PER_MIN"), 30)
    extractor_allowlist: list[str] = field(
        default_factory=lambda: [
            x.strip() for x in os.getenv("EXTRACTOR_ALLOWLIST", "").split(",") if x.strip()
        ]
    )
    allowed_origin: str | None = os.getenv("ALLOWED_ORIGIN") or None
    app_token: str | None = os.getenv("APP_TOKEN") or None

    # ---- LLM (OpenAI 兼容) ----
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    openai_model: str = os.getenv("OPENAI_MODEL", "deepseek-chat")
    ai_max_chars: int = _int(os.getenv("AI_MAX_CHARS"), 120000)
    ai_timeout_seconds: float = _float(os.getenv("AI_TIMEOUT_SECONDS"), 90)
    # 结构化摘要（学习型 v2）：单次 vs 分块 map-reduce
    # 单次直出结构化摘要的上限（字符），超过则走「分块 map-reduce」
    ai_single_shot_chars: int = _int(os.getenv("AI_SINGLE_SHOT_CHARS"), 30000)
    # 分块 map-reduce：每章最大字符/预算
    ai_chapter_max_chars: int = _int(os.getenv("AI_CHAPTER_MAX_CHARS"), 8000)
    # 分块 map-reduce：每章目标时长（秒）——先到先切（达到目标时长或字符预算）
    ai_chapter_goal_seconds: int = _int(os.getenv("AI_CHAPTER_GOAL_SECONDS"), 600)
    # 分块 map-reduce：最大章节数
    ai_max_chapters: int = _int(os.getenv("AI_MAX_CHAPTERS"), 20)
    # 分块 map-reduce：map 阶段并发的 LLM 调用数
    ai_map_concurrency: int = _int(os.getenv("AI_MAP_CONCURRENCY"), 3)
    # AI 问答：上下文（相关性章节）最大字符数
    ai_chat_context_chars: int = _int(os.getenv("AI_CHAT_CONTEXT_CHARS"), 15000)
    # 字幕稿缓存（AI 问答复用，避免每轮重复提取）TTL 秒
    transcript_cache_ttl_seconds: int = _int(os.getenv("TRANSCRIPT_CACHE_TTL_SECONDS"), 600)

    # ---- 设备无关的 UA，规避平台基础反爬 ----
    user_agent: str = os.getenv(
        "YTDLP_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )


settings = Settings()
