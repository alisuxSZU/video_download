# DESIGN — 架构设计详解

> 解释「为什么这么设计」，供扩展时快速理解既有结构。代码以 [app/](../app/) 为准，本文与代码同步。范围与限制见 [PLAN.md](PLAN.md)。

## 1. 模块与职责

```
app/
  main.py        # FastAPI 入口、lifespan(启动清旧临时文件+建库+后台清理)、安全中间件、挂载 /static、/ 返回 index.html
  config.py      # 从 .env 读取的 Settings 单例（集中配置；含 Stripe 套餐表与 billing_enabled 推导）
  models.py      # Pydantic 请求体 + Job 数据类(dataclass + threading.Lock)
  security.py    # validate_url(URL/SSRF校验)、RateLimiter(滑窗限流)、安全响应头、错误脱敏、sanitize_filename、全局异常（含 HTTPException 错误展平）
  downloader.py  # yt-dlp 薄封装：probe/build_format_string/run_download/progress_hook/extract_subtitle/subtitle_to_text + 抖音路由 + 免费清晰度封顶
  tasks.py       # 内存 job store + ThreadPoolExecutor + 总量上限 MAX_ACTIVE_JOBS + 后台 TTL 清理 + 取消
  ai.py          # OpenAI 兼容 LLM：translate + 结构化摘要(单次/分块map-reduce+导图派生) + SSE流式问答
  douyin.py      # 抖音特例：Playwright 无头浏览器解析 + httpx 直连下载
  db.py          # SQLite 数据层（v0.7.0）：users/auth_tokens/orders/webhook_events/ai_usage + WAL + BEGIN IMMEDIATE 写事务
  auth.py        # 账户与会话（v0.7.0）：PBKDF2 密码 / 不透明令牌(仅存哈希) / is_pro / AI 日配额
  billing.py     # Stripe 支付（v0.7.0）：Checkout 下单 / Webhook 验签与事件去重 / 幂等履约
  routes.py      # 全部 API 端点（纯 HTTP 层，不含业务）
static/          # index.html / app.js / styles.css（单页前端，Tailwind CDN，零构建）
data/            # vdl.db（SQLite，gitignore）
```

**分层**：`routes`(HTTP) → `tasks`(状态中心) → `downloader`(yt-dlp) / `ai`(LLM) / `douyin`(抖音)；`security` 为横切面；`db → auth/billing` 为账户支付纵切（v0.7.0）。downloader 可脱离 HTTP 独立用于脚本。

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

**AI 结构化摘要**（`ai.summarize(segments, meta)`）：**v0.3.1 起按 `_should_split(segments)` 分档**——`时长 ≥ AI_CHAPTER_GOAL_SECONDS(600s≈10分钟) 或字符 ≥ AI_SINGLE_SHOT_CHARS(30000)` 任一满足即判为「需分块」，否则单次直出。（⚠️ 此前仅按字符判长短：口述访谈每分钟字数少，60+ 分钟整段也能 <30000 字，被误判为「短」并合成一条覆盖整片时长的**伪章节** `[00:00-01:02:52] 标题=主题、摘要=总览`，即主人反馈的「章节时段莫名其妙 / 前后重复」。）——
- **短（`not _should_split`）** → 单次直出 `{theme, overview, key_points[], keywords[]}`，`chapters=[]`、**不再合成伪章节**（`_markdown` 一律纯叙述、**不输出 `## 章节`**——长短皆然；`derive_mindmap` 无章节时回退 `根→要点`）。
- **长（`_should_split`）** → `segment_chapters` 切块（≤ `AI_MAX_CHAPTERS`），保证每章起止时间确定、连续；**切分判据 v0.2.8 起为「时间目标 ≥ `AI_CHAPTER_GOAL_SECONDS`（默认 600s≈10 分钟）或字符 ≥ `AI_CHAPTER_MAX_CHARS` 先到先切」**——更细的时间轴（此前纯字符预算 ≈ 30 分钟/章）；**map**：每章独立摘要（`_chapter_json`，并发 ≤ `AI_MAP_CONCURRENCY`）；**reduce**：`_reduce` 汇总各章 → 总 `{theme, overview, keywords[]}`。故 O(章节数) 次调 LLM，无损长视频。

