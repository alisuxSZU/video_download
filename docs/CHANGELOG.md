# CHANGELOG — 按里程碑记录的实现进度与决策变更

## [0.2.7] — 格式选项去噪：删「需合并音视频/单文件」角标 + 剔除 MHTML 故事板伪格式（2026-09-08）

> 目标：**清晰度/格式列表只给用户「能从里面挑到真实可下视频」的选项**。删掉没有意义的「需合并音视频/单文件」角标；并把 yt-dlp 返回的 `sb*`（storyboard 缩略图，`ext=mhtml`、`filesize=0`、无真实码流）这类**伪格式**过滤掉 —— 此前它们会以「180p / MHTML」「90p / MHTML」这样的选项混在列表里，用户点下去下载不到真视频（正是用户提问「MHTML 是什么」的来源）。**真实浏览器端到端回归。**

### Changed

- **`static/app.js`（`formatRow`）**：删除右下角 `mergeChip`（「需合并音视频」琥珀色 /「单文件」绿色），格式卡片现只展示 `清晰度 + 格式 · 大小`。理由：合并是我们的后台工作，用户无需理解；且统一去掉后所有选项视觉一致。卡片布局改为 `justify-between` 但仅左侧内容。
- **`app/downloader.py`（`_clean_formats`）**：新增 `_is_storyboard(f)`（`ext=='mhtml'` 或 `protocol=='mhtml'` → 剔除），在格式清洗循环里 `continue`，避免 mhtml 故事板进入 `formats`。

### Security / 边界

- **只剔 mhtml，不动其它**：storyboard 是唯一 `ext/protocol=mhtml` 的伪格式；真实视频（mp4/webm）、音视频分离档（video-only 需合并）、以及 `_is_audio_only` 过滤逻辑均保持不变 → 不影响选真实档位的准确性，也未误伤 Archive.org 等无 codec 信息的平台。

### Verified（真实浏览器端到端回归）

- ✅ `e2e_test.py`：解析后 `#formatList` 文本仅含 `1080p/720p/…/144p + MP4 · 大小`，**不含**「需合并音视频」「单文件」「MHTML」；格式网格 `rows=6`（此前含 sb0-sb3 为 10）。其余 6 个 AI 功能（字幕/翻译/摘要/章节/导图/问答）无回归；无控制台错误。

> 本文件记录「做了什么、改了什么、为什么」，供回溯。格式遵循 keep-a-changelog 精神：`[Added] / [Changed] / [Fixed] / [Removed]`，并附实现细节与决策。

## [0.2.6] — AI 问答改为「气泡聊天框」（2026-09-08）

> 目标：把问答面板从「纯文本 Q&A 流式输出」升级为**气泡聊天**——用户问题一个气泡（靠右、品牌蓝）、AI 回答一个气泡（靠左、灰），SSE 增量实时写入 AI 气泡并自动滚动。**真实浏览器端到端回归。**（沿用上一版心智：`#askStatus` 不存在，保留 `#askInput/#askOutput/#askBtn`。）

### Changed

- **`static/index.html`**：`#askOutput` 由纯文本 `div`（`whitespace-pre-wrap`）改为 `flex flex-col gap-2.5 … overflow-auto` 气泡容器。
- **`static/app.js`（`handleAsk`）**：不再把整段 `Q：…`/`A：…` `textContent` 拼接；改为 `askBubble(role,text)` 生成**用户问题气泡（`bg-brand` 白字、`rounded-br-sm`）+ AI 回答气泡（灰底、`rounded-bl-sm`）**，两者以 `[data-role=user|assistant]` 标记。SSE 增量用 `aiMsg.bubble.textContent += delta` 写进单个 AI 气泡（`started` 标志在**首帧**清掉「…」占位），并 `out.scrollTop = out.scrollHeight` 自动滚到底。角色侧标：「我」／「AI」。

### Added

- **`static/app.js`**：新助手 `askBubble(role,text)`（生成气泡元素，返回 `{wrap, bubble}`）、`askClearChat()`（清空 `#askOutput` 并 `askHistory=[]`）。`handleAsk` 开头加 `if (btn.disabled) return` 防回答中再按 Enter/发送导致重复提交。

### Security / 边界（取舍）

- **全程 `textContent`，不用 `innerHTML`**：用户问题、模型增量、错误文案都以纯文本写入，避免被当作 HTML 注入（XSS）。
- 空问题 / 未解析链路先 `toast` 返回，不建气泡；「清空」与「换链接」（`resetAnalyze` 复用 `askClearChat`）后容器文本为 `""`、`[data-role]` 气泡数为 0。

### Fixed / 状态一致性（对流中清空、换链接、空回复的加固）

> **adversarial review 发现的 3 类真实缺陷**：① 回答问题进行中「清空/换链接」→ 在途 SSE 流仍会跑完，`if (full) askHistory.push(...)` 把旧问答**写回已清空的 `askHistory`**，污染下一次提问上下文（跨视频串问）；② 陈旧 `finally` 会**互相覆盖按钮状态**（`resetAnalyze` 的 `setAnBtns(false)` 曾与 `askClearChat` 的按钮复位打架）；③ 空回复（只 `done` 无 `delta`/`error` 帧）时「…」占位永远不消失。均为**前端竞态**，已在 `handleAsk`/`askClearChat` 修复：

- **`askAbort`（AbortController）**：`handleAsk` 为每次 `/api/ai/ask` 建独立 `AbortController` 并传 `signal`；`askClearChat(resetBtn)` 先 `askGen++` 再 `askAbort.abort()`，取消在途请求，配合令牌使旧流立即失效。
- **`askGen` 会话令牌**：`handleAsk` 记录 `myGen = ++askGen`；`appendDelta`、历史写回、按钮复位均**只在 `myGen === askGen` 时执行** → 清空/换链接（`askGen` 再 ++）后，陈旧流既不写 DOM、也不 `askHistory.push`、更不复位按钮。
- **Abort 静默**：`catch(e)` 对 `e.name === "AbortError"` 不追加错误文案（仅真异常才 `appendDelta`），避免清空后弹「出错」。
- **空回复**：流结束后 `if (!started) aiMsg.bubble.textContent = "(无回答)"`（清掉「…」占位），且不写历史。
- **按钮复位单一职责**：`#askClear` 传 `askClearChat(true)`（复位按钮，允许立即再问）；`resetAnalyze` 调 `askClearChat()`（不复位，交由 `setAnBtns` 统一管控）→ 消除陈旧 `finally` 冲突。

