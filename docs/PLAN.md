# PLAN — 万能视频下载网站 · 总方案

> 本文件是本项目的立项总纲，是后续所有设计与扩展的根。**扩展新能力前先读这里**。

## 一、为什么做

很多同学想把手上的视频保存到本地，但部分平台不支持下载、清晰度受限、批量难、还常被水印缠绕。本项目做一个**万能视频下载网站**：从各主流平台解析并下载视频，快速、随时随地（浏览器/手机均可），并叠加增值功能（视频摘要、字幕翻译）与付费体系。

**核心策略：站在巨人肩膀上**。封装主流开源项目 [yt-dlp](https://github.com/yt-dlp/yt-dlp)（十几万 Star），Python 技术栈，**纯封装、尽量少改开源代码**。我们不重造下载引擎，只做「安全的 API 门面 + 精美的前端 + 付费与增值」。

## 二、已确认决策（已经用户人工确认）

| 项 | 决策 |
|---|---|
| v1 功能 | ① 核心：粘贴链接 → 解析(标题/缩略图/时长/全部清晰度与格式) → 选格式 → 下载 ② 批量解析下载 ③ 字幕提取/翻译 ④ AI 视频摘要（v1 全上） |
| 前端形态 | 单页静态 HTML + Tailwind(Play CDN)，由 FastAPI `/static` 托管；纯手写、零构建、最轻量 |
| 付费能力 | v1 只做「价格展示 + PRO 会员体系」：完整定价/权益/免费对比/升级 CTA；支付端点为占位门面，不接真实支付、无 DB、无账户 |
| 部署形态 | **公开上线给他人用** → 必须做安全防护（见 SECURITY.md） |
| 实施节奏 | 核心业务(阶段0-2)先行跑通；**前端完整打磨尽早完成**（参考给定 UI）；分析/设计文档**同步沉淀**到 `docs/` |

## 三、环境现状（已核实）

- Windows 11，Python 3.12.4，目录 `d:\LCP_agent\ai-ppt-generator`。
- `.venv` 已装：`fastapi 0.141.1`、`uvicorn 0.52.4`、`pydantic 2.13.5`、`httpx 0.28.1`、`yt-dlp 2026.08.19`、`python-dotenv` → **核心后端仅 yt-dlp 一处依赖**（LLM 走 httpx）。后因抖音（0.1.2）新增 `playwright`（复用系统 Chrome，免下载 Chromium）。
- ⚠️ **ffmpeg 未安装**（`ffmpeg`/`ffprobe` 不在 PATH）。yt-dlp 合并分离音视频流（高清档多为 video-only + audio-only 分离流）需要 ffmpeg，否则这类档无法合并。→ 阶段2前需先装，并把下载逻辑设为无 ffmpeg 时回退单文件 progressive 档。
- LLM：用 **OpenAI 兼容接口 + env 配置**（`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`），支持 DeepSeek/智谱/星火等国产兼容模型；不内置密钥、不做本地 whisper 转写（太重）。

## 四、参考站 UI 设计语言（已用 browser-use 实测抓取 https://ai.codefather.cn/painting）

- 纯白底 `#ffffff`；大标题 `#0f172a` 36px 字重 900；副标题 `#64748b` 16px。
- 主按钮：亮蓝 `#1777ff` 白字 `border-radius:9999px`。
- 卡片：白底 `1px solid rgba(0,0,0,.05)` 淡边框 `border-radius:12px` + `group`。
- 响应式网格（手机 2 列 → 桌面 3 列），卡片 = 图 + 标题 + 多色马卡龙标签芯片（`#标签` 小字）。
- 我们在此之上加强付费引导（PRO/权益对比/升级 CTA/信任背书）与更独特视觉层次 → 「吸引付费」。

## 五、目录结构（最小文件数）

见 [README.md](../README.md)。本方案是核心，落地代码结构与它一一对应。

## 六、端到端测试矩阵（真实公网链接）

**应成功**：YouTube(公开短视频)、Bilibili(普通)、Archive.org(最简单友好)、Vimeo、泛化 `.mp4` 直链。

**预期失败需降级**（返回友好中文错误而非 500）：X/Twitter、TikTok、Instagram（常需 cookies/JS 签名/强反爬）；YouTube 需登录/限制级 → 提示需 cookies。

**自动化**：`pytest` + `httpx.AsyncClient` 覆盖各端点；`security.validate_url` 表驱动单测（mock socket：localhost/私网/回环/file/合法 https）。

**手动**：一条 curl 脚本顺序 parse → download → 轮询 → file → subtitles → summary；浏览器手动过移动端断点。

## 七、需注意的坑

1. **ffmpeg 缺失（最高优先）**：影响合并高清档与 m3u8。`winget install Gyan.FFmpeg`。
2. **Windows 大文件流式**：`/file` 用 FileResponse 流式，不整读内存；路径短于 260；`Content-Disposition` 用清洗文件名。
3. **内存 job store → 必须单进程单 worker**：多 worker 各自独立致轮询 404。部署写 `--workers 1`；重启丢失进行中任务（v1 权衡，横向扩需换 Redis）。
4. **平台反爬/登录/cookies/限速**：X/Instagram/TikTok 多数失败；YouTube 部分需 cookies。v1 不做前端上传 cookies，支持运营者 `COOKIES_FILE` 自测；`http_headers` 设真实 UA。
5. **`noprogress=True`**：否则 yt-dlp 往 stderr 灌进度干扰日志；进度只靠 progress_hooks。
6. **`noplaylist=True`**：避免粘贴播放列表误拉整列表；整套列表留 v2。
7. **LLM 上下文/成本**：字幕长则 `AI_MAX_CHARS` 截断 + 单次调用；超长 map-reduce（v2）。
8. **yt-dlp 更新频繁**：站点改版多，更新后跑矩阵回归。

## 八、文档体系

- [**DESIGN.md**](DESIGN.md) — 架构设计详解（Job 模型、并发、格式启发式、安全模型、选型理由）
- [**API.md**](API.md) — 接口契约（端点/字段/错误码）
- [**SECURITY.md**](SECURITY.md) — 安全清单与威胁模型
- [**ROADMAP.md**](ROADMAP.md) — 演进方向与待办（v2）
- [**CHANGELOG.md**](CHANGELOG.md) — 按里程碑记录实现进度与决策变更
