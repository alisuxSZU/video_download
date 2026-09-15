# API — 接口契约

> 扩展新端点前先读这里。请求/响应皆为 JSON（除 `/file`、`/thumbnail` 为流式文件/图片下载）。
> 所有错误统一返回 `{"ok":false,"error":"<友好中文>","code":"<机器码>"}`（限流与 500 有时带 `detail` 包裹，见「错误格式」）。

Base URL：`http://<host>:<port>`（默认 `8000`）。

## 全局错误码

| code | HTTP | 含义 |
|---|---|---|
| `invalid_url` | 400 | 链接为空/非 http/https/非法主机等 |
| `ssrf` | 422 | 命中内网/私网/回环/保留地址（重点 `169.254.169.254`） |
| `unsupported` | 400 | 平台或链接格式不支持 |
| `not_found` | 404 | 视频不存在或已失效 |
| `not_ready` | 409 | 成品文件尚未就绪 |
| `forbidden` | 502 | 平台拒绝访问（需登录/地区/版权限制） |
| `login_required` | 403 | 需要登录后才能访问 |
| `extract_failed` | 502 | 无法提取（反爬/链接失效） |
| `no_ffmpeg` | 500 | 服务器缺 ffmpeg，高清合并不可用 |
| `no_subtitles` | 502 | 视频无可用字幕。注意：**B 站部分视频的字幕需登录态（`need_login_subtitle=True`）**，未配 `COOKIES_FILE`（B 站登录 cookie）时取不到真实字稿，会返回本码（`error` 字段带登录引导语，如实说明「确有字幕但需登录态，请配置 COOKIES_FILE」）；项目不会拿弹幕 XML 伪装成字幕 |
| `llm` | 502 | LLM 未配置或调用失败 |
| `rate_limited` | 429 | 超过本服务限流 |
| `rate_limited_by_platform` | 429 | 被目标平台限速 |
| `timeout` | 504 | 请求超时 |
| `thumbnail` | 502 | 封面图加载失败 |
| `invalid_email` | 400 | 邮箱格式不正确 |
| `weak_password` | 400 | 密码强度不足（8~128 位） |
| `email_taken` | 409 | 邮箱已注册 |
| `invalid_credentials` | 401 | 邮箱或密码错误（不区分账号是否存在，防枚举） |
| `login_required` (auth) | 401 | 账户端点需要登录（令牌缺失/失效） |
| `pro_required` | 403 | 该能力为 PRO 专属：1080p+ 下载 / 字幕翻译 / AI 超出每日免费额度（响应带 `ai_used_today`/`ai_daily_limit`） |
| `invalid_plan` | 400 | 套餐键非法（仅 `month`/`year`） |
| `billing_disabled` | 503 | Stripe 未配置完整（密钥/价格缺一即关闭），前端展示「即将上线」 |
| `checkout_failed` | 502 | 创建 Stripe Checkout 会话失败（网络/密钥问题） |
| `unknown` | 502 | 兜底未知错误 |
| `internal` | 500 | 服务器内部错误 |

## 1. GET `/api/health`

存活探针。`→ 200 {"ok": true, "version": "0.3.0", "ffmpeg": true}`（`ffmpeg` 为服务端 ffmpeg 是否可用；`version` 随 `config.py#Settings.version` 前进）

## 2. POST `/api/parse`

解析链接元数据与全部可用格式。**不下载。**

请求：`{"url": "https://..."}`

响应 200：
```json
{
  "ok": true,
  "title": "Big Buck Bunny",
  "description": "这是一段视频简介……",
  "thumbnail": "https://...",
  "duration": 596.0,
  "extractor": "ArchiveOrg",
  "webpage_url": "https://archive.org/details/...",
  "ffmpeg": false,
  "formats": [
    {
      "format_id": "22",
      "ext": "mp4",
      "resolution": "720p",
      "height": 720,
      "fps": 30.0,
      "vcodec": "avc1",
      "acodec": "mp4a",
      "filesize": 332243668,
      "needs_merge": false,   // true=需合并音视频流
      "progressive": true,
      "note": "高清"
    }
  ],
  "subtitles": [{"lang": "zh", "is_auto": false, "source": "manual"}]
}
```

