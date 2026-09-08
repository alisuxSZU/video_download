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

> M2 已完成并通过端到端回归：`extract_subtitle`（翻译）、`/api/ai/summary`（v2 结构化，含章节时间轴 + 思维导图派生 + Markdown 全文）、`/api/ai/ask`（SSE 流式问答，前端气泡聊天）。`static/app.js` 的摘要面板已拆成「摘要 / 章节·时间轴 / 思维导图 / 问答」四个 tab，支持复制/下载 `.md`，并按 `url→feature` 缓存。**端到端验证**由仓库根 `e2e_test.py`（Playwright + 系统 Chrome，真实公网链接）回归，需 `RATE_AI_PER_MIN=100` 启动避免连点 429。

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
| AI 限流（提醒） | 六个功能的 5 个 AI 端点共用限流组 `ai`，默认 `RATE_AI_PER_MIN=3`（每 IP 每分钟）；60 秒内连点 ≥4 个功能会 `429`，**.env 调高 `RATE_AI_PER_MIN` 可改善多点连用体验** |
| 模块合并 + 单输入框（v1.3，已实现） | 六个功能并入「解析结果」同一卡片（`#analyze` 移入 `#resultPanel`），**只保留顶部一个 `#urlInput`**；删除 `#analyzeUrl`/`#useCurrent` |
| 思维导图可交互（v1.3，已实现） | mermaid 脑图支持**拖拽平移 / 滚轮缩放（绕光标）/ 点击折叠展开 / 全部展开/折叠 / 重置视图**；因 CSP 只放行 jsdelivr，markmap 无法加载，故在 mermaid SVG 上**自实现** pan/zoom/fold（`#mmViewport` 组 + `window.__mm`） |
| 结果按链接缓存（v1.3，已实现） | 每个功能结果按 `url → feature` 缓存；未换链接/未刷新前**保持不变**；点各面板「⟳ 重新生成」才强制重算。⚠️ 绑定不能用 `onclick = fn` 直接传 `force`，否则事件对象被当作参数使 `!force` 恒假 → 永远重算（已改为 `() => handleSummary()`） |
| 问答分钟级定位（v1.3，已实现） | 旧逻辑按**章节整块**检索且章节内合并文本丢失逐条时间戳，模型只能凭章首尾估时间；改为**逐字幕条**打分、取时间连续、逐行带 `[MM:SS - MM:SS]` 的窗口 + 提示词要求返回具体时间段（可合成区间、未覆盖如实说明、不编造） |
| 清晰度/格式响应式网格（v1.3，已实现） | `#formatList` 由 `space-y-2`（每项占一整行）改为 `grid gap-2 repeat(auto-fill,minmax(220px,1fr))`：宽屏自动多列（实测 1440px 下 3 列）、手机回落单列；调用 `resetAnalyze()` 前 `activeUrl()!==url` 判定链接是否变化 |
| 换链接清空 AI 内容（v1.3，已实现） | `parseSingle` 检测到**换新链接**（或解析失败）时调用 `resetAnalyze()`：清空上一视频的字幕/摘要/章节/导图/问答可见内容与跨链接临时状态（`lastSummary`/`askHistory`/`subModeTranslate`/`mmCollapsed`/`mmTree`/`mmFitted`）。⚠️ 不删 `featureCache`（按 url 分键），切回旧链接仍可命中缓存恢复 |
| 下载后自动保存到本地（v1.3，已实现） | ① 彻底删除 `#progDownload` 与批量行的 `.dl-link`「下载」链接，不再有手动兜底；② 单条任务 `done` 后前端**自动点击** `/api/jobs/{id}/file`（`link.click()`，Content-Disposition 流式写盘，零内存）保存到用户本地下载目录；③ 批量队列每行 `done` 后经 **`fetch→Blob→anchor`** 逐条、串行自动保存（`enqueueBatchDownload` + `drainBatchQueue`：取完一条（fetch 完成）再 sleep 0.7s 取下一条），序列化避免并发触发下载引擎。单条用直连链接（内存友好），批量用 Blob（每次仅一份在内存）。⚠️ **浏览器「多文件自动下载」策略**：Chromium 限制无用户手势的自动下载，批量**第 2 条起**真实浏览器会弹**一次性**「此网站尝试下载多个文件→允许？」（每站点一次，允许后整批自动）；无头自动化需 `--enable-automatic-downloads` 才能放行以便测试 |
| 问答 = 气泡聊天（v0.2.6，已实现） | `#askOutput` 改为 `flex` 气泡容器；`handleAsk` 用 `askBubble(role,text)` 生成**用户问题气泡（右、`bg-brand` 白字）+ AI 回答气泡（左、灰底）**，SSE 增量写进单个 AI 气泡并自动滚动。**统一 `textContent`（不用 innerHTML）防 XSS**；`if (btn.disabled) return` 防回答中重复提交；`askClearChat()` 清空 + 重置 `askHistory`，`resetAnalyze`/`#askClear` 复用。**流式竞态加固**：模块级 `askGen`(令牌) + `askAbort`(AbortController) —— 回答进行中清空/换链接即 `askGen++` 且 `askAbort.abort()`，陈旧流（`myGen<askGen`）既不写 DOM、不 `askHistory.push`（防旧问答复活污染下一问上下文）、不复位按钮；Abort 静默不弹错；空回复把「…」占位改为「(无回答)」 |
| 格式角标去噪 + 剔 MHTML 故事板（v0.2.7，已实现） | ① `formatRow` 不再渲染右下角 `mergeChip`（「需合并音视频」/「单文件」），卡片只留**清晰度 + 格式 · 大小**，所有选项一致（合并是后台工作，对用户无意义）；② `app/downloader.py` 的 `_clean_formats` 加 `_is_storyboard(f)`（`ext/protocol=='mhtml'` → `continue`），剔除 yt-dlp 的 **`sb0`-`sb3` 故事板伪格式**（`ext='mhtml'`、`filesize=0`、无码流）——否则会以「180p/90p/45p/27p MHTML」混进列表，用户点下去下载不到真视频。只剔 mhtml 不动其余，避免误伤 Archive.org 等无 codec 信息的平台 |

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

### M3 待办
- [ ] 部署：反代 + HTTPS + ICP 备案
- [ ] `--workers 1` 固化到部署脚本
- [ ] 缩略图代理强化（防盗链/referrer，已有基础的 `/api/thumbnail`）
- [ ] 回归矩阵脚本化
- [ ] 可选 `APP_TOKEN` 鉴权说明

### v2 演进池（备选，按需排期）
- 整套播放列表解析（当前 `noplaylist=True`）；真实支付替换 PRO 门面；Redis 换内存 job store（多实例+持久化）；可选本地 Whisper 转写（无字幕视频）；前端上传 cookies；下载历史&收藏夹；导图交互化（折叠/导出图片）。
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
