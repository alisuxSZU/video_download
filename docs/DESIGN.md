# DESIGN — 架构设计详解

> 解释「为什么这么设计」，供扩展时快速理解既有结构。代码以 [app/](../app/) 为准，本文与代码同步。范围与限制见 [PLAN.md](PLAN.md)。

## 1. 模块与职责

```
app/
  main.py        # FastAPI 入口、lifespan(启动清旧临时文件+后台清理)、安全中间件、挂载 /static、/ 返回 index.html
  config.py      # 从 .env 读取的 Settings 单例（集中配置）
  models.py      # Pydantic 请求体 + Job 数据类(dataclass + threading.Lock)
  security.py    # validate_url(URL/SSRF校验)、RateLimiter(滑窗限流)、安全响应头、错误脱敏、sanitize_filename、全局异常
  downloader.py  # yt-dlp 薄封装：probe/build_format_string/run_download/progress_hook/extract_subtitle/subtitle_to_text + 抖音路由
  tasks.py       # 内存 job store + ThreadPoolExecutor + 总量上限 MAX_ACTIVE_JOBS + 后台 TTL 清理 + 取消
  ai.py          # OpenAI 兼容 LLM：translate + 结构化摘要(单次/分块map-reduce+导图派生) + SSE流式问答
  douyin.py      # 抖音特例：Playwright 无头浏览器解析 + httpx 直连下载
  routes.py      # 全部 API 端点（纯 HTTP 层，不含业务）
static/          # index.html / app.js / styles.css（单页前端，Tailwind CDN，零构建）
```

**分层**：`routes`(HTTP) → `tasks`(状态中心) → `downloader`(yt-dlp) / `ai`(LLM) / `douyin`(抖音)；`security` 为横切面。downloader 可脱离 HTTP 独立用于脚本。

## 2. 数据流（端到端）

```
粘贴 URL
  → POST /api/parse        → downloader.probe(只解析不下载) → 标题/缩略图/时长/全部格式
  → POST /api/download     → tasks 建 Job(queued) → 后台线程 probing→downloading→done
  → 前端 1.1s 轮询 GET /api/jobs/{job_id} 拿 progress/speed（不含服务端绝对路径）
  → 完成后 GET /api/jobs/{job_id}/file 流式下载成品（Content-Disposition 用清洗文件名）
    前端在单条任务 done 后**自动点击**该链接（link.click()）把文件保存到用户本地下载目录，无需用户再点一次；批量队列每行 done 后经 `fetch→Blob→anchor` **逐条、串行**自动保存（取完一条再取下一条，序列化避免并发触发下载引擎）。⚠️ Chromium 会限制「无用户手势的自动下载」——批量**第 2 条起**真实浏览器会弹**一次性**「此网站尝试下载多个文件→允许？」授权框（每站点一次）；无头自动化需 `--enable-automatic-downloads` 才能放行以便测试
  → 可选 POST /api/subtitles (提取/翻译)、POST /api/ai/summary (LLM 摘要)
```

## 3. Job 模型与并发

一个下载/解析任务对应一个 `Job`（[app/models.py](../app/models.py)）。状态机：

```
status: queued ─> probing ─> downloading ─> done
                   │              │
                   └──────────────> error / closed(取消)
```

- **公开字段**（轮询给前端）：`id/url/status/title/thumbnail/duration/extractor/format_id/ext/needs_merge/progress/downloaded_bytes/total_bytes/speed/filename/filesize/error/subtitles`（即 `Job.snapshot()` 返回值；`created_at` 为内部字段，不下发）。
- **私有字段**（绝不下发）：`filepath`（服务端绝对路径）。
- **线程安全**：`progress_hooks` 在工作线程运行，Job 挂 `threading.Lock`，回调 `with lock:` 更新；`snapshot()` 在锁内拷贝公开字段，避免脏读。
- **过期清理**：`expires_at = created_at + FILE_TTL`；后台每 `CLEANUP_INTERVAL` 删过期 job 及临时目录。

**并发**：yt-dlp 是同步阻塞调用，不能进 async 事件循环 → `ThreadPoolExecutor(MAX_CONCURRENT_DOWNLOADS)` 承载阻塞下载（`tasks.start_download` 提交）；解析/字幕/摘要经路由 `await loop.run_in_executor(...)` 不阻塞循环。**总量上限**：`tasks.MAX_ACTIVE_JOBS=100` + `active_or_queued_count()`，路由在 `active_or_queued_count() >= MAX_ACTIVE_JOBS` 时拒绝创建 → 503。**选线程池而非全异步**，因 yt-dlp 无 async API，强行 async 徒增复杂度 —— 这是「站在巨人肩膀上、改动最小」的取舍。

