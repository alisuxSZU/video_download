# CHANGELOG — 按里程碑记录的实现进度与决策变更

> 本文件记录「做了什么、改了什么、为什么」，供回溯。格式遵循 keep-a-changelog 精神：`[Added] / [Changed] / [Fixed] / [Removed]`，并附实现细节与决策。

## [0.1.2] — 抖音视频下载（服务端无头浏览器）（2026-09-07）

> 目标：在不要求用户提供 Cookie、不使用浏览器插件/油猴、不自造轮子的前提下，让抖音视频可解析并下载。端到端自测验证：解析→下载→成品 MP4。

### 背景与调研结论（「为什么这么做」）

抖音 Web 业务 API（详情/列表/评论）被 `a_bogus` + `msToken` 签名墙覆盖，且 `a_bogus` 已**绑定浏览器环境指纹**（UA/版本/设备参数须在签名时一致）。据此排除掉三条「免 Cookie 直连」路线：

1. **yt-dlp 直跑**：最新版 `DouyinIE`（`yt_dlp/extractor/tiktok.py`）里只有 `# TODO: Run verification challenge code to generate signature cookies`，**没有 `a_bogus` 签名器**，`_real_extract` 直接裸请求 → 无签名被拒。
2. **纯 Python 签名器（如 f2）**：签名需实时复现浏览器指纹，纯 Python 无法与真实浏览器一致 → 直连一律 403。
3. **免 Cookie 的 SSR 直连**（share 页 → `uri` → `aweme.snssdk.com/v1/play`）：该通道已于 **2026-08-30** 关闭。share 页不再渲染 `play_addr.uri`，返回空壳页（`video_layout:null` + captcha/slardar 标记）。采样对标开源项目 `rathodpratham-dev/douyin_video_downloader`（同为 requests-only SSR 方案），实测 iteminfo 为空、share 页无 videoInfoRes —— 已死。

**可行路线只有一条**：服务器内置无头浏览器，用**浏览器自己的匿名游客会话**加载抖音页 —— 页内真实 JS 现场计算 `a_bogus`，我们只拦截它对 `/aweme/v1/web/aweme/detail` 的**真实网络请求响应**，从中拿播放地址。这满足全部硬约束：不需用户 Cookie、不需要用户端浏览器插件、不在自造逆向轮子。

### Added

- **新增模块 `app/douyin.py`（服务端无头浏览器解析）**：
  - `is_douyin_url(url)` → 识别 `douyin.com` / `iesdouyin.com` / `v.douyin.com` 短链。
  - `_get_context()`：惰性单例（`threading.Lock` 保护），用 **Playwright 复用已装好的系统 Chrome**（`channel="chrome"`，`headless` 读取 `settings.douyin_headless`），首次访问 `douyin.com` 埋下游客 cookie（升温），UA/locale/viewport 拟真。
  - `resolve(url) -> dict`：返回 **yt-dlp 形状的 `info`**（含 `id/title/thumbnail/duration/formats/subtitles`），让既有 `probe`/`_build_payload`/`_apply_format` 管线**零改动复用**；另附私有 `_play_url` + `_play_headers` 供 `run_download` 走直连。
  - `_fetch_aweme_detail(page, url)`：**关键** —— 不自己发 `fetch`（会因缺 `a_bogus` 拿空 body），而是 `page.expect_response(lambda r: "aweme/v1/web/aweme/detail" in r.url)` 拦截页面对该 API 的**真实响应**（页内 JS 现场签名），`wait_until="domcontentloaded"`（抖音长连 `networkidle` 永不 settle）。
  - `download(job, info, job_dir)`：httpx 流式拉 `play_addr.url_list[0]`（`playwm→play` 去水印），更新进度/速度（每 ~0.5s 采样），支持取消，收尾写回精确字节数。
- **`app/config.py`** 新增抖音开关：`DOUYIN_ENABLED` / `DOUYIN_HEADLESS` / `DOUYIN_TIMEOUT_SECONDS` / `DOUYIN_AUTO_REFRESH`。服务器需装 Chrome/Edge；关闭后走友好降级，不影响其他平台。
- **`app/security.py`** 新增错误映射：`DouyinUnsupportedError`→`unsupported`、`DouyinBlockedError`→`forbidden`。

### Changed

