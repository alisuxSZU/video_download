# OVERVIEW — 开发前 · 当前状态快照

> 本文件是「进一步开发前」的**地基快照**：把项目当前是什么、有哪些关键约束与坑、下一步做什么，浓缩成单一入口。
> **进入任何扩展（M2/M3）前先读这里**；技术细节以 [DESIGN.md](DESIGN.md) / [API.md](API.md) / [SECURITY.md](SECURITY.md) 为准，进展与决策变更记到 [CHANGELOG.md](CHANGELOG.md)。

---

## 一、项目定位

**万能视频下载网站** —— 一条链接下遍全网。粘贴 URL 即解析多清晰度/格式、批量下载、提取并翻译字幕、AI 生成视频摘要，并带 PRO 付费展示。

**核心策略：站在巨人肩膀上**。封装开源 **yt-dlp**（十几万 Star）作下载引擎，纯 Python 技术栈；仅做「安全的 API 门面 + 精美前端 + 增值/付费」。抖音因 `a_bogus` 签名墙走**服务端无头浏览器**（Playwright 复用系统 Chrome）独立方案。

## 二、版本与里程碑状态

| 模块 | 版本 | 状态 |
|---|---|---|
| 后端/前端/文档 | v0.1.0 | ✅ M1 核心业务 + 前端精美 + 安全基础 |
| 抖音下载 | v0.1.2 | ✅ M1.1 服务端无头浏览器打通 |
| 字幕/AI 摘要 | — | ⬜ M2 待启动：**已实现后端但未接前端、未联调** |
| 部署加固 | — | ⬜ M3 待启动：反代/HTTPS/备案/回归脚本 |

> 当前代码已实现字幕 `extract_subtitle` 与 AI `summarize/translate` 端点，但 M2 只差「前端面板接通 + LLM Key 联调」，后端逻辑基本就绪。

## 三、环境现状（已核实）

- Windows 11，Python 3.12.4。
- 依赖：`fastapi` / `uvicorn` / `pydantic` / `httpx` / `yt-dlp` / `python-dotenv` / `playwright`（抖音）。
- ⚠️ **ffmpeg 必需但需确认已装**（合并分离音视频流、m3u8）。未装则高清档降级为单文件 progressive。
- LLM 用 **OpenAI 兼容接口**（`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`），支持 DeepSeek/智谱/星火；无 Key 时字幕翻译/摘要端点返回可读 502。

**配置项全部集中在 `app/config.py`（读 `.env`）**，完整清单见 `.env.example`。分组：监听 / 下载与临时文件 / ffmpeg / cookies与proxy / 抖音 / 安全限流 / LLM / UA。

## 四、代码结构与模块职责

```
app/
  main.py        # FastAPI 入口、lifespan(启动清旧临时文件+后台清理)、安全中间件、挂载 /static、/ 返回 index.html
  config.py      # 从 .env 读取的 Settings 单例（.env 集中配置）
  models.py      # Pydantic 请求体 + Job 数据类(dataclass + threading.Lock)
  security.py    # validate_url(URL/SSRF 校验)、RateLimiter(滑窗限流)、安全响应头、错误脱敏、sanitize_filename、全局异常处理
  downloader.py  # yt-dlp 薄封装：probe/build_format_string/run_download/progress_hook/extract_subtitle/subtitle_to_text + 抖音路由
  tasks.py       # 内存 job store + ThreadPoolExecutor + asyncio.Semaphore + 后台 TTL 清理 + 取消
  ai.py          # OpenAI 兼容 LLM：translate + summarize（httpx POST /chat/completions）
  douyin.py      # 抖音特例：Playwright 无头浏览器解析 + httpx 直连下载
  routes.py      # 全部 API 端点（纯 HTTP 层，不含业务）
static/
  index.html / app.js / styles.css   # 前端单页（HTML + 逻辑 + 样式），Tailwind CDN
docs/            # 本文 + PLAN/DESIGN/API/SECURITY/ROADMAP/CHANGELOG
```

**分层职责**：`routes`(HTTP) → `tasks`(状态中心) → `downloader`(yt-dlp) / `ai`(LLM) / `douyin`(抖音)；`security` 为横切面。

## 五、核心数据流（端到端）

```
前端粘贴 URL
  → POST /api/parse        → downloader.probe(只解析不下载) → 标题/缩略图/时长/全部格式
  → POST /api/download     → tasks 建 Job(queued) → 后台线程 probing→downloading→done
  → 前端 1.1s 轮询 GET /api/jobs/{job_id} 拿 progress/speed（不含服务端绝对路径）
  → 完成后 GET /api/jobs/{job_id}/file 流式下载成品（Content-Disposition 用清洗文件名）
  → 可选 POST /api/subtitles (提取/翻译) 、POST /api/ai/summary (LLM 摘要)
```

Job 状态机：`queued → probing → downloading → done`，可 `error / closed`（取消）。进度回调在工作线程运行，Job 挂 `threading.Lock` 线程安全；`snapshot()` 只下发公开字段（`filepath` 绝不下发）。

## 六、关键决策与坑（重要，开发时避免重踩）

