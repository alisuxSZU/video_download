# SECURITY — 安全清单与威胁模型

本项目面向**公开上线给他人使用**，安全是硬性要求。以下为已落实的防护与威胁模型。代码实现在 [app/security.py](../app/security.py)。

## 威胁模型

本项目核心风险不是「存储泄漏或注入」，而是**把我们自己的服务器变成攻击者的工具**：

| 威胁 | 场景 | 对策 |
|---|---|---|
| **SSRF 内网探测** | 攻击者传内网 URL，命令服务器访问 `/etc/`、云元数据 `169.254.169.254`、内网数据库 | validate_url(主机名→真实 IP→私网/回环/保留段拦截) |
| **协议注入** | `file:///c:/...`、`gopher://`、本地文件读取 | 仅允许 `http/https` |
| **DNS rebinding** | 首次校验公网 IP，二次解析转发到内网 | 前置校验 + 日志审查（务实折中，见下） |
| **成为下载中继/滥用** | 攻击者让我们的服务器批量抓取任意站（= 免费代理） | 限流 + 单机并发上限。⚠️ 可选的 `EXTRACTOR_ALLOWLIST` 配置项已留存，但**尚未接入** `validate_url`，当前不等同于已生效的白名单 |
| **文件系统攻击** | 文件名路径穿越、Windows 非法字符 | `restrictfilenames` + `sanitize_filename` 二次清洗 + `TEMP_DIR/{uuid}/` 隔离 |
| **磁盘耗尽** | 持续下载不清理 | 后台 TTL 循环清理 + 单机并发上限 |
| **信息泄露** | 异常回显堆栈/绝对路径/完整 URL | 全局 ExceptionHandler 脱敏返回 + 日志只记 host |
| **XSS / 点击劫持** | 前端注入、iframe 套壳 | `X-Frame-Options:DENY` + CSP。⚠️ 前端 `app.js` 的 `addBatchRow` 把用户输入 `url` 直插 `innerHTML` 未转义，且 CSP `script-src`/`style-src` 含 `'unsafe-inline'` → 注入防线被削弱，见「已确认缺口」 |
| **越权** | 无账户体系，仅限轮询自己 job | job_id 为 UUID 不可猜测；`/file` 校验 job 存在。v0.7.0 起订单/账户查询强制 `WHERE user_id=当前用户`，令牌仅哈希存储 |
| **伪造支付回调**（v0.7.0） | 攻击者直 POST `/api/billing/stripe/webhook` 伪造「支付成功」白嫖会员 | Webhook 强制 Stripe 签名验证（原始字节体 + `whsec_` 密钥），失败 400；会员开通**只**由验签后的事件驱动 |
| **重放/重复开通**（v0.7.0） | 同一支付事件重放、并发回调 → 重复叠加会员时长 | 三层幂等：`stripe_session_id` UNIQUE / `event_id` 主键去重 / 订单 `pending→paid` 条件更新（详见 MEMBERSHIP.md §3.4） |
| **金额篡改**（v0.7.0） | 前端改请求体指定金额/天数 | 前端只传 `plan` 键；金额/天数只取自服务端套餐表与验签后的 Stripe 回执 |
| **凭据破解/枚举**（v0.7.0） | 撞库、批量注册、探测已注册邮箱 | 密码 PBKDF2-HMAC-SHA256 加盐 200k 迭代 + 常数时间比较；登录失败统一文案防枚举；注册/登录/下单独立限流 |

## 已落实的具体防护

### 1. URL / SSRF 校验（`validate_url`）
- 仅允许 `http/https` schema → 否则 `InvalidURL`(400)。
- `socket.getaddrinfo` 解析出**真实 IP**（抗域名解析），用 `ipaddress` 判断：
  - 私网/回环/保留段：`127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 0.0.0.0/8, 100.64.0.0/10`
  - IPv6：`::1/128, fc00::/7, fe80::/10, ::ffff:0:0/96`（覆盖所有 IPv4 映射地址，含 `127/8`、`169.254`）
  - 命中私网/回环/保留段 → `SSRFError`(422)；域名解析不出 / 非法 URL → `InvalidURL`(400)（两条路径状态码不同）。