> `formats` 已由后端 `_clean_formats` 过滤/去重：纯音频流不展示（`vcodec` 不会为 `null`），且已剔除 yt-dlp 的 **`sb*` 故事板伪格式**（`ext/protocol='mhtml'`、`filesize=0`、无真实码流，如「180p MHTML」）——只留真实可下的视频，故 `ext` 不出现 `mhtml`。

> `description`（v0.4.0 新增）：视频简介，已压缩空白并截断至 **800 字符**；无简介时为空字符串 `""`，不报错。

错误：400 / 403 / 404 / 422 / 429 / 500 / 502 / 504（经 `friendly_error` + `error_status` 映射，非固定子集）。

> **抖音特例**：抖音 URL 走服务端无头浏览器（`app/douyin.py`）。返回的 `formats` 恒为**单个 progressive mp4 档**（`needs_merge=false`，`ext` 恒 `mp4`），`subtitles` 为空数组，`extractor` 为 `Douyin`。

## 3. GET `/api/thumbnail?url=...`

**服务端代理封面图**（真实 UA + 对应平台 Referer，走本站 https），规避 B站等 CDN 防盗链 + 混合内容拦截。前端 `<img src="/api/thumbnail?url=...">`。对 `url` 复用 `validate_url` 防 SSRF。

- 错误：400（`invalid_url`，url 非法）、422（`ssrf`）、502（`thumbnail`，封面加载失败）。

## 4. POST `/api/download`

创建单个下载任务，返回可轮询的 `job_id`。

请求：`{"url": "...", "format_id": "22"}`（`format_id` 可省，默认最佳）

响应 202：`{"ok": true, "job_id": "6f2c...", "status": "queued"}`

错误：400 / 422 / 429 / 503（`active_or_queued_count() >= MAX_ACTIVE_JOBS` 时拒绝）。

## 5. POST `/api/download/batch`

批量创建任务（≤ `MAX_BATCH`）。

请求：
```json
{"items": [{"url": "..."}, {"url": "...", "format_id": "18"}]}
```

响应 202：
```json
{"ok": true, "jobs": [{"job_id": "a1f0", "status": "queued", "url": "..."}]}
```

某项校验失败时该项返回 `{"url":"...", "job_id": null, "status": "invalid", "error": "..."}`（非标准 `status` 值）。错误：400（超 MAX_BATCH/空列表）/429/503。

## 6. GET `/api/jobs/{job_id}`

轮询任务状态与进度。**不含服务端绝对路径。**

响应 200（示例 `downloading`）：
```json
{
  "ok": true,
  "job": {
    "id": "6f2c...",
    "url": "https://...",
    "status": "downloading",
    "title": "Big Buck Bunny",
    "thumbnail": "...",
    "duration": 596.0,
    "extractor": "ArchiveOrg",
    "format_id": "22",
    "ext": "mp4",
    "needs_merge": false,
    "progress": 42.5,      // 0-100
    "downloaded_bytes": 141076288,
    "total_bytes": 332243668,
    "speed": "23.4MB/s",
    "filename": "Big_Buck_Bunny.mp4",
    "filesize": 0,          // done 后为实际大小
    "error": null,
    "subtitles": []
  }
}
```

> 即 `Job.snapshot()`，字段以它为准（不含 `created_at`/`filepath`）。

`status` 取值：`queued | probing | downloading | done | error | closed`。

错误：404（不存在）、410（过期已清理）。

## 7. GET `/api/jobs/{job_id}/file`

流式下载成品文件。带 `Content-Disposition: attachment; filename=...`、`Content-Type` 按 ext。