- **`app/downloader.py`** 接线抖音分支：
  - `_extract_info`：`_is_douyin(url)` → 先走 `douyin.resolve(url)`，再落 yt-dlp。
  - `run_download`：`info.get("_play_url")` → 走 `douyin.download` 直连（而非再走 yt-dlp），`_resolve_output` 收尾。
  - `_build_base_params`：加 `_bytedance` 标记，**cookiefile/proxy 不对字节系转发**（用户 Cookie/运营者代理绝不泄漏给抖音）。
  - `extract_subtitle`：抖音 URL 直接 `_SubtitleError`（无字幕提取/摘要），避免误报 500。

### Verified（HTTP 端到端实测）

- ✅ `GET /api/parse`（`www.douyin.com/video/6961737553342991651`）→ 标题 `#杨超越 小小水手带你去远航❤️`、时长 19.78s、缩略图、格式列表。
- ✅ `POST /api/download` → job_id → 轮询 `probing→done`（进度 100）→ `GET /api/jobs/{id}/file` 返回真 MP4（magic `ftypisom`，3.67MB，`video/mp4`，Content-Disposition 正确）。
- ✅ 字节精确：收尾后 `downloaded_bytes == filesize`。
- ✅ 回归：同一管线上的普通 URL（`.../mov_bbb.mp4`）解析/下载/成品仍为 200，`extractor: Generic`，未被抖音分支破坏。

### 关键修复

- **Playwright 超时单位**：`expect_response`/`goto` 的 `timeout` 是**毫秒**，传入秒会导致「45ms 超时」秒败。统一 `timeout_ms = int(douyin_timeout_seconds * 1000)`。
- **`networkidle` 永不 settle**：抖音对页面保持长连接，`wait_until="networkidle"` 用不触发 → 改 `domcontentloaded`。
- **合成 `fetch` 为空**：页内 JS 发起的 `fetch` 也需 `a_bogus`，无签名拿空 body → 必须拦截页面**自身**对 detail 的请求。
- **Playwright sync API 跨线程崩溃（`greenlet.error: Cannot switch to a different thread`）**：Playwright 的 greenlet 绑定到「创建它的线程」，而 `/api/parse` 走 FastAPI 默认执行器线程、下载 job 走独立的 `vdl` 线程池 —— 同一浏览器 context 被两个线程复用时报 `greenlet.error`。
  - 根因：`resolve()` 会被两类线程调用；原实现在首个调用线程里惰性启动浏览器并常驻，后续其它线程复用即崩。此前端点单独测试通过、连通/并发测试才复现。
  - 修复：改为**单浏览器专属线程**独占 Playwright 实例（`_ensure_worker` 惰性拉起 + `queue.Queue` + `Future` 请求-响应），`resolve()` 只向该线程提交请求并限时等待结果；所有 Playwright 调用串行落在同一线程。启动/预热失败记录 `start_error`，后续请求即时转友好 `DouyinError`，不用半损坏实例。
  - `download()` 走纯 httpx（无 Playwright），不受影响，依旧可在任意线程执行。
- **下载进度收尾字节数近似**：轮询中 `downloaded_bytes` 每 ~0.5s 采样是近似值，下完结时写回精确 `downloaded_bytes == total_bytes` 并标 100。

### 备注

- 抖音风控具时效性（站点/签名算法会不定期更新）；`_fetch_aweme_detail` 超时/异常统一映射为友好中文，绝不 500/泄漏。
- `DOUYIN_AUTO_REFRESH` 预留为后续「风控触发时自动刷新游客会话」的开关，当前未启用硬刷新。

---

## [0.1.1] — 核心业务 Bug 修复（2026-09-07）

> 用户反馈 5 个核心业务问题，逐一修复并端到端自测验证。目标：B站/YouTube「解析→下载→字幕」全链路可用。

### Fixed
- **B站视频无法下载（HTTP 412）**：B站风险控制/限速返回间歇性 `HTTP 412 / 429 / 403`（~33% 抖动），初次失败不代表失败。
  - 新增 `_retry_antibot(fn, max_attempts=4, base_sleep=2.5)`：命中风控标记时带退避重试（`base_sleep * (attempt+1)`），确定性错误（404/不支持链接）直接抛出。
  - 关键：`tasks._probe` 原先绕过重试直接用 `YoutubeDL(...).extract_info` 裸提取，导致下载前解析就断在 412。改为走 `_retry_antibot(lambda: _extract_info(url, download=False))`。
  - **额外**把 `_retry_antibot` 的风控标记扩展至连接级瞬时抖动（`unexpected_eof_while_reading` / `EOF occurred in violation of protocol` / `connection reset` / `read operation timed out` 等）——B站、部分 CDN 传输中会 SSL 重置，这类错误重试即自愈。