### Verified（真实浏览器端到端回归）

- ✅ `e2e_test.py`：问答气泡 user/assistant 各 ≥1、用户气泡含问题文本、AI 气泡文本 >20；`#askClear` 后容器 `inner_text==""` 且气泡数=0；**中流清空回归**（长问题点 `#askBtn` 后立即 `#askClear`）→ 气泡不复活、`#askBtn.disabled is False`、文案 `发送`，且下一次 `/api/ai/ask` 请求体 `history==[]`（证陈旧流未写回）；换链接后 `#askOutput.innerText==""`；无 `#askStatus`；`#askInput/#askOutput/#askBtn` 均在。其余 5 个 AI 功能（字幕/翻译/摘要/章节/导图）无回归。

## [0.2.5] — 彻底去掉「下载文件」链接 + 批量逐条自动下载（避免并发）（2026-09-08）

> 目标：① 完全删除「下载文件」链接（不再保留兜底）；② 批量队列也改为**自动**下载，且**逐条、串行**执行，避免多任务并发触发浏览器「下载多个文件」授权弹窗。**用 `OvMW1BQFCJc` + `PeNILuH9LL0` 两个视频真实浏览器端到端回归。**

### Removed

- **`static/index.html`**：删除 `#progDownload`（「下载文件」链接）——它在 v0.2.4 被保留作「浏览器拦截自动下载」的兜底，本轮按需求**彻底移除**。
- **`static/app.js`**：删除批量行模板里的 `<a class="dl-link">下载</a>`（批量不再要求用户逐条手动点）。

### Changed

- **`static/app.js`（单条 `onDone`）**：`done` 后状态文案定为「✅ 下载完成，文件已自动保存到本地下载目录」，并调用新增的 `triggerDownload(\`/api/jobs/${j.id}/file\`)` **自动触发**浏览器下载（新建隐藏 `<a href=/file>` + `click()`，服务端 `Content-Disposition: attachment` **流式写盘、零内存**）。
  - **⚠️ 单条直连 `/file` 别立即 `remove()`**：`triggerDownload` 用 `setTimeout(() => a.remove(), 3000)` **延迟移除**隐藏链接——单条是直接导航到 `/file`、**异步流式写盘**，`a.click()` 后立即 `a.remove()` 可能中断进行中的导航下载；与批量 `saveFileViaFetch` 的 `setTimeout(…,4000)` 一致。
- **`static/app.js`（批量逐条自动）**：批量行 `onUpdate` 中 `done` 分支由「仅显示状态」改为「`info` 提示『已自动保存到本地』+ `enqueueBatchDownload(\`/api/jobs/${j.id}/file\`)` 加入自动下载队列」。新增批量下载队列：`batchQueue` + `batchBusy` 互斥，`drainBatchQueue` 每次从队头取一个、`await saveFileViaFetch(href)`（`fetch→Blob→anchor.download`）**取完一条（fetch 完成）才 sleep 0.7s 并取下一条** —— 严格串行，整个批量**同一时刻仅一份 Blob 在内存**，避免并发下载触发「下载多个文件」授权弹窗。`saveFileViaFetch` 从 `Content-Disposition` 解析 `filename*=UTF-8''` / `filename=` 还原真实文件名。

### Added

- **`static/app.js`**：新助手 —— `triggerDownload(href)`（单条流式直连）、`filenameFromCD(cd)`（解析下载头文件名）、`saveFileViaFetch(href)`（Blob 保存）、`enqueueBatchDownload`/`drainBatchQueue` + `batchQueue`/`batchBusy`（批量串行队列）、`sleep(ms)`。
- **`e2e_batch.py`**：新回归脚本 —— 用 `OvMW1BQFCJc`（144p=160）+ `PeNILuH9LL0`（144p=394）各自解析→选 144p→加入批量，等 `#batchList .status-badge` 全部 `=== '已完成'` 后用 `page.on("download", ...)` 监听收集**自动发起**的下载（批量经 fetch→Blob 触发、无 user gesture，不能用 `expect_download` 按 click 捕获，应事件监听轮询），`save_as` 校验各文件非空 + `.mp4`，断言 `.prog-info` 含「已自动保存」、批量队列 2 行、`#progDownload`/`.dl-link` 不存在。

### Verified（真实浏览器端到端回归 · `OvMW1BQFCJc` + `PeNILuH9LL0`）

- ✅ 单条：`e2e_download.py`「点下载→浏览器自动下载」通过，`#progDownload` 计数=0（链接已删）。
- ✅ 回归：`e2e_test.py`（6 AI 功能 + 响应式网格 + 换链接清空 + 缓存 + 导图 + 问答）无回归，无控制台报错。
- ✅ **批量逐条自动**（`e2e_batch.py`）：两视频各选 144p（160/394）加入批量 → 两句 `已完成` → **两条自动下载**均被捕获并保存：`20260129.mp4`（37,804,555B）+ `20260907.mp4`（43,839,936B），均非空 `.mp4`；批量行 `prog-info` 均含「已自动保存到本地」；两条 `/file` 均 `status=200`，页内 `createObjectURL`=2、`<a download>` 点击=2。**串行**（第 1 条完整保存后才触发第 2 条：`download-event #0 已存37.8MB` 先于 `download-event #1`），无并发。

### 关键发现 / 取舍（浏览器「多文件自动下载」策略）