- **前端行为**：单条下载任务 `done` 后，前端会**自动**点击该链接把文件保存到用户本地下载目录（不再要求用户再点一次，`#progDownload` 兜底链接已彻底删除）。批量队列每行 `done` 后自动保存：经 `fetch→Blob→anchor.download` **逐条、串行**触发浏览器下载（每次一份在内存，取完一条再取下一条），序列化避免并发触发下载引擎。⚠️ Chromium 限制无用户手势的自动下载——批量**第 2 条起**真实浏览器会弹**一次性**「此网站尝试下载多个文件→允许？」授权框（每站点一次）；无头自动化需 `--enable-automatic-downloads` 放行才能测满。
- 错误：404（不存在/未完成）、409（未为 done，`code=not_ready`）、410（过期）。

## 8. DELETE `/api/jobs/{job_id}`

尽力取消并清理。成功 `{ok:true}`；不存在 `404`。

## 9. POST `/api/subtitles`

提取或翻译字幕。

请求：`{"url":"...","lang":"zh","is_auto":false,"target_lang":"简体中文"}`（`target_lang` 有则翻译）

- `target_lang` 为**自由语言名**（`ai.translate` 直接拼进 LLM 系统提示的「翻译成{target_lang}」）。前端「字幕」模块（**单个「🎬 字幕」按钮**，面板内「翻译为：下拉 + 翻译」控制）经 `#subLangSel` 提供 5 个选项：`简体中文 / 繁体中文 / 英文 / 日语 / 朝鲜语`（默认简体中文）；**切换语言后点「翻译」或「重新生成」即按新语言重翻**，缓存键为 `url→subTranslate:{target_lang}`（逐语言独立缓存）。省略或空字符串则只提取不翻译。

响应 200：
```json
{
  "ok": true,
  "lang": "zh",
  "source": "manual",      // manual | auto
  "format": "srt",
  "content": "1\n00:00:00,000 --> ...\n",
  "translated": "这是翻译后的字幕..."   // 仅当 target_lang 存在
}
```

错误：400 / 429 / 502（`no_subtitles` 无字幕、`llm` 翻译失败）。

> **前端下载**：提取成功即把 `content`（手动字幕为 `format` 后缀，如 `.srt`）/`translated`（译文为 `.txt`）生成 Blob 放进 `#subDl` 的 `<a download>`；翻译文件名形如 `字幕-英文.txt`。此为**纯前端 Blob 下载**，无对应后端端点。

## 10. POST `/api/ai/summary`

由字幕生成**学习型结构化摘要**（v2：主题 + 章节·时间轴 + 思维导图 + 关键词 + 全文 Markdown）。

请求：`{"url":"..."}`

响应 200：
```json
{
  "ok": true,
  "theme": "一句话概括视频主题",
  "overview": "整段视频的总体概述",
  "chapters": [
    {
      "start": 0.0,          // 秒（含时间轴）
      "end": 213.75,         // 秒
      "title": "本章标题",
      "summary": "本章摘要",
      "key_points": ["要点1", "要点2"],
      "keywords": ["关键词"]
    }
  ],
  "keywords": ["总关键词1", "总关键词2"],
  "mindmap": {
    "title": "<=theme>",
    "children": [{"title": "<章节title>", "children": [{"title": "<要点>"}]}]
  },
  "summary": "# <theme>\n\n<overview>\n\n## 要点\n- <要点1>\n- <要点2>\n\n## 关键词\nkw1 kw2",
  "lang": "zh",
  "model": "deepseek-chat",
  "is_auto": false,          // 取的是自动字幕还是手动字幕
  "used_source": "manual"    // manual | auto
}
```