统一组装为 `{theme, overview, chapters[{start,end,title,summary,key_points[],keywords[]}], keywords[], mindmap, summary(全文Markdown)}`，并附 `meta{lang,is_auto,model,used_source}`。

**思维导图**（`derive_mindmap`）：由结构**确定性派生**，`{title: theme, children:[{title: 章.title, children:[{title: 要点}]}]}`，**零额外 LLM 调用** —— 稳定、可复现、顶多翻车于 LLM 输出的章节标题本身。

**Markdown**（`_markdown`）：由结构渲染 `# theme / 总览 / ## 要点 / ## 关键词`（**纯叙述**，**不内嵌 `## 章节`/时间戳**——时间轴由独立「章节·时间轴」面板承载，避免短视频「单条 [00:00-整片] 伪章节」与主题/总览重复），供前端「复制 / 下载 .md」。

**前端 Markdown 渲染（v0.2.8）**：摘要与问答**默认改为 markdown 排版**。引入两条 jsdelivr 经典脚本（CSP `script-src` 已放行 jsdelivr）——`marked@18/lib/marked.umd.js`（全局 `window.marked`，须 `lib/marked.umd.js`，裸包名是 ESM 不是 UMD）解析、`dompurify@3/dist/purify.min.js`（全局 `window.DOMPurify`）消毒（LLM 输出不可信，**必先消毒再入 DOM**）。排版样式全内联 `styles.css` 的 `.md`（h1-h3/p/ul·ol/li/内联 code/代码块 pre/blockquote/table/a），因 CSP `style-src` 无 jsdelivr、不可外部样式表。摘要 `#sumTheme/#sumOverview/#sumPoints/#sumKeywords` 结构化卡片收敛为单一 `#sumMd`；问答**流式期间仍 `textContent` 增量**（不闪烁）、**流结束且无错误**才一次性 `marked.parse` 渲染进气泡内 `.md`（`bubble.classList.remove('whitespace-pre-wrap')`）；`renderAskMarkdown` 在 `myGen === askGen` 且未 `errorOccurred` 时触发，保住流式竞态守卫。max `window.marked/DOMPurify` 缺失（CDN 失败）回退纯文本并 `console.warn`（不抛 console.error，过 e2e 无报错门禁）。