- **Chromium 限制「无用户手势的自动下载」**：只在前次用户交互的短暂激活窗口内允许自动下载；连续第 2 条起若无新手势会被其「正在下载多个文件」策略拦截。故批量逐条自动下载**第 2 条起**在真实浏览器会弹出**一次性**「此网站尝试下载多个文件 → 允许？」授权框（每站点一次，允许后后续整批自动下载不再弹）；无头自动化里该弹窗无法代点，需加 `--enable-automatic-downloads` + `--disable-features=AutomaticDownloadsCheck` 才能放行第 2 条以便测试。
- **为何仍选「fetch→Blob→anchor」而非并发**：真实浏览器第 2 条起其实会被拦截/询问，**逐条**的意义在于不要并发触发下载引擎、且把每次内存占用控制在单个文件（当前每次仅一份 Blob 在内存）——若真多开并发会撞上浏览器连发授权窗。真机用户首个整批需点一次「允许」，之后全自动。
- **测试脚本坑**：连续两条 blob 下载事件，**不要用 `time.sleep` 轮询**地等 `len(downloads)>=2` —— `time.sleep` 阻塞 Python 事件循环，Playwright 派发不了第 2 个 `download` 事件（会一直只见 1 条）。要用 `page.wait_for_function`（内部派发事件）+ 一段 `wait_for_timeout` 收尾；并用 `add_init_script` 包一层 `window.__dlClicks` / `createObjectURL` 计数来佐证 app 确实点了 2 次（`e2e_batch.py`）。
- 另：批量自动下载**顺序**按各 job 完成先后入队，不一定等于批量行顺序（本例 video2 先完成故先下载）——仍是逐条串行，仅顺序自然。
- **测试抖动提示**：同一视频（尤其 `OvMW1BQFCJc`）在短时间内被反复下载可能触发 YouTube 限流，`yt-dlp` 会**偶发一次任务失败**——批量该行 `status-badge` 停在非「已完成」（`e2e_batch.py` 的徽章等待即超时）、单条 `progStatus` 显示「下载失败：请求失败，请稍后再试」。这是**服务端/网络瞬时 flake，非前端回归**，稍候重跑即绿（本轮单条 + 批量最终均通过）。若 e2e 遇到，先 `curl -X POST /api/download` 建任务轮询，排掉「任务自身失败」与「下载事件未触发」这两类不同故障。

## [0.2.4] — 点击「下载」后文件自动保存到本地（无需再点「下载文件」）（2026-09-08）

> 目标：用户点一次「⬇️ 下载」→ 服务端下载完成后**自动**把文件保存到用户本地下载目录，去掉多出来的「点击右侧下载文件」一步。**用给定视频 `OvMW1BQFCJc`（约 36 分钟）真实浏览器端到端回归通过。**

### Changed

- **`static/app.js`（下载 `onDone`）**：原先仅设置 `#progDownload` 链接并提示「点击右侧下载文件」，需用户再点一次。现改为：状态文案变更为「✅ 下载完成，文件已自动保存到本地下载目录」，并在 `onDone(j)` 里用 `link.click()` **自动触发**浏览器下载（等价于点击「下载文件」链接，`Content-Disposition: attachment` 已由 `/api/jobs/{id}/file` 下发）。`onDone` 签名由 `() =>` 改为 `(j) =>`（`pollJob` 本就回调 `d.job`），避免依赖可能已置空的 `currentJobId`。
- **`#progDownload` 保留为兜底**：自动下载被浏览器「自动下载」策略拦截时（极少）仍可点右侧「下载文件」手动重下 —— 链接继续显示，不因自动化而移除。

### Added

- **`e2e_download.py`**：新回归脚本 —— 用 `OvMW1BQFCJc` 解析后选 144p（format_id `160`）压缩体积，点「下载」后以 `page.expect_download` **断言浏览器自动发起了下载**（而非再点「下载文件」），`save_as` 校验文件非空、`suggested_filename` 为 `<标题>.mp4`，并断言 `progStatus` 文案为「已自动保存到本地下载目录」、兜底链接仍可见。`context = new_context(accept_downloads=True)` 开启下载捕获。

### Verified（真实浏览器端到端回归 · `OvMW1BQFCJc`）

- ✅ 解析成功、选中 144p（selected=`160`）；点「下载」后**自动下载已发起**，`suggested_filename='20260907.mp4'`，保存到本地 `auto_download.mp4`，大小 43,839,936B（>0，视频+音频合并后的 mp4）。
- ✅ `progStatus` = 「✅ 下载完成，文件已自动保存到本地下载目录」；兜底链接 `#progDownload` 仍可见（未隐藏）。
- ✅ 服务端 job 走 `/api/download`（`format_id=160`）约 28s 完成（`done 100%`）；无控制台报错。
- ⚠️ 批量队列（`#batchList`）仍保持**每行手动点「下载」**：多个任务完成后若同时自动下载会触发浏览器「正在下载多个文件」授权弹窗，故批量不自动化。

## [0.2.3] — 清晰度/格式选择响应式网格 + 换链接清空上一视频的 AI 内容（2026-09-08）

> 目标：① 清晰度与格式选择在宽屏下不再「一个选项占一整行」，改为**响应式网格**随容器宽度动态排列；② 更换链接解析后，上一视频在 AI 模块里生成的内容（字幕/摘要/章节/导图/问答）应被清空，避免误以为仍是旧视频的产物。**全部经真实浏览器（系统 Chrome）端到端回归通过，无控制台报错。**

### Changed

- **`static/index.html`**：`#formatList` 由 `space-y-2`（每个格式项占一整行、纵向堆叠）改为**响应式网格** —— `grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]`。宽屏自动排成多列（实测 1440px 下 3 列），窄屏/手机自动回落为单列（`auto-fill` + `minmax` 保底）。
- **`static/app.js`**：
  - 新增 `resetAnalyze()`：把 AI 模块里**跨链接**的可见内容与临时状态全部清理 —— `lastSummary`/`askHistory`/`subModeTranslate` 复位、`mmCollapsed`/`mmTree`/`mmFitted` 复位、所有 `.an-panel` 隐藏、`#anError`/`#anLoading` 隐藏、清空 `#subText`/`#subMeta`/`#sumTheme`/`#sumOverview`/`#sumPoints`/`#sumKeywords`/`#sumChapters`/`#mindContainer`/`#askOutput`/`#askInput`、撤掉 `#subDl` 下载 URL。
  - `parseSingle` 开头用 `const changed = activeUrl() !== url` 判断**链接是否真的变化**：变化才 `resetAnalyze()`（换链接清空上一视频内容）；`catch` 失败分支同样 `resetAnalyze()` 再 `showAnalyze(false)`。**注意**：`resetAnalyze` 不删 `featureCache`（缓存本就按 `url` 分键），切回旧链接仍可命中缓存恢复。

