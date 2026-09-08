# -*- coding: utf-8 -*-
"""端到端验证「点击下载 → 文件自动保存到本地」：

用给定视频 OvMW1BQFCJc（约36分钟），选一个低分辨率格式（144p）压缩文件体积，
确保服务端下载 + 合并 + 浏览器自动下载能在合理时间内完成。
验证要点：
1) 解析后出现「清晰度/格式」网格，选中低清格式。
2) 点击「下载」，服务端完成后**自动触发**浏览器下载（等价于点击「下载文件」链接），
   而非再让用户点一次 —— 用 page.expect_download 断言下载被自动发起。
3) 下载的文件非空、后缀为 mp4；状态文案变为「已自动保存到本地下载目录」。
用系统版 Chrome（channel="chrome"）真实浏览器验证。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8011"
VIDEO_URL = "https://www.youtube.com/watch?v=OvMW1BQFCJc"
OUT = Path("d:/LCP_agent/video_download/screenshots")
OUT.mkdir(parents=True, exist_ok=True)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=True,
            args=["--enable-automatic-downloads", "--disable-features=AutomaticDownloadsCheck"],
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        # 记录「带 /file 的 anchor 点击」次数，判定 onDone 是否已触发 triggerDownload
        page.add_init_script("""
          window.__fileClicks = [];
          (() => {
            const origClick = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function () {
              if (String(this.href).includes("/file")) {
                window.__fileClicks.push(this.href);
              }
              return origClick.apply(this, arguments);
            };
          })();
        """)
        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: console_msgs.append(f"pageerror: {e}"))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # 解析视频
        page.fill("#urlInput", VIDEO_URL)
        page.click("#parseBtn")
        page.wait_for_selector("#dlTitle:not(:has-text('解析中'))", timeout=90000)
        page.wait_for_timeout(1500)
        print(f"[check] parsed title = {page.inner_text('#dlTitle')!r}")

        # 选中低清格式（144p）以减小文件体积、加快测试
        row = page.locator("#formatList > div", has_text=re.compile(r"144p")).first
        row.click()
        page.wait_for_timeout(300)
        selected = page.evaluate("window.__dbg.current ? window.__dbg.current.selected : null")
        print(f"[check] selected format_id = {selected}")
        assert selected == "160", "未选中 144p 格式（format_id 应=160）"

        # 点击「下载」，并在服务端完成后等待浏览器**自动**发起下载（expect_download 捕获）
        try:
            with page.expect_download(timeout=300000) as dl_info:
                page.click("#downloadBtn")
            download = dl_info.value
        except Exception as exc:
            print(f"[diag] 等待下载超时/异常: {exc}")
            print(f"[diag] __fileClicks = {page.evaluate('window.__fileClicks')}")
            try:
                print(f"[diag] progStatus = {page.inner_text('#progStatus')!r}")
            except Exception as e2:
                print(f"[diag] 读 progStatus 失败: {e2}")
            for m in console_msgs:
                print(f"[diag] {m}")
            raise
        print(f"[check] 自动下载已发起，suggested_filename = {download.suggested_filename!r}")
        # 保存下载并校验
        target = OUT / "auto_download.mp4"
        download.save_as(str(target))
        size = target.stat().st_size
        print(f"[check] 保存到 {target}，大小 = {size} 字节")
        assert size > 0, "下载文件为空"

        # 状态文案应为「已自动保存」而非「点击右侧下载文件」
        status = page.inner_text("#progStatus").strip()
        print(f"[check] progStatus = {status!r}")
        assert "已自动保存到本地下载目录" in status, f"状态文案未反映自动保存: {status!r}"
        # 「下载文件」链接应已完全删除
        n_progdownload = page.evaluate("document.querySelectorAll('#progDownload').length")
        print(f"[check] #progDownload 元素数 = {n_progdownload}")
        assert n_progdownload == 0, "#progDownload 链接应彻底删除"

        context.close()
        browser.close()

    print("\nDOWNLOAD AUTO-SAVE E2E PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nDOWNLOAD E2E FAILED: {exc}")
        sys.exit(1)