- 重点拦云元数据 `169.254.169.254`。

### 2. 滑窗限流（`RateLimiter`）
- 内存滑窗，按 `IP + 端点分组` 计数，每 60s 一窗。
- 分档：parse(20/min) < download(6/min) < ai(3/min)（默认值，均可在 `.env` 调）。
- 超限 → 429 + `Retry-After`（响应里给友好中文）。
- 另设单机并发下载上限（`MAX_CONCURRENT_DOWNLOADS`）兜底。

### 3. 临时文件安全
- 每个任务独立目录 `TEMP_DIR/{uuid}/`，隔离互扰。
- `restrictfilenames` + `sanitize_filename`（剔除 `\/:*?"<>|`、控制字符、路径穿越，限长 150）。
- 后台每 `CLEANUP_INTERVAL` 清理超 `FILE_TTL` 的目录与过期 job。
- 响应绝不下发服务端绝对路径，只给清洗后的 `filename`。

### 4. 安全响应头（`SecurityHeadersMiddleware`）
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- `Content-Security-Policy`（实际，[security.py](app/security.py) `SecurityHeadersMiddleware.dispatch` 内联拼装，无 `_build_csp` 函数）：`default-src 'self'`；`script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net`（v0.2.8 起加 jsdelivr，供 marked/DOMPurify/mermaid）；`style-src 'self' 'unsafe-inline' https://fonts.googleapis.com`；`img-src 'self' data: blob: http: https:`（v0.2.8 起加 `blob:`，供导图导出 `new Image().src=objectURL`）；`font-src 'self' https://fonts.gstatic.com`；`connect-src 'self' https:`。

> ⚠️ `script-src` / `style-src` 含 `'unsafe-inline'`（Tailwind Play CDN 需要），会削弱注入防御；需理解其取舍，加固见文末「已确认缺口」。

### 5. CORS
- 默认同源，不开宽松。
- 跨域部署才设 `ALLOWED_ORIGIN=你的域`，仅放行该源。

### 6. 错误不泄露
- 全局 `ExceptionHandler`：堆栈只进日志，返回友好中文 + 机器码。
- `friendly_error(exc, url)` 按错误特征映射（unsupported/not_found/forbidden/no_ffmpeg/no_subtitles/timeout/...），绝不吐堆栈/绝对路径/完整 URL。
- `_safe_url`：日志只记 `scheme://host`，去掉敏感参数。

### 7. 依赖更新
- `yt-dlp>=2026.08.19`，README 提醒定期 `pip install -U yt-dlp` 并跑矩阵回归。

### 8. 其他
- 严禁 shell 拼接 / `--exec`。
- ⚠️ `APP_TOKEN` 配置项已留存（.env/`config.py`），但各端点**均未校验** `X-App-Token`，当前不构成鉴权，请勿把它当作已启用。
- LLM Key 仅服务端，不下发前端。
- 缩略图走本站代理端点 `GET /api/thumbnail?url=...`（真实 UA + 对应平台 Referer，复用 `validate_url` 防 SSRF），规避 CND 防盗链 + 浏览器混合内容拦截；防盗链细化留 v2 强化。
- **AI 端点安全**：新增的 `/api/ai/ask`（SSE 流式问答）与 `/api/ai/summary` 一样**先过 `validate_url`（防 SSRF）+ `ai` 组限流（默认 3/min）**，LLM 调用走服务端 httpx（Key 不下发）。SSE 帧为服务端手写 `data: <json>`（原生 `StreamingResponse`），`json.dumps` 会把 token 内换行/特殊字符转义，不含可注入的裸 HTML/script。
- **AI 输出消毒（v0.2.8）**：摘要与问答前端**默认用 markdown 渲染**（`marked.parse`），LLM 输出**不可信、必先经 `DOMPurify.sanitize` 再入 DOM**，否则 markdown 可注入裸 `<script>`/事件属性。DOMPurify 默认剥 `script`/事件处理器/`style`，只留安全标签 —— 是 v0.2.8 引入 marked 后新增的注入面，已成对加固。
- **字幕稿缓存**：`_transcript_cache` 只存内存（TTL `TRANSCRIPT_CACHE_TTL_SECONDS`，超限丢最旧），用于 AI 问答复用，**不落盘、不超过 100 条**；与 job store 同属内存态，重启即清 —— 不属于持久化敏感数据的新面。