### Added

- **`e2e_test.py`**：新增两处断言 —— ① 清晰度/格式网格：`#formatList` 的 `getComputedStyle().display === 'grid'`，且格式项 ≥2 时 `gridTemplateColumns` 解析出的列数 ≥2；② 更换链接：用 `archive.org/details/BigBuckBunny` 二次解析（仅需解析成功、无需字幕），断言旧视频的 `#sumOverview`/`#subText`/`#askOutput` 已清空、`#mindContainer` 无子节点、无 `.an-panel:not(.hidden)` 残留、模块仍显示且标题切换为 `Big Buck Bunny`、旧链接缓存键不丢。

### Verified（真实浏览器端到端回归 · `UISJGnJ1LpA` + `BigBuckBunny`）

- ✅ **响应式网格**：`#formatList` `display=grid`，`gridTemplateColumns` 解析为 `223.328px x3`（1440px 宽屏 3 列，共 10 个格式项）；截图目测每个选项为紧凑卡片，无溢出/换行错乱。
- ✅ **换链接清理**：解析 `BigBuckBunny` 后 `#sumOverview`/`#subText`/`#askOutput` 均为空、`#mindContainer` 子节点数 0、无面板显示；`#analyze` 仍显示（新视频解析成功）、标题切为 `Big Buck Bunny`；缓存键数 `1->1`（旧链接缓存保留）。
- ✅ 既有 6 功能、缓存（再点不重发/重生成重发）、导图拖拽/缩放/折叠、问答 SSE 全部仍通过；无控制台报错。

## [0.2.2] — 模块合并成单框 + 思维导图可交互 + 结果按链接缓存 + 问答分钟级定位（2026-09-08）

> 目标：① 六个功能并入「解析结果」同一卡片，**只保留一个链接输入框**；② 思维导图支持拖拽平移 / 滚轮缩放 / 点击折叠 / 全部展开/折叠；③ 结果按链接缓存，未换链接或刷新前不重算；④ 问答能回答「第几分钟讲了什么」，给出具体时间区间。**全部经真实浏览器（系统 Chrome）端到端回归通过，无控制台报错。**

### Changed

- **`static/index.html`**：把 AI 功能框 `#analyze` 从独立 section **移入 `#resultPanel`（解析结果卡片）内部**，真正合成「解析 + 六个功能」一个模块；删除 `#analyzeUrl` 输入框与 `#useCurrent` 按钮（只留顶部的 `#urlInput`）。为思维导图加工具栏（缩放/重置/全部展开/全部折叠/重新生成），`#mindContainer` 置 `overflow:hidden; height:60vh; cursor:grab`；问答面板重排为**简洁聊天**（头部「对当前视频提问」+ 清空按钮、`#askOutput` 消息区、`#askInput` + `#askBtn` 发送行），移除 `#askStatus` 状态条；各面板补「⟳ 重新生成」按钮（`#subRefresh`/`#sumRefresh`/`#chaptersRefresh`/`#mindRefresh`）与 `#askClear`。
- **`static/app.js`**：
  - `activeUrl()` 改为取 `current.url`（不再读 `#analyzeUrl`）；`showAnalyze`/`syncAnalyze` 相应去掉对 `#analyzeUrl`/`#useCurrent` 的引用。
  - 新增**按「链接 → 功能」缓存** `featureCache`：`getCache`/`setCache`；`handleSubtitle`/`handleSummary`/`handleChapters`/`handleMindmap` 均增加 `force` 参数 —— 命中缓存且未 `force` 时直接渲染、不发请求；点「重新生成」传入 `force=true` 强制重算。`handleSummary`/`handleChapters`/`handleMindmap` 的 `onclick` 改为**包一层箭头函数**（`() => handleSummary()`），否则浏览器会把事件对象当作 `force` 传入（truthy）导致永远重算、缓存形同虚设（这正是首轮缓存测试「再点仍重发请求」的根因）。
  - 问答重写为简洁聊天：去掉 `setAskStatus` 与 `#askStatus` 依赖，改用 `#askBtn` 文案「思考中…/发送」+ 消息区长度判断；新增 `enter` 触发提问、`#askClear` 清空并重置 `askHistory`。
  - 思维导图交互：`bindMindmapInteractions()`（滚轮缩放绕光标 + mousedown 拖拽平移 + 点击节点折叠/展开，`mmDragMoved` 区分拖拽与点击）、`setupMindmapDom` 把 svg 顶层子元素包进 `#mmViewport` 便于整体缩放平移、`toggleFold`/折叠徽标（父节点 +/− 表示）、工具栏 `mmZoomIn/mmZoomOut/mmReset/mmExpandAll/mmCollapseAll`；`window.__mm` 暴露折叠与视图状态（测试/调试钩子）。
- **`app/ai.py`（问答时间定位）**：
  - 新增 `_expand_window` 与 `_retrieve_timeline`：把**逐字幕条**（`[{start,end,text}]`）按问题关键词打分，围绕最相关分段取一段**时间连续、逐行带 `[MM:SS - MM:SS]`** 的窗口（预算 ≤ `AI_CHAT_CONTEXT_CHARS`），预算有余时补窗口外高分片段作为「另一次出现」。关键词全不匹配时退回章节级 `_retrieve_context`（保留原逻辑作兜底）。
  - `generate_answer` 改用 `_retrieve_timeline` 取上下文；`_ask_messages` 系统提示补充：遇到「哪一段/哪几分钟/什么时候」这类**定位题**应依据时间戳回答具体时间段（可合成区间），未覆盖则如实说明、**不编造时间**。
  - 旧 `segment_chapters` 把分段合并进章节块时丢失逐条时间戳、且按整章取 top-3 —— 模型只能凭章节首尾时间猜，很难定位到分钟；改为逐条时间戳后可直接引用。