**前端思维导图（Mermaid v11 脑图，可交互）**：`index.html` 用 `<script type="module">` 从 `https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs` 导入并 `mermaid.initialize({ securityLevel:"strict", theme:"base", mindmap:{padding:12} })`，`mermaid.render(id, text)` 返回 `{svg}` 后注入容器。要点：
- **CSP 必须放行**：`script-src` 需含 `cdn.jsdelivr.net`，否则 mermaid 被拦（此前 real-browser 实测 `mindmap SVG rendered: False` 的根因）。由于 CSP 只放行 jsdelivr，markmap（`+esm` 依赖未打包）无法加载 → **不支持第三方交互库**。
- **懒加载竞态**：`app.js#ensureMermaid` 优先复用 `window.__mermaid` 实例，未就绪再动态 `import()`，彻底失败才回退纯 `<ul>` 列表。避免「模块加载慢于点击 → 误走降级」。
- **标签转义**：mermaid 脑图对 `()`、`[]` 等字符会解析失败，`buildMindmap#safeLabel` 统一换全角括号。
- **交互在 mermaid SVG 上自实现**（`bindMindmapInteractions` + `setupMindmapDom`）：
  - `setupMindmapDom` 把 svg 顶层子元素全部包进一个 `<g id="mmViewport">`（便于整体缩放/平移，节点与连线一致移动），并给每个父节点加折叠徽标（`+`/`−`），点击=折叠/展开。
  - 交互 = 滚轮缩放（绕光标，scale∈[0.1,6]）+ mousedown 拖拽平移 + 点击节点折叠展开；`mmDragMoved` 区分「拖拽」与「点击」，避免拖完误触折叠。
  - **折叠用「路径键」而非 mermaid 的迭代 id**：mermaid 会重排 id（`id)Label(` 不落到 DOM，只给 `node_N`），故 `indexTree` 用 `0` / `0.0` / `0.0.0` 这样的路径键，`buildMindmap` 据此跳过已折叠子树并整棵重绘。
  - 折叠态/视图态存 `mmCollapsed`(Set) + `mmView{scale,tx,ty}`；`toggleFold` → 客户端重绘（不触发 busy）；工具栏 `mmZoomIn/Out/Reset/ExpandAll/CollapseAll`，`window.__mm` 暴露状态（测试/调试钩子）。
  - **svg 归一化（v0.2.9，修「导图缩成一团」）**：mermaid v11 的 mindmap 输出自带 `viewBox` + `width/height=100%`，浏览器据此已自动 fit（~0.43）；`setupMindmapDom` 里的 `fitView` 又按容器对 `#mmViewport` 再乘一次 scale（~0.40）→ 两次相乘 ≈0.17，树被缩成一团。修复：渲染后把 svg 归一化到 **1 用户单位 = 1px** —— `viewBox` 设为内容 bbox + pad，`width/height` 设为内容实际 px，`fitView` 只做一次（scale≈0.45，树占容器 92%）；并给 `#mindContainer` 加 `overflow:hidden` 防溢出滚动条。
  - **全屏（v0.2.8）**：工具栏 `#mmFullscreen` 切换 `#panelMind` 的 `mm-fullscreen` class（`position:fixed; inset:0; z-index:120; display:flex; flex-direction:column`），复用**同一个 `#mindContainer`** → 拖拽/缩放/折叠零改动保留；`document.body.style.overflow='hidden'` 锁背景滚动，随后 `mmFitted=false; fitView()`（容器尺寸已变须重 fit）。`#toast` 提到 `z-[130]` 以便全屏时仍可见。⚠️ **必坑：`position:fixed` 会被带 `transform` 的祖先当作包含块**——「解析结果卡片」（v0.2.x 面板外的 `animate-rise` 卡片，identity `matrix(1,0,0,1,0,0)`）使全屏态被困在卡片内而非视口。故 `toggleMindFullscreen` 进入全屏时把 `#panelMind` **搬到 `<body>`**（`mmRestoreParent/mmRestoreNext` 记原位），退出用 `insertBefore` 还原，绕开 transform 包含块；JS 引用全是 id/事件在节点上，搬移不破坏交互。
  - **下载高清 PNG（v0.2.8，原生实现不加库）**：`#mmDownload` → `exportMindmapPNG()`。⚠️ **mermaid v11 的 mindmap 标签是 `<foreignObject>`**，直接 serialize→`<img>`→canvas 光栅化会**丢文字（空白）** —— 须先在 clone 上把 `foreignObject` 换成 `<text>`（用 live 节点 `getComputedStyle` 读字号/颜色，避免字体样式级联丢失），去掉 `.mm-fold` 折叠徽标与 `#mmViewport` 的平移缩放 transform，补 `viewBox`/`width`/`height` + 白底 `<rect>`，再 `XMLSerializer→Blob→Image→canvas`（scale ≤3、最长边 ≤4096）。⚠️ **CSP `img-src`（security.py）必须加 `blob:`**，否则 `new Image().src=objectURL` 被拦、静默失败。导出尊重当前折叠态（要全量先点「全部展开」）。