**进度**：前端**轮询**（1.1s）而非 WebSocket —— 最简、无长连接、天然兼容无状态。
**取消**：`DELETE /api/jobs/{id}` → 置位 `_cancel` → 下一次 `progress_hook` 抛 `DownloadCancelled` → yt-dlp 中断（尽力而为）→ 清理目录。

## 4. yt-dlp 封装要点

**解析（不下载）**：`with YoutubeDL(_build_base_params()) as ydl: info = ydl.extract_info(url, download=False)`，返回 `{title, thumbnail, duration, extractor, formats, subtitles}`。

**关键参数**：`format` / `outtmpl=f"{TEMP_DIR}/{job.id}/%(title).100s.%(ext)s"` / `progress_hooks` / `noprogress=True` / `noplaylist=True` / `restrictfilenames=True` / `quiet=True` / `merge_output_format='mp4'` / `ffmpeg_location`(若配) / `http_headers`(真实 UA)。

**格式启发式**：
- `_is_audio_only(f)` = `vcodec=='none' and acodec!='none'`
- `_is_video_only(f)` = `vcodec!='none' and acodec=='none'` → 需 `+bestaudio` 合并
- `_is_progressive(f)` = 非 audio_only 且非 video_only → 单文件（平台不返回 codec，如 Archive.org，默认视为单文件）

**`build_format_string(info, format_id, needs_merge)`**（实际 4 分支）：
- 未选/找不到 → 有 ffmpeg 用 `bestvideo+bestaudio/best`，否则 `best[ext=mp4]/best`。
- 选中 audio_only → 直接其 `format_id`（不加音轨）。
- 选中 progressive → 直接其 `format_id`。
- 选中 video-only → 无 ffmpeg：降级到同高度 progressive，再降任意 progressive，最后 `{id}+bestaudio/best`；有 ffmpeg：`{id}+{同高度progressive 作音频兜底}/{id}+bestaudio/best`（无同高度档则取 `bestaudio`）。

**输出定位**：下载完成后 glob 目录取最大文件为成品；`filename` 经 `sanitize_filename` 清洗（供 Content-Disposition），`filepath` 仅服务端。

## 5. 抖音特例（服务端无头浏览器）

抖音因 `a_bogus` 签名墙**不走 yt-dlp**，走 [app/douyin.py](../app/douyin.py)：

- **策略**：Playwright（`channel="chrome"` 复用系统 Chrome，`headless=DOUYIN_HEADLESS`）加载抖音页 → 页面**自身 JS 现场算 `a_bogus`** → `expect_response` 拦截它对 `/aweme/v1/web/aweme/detail` 的**真实响应** → 取 `video.play_addr.url_list[0]`，`playwm→play` 去水印。
- **为何不发合成 `fetch`**：页内 JS 发起的 fetch 同样缺 `a_bogus` → 空 body。必须让页面自己请求，我们只旁观拦截。
- **线程模型（关键）**：Playwright sync API 的 greenlet 绑创建线程，不可跨线程复用同一 context/page（否则 `greenlet.error: Cannot switch to a different thread`）。而 `/api/parse`（FastAPI 执行器线程）与下载 job（vdl 线程池）会并发调 `resolve()` → 由**单浏览器专属线程**独占 Playwright，`resolve()` 经 `queue.Queue`+`Future` 提交并限时等待；所有 Playwright 调用串行落该线程。`download()` 是纯 httpx（无 Playwright），任意线程可执行。
- **与主流程衔接**：`douyin.resolve(url)` 返回 **yt-dlp 形状的 `info`**（单档、`subtitles:{}`、`_play_url`、`_play_headers`），故 `probe`/`_build_payload`/`_apply_format` 零改动复用；`run_download` 见 `_play_url` 改走 `douyin.download` 的 httpx 直连。
- **隐私/安全**：匿名游客会话、不用用户 Cookie；`_bytedance` 标记阻止 `COOKIES_FILE`/`PROXY` 转发给抖音。
- **可关闭**：`DOUYIN_ENABLED=false` 降级；超时 `DOUYIN_TIMEOUT_SECONDS` 调。

## 6. 字幕与 AI 流程（v2 学习型结构化摘要）

**字幕提取**（`extract_subtitle`）：`writesubtitles`/`writeautomaticsub` + `subtitleslangs=[lang]` + `skip_download=True` 定向写单语言 → 找最大字幕文件 → 读文本。优先手动内建，其次自动。**自动回退**：前端硬编码 `lang=zh` 常失败 → `_ordered_sub_candidates` 逐条尝试直到成功（精确→语言族→中/英手动→任意手动→任意）。