### Added

- **`static/app.js`**：每面板「⟳ 重新生成」按钮的绑定；思维导图工具栏按钮绑定；`#askClear` 清空逻辑。
- **`e2e_test.py`**：回归用例升级 —— 断言单链接输入框（`#analyzeUrl`/`#useCurrent` 为 0）、解析前/后 `#analyze` 显隐、单飞「再次点击不重发请求 + 点重新生成发一次」（用 `page.on("request")` 计数）、思维导图滚轮放大/拖拽平移/点击折叠/全部展开、问答无 `#askStatus` 且 SSE 流式、无控制台报错。

### Verified（真实浏览器端到端回归 · `UISJGnJ1LpA`）

- ✅ `#analyzeUrl`=0、`#useCurrent`=0、`#urlInput`=1（单一链接输入框）；`#askStatus`=0、`#askInput`/`#askOutput`=1（简洁聊天）。
- ✅ `#analyze` 解析前 `hidden=True`，解析成功 `hidden=False`（同一卡片内）。
- ✅ 六功能全部跑通；思维导图为真实 Mermaid SVG；无控制台报错。
- ✅ **缓存**：摘要再点一次不再发请求（请求数不变）、点「重新生成」恰好多发一次。
- ✅ **导图交互**：滚轮缩放 scale 0.47→0.55；拖拽平移 tx 变化 >5px；点击折叠节点数减少、`window.__mm.collapsed` 非空；全部展开后清空。
- ✅ **问答时间定位**：问「哪几分钟具体讲到对年轻人的影响？」→ 给出 **44:20–44:43** 及子区间（44:20–44:31 讲内需/失业、44:32–44:43 讲年轻人失业率近 20%），可精确定位到分钟。
- ✅ 回归期实例以 `RATE_AI_PER_MIN=100` 启动，避免多 AI 调用被 3/分钟 限流（生产默认 3/分钟 未改）。

## [0.2.1] — 六功能模块"解析后显示" + 前端健壮性修复 + 端到端真机回归（2026-09-08）

> 目标：把「字幕提取 / 字幕翻译 / 摘要生成 / 章节时间轴 / 思维导图 / 问答」统一成一个**只在视频解析成功后才显示**的功能框；并针对前端并发竞态、mermaid 加载竞态、解析失败残留状态做一轮加固。**全部改动经真实浏览器（系统 Chrome / channel="chrome"）端到端回归**，六个功能全部跑通，无控制台报错。

### Changed

- **`static/index.html`**：
  - `#analyze`（六个功能模块）初始 `hidden`，**仅当视频解析成功后才显示**；解析失败整块隐藏并清空上次状态。满足「视频解析后才出现功能框」的需求。
  - 删除标题「🌐 字幕提取 · AI 分析」（用户认为没品），改为仅显示当前视频的 `#anTitle` + 一行简洁提示，整体是一张干净的「功能框」。
  - 补内联 favicon（`data:image/svg+xml` 的 ⚡），消除 `/favicon.ico` 404。
- **`app/security.py`（CSP）**：`script-src` 放行 `https://cdn.jsdelivr.net`（mermaid），`style-src` 放行 `https://fonts.googleapis.com`；否则 mermaid 脑图被 CSP 拦截、预览为空。

### Fixed

- **`app/ai.py`（AI 返回健壮性）**：
  - `_parse_json`：强制返回 `dict`（或 `[dict]`），杜绝裸数组/标量被当 dict 用而 `AttributeError → 500`。
  - `_as_list`：对待 `key_points`/`keywords`/`children` 等**期望为列表**的字段，若 LLM 返回标量则安全转 `${[x for x in 标量]}`，避免 `TypeError → 500`；已应用于 `_short_summary`/`_chapter_json`/`_reduce`/`_markdown`/`derive_mindmap`。
  - `_normalize_nodes`：把思维导图 `children` 递归归一为 `{title, children}` 结构。
  - `_reduce`：新增顶层 `key_points`（`_flatten_key_points` 全篇要点 top-15），供前端作为简洁要点展示。
- **`app/routes.py`（SSE 事件流）**：`/api/ai/ask` 的 `event_stream` 出错时以 `error` 帧终止并 `return`（不再补发 `done` 帧），避免前端用 `done` 覆盖「出错」状态、把失败显示成无回答。
- **`static/app.js`（前端）**：
  - 单飞守卫：并发请求在途时**禁用全部六个按钮**（`startBusy`/`stopBusy` 通过 `setAnBtns`），杜绝「同时点多个按钮 → 共享的状态/结果被互相覆盖」。
  - `handleMindmap`：把 `await renderMindmap(...)`（含 mermaid 异步渲染）移到 `stopBusy()` **之前**，解决「API 返回后 busy 已解除、按钮已恢复，而 SVG 仍在渲染」的竞态（曾导致面板定格在"正在生成导图…"）。
  - `ensureMermaid()`：优先复用 `index.html` `<script type=module>` 已 import 的实例，未就绪时再**懒加载** `cdn.jsdelivr.net`，失败才回退纯 HTML 列表 —— 修复「模块加载慢于点击导致误走降级渲染」的加载竞态。
  - `parseSingle(url)` 成功 → `showAnalyze(true)`；失败 → `showAnalyze(false)`（清 `#analyzeUrl`、`#anTitle`、`current`），避免「重解析失败后仍残留上一个视频的状态、可对旧视频误操作」。
  - `#askOpenBtn` / `#useCurrent` 补 `hideError()`。

### Verified（真实浏览器端到端回归 · `UISJGnJ1LpA`）