- **结果缓存**：每个功能结果按 `url→feature` 缓存（`featureCache`），未换链接/未刷新前不变；各面板「⟳ 重新生成」传 `force=true` 强制重算。⚠️ `onclick` 不能直接绑定 `handleSummary`（会把事件对象当 `force` 传入 → 恒真 → 永远重算），须包一层箭头函数。
- **清晰度/格式响应式网格**：`#formatList` 为 `grid gap-2 repeat(auto-fill,minmax(220px,1fr))` —— 宽屏自动多列、窄屏/手机单列；每项为紧凑卡片（**只展示清晰度 + 格式 · 大小**，v0.2.7 起去掉「需合并音视频/单文件」角标——合并属后台工作、对用户无意义）。⚠️ **须在 `_clean_formats` 里过滤 `_is_storyboard`（mhtml/sb*）伪格式**：否则 yt-dlp 会混入 `sb0`-`sb3`（`ext='mhtml'`、`filesize=0`、无码流）故事板作为「180p/90p/45p/27p MHTML」选项，用户点下去下载不到真视频。
- **换链接清空 AI 内容**（`resetAnalyze`）：`parseSingle` 以 `activeUrl() !== url` 判定链接是否变化，变化/失败时调用 `resetAnalyze()` 清空上一视频的可见产物与跨链接临时状态（`lastSummary`/`askHistory`/`subModeTranslate`/`mmCollapsed`/`mmTree`/`mmFitted`）。⚠️ 不删 `featureCache`（按 url 分键），切回旧链接仍可命中缓存恢复。
- **AI 问答 = 气泡聊天（v0.2.6）**：`#askOutput` 为 `flex` 气泡容器；`handleAsk` 用 `askBubble(role,text)` 造**用户问题气泡（右、`bg-brand` 白字）+ AI 回答气泡（左、灰底）**，`[data-role=user|assistant]` 标记，SSE 增量写进单个 AI 气泡。**统一 `textContent`（不用 innerHTML）** 防 XSS；`handleAsk` 开头 `if (btn.disabled) return` 防回答中重复提交；`askClearChat()` 清空并重置 `askHistory`，`resetAnalyze` 与 `#askClear` 复用。**流式竞态加固（`askGen` 令牌 + `askAbort` AbortController）**：回答进行中「清空/换链接」会 `askAbort.abort()` 取消在途请求并 `askGen++`，使陈旧流（`myGen < askGen`）既不写 DOM、也不 `askHistory.push`（防旧问答复活污染下一问上下文）、不复位按钮；Abort 静默、空回复把「…」占位改为「(无回答)」；`#askClear` 传 `resetBtn=true` 复位按钮、`resetAnalyze` 用默认不复位（交由 `setAnBtns`）。**输入框清空（v0.2.9）**：`handleAsk` 发送后立即 `$("#askInput").value = ""`，修「发送后输入框仍显示问题」。

**AI 问答**（`generate_answer`，SSE）：问答前不一定调过摘要。为能回答「第几分钟讲了什么」，把上下文艺术从「一章一块（丢失逐条时间戳）」升级为**逐字幕条**打分（`_retrieve_timeline`）+ 取时间连续、逐行带 `[MM:SS - MM:SS]` 的窗口（预算 ≤ `AI_CHAT_CONTEXT_CHARS`，关键词全不匹配则退回 `segment_chapters` 章节级上下文作兜底）；`_ask_messages` 提示词要求：对定位题（哪一段/哪几分钟/什么时候）依据时间戳回答具体时间段、可合成区间、未覆盖如实说明且**不编造时间**。→ 拼 system + history(最近6条) + question → `_chat_stream` 逐 token 流式返回（`AsyncIterator[str]`）。路由经原生 `StreamingResponse` 手写 `data: {"delta": ...}` 帧（`fastapi.sse.EventSourceResponse` 本版本对 `ServerSentEvent.encode` 处理异常，已规避）。**及时性（v0.2.9）**：把「字幕抽取 + 上下文构建」冷路径也挪进流内，SSE 一建立即推 `{"status":"preparing"}`、字幕就绪推 `{"status":"generating"}`，再逐 token 推 `delta`，避免首 token 前长时间空白；响应带 `Cache-Control: no-cache` + `X-Accel-Buffering: no` 防反代缓冲。**错误（v0.2.9）**：无字幕/LLM 失败以流内 `{"error": "..."}` 帧终止（**HTTP 200** + 不再补发 `done`），前端据此停止拼接——不再拆成 502 异常响应。其中 `no_subtitles` 帧**额外带 `"code": "no_subtitles"`**，LLM 失败与字幕抽取 generic 异常帧**无 `code`**。

