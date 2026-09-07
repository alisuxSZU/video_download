/* 前端逻辑：解析 → 渲染结果 / 批量队列 / 字幕 / AI 摘要 / 动效 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------- 全局状态 ----------
  let current = null; // {url, formats, selected}
  let polling = new Map(); // jobId -> interval

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
    showResult(true);
    $("#dlTitle").textContent = "解析中…";
    $("#dlMeta").textContent = "正在获取视频信息与清晰度…";
    $("#thumb").classList.add("hidden");
    $("#formatList").innerHTML = `<div class="skeleton h-12 rounded-lg"></div><div class="skeleton h-12 rounded-lg"></div>`;
    $("#downloadProgress").classList.add("hidden");
    try {
      const data = await api("/api/parse", { url });
      current = { url, formats: data.formats || [], selected: null, meta: data };
      renderResult(data);
    } catch (e) {
      showResult(false);
      toast(e.message);
    }
  }

  function renderResult(data) {
    $("#dlTitle").textContent = data.title || "未命名视频";
    $("#dlMeta").textContent = `${data.extractor || "通用视频"} · ${humanSec(data.duration) || "未知时长"}`;
    const extBadges = { ArchiveOrg: "Archive 视频", YouTube: "YouTube", BiliBili: "哔哩哔哩", TikTok: "TikTok", Generic: "通用视频" };
    $("#extBadge").textContent = extBadges[data.extractor] || "通用视频";

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

  function formatRow(f, checked) {
    const mergeChip = f.needs_merge ? `<span class="chip chip-amber">需合并音视频</span>` : `<span class="chip chip-emerald">单文件</span>`;
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
      </div>
      <div class="flex items-center gap-2 flex-none">${mergeChip}</div>`;
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
      $("#progDownload").classList.add("hidden");
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
        onDone: () => {
          const link = $("#progDownload");
          link.href = `/api/jobs/${currentJobId}/file`;
          link.classList.remove("hidden");
          $("#progStatus").textContent = "✅ 下载完成，点击右侧下载文件";
          toast("下载完成！");
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
      <div class="flex items-center justify-between mt-1 text-xs text-slate-400"><span class="prog-info"></span><a class="dl-link hidden text-brand font-semibold">下载</a></div>`;
    list.appendChild(row);
    if (initError) { row.querySelector(".status-badge").textContent = "失败"; row.querySelector(".status-badge").classList.add("bg-rose-100", "text-rose-600"); }
    if (!jobId) return;
    startPoll(jobId, {
      onUpdate: (j) => {
        const badge = row.querySelector(".status-badge");
        badge.textContent = statusZh(j.status);
        badge.className = "status-badge text-xs font-semibold px-2 py-1 rounded-full shrink-0 " + statusColor(j.status);
        row.querySelector(".prog-bar").style.width = (j.progress || 0) + "%";
        row.querySelector(".prog-info").textContent = (j.title ? j.title.slice(0, 40) + " · " : "") + (j.speed || "") + " " + humanBytes(j.downloaded_bytes);
        if (j.status === "done") {
          const link = row.querySelector(".dl-link");
          link.href = `/api/jobs/${j.id}/file`;
          link.classList.remove("hidden");
        }
        if (j.status === "error") row.querySelector(".prog-info").textContent = (j.error || "失败");
      },
      onError: (j) => {
        const badge = row.querySelector(".status-badge");
        badge.textContent = "失败";
        badge.className = "status-badge text-xs font-semibold px-2 py-1 rounded-full shrink-0 bg-rose-100 text-rose-600";
        row.querySelector(".prog-info").textContent = (j.error || "下载失败");
      },
    });
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

  // ---------- 字幕 ----------
  async function handleSubtitle(translate) {
    const url = $("#subUrl").value.trim();
    if (!url) { toast("请粘贴视频链接"); return; }
    $("#subResult").classList.remove("hidden");
    const box = $("#subText");
    if (translate) { box.textContent = "正在翻译…"; }
    else { box.textContent = "正在提取字幕…"; }
    try {
      const body = { url, lang: "zh", is_auto: false };
      if (translate) body.target_lang = "简体中文";
      const d = await api("/api/subtitles", body);
      $("#subMeta").textContent = `${d.lang || ""} · ${d.source === "manual" ? "内建字幕" : "自动字幕"} · ${(d.format || "").toUpperCase()}`;
      box.textContent = d.translated || d.content || "（空）";
      makeSubDownload(d, translate);
    } catch (e) {
      box.textContent = e.message;
      $("#subMeta").textContent = "";
    }
  }
  function makeSubDownload(d, isTrans) {
    // 用 Blob 而非 data: URI，防止部分浏览器拦截 data: 下载
    const content = (d.translated || d.content) || "";
    const ext = isTrans ? "txt" : d.format || "srt";
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const link = $("#subDl");
    if (link._url) URL.revokeObjectURL(link._url);
    link._url = URL.createObjectURL(blob);
    link.href = link._url;
    link.setAttribute("download", `subtitle.${ext}`);
  }

  // ---------- AI 摘要 ----------
  async function handleSummary() {
    const url = $("#sumUrl").value.trim();
    if (!url) { toast("请粘贴视频链接"); return; }
    $("#sumResult").classList.remove("hidden");
    $("#sumText").textContent = "正在生成摘要，请稍候…";
    try {
      const d = await api("/api/ai/summary", { url });
      $("#sumText").textContent = d.summary || "已生成";
    } catch (e) {
      $("#sumText").textContent = e.message;
    }
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
  $("#subBtn").onclick = () => handleSubtitle(false);
  $("#subTranslate").onclick = () => handleSubtitle(true);
  $("#sumBtn").onclick = handleSummary;
  $("#sumCopy").onclick = () => navigator.clipboard.writeText($("#sumText").textContent).then(() => toast("摘要已复制"));
  $("#upgradeBtn").onclick = openPro;
  $("#ctaUpgrade").onclick = openPro;
  $("#proClose").onclick = closePro;
  $("#payMonth").onclick = () => toast("支付能力即将上线，敬请期待！");
  $("#payYear").onclick = () => toast("支付能力即将上线，敬请期待！");
  $("#proModal").addEventListener("click", (e) => { if (e.target.id === "proModal") closePro(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePro(); });

  // 自动扩容 textarea
  const ta = $("#urlInput");
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; });

  window.__vdl = { current, startPoll, stopPoll };
})();