### 9. 抖音（服务端无头浏览器）附加防护
抖音走 `app/douyin.py` 的服务端无头 Chrome 方案（详见 [CHANGELOG](CHANGELOG.md) 0.1.2）。相较通用流程，额外注意：

- **不接触用户 Cookie/登录态**：一律用浏览器**匿名游客会话**，绝不加载、读取或转发任何用户凭据。安全约束「无需用户提供 Cookie」由此成立。
- **cookiefile 不转发**：即使运营者配置了 `COOKIES_FILE`，`_build_base_params` 对字节系 URL 打 `_bytedance` 标记，**cookiefile/proxy 均不转发给抖音**，避免运营者 Cookie（可能含账号信息的隐私）外泄到第三方。
- **不转发票据/凭据给外部**：解析只从页面响应拿 `play_addr.url_list[0]`，下载用 httpx 仅带 UA + `Referer: https://www.douyin.com/` 直连 CDN，无任何字节 token 会被存储或回传。
- **可关闭**：`DOUYIN_ENABLED=false` 一键关闭抖音，走友好降级提示，不影响其他平台。
- **风控兜底**：超时/验证码/异常统一映射为友好中文（`DouyinBlockedError`→`forbidden`、`DouyinUnsupportedError`→`unsupported`），绝不 500/泄漏堆栈。

### 10. 会员账户与支付（v0.7.0，Stripe）

支付安全是本项目的硬底线，实现见 [app/billing.py](../app/billing.py) / [app/auth.py](../app/auth.py)，威胁模型见上表新增四行。逐条落实：

- **Webhook 强制验签**：`construct_event` 用 Stripe SDK 对**原始字节体**（`await request.body()`，禁止先 JSON 解析）+ `whsec_` 密钥验签，失败一律 400（Stripe 会重试，伪造请求被拒）。
- **履约只信验签后的事件**：会员开通**唯一**由 `checkout.session.completed` / `async_payment_succeeded` 驱动；`success_url` 浏览器回跳仅 UX 展示，绝不据此发放权益（防关页面/伪造跳转）。
- **三层幂等防重复开通**：① `orders.stripe_session_id` UNIQUE（一会话一订单）；② `webhook_events.event_id` 主键去重（Stripe 官方声明事件可能**重复且乱序**投递，重放直接 200 短路）；③ 履约在 `BEGIN IMMEDIATE` 事务内做 `UPDATE orders ... WHERE status='pending'` 条件更新，影响行数=0 即短路，并发/重放也只加一次时长。履约失败时删除事件占位并返回 500，让 Stripe 重试可重新处理。
- **金额/天数不可被前端指定**：请求体只有 `plan` 键；天数查服务端套餐表，金额落单取验签后回执的 `amount_total/currency` 留痕。
- **密钥管理**：`sk_test_/sk_live_` 只存 `.env`（已 gitignore）；任何 API 响应不回传密钥/密码哈希/令牌明文；`billing_enabled` 由密钥是否齐全推导，未配置时支付端点 503 且不发起任何对 Stripe 的外呼。
- **密码存储**：PBKDF2-HMAC-SHA256，每用户 16 字节独立随机 salt、200k 迭代（标准库实现）；校验用 `hmac.compare_digest` 常数时间比较。
- **会话令牌**：`secrets.token_urlsafe(32)` 不透明令牌，库中仅存 SHA-256 哈希（拖库不可反推）；登出即删可吊销；无 JWT 撤销难题。
- **防邮箱枚举**：登录对「账号不存在」与「密码错误」返回同一 401 文案。
- **越权隔离**：`/api/billing/orders` 强制 `WHERE user_id=当前用户`；`public_user` 视图不含 `user_id`/密码哈希。
- **限流**：注册/登录（`RATE_AUTH_PER_MIN`=5/分/IP）、创建支付会话（`RATE_BILLING_PER_MIN`=5/分/IP）独立限流防爆破与刷单；AI 配额按 subject（游客 IP / 用户 ID）日计。
- **SQL 注入**：SQLite 全量参数化 `?` 占位；写操作经统一锁（`BEGIN IMMEDIATE`）串行化。
- **错误展平（v0.7.0 修复）**：`HTTPException` 自定义处理器把 `{"detail":{...}}` 展平为统一 `{"ok":false,error,code}`，前端不再丢失 401/403/429 真实文案（原「已确认缺口 #4」已闭环）。
- 已知取舍：v1 不做邮箱验证/忘记密码（需 SMTP）；退款后不自动回收会员（Stripe Dashboard 人工操作，记 v2）；SQLite 单文件库依赖文件系统权限保护（部署目录不可被 web 根直接访问）。