**翻译**（`translate`）：与摘要共用 `_chat`，带 `target_lang` 交 LLM。Key 仅服务端。**目标语言选择（v0.3.0）**：前端 `#subLangSel` 下拉提供 `简体中文/繁体中文/英文/日语/朝鲜语` 5 项（默认简体），`handleSubtitle(translate)` 读当前选中值作为 `target_lang`（省略则只提取不翻译）；**缓存键按目标语言区分**（`subTranslate:{target_lang}`），切换语言后点「翻译」或「重新生成」即按新语言重翻。系统提示 `...翻译成{target_lang}，若文本已是该语言则原样返回`，故任何语言名均可、已为目标语言的文本为 no-op。**UI 合并为单钮（v0.3.0）**：主人反馈「提取字幕 / 翻译字幕」两个按钮共用同一面板、无标识、用法不清楚 → 把两个按钮**合并成单个「🎬 字幕」按钮**（六宫格变五宫格）。点「🎬 字幕」恒为**原字幕视图**；面板内一行「翻译为：`[#subLangSel]` `[#subTranslateBtn 翻译]`」点了即翻。面板加**模式标签 `#subModeLabel`**（`原字幕` / `已翻译为：X`）+ 下载文案动态（原字幕 `下载 SRT`、译文 `下载 TXT（X）`），让用户在同一个面板里一眼看清当前看的是哪种结果。想回原字幕再点一次「🎬 字幕」即可（恒 `handleSubtitle(false)`）。

**AI 生成中「动图」反馈（v0.3.0）**：主人反馈「章节时间轴生成时的提示语啰嗦」，截图显示全局 `#anLoading` 一行 + 面板头一行 + 面板内占位一行，三行几乎同义叠加。为给「AI 正在工作」一个可见、又不啰嗦的反馈，改为**双形态动效**：全局 `#anLoading` 用「旋转环 `.spin` + 短文案 `#anLoadingText`（`startBusy(msg)` 写入）」的 flex 行；各面板占位用 `#skelHTML(rows)` 生成的**骨架屏**（`.skel` 流光条，按行数差宽，视觉即「内容正在成形」），替代原先具体又重复的句子占位。`#anLoading` 仍以 `hidden` class 做开关，故 e2e 的 `wait_busy`（等它隐藏）无需改；CSS 只追加 `.spin`/`.skel` 与对应 keyframes。

**各模块下载 / 导出（v0.3.0，纯前端 Blob）**：调用方在拿到数据后本地生成下载，**无对应后端端点**——
- **字幕（`makeSubDownload`）**：先 `link.classList.remove("hidden")` 把 `#subDl` 的 `<a download>` 揭示（此前恒 `hidden`，下载功能形同虚设——有内容即显示入口）；文件名：译文 `字幕-<target_lang>.txt`、原文 `字幕.<format||srt>`。提取失败时回到 `handleSubtitle` 的 `catch` 把 `#subDl` 重新 `hidden`，避免残留过期下载。
- **章节时间轴（`downloadChapters`）**：由 `lastChapters`（`renderChapters` 缓存）渲染 `# 章节时间轴 / ## [MM:SS - MM:SS] title / summary / - key_points` → `章节时间轴.md`。
- **问答（`exportChat`）**：由 `askHistory` 渲染 `# 视频问答记录 / **我**：… / **AI**：…` → `视频问答记录.md`（不包含 flow 状态帧/错误注记，只含提问与完整回答）。
- **摘要（`#sumDownload`）** 与 **思维导图（`#mmDownload`，PNG）** 为既有下载，沿用不变。
- 统一经 `Blob→a[download].click()` 触发；`resetAnalyze` 清空 `lastChapters`，避免换链接后导出陈旧章节。

> v1 的 `ai.summarize(text)`（单次、无时间轴）仍保留 `_collect_transcript_sync` 在 routes 内（未被调用，留作回归/对比），当前摘要端点已升级为分段版。

## 7. 账户与支付架构（v0.7.0）

