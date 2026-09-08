# PLAN — 万能视频下载网站 · 总方案与当前状态

> **扩展任何新能力前先读这里**。本文 = 立项总纲 + 当前状态快照 + 下一步。技术细节看 [DESIGN.md](DESIGN.md) / [API.md](API.md) / [SECURITY.md](SECURITY.md)，实现进度回溯看 [CHANGELOG.md](CHANGELOG.md)。

---

## 一、是什么 / 为什么 / 怎么干

很多同学想把手上的视频保存到本地，但部分平台不支持下载、清晰度受限、批量难、还被水印缠。本项目做一个**万能视频下载网站**：粘贴链接 → 解析多清晰度/格式 → 批量下载 → 提取/翻译字幕 → AI 摘要，附 PRO 付费展示。

**战略：站在巨人肩膀上**。封装 [yt-dlp](https://github.com/yt-dlp/yt-dlp)（十几万 Star）作下载引擎，纯 Python；**我们不重造下载引擎**，只做「安全 API 门面 + 精美前端 + 增值/付费」。抖音因 `a_bogus` 签名墙走**服务端无头浏览器**（Playwright 复用系统 Chrome，匿名游客会话，详见 CHANGELOG 0.1.2）。

> ⚠️ 仅供个人学习/研究/备份；遵守平台条款与版权法，严禁侵权转售。

## 二、里程碑状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 `v0.1.0` | 核心业务 + 前端精美 + 安全基础 | ✅ |
| M1.1 `v0.1.2` | 抖音下载（服务端无头浏览器） | ✅ |
| M2 | 字幕提取/翻译 + AI 摘要 | ✅ 完成（已真实浏览器端到端回归）：学习型结构化摘要 v2（章节·时间轴/思维导图/SSE 问答）+ 前端四面板，逐功能命中 + 结果缓存 + 换链接清空 + 问答气泡聊天均已通过 e2e |
| M3 | 加固上线（反代/HTTPS/备案/回归） | ⬜ |

> M2 已完成并通过端到端回归：`extract_subtitle`（翻译）、`/api/ai/summary`（v2 结构化，含章节时间轴 + 思维导图派生 + Markdown 全文）、`/api/ai/ask`（SSE 流式问答，前端气泡聊天）。`static/app.js` 的摘要面板已拆成「摘要 / 章节·时间轴 / 思维导图 / 问答」四个 tab，支持复制/下载 `.md`，并按 `url→feature` 缓存。**端到端验证**由仓库根 `e2e_test.py`（Playwright + 系统 Chrome，真实公网链接）回归。
> **限流注意**：5 个 AI 功能端点共用一个 `ai` 限流组，默认 `RATE_AI_PER_MIN=**3**`（每 IP 每分钟）——这是刻意的 API 止损；60s 内连点 ≥4 个功能会触发**应用自身** `429`（非 deepseek 外部限流）。做多功能联排回归时务必将 `RATE_AI_PER_MIN` 调高（如 100/999）再启动，否则会误报为「请求失败」。M2.1/M2.2（v0.2.8/v0.2.9）已经历 `e2e_test.py` 全绿回归，含一次「默认限流下 429」的踩坑复跑（见 CHANGELOG 0.2.9）。

## 三、当前环境（已核实）

- Windows 11 / Python 3.12.4；项目目录 `d:\LCP_agent\video_download`。
- 依赖：`fastapi` / `uvicorn` / `pydantic` / `httpx` / `yt-dlp` / `python-dotenv` / `playwright`。核心后端仅 yt-dlp 一处真正重量级依赖（LLM 走 httpx）。
- ✅ **ffmpeg 已装**（Gyan.FFmpeg 9.0.1，`YTDLP_FFMPEG_LOCATION` 已指向其可执行目录）；无 ffmpeg 时仍会降级单文件 progressive，见坑 #2。
- LLM 用 **OpenAI 兼容接口**：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，支持 DeepSeek/智谱/星火；无 Key 时字幕/摘要端点返回可读错误（`llm` 502）。
- 配置全集中在 `app/config.py`（读 `.env`）。分组：监听 / 下载与临时文件 / ffmpeg / cookies与proxy / 抖音 / 安全限流 / LLM / UA。样例见 `.env.example`。

## 四、已确认决策（已人工确认）

| 项 | 决策 |
|---|---|
| v1 功能 | ① 核心：粘贴链接→解析(标题/缩略图/时长/全部格式)→选格式→下载 ② 批量 ③ 字幕提取/翻译 ④ AI 摘要（v1 全上） |
| 前端形态 | 单页静态 HTML + Tailwind(Play CDN)，由 FastAPI `/static` 托管；纯手写、零构建、最轻量 |
| 付费能力 | v1 只做「价格展示 + PRO 体系」：定价/权益/对比/升级 CTA；支付端点为占位门面，不接真实支付、无 DB、无账户 |
| 部署形态 | **公开上线给他人用** → 必须做安全防护（见 SECURITY.md） |
| 实施节奏 | 核心业务先行跑通；前端打磨尽早完成；分析/设计文档同步沉淀到 `docs/` |
| Llm | 不内置密钥、不做本地 whisper 转写（太重）；LLM 仅服务端持 Key |

### 本期「学习型摘要」决策（v1.1，已人工确认）

对标 BibiGPT / NoteGPT，把 v1 的「纯文本摘要」升级为**学习导向的结构化解说**，下述已确认并实现：

| 项 | 决策 |
|---|---|
| 功能范围 | 摘要 + 章节·时间轴 + 思维导图 + 问答（对齐竞品） |
| 功能落点 | 四面板：摘要 / 章节·时间轴 / 思维导图 / 问答；结果可复制 + 下载 Markdown |
| 长视频/超长字幕 | 分块 **map-reduce**（`AI_SINGLE_SHOT_CHARS` 内单次直出，超出分块，≤ `AI_MAX_CHAPTERS`） |
| 无字幕视频 | **本期不做本地转写**（保持 v1 决策），返回 `no_subtitles` 友好空态 |
| 思维导图 | **确定性派生**（`derive_mindmap`，零额外 LLM 调用） |
| AI 问答 | 本期做**流式 SSE**（`/api/ai/ask`，`fetch` + ReadableStream 解析） |
| 功能落点（v1.2） | 六个功能（字幕提取/翻译、摘要、章节、思维导图、问答）统一成**一个「功能框」**，**只在视频解析成功后显示**；解析前整块隐藏，解析失败清空上次状态 |
| AI 限流（提醒） | 五个功能的 5 个 AI 端点共用限流组 `ai`，默认 `RATE_AI_PER_MIN=30`（每 IP 每分钟，v0.3.0 由 3 调高）；普通用户连点多个功能 + 问几个问题不打断，仍保留刷量拦截；需更严/更宽松可 `.env` 调 `RATE_AI_PER_MIN` |
| 模块合并 + 单输入框（v1.3，已实现） | 六个功能并入「解析结果」同一卡片（`#analyze` 移入 `#resultPanel`），**只保留顶部一个 `#urlInput`**；删除 `#analyzeUrl`/`#useCurrent` |
| 思维导图可交互（v1.3，已实现） | mermaid 脑图支持**拖拽平移 / 滚轮缩放（绕光标）/ 点击折叠展开 / 全部展开/折叠 / 重置视图**；因 CSP 只放行 jsdelivr，markmap 无法加载，故在 mermaid SVG 上**自实现** pan/zoom/fold（`#mmViewport` 组 + `window.__mm`） |
| 结果按链接缓存（v1.3，已实现） | 每个功能结果按 `url → feature` 缓存；未换链接/未刷新前**保持不变**；点各面板「⟳ 重新生成」才强制重算。⚠️ 绑定不能用 `onclick = fn` 直接传 `force`，否则事件对象被当作参数使 `!force` 恒假 → 永远重算（已改为 `() => handleSummary()`） |
| 问答分钟级定位（v1.3，已实现） | 旧逻辑按**章节整块**检索且章节内合并文本丢失逐条时间戳，模型只能凭章首尾估时间；改为**逐字幕条**打分、取时间连续、逐行带 `[MM:SS - MM:SS]` 的窗口 + 提示词要求返回具体时间段（可合成区间、未覆盖如实说明、不编造） |
| 清晰度/格式响应式网格（v1.3，已实现） | `#formatList` 由 `space-y-2`（每项占一整行）改为 `grid gap-2 repeat(auto-fill,minmax(220px,1fr))`：宽屏自动多列（实测 1440px 下 3 列）、手机回落单列；调用 `resetAnalyze()` 前 `activeUrl()!==url` 判定链接是否变化 |
| 换链接清空 AI 内容（v1.3，已实现） | `parseSingle` 检测到**换新链接**（或解析失败）时调用 `resetAnalyze()`：清空上一视频的字幕/摘要/章节/导图/问答可见内容与跨链接临时状态（`lastSummary`/`askHistory`/`subModeTranslate`/`mmCollapsed`/`mmTree`/`mmFitted`）。⚠️ 不删 `featureCache`（按 url 分键），切回旧链接仍可命中缓存恢复 |
| 下载后自动保存到本地（v1.3，已实现） | ① 彻底删除 `#progDownload` 与批量行的 `.dl-link`「下载」链接，不再有手动兜底；② 单条任务 `done` 后前端**自动点击** `/api/jobs/{id}/file`（`link.click()`，Content-Disposition 流式写盘，零内存）保存到用户本地下载目录；③ 批量队列每行 `done` 后经 **`fetch→Blob→anchor`** 逐条、串行自动保存（`enqueueBatchDownload` + `drainBatchQueue`：取完一条（fetch 完成）再 sleep 0.7s 取下一条），序列化避免并发触发下载引擎。单条用直连链接（内存友好），批量用 Blob（每次仅一份在内存）。⚠️ **浏览器「多文件自动下载」策略**：Chromium 限制无用户手势的自动下载，批量**第 2 条起**真实浏览器会弹**一次性**「此网站尝试下载多个文件→允许？」（每站点一次，允许后整批自动）；无头自动化需 `--enable-automatic-downloads` 才能放行以便测试 |
| 问答 = 气泡聊天（v0.2.6，已实现） | `#askOutput` 改为 `flex` 气泡容器；`handleAsk` 用 `askBubble(role,text)` 生成**用户问题气泡（右、`bg-brand` 白字）+ AI 回答气泡（左、灰底）**，SSE 增量写进单个 AI 气泡并自动滚动。**统一 `textContent`（不用 innerHTML）防 XSS**；`if (btn.disabled) return` 防回答中重复提交；`askClearChat()` 清空 + 重置 `askHistory`，`resetAnalyze`/`#askClear` 复用。**流式竞态加固**：模块级 `askGen`(令牌) + `askAbort`(AbortController) —— 回答进行中清空/换链接即 `askGen++` 且 `askAbort.abort()`，陈旧流（`myGen<askGen`）既不写 DOM、不 `askHistory.push`（防旧问答复活污染下一问上下文）、不复位按钮；Abort 静默不弹错；空回复把「…」占位改为「(无回答)」 |
| 格式角标去噪 + 剔 MHTML 故事板（v0.2.7，已实现） | ① `formatRow` 不再渲染右下角 `mergeChip`（「需合并音视频」/「单文件」），卡片只留**清晰度 + 格式 · 大小**，所有选项一致（合并是后台工作，对用户无意义）；② `app/downloader.py` 的 `_clean_formats` 加 `_is_storyboard(f)`（`ext/protocol=='mhtml'` → `continue`），剔除 yt-dlp 的 **`sb0`-`sb3` 故事板伪格式**（`ext='mhtml'`、`filesize=0`、无码流）——否则会以「180p/90p/45p/27p MHTML」混进列表，用户点下去下载不到真视频。只剔 mhtml 不动其余，避免误伤 Archive.org 等无 codec 信息的平台 |
| Markdown 渲染（v0.2.8，已实现） | 摘要 + 问答**默认改 markdown**：引入 `marked@18/lib/marked.umd.js`（解析）+ `dompurify@3/dist/purify.min.js`（消毒，LLM 输出不可信必先消毒），`.md` 排版样式全内联进 `styles.css`（CSP `style-src` 不放行外部样式表）。问答**流式期间仍 `textContent` 增量**，流结束且未被错误中断时一次性渲染；摘要 `#sumTheme/#sumOverview/#sumPoints/#sumKeywords` 结构卡片改为单一 `#sumMd` |
| 思维导图全屏 + 高清导出（v0.2.8，已实现） | ① 全屏用**页面内遮罩**（`#panelMind.mm-fullscreen`：`position:fixed; inset:0; z-index:120`），复用同一 `#mindContainer` → 拖拽/缩放/折叠零改动保留；⚠️ **`position:fixed` 会被带 transform 的祖先吞成包含块**（解析结果卡片 `animate-rise` 的 identity 矩阵实测被困 734×625 而非视口）→ 进入全屏把 `#panelMind` **搬到 `<body>`**、退出 `insertBefore` 还原（`mmRestoreParent/mmRestoreNext`），绕开 transform；②「下载高清 PNG」**原生实现**（serialize→Blob→Image→canvas），**mermaid v11 mindmap 标签是 `<foreignObject>`**，经 `<img>` 光栅化会丢文字 → 先在 clone 上换成 `<text>` 再导出。⚠️ CSP `img-src` 须加 `blob:`（否则 `new Image()` 被拦） |
| 章节细化 ~10 分钟（v0.2.8，已实现） | `segment_chapters` 增加 `goal_seconds` 判据：章累计时长 ≥ `AI_CHAPTER_GOAL_SECONDS`（默认 600）**或** 字符 ≥ `max_chars` 先到先切。全局默认，不改 models/routes/前端请求体；四处调用点（summarize/chapters/mindmap/retrieve）自动同一行为。短于 10 分钟视频仍单章 |
| AI 体验三处修复（v0.2.9，已实现） | 主人演示反馈的三个体验问题：① **思维导图缩成一团**（真 BUG、双倍缩放）：mermaid 输出的 svg 自带 `viewBox`（如 `3 3 1592 585`），若再写 `width/height=100%`，浏览器**自动**按 viewBox 把整树缩进容器（≈0.43），随后 `fitView` 又按 `getBBox()` 算一次 scale（≈0.40）→ 有效 ≈**0.17** → 压团。改法：`setupMindmapDom` 把 svg **归一为「1 用户单位=1 像素」**（viewBox 收敛到内容 bbox 留 10px 边距、宽高设像素尺寸、`#mindContainer` 加 `overflow:hidden`），`fitView` 故只作用一次，且滚轮/拖拽坐标（按像素）精确。② **问答 SSE 感知不及时**：`_chat_stream` 本为逐 token 流式，但生成前有「字幕抽取+上下文构建」冷路径（可能 2s+）静默等待，观感像「等全部生成完才吐」。改法：把冷路径挪进 `event_stream`，流一启即推 `{"status":"preparing"}`→`{"status":"generating"}`→逐 token `delta`，并给 `StreamingResponse` 加 `Cache-Control: no-cache`/`X-Accel-Buffering: no`/`Connection: keep-alive` 防反代缓冲；前端识别 `status` 帧把占位符实时切换为文案。③ **问答发送后输入框残留**（真 BUG）：`handleAsk` 读走 `q` 后从未清空 → 校验通过后即 `#askInput.value=''`。**安全取舍**：无字幕/LLM 错误改以流内 `{"error":...}` 帧终止（**HTTP 200**，服务端无法预知流式中间成败）而非 502；前端已能识别 `error` 帧、不补发 `done` 覆盖失败态 |

### v0.4.0 决策：视频信息 + AI 总结同屏（已人工确认，已实现）

| 项 | 决策 |
|---|---|
| 布局 | 桌面**左右双栏**（`grid lg:grid-cols-12`：左 col-span-5 视频信息/清晰度/下载，右 col-span-7 AI 功能）；<lg 自动**上下堆叠**（先左后右） |
| 右栏形态 | **Tab 标签栏**（摘要[默认] / 章节·时间轴 / 思维导图 / 字幕 / 问答），沿用原按钮 ID，保留 `.an-panel` 面板结构 |
| 自动摘要 | **默认关**（人工确认），右栏 `#autoSumToggle` 开关 + localStorage（`vdl_auto_summary`）；开启后解析成功自动调 `/api/ai/summary`（缓存命中直接渲染），无需再点一次 |
| 视频描述 | `/api/parse` 补 `description`（≤800 字符，空白压缩）；左栏 `textContent` 渲染 + 折叠 4 行 + 「展开/收起」（>120 字） |
| 范围 | 本次只做布局 + 自动摘要；Visual Storytelling / 动态网站 / 互动指南 不在本期 |

### v0.4.1 决策：验收反馈修复（已人工确认，已实现 + 回归）

| 项 | 决策 |
|---|---|
| 滚动遮挡 | 全局 `scroll-margin-top: 5rem`（`#resultPanel/#batchPanel/.an-panel`），`scrollIntoView` 自动避开 sticky 导航 |
| 标题 | 右栏标题单行省略（完整进 `title` 悬停）；左栏标题 `.clamp-title` 3 行省略（完整进 `title`）；封面 `max-h-32`（≤128px）横版铺满、零空白 |
| Tab | 解析成功后**默认无选中**；`#anEmpty` 空态占位（点击标签后隐藏）；删除「以下功能…」说明段（主人要求） |
| 等高 | 左右卡片等高（grid stretch）；右栏**头部（标题+Tab）固定**；`#anBody` 内容区 `flex-1 min-h-0 overflow-y-auto` |
| 面板 | 问答/字幕/摘要/章节 桌面下 `height:100%; margin-top:0` + 内容区 `flex-1 min-h-0`——内容少撑满（无空白）、内容多**仅面板内一个滚动条** |
| 居中 | 解析中（`#analyze[hidden]`）左栏卡片占满整行、`max-width:48rem` 居中；完成后恢复左右分栏（`:has()` 方案） |
| LLM 加固 | `_chat`（网络/5xx/429 重试 1 次）；`_chat_json`（JSON 格式异常重新生成 1 次）——长视频 map-reduce 偶发 502 归零 |

## 五、关键决策与坑（开发时避免重踩）

合并原 OVERVIEW/PLAN 的坑，去重后保留下述高价值项：

| # | 坑 / 决策 | 后果与做法 |
|---|---|---|
| 1 | **必须单进程单 worker** | job store/限流存内存，多 worker 各自独立 → 轮询 404。部署 `--workers 1`；横扩需换 Redis（v2） |
| 2 | **无 ffmpeg 降级** | `_is_progressive` 对平台不返回 codec 的档（如 Archive.org）**视为单文件**，不误拼 `+bestaudio`；高清档合并与 m3u8 需 ffmpeg |
| 3 | 进度用**轮询**（1.1s）非 WebSocket | 最简、无长连接、天然兼容无状态部署 |
| 4 | **抖音不合成 fetch** | 页内 JS 发起的 `fetch` 也缺 `a_bogus`；须 `page.expect_response` 拦截页面对 `aweme/v1/web/aweme/detail` 的**真实响应** |
| 5 | **Playwright 单线程占有** | greenlet 绑创建线程；`resolve()` 被 parse 线程与下载线程并发调用会 `greenlet.error`。用单浏览器专属线程 + `queue.Queue`+`Future` 串行提交 |
| 6 | **Playwright 超时单位是毫秒** | 传秒会「45ms 秒败」；`timeout_ms=int(sec*1000)` |
| 7 | `wait_until="networkidle"` 永不 settle | 抖音长连接 → 改 `domcontentloaded` |
| 8 | `_retry_antibot` 处理瞬时抖动 | B站 `HTTP 412/429/403`、连接级 SSL reset/EOF 等带退避重试可自愈；确定性错误（404/不支持）直接抛 |
| 9 | **cookiefile/proxy 不对字节系转发** | `_bytedance` 标记，避免运营者 Cookie 泄漏给抖音；抖音一律匿名游客会话 |
| 10 | `/file` 用 FileResponse 流式 | Windows 大文件不整读内存；路径尽量短（`TEMP_DIR=D:/vdl_tmp` 规避 260 限制） |
| 11 | **字幕自动回退** | 前端硬编码 `lang=zh` 常失败；后端 `_ordered_sub_candidates` 逐条尝试直到成功 |
| 11b | **B 站字幕需登录态 + 弹幕当字幕** | B 站部分视频（如 BV1pGdsB2Ebq）AI 字幕需登录态（`need_login_subtitle=True`），无 cookie 时 B 站在任何阶段都不返回真实字幕 URL（`subtitles`/`automatic_captions`/`requested_subtitles` 全空），只给 `danmaku`（弹幕）XML。此前 `/api/subtitles` 用「选最大文件」会把弹幕 XML 当字幕返回（`subtitle_to_segments` 解析 0 段）→ 摘要侧报 `no_subtitles`。已修：`_subtitles_list`/`_finalize` 排除弹幕与纯 xml，摘要回退走手动桶 `extract_subtitle("",False)`；`no_subtitles` 的 `error` 现带**登录引导语**（B 站 `need_login_subtitle=True` 时如实告知「确有字幕但需登录态，请配置 COOKIES_FILE/SESSDATA」），非 B 站/真无字幕不误报；**配置 `COOKIES_FILE`（B 站登录 cookie）后**此类视频才能真正出字稿与摘要，否则诚实 `no_subtitles`（见 CHANGELOG 0.2.0 Fixed） |
| 12 | 二次标准化字幕列表 | 避免 `AttributeError: 'list' object has no attribute 'items'` 500（`_collect_transcript_sync` 直接取 `payload['subtitles']`） |
| 13 | **参考站 UI 语言** | 纯白底 `#ffffff`、大标题 `#0f172a` 36px/900、副标题 `#64748b`、主按钮亮蓝 `#1777ff` 圆角满、卡片白底淡边框、手机2列→桌面3列网格、多色马卡龙标签芯片；在此之上加付费引导 |
| 14 | 平台强反爬 | X/Instagram/TikTok 多数**预期失败**（需 cookies/JS 签名）；YouTube 部分需登录；都返回友好中文错误而非 500 |
| 15 | **mermaid 输出 svg 的 viewBox 会双倍缩小**（v0.2.9 真 BUG） | mermaid v11 mindmap 的 `<svg>` 自带 `viewBox`（如 `3 3 1592 585`）。若保留它且设 `width/height=100%`，浏览器会**自动**按 viewBox 整树缩进容器（≈0.43），随后 `fitView` 又按 `getBBox()`（用户单位）对容器像素再算一次 → 有效 ≈**0.17** → 图被压成一团。**修法**：把 svg **归一为「1 用户单位=1 像素」**——`viewBox` 收敛到内容 bbox（含折叠徽标，留边距）、宽高设内容像素尺寸、容器加 `overflow:hidden`；如此 `fitView` 的 scale 才是单次正确应用，且滚轮缩放/拖拽平移坐标（按像素）从「近似」变回**精确**。反过来，若某处又要 svg 自适应容器（不缩放平移），则应**去掉** `width/height=100%` 而用 fitView 统一控 scale |
| 16 | **SSE 要及时 = 首帧就要「流在动」**（v0.2.9） | 流式「及时性」不只是 `_chat_stream` 逐 token——生成前的冷路径（字幕抽取+上下文构建）若在流外，首帧前会静默等待（观感像「等全生成完才吐」）。修法：把冷路径**挪进 `event_stream`**，流一建立即推 `{"status":"preparing"}`、可再推 `{"status":"generating"}`，前端据此把占位符切为文案；并给响应加 `X-Accel-Buffering: no`/`Cache-Control: no-cache` 防 nginx 等反代缓冲到收尾一次吐出。⚠️ 流式端点**不能**用非流式异常码拆错误（如 502），因为服务端无法预知流式进行中的中间成败，宜以流内 `error` 帧终止（HTTP 200） |
| 17 | **`hidden` 属性会被 `display` 类覆盖**（v0.4.1） | `#analyze` 改 `lg:flex` 后，UA 默认 `[hidden]{display:none}` 被 class 的 `display:flex` 覆盖 → 解析前右栏直接显示。修法：`#analyze[hidden]{display:none!important}`；任何带 display 类的显隐面板都要补 `.x.hidden{display:none}`（如 `#panelAsk.hidden` 等） |
| 18 | **`height:100%` + margin 溢出 → 双滚动条**（v0.4.1） | 面板撑满用 `height:100%` 时若残留 `mt-4`，内容会溢出 `#anBody` 16px → 内外同时出现滚动条（主人截图「2 个拖动条」）。修法：桌面媒体查询里统一 `margin-top: 0`；「撑满+内部滚动」三件套 = `flex/height:100%` + 内容区 `flex:1 min-h-0 overflow-y-auto` 的 `min-h-0` 不可省（否则 flex 子项不收缩、滚动失效） |
| 19 | **LLM 偶发 502：map-reduce 任一路失败整点 502**（v0.4.1） | 长视频摘要/章节/导图 = 多路并发 LLM 调用，任一路 网络抖动/5xx/JSON 格式异常 → 整个端点 502（e2e 连跑 24h 内观察 3 次，前端已友好降级但 console 报错）。修法：`_chat` 对 超时/连接/429/5xx 重试 1 次（401/400 不重试）；`_chat_json` 对「格式异常」**重新生成 1 次**（`_chat` 网络重试覆盖不到解析失败） |
| 20 | **等高 + 内部滚动要用「内容面板撑满」而非「内容区滚动」**（v0.4.1） | 若直接让 `#anBody` 滚动而面板内容只占自然高度，面板下方会露出大片空白（主人反馈多次：字幕/问答下方「空白太多」）。正确姿势：右栏 `flex-col`，头部 `shrink-0` 固定，面板 `height:100%; margin-top:0` 撑满，面板内**内容区**（subText/askOutput/sumMd/sumChapters）`flex-1 min-h-0 overflow-y-auto` → 无空白、单滚动条、头部固定三者兼得 |

## 六、已知限制（v1 有意为之）

- 无持久化：重启丢进行中任务；单进程。
- 字幕翻译/摘要依赖 LLM Key；无字幕视频无法摘要（**v1 不做本地 whisper 转写**）。
- **B 站部分视频的字幕需登录态才下发**（`need_login_subtitle=True`，如 `BV1pGdsB2Ebq`、`BV1mAAmzqEfP`）。项目未配 `COOKIES_FILE` 时取不到真实字稿，此类视频**正确返回带登录引导语的 `no_subtitles`**（error 如实说明「确有字幕但需登录态，请配置 COOKIES_FILE」；此前误把弹幕 XML 当字幕，已修）。要取这类视频字幕需配置 **B 站登录 cookie（netscape cookies.txt，宜含 `SESSDATA`）**，见 CHANGELOG 0.2.0 Fixed。
- 付费为展示占位：不接真实支付、无账户体系、无 DB。
- 抖音风控具时效性：签名/风控不定期换代，可 `DOUYIN_TIMEOUT_SECONDS` 调超时、`DOUYIN_ENABLED=false` 关停。
- 前后端同部署、同源；缩略图走本站代理端点的防盗链在 v2 强化。

## 七、端到端测试矩阵（真实公网链接）

**应成功**：YouTube(公开短视频)、Bilibili(普通)、Archive.org(最友好)、Vimeo、泛化 `.mp4` 直链。
**预期失败需降级**（友好中文而非 500）：X/Twitter、TikTok、Instagram；YouTube 需登录/限制级 → 提示需 cookies。

**自动化**：`pytest` + `httpx.AsyncClient` 覆盖各端点；`security.validate_url` 表驱动单测（mock socket：localhost/私网/回环/file/合法 https）。
**手动**：一条 curl 脚本 parse → download → 轮询 → file → subtitles → summary；浏览器手动过移动端断点。
**回归触发**：yt-dlp 升级后必跑（站点改版频繁）。`pip install -U yt-dlp`。

## 八、下一步

### M2 状态（代码已具备，待联调）
- ✅ ffmpeg 已装、`YTDLP_FFMPEG_LOCATION` 已配；✅ `OPENAI_API_KEY` 已配。
- ✅ 前端字幕/AI 摘要面板已建、`static/app.js` 已调用 `/api/subtitles`、`/api/ai/summary`。

### M2 待办（代码已就绪；均已通过真实浏览器 e2e 回归，见 CHANGELOG）
- [x] 端到端联调：字幕提取 → 翻译 → AI 摘要(v2 结构化) → SSE 问答全链路（`e2e_test.py`，真实公网链接 + Playwright/系统 Chrome）
- [x] 无字幕视频友好空态（`no_subtitles` 带引导语 + 排除弹幕/纯 xml，已回归覆盖）
- [x] 长视频 map-reduce 效果（`AI_SINGLE_SHOT_CHARS` 分档实测 `format grid rows` 正常，章节时间轴已出）
- [x] LLM 未配置/超时 → 前端可读错误映射（后端已映射 `llm`）
- [x] 字幕稿缓存（`TRANSCRIPT_CACHE_TTL_SECONDS`，按 `url→feature` 缓存，再点不重发、「重新生成」才重发）

> ⚠️ 剩余可选打磨：为「无真实字幕视频」补一次**手动**回归（非 B 站全凭 `no_subtitles` 分支）；长视频 `AI_CHAPTER_MAX_CHARS`/`AI_MAP_CONCURRENCY` 调参仍建议针对更长的片源各测一轮。

### M2.1 体验打磨（v0.2.8，已实现 + 回归）
- [x] **markdown 渲染**（摘要 + 问答）：`marked@18` 解析 + `DOMPurify@3` 消毒（LLM 输出不可信必先消毒）→ `.md` 排版，`#sumTheme/*` 收敛为 `#sumMd`；问答流式期仍 `textContent` 增量、流结束无错误才渲染。
- [x] **思维导图全屏 + 高清导出**：`#mmFullscreen` 页面内遮罩（复用同一 `#mindContainer`，拖拽/缩放/折叠全保留）+ `#mmDownload` 导出 PNG（`foreignObject`→`<text>` 规避 mermaid v11 丢字；CSP `img-src` 加 `blob:`）。
- [x] **章节细化 ~10 分钟**：`segment_chapters` 加 `goal_seconds` 判据（`AI_CHAPTER_GOAL_SECONDS` 默认 600s ≈ 10 分钟，或字符预算先到先切），全局默认。
- ✅ 验证：`e2e_test.py`（六功能 + 缓存 + 换链接清空 + 问答气泡 + 中流清空守卫）过；markdown 渲染/消毒、导图全屏交互、PNG 非空白导出均经真实浏览器（Playwright + 系统 Chrome）探针与 `e2e_download.py` 目检确认。

### M2.2 体验修复（v0.2.9，已实现 + 回归）
> 主人演示时反馈的三个 AI 体验问题。**三处均真实浏览器端到端回归。**
- [x] **思维导图不再缩成一团**（真 BUG、双倍缩放）：mermaid svg 自带 `viewBox` + `width/height=100%` 触发**浏览器自动缩放 × `fitView` 二次缩放** → 有效 ≈0.17 压团。已把 svg **归一为「1 用户单位=1 像素」**（`setupMindmapDom`：viewBox 收敛内容 bbox、宽高设像素、`#mindContainer` `overflow:hidden`），`fitView` 单次正确缩放（实测 1616px 宽树→容器 684px，scale≈0.39，渲染宽 ≈92% 填满）。
- [x] **问答 SSE 感知及时**：把「字幕抽取+上下文构建」冷路径挪进 `event_stream`，流一启即推 `preparing`→`generating`→逐 token `delta`；响应加 `Cache-Control: no-cache`/`X-Accel-Buffering: no`/`Connection: keep-alive` 防反代缓冲；前端识别 `status` 帧实时切换占位文案（首 token 前即见「流已在动」）。
- [x] **问答发送后清空输入框**（真 BUG）：`handleAsk` 校验通过后 `#askInput.value=''`。
- ✅ 验证（真实浏览器探针 + `e2e_test.py` 全绿、**无 console 报错**）：导图不再缩团（归一化后 `scale≈0.45`、树占容器 ~92%）、着色 SVG 归一化生效；问答 SSE `len` 逐 token 递增（`28→113→178→256→322→339`）、发送后 `#askInput=''`；六功能 + 缓存 + 换链接清空 + 问答气泡 + 中流清空守卫全过。踩坑：默认 `RATE_AI_PER_MIN=3` 下 e2e 连点会 429（此前被误判为外部 deepseek 限流），用 `RATE_AI_PER_MIN=999` 高限流实例复跑才确认 429 是**应用自身限流过低**所致，非本次改动回归。

### M2.3 各模块下载 / 导出 + 翻译目标语言选择（v0.3.0，已实现 + 回归）
- [x] **「提取字幕」「翻译」「章节时间轴」「问答」提供下载 / 导出**（摘要为既有 `.md`、导图既有 PNG）：字幕（`makeSubDownload`，**修复 `#subDl` 恒 `hidden` 的旧 BUG**——此前有内容也看不到下载入口）→ `字幕.<format||srt>`；翻译 → `字幕-<目标语言>.txt`；章节（`downloadChapters`）→ `章节时间轴.md`；问答（`exportChat`）→ `视频问答记录.md`。均为**纯前端 Blob**，无新后端端点。
- [x] **翻译目标语言选择**：`#subLangSel` 下拉（简体中文/繁体中文/英文/日语/朝鲜语，默认简体），`handleSubtitle` 读选中值作 `target_lang`；缓存键逐语言区分（`subTranslate:{target_lang}`），切换语言后「重新生成」即重翻。
- [x] **字幕模块合并为单钮**（主人反馈「提取字幕/翻译字幕」两钮共用一面板、用法不清楚）：六宫格变五宫格，只剩「🎬 字幕」按钮（点击恒为原字幕视图）+ 面板内「翻译为：下拉 + 翻译」控制；面板加模式标签（`原字幕`/`已翻译为：X`）与动态下载文案（`下载 SRT`/`下载 TXT（X）`）。
- [x] **AI 生成中「动图」反馈**（主人反馈：章节时间轴生成提示语啰嗦，截图显示 3 行几乎重复的占位句叠加）：全局 `#anLoading` 改为「旋转环 `.spin` + `#anLoadingText`」flex 行；四个面板占位（字幕/摘要/章节/导图）由冗长句子改为 `skelHTML(rows)` **骨架屏**（`.skel` 流光条）；新增 `startBusy(msg)`/`skelHTML(rows)`，`#anLoading` 仍以 `hidden` class 开关、`wait_busy` 等待逻辑不变。**`e2e_features.py` 复跑全绿、无控制台报错**。
- [x] 验证：**mock 后端**确定性回归 `e2e_features.py`（无需外网 YouTube/DeepSeek——本环境 YouTube 不可达）：5 语言选项默认简体、翻译成英文/日语渲染正确、字幕下载文件名含目标语言、章节生成+下载、摘要下载、问答 SSE→气泡→导出，**全程无控制台报错**；后端 `ai.translate` 直连 DeepSeek 实测 5 语言（简体→原样 no-op、繁体→繁体、英文→English、日语→日语、朝鲜语→Korean）。

### M2.4 章节「伪章节」修复（v0.3.1，已实现 + 回归）
> 主人反馈（截图）：摘要卡片「章节」出现 `[00:00 - 01:02:52] 主题标题`——`01:02:52` 是**整片时长**，章节标题=主题、摘要=总览，前后**重复**。
- [x] **根因**：`summarize` 以**字符预算** `ai_single_shot_chars` 判长短视频；**口述访谈**每分钟字数少，60+ 分钟整段也能 <30000 字 → 被误判为「短视频」走单次直出，并**合成一条覆盖整片时长的伪章节**（标题=主题、摘要=总览）。章节粒度本质由**时长**决定，字符判据是错误信号。
- [x] **修复**：新增 `_should_split(segments)`（`时长≥600s 或 字符≥30000` 任一触发即分块）；`summarize`/`mindmap` 判据改用之；短视频单次直出**不再合成伪章节**（`chapters=[]`）；`_markdown` **一律纯叙述**（主题+总览+要点+关键词），长短皆**不输出 `## 章节`/时间戳**——时间轴由独立「章节·时间轴」面板承载（主人确认：摘要模块不应有时间轴）；`derive_mindmap` 无章节时回退 `根→要点`。
- ✅ 验证：mock 62min 稀疏访谈 → 7 个 ~10 分钟真实章节（00:00–09:54 / 10:04–19:58 …），不再是单条 `[00:00-01:02:52]`；`_markdown` 长短皆纯叙述、无 `## 章节`/时间戳；`summarize` 全链路（桩 LLM）+ `e2e_features.py` mock 回归全绿、无控制台报错。

### M2.5 布局与自动摘要（v0.4.0，已实现 + 回归）
> 主人反馈（截图）：视频信息（下载）与总结上下两板块，页面纵向过长、无法一屏同览。
- [x] **左右分栏同屏**：`#resultPanel` 改 `grid lg:grid-cols-12`，左=视频信息(+`description`)+清晰度+下载，右=AI 功能 Tab（摘要默认激活）；<lg 上下堆叠。
- [x] **AI 功能 Tab 栏**：按钮宫格 → `#aiTabs` 胶囊 Tab（摘要/章节·时间轴/思维导图/字幕/问答，ID 复用，e2e 断言零破坏）。
- [x] **解析后自动摘要**：`#autoSumToggle` 开关（默认关，localStorage `vdl_auto_summary`）；开启后解析成功自动调 `/api/ai/summary`（缓存命中直接渲染）；`handleSummary` 加换链接竞态守卫。
- [x] `/api/parse` 新增 `description`（≤800 字符）；`e2e_test.py`/`e2e_features.py` 增断言与自动摘要场景，全绿、无控制台报错。

### M2.6 验收反馈修复（v0.4.1，已实现 + 回归）
> 主人验收 v0.4.0 时逐条反馈（截图标注）的体验问题，已全部修复并回归。
- [x] **顶部导航遮挡**→ `scroll-margin-top:5rem`；**右栏标题溢出**→ `block truncate` + `title` 悬停；**封面空白/竖长**→ `max-h-32` + 标题 3 行省略；**Tab 默认激活**→ 默认无选中 + `#anEmpty` 空态 + 删除说明段。
- [x] **左右等高 + 内部滚动 + 头部固定**：grid stretch 等高；右栏 `flex-col`，头部 `shrink-0`，`#anBody flex-1 min-h-0 overflow-y-auto`。
- [x] **面板下方空白 / 双滚动条**：问答/字幕/摘要/章节 撑满 + 内容区单滚动条（`margin-top:0` 防溢出）。
- [x] **首次解析居中**：`:has()` 方案，解析中 768px 居中，完成后恢复分栏。
- [x] **LLM 偶发 502**：`_chat` 重试 1 次 + `_chat_json` 格式异常重生成 1 次。
- ✅ 回归：`e2e_features.py` / `e2e_test.py` 全绿无 console 错误；探针：居中/等高/固定/单滚动条/封面/标题 全过。

### M3 待办
- [ ] 部署：反代 + HTTPS + ICP 备案
- [ ] `--workers 1` 固化到部署脚本
- [ ] 缩略图代理强化（防盗链/referrer，已有基础的 `/api/thumbnail`）
- [ ] 回归矩阵脚本化
- [ ] 可选 `APP_TOKEN` 鉴权说明

### v2 演进池（备选，按需排期）
- 整套播放列表解析（当前 `noplaylist=True`）；真实支付替换 PRO 门面；Redis 换内存 job store（多实例+持久化）；可选本地 Whisper 转写（无字幕视频）；前端上传 cookies；下载历史&收藏夹；**导图交互化（折叠已于 v0.2.6、导出高清 PNG 已于 v0.2.8 落地，剩：动画、暗色主题、子图折叠记忆）**。
- > 注：**超长字幕 map-reduce 摘要**已在 v1.1 `AI_SINGLE_SHOT_CHARS` 分档实现，故从演进池移除。

## 九、文档地图与开发约定

| 文档 | 作用 |
|---|---|
| **本 PLAN** | ★ 先读：总方案 + 当前状态 + 关键坑 + 下一步 |
| [DESIGN.md](DESIGN.md) | 架构详解：分层、Job 模型、并发、格式启发式、安全模型、选型理由 |
| [API.md](API.md) | 接口契约：端点/字段/全局错误码 |
| [SECURITY.md](SECURITY.md) | 威胁模型 + 已落实防护 + 上线检查清单 |
| [CHANGELOG.md](CHANGELOG.md) | 按里程碑记录：Added/Changed/Fixed/Verified + 决策与根因 |

> **开发约定**：每次扩展**先更新本文件的「下一步」与里程碑状态**，再回 DESIGN/API/SECURITY/CHANGELOG 同步，保持 `docs/` 与代码一致。
