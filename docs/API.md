# API.md — 接口契约

> 扩展新端点前先读这里。请求/响应皆为 JSON（除 `/file` 为流式文件下载）。
> 所有错误统一返回 `{"ok":false,"error":"<友好中文>","code":"<机器码>"}`（限流与 500 有时带 `detail` 包裹，见末尾「错误格式」）。

Base URL：`http://<host>:<port>`（默认 `8000`）。

---

## 全局错误码

| code | HTTP | 含义 |
|---|---|---|
| `invalid_url` | 400 | 链接为空/非 http/https/非法主机等 |
| `ssrf` | 422 | 命中内网/私网/回环/保留地址（重点 `169.254.169.254`） |
| `unsupported` | 400 | 平台或链接格式不支持 |
| `not_found` | 404 | 视频不存在或已失效 |
| `forbidden` | 502 | 平台拒绝访问（需登录/地区/版权限制） |
| `login_required` | 403 | 需要登录后才能访问 |
| `extract_failed` | 502 | 无法提取（反爬/链接失效） |
| `no_ffmpeg` | 500 | 服务器缺 ffmpeg，高清合并不可用 |
| `no_subtitles` | 422 | 视频无可用字幕 |
| `rate_limited` | 429 | 超过本服务限流 |
| `rate_limited_by_platform` | 429 | 被目标平台限速 |
| `timeout` | 504 | 请求超时 |
| `unknown` | 502 | 兜底未知错误 |
| `internal` | 500 | 服务器内部错误 |

---

## 1. GET `/api/health`

存活探针。

```json
→ 200 {"ok": true, "version": "0.1.0"}
```

---

## 2. POST `/api/parse`

解析链接元数据与全部可用格式。**不下载。**

请求：`{"url": "https://..."}`

响应 200：
```json
{
  "title": "Big Buck Bunny",
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
      "vcodec": "avc1",       // null 表示该档为音频
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

错误：400 / 422 / 429 / 502。

> **抖音特例**：抖音 URL 走服务端无头浏览器（`app/douyin.py`）。返回的 `formats` 恒为**单个 progressive mp4 档**（`needs_merge=false`，`extension` 恒 `mp4`），`subtitles` 为空数组（抖音无字幕提取）。`extractor` 为 `Douyin`。其余字段（title/thumbnail/duration/webpage_url）与通用格式一致。

---

## 3. POST `/api/download`

创建单个下载任务，返回可轮询的 `job_id`。

请求：`{"url": "...", "format_id": "22"}`（`format_id` 可省，默认最佳）

响应 202：
```json
{"job_id": "6f2c...", "status": "queued"}
```

错误：400 / 422 / 429 / 503。

---

## 4. POST `/api/download/batch`

批量创建任务（≤ `MAX_BATCH`）。

请求：
```json
{"items": [{"url": "..."}, {"url": "...", "format_id": "18"}]}
```

响应 202：
```json
{"jobs": [{"job_id": "a1f0", "status": "queued", "url": "..."}]}
```

某项失败时该项 `job_id` 为空并带 `error`。错误：400（超 MAX_BATCH）/429/503。

---

## 5. GET `/api/jobs/{job_id}`

轮询任务状态与进度。**不含服务端绝对路径。**

响应 200（示例 `downloading`）：
```json
{
  "job": {
    "id": "6f2c...",
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
    "created_at": 1799...
  }
}
```

`status` 取值：`queued | probing | downloading | done | error | closed`。

错误：404（不存在）、410（过期已清理）。

---

## 6. GET `/api/jobs/{job_id}/file`

流式下载成品文件。带 `Content-Disposition: attachment; filename=...`、`Content-Type` 按 ext。

- 错误：404（不存在/未完成）、409（未为 done）、410（过期）。

---

## 7. DELETE `/api/jobs/{job_id}`

尽力取消并清理。成功 `{ok:true}`；不存在 `404`。

---

## 8. POST `/api/subtitles`

提取或翻译字幕。

请求：`{"url":"...","lang":"zh","is_auto":false,"target_lang":"简体中文"}`（`target_lang` 有则翻译）

响应 200：
```json
{
  "lang": "zh",
  "source": "manual",      // manual | auto
  "format": "srt",
  "content": "1\n00:00:00,000 --> ...\n",
  "translated": "这是翻译后的字幕..."   // 仅当 target_lang 存在
}
```

错误：400 / 422（`no_subtitles`） / 429 / 502。

---

## 9. POST `/api/ai/summary`

由字幕生成 AI 摘要。

请求：`{"url":"..."}`

响应 200：
```json
{
  "summary": "【要点】\n- ...\n【关键词】\n- ...",
  "lang": "zh",
  "model": "deepseek-chat",
  "used_source": "manual"
}
```

错误：400 / 422（无字幕）/ 429 / 502（LLM 未配置或失败）。

---

## 错误格式

常规（JSONResponse 直接返回）：
```json
{"ok": false, "error": "操作过于频繁，请稍后再试", "code": "rate_limited"}
```

限流与兜底 500 经 FastAPI 异常处理器包裹（`detail` 内含子对象）：
```json
{"detail": {"ok": false, "error": "...", "code": "..."}}
```

> 前端统一 `res.json()`，取 `data.error || data.detail?.error` 展示即可（见 `static/app.js` 的 `api()`）。