| # | 决策 / 坑 | 后果 |
|---|---|---|
| 1 | **必须单进程单 worker** | job store/限流存内存，多 worker 各自独立→轮询 404。部署 `--workers 1` |
| 2 | **无 ffmpeg 降级** | `_is_progressive` 对平台不返回 codec（如 Archive.org）视为单文件；不误拼 `+bestaudio` |
| 3 | 进度用**轮询**（1.1s）非 WebSocket | 最简、无长连接、天然兼容无状态部署 |
| 4 | **抖音不合成 fetch** | 页内 JS 发起的 `fetch` 也缺 `a_bogus`；必须 `page.expect_response` 拦截页面**自身**对 `aweme/v1/web/aweme/detail` 的真实网络响应 |
| 5 | **Playwright 单线程占有** | greenlet 绑创建线程；`resolve()` 被 parse 线程与下载线程并发调用会 `greenlet.error`。用单浏览器专属线程 + `queue.Queue`+`Future` 串行提交 |
| 6 | **Playwright 超时单位是毫秒** | 传入秒会「45ms 秒败」；`timeout_ms=int(sec*1000)` |
| 7 | `wait_until="networkidle"` 永不 settle | 抖音长连接 → 改 `domcontentloaded` |
| 8 | `_retry_antibot` 处理瞬时抖动 | B站 `HTTP 412/429/403`、连接级 SSL reset 等带退避重试可自愈；确定性错误直接抛 |
| 9 | **cookiefile/proxy 不对字节系转发** | `_bytedance` 标记，避免运营者 Cookie 泄漏给抖音；抖音一律匿名游客会话 |
| 10 | `/file` 用 FileResponse 流式 | Windows 大文件不整读内存；路径尽量短（`TEMP_DIR=D:/vdl_tmp` 规避 260 限制） |
| 11 | 字幕自动回退 | 前端硬编码 `lang=zh` 常失败；后端 `_ordered_sub_candidates` 逐条尝试直到成功 |
| 12 | 二次标准化字幕列表 | 避免 `AttributeError: 'list' object has no attribute 'items'` 500（`_collect_transcript_sync` 直接取 payload['subtitles']） |

## 七、已知限制与边界（v1 有意为之 / 可接受降级）

- 无持久化：重启丢进行中任务；单进程。
- 字幕翻译/AI 摘要依赖 LLM Key；无字幕视频无法摘要（**v1 不做本地 whisper 转写**）。
- 付费为展示占位：不接真实支付、无账户体系、无 DB。
- 强反爬平台（X/Instagram/TikTok）多数**预期失败**（需 cookies/JS 签名）；YouTube 部分需登录。失败返回友好中文错误，不 500。
- 抖音风控具时效性：签名/风控不定期换代，可用 `DOUYIN_TIMEOUT_SECONDS` 调超时、`DOUYIN_ENABLED=false` 关停。

## 八、下一步开发上下文

**M2（增值功能）前置条件**：
1. 安装 ffmpeg：`winget install Gyan.FFmpeg`，未入 PATH 则填 `.env` 的 `YTDLP_FFMPEG_LOCATION`。
2. 配置 LLM：`OPENAI_API_KEY`（DeepSeek 等兼容接口）。

**M2 待办**：
- [ ] 前端字幕面板接通 `/api/subtitles`（语言下拉/提取/翻译/预览/下载）
- [ ] 前端 AI 摘要面板接通 `/api/ai/summary`
- [ ] 无字幕视频友好空态；超长字幕截断（`AI_MAX_CHARS`）
- [ ] LLM 未配置/超时 → 可读错误映射

**M3 待办**：反代 + HTTPS + ICP 备案；`--workers 1` 固化到部署脚本；缩略图代理端点（防泄漏 referrer / 防盗链）；回归矩阵脚本化；可选 `APP_TOKEN` 鉴权。

## 九、文档索引与开发约定

| 文档 | 作用 |
|---|---|
| **本 OVERVIEW** | ★ 开发前地基快照（先读） |
| [PLAN.md](PLAN.md) | 总方案：动机、已确认决策、环境、UI 设计语言、端到端测试矩阵 |
| [DESIGN.md](DESIGN.md) | 架构详解：Job 模型、并发、格式启发式、安全模型、选型理由 |
| [API.md](API.md) | 接口契约：端点/字段/全局错误码 |
| [SECURITY.md](SECURITY.md) | 威胁模型 + 已落实防护 + 上线检查清单 |
| [ROADMAP.md](ROADMAP.md) | 里程碑状态 + 待办 + v2 演进池 |
| [CHANGELOG.md](CHANGELOG.md) | 按里程碑的记录：Added/Changed/Fixed/Verified + 决策 |

> **开发约定（来自 ROADMAP）**：每次扩展先更新 ROADMAP，再回 PLAN/API/CHANGELOG，保持 `docs/` 与代码同步。本期为开发前快照，故本文件记录「当前已实现的全部事实」。

---

## 附：代码内遗留注释（建议顺手修正）

- `app/config.py:14`：注释把项目根写成 `d:\LCP_agent\ai-ppt-generator`，实际应为 `d:\LCP_agent\video_download`。不影响功能，仅注释误导，可在下次触碰该文件时一并更正。