> 完整设计方案（决策/链路/表结构/权益矩阵）见 [MEMBERSHIP.md](MEMBERSHIP.md)；安全专项见 [SECURITY.md](SECURITY.md) §10；接口契约见 [API.md](API.md) §12/§13。此处只讲「为什么这么设计」。

**为什么是 Stripe Checkout 托管收银台而非自建收银页 / Elements**：本站全程不接触卡号 → PCI 合规负担最小；前端只传 `plan` 键、金额以后台 Price 为唯一事实源 → 金额无法被篡改；一次性 `mode=payment` 而非订阅 → 免去 dunning/取消/发票复杂度，「重复购买叠加天数」对用户直觉且实现简单（`MAX(当前到期, now) + days`）。

**为什么「履约只信 Webhook、不信 success_url」**：浏览器回跳可被用户关闭/伪造/丢失（支付成功后关页面 = 永远拿不到会员）；Webhook 是 Stripe 服务器对服务器的签名回调，可重放验证。前端在 `?pay=success` 后轮询 `/api/auth/me` 只是**展示层**等待，权益以 Webhook 履约结果为准。

**为什么三层幂等**（`stripe_session_id` UNIQUE / `event_id` 主键 / 订单状态机条件更新）：Stripe 官方明确 Webhook 会**重复且乱序**投递；仅靠一层在并发/崩溃恢复下会重复加时长。三层各挡一类：UNIQUE 挡一个会话两订单、event 主键挡事件重放、`UPDATE ... WHERE status='pending'` 条件更新（`BEGIN IMMEDIATE` 事务内）挡并发回调——履约代码执行 N 次也只生效 1 次。履约失败删事件占位 + 返 500 让 Stripe 重试，可恢复。

**为什么 SQLite 而非 Postgres/MySQL**：契合 `--workers 1` 单进程部署（内存 job store 同理）；零额外服务、运维成本为零；写并发用「单连接 + Lock + `BEGIN IMMEDIATE`」串行化（本站写频率低：注册/下单/履约/配额计数）。多实例横扩时才需换 Redis/SQL（PLAN v2 池）。

**为什么不透明令牌而非 JWT**：服务端可主动吊销（登出即删行）、每次请求可查实时会员状态；无 JWT 过期/撤销难题。代价是每请求一次库查询（SQLite 本地读，代价可忽略）。

**权益拦截在服务端而非前端**：前端锁标（1080p+ 👑）只是 UX；真正的强制在 `POST /api/download`（格式 height 校验 + 默认格式串压制 `[height<=720]` 防绕过）、`/api/subtitles`（翻译 403）、AI 端点（日配额 `ai_usage` 按游客 IP / 账户 ID 双口径，成功调用才计数）。PRO 判定 `member_expire_at > now` 单一来源。

## 8. 安全模型

见 [SECURITY.md](SECURITY.md) 与 [security.py](../app/security.py)。要点：URL/SSRF 校验、滑窗限流、安全响应头+CSP、错误脱敏、临时文件过期清理、日志脱敏；v0.7.0 起新增 `HTTPException` 错误展平（统一 `{ok,error,code}`，前端弹窗/状态清理依赖 `code`）。

## 9. 技术选型理由

| 选择 | 理由 |
|---|---|
| yt-dlp | 十几万 Star、全平台维护频繁、功能最全 → 「站在巨人肩膀上」 |
| FastAPI + uvicorn | 异步、Pydantic 校验、原生 Swagger、极轻 |
| 内存 job store + SQLite 账户库 | 下载任务 ephemeral 存内存即可；账户/订单/会员需持久化 → SQLite 零运维（v0.7.0）；横扩需 Redis（PLAN v2 池） |
| 轮询（非 WebSocket） | 最简、无长连接、天然兼容无状态部署 |
| 前端静态 HTML + Tailwind CDN | 零构建、零依赖、打开即用；符合参考站风格 |
| 前端解析 + 后端 API 分离 | 前后端清晰，后续可拆独立前端 |
| Stripe Checkout + Python SDK | 托管收银台免 PCI 负担；官方 SDK 处理签名/类型；一次性 payment 模式最简（v0.7.0） |