> **行为分档（v0.3.1 起）**：`时长 ≥ AI_CHAPTER_GOAL_SECONDS`（默认 600s≈10 分钟）**或** 字幕总字符 ≥ `AI_SINGLE_SHOT_CHARS`（30000）任一满足 → **分块**成最多 `AI_MAX_CHAPTERS` 个章节做 map-reduce，产生真实时间轴；否则 → 单次直出 `{theme, overview, key_points[], keywords[]}`，**`chapters` 为 `[]`**（不再合成一条覆盖整片时长的伪章节）。⚠️ 此前仅按字符分档，口述访谈等**每分钟字数少**的长视频会被误判为「短片」并合成 `[00:00 - 整片时长] 标题=主题、摘要=总览` 的重复伪章节——已修复。`mindmap` 由结构**确定性派生**（无额外 LLM 调用）。
> `summary` 为 **纯叙述 Markdown**（主题+总览+`## 要点`+`## 关键词`，**不含 `## 章节`/时间戳**——时间轴由独立「章节·时间轴」面板承载），供前端「复制 / 下载 .md」。⚠️ 无论长短视频，`summary` 均无 `## 章节` 块（v0.3.1 起）；短视频 `chapters` 为 `[]`、`mindmap.children` 回退为顶层 `key_points`。
> `chapters` 字段本身（含 `start/end/title/summary/key_points`）仍在响应中，供「章节·时间轴」面板/导出及 `derive_mindmap` 消费；只是不内嵌进摘要 Markdown。
> `used_source` 反映取的是手动（`manual`）还是自动（`auto`）字幕，不再恒为 `transcript`。

错误：400 / 429 / 502（`no_subtitles` 无字幕、`llm` LLM 未配置或失败）。

## 11. POST `/api/ai/ask`

对视频内容**追问**，返回 **SSE 流式**答案（逐 token）。用于「问答」面板。

请求：`{"url":"...","question":"核心观点是什么？","history":[{"role":"user","content":"..."}]}`

- `history`：可选，`[{role, content}, ...]`，前端维护（最近 6 条拼进上下文）；无则独占问答。
- ⚠️ **PRO 专属**：非 PRO（游客 / 免费账户）请求直接返回 **403 `{"ok":false,"code":"pro_required","error":"AI 问答为 PRO 会员专属功能…"}`**（不消耗每日免费 AI 配额）；前端 `askAllowed()` 已前置引导升级。

响应：`Content-Type: text/event-stream`。每帧 `data: <JSON>`，事件序列形式如下：

```text
data: {"status": "preparing"}   # 流一经建立立即推出：正在检索字幕与上下文（冷路径可能 2s+）
data: {"status": "generating"}  # 字幕就绪、进入 token 生成阶段
data: {"delta": "回答的字词片段"}   # 逐 token 增量
data: {"delta": "..."}
...
data: {"error": "友好中文"}   # 出错即终止流，不再补发 done（见下）
data: {"done": true}          # 正常结束
```

- **status 帧提升“及时性”**：把「字幕抽取 + 上下文构建」这段冷路径也挪进流内，SSE 连接一建立就推 `preparing`，待字幕就绪再推 `generating`，随后逐 token 推 `delta` —— 前端在首个 token 之前就有状态反馈，避免“请求后长时间空白”。
- **网络层不缓冲**：响应带 `Cache-Control: no-cache` + `X-Accel-Buffering: no`，防止 nginx 等反向代理把流缓冲到收尾才一次性吐出。
- **出错帧即终止**：若中途出错，服务端推一条 `{"error": "..."}` 帧后**直接结束响应**，不会再发 `{"done": true}` —— 因为前端用 `done` 作为「完成」信号，若出错后再补 `done` 会把失败覆盖成"无回答"。前端随后可在错误帧处停止拼接。
- 前端用 `fetch` + `ReadableStream` 逐帧解析（`EventSource` 仅支持 GET，本端点为 POST，故用流式 fetch）。
- 服务端按「问题关键词 × 字幕条」打分，取**时间连续、逐行带 `[MM:SS - MM:SS]` 的细粒度时间线窗口**（预算 ≤ `AI_CHAT_CONTEXT_CHARS`，关键词全不匹配则退回章节级上下文）作为上下文，**无需先调 `/ai/summary`**。
- **时间定位**：因上下文逐条带时间戳，且提示词要求——对「哪一段 / 哪几分钟 / 什么时候」这类定位题依据时间戳回答具体时间段，模型可直接给出像 `44:20–44:43` 这样的分钟级区间；片段未覆盖时如实说明、不编造时间。

