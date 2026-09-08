/* 前端逻辑：解析 → 渲染结果 / 批量队列 / 字幕 / AI 摘要 / 动效 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------- 全局状态 ----------
  let current = null; // {url, formats, selected}
  let polling = new Map(); // jobId -> interval
  let lastSummary = ""; // 当前摘要的 Markdown 全文（复制/下载用）
  let lastChapters = null; // 当前章节·时间轴数据（下载用）
  let askHistory = []; // AI 问答 [{role, content}]
  let askGen = 0;     // 问答会话令牌：提问/清空/换链接递增，用于使在途 SSE 流失效
  let askAbort = null; // 当前提问的 AbortController（清空/换链接时 abort，避免陈旧流写回）
  // 按「链接 → 功能」缓存各功能结果：链接不变则结果不变；点「重新生成」才强制重算。
  let featureCache = {}; // url -> { [feature]: data }
  let subModeTranslate = false; // 最近一次字幕操作是否为「翻译」，供「重新生成」按同一模式重算

  // ---------- 工具 ----------
  async function api(path, body) {
    const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok) throw new Error(data.error || "请求失败，请稍后再试");
    return data;
  }

  function toast(msg, ms = 2600) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), ms);
  }

  function humanBytes(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 100 ? 0 : 1) + u[i];
  }

  function humanSec(sec) {
    if (!sec) return "";
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function showResult(show) {
    const p = $("#resultPanel");
    p.classList.toggle("hidden", !show);
    if (show) p.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function showBatch(show) { $("#batchPanel").classList.toggle("hidden", !show); }

  async function pollJob(jobId, { onUpdate, onDone, onError } = {}) {
    try {
      const d = await api(`/api/jobs/${jobId}`);
      onUpdate && onUpdate(d.job);
      const st = d.job.status;
      if (st === "done") { stopPoll(jobId); onDone && onDone(d.job); }
      else if (st === "error" || st === "closed") { stopPoll(jobId); onError && onError(d.job); }
    } catch (e) {
      // 404/过期
      stopPoll(jobId);
      onError && onError({ status: "error", error: e.message });
    }
  }

  function stopPoll(jobId) {
    if (polling.has(jobId)) { clearInterval(polling.get(jobId)); polling.delete(jobId); }
  }

  function startPoll(jobId, cb) {
    stopPoll(jobId);
    const iv = setInterval(() => pollJob(jobId, cb), 1100);
    polling.set(jobId, iv);
  }

  // ---------- 解析 ----------
  async function handleParse() {
    const raw = $("#urlInput").value.trim();
    if (!raw) { toast("请先粘贴视频链接"); return; }
    const urls = [...new Set(raw.split(/[\n,;\s]+/).filter((u) => /^https?:\/\//i.test(u)))];
    if (!urls.length) { toast("未识别到有效链接"); return; }
    if (urls.length === 1) await parseSingle(urls[0]);
    else await submitBatch(urls.map((u) => ({ url: u })));
  }

  async function parseSingle(url) {
    const changed = activeUrl() !== url; // 新链接是否不同于当前已加载链接
    showResult(true);
    if (changed) resetAnalyze(); // 换链接：先清空上一视频在 AI 模块里留下的内容与状态
    $("#dlTitle").textContent = "解析中…";
    $("#dlMeta").textContent = "正在获取视频信息与清晰度…";
    $("#thumb").classList.add("hidden");
    $("#formatList").innerHTML = `<div class="skeleton h-12 rounded-lg"></div><div class="skeleton h-12 rounded-lg"></div>`;
    $("#downloadProgress").classList.add("hidden");
    try {
      const data = await api("/api/parse", { url });
      current = { url, formats: data.formats || [], selected: null, meta: data };
      renderResult(data);
      showAnalyze(true); // 视频解析成功后才显示「字幕提取·AI分析」模块
      syncAnalyze(); // 解析成功后自动带入统一分析模块
      maybeAutoSummary(url); // 若开启「解析后自动生成摘要」→ 无需再点一次
    } catch (e) {
      resetAnalyze(); // 解析失败同样清理，避免旧内容被残留
      showAnalyze(false); // 整块隐藏并清空上次状态，避免对旧视频误操作
      showResult(false);
      toast(e.message);
    }
  }

  function renderResult(data) {
    $("#dlTitle").textContent = data.title || "未命名视频";
    // 标题最多 3 行显示；完整标题写入 title 属性，鼠标悬停可见
    $("#dlTitle").title = data.title || "";
    $("#dlMeta").textContent = `${data.extractor || "通用视频"} · ${humanSec(data.duration) || "未知时长"}`;
    const extBadges = { ArchiveOrg: "Archive 视频", YouTube: "YouTube", BiliBili: "哔哩哔哩", TikTok: "TikTok", Generic: "通用视频" };
    $("#extBadge").textContent = extBadges[data.extractor] || "通用视频";
    renderDesc(data.description); // 左栏视频描述（后端 /api/parse 新增字段）

    if (data.thumbnail) {
      const t = $("#thumb");
      // 走服务端代理，规避 CDN 防盗链 / http 混合内容被拦
      t.src = `/api/thumbnail?url=${encodeURIComponent(data.thumbnail)}`;
      t.classList.remove("hidden");
    }

    const list = $("#formatList");
    list.innerHTML = "";
    const fmts = data.formats || [];
    if (!fmts.length) {
      // 无多格式信息 → 默认最佳
      current.selected = null;
      list.appendChild(formatRow({ height: "", resolution: "默认最佳画质", ext: data.ffmpeg ? "mp4" : "mp4", filesize: 0, needs_merge: false, progressive: true }, true));
    } else {
      fmts.forEach((f, i) => { if (i === 0) current.selected = f.format_id; list.appendChild(formatRow(f, i === 0)); });
    }
  }

  // 左栏视频描述：textContent 渲染（防注入），空则隐藏；>120 字提供「展开/收起」
  function renderDesc(text) {
    const wrap = $("#dlDesc"), body = $("#dlDescText"), btn = $("#dlDescToggle");
    if (!wrap || !body || !btn) return;
    const t = (text || "").replace(/\s+/g, " ").trim();
    if (!t) { wrap.classList.add("hidden"); return; }
    body.textContent = t;
    body.classList.remove("open");
    wrap.classList.remove("hidden");
    const long = t.length > 120;
    btn.classList.toggle("hidden", !long);
    btn.textContent = "展开";
  }

  function formatRow(f, checked) {
    const div = document.createElement("div");
    div.className = "group flex items-center justify-between rounded-xl border px-4 py-3 cursor-pointer transition " +
      (checked ? "border-brand bg-brand-soft" : "border-slate-200 hover:border-brand/50");
    div.innerHTML = `
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-4 h-4 rounded-full border-2 ${checked ? "border-brand bg-brand" : "border-slate-300"} flex-none"></div>
        <div class="min-w-0">
          <div class="font-semibold text-sm text-slate-800">${f.resolution || "默认"}</div>
          <div class="text-xs text-slate-400 truncate">${(f.ext || "").toUpperCase()}${f.filesize ? " · " + humanBytes(f.filesize) : ""}</div>
        </div>
      </div>`;
    div.onclick = () => {
      current.selected = f.format_id;
      listRows(); // 更新选中态
      toast(`已选 ${f.resolution || "最佳画质"}`);
    };
    div._fid = f.format_id;
    return div;
  }

  function listRows() {
    $$("#formatList > div").forEach((d) => {
      const on = current.selected === d._fid;
      d.className = "group flex items-center justify-between rounded-xl border px-4 py-3 cursor-pointer transition " +
        (on ? "border-brand bg-brand-soft" : "border-slate-200 hover:border-brand/50");
      const dot = d.querySelector("div.flex.items-center.gap-3 > div");
      dot.className = "w-4 h-4 rounded-full border-2 " + (on ? "border-brand bg-brand" : "border-slate-300");
    });
  }

  // ---------- 下载（单条，带进度） ----------
  let currentJobId = null;
  async function handleDownload() {
    if (!current || !current.url) { toast("请先解析"); return; }
    const url = current.url, format_id = current.selected || null;
    $("#downloadProgress").classList.remove("hidden");
    try {
      const d = await api("/api/download", { url, format_id });
      currentJobId = d.job_id;
      $("#progStatus").textContent = "开始下载…";
      $("#progPct").textContent = "0%";
      $("#progBar").style.width = "0%";
      $("#progSpeed").textContent = "";
      startPoll(d.job_id, {
        onUpdate: (j) => {
          $("#progStatus").textContent = statusZh(j.status);
          $("#progPct").textContent = Math.round(j.progress) + "%";
          $("#progBar").style.width = j.progress + "%";
          $("#progSpeed").textContent = j.speed ? j.speed + " · " + humanBytes(j.downloaded_bytes) : humanBytes(j.downloaded_bytes);
          if (j.status === "error") {
            $("#progStatus").textContent = "下载失败：" + (j.error || "未知原因");
          }
        },
        onDone: (j) => {
          $("#progStatus").textContent = "✅ 下载完成，文件已自动保存到本地下载目录";
          toast("下载完成，已保存到本地");
          triggerDownload(`/api/jobs/${j.id}/file`); // 直连流式写盘，无需再点一次
          currentJobId = null;
        },
        onError: (j) => { $("#progStatus").textContent = "下载失败：" + (j.error || "请重试"); },
      });
    } catch (e) { toast(e.message); }
  }

  // ---------- 批量 ----------
  async function submitBatch(items) {
    showBatch(true);
    try {
      const d = await api("/api/download/batch", { items });
      d.jobs.forEach((job, i) => {
        if (job.job_id) addBatchRow(items[i] ? items[i].url : job.url, job.job_id);
        else addBatchRow(job.url, null, job.error);
      });
      toast(`已加入 ${d.jobs.filter((j) => j.job_id).length} 个任务`);
    } catch (e) { toast(e.message); }
  }

  function addBatchRow(url, jobId, initError) {
    const list = $("#batchList");
    const row = document.createElement("div");
    row.className = "rounded-xl border border-slate-200 p-3";
    row.innerHTML = `
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0 text-sm font-medium text-slate-700 truncate">${url}</div>
        <span class="status-badge text-xs font-semibold px-2 py-1 rounded-full shrink-0">排队</span>
      </div>
      <div class="h-2 rounded-full bg-slate-100 mt-2 overflow-hidden"><div class="prog-bar h-full bg-brand" style="width:0%"></div></div>
      <div class="flex items-center justify-between mt-1 text-xs text-slate-400"><span class="prog-info"></span></div>`;
    list.appendChild(row);
    if (initError) { row.querySelector(".status-badge").textContent = "失败"; row.querySelector(".status-badge").classList.add("bg-rose-100", "text-rose-600"); }
    if (!jobId) return;
    startPoll(jobId, {
      onUpdate: (j) => {
        const badge = row.querySelector(".status-badge");
        badge.textContent = statusZh(j.status);
        badge.className = "status-badge text-xs font-semibold px-2 py-1 rounded-full shrink-0 " + statusColor(j.status);
        row.querySelector(".prog-bar").style.width = (j.progress || 0) + "%";
        const info = row.querySelector(".prog-info");
        if (j.status === "done") {
          info.textContent = (j.title ? j.title.slice(0, 40) + " · " : "") + "已自动保存到本地";
          enqueueBatchDownload(`/api/jobs/${j.id}/file`); // 逐条自动下载，避免并发触发「下载多个文件」弹窗
        } else {
          info.textContent = (j.title ? j.title.slice(0, 40) + " · " : "") + (j.speed || "") + " " + humanBytes(j.downloaded_bytes);
        }
        if (j.status === "error") info.textContent = (j.error || "失败");
      },
      onError: (j) => {
        const badge = row.querySelector(".status-badge");
        badge.textContent = "失败";
        badge.className = "status-badge text-xs font-semibold px-2 py-1 rounded-full shrink-0 bg-rose-100 text-rose-600";
        row.querySelector(".prog-info").textContent = (j.error || "下载失败");
      },
    });
  }

  // ---------- 自动下载：直连流式（单条） + fetch/Blob（批量逐条） ----------
  // 单条：直接新建隐藏 <a href=/file> 点击，浏览器经 Content-Disposition:attachment 流式写盘，零内存占用。
  function triggerDownload(href) {
    const a = document.createElement("a");
    a.href = href;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    // 稍候移除：导航到 /file 的下载是异步完成的，立即 remove 可能中断导航下载；
    // 与批量 saveFileViaFetch 一样，暂时保留隐藏元素、给浏览器一点时间再清理。
    setTimeout(() => a.remove(), 3000);
  }

  // 从 Content-Disposition 解析 filename（支持 `filename*=UTF-8''…` 与 `filename="…"`）
  function filenameFromCD(cd) {
    if (!cd) return "";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) { try { return decodeURIComponent(star[1].trim()); } catch (_) {} }
    const plain = cd.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1].trim() : "";
  }
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // 批量逐条：一次只下载一个（fetch→Blob→anchor 点击），取完一个再取下一个，
  // 避免多个任务完成时同时触发浏览器下载而弹「正在下载多个文件」授权窗。
  const batchQueue = [];
  let batchBusy = false;
  function enqueueBatchDownload(href) {
    batchQueue.push(href);
    drainBatchQueue();
  }
  async function drainBatchQueue() {
    if (batchBusy || !batchQueue.length) return;
    batchBusy = true;
    try {
      while (batchQueue.length) {
        const href = batchQueue.shift();
        try { await saveFileViaFetch(href); await sleep(700); } // 稍候写盘，再触发下一条
        catch (e) { console.warn("批量自动下载失败:", href, e); }
      }
    } finally {
      batchBusy = false;
    }
  }
  async function saveFileViaFetch(href) {
    const resp = await fetch(href);
    if (!resp.ok) throw new Error("下载失败");
    const blob = await resp.blob();
    const name = filenameFromCD(resp.headers.get("Content-Disposition")) || "download";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 4000);
  }

  // ---------- 状态文案 & 颜色 ----------
  function statusZh(s) {
    return { queued: "排队", probing: "解析中", downloading: "下载中", done: "已完成", error: "失败", closed: "已取消" }[s] || s;
  }
  function statusColor(s) {
    if (s === "downloading") return "bg-brand-soft text-brand";
    if (s === "done") return "bg-emerald-100 text-emerald-700";
    if (s === "error" || s === "closed") return "bg-rose-100 text-rose-600";
    return "bg-slate-100 text-slate-500";
  }

  // ---------- 统一输入：字幕 ----------
  // 按「链接 → 功能」缓存结果；链接不变则结果不变，点「重新生成」才强制重算。
  function getCache(url, key) { return (featureCache[url] || {})[key] || null; }
  function setCache(url, key, data) { featureCache[url] = featureCache[url] || {}; featureCache[url][key] = data; }
  function renderSubtitleData(url, key, d, translate, targetLang) {
    // 模式标签：让用户在同一个字幕面板里一眼看清当前是「原字幕」还是「已翻译为：X」
    $("#subModeLabel").textContent = translate ? `已翻译为：${targetLang}` : "原字幕";
    $("#subMeta").textContent = `${(d.lang || "") || ""} · ${d.source === "manual" ? "内建字幕" : "自动字幕"} · ${(d.format || "").toUpperCase()}`;
    $("#subText").textContent = d.translated || d.content || "（空）";
    makeSubDownload(d, translate, targetLang);
    subModeTranslate = translate;
    if (translate && targetLang) $("#subLangSel").value = targetLang;
    setCache(url, key, d);
  }
  async function handleSubtitle(translate, force) {
    const url = activeUrl();
    if (!url) { toast("请先解析视频链接"); return; }
    hideError();
    selectTab("subBtn");
    const targetLang = translate ? ($("#subLangSel").value || "简体中文") : "";
    const key = translate ? "subTranslate:" + targetLang : "subtitle";
    const c = getCache(url, key);
    if (c && !force) { renderSubtitleData(url, key, c, translate, targetLang); return; } // 命中缓存而不再请求
    const box = $("#subText");
    box.innerHTML = skelHTML(4); // 骨架屏占位，替代冗余的「正在提取字幕…」句子
    startBusy(translate ? "正在翻译字幕…" : "正在提取字幕…");
    try {
      const body = { url, lang: "zh", is_auto: false };
      if (translate) body.target_lang = targetLang;
      const d = await api("/api/subtitles", body);
      renderSubtitleData(url, key, d, translate, targetLang);
      stopBusy();
      toast(translate ? `已翻译成${targetLang}` : "字幕已提取");
    } catch (e) {
      stopBusy();
      box.textContent = e.message;
      $("#subMeta").textContent = "";
      $("#subDl").classList.add("hidden"); // 出错时不保留旧字幕可下载
      showError(e.message);
    }
  }
  function makeSubDownload(d, isTrans, targetLang) {
    // 用 Blob 而非 data: URI，防止部分浏览器拦截 data: 下载
    const content = (d.translated || d.content) || "";
    const ext = isTrans ? "txt" : d.format || "srt";
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const link = $("#subDl");
    if (link._url) URL.revokeObjectURL(link._url);
    link._url = URL.createObjectURL(blob);
    link.href = link._url;
    const fname = isTrans ? `字幕-${(targetLang || "译文")}.${ext}` : `字幕.${ext}`;
    link.setAttribute("download", fname);
    link.textContent = isTrans ? `下载 TXT（${targetLang || "译文"}）` : `下载 ${ext.toUpperCase()}`; // 明确告诉用户当前下的是什么
    link.classList.remove("hidden"); // 有内容即揭示下载入口（此前恒 hidden，下载功能形同虚设）
  }

  // ---------- 统一分析模块：各面板独立调用 AI ----------
  function activeUrl() { return (current && current.url) ? current.url : ""; }

  function showPanel(name) {
    $$(".an-panel").forEach((p) => p.classList.add("hidden"));
    const el = $("#" + name);
    if (el) { el.classList.remove("hidden"); el.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  }
  // Tab 按钮 id → 面板 id；selectTab 负责高亮 + 显隐（默认「摘要」）
  const TAB_PANEL = { sumBtn: "panelSum", chaptersBtn: "panelChap", mindmapBtn: "panelMind", subBtn: "panelSub", askOpenBtn: "panelAsk" };
  function selectTab(btnId) {
    $$(".ai-tab").forEach((b) => b.classList.toggle("ai-tab-active", b.id === btnId));
    const empty = $("#anEmpty");
    if (empty) empty.classList.add("hidden"); // 选中功能后隐藏空态占位
    showPanel(TAB_PANEL[btnId]);
  }
  const AN_BTNS = ["subBtn", "subTranslateBtn", "sumBtn", "chaptersBtn", "mindmapBtn", "askBtn"];
  function setAnBtns(disabled) {
    AN_BTNS.forEach((id) => { const el = $("#" + id); if (el) el.disabled = disabled; });
  }
  // 骨架屏：几段带流光的灰条，占位表示「AI 正在这里生成」，代替冗余的「正在生成…」占位句子
  function skelHTML(rows) {
    const widths = ["100%", "88%", "72%", "94%"];
    let h = "";
    for (let i = 0; i < rows; i++) h += `<div class="skel" style="width:${widths[i % widths.length]}"></div>`;
    return h;
  }

  // 单飞守卫：任一 AI 请求进行中时禁用全部功能按钮，避免并发请求交叉清空共享的
  // #anLoading / #anError / toast 状态（原先可同时点多个按钮导致状态互相覆盖）。
  function startBusy(msg) {
    $("#anLoadingText").textContent = msg || "正在处理…";
    $("#anLoading").classList.remove("hidden");
    setAnBtns(true);
  }
  function stopBusy() {
    $("#anLoading").classList.add("hidden");
    setAnBtns(false);
  }
  function showError(msg) { const e = $("#anError"); e.textContent = msg; e.classList.remove("hidden"); }
  function hideError() { $("#anError").classList.add("hidden"); }
  // 「字幕提取·AI分析」模块只在视频解析成功后出现；解析失败则整块隐藏并清空上次状态，
  // 避免对旧视频误操作。
  function showAnalyze(on) {
    const sec = $("#analyze");
    if (!sec) return;
    sec.hidden = !on; // 与 index.html 里的 hidden 属性一致，避免 class/attribute 互不相干
    if (!on) {
      $("#anTitle").textContent = "未解析视频";
      current = null;
    }
  }
  function syncAnalyze() {
    if (current && current.url) {
      $("#anTitle").textContent = (current.meta && current.meta.title) || "已解析视频";
    }
  }

  // ---------- 自动摘要：开关默认关（localStorage 持久化），开启后解析成功即自动生成 ----------
  const AUTO_SUM_KEY = "vdl_auto_summary";
  function autoSummaryOn() {
    try { return localStorage.getItem(AUTO_SUM_KEY) === "1"; } catch (_) { return false; }
  }
  function setAutoSummary(on) {
    try { localStorage.setItem(AUTO_SUM_KEY, on ? "1" : "0"); } catch (_) {}
  }
  // 解析成功后调用：开关开 + 当前无 AI 任务在跑 → 命中缓存直接渲染，否则自动调 /api/ai/summary
  async function maybeAutoSummary(url) {
    if (!url || !autoSummaryOn()) return;
    const loading = $("#anLoading");
    if (loading && !loading.classList.contains("hidden")) return; // 已有任务在跑，跳过本次自动
    selectTab("sumBtn");
    const c = getCache(url, "summary");
    if (c && c.data) { renderSummary(c.data); lastSummary = c.summary || ""; return; }
    await handleSummary();
  }

  // 换链接后：把 AI 模块里的可见内容与跨链接的临时状态全部清理，避免看到上一个视频的产物。
  // 注意：不删 featureCache（缓存按 url 分键），切回旧链接仍能命中缓存恢复。
  function resetAnalyze() {
    lastSummary = "";
    lastChapters = null;
    askHistory = [];
    subModeTranslate = false;
    mmCollapsed.clear();
    mmTree = null;
    mmFitted = false;
    $$(".an-panel").forEach((p) => p.classList.add("hidden"));
    $$(".ai-tab").forEach((b) => b.classList.remove("ai-tab-active")); // 换链接/失败后回到「未选中」态
    const empty = $("#anEmpty");
    if (empty) empty.classList.remove("hidden"); // 恢复空态占位
    hideError();
    $("#anLoading").classList.add("hidden");
    setAnBtns(false);
    $("#subText").textContent = "";
    $("#subMeta").textContent = "";
    $("#subModeLabel").textContent = "";
    $("#subDl").classList.add("hidden");
    if ($("#subDl")._url) { URL.revokeObjectURL($("#subDl")._url); delete $("#subDl")._url; }
    $("#sumMd").textContent = "";
    $("#sumChapters").innerHTML = "";
    $("#mindContainer").innerHTML = "";
    askClearChat();
    $("#askInput").value = "";
  }

  function fmtSec(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
             : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function esc(t) {
    return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // 摘要（v3：Markdown 排版渲染，后端 summary 为全文 Markdown，先消毒再注入）
  function renderSummary(d) {
    const raw = (d.summary || "").trim()
      ? d.summary
      : (d.theme || "") + "\n\n" + (d.overview || "") + "\n\n" + (d.key_points || []).map((p) => "- " + p).join("\n");
    lastSummary = raw;
    const md = $("#sumMd");
    if (window.marked && window.DOMPurify) {
      md.innerHTML = window.DOMPurify.sanitize(window.marked.parse(raw));
    } else {
      // 极端 CDN 失败兜底：纯文本，用 console.warn 不抛 console.error（e2e 有「无 console error」门禁）
      md.textContent = raw;
      console.warn("marked/DOMPurify 未加载，摘要以纯文本显示");
    }
  }

  // 章节·时间轴（独立生成）
  function renderChapters(chapters) {
    const box = $("#sumChapters");
    box.innerHTML = "";
    lastChapters = chapters || []; // 记录供「下载 .md」使用
    if (!chapters || !chapters.length) {
      box.innerHTML = `<p class="text-xs text-slate-400">该视频较短，暂未细分章节。</p>`;
      return;
    }
    chapters.forEach((c) => {
      const pts = (c.key_points || []).map((p) => `<li class="text-slate-600">${esc(p)}</li>`).join("");
      const div = document.createElement("div");
      div.className = "rounded-lg border border-slate-200 p-3";
      div.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 shrink-0">${esc(fmtSec(c.start))} - ${esc(fmtSec(c.end))}</span>
          <span class="text-sm font-semibold text-slate-900">${esc(c.title)}</span>
        </div>
        <div class="text-sm text-slate-600">${esc(c.summary || "")}</div>
        ${pts ? `<ul class="mt-2 space-y-0.5 text-xs list-disc list-inside">${pts}</ul>` : ""}`;
      box.appendChild(div);
    });
  }

  // ============ 思维导图 → Mermaid 脑图 + 拖拽/缩放/折叠展开 ============
  const MM_NS = "http://www.w3.org/2000/svg";
  let mmTree = null;             // 当前导图的源树（用于折叠/重绘）
  let mmCollapsed = new Set();   // 已折叠节点的路径键，如 "0.0"、"0.1.2"
  let mmIndex = new Map();       // 渲染标签(全角处理) -> [{path, hasChildren, node}]
  let mmView = { scale: 1, tx: 0, ty: 0 };
  let mmFitted = false;          // 首次渲染做一次 fit，之后保留用户视图（折叠重绘也不跳）
  let mmDragging = false, mmDragMoved = false, mmDragStart = null;
  let mmRestoreParent = null, mmRestoreNext = null; // 全屏时搬到 <body> 暂存，退出还原（见 toggleMindFullscreen）

  const safeLabel = (s) => {
    let label = String(s || "").replace(/\n/g, " ").trim();
    if (!label) return "…";
    // Mermaid mindmap 对半角括号敏感：( ) [ ] 会被当作形状标记导致解析失败，统一转全角
    return label.replace(/\(/g, "（").replace(/\)/g, "）")
                .replace(/\[/g, "【").replace(/\]/g, "】");
  };

  // 走树并给每个节点分配唯一路径键（根=“0”，子="0.i"），同时建立“标签→节点”反向索引。
  function indexTree(mm) {
    mmIndex = new Map();
    const walk = (node, depth, path) => {
      node.__path = path;
      const label = safeLabel(node.title);
      const kids = (node.children || []).filter((c) => c && c.title);
      if (!mmIndex.has(label)) mmIndex.set(label, []);
      mmIndex.get(label).push({ path, node, hasChildren: kids.length > 0 });
      kids.forEach((c, i) => walk(c, depth + 1, `${path}.${i}`));
    };
    walk(mm, 0, "0");
  }

  // 构建 Mermaid 源码；处于折叠态的节点只输出自身，不再递归子节点。
  function buildMindmap(mm) {
    if (!mm || !mm.title) return null;
    const lines = ["mindmap"];
    const walk = (node, depth, path) => {
      lines.push("  ".repeat(depth) + safeLabel(node.title));
      if (mmCollapsed.has(path)) return;
      (node.children || []).filter((c) => c && c.title)
        .forEach((c, i) => walk(c, depth + 1, `${path}.${i}`));
    };
    walk(mm, 0, "0");
    return lines.join("\n");
  }

  // 判断某节点是否处于「可见」状态（其所有祖先都未被折叠）。
  function isVisiblePath(path) {
    const parts = path.split(".");
    for (let i = 1; i < parts.length; i++) {
      if (mmCollapsed.has(parts.slice(0, i).join("."))) return false;
    }
    return true;
  }

  // 由渲染后的标签反查树节点（优先选当前可见的那个，处理同名标签）。
  function findNodeByLabel(label) {
    const arr = mmIndex.get(label);
    if (!arr || !arr.length) return null;
    return arr.find((it) => isVisiblePath(it.path)) || arr[0];
  }

  function applyView() {
    const vp = document.querySelector("#mindContainer #mmViewport");
    if (!vp) return;
    vp.setAttribute("transform", `translate(${mmView.tx},${mmView.ty}) scale(${mmView.scale})`);
  }

  function fitView() {
    const box = $("#mindContainer");
    const vp = box.querySelector("#mmViewport");
    if (!vp) return;
    let bb;
    try { bb = vp.getBBox(); } catch (e) { return; }
    if (!bb || !bb.width || !bb.height) return;
    const cw = box.clientWidth || 600, ch = box.clientHeight || 400;
    const scale = Math.max(0.1, Math.min(cw / bb.width, ch / bb.height) * 0.92);
    mmView = {
      scale,
      tx: cw / 2 - (bb.x + bb.width / 2) * scale,
      ty: ch / 2 - (bb.y + bb.height / 2) * scale,
    };
    applyView();
  }

  function setupMindmapDom(svg) {
    // 把 svg 顶层所有子元素包进一个 #mmViewport 组，便于整体缩放/平移（含节点与连线）
    const vp = document.createElementNS(MM_NS, "g");
    vp.setAttribute("id", "mmViewport");
    while (svg.firstChild) vp.appendChild(svg.firstChild);
    svg.appendChild(vp);

    // 给每个节点标注 path + 有子节点时添加 折叠/展开 徽标，并绑定点击
    // ⚠️ 徽标若留在节点组内，会被后绘制的兄弟节点（叶子路径）盖住而点不到（e2e 实测首个徽标被拦截）。
    // 解法：包一层与节点相同 transform 的顶层 <g>、最后统一追加到 svg 末尾 → 永远绘制在最上层；
    // 点击直接绑在徽标上（不再依赖向节点组冒泡，避免与节点点击双重触发）。
    const foldHolders = [];
    svg.querySelectorAll("g.node").forEach((g) => {
      const labelEl = g.querySelector(".label");
      const label = labelEl ? labelEl.textContent.trim() : "";
      const info = findNodeByLabel(label);
      if (!info) return;
      g.dataset.mmPath = info.path;
      g.dataset.mmHasChildren = info.hasChildren ? "1" : "0";
      if (info.hasChildren) {
        let bb; try { bb = g.getBBox(); } catch (e) { return; }
        const badge = document.createElementNS(MM_NS, "text");
        badge.setAttribute("class", "mm-fold");
        badge.setAttribute("font-size", "14");
        badge.setAttribute("font-weight", "bold");
        badge.setAttribute("pointer-events", "all");
        badge.setAttribute("x", bb.x + bb.width + 3);
        badge.setAttribute("y", bb.y + 5);
        badge.textContent = mmCollapsed.has(info.path) ? "+" : "−";
        badge.style.fill = "#64748b";
        badge.style.cursor = "pointer";
        const holder = document.createElementNS(MM_NS, "g");
        const t = g.getAttribute("transform");
        if (t) holder.setAttribute("transform", t);
        holder.appendChild(badge);
        foldHolders.push(holder);
        badge.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (mmDragMoved) return; // 刚拖拽完，不算点击
          toggleFold(info.path);
        });
      }
      g.addEventListener("click", () => {
        if (mmDragMoved) return; // 刚拖拽完，不算点击
        if (g.dataset.mmHasChildren === "1") toggleFold(g.dataset.mmPath);
      });
    });
    foldHolders.forEach((h) => vp.appendChild(h)); // 全部徽标最后绘制（仍在 #mmViewport 内，随平移缩放），保证可点

    // ⚠️ mermaid 输出的 svg 自带 viewBox（如 "3 3 1592 585"）。若保留它并设 width/height=100%，
    // 浏览器会**自动**把整棵树按 viewBox 缩放进容器（对 1568px 宽的树 ≈0.43 倍），随后 fitView 再按
    // getBBox() 算一次 scale，等于**双倍缩小** → 图被压成一团（前端实测有效缩放 ≈0.17）。
    // 这里把 svg 归一为「1 用户单位 = 1 像素」：viewBox 收敛到内容 bbox（此 bbox 已含折叠徽标）、
    // 宽高设为内容像素尺寸。如此 fitView 的 scale 才是一次正确应用，且滚轮/拖拽平移的坐标（按像素算）也对得上。
    // pad 留给最右/最下节点上的徽标（x=节点右缘+3）与描边，避免被 viewBox 裁掉。
    let bb;
    try { bb = svg.getBBox(); } catch (e) {}
    if (bb && bb.width > 0 && bb.height > 0) {
      const pad = 10;
      svg.setAttribute("viewBox", `${bb.x - pad} ${bb.y - pad} ${bb.width + 2 * pad} ${bb.height + 2 * pad}`);
      svg.setAttribute("width", `${bb.width + 2 * pad}px`);
      svg.setAttribute("height", `${bb.height + 2 * pad}px`);
    } else {
      svg.setAttribute("width", "100%");
      svg.setAttribute("height", "100%");
    }
    applyView();
  }

  function toggleFold(path) {
    if (mmCollapsed.has(path)) mmCollapsed.delete(path);
    else mmCollapsed.add(path);
    if (mmTree) renderMindmap(mmTree); // 客户端重绘，不显示 busy
  }

  function renderMindmapFallback(mm) {
    const box = $("#mindContainer");
    const walk = (node) =>
      `<li><span class="mind-node">${esc(node.title)}</span>${node.children && node.children.length ? `<ul class="mind-branch">${node.children.map(walk).join("")}</ul>` : ""}</li>`;
    box.innerHTML = `<ul class="text-sm ml-2">${walk(mm)}</ul>`;
    box.classList.add("flex", "items-center", "justify-center");
  }

  // 懒加载 mermaid：优先复用 index.html 里 <script type=module> 早就 import 好的实例
  async function ensureMermaid() {
    if (window.__mermaid) return window.__mermaid;
    try {
      const mod = await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");
      mod.default.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base", mindmap: { padding: 12 } });
      window.__mermaid = mod.default;
      return mod.default;
    } catch (e) {
      console.warn("mermaid 加载失败:", e);
      return null;
    }
  }

  async function renderMindmap(mm) {
    const box = $("#mindContainer");
    if (!mm || !mm.title) { box.innerHTML = `<span class="text-sm text-slate-400">暂无思维导图</span>`; return; }
    mmTree = mm;
    indexTree(mm);
    const m = await ensureMermaid();
    if (!m) { renderMindmapFallback(mm); return; }
    const text = buildMindmap(mm);
    if (!text) { renderMindmapFallback(mm); return; }
    try {
      const { svg } = await m.render("mindSvg", text);
      box.innerHTML = svg;
      box.classList.remove("flex", "items-center", "justify-center");
      setupMindmapDom(box.querySelector("svg"));
      if (!mmFitted) { fitView(); mmFitted = true; } else { applyView(); }
    } catch (err) {
      console.warn("mermaid mindmap render failed:", err);
      renderMindmapFallback(mm);
    }
  }

  // 导图交互：拖拽=平移、滚轮=缩放（绕光标）、点击节点=折叠/展开
  function bindMindmapInteractions() {
    const box = $("#mindContainer");
    if (!box || box.dataset.mmBound) return;
    box.dataset.mmBound = "1";

    box.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = box.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
      const ns = Math.max(0.1, Math.min(6, mmView.scale * factor));
      const wx = (cx - mmView.tx) / mmView.scale, wy = (cy - mmView.ty) / mmView.scale;
      mmView.scale = ns;
      mmView.tx = cx - wx * ns;
      mmView.ty = cy - wy * ns;
      applyView();
    }, { passive: false });

    box.addEventListener("mousedown", (e) => {
      mmDragging = true; mmDragMoved = false;
      mmDragStart = { x: e.clientX, y: e.clientY, tx: mmView.tx, ty: mmView.ty };
      document.addEventListener("mousemove", onMmMove);
      document.addEventListener("mouseup", onMmUp);
      e.preventDefault();
    });
  }
  function onMmMove(e) {
    if (!mmDragging) return;
    const dx = e.clientX - mmDragStart.x, dy = e.clientY - mmDragStart.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) mmDragMoved = true;
    mmView.tx = mmDragStart.tx + dx;
    mmView.ty = mmDragStart.ty + dy;
    applyView();
  }
  function onMmUp() {
    mmDragging = false;
    document.removeEventListener("mousemove", onMmMove);
    document.removeEventListener("mouseup", onMmUp);
  }

  // 全屏：给 #panelMind 挂 mm-fullscreen 遮罩态，复用同一 #mindContainer（拖拽/缩放/折叠全保留）
  function toggleMindFullscreen() {
    const panel = $("#panelMind");
    const isFull = panel.classList.toggle("mm-fullscreen");
    if (isFull) {
      // ⚠️ 祖先「解析结果卡片」带 transform（animate-rise 的 identity 矩阵），会把
      // position:fixed 的包含块固定在卡片上而非视口 → 全屏时搬到 <body> 逃生，退出还原。
      if (!mmRestoreParent) {
        mmRestoreParent = panel.parentNode;
        mmRestoreNext = panel.nextSibling;
      }
      document.body.appendChild(panel);
    } else {
      // 还原回原父节点原位（保持既有布局）
      const p = mmRestoreParent, n = mmRestoreNext;
      if (p) { p.insertBefore(panel, n); mmRestoreParent = null; mmRestoreNext = null; }
    }
    document.body.style.overflow = isFull ? "hidden" : "";
    mmFitted = false; // 视口尺寸 60vh→全屏 变化，复位 fit 让整树重新适配
    fitView();
  }

  // 下载高清 PNG：原生实现（serialize→Blob→Image→canvas）。mermaid mindmap 标签是
  // <foreignObject>，经 <img> 光栅化会丢文字（空白），须先转成 <text> 再导出。
  function exportMindmapPNG() {
    const svg = document.querySelector("#mindContainer svg");
    if (!svg) { toast("请先生成思维导图"); return; }
    const src = svg.querySelector("#mmViewport") || svg;
    let bb;
    try { bb = src.getBBox(); } catch (e) { toast("无法导出导图"); return; }
    if (!bb || !bb.width || !bb.height) { toast("无法导出导图（视图为空）"); return; }

    const pad = 24;
    const w = Math.ceil(bb.width + pad * 2);
    const h = Math.ceil(bb.height + pad * 2);

    // 先在**活的** SVG 上读每个 foreignObject 的文本与计算样式（序列化后脱离 DOM，getComputedStyle 会失效）
    const liveData = Array.from(svg.querySelectorAll("foreignObject")).map((fo) => {
      const probe = fo.querySelector("span") || fo;
      const cs = getComputedStyle(probe);
      return {
        x: Number(fo.getAttribute("x")) || 0,
        y: Number(fo.getAttribute("y")) || 0,
        h: Number(fo.getAttribute("height")) || 0,
        text: fo.textContent.replace(/\s+/g, " ").trim(),
        fontSize: cs.fontSize || "16px",
        fill: cs.color || "#0f172a",
        fontFamily: cs.fontFamily || "inherit",
      };
    });

    const clone = svg.cloneNode(true);
    clone.querySelectorAll(".mm-fold").forEach((n) => n.remove()); // 去掉 UI 折叠徽标
    const vp = clone.querySelector("#mmViewport");
    if (vp) vp.removeAttribute("transform"); // 去掉平移缩放，导出完整树

    // foreignObject -> <text>
    clone.querySelectorAll("foreignObject").forEach((fo, i) => {
      const d = liveData[i] || { x: 0, y: 0, h: 0, text: "", fontSize: "16px", fill: "#0f172a", fontFamily: "inherit" };
      const t = document.createElementNS(MM_NS, "text");
      t.setAttribute("x", String(d.x + 8));
      t.setAttribute("y", String(d.y + d.h / 2));
      t.setAttribute("dominant-baseline", "middle");
      t.setAttribute("font-size", d.fontSize);
      t.setAttribute("fill", d.fill);
      t.setAttribute("font-family", d.fontFamily);
      t.textContent = d.text;
      fo.parentNode.insertBefore(t, fo);
      fo.remove();
    });

    clone.setAttribute("xmlns", MM_NS);
    clone.setAttribute("width", String(w));
    clone.setAttribute("height", String(h));
    clone.setAttribute("viewBox", `${bb.x - pad} ${bb.y - pad} ${w} ${h}`);
    const rect = document.createElementNS(MM_NS, "rect");
    rect.setAttribute("x", String(bb.x - pad));
    rect.setAttribute("y", String(bb.y - pad));
    rect.setAttribute("width", String(w));
    rect.setAttribute("height", String(h));
    rect.setAttribute("fill", "#ffffff");
    clone.insertBefore(rect, clone.firstChild);

    let str;
    try { str = new XMLSerializer().serializeToString(clone); } catch (e) { toast("序列化导图失败"); return; }
    const url = URL.createObjectURL(new Blob([str], { type: "image/svg+xml;charset=utf-8" }));
    const img = new Image();
    img.onload = () => {
      const maxSide = 4096;
      const scale = Math.max(1, Math.min(3, maxSide / Math.max(w, h)));
      const cw = Math.round(w * scale), cht = Math.round(h * scale);
      const canvas = document.createElement("canvas");
      canvas.width = cw; canvas.height = cht;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, cw, cht);
      ctx.drawImage(img, 0, 0, cw, cht);
      canvas.toBlob((png) => {
        if (!png) { toast("导出图片失败"); URL.revokeObjectURL(url); return; }
        const a = document.createElement("a");
        a.href = URL.createObjectURL(png);
        a.download = "mindmap.png";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        URL.revokeObjectURL(url);
        toast("高清导图已导出");
      }, "image/png");
    };
    img.onerror = () => { console.warn("导出导图图片加载失败"); URL.revokeObjectURL(url); toast("导出导图失败"); };
    img.src = url;
  }


  async function handleSummary(force) {
    const url = activeUrl();
    if (!url) { toast("请先解析视频链接"); return; }
    hideError();
    selectTab("sumBtn");
    const c = getCache(url, "summary");
    if (c && !force) { renderSummary(c.data); lastSummary = c.summary; return; } // 命中缓存而不再请求
    $("#sumMd").innerHTML = skelHTML(4); // 骨架屏占位，替代冗余的「生成中…」句子
    startBusy("正在生成摘要，长视频耗时较久…");
    askHistory = [];
    try {
      const d = await api("/api/ai/summary", { url });
      // 竞态守卫：请求期间换链接则丢弃旧结果（界面已由 resetAnalyze 清理）
      if (activeUrl() !== url) return;
      renderSummary(d);
      lastSummary = (d.summary || "").trim() ? d.summary : lastSummary;
      setCache(url, "summary", { data: d, summary: lastSummary });
      stopBusy();
      toast("摘要已生成");
    } catch (e) {
      if (activeUrl() !== url) return;
      stopBusy(); showError(e.message);
    }
  }

  async function handleChapters(force) {
    const url = activeUrl();
    if (!url) { toast("请先解析视频链接"); return; }
    hideError();
    selectTab("chaptersBtn");
    const c = getCache(url, "chapters");
    if (c && !force) { renderChapters(c.chapters || []); return; } // 命中缓存而不再请求
    $("#sumChapters").innerHTML = skelHTML(3); // 骨架屏占位，替代冗余的「生成章节时间轴中…」句子
    startBusy("正在生成章节时间轴…");
    try {
      const d = await api("/api/ai/chapters", { url });
      renderChapters(d.chapters || []);
      setCache(url, "chapters", { chapters: d.chapters || [] });
      stopBusy();
      toast("章节时间轴已生成");
    } catch (e) { stopBusy(); showError(e.message); }
  }

  async function handleMindmap(force) {
    const url = activeUrl();
    if (!url) { toast("请先解析视频链接"); return; }
    hideError();
    selectTab("mindmapBtn");
    const c = getCache(url, "mindmap");
    if (c && !force) { // 命中缓存而不再请求
      mmCollapsed.clear(); mmFitted = false;
      await renderMindmap(c.mindmap);
      return;
    }
    $("#mindContainer").innerHTML = skelHTML(4); // 骨架屏占位，替代冗余的「正在生成导图…」句子
    startBusy("正在生成思维导图…");
    try {
      const d = await api("/api/ai/mindmap", { url });
      mmCollapsed.clear(); mmFitted = false;
      await renderMindmap(d.mindmap); // 渲染（含 mermaid 异步）完成后才解除 busy，避免按钮提前恢复
      setCache(url, "mindmap", d);
      stopBusy();
      toast("思维导图已生成");
    } catch (e) {
      stopBusy();
      showError(e.message);
      $("#mindContainer").innerHTML = `<span class="text-sm text-slate-400">导图生成失败</span>`;
    }
  }

  function downloadMarkdown() {
    if (!lastSummary) { toast("请先生成摘要"); return; }
    const blob = new Blob([lastSummary], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "video-summary.md";
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }

  // 章节·时间轴（独立面板）下载 .md：把章节整理为带时间的 Markdown
  function downloadChapters() {
    if (!lastChapters || !lastChapters.length) { toast("请先生成章节时间轴"); return; }
    const lines = ["# 章节时间轴", ""];
    lastChapters.forEach((c) => {
      lines.push(`## [${fmtSec(c.start)} - ${fmtSec(c.end)}] ${c.title || ""}`);
      if (c.summary) lines.push("", c.summary);
      (c.key_points || []).forEach((p) => lines.push(`- ${p}`));
      lines.push("");
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "章节时间轴.md";
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }

  // 问答 导出对话：把 askHistory 复盘为 Markdown
  function exportChat() {
    if (!askHistory.length) { toast("暂无对话可导出"); return; }
    const lines = ["# 视频问答记录", ""];
    askHistory.forEach((m) => {
      const who = m.role === "user" ? "我" : "AI";
      lines.push(`**${who}**：${m.content}`);
      lines.push("");
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "视频问答记录.md";
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }

  // ---------- AI 问答（气泡聊天框 · SSE 流式） ----------
  // 用户问题气泡靠右（brand 色）、AI 回答气泡靠左（灰），增量流式写入。
  // 统一用 textContent 写入（不用 innerHTML），避免用户/模型文本被当 HTML 注入。
  function askBubble(role, text) {
    const isUser = role === "user";
    const wrap = document.createElement("div");
    wrap.className = "flex " + (isUser ? "justify-end" : "justify-start");
    const body = document.createElement("div");
    body.className = "max-w-[85%]";
    const tag = document.createElement("div");
    tag.className = "text-[11px] text-slate-400 mb-0.5 select-none " + (isUser ? "text-right" : "");
    tag.textContent = isUser ? "我" : "AI";
    const bubble = document.createElement("div");
    bubble.className = "whitespace-pre-wrap leading-relaxed text-sm px-3 py-2 rounded-2xl " +
      (isUser ? "bg-brand text-white rounded-br-sm" : "bg-slate-100 text-slate-700 rounded-bl-sm");
    bubble.dataset.role = role;
    bubble.textContent = text;
    body.appendChild(tag);
    body.appendChild(bubble);
    wrap.appendChild(body);
    return { wrap, bubble };
  }
  function askClearChat(resetBtn) {
    askGen++; // 令在途流的 myGen 失效，防止其结束后把旧问答写回/复位按钮
    if (askAbort) { askAbort.abort(); askAbort = null; }
    $("#askOutput").innerHTML = "";
    askHistory = [];
    if (resetBtn) { // 仅「清空」按钮界面复位按钮；换链接路径交由 setAnBtns 统一管控
      const b = $("#askBtn");
      b.disabled = false;
      b.textContent = "发送";
    }
  }

  // 流结束后把 AI 回答的纯文本渲染成 Markdown（消毒，防 LLM 文本被当 HTML 注入）
  function renderAskMarkdown(bubble, md) {
    if (window.marked && window.DOMPurify) {
      const el = document.createElement("div");
      el.className = "md";
      el.innerHTML = window.DOMPurify.sanitize(window.marked.parse(md));
      bubble.textContent = "";
      bubble.classList.remove("whitespace-pre-wrap");
      bubble.appendChild(el);
    } else {
      // 极端 CDN 失败兜底：保留纯文本
      console.warn("marked/DOMPurify 未加载，问答以纯文本显示");
    }
  }

  async function handleAsk() {
    const btn = $("#askBtn");
    if (btn.disabled) return; // 正在回答中，忽略重复提交（含再按 Enter）
    const url = activeUrl();
    const q = $("#askInput").value.trim();
    if (!url) { toast("请先解析视频链接"); return; }
    if (!q) { toast("请输入问题"); return; }
    $("#askInput").value = ""; // 发送后清空输入框（问题已进气泡/历史，不再残留）
    selectTab("askOpenBtn");
    const out = $("#askOutput");
    const myGen = ++askGen;   // 本轮的会话令牌：被清空/换链接（askGen 再 ++）后即失效
    askAbort = new AbortController();
    const signal = askAbort.signal;
    // 用户问题气泡（右侧）
    const userMsg = askBubble("user", q);
    // AI 回答气泡（左侧，placeholder 提示「思考中」；首帧到达后替换）
    const aiMsg = askBubble("assistant", "");
    const typing = document.createElement("span");
    typing.className = "text-slate-400";
    typing.textContent = "…";
    aiMsg.bubble.appendChild(typing);
    out.appendChild(userMsg.wrap);
    out.appendChild(aiMsg.wrap);
    out.scrollTop = out.scrollHeight;
    let started = false;
    let errorOccurred = false;
    const appendDelta = (text) => {
      if (myGen !== askGen) return; // 已清空/换链接：忽略后续帧，不写进已清空的 DOM
      if (!started) { aiMsg.bubble.textContent = ""; started = true; } // 清掉「…」占位
      aiMsg.bubble.textContent += text;
      out.scrollTop = out.scrollHeight;
    };
    btn.disabled = true; btn.textContent = "思考中…";

    try {
      const resp = await fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, question: q, history: askHistory }),
        signal, // 支持清空/换链接时取消在途请求，杜绝陈旧流写回
      });
      if (!resp.ok) {
        let msg = "AI 回答失败";
        try { msg = (await resp.json()).error || msg; } catch (_) {}
        throw new Error(msg);
      }
      if (!resp.body) throw new Error("不支持流式响应");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let full = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const json = parseSSEFrame(frame);
          if (!json) continue;
          if (json.delta) { appendDelta(json.delta); full += json.delta; }
          else if (json.error) { errorOccurred = true; appendDelta("\n[错误] " + json.error); }
          else if (json.status && !started) {
            // 生成前阶段实时进度：字幕检索/上下文构建/开始生成都会先推 status 帧，
            // 让用户看到「流已经动起来」，而非静默等待到首 token（冷路径可能 2s+）。
            typing.textContent = json.status === "preparing" ? "正在检索字幕与上下文…" : "正在生成回答…";
            out.scrollTop = out.scrollHeight;
          }
        }
      }
      // 无任何 delta（空回复）：清掉「…」占位、不写历史，避免气泡卡在思考态
      if (!started) aiMsg.bubble.textContent = "(无回答)";
      // 流正常结束且未被错误中断：把纯文本 render 成 Markdown（仅当前会话、有内容、未被清空/换链接）
      if (started && full && !errorOccurred && myGen === askGen) renderAskMarkdown(aiMsg.bubble, full);
      if (full && myGen === askGen) askHistory.push({ role: "user", content: q }, { role: "assistant", content: full });
    } catch (e) {
      if (e.name === "AbortError") {
        // 清空/换链接时主动取消：静默，不追加错误、不写回历史
      } else if (myGen === askGen) {
        appendDelta("\n[出错] " + e.message);
      }
    } finally {
      if (myGen === askGen) { // 仅当前会话（未被清空/换链接）才复位按钮
        btn.disabled = false;
        btn.textContent = "发送";
        askAbort = null;
      }
    }
  }

  function parseSSEFrame(frame) {
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return null;
    try { return JSON.parse(data); } catch (_) { return null; }
  }

  // ---------- PRO 弹窗 ----------
  function openPro() {
    const m = $("#proModal");
    m.classList.remove("hidden");
    m.classList.add("flex");
  }
  function closePro() {
    const m = $("#proModal");
    m.classList.add("hidden");
    m.classList.remove("flex");
  }

  // ---------- 事件绑定 ----------
  $("#parseBtn").onclick = handleParse;
  $("#urlInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleParse(); } });
  $("#downloadBtn").onclick = handleDownload;
  $("#copyLink").onclick = () => { if (current) { navigator.clipboard.writeText(current.url).then(() => toast("链接已复制")); } };
  $("#addToBatch").onclick = () => { if (current) submitBatch([{ url: current.url, format_id: current.selected || null }]); };
  $("#clearBatch").onclick = () => { $("#batchList").innerHTML = ""; showBatch(false); };
  $("#subBtn").onclick = () => handleSubtitle(false);       // 🎬 字幕：回到「原字幕」视图
  $("#subTranslateBtn").onclick = () => handleSubtitle(true); // 翻译为：选中的语言（点一下即翻）
  $("#sumBtn").onclick = () => handleSummary();      // 不能直接绑定 handleSummary：onclick 会把事件对象当 force 传入导致永远重算
  $("#chaptersBtn").onclick = () => handleChapters();
  $("#mindmapBtn").onclick = () => handleMindmap();
  $("#askOpenBtn").onclick = () => { hideError(); selectTab("askOpenBtn"); $("#askInput").focus(); };
  $("#sumCopy").onclick = () => { if (lastSummary) navigator.clipboard.writeText(lastSummary).then(() => toast("摘要已复制")); else toast("请先生成摘要"); };
  $("#sumDownload").onclick = downloadMarkdown;
  $("#askBtn").onclick = handleAsk;
  $("#askInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); handleAsk(); } });
  $("#askClear").onclick = () => askClearChat(true); // 用户主动清空：复位按钮，允许立即再问
  $("#askExport").onclick = exportChat;
  $("#subRefresh").onclick = () => handleSubtitle(subModeTranslate, true);
  $("#sumRefresh").onclick = () => handleSummary(true);
  $("#chaptersRefresh").onclick = () => handleChapters(true);
  $("#chaptersDownload").onclick = downloadChapters;
  $("#mindRefresh").onclick = () => handleMindmap(true);
  // 导图工具栏：拖动/滚轮缩放由 bindMindmapInteractions 处理；这里负责按钮 + 折叠/展开
  $("#mmZoomIn").onclick = () => { mmView.scale = Math.min(6, mmView.scale * 1.2); applyView(); };
  $("#mmZoomOut").onclick = () => { mmView.scale = Math.max(0.1, mmView.scale / 1.2); applyView(); };
  $("#mmReset").onclick = () => { mmFitted = false; fitView(); };
  $("#mmExpandAll").onclick = () => { mmCollapsed.clear(); if (mmTree) renderMindmap(mmTree); };
  $("#mmCollapseAll").onclick = () => {
    if (!mmTree) return;
    mmCollapsed.clear();
    const walk = (n, path) => {
      const kids = (n.children || []).filter((c) => c && c.title);
      if (kids.length && path !== "0") mmCollapsed.add(path); // 折叠全部子分支，但保留根
      kids.forEach((c, i) => walk(c, `${path}.${i}`));
    };
    walk(mmTree, "0");
    renderMindmap(mmTree);
  };
  $("#mmFullscreen").onclick = toggleMindFullscreen;
  $("#mmDownload").onclick = exportMindmapPNG;
  bindMindmapInteractions();
  $("#upgradeBtn").onclick = openPro;
  $("#ctaUpgrade").onclick = openPro;
  $("#proClose").onclick = closePro;
  $("#payMonth").onclick = () => toast("支付能力即将上线，敬请期待！");
  $("#payYear").onclick = () => toast("支付能力即将上线，敬请期待！");
  $("#proModal").addEventListener("click", (e) => { if (e.target.id === "proModal") closePro(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePro(); });

  // 自动摘要开关（默认关，localStorage 持久化）+ 视频描述展开/收起
  $("#autoSumToggle").checked = autoSummaryOn();
  $("#autoSumToggle").onchange = (e) => setAutoSummary(e.target.checked);
  $("#dlDescToggle").onclick = () => {
    const body = $("#dlDescText"), btn = $("#dlDescToggle");
    const open = body.classList.toggle("open");
    btn.textContent = open ? "收起" : "展开";
  };

  // 自动扩容 textarea
  const ta = $("#urlInput");
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; });

  window.__vdl = { current, startPoll, stopPoll, autoSummary: autoSummaryOn, setAutoSummary };
  // 测试/调试钩子：暴露思维导图的折叠与视图状态（供 e2e 归因断言）
  window.__mm = {
    get collapsed() { return Array.from(mmCollapsed); },
    get view() { return { scale: mmView.scale, tx: mmView.tx, ty: mmView.ty }; },
    get tree() { return mmTree; },
  };
  window.__dbg = {
    get current() { return current; },
    get cache() { return featureCache; },
  };
})();
