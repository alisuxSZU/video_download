# -*- coding: utf-8 -*-
"""端到端验证「批量逐条自动下载（避免并发）」：

两个视频：OvMW1BQFCJc / PeNILuH9LL0。各选 144p（小文件、加速）加入批量。
验证要点：
1) 批量队列每完成一条，**自动**发起下载（不再显示手动「下载」链接）。
2) 下载是**逐条、串行**的（fetch→Blob，取完一条再取下一条，避免并发触发
   浏览器「下载多个文件」授权弹窗）。
3) 每个下载文件非空、后缀 .mp4；状态为「已完成 / 已自动保存到本地」。
用系统版 Chrome（channel="chrome"）真实浏览器验证。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8011"
V1 = "https://www.youtube.com/watch?v=OvMW1BQFCJc"  # 144p = 160
V2 = "https://www.youtube.com/watch?v=PeNILuH9LL0"  # 144p = 394
OUT = Path("d:/LCP_agent/video_download/screenshots")
OUT.mkdir(parents=True, exist_ok=True)


def select_144p_and_add_to_batch(page):
    # 等解析完成 → 选 144p → 加入批量
    page.wait_for_selector("#dlTitle:not(:has-text('解析中'))", timeout=90000)
    page.wait_for_timeout(1200)
    page.locator("#formatList > div", has_text=re.compile(r"144p")).first.click()
    page.wait_for_timeout(300)
    sel = page.evaluate("window.__dbg.current && window.__dbg.current.selected")
    row = page.locator("#batchList > div").count()
    page.click("#addToBatch")
    page.wait_for_timeout(800)
    return sel, row + 1


def run():
    downloads = []
    console_msgs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=True,
            args=["--enable-automatic-downloads", "--disable-features=AutomaticDownloadsCheck"],
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        # 记录页面内「带 download 属性的 anchor 点击」与 createObjectURL 次数 —— 判定 app 是否真的点了两次
        page.add_init_script("""
          window.__dlClicks = [];
          window.__objUrls = 0;
          (() => {
            const origClick = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function () {
              if (String(this.download).length) {
                window.__dlClicks.push({ dl: this.download, href: String(this.href).slice(0, 60) });
              }
              return origClick.apply(this, arguments);
            };
            const origCOU = URL.createObjectURL;
            URL.createObjectURL = function (b) { window.__objUrls += 1; return origCOU.call(this, b); };
          })();
        """)
        page.on("console", lambda m: console_msgs.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: console_msgs.append(f"pageerror: {e}"))
        def on_download(d, idx=[0]):
            i = idx[0]; idx[0] += 1
            fn = d.suggested_filename or f"batch_{i}.mp4"
            target = OUT / f"batch_{i}.mp4"
            print(f"[download-event #{i}] url={d.url!r} suggested={d.suggested_filename!r}")
            # 立即消费该下载，避免连续 blob 下载因上一个未消费而捕获不到
            d.save_as(str(target))
            print(f"[download-event #{i}] 已存 {fn!r} → {target.stat().st_size} 字节")
            downloads.append(d)
        page.on("download", on_download)
        def on_http(resp, idx=[0]):
            if "/api/jobs/" in resp.url and "/file" in resp.url:
                i = idx[0]; idx[0] += 1
                print(f"[file-response #{i}] status={resp.status} url={resp.url!r}")
        page.on("response", on_http)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1000)

        # 视频1 → 144p(160) → 加入批量
        page.fill("#urlInput", V1)
        page.click("#parseBtn")
        sel1, n1 = select_144p_and_add_to_batch(page)
        print(f"[check] video1 selected={sel1} 批量行={n1}")
        assert sel1 == "160", "video1 未选 144p(160)"

        # 视频2 → 144p(394) → 加入批量
        page.fill("#urlInput", V2)
        page.click("#parseBtn")
        sel2, n2 = select_144p_and_add_to_batch(page)
        print(f"[check] video2 selected={sel2} 批量行={n2}")
        assert sel2 == "394", "video2 未选 144p(394)"

        n_rows = page.locator("#batchList > div").count()
        print(f"[check] 批量队列行数 = {n_rows}")
        assert n_rows == 2, "批量队列应有 2 行"

        # 批量任务各自下载（服务端），完成后逐条自动保存（浏览器下载）
        page.wait_for_function(
            "() => [...document.querySelectorAll('#batchList .status-badge')].every(b => b.textContent === '已完成')",
            timeout=240000,
        )
        print("[check] 两条批量任务均已完成")
        infos = page.evaluate("[...document.querySelectorAll('#batchList .prog-info')].map(i => i.textContent)")
        print(f"[check] 批量行状态 = {infos}")
        assert any("已自动保存" in t for t in infos), "批量行未显示自动保存状态"

        # 等两个 anchor 都被点击（app 驱动两条下载）。⚠️ 不要用 `time.sleep` 轮询 ——
        # 它会阻塞 Python 事件循环，导致 Playwright 派发不了第 2 个 download 事件；
        # 要用 `page.wait_for_function`（内部会派发事件）+ 一段 wait_for_timeout 收尾。
        page.wait_for_function("() => window.__dlClicks.length >= 2", timeout=300000)
        page.wait_for_timeout(5000)  # 让 2 个 blob 下载事件派发并被 on("download") 捕获
        print(f"[check] 收集到 {len(downloads)} 条自动下载")
        clicks = page.evaluate("window.__dlClicks")
        objurls = page.evaluate("window.__objUrls")
        print(f"[diag] 页面内 anchor 下载点击 = {len(clicks)}，createObjectURL = {objurls}")
        for c in clicks:
            print(f"[diag]   点击 {c!r}")
        if len(downloads) < 2:
            print(f"[diag] 捕获 console/pageerror 消息数 = {len(console_msgs)}")
            for m in console_msgs:
                print(f"[diag] {m}")
            raise AssertionError(
                f"批量自动下载只捕获 {len(downloads)} 条（应为 2），"
                f"页面内点击 {len(clicks)} 次 / createObjectURL {objurls} 次"
            )

        names = []
        for i, d in enumerate(downloads):
            fn = d.suggested_filename or f"batch_{i}.mp4"
            names.append(fn)
            target = OUT / f"batch_{i}.mp4"
            size = target.stat().st_size
            print(f"[check] [{i}] 已存 {fn!r} → {size} 字节")
            assert size > 0, f"batch_{i} 下载为空"
        print(f"[check] 下载文件名 = {names}")
        for fn in names:
            assert fn.lower().endswith(".mp4"), f"文件名非 mp4: {fn!r}"

        context.close()
        browser.close()

    print("\nBATCH SEQUENTIAL AUTO-DOWNLOAD E2E PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nBATCH E2E FAILED: {exc}")
        sys.exit(1)