- ✅ `#analyze` 解析前 `hidden=True`，解析成功 `hidden=False`。
- ✅ 字幕提取 → 51888 字符；字幕翻译（简中）→ 19118 字符。
- ✅ 摘要生成 → 结构化 `{theme, overview, ...}`；章节时间轴 → 3 条；思维导图 → **真实 Mermaid SVG** 渲染；问答（SSE）→ 完整流式答案（status="完成"，>600 字）。
- ✅ **无任何控制台错误**（CSP / JS 异常 / 404 / SSE 解析均干净）。
- ✅ 说明：回归期临时以 `RATE_AI_PER_MIN=100` 启动测试实例，避免 6 次 AI 调用被 3/分钟 限流（见 PLAN「AI 限流阈值」；默认 3/分钟 为生产防护，未改）。

## [0.2.0] — 学习型结构化摘要（章节·时间轴 / 思维导图 / SSE 问答）（2026-09-08）

> 目标：把 v1 的「纯文本摘要」升级为**学习导向的结构化解说**（对标 BibiGPT / NoteGPT），并补齐「问答」闭环。设计经人工确认（见 PLAN「本期学习型摘要决策」）。后端 + 前端已实现，**端到端真实链路待联调**（本条目如实标注，未跑真实验证）。

### Added

- **`app/downloader.py`**：新增 `subtitle_to_segments(content, fmt) -> [{start, end, text}]` 与 `_parse_timestamp`。与保留时间轴的 `subtitle_to_text`（去时间戳）不同，前者**保留 `start/end`（秒）**，是「章节·时间轴 / 思维导图 / 问答」的根基。
- **`app/config.py`**：新增学习型摘要可调项及默认值 —— `AI_SINGLE_SHOT_CHARS=30000`（短于它单次直出，否则分块）、`AI_CHAPTER_MAX_CHARS=8000`（每章字符预算）、`AI_MAX_CHAPTERS=20`、`AI_MAP_CONCURRENCY=3`、`AI_CHAT_CONTEXT_CHARS=15000`（问答上下文）、`TRANSCRIPT_CACHE_TTL_SECONDS=600`（字幕稿缓存 TTL）。
- **`app/models.py`**：新增 `AskRequest`（`url/question/history`，history 为前端维护的 `[{role,content}]`）。
- **`app/ai.py`（重写核心）**：
  - 分段/时间工具：`fmt_time`、`_segments_lines`、`segment_chapters`（按字符预算切块，≤ `AI_MAX_CHAPTERS`，起止时间取该章首末分段，保证时间轴确定连续）。
  - 结构化摘要 `summarize(segments, meta)`：**短**（≤ 单次预算）→ `_short_summary` 一次直出 `{theme,overview,key_points[],keywords[]}` + 合成一条覆盖全片时间的章节；**长** → `segment_chapters` + `_map_chapters`（每章独立 `_chapter_json`，并发 ≤ `AI_MAP_CONCURRENCY`）+ `_reduce`（汇总各章 → 总 `{theme,overview,keywords[]}`）。
  - `derive_mindmap`：由结构**确定性派生** `{title: theme, children:[{title: 章title, children:[{title:要点}]}]}`，**零额外 LLM 调用**。
  - `_markdown`：由结构渲染全片 Markdown（`# theme / ## 章节 / ### [时间] title / - 要点 / ## 关键词`），供「复制 / 下载 .md」。
  - SSE 问答：`generate_answer(segments, question, history)` 内部用 `segment_chapters` 分章 → `_retrieve_context`（按「问题关键词 × 章节标题/文本」打分取 top-k ≤ `AI_CHAT_CONTEXT_CHARS`）→ `_ask_messages`（system+history 最近6条+question）→ `_chat_stream` 逐 token `AsyncIterator`。新增 `_chat_json`（容忍代码块/前后杂文本的 JSON 解析）与 `_chat_stream`（httpx SSE 流式）。
- **`app/routes.py`**：
  - `_collect_segments_sync(url)`：提取带时间戳分段（优先手动字幕，其次自动），走 `_transcript_cache`（内存、TTL、超 100 丢最旧）复用来避免问答重复提取；返回 `(segments, meta{lang,is_auto,model,used_source})`。
  - 重塑 `POST /api/ai/summary`：返回结构化 `{theme,overview,chapters[],keywords[],mindmap,summary(全文md),lang,is_auto,model,used_source}`。
  - 新增 `POST /api/ai/ask`：SSE 流式问答，原生 `StreamingResponse` + 手写 `data: <json>` 帧推 `{"delta":...}` / `{"error":...}` / `{"done":true}`（不依赖 `fastapi.sse.EventSourceResponse` —— 本版本对其 `ServerSentEvent` 的 `.encode` 处理异常，已规避）。
- **`static/`**（前端）：摘要面板拆成四 tab —— 摘要 / 章节·时间轴 / 思维导图 / 问答；新增「复制 / 下载 .md」；问答用 `fetch` + `ReadableStream` 逐帧解析 SSE（EventSource 仅支持 GET，本端点为 POST）。
  - `index.html`：`#sumTabs` / `#panelSummary` / `#panelChapters` / `#panelMindmap` / `#panelQA` + `#askInput`/`#askBtn`/`#askOutput`。
  - `app.js`：`handleSummary`（结构化渲染）、`renderChapters`（含 `MM:SS - MM:SS` 时间轴）、`renderMindmap`（树）、`handleAsk`（SSE 流式拼接 + `askHistory` 记忆）、`switchSumPanel`、`downloadMarkdown`。
  - `styles.css`：`.sum-tab` / `.tab-active` / `.mind-root` / `.mind-branch` / `.mind-leaf` / `.blink`。

### Changed

- **`app/ai.py`**：`summarize` 签名由 `(text: str)` 改为 `(segments: list[dict], meta: dict)`；保留 `_chat`、`as_http_error`（`_collect_transcript_sync` 保留于 routes 但已不被调用）。> ⚠️ 更正：本轮的 `translate` 曾被**误删**，而 `/api/subtitles` 翻译路径仍在调用它，导致任何带 `target_lang` 的翻译请求 500 —— 已在 Fixed 中**重建修复**。
- **`app/routes.py`**：`/api/ai/summary` 不再用 `_collect_transcript_sync`（纯文本），改用 `_collect_segments_sync`（带时间轴分段）。`.env.example` 补文档化新 AI 项。