**字幕解析为分段**（新增 `subtitle_to_segments`）：与 `subtitle_to_text`（去时间轴，仅供 v1 纯文本/翻译）不同，`subtitle_to_segments` **保留 `start/end`（秒）+ text**，解析为 `[{start, end, text}, ...]` —— 这是「章节·时间轴 / 思维导图 / 问答」的根基，没有时间轴就无法产出分段章节。

**字幕稿缓存**（routes 内 `_transcript_cache`）：AI 问答每轮都需字幕做上下文，避免重复走 yt-dlp 提取。按 `TRANSCRIPT_CACHE_TTL_SECONDS` 过期；超 100 条丢最旧。`_collect_segments_sync` 优先读缓存，命中即免提取。

**AI 结构化摘要**（`ai.summarize(segments, meta)`）：先按总字符分档——
- **短（≤ `AI_SINGLE_SHOT_CHARS`）** → 单次直出 `{theme, overview, key_points[], keywords[]}`，并合成一条覆盖全片时间的章节（时间轴 = 首末分段）。
- **长（超出）** → `segment_chapters` 按 `AI_CHAPTER_MAX_CHARS` 字符预算切块（≤ `AI_MAX_CHAPTERS`），保证每章起止时间确定、连续；**map**：每章独立摘要（`_chapter_json`，并发 ≤ `AI_MAP_CONCURRENCY`）；**reduce**：`_reduce` 汇总各章 → 总 `{theme, overview, keywords[]}`。故 O(章节数) 次调 LLM，无损长视频。

统一组装为 `{theme, overview, chapters[{start,end,title,summary,key_points[],keywords[]}], keywords[], mindmap, summary(全文Markdown)}`，并附 `meta{lang,is_auto,model,used_source}`。

**思维导图**（`derive_mindmap`）：由结构**确定性派生**，`{title: theme, children:[{title: 章.title, children:[{title: 要点}]}]}`，**零额外 LLM 调用** —— 稳定、可复现、顶多翻车于 LLM 输出的章节标题本身。

**Markdown**（`_markdown`）：由结构渲染 `# theme / ## 章节 / ### [时间] title / - 要点 / ## 关键词`，供前端「复制 / 下载 .md」。

**前端思维导图（Mermaid v11 脑图，可交互）**：`index.html` 用 `<script type="module">` 从 `https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs` 导入并 `mermaid.initialize({ securityLevel:"strict", theme:"base", mindmap:{padding:12} })`，`mermaid.render(id, text)` 返回 `{svg}` 后注入容器。要点：
- **CSP 必须放行**：`script-src` 需含 `cdn.jsdelivr.net`，否则 mermaid 被拦（此前 real-browser 实测 `mindmap SVG rendered: False` 的根因）。由于 CSP 只放行 jsdelivr，markmap（`+esm` 依赖未打包）无法加载 → **不支持第三方交互库**。
- **懒加载竞态**：`app.js#ensureMermaid` 优先复用 `window.__mermaid` 实例，未就绪再动态 `import()`，彻底失败才回退纯 `<ul>` 列表。避免「模块加载慢于点击 → 误走降级」。
- **标签转义**：mermaid 脑图对 `()`、`[]` 等字符会解析失败，`buildMindmap#safeLabel` 统一换全角括号。
- **交互在 mermaid SVG 上自实现**（`bindMindmapInteractions` + `setupMindmapDom`）：
  - `setupMindmapDom` 把 svg 顶层子元素全部包进一个 `<g id="mmViewport">`（便于整体缩放/平移，节点与连线一致移动），并给每个父节点加折叠徽标（`+`/`−`），点击=折叠/展开。
  - 交互 = 滚轮缩放（绕光标，scale∈[0.1,6]）+ mousedown 拖拽平移 + 点击节点折叠展开；`mmDragMoved` 区分「拖拽」与「点击」，避免拖完误触折叠。
  - **折叠用「路径键」而非 mermaid 的迭代 id**：mermaid 会重排 id（`id)Label(` 不落到 DOM，只给 `node_N`），故 `indexTree` 用 `0` / `0.0` / `0.0.0` 这样的路径键，`buildMindmap` 据此跳过已折叠子树并整棵重绘。
  - 折叠态/视图态存 `mmCollapsed`(Set) + `mmView{scale,tx,ty}`；`toggleFold` → 客户端重绘（不触发 busy）；工具栏 `mmZoomIn/Out/Reset/ExpandAll/CollapseAll`，`window.__mm` 暴露状态（测试/调试钩子）。
