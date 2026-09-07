# DESIGN — 架构设计详解

本文件解释「为什么这么设计」，供扩展时快速理解既有结构。代码以 [app/](../app/) 为准，本文与代码同步。

## 1. 分层与职责

```
浏览器 ── HTTP ──> FastAPI(routes.py) ──> tasks.py(任务调度) ──> downloader.py(yt-dlp封装)
                          │                        │                       │
                          │                        └──> ai.py (LLM: 翻译/摘要)
                          └──> security.py(URL校验/限流/安全头/异常)
```

- **routes.py**：纯 HTTP 层，解析请求、限流、调用下游、映射错误。不含业务。
- **tasks.py**：内存 job store + 线程池 + 信号量 + 后台清理。唯一的「状态中心」。
- **downloader.py**：对 yt-dlp 的薄封装。可独立用在脚本里（不依赖 HTTP）。
- **ai.py**：LLM 客户端，OpenAI 兼容。
- **security.py**：横切关注点，可被所有层复用。

## 2. Job 模型

一个下载/解析任务对应一个 `Job`（[app/models.py](../app/models.py)）。核心字段与状态机：

```
status: queued ─> probing ─> downloading ─> done
                   │              │
                   └──────────────> error / closed(取消)
```

**公开字段**（轮询给前端）：`id/url/status/title/thumbnail/duration/extractor/format_id/ext/needs_merge/progress/downloaded_bytes/total_bytes/speed/filename/filesize/error/subtitles/created_at`。

**私有字段**（绝不下发）：`filepath`（服务端绝对路径）。

**线程安全**：`progress_hooks` 在工作线程运行，Job 挂 `threading.Lock`，回调 `with lock:` 更新；`snapshot()` 在锁内拷贝出公开字段，避免脏读。

**过期与清理**：`expires_at = created_at + FILE_TTL`。后台每 `CLEANUP_INTERVAL` 扫描，删过期 job 及其临时目录。

## 3. 下载并发模型

yt-dlp 是**同步阻塞**调用，不能放进 FastAPI 的 async 事件循环。方案：

- `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)` 承载阻塞下载。
- 路由里 `await loop.run_in_executor(...)` 不阻塞事件循环。
- 另用 `asyncio.Semaphore` 兜底并发上限，超限返回 503。

> **选 Thpool + Semaphore 而非全异步**：yt-dlp 无 async API，强行 async 反而增加复杂度。线程池是「站在巨人肩膀上、改动最小」的正确选择。

**进度上报**：前端**轮询** `/api/jobs/{id}`（1.1s 间隔），不用 WebSocket —— 最简、最稳、无长连接管理成本。

**取消**：`DELETE /api/jobs/{id}` → 置位 `_cancel` → 下一次 `progress_hook` 抛 `DownloadCancelled` → yt-dlp 中断（尽力而为）→ 清理目录。

## 4. yt-dlp 封装要点

### 4.1 解析（不下载）
```python
with YoutubeDL(_build_base_params()) as ydl:
    info = ydl.extract_info(url, download=False)
```
返回 `{title, thumbnail, duration, extractor, formats, subtitles}`。

### 4.2 关键参数
`format` / `outtmpl=f"{TEMP_DIR}/{job.id}/%(title).100s.%(ext)s"` / `progress_hooks` / `noprogress=True` / `noplaylist=True` / `restrictfilenames=True` / `quiet=True` / `merge_output_format='mp4'` / `ffmpeg_location`(若配) / `http_headers`(真实 UA)。

### 4.3 格式启发式
- `_is_audio_only(f)` = `vcodec=='none' and acodec!='none'`
- `_is_video_only(f)` = `vcodec!='none' and acodec=='none'` → 需 `+bestaudio` 合并
- `_is_progressive(f)` = 非 audio_only 且非 video_only → 单文件（平台不返回 codec 信息，如 Archive.org，默认视为单文件）

**`build_format_string(info, format_id, needs_merge)`** 逻辑：
- 未选 → 有 ffmpeg 用 `bestvideo+bestaudio/best`，否则 `best[ext=mp4]/best`。
- 选中 progressive → 直接用其 `format_id`。
- 选中 video-only → 无 ffmpeg 降级到同高度 progressive（或任意 progressive），有 ffmpeg 用 `{id}+bestaudio/{id}+bestaudio/best`。

### 4.4 输出定位
下载完成后 glob 目录，取最大文件作为成品；`filename` 经 `sanitize_filename` 清洗（供 Content-Disposition），`filepath` 仅服务端。