### Fixed

- **B 站视频「能提字幕却报『没有字幕』」**（用户实测：`https://www.bilibili.com/video/BV1pGdsB2Ebq/` 经 `/api/subtitles` 返回"成功"，但 `/api/ai/summary` 报 `no_subtitles`）。
  - **真实根因（逐层实证）**：
    - 该视频字幕为 **B 站 AI 字幕**，但 B 站接口对它 `need_login_subtitle=True`（**需登录态才下发**）。项目未配置 B 站 cookie（`COOKIES_FILE` 未设），故在任何阶段（parse/写字幕）B 站**都不返回真实字幕 URL** —— `info['subtitles']`/`automatic_captions`/`requested_subtitles` 三类全空。
    - B 站 extractor 的 `_get_subtitles` 恒返回一个 `danmaku`（弹幕）XML 条目。于是 `/api/subtitles` 的「成功」其实是把**弹幕 XML 当成字幕**返回了（`_finalize` 选最大文件 → 选中巨大的 danmaku.xml；`_ordered_sub_candidates` 回退也拿到 danmaku）。该 XML `subtitle_to_segments` 解析为 **0 段**，`subtitle_to_text` 返回原始 XML 标签 —— 它不是可读字幕稿。
    - 摘要侧 `subtitle_to_segments` 拿到这 0 段 → 触发 `no_subtitles`。**所以摘要报无字幕本身没错**，错的是字幕路径在用弹幕伪装"有字幕"。
  - **修复一（`app/downloader.py` `_subtitles_list`）**：排除 B 站弹幕 `danmaku` 及「纯 xml」字幕源（新增 `_is_xml_only`）。真实 srt/vtt（含 B 站可访问的 AI 字幕 `ai-zh`、YouTube 等）仍照常暴露。
  - **修复二（`app/downloader.py` `extract_subtitle._finalize`）**：选取产出文件时排除 `.xml`（弹幕），只剩 xml 时视为无字幕（返回 None）。避免弹幕 XML 盖掉真正的 srt 字幕。
  - **修复三（`app/routes.py` `_collect_segments_sync`）**：probe 无字幕时回退走 `extract_subtitle(url, "", False, out_dir)`（**手动桶** + 不限语言，内部逐条回退），而非上版的自动桶（`is_auto=True` 对 B 站写不出东西，是错的）。`meta` 改为从 `result` 反推 `used_source`/`is_auto`，`lang` 空时回退 `"zh"`。
  - **验证（确定性打桩）**：① `_subtitles_list` 排除 danmaku/xml、保留 `['ai-zh','en','zh-CN']`；② `extract_subtitle` 同目录产出 srt+弹幕xml 时返回 **srt**（非最大xml），`subtitle_to_segments` 解析 1 段；③ routes 分支 A「probe有字幕→手动优先」/ B「probe无字幕→回退手动桶」/ C「回退失败→no_subtitles」3/3；④ 真实 B 站 `BV1pGdsB2Ebq` → 诚实 `no_subtitles`（不再返回弹幕 XML 垃圾）。
  - **⭐ 正确行为说明**：该视频字幕**需要 B 站登录 cookie** 才能真正拿到。**项目配置 `COOKIES_FILE`（B 站 netscape cookie.txt）后**，`_get_subtitles` 才会收到真实字幕 URL，`/api/subtitles` 与 `/api/ai/summary` 即可正常产出可读字稿与摘要；未配置 cookie 时，此类 `need_login_subtitle` 视频**正确、诚实**地返回 `no_subtitles`（而非用弹幕伪装成有字幕）。
  - **修复四（`app/downloader.py` `bilibili_subtitle_login_hint` + `_is_bilibili`）**：把上述 `no_subtitles` 的 `error` 从生硬的「该视频暂无可用字幕」升级为**准确、可操作**的引导语 —— 「B 站该视频确实带字幕，但字幕需登录态才下发；请按 .env 的 COOKIES_FILE 填入 B 站登录 cookie（Netscape 格式，宜含 SESSDATA）后重试」。仅在 `need_login_subtitle=True`（B 站）时追加；**非 B 站 / 真无字幕（`need_login_subtitle=False`）恒为空**，不误报，且非 B 站 URL 不发起任何额外网络请求（`_is_bilibili` 先短路）。
  - **修复五（`app/ai.py` 重建 `translate`）**：v0.2 重写 `ai.py` 时**误删**了 `translate`，而 `/api/subtitles` 翻译路径（routes.py:215 `ai.translate(text, req.target_lang)`）仍在调用 → 任何带 `target_lang` 的翻译请求 `AttributeError` → HTTP 500（`unknown`）。已按调用签名 `translate(text, target_lang)` 用 `_chat` 重建：超过 `AI_MAX_CHARS` 自动分块逐块翻译后按行拼接；某块失败即整体如实抛 `LLMError`（路由映射 `llm` 502）。

### Verified（确定性 / 打桩（mock）验证）