## 已知务实折中

- **DNS rebinding**：本项目先「前置校验 + 日志审查」。彻底防御需在真正发起连接处二次校验或禁用重定向，成本高；对公网普通防护已足够，后续可加强。
- **多 worker**：内存限流与内存 job store 均单进程生效。若未来多实例部署，限流与 job 需外置（Redis）。

## 已确认缺口（文档如实标注，是否落地由你来定）

以下项在文档中**如实标注**：配置已留存但代码未落地，或防御被削弱，均**不构成已启用的防护**。公开上线前建议逐一决定取舍：

1. **`EXTRACTOR_ALLOWLIST` 未接入**：`settings.extractor_allowlist` 无任何读取点，`validate_url` 只留注释即 `return url`。启用需在 `validate_url` 里按 `ie_key`/host 校验。不启用则任何站都可被抓（仅靠限流兜底）。
2. **`APP_TOKEN` 未校验**：任何端点都不检查 `X-App-Token`，配置项无实际效果。启用需加依赖/中间件（全部 `/api` 路由）。
3. **XSS 注入面收紧**：`static/app.js` `addBatchRow` 用 `innerHTML` 直插 `url` 未转义；CSP `script-src`/`style-src` 含 `'unsafe-inline'`（Tailwind Play CDN 需要但不必然冲突，可改用编译后的 Tailwind 或 `textContent` 赋值）。三处建议：`textContent` 替代 `innerHTML`、`url` 入库前洗、评估是否去掉 `'unsafe-inline'`。⚠️ **已局部改善**：AI 问答气泡（v0.2.6 `askBubble`/`handleAsk`）已改 `textContent` 赋值（用户问题/模型增量/错误文案不再进 `innerHTML`）；**摘要/问答 markdown（v0.2.8）经 `DOMPurify.sanitize(marked.parse(...))` 消毒后再渲染**，AI 输出注入面已闭环。**仍为注入面**：`formatRow`（`static/app.js`）对 `f.resolution`/`f.ext`（来自 yt-dlp 的平台元数据，属不可信输入）仍用 `innerHTML` 直插——虽一般非用户直接控制，但接的是外部数据、仍是剩余注入面；`addBatchRow` 的 `url` 亦仍待改。
4. ~~**前端错误文案丢失**~~：✅ **v0.7.0 已修复**——`app/security.py` 注册 `HTTPException` 处理器，服务端把 `detail` 包裹的错误展平为统一扁平格式（含 401/403/429），前端 `api()` 无需改动即读到真实文案。

## 上线检查清单

- [x] validate_url 拦截 localhost / 192.168.1.1 / file:// 且不触发下游请求
- [x] 连续请求触发 429 + 友好文案
- [x] curl 查看响应头（CSP/X-Frame-* 等）齐全
- [x] 构造错误不包含堆栈/绝对路径/完整 URL
- [x] 支付：Webhook 验签拒绝伪造请求；同一事件重放只履约一次；未登录/他人订单不可见（test_membership.py 29 项离线回归全绿）
- [ ] 部署：反代 + HTTPS + ICP 备案（支付域名需 HTTPS，Stripe 强制）
- [ ] 定期升级 yt-dlp 并跑回归矩阵
- [ ] 逐项评估「已确认缺口」（extractor 白名单 / APP_TOKEN / XSS 注入面）
- [ ] 上线切 live 密钥前：轮换 `sk_live_`/`whsec_`（Webhook 端点在 Dashboard 重建）、核对两个 Price 为 live 模式、确认 `.env` 不含 test 密钥