- **B站封面不显示**：两处根因——
  1. CSP `img-src` 仅允许 `'self' data: https:`，而 B站封面是 `http://i1.hdslb.com/...`，被浏览器按混合内容拦截 → CSP 加 `http:`。
  2. B站 CDN 防盗链且 `http://`。新增 `GET /api/thumbnail?url=...` 服务端代理（真实 UA + 对应平台 `Referer`，走本站 https），前端 `<img src="/api/thumbnail?url=...">`。后端对缩略图 URL 复用 `validate_url` 防 SSRF。
- **YouTube 只下载 `.mhtml`（非可播放视频）**：根因是服务端无 ffmpeg，多档为 adaptive 分离音视频流，无法合并 → 选择到坏档。配置 `.env` 的 `YTDLP_FFMPEG_LOCATION` 指向 winget 安装的 ffmpeg 9.0.1 后，`format="bestvideo+bestaudio"` 正常合并为真 MP4（h264+opus/aac）。解析 payload 增加 `ffmpeg` 布尔供前端判断。
- **解析结果面板与链接输入距离过远**：`#resultPanel`/`#batchPanel` 原位于页面下方，移到 Hero `</section>` 之后、信任条之前，紧贴输入框；`showResult` 时 `scrollIntoView({behavior:'smooth', block:'start'})`。
- **字幕无法下载**：
  - 前端硬编码 `lang='zh', is_auto=false`，很多视频只有自动字幕 / 只有英文 → 后端 `extract_subtitle` 增加自动回退：先按用户指定取，未产出则从可用字幕清单 `_ordered_sub_candidates` 逐条尝试（精确→语言族→中/英手动→任意手动→任意），失败继续尝试下一条，直到成功。
  - 字幕下载改用 Blob 而非 `data:` URI，避免浏览器拦截。
  - **顺带修复**：`_collect_transcript_sync`（AI 摘要取稿）原先把 `probe` 返回的 payload 再次套 `_subtitles_list`，因 payload 的 `subtitles` 已是标准化 `[dict]` 列表，二次标准化触发 `AttributeError: 'list' object has no attribute 'items'` → 500。改为直接取 `payload['subtitles']`。

### Verified（ffprobe / 浏览器实测）
- ✅ B站 `BV1GJ411x7h7`：解析 4 档 → 下载 → **h264 + aac 真 MP4**（1080p 75MB / 360p 亦可），`ftypisom` 头。
- ✅ YouTube `dQw4w9WgXcQ`：下载 → **h264 + opus 真 MP4**（135 档 17.5MB、401 档 243MB），不再 `.mhtml`。
- ✅ B站封面：`/api/thumbnail` 返回 `image/jpeg`（magic `ffd8ffe0`），浏览器中 `naturalWidth=1920` 正常显示。
- ✅ 结果面板：紧贴输入框下方（间距 ~146px），解析后可见封面 + 多格式列表。
- ✅ 字幕：前端默认请求 `lang=zh` 时该视频回退到 `zh-Hans auto`（200，2305 字符）；`lang=en auto` 直接 200。AI 摘要不再报 500（无 LLM Key 时友好 502）。

### 备注
- B站 412/SSL 抖动具偶发性，重试可自愈；单用户正常负载下稳定。
- AI 摘要需配置 `OPENAI_API_KEY`（未配则 502，属预期，非本批 bug）。

---

## [0.1.0] — M1 核心业务 + 前端精美（2026-09-07）

### Added
- **脚手架**：`app/config.py`（.env 集中配置）、`app/main.py`（FastAPI 入口 + lifespan + 安全中间件 + 挂载 `/static`）、`GET /api/health`。
- **安全模块** `app/security.py`：
  - `validate_url`：仅 `http/https`；getaddrinfo 解析真实 IP → ipaddress 判私网/回环/保留段（含 `169.254.169.254` 元数据拦截），命中 422。
  - `RateLimiter`：内存滑窗按 `IP + 端点` 计数，超限 429 + Retry-After。
  - `SecurityHeadersMiddleware`：CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy / Permissions-Policy。
  - `sanitize_filename`（防路径穿越/Windows 非法字符）、`friendly_error`（错误脱敏）、`error_status`、全局异常处理器。