错误：本端点几乎恒以 **200 + SSE 流** 应答（服务端无法预知流式进行的中间成功）：
- **URL 非法 / 超限**：请求前校验，仍为 400 / 429（`rate_limited`，来自 shared `ai` 组）。
- **无字幕 / LLM 失败**（`no_subtitles`、`llm`）：不强拆成 502，而是**在流内推 `{"error": "..."}` 帧并终止**（HTTP 200）。前端据此区分“出错”并停止拼接，不再依赖异常响应码。其中 `no_subtitles` 帧**额外带 `"code": "no_subtitles"`**（供前端按类型提示文案）；LLM 失败与字幕抽取的 generic 异常帧**无 `code`**。

> ⚠️ **AI 限流**：`/api/ai/summary`、`/api/ai/chapters`、`/api/ai/mindmap`、`/api/ai/ask` 与 `/api/subtitles`（字幕提取/翻译）共用**同一限流组 `ai`**，默认 `RATE_AI_PER_MIN=30`（每 IP 每分钟）。普通用户一次连点多个 AI 功能 + 问几个问题（~10 次/分钟）不会被打断；但仍保留刷量拦截（脚本/爬虫 60 秒内连点三十多次会触发 `429 rate_limited`）。如需更宽松/更严格可在 `.env` 调 `RATE_AI_PER_MIN`。

## 12. 账户：注册 / 登录 / 退出 / 当前用户（v0.7.0）

鉴权方式：登录成功后返回**不透明令牌**（明文只出现一次），前端存 localStorage，此后请求带 `Authorization: Bearer <token>`。令牌服务端可吊销（登出即删），库中仅存 SHA-256 哈希。

### POST `/api/auth/register`

请求：`{"email": "a@b.com", "password": "≥8位"}`（邮箱小写归一）

响应 200：`{"ok": true, "token": "...", "user": {...}}`（注册即自动登录）

错误：400 `invalid_email` / `weak_password`；409 `email_taken`；429 限流（`RATE_AUTH_PER_MIN`，默认 5/分钟/IP）。

### POST `/api/auth/login`

请求：`{"email": "...", "password": "..."}`

响应 200：同 register。

错误：401 `invalid_credentials`（**账号不存在与密码错误返回同一文案**，防邮箱枚举）；429 限流。

### POST `/api/auth/logout`

需要登录（Bearer）。吊销当前令牌。`→ 200 {"ok": true}`（幂等：令牌无效也 200）。

### GET `/api/auth/me`

**可选登录，不报错**。全站启动与支付回跳后用它刷新状态。

- 未登录：`→ {"ok": true, "user": null, "is_pro": false, "ai_used_today": 0, "ai_daily_limit": 3, "free_max_height": 720}`（游客按 IP 统计 AI 配额）
- 已登录：`{"ok": true, "user": {"email", "member_expire_at"(秒级时间戳或 null), "is_pro", "ai_used_today", "ai_daily_limit"}, "free_max_height": 0}`（PRO 不封顶返回 0，免费返回 `FREE_MAX_HEIGHT`）

## 13. 支付：Checkout / Webhook / 订单（v0.7.0，Stripe Checkout 一次性付款）

### POST `/api/billing/checkout`

需要登录。请求：`{"plan": "month" | "year"}`（仅此两键，金额/天数由服务端套餐表决定，前端无法指定）