- ✅ 导入自检：`downloader.subtitle_to_segments`、`ai.summarize/generate_answer/segment_chapters/derive_mindmap`、`routes.summary/ask/_collect_segments_sync` 均连通。
- ✅ **单次直出分支**（打桩 `_chat_json`）：`summarize` 返回结构化 `{theme,overview,chapters[],keywords[],mindmap,summary}`，章节含 `start/end/title/summary/key_points/keywords`，Mindmap 根=theme、一级=章节、二级=要点；`_markdown` 渲染正常。
- ✅ **map-reduce 分支**（打桩 `_chat_json` + 收紧 `AI_SINGLE_SHOT_CHARS`/`AI_CHAPTER_MAX_CHARS`）：25 段 → 5 章节，时间轴连续（首章 `start=0`、末章 `end=最后分段`），调用次数 = 章节数 + 1（reduce）。
- ✅ **B 站登录引导语（本日实测）**：`BV1mAAmzqEfP`（`need_login_subtitle=True`）经真实 HTTP 链路 `/api/ai/summary` 与 `/api/subtitles` 均返回 `no_subtitles` 且 `error` 带登录引导语（「确有字幕但需登录态，请配置 COOKIES_FILE/SESSDATA」）；`BV1XXXXXXXX`（`need_login_subtitle=False`）与非 B 站 URL 恒无提示（mock 打桩，非 B 站 0 网络请求）。
- ✅ **摘要管线可用性（本日实测）**：喂样本字幕分段 → `ai.summarize` 真实 LLM 产出完整结构化 `{theme,overview,chapters[],keywords[],mindmap,summary,used_source=manual}`。
- ✅ **YouTube 端到端全链路（本日实测 `UISJGnJ1LpA`，真实用户路径）**：`/api/subtitles`（zh-CN 手动 SRT 51890 字符）✅ → `/api/subtitles`+`target_lang` 翻译（简体中文 18152 字符）✅ → `/api/ai/summary`（结构化摘要，单次直出分支，chapters=1 覆盖 0~3772s）✅ → `/api/ai/ask`（SSE 流式 975 字答案）✅。期间**发现并修复 1 个真 bug**：翻译路径 500（`ai.translate` 被 v0.2 误删，见 Fixed 修复五）。
- ✅ **翻译修复回归（本日实测）**：`UISJGnJ1LpA` 带 `target_lang=简体中文` → HTTP 200，`translated_len=18152`，译文正确。
- ⬜ **真实链路（待办）**：取到真实公网字幕后的 摘要 → 问答、LLM 未配置/超时、长视频 map-reduce 效果回归，尚未实测。

### 关键说明 / 取舍

- **为何保留时间轴分段而非纯文本**：章节的 `start/end` 需确定值；若让 LLM 自己给时间，单/短视频直接可用，但长视频分块后时间必须来自后端切分，否则不可复现。故统一由 `subtitle_to_segments` 提供时间轴。
- **单次 vs map-reduce 分档**：短视频一次直出最省钱；超阈值走 map-reduce（O(章节数) 次调用），长视频无损且能产出真实时间轴。
- **Mindmap 确定性派生**：不额外消耗 LLM 调用，稳定可复现，代价是仅受各章标题/要点的质量约束。
- **问答与摘要解耦**：问答前不一定调过摘要，故 `generate_answer` 内部自用 `segment_chapters` 分章做检索上下文。
- **缓存**：`_transcript_cache` 内存短时、TTL 过期、超限丢最旧，只为避免问答重复走 yt-dlp 提取；不落盘，重启即清。

---

## [0.1.2] — 抖音视频下载（服务端无头浏览器）（2026-09-07）

> 目标：在不要求用户提供 Cookie、不使用浏览器插件/油猴、不自造轮子的前提下，让抖音视频可解析并下载。端到端自测验证：解析→下载→成品 MP4。

### 背景（「为什么这么做」）

抖音 Web API 被 `a_bogus` + `msToken` 签名墙覆盖，且 `a_bogus` **绑定浏览器环境指纹**（UA/版本/设备参数须一致）。三条「免 Cookie 直连」路线均排除：
- **yt-dlp 直跑**：`DouyinIE` 里只有 `# TODO: Run verification challenge code to generate signature cookies`，无签名器 → 裸请求被拒。
- **纯 Python 签名器（如 f2）**：无法与真实浏览器指纹一致 → 直连一律 403。
- **免 Cookie SSR 直连**（share 页 → uri → `aweme.snssdk.com/v1/play`）：已于 **2026-08-30** 关闭，返回空壳页。

**唯一可行路线**：服务器内置无头浏览器，用浏览器的**匿名游客会话**加载抖音页 → 页面 JS 现场算 `a_bogus` → 只拦截它对 `/aweme/v1/web/aweme/detail` 的**真实响应**，从中取播放地址。满足全部硬约束：不需用户 Cookie、不需用户端插件、不自造逆向。

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
- **tasks.py**：内存 job store（dict + uuid4 + `threading.Lock`）+ `ThreadPoolExecutor`（下载线程池）+ `MAX_ACTIVE_JOBS=100` 总量上限（`active_or_queued_count()`）+ 后台 TTL 清理循环。
- **ai.py**：OpenAI 兼容 LLM 客户端 `summarize` / `translate`（httpx POST `/chat/completions`），错误映射。
- **routes.py**：`/api/parse`、`/api/download`、`/api/download/batch`、`/api/jobs/{id}`、`/api/jobs/{id}/file`（流式）、`DELETE /api/jobs/{id}`、`/api/subtitles`、`/api/ai/summary`，全部走限流 + 错误映射。
- **前端单页** `static/`：`index.html`（Hero「一条链接，下遍全网」/ 信任背书 / 功能卡网格 / 结果面板(多格式多选) / 批量队列面板 / 字幕面板 / AI 摘要面板 / 定价+PRO 对比卡 / 升级 CTA 横幅 / FAQ / Footer 免责 + PRO 弹窗）+ `app.js`（解析/渲染/批量轮询/进度条/字幕/摘要/动效/toast）+ `styles.css`（马卡龙芯片、hover 上浮、骨架屏、动效）。
- **文档沉淀** `docs/`：`PLAN.md`（总方案）、`DESIGN.md`（架构详解）、`API.md`（接口契约）、`SECURITY.md`（安全清单）、`CHANGELOG.md`（本文件）。*（后精简：OVERVIEW/ROADMAP 并入 PLAN）*
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

## 操作提醒

- 本文件只记录**已发生**的历史。下一步待办与里程碑状态见 [PLAN.md](PLAN.md) 的「下一步」。
- 若此前 uvicorn 仍以旧进程运行，需重启以使 `security.py` 的 `InvalidURL` 优先判断生效（逻辑已改，旧进程未加载）。
