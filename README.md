# 万能视频下载网站

一条链接，下遍全网。粘贴链接即可解析多清晰度/格式、批量下载、提取并翻译字幕、AI 生成视频摘要。

站在巨人肩膀上 —— 封装开源项目 **yt-dlp**（十几万 Star）作为下载引擎，Python 技术栈；抖音走成熟的无头浏览器方案（Playwright 复用系统 Chrome）。

> ⚠️ 免责声明：本工具仅供个人学习、研究与备份，请遵守原始平台条款与版权法律，严禁侵权或转售。页面已含免责声明全文。

---

## 快速开始

### 1. 安装 ffmpeg（必需）

高清档多为「视频流 + 音频流分离」，yt-dlp 需要 ffmpeg 合并，否则只能下单文件/降级清晰度。

```bash
winget install Gyan.FFmpeg
```

装完后在终端确认：

```bash
ffmpeg -version
```

若未加入 PATH，把 `ffmpeg.exe` 所在目录填到 `.env` 的 `YTDLP_FFMPEG_LOCATION`。

### 2. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

**抖音需额外装本机 Chrome/Edge**（Playwright 复用系统 Chrome，无需下载 Chromium）：
```bash
# 安装 Google Chrome 或 Microsoft Edge 之一即可；无需执行 `playwright install`
```
- `DOUYIN_ENABLED=false` 可一键关闭抖音；缺少 Chrome 时抖音走友好降级，不影响其他平台。

### 3. 配置

```bash
cp .env.example .env
```

（Windows PowerShell 用 `Copy-Item .env.example .env`）

- 至少填 `OPENAI_API_KEY` 才能用字幕翻译 / AI 摘要（用 DeepSeek 等兼容接口）。
- 其余默认即可，详见 [docs/SECURITY.md](docs/SECURITY.md) 与 [docs/API.md](docs/API.md)。

### 4. 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **必须单 worker**：任务存于进程内存，多 worker 各自独立会导致轮询 404。见「注意事项」。

浏览器打开 http://127.0.0.1:8000 。

---

## 主要功能（v1）

| 能力 | 说明 |
|---|---|
| 万能解析 | 支持数百平台；粘贴即出标题/缩略图/时长/全部清晰度与格式 |
| 格式多选下载 | 每档标注分辨率/大小/是否需合并；选高清档自动合并音视频 |
| 批量下载 | 一次解析多个链接或粘贴多个 URL，逐条进度轮询 |
| 字幕提取/翻译 | 手动>自动字幕；一键翻译成简体中文（LLM） |
| AI 视频摘要 | 由字幕生成分点摘要 + 关键词（LLM，仅服务端持 Key） |
| PRO 定价展示 | 免费 vs PRO 权益对比、升级引导（v1 占位，未接真实支付/无 DB） |
| 隐私安全 | URL/SSRF 校验、限流、安全响应头、错误脱敏、临时文件过期自动清理 |

---

## 目录结构

```
app/
  main.py        # FastAPI 入口、lifespan、安全中间件、挂载 /static
  config.py      # .env 集中配置
  models.py      # Pydantic 模型 + Job 数据类(threading.Lock)
  security.py    # URL/SSRF 校验、滑窗限流、安全头、全局异常、文件名清洗
  downloader.py  # yt-dlp 封装：probe/download/进度/格式启发式/字幕
  tasks.py       # 内存 job store + 线程池 + 信号量 + 后台清理
  ai.py          # OpenAI 兼容 LLM：翻译 + 摘要
  routes.py      # 全部 API 端点
static/
  index.html.app.js / styles.css          # 前端单页（HTML + 逻辑 + 样式）
docs/            # ★ 方案与设计文档（扩展功能的依据）
  OVERVIEW.md    # ★ 开发前地基快照（先读）——当前状态/关键坑/下一步
  PLAN.md        # 总方案（动机、决策、环境、UI 设计语言）
  DESIGN.md      # 架构设计详解
  API.md         # 接口契约
  SECURITY.md    # 安全清单与威胁模型
  ROADMAP.md     # 演进方向（v2）
  CHANGELOG.md   # 里程碑实现进度
```

---

## API 一览

见 [docs/API.md](docs/API.md)。核心端点：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 存活探针 |
| POST | `/api/parse` | 解析链接元数据 + 全部格式 |
| POST | `/api/download` | 创建下载任务 |
| POST | `/api/download/batch` | 批量创建下载任务 |
| GET | `/api/jobs/{job_id}` | 轮询任务状态/进度 |
| GET | `/api/jobs/{job_id}/file` | 下载成品文件（流式） |
| DELETE | `/api/jobs/{job_id}` | 取消并清理 |
| POST | `/api/subtitles` | 提取/翻译字幕 |
| POST | `/api/ai/summary` | 生成 AI 摘要 |

---

## 注意事项 / 已知坑

1. **单进程运行**：任务在内存中，多 worker 会各自为政。横向扩展需把 job store 换成 Redis（见 ROADMAP v2）。
2. **平台反爬/登录/地区版权**：抖音走**服务端无头浏览器**（Playwright 复用系统 Chrome，匿名游客会话，无需用户 Cookie / 免浏览器插件），见 `.env` 的 `DOUYIN_*`；X、Instagram、TikTok 多数需 cookies/JS 签名，仍易失败；YouTube 部分需登录/有限制。失败会返回友好中文错误而非崩溃。运营者可用 `COOKIES_FILE` 自测。
3. **ffmpeg 缺失**：影响合并高清档与 m3u8 下载；解析阶段会标 `needs_merge`，无 ffmpeg 时前端显示并降级单文件档。
4. **更新 yt-dlp**：平台改版频繁，持续可用需定时升级并跑回归：

   ```bash
   pip install -U yt-dlp
   ```

   回归矩阵见 `docs/PLAN.md · 端到端测试`。

---

## 安全（面向公开上线）

公开运营必须重视。本项目已内置：URL/SSRF 校验（含 `169.254.169.254` 元数据拦截）、滑窗限流（返回 429 + 重试等待）、安全响应头与 CSP、错误统一脱敏（不泄堆栈/绝对路径/完整 URL）、临时文件过期清理、日志脱敏。详单见 [docs/SECURITY.md](docs/SECURITY.md)。

---

## 许可证与合规

本项目仅供学习研究参考，作者不承担任何因滥用导致的法律责任。下载内容仅限个人学习研究，请务必遵守原始平台条款与相关版权法律。