响应 200：`{"ok": true, "order_id": "...", "url": "https://checkout.stripe.com/..."}` → 前端 `location.href = url` 跳 Stripe 托管收银台。

- 会员为**一次性购买**：月卡=30 天、年卡=365 天，重复购买**叠加**天数（非订阅）。
- 后端先建 `pending` 本地订单（幂等基石），再创建 Checkout Session（`client_reference_id=order_id`），回写 `stripe_session_id`。
- 错误：401 未登录；400 `invalid_plan`；503 `billing_disabled`（Stripe 未配置完整）；502 `checkout_failed`；429 限流（`RATE_BILLING_PER_MIN`）。

### POST `/api/billing/stripe/webhook`

Stripe 服务器回调（**不开放给普通用户**）。用 `whsec_` 密钥对**原始字节体**验签，失败 400。开通会员的**唯一可信依据**（`success_url` 回跳仅 UX 展示，绝不据此履约）。

处理事件：

| 事件 | 处理 |
|---|---|
| `checkout.session.completed` | 履约：订单 pending→paid（条件更新）+ 叠加会员时长 |
| `checkout.session.async_payment_succeeded` | 同上（3DS 异步成功） |
| `checkout.session.async_payment_failed` | 订单标记 `failed` |
| `checkout.session.expired` | 订单标记 `expired` |
| 其他 | 记录后 200（避免无意义重试） |

幂等三层（详见 [MEMBERSHIP.md](MEMBERSHIP.md) §3.4）：`stripe_session_id` UNIQUE / `event_id` 主键去重 / 订单状态机条件更新——同一事件重放、并发回调均只生效一次。非 2xx 时 Stripe 按策略重试（履约失败返回 500 并清掉事件占位，重试可重新处理）。

### GET `/api/billing/orders`

需要登录。返回本人最近 50 笔订单（越权隔离：强制 `WHERE user_id=当前用户`）：

```json
{"ok": true, "orders": [{"order_id", "plan_key", "amount_cents", "currency", "status", "created_at", "paid_at"}]}
```

`status`：`pending | paid | failed | expired`。

### PRO 权益拦截（作用于既有端点）

| 端点 | 非 PRO 行为 |
|---|---|
| POST `/api/download`、`/api/download/batch` | 所选格式 `height > FREE_MAX_HEIGHT`（默认 720）→ 403 `pro_required`；未指定格式（默认最佳）→ 服务端强制压到 `[height<=720]` 防绕过 |
| POST `/api/subtitles`（`target_lang` 非空） | 403 `pro_required`（仅提取免费） |
| AI 四端点 | 非 PRO 每日超过 `AI_FREE_DAILY_LIMIT`（默认 3，游客按 IP、账户按 user_id）→ 403 `pro_required`（响应带 `ai_used_today`/`ai_daily_limit`）；PRO 走独立更高分钟限流 `RATE_AI_PRO_PER_MIN` |

`pro_required` 统一响应：`{"ok":false,"error":"...","code":"pro_required"}`，前端弹升级弹窗。

## 错误格式

所有非标准 JSON 错误统一形如 `{"ok":false,"error":"<友好中文>","code":"<机器码>"}`：

- **JSONResponse 直接返回**（大多数端点的手工 `return JSONResponse(...)`）：扁平结构。
- **HTTPException 抛出的**（401 未登录 / 403 pro_required / 404 任务不存在 / 429 限流等）：v0.7.0 起由自定义处理器**展平为同一扁平结构**（`app/security.py` 注册 `HTTPException` 处理器，dict detail 直接作为响应体，headers 如 429 的 `Retry-After` 透传）——此前 FastAPI 默认包成 `{"detail":{...}}` 导致前端丢失真实文案（已修复）。
- 请求体 Pydantic 校验失败仍为 FastAPI 默认 422 `{"detail":[...]}`（开发期信号，前端不可触发）。
