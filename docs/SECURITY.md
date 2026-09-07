# SECURITY — 安全清单与威胁模型

本项目面向**公开上线给他人使用**，安全是硬性要求。以下为已落实的防护与威胁模型。代码实现在 [app/security.py](../app/security.py)。

## 威胁模型

本项目核心风险不是「存储泄漏或注入」，而是**把我们自己的服务器变成攻击者的工具**：

| 威胁 | 场景 | 对策 |
|---|---|---|
| **SSRF 内网探测** | 攻击者传内网 URL，命令服务器访问 `/etc/`、云元数据 `169.254.169.254`、内网数据库 | validate_url(主机名→真实 IP→私网/回环/保留段拦截) |
| **协议注入** | `file:///c:/...`、`gopher://`、本地文件读取 | 仅允许 `http/https` |
| **DNS rebinding** | 首次校验公网 IP，二次解析转发到内网 | 前置校验 + 日志审查（务实折中，见下） |
| **成为下载中继/滥用** | 攻击者让我们的服务器批量抓取任意站（= 免费代理） | 限流 + 可选的 extractor 白名单 + 单机并发上限 |
| **文件系统攻击** | 文件名路径穿越、Windows 非法字符 | `restrictfilenames` + `sanitize_filename` 二次清洗 + `TEMP_DIR/{uuid}/` 隔离 |
| **磁盘耗尽** | 持续下载不清理 | 后台 TTL 循环清理 + 单机并发上限 |
| **信息泄露** | 异常回显堆栈/绝对路径/完整 URL | 全局 ExceptionHandler 脱敏返回 + 日志只记 host |
| **XSS / 点击劫持** | 前端注入、iframe 套壳 | CSP + `X-Frame-Options:DENY` + 输出转义 |
| **越权** | 无账户体系，仅限轮询自己 job | job_id 为 UUID 不可猜测；`/file` 校验 job 存在 |

## 已落实的具体防护

### 1. URL / SSRF 校验（`validate_url`）
- 仅允许 `http/https` schema → 否则 `InvalidURL`(400)。
- `socket.getaddrinfo` 解析出**真实 IP**（抗域名解析），用 `ipaddress` 判断：
  - 私网/回环/保留段：`127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 0.0.0.0/8, 100.64.0.0/10`
  - IPv6：`::1/128, fc00::/7, fe80::/10, ::ffff:0:0/96`（覆盖所有 IPv4 映射地址，含 `127/8`、`169.254`）
  - 命中或解析不出合法 IP → 保守拒绝（422）。
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
- `Content-Security-Policy`：`default-src 'self'`；script 仅同源 + `cdn.tailwindcss.com`；font 仅 `fonts.gstatic.com`；connect 仅同源 + `https:`。

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
- 请求体大小/超时校验（FastAPI 默认）。
- 可选 `APP_TOKEN`（请求头 `X-App-Token`）鉴权，留空关闭。
- LLM Key 仅服务端，不下发前端。
- 缩略图是第三方 URL 直连（v2 加代理端点防泄漏 referrer）。

### 9. 抖音（服务端无头浏览器）附加防护
抖音走 `app/douyin.py` 的服务端无头 Chrome 方案（详见 [CHANGELOG](CHANGELOG.md) 0.1.2）。相较通用流程，额外注意：

- **不接触用户 Cookie/登录态**：一律用浏览器**匿名游客会话**，绝不加载、读取或转发任何用户凭据。安全约束「无需用户提供 Cookie」由此成立。
- **cookiefile 不转发**：即使运营者配置了 `COOKIES_FILE`，`_build_base_params` 对字节系 URL 打 `_bytedance` 标记，**cookiefile/proxy 均不转发给抖音**，避免运营者 Cookie（可能含账号信息的隐私）外泄到第三方。
- **不转发票据/凭据给外部**：解析只从页面响应拿 `play_addr.url_list[0]`，下载用 httpx 仅带 UA + `Referer: https://www.douyin.com/` 直连 CDN，无任何字节 token 会被存储或回传。
- **可关闭**：`DOUYIN_ENABLED=false` 一键关闭抖音，走友好降级提示，不影响其他平台。
- **风控兜底**：超时/验证码/异常统一映射为友好中文（`DouyinBlockedError`→`forbidden`、`DouyinUnsupportedError`→`unsupported`），绝不 500/泄漏堆栈。

## 已知务实折中

- **DNS rebinding**：本项目先「前置校验 + 日志审查」。彻底防御需在真正发起连接处二次校验或禁用重定向，成本高；对公网普通防护已足够，后续可加强。
- **多 worker**：内存限流与内存 job store 均单进程生效。若未来多实例部署，限流与 job 需外置（Redis）。

## 上线检查清单

- [x] validate_url 拦截 localhost / 192.168.1.1 / file:// 且不触发下游请求
- [x] 连续请求触发 429 + 友好文案
- [x] curl 查看响应头（CSP/X-Frame-* 等）齐全
- [x] 构造错误不包含堆栈/绝对路径/完整 URL
- [ ] 部署：反代 + HTTPS + ICP 备案
- [ ] 定期升级 yt-dlp 并跑回归矩阵
