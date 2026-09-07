# ROADMAP — 阶段性演进与待办

> 本文件记录**此前已完成**与**后续待做**。每次扩展，先更新这里，再回 [PLAN.md](PLAN.md) / [API.md](API.md) / [CHANGELOG.md](CHANGELOG.md)。

## 里程碑总览

| 里程碑 | 目标 | 状态 |
|---|---|---|
| **M1** | 核心业务跑通 + 前端按参考 UI 精美 + 安全基础 | ✅ 完成（v0.1.0） |
| **M1.1** | 抖音视频下载（服务端无头浏览器） | ✅ 完成（v0.1.2） |
| **M2** | 增值功能：字幕提取/翻译 + AI 摘要 | ⬜ 待启动 |
| **M3** | 加固上线：部署/反代/HTTPS/回归 | ⬜ 待启动 |

---

## M1（已完成）— 核心业务 + 前端精美

- [x] 脚手架：config/main/health/static 挂载
- [x] 前端单页全部区块（Hero/信任/功能卡/结果/批量/字幕/AI/定价/PRO/升级CTA/FAQ/Footer），参考给定 UI，响应式 + 动效
- [x] 安全模块：URL/SSRF 校验、滑窗限流、安全响应头、错误脱敏、临时文件清理
- [x] `downloader.probe` 解析 + 格式启发式（needs_merge/progressive/去重）
- [x] 下载调度：线程池 + 信号量 + 进度回调 + 流式 `/file` + 批量 + TTL 清理
- [x] 端到端验证：解析(Archive.org 3 档)/下载(进度/速度/成品 mp4)/批量/安全校验(422/400/429/响应头) 全通过

> M1 的**字幕/AI 摘要端点已实现但未联调**（M2 接通前端），因为 ffmpeg 也未装、无 LLM Key，M1 阶段以「服务端友好降级」为准。

---

## M1.1（已完成，v0.1.2）— 抖音视频下载

> 平台支持现状（「万能下载」的真实边界）：YouTube / BiliBili / Archive.org / 泛化 `.mp4` 等走 yt-dlp 直接可用；**抖音**因 `a_bogus` + msToken 签名墙（绑定浏览器指纹），yt-dlp 直跑/纯 Python 签名一律失败，最终以**服务端无头浏览器**（Playwright 复用系统 Chrome，匿名游客会话，拦截页面真实 `aweme/detail` 响应）打通。详见 [CHANGELOG](CHANGELOG.md) 0.1.2。

- [x] `app/douyin.py`：无头浏览器解析 + httpx 直连下载（playwm→play 去水印）
- [x] 接线 `downloader`：`_is_douyin` 路由 + `_play_url` 直连 + cookiefile/proxy 不转发字节
- [x] 开关：`DOUYIN_ENABLED`/`DOUYIN_HEADLESS`/`DOUYIN_TIMEOUT_SECONDS`
- [x] 端到端自测：解析（标题/时长/缩略图/格式）→ 下载（进度/速度）→ 成品 `ftypisom` MP4；回归普通 URL 不受影响

**已知边界**：抖音风控具时效性，签名/风控会不定期换代，超时用 `DOUYIN_TIMEOUT_SECONDS` 调、可 `DOUYIN_ENABLED=false` 一键关停。X/Twitter、Instagram、TikTok 等强反爬平台仍为预期失败（需 cookies/JS 签名），属可接受降级。

---

## M2（待启动）— 增值功能

### M2-a 字幕提取 / 翻译
- [ ] 前端字幕面板接通 `/api/subtitles`（语言下拉/提取/翻译/预览/下载）
- [ ] 无字幕视频友好提示（`no_subtitles`）
- [ ] 接入 LLM 翻译（已具备，仅需 Key + 联调）

### M2-b AI 摘要
- [ ] 前端 AI 摘要面板接通 `/api/ai/summary`
- [ ] 无字幕视频友好空态；超长字幕截断（`AI_MAX_CHARS`）
- [ ] LLM 未配置 / 超时 → 可读错误映射

**前置**：安装 ffmpeg（`winget install Gyan.FFmpeg`）、配置 `OPENAI_API_KEY`。

---

## M3（待启动）— 加固上线

- [ ] 部署：反代 + HTTPS + ICP 备案
- [ ] `--workers 1` 说明固化到部署脚本
- [ ] 缩略图代理端点（防泄漏 referrer / 防盗链）
- [ ] 回归矩阵脚本化（`pytest` 覆盖各端点 + validate_url 表驱动）
- [ ] 可选 `APP_TOKEN` 鉴权启用说明

---

## v2 演进方向（备选池，按需排期）

- **整套播放列表解析**：支持一次性解析 + 下载整个列表（当前 `noplaylist=True`）。
- **真实支付**：对接支付渠道，替换占位的 PRO 门面。
- **Redis 换内存 job store**：多实例横向扩容 + 持久化任务。
- **缩略图代理**：避免第三方直连泄漏 referrer。
- **超长字幕 map-reduce 摘要**：分块 + 归纳，突破 `AI_MAX_CHARS`。
- **可选本地 Whisper 转写**：无字幕视频也能生成摘要/翻译。
- **前端上传 cookies**：支持用户登录态抓取受限视频。
- **下载历史 & 收藏夹**：需要持久化 + 账户体系。