- **downloader.py**：`probe`（不下载解析）→ `_build_payload`（标题/缩略图/时长/extractor/全部格式/字幕列表）；`_clean_formats`（按高度去重、标注 `needs_merge`、progressive 优先）；`build_format_string`（格式选择表达式，含无 ffmpeg 降级）；`run_download` + `progress_hook`（进度/速度/取消）；`extract_subtitle` + `subtitle_to_text`（字幕提取与转纯文本）。
- **tasks.py**：内存 job store（dict + uuid4 + `threading.Lock`）+ `ThreadPoolExecutor` + `asyncio.Semaphore` 并发上限 + `Queue` 调度 + 后台 TTL 清理循环。
- **ai.py**：OpenAI 兼容 LLM 客户端 `summarize` / `translate`（httpx POST `/chat/completions`），错误映射。
- **routes.py**：`/api/parse`、`/api/download`、`/api/download/batch`、`/api/jobs/{id}`、`/api/jobs/{id}/file`（流式）、`DELETE /api/jobs/{id}`、`/api/subtitles`、`/api/ai/summary`，全部走限流 + 错误映射。
- **前端单页** `static/`：`index.html`（Hero「一条链接，下遍全网」/ 信任背书 / 功能卡网格 / 结果面板(多格式多选) / 批量队列面板 / 字幕面板 / AI 摘要面板 / 定价+PRO 对比卡 / 升级 CTA 横幅 / FAQ / Footer 免责 + PRO 弹窗）+ `app.js`（解析/渲染/批量轮询/进度条/字幕/摘要/动效/toast）+ `styles.css`（马卡龙芯片、hover 上浮、骨架屏、动效）。
- **文档沉淀** `docs/`：`PLAN.md`（总方案）、`DESIGN.md`（架构详解）、`API.md`（接口契约）、`SECURITY.md`（安全清单）、`ROADMAP.md`（演进）、`CHANGELOG.md`（本文件）。
- **工程文件**：`.env.example`、`requirements.txt`、`README.md`（含 ffmpeg 安装、单 worker 警告、yt-dlp 升级说明）。

### Key decisions
- **站在巨人肩膀上**：封装 yt-dlp 库（非 CLI），纯前端静态页 + FastAPI 轻后端，零新增依赖。
- **无 ffmpeg 降级**：`_is_progressive` 对平台不返回 codec 信息的情况（如 Archive.org）_视为单文件_，避免错误拼接 `+bestaudio`。
- **单文件回退**：`build_format_string` 在无 ffmpeg 且选中 video-only 档时，降级使用同高度/任意 progressive 档。
- **单进程**：任务与限流存内存，must `--workers 1`；横向扩需 Redis（v2）。
- **轮询而非 WebSocket**：前端 1.1s 轮询 job，最简稳健。

### Fixed
- **Pydantic 顺序**：`models.py` 在 import 前使用 BaseModel → 移到顶部。
- **IPv6 网段 host bits**：`::ffff:127.0.0.1/104` 改为 `::ffff:0:0/96`（覆盖所有映射地址）。
- **错误码映射顺序**：`InvalidURL`（SSRFError 子类）需先判断，否则 `file://` 被误判为 422 而非 400。
- **progressive 误判**：`_is_progressive` 对 vcodec/acodec 为 None 的档返回 `False`，导致误加 `+bestaudio` → 改为 `not video_only and not audio_only`。
- **parse 未映射错误**：已知 yt-dlp 错误返回 500 → 增加 `error_status` 映射。
- **前端 job 时序**：`currentJobId` 在 `onDone` 前未赋值 → 移到 `handleDownload` 前并在创建后赋值。

### Verified (M1 端到端)
- 解析：Archive.org `BigBuckBunny_124` → 3 档（720p AVI 317MB / 360p MP4 59MB / 300p OGV 44.8MB）带 单文件 芯片。
- 下载：probe → downloading(进度/速度) → done，`/file` 返回合法 mp4（ftyp 头）+ Content-Disposition attachment。
- 批量：多行渲染逐条进度，一条完成。
- 安全：`localhost`/`192.168.1.1`/`file://` 分别拦截且不触发下游；连续请求第 4 次起 429；CSP/X-Frame-* 响应头齐全。
- 前端：多格式多选在浏览器正常渲染，toast 正常。

---

## 待办（下一步）
- [ ] **M2**：字幕面板 / AI 摘要面板接通（需先装 ffmpeg + 配 `OPENAI_API_KEY`）。
- [ ] **M3**：部署加固（反代/HTTPS/备案/回归脚本）。
- [ ] 重启 uvicorn 使 `security.py` 的 `InvalidURL` 优先判断生效（逻辑已改，旧进程未加载）。