### 4.5 抖音特例（服务端无头浏览器）
抖音因 `a_bogus` 签名墙，**不走 yt-dlp**，而走 [app/douyin.py](../app/douyin.py) 的服务端无头 Chrome。设计要点：

- **策略**：`Playwright`（`channel="chrome"` 复用系统 Chrome，`headless=DOUYIN_HEADLESS`）加载抖音页 → 页面 **自身 JS 现场计算 `a_bogus`** → 用 `expect_response` 拦截它对 `/aweme/v1/web/aweme/detail` 的**真实网络响应** → 取 `video.play_addr.url_list[0]`，`playwm→play` 去水印。
- **为何不发合成 `fetch`**：页内 JS 发起的 `fetch` 同样缺 `a_bogus` → 空 body。必须让页面自己请求，我们只旁观拦截。
- **线程模型（关键）**：Playwright sync API 的 greenlet 绑定到创建它的线程，不可跨线程复用同一个 context/page（否则 `greenlet.error: Cannot switch to a different thread`）。而 `/api/parse`（FastAPI 执行器线程）与下载 job（`vdl` 线程池）会并发调用 `resolve()`，故由**单浏览器专属线程**独占 Playwright 实例，`resolve()` 经 `queue.Queue` + `Future` 提交请求并限时等待；所有 Playwright 调用串行落在该线程。`download()` 是纯 httpx（无 Playwright），任意线程可执行。
- **与主流程衔接**：`douyin.resolve(url)` 返回 **yt-dlp 形状的 `info`**（`formats` 单档、`subtitles:{}`、`_play_url`、`_play_headers`），因此 `probe`/`_build_payload`/`_apply_format` 零改动复用；`run_download` 见 `_play_url` 则改走 `douyin.download` 的 httpx 直连。
- **隐私/安全**：不用用户 Cookie（匿名游客会话）；`_bytedance` 标记阻止 `COOKIES_FILE`/`PROXY` 转发给抖音。
- **可关闭**：`DOUYIN_ENABLED=false` 降级。超时可用 `DOUYIN_TIMEOUT_SECONDS` 调。
- **风控时效性**：抖音签名/风控不定期更换，`_fetch_aweme_detail` 超时/异常统一映射友好中文（`DouyinBlockedError`/`DouyinUnsupportedError`）。

## 5. 字幕与 AI 流程

**字幕提取**（[downloader.extract_subtitle](../app/downloader.py)）：
用 `writesubtitles`/`writeautomaticsub` + `subtitleslangs=[lang]` + `skip_download=True` 定向写单语言 → 找最大字幕文件 → 读文本。优先手动内建字幕，其次自动。

**字幕转纯文本**（[subtitle_to_text](../app/downloader.py)）：从 srt/vtt 去掉序号、时间轴行、URL，供 AI 使用。

**AI 摘要**（[ai.summarize](../app/ai.py)）：
取纯文本（手动 > 自动，皆无 → 502「该视频无可读字幕，v1 不做本地转写」）→ 截断到 `AI_MAX_CHARS` → 单次 httpx POST `{BASE_URL}/chat/completions`，system prompt 要求输出结构化中文摘要（要点 + 关键词）。Key 仅服务端。

**翻译**：同上，带 `target_lang` 交 LLM。

## 6. 安全模型

见 [SECURITY.md](SECURITY.md) 与 [security.py](../app/security.py)。要点：URL/SSRF 校验、滑窗限流、安全响应头 + CSP、错误脱敏、临时文件过期清理、日志脱敏。

## 7. 技术选型理由

| 选择 | 理由 |
|---|---|
| yt-dlp | 十几万 Star、全平台维护频繁、功能最全 → 「站在巨人肩膀上」 |
| FastAPI + uvicorn | 异步、Pydantic 校验、原生 Swagger、极轻 |
| 内存 job store | v1 无 DB、无账户、单机；横扩需 Redis（ROADMAP v2） |
| 轮询（非 WebSocket） | 最简、无长连接、天然兼容无状态部署 |
| 前端静态 HTML + Tailwind CDN | 零构建、零依赖、打开即用；符合参考站风格 |
| 前端解析 + 后端 API 分离 | 前后端清晰，后续可拆独立前端 |

## 8. 已知限制（v1 有意为之）

- 无持久化：重启丢任务、单进程。
- 字幕翻译/摘要依赖 LLM Key，无 Key 时相关端点报可读错误。
- 不做本地 whisper 转写（无字幕视频无法摘要）。
- 不接真实支付、无账户体系（付费为展示占位）。