- **结果缓存**：每个功能结果按 `url→feature` 缓存（`featureCache`），未换链接/未刷新前不变；各面板「⟳ 重新生成」传 `force=true` 强制重算。⚠️ `onclick` 不能直接绑定 `handleSummary`（会把事件对象当 `force` 传入 → 恒真 → 永远重算），须包一层箭头函数。
- **清晰度/格式响应式网格**：`#formatList` 为 `grid gap-2 repeat(auto-fill,minmax(220px,1fr))` —— 宽屏自动多列、窄屏/手机单列；每项为紧凑卡片（**只展示清晰度 + 格式 · 大小**，v0.2.7 起去掉「需合并音视频/单文件」角标——合并属后台工作、对用户无意义）。⚠️ **须在 `_clean_formats` 里过滤 `_is_storyboard`（mhtml/sb*）伪格式**：否则 yt-dlp 会混入 `sb0`-`sb3`（`ext='mhtml'`、`filesize=0`、无码流）故事板作为「180p/90p/45p/27p MHTML」选项，用户点下去下载不到真视频。
- **换链接清空 AI 内容**（`resetAnalyze`）：`parseSingle` 以 `activeUrl() !== url` 判定链接是否变化，变化/失败时调用 `resetAnalyze()` 清空上一视频的可见产物与跨链接临时状态（`lastSummary`/`askHistory`/`subModeTranslate`/`mmCollapsed`/`mmTree`/`mmFitted`）。⚠️ 不删 `featureCache`（按 url 分键），切回旧链接仍可命中缓存恢复。
- **AI 问答 = 气泡聊天（v0.2.6）**：`#askOutput` 为 `flex` 气泡容器；`handleAsk` 用 `askBubble(role,text)` 造**用户问题气泡（右、`bg-brand` 白字）+ AI 回答气泡（左、灰底）**，`[data-role=user|assistant]` 标记，SSE 增量写进单个 AI 气泡。**统一 `textContent`（不用 innerHTML）** 防 XSS；`handleAsk` 开头 `if (btn.disabled) return` 防回答中重复提交；`askClearChat()` 清空并重置 `askHistory`，`resetAnalyze` 与 `#askClear` 复用。**流式竞态加固（`askGen` 令牌 + `askAbort` AbortController）**：回答进行中「清空/换链接」会 `askAbort.abort()` 取消在途请求并 `askGen++`，使陈旧流（`myGen < askGen`）既不写 DOM、也不 `askHistory.push`（防旧问答复活污染下一问上下文）、不复位按钮；Abort 静默、空回复把「…」占位改为「(无回答)」；`#askClear` 传 `resetBtn=true` 复位按钮、`resetAnalyze` 用默认不复位（交由 `setAnBtns`）。

**AI 问答**（`generate_answer`，SSE）：问答前不一定调过摘要。为能回答「第几分钟讲了什么」，把上下文艺术从「一章一块（丢失逐条时间戳）」升级为**逐字幕条**打分（`_retrieve_timeline`）+ 取时间连续、逐行带 `[MM:SS - MM:SS]` 的窗口（预算 ≤ `AI_CHAT_CONTEXT_CHARS`，关键词全不匹配则退回 `segment_chapters` 章节级上下文作兜底）；`_ask_messages` 提示词要求：对定位题（哪一段/哪几分钟/什么时候）依据时间戳回答具体时间段、可合成区间、未覆盖如实说明且**不编造时间**。→ 拼 system + history(最近6条) + question → `_chat_stream` 逐 token 流式返回（`AsyncIterator[str]`）。路由经原生 `StreamingResponse` 手写 `data: {"delta": ...}` 帧（`fastapi.sse.EventSourceResponse` 本版本对 `ServerSentEvent.encode` 处理异常，已规避）。

**翻译**（`translate`）：与摘要共用 `_chat`，带 `target_lang` 交 LLM。Key 仅服务端。

> v1 的 `ai.summarize(text)`（单次、无时间轴）仍保留 `_collect_transcript_sync` 在 routes 内（未被调用，留作回归/对比），当前摘要端点已升级为分段版。

## 7. 安全模型

见 [SECURITY.md](SECURITY.md) 与 [security.py](../app/security.py)。要点：URL/SSRF 校验、滑窗限流、安全响应头+CSP、错误脱敏、临时文件过期清理、日志脱敏。

## 8. 技术选型理由

| 选择 | 理由 |
|---|---|
| yt-dlp | 十几万 Star、全平台维护频繁、功能最全 → 「站在巨人肩膀上」 |
| FastAPI + uvicorn | 异步、Pydantic 校验、原生 Swagger、极轻 |
| 内存 job store | v1 无 DB、无账户、单机；横扩需 Redis（PLAN v2 池） |
| 轮询（非 WebSocket） | 最简、无长连接、天然兼容无状态部署 |
| 前端静态 HTML + Tailwind CDN | 零构建、零依赖、打开即用；符合参考站风格 |
| 前端解析 + 后端 API 分离 | 前后端清晰，后续可拆独立前端 |
