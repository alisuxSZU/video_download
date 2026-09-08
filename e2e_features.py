# -*- coding: utf-8 -*-
"""端到端特性验证（mock 后端）：翻译目标语言选择 + 各模块下载/导出。

用 page.route 拦截 /api/*（不依赖外网 YouTube/DeepSeek），仅需能加载 CDN 脚本的网络。
覆盖：
  1) 翻译目标语言选项（简体/繁体/英文/日语/朝鲜语）存在且默认选中简体。
  2) 翻译按所选语言生成；下载文件名含目标语言；切换语言后「重新生成」重翻到新语言。
  3) 章节时间轴生成 + 下载 .md（内容含章节标题/要点）。
  4) 摘要渲染 + 下载 .md。
  5) 问答 SSE → 气泡 + 导出对话 .md。
  6) 全程无控制台报错。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8033"
SHOTS = Path("d:/LCP_agent/video_download/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)

FAKE_URL = "https://example.com/watch?v=AAAA"
# 过滤：网络资源加载失败（CDN 抖动）、favicon、404 等非本应用代码错误
BAD_KEYS = ("favicon", "Failed to load resource", "ERR_", "404", "net::")

PARSE_BODY = {
    "ok": True, "title": "Mock 视频", "thumbnail": None, "duration": 600,
    "extractor": "Generic", "webpage_url": FAKE_URL, "ffmpeg": True,
    "formats": [{
        "format_id": "22", "ext": "mp4", "resolution": "720p", "height": 720,
        "fps": 30, "vcodec": "avc1", "acodec": "mp4a", "filesize": 123,
        "needs_merge": False, "progressive": True, "note": "高清",
    }],
    "subtitles": [],
}


def install_mocks(page):
    page.route("**/api/parse", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(PARSE_BODY, ensure_ascii=False)))

    def sub(r):
        body = json.loads(r.request.post_data or "{}")
        d = {"ok": True, "lang": "zh", "source": "manual", "format": "srt",
             "content": "1\n00:00:00,000 --> 00:00:01,000\n你好\n"}
        if body.get("target_lang"):
            d["translated"] = f"【{body['target_lang']}】这是译文内容 hello world"
        r.fulfill(status=200, content_type="application/json", body=json.dumps(d, ensure_ascii=False))
    page.route("**/api/subtitles", sub)

    def chap(r):
        r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True,
            "chapters": [
                {"start": 0, "end": 213, "title": "章节一", "summary": "第一段摘要",
                 "key_points": ["要点A", "要点B"], "keywords": ["k1"]},
                {"start": 214, "end": 400, "title": "章节二", "summary": "第二段摘要",
                 "key_points": ["要点C"], "keywords": ["k2"]},
            ],
            "lang": "zh", "used_source": "manual", "is_auto": False, "model": "m",
        }, ensure_ascii=False))
    page.route("**/api/ai/chapters", chap)

    def summ(r):
        r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "theme": "主题T", "overview": "总览O", "chapters": [], "keywords": ["k"],
            "summary": "# 主题T\n\n总览O\n\n## 要点\n- 要点\n",
            "lang": "zh", "is_auto": False, "used_source": "manual", "model": "m",
        }, ensure_ascii=False))
    page.route("**/api/ai/summary", summ)

    def ask(r):
        r.fulfill(status=200, content_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        }, body=(
            "data: {\"status\":\"preparing\"}\n\n"
            "data: {\"status\":\"generating\"}\n\n"
            "data: {\"delta\":\"这是\"}\n\n"
            "data: {\"delta\":\"Mock 回答。\"}\n\n"
            "data: {\"done\":true}\n\n"))
    page.route("**/api/ai/ask", ask)


def set_lang(page, lang):
    page.evaluate("v => { document.querySelector('#subLangSel').value = v; }", lang)
    assert page.evaluate("document.querySelector('#subLangSel').value") == lang


def wait_busy(page, ms=20000):
    page.locator("#anLoading").wait_for(state="hidden", timeout=ms)
    page.wait_for_timeout(300)


def run():
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"PAGEERROR: {e}"))
        install_mocks(page)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # 1) 语言选项
        langs = page.evaluate("Array.from(document.querySelectorAll('#subLangSel option')).map(o=>({v:o.value,s:o.selected}))")
        print(f"[check] 翻译目标语言选项 = {langs}")
        assert [x["v"] for x in langs] == ["简体中文", "繁体中文", "英文", "日语", "朝鲜语"], langs
        assert langs[0]["s"] is True, "默认语言应为简体中文"

        # 2) 解析（mock）
        page.fill("#urlInput", FAKE_URL)
        page.click("#parseBtn")
        page.wait_for_selector("#dlTitle:not(:has-text('解析中'))", timeout=20000)
        page.wait_for_timeout(600)
        assert page.evaluate("document.querySelector('#analyze').hidden") is False, "解析后模块未显示"

        # 3) 提取原字幕（合并后的「🎬 字幕」按钮 → 原字幕视图；无目标语言）
        page.click("#subBtn")
        wait_busy(page)
        mode = page.inner_text("#subModeLabel")
        sub_orig = page.inner_text("#subText")
        print(f"[check] 原字幕 mode={mode!r} 内容={sub_orig[:40]!r}")
        assert mode == "原字幕", f"提取后模式标签应为原字幕: {mode!r}"
        assert "你好" in sub_orig, f"原字幕内容未显示: {sub_orig!r}"
        dln = page.evaluate("document.querySelector('#subDl').getAttribute('download')")
        print(f"[check] 原字幕下载文件名 = {dln}")
        assert dln.startswith("字幕."), f"原字幕文件名应为 字幕.<ext>: {dln}"

        # 4) 翻译为「英文」：选语言 → 点面板内「翻译」即翻
        set_lang(page, "英文")
        page.click("#subTranslateBtn")
        wait_busy(page)
        mode2 = page.inner_text("#subModeLabel")
        meta = page.inner_text("#subMeta")
        subtext = page.inner_text("#subText")
        print(f"[check] 翻译(英文) mode={mode2!r} meta={meta!r} 内容={subtext[:50]!r}")
        assert mode2 == "已翻译为：英文", f"翻译后模式标签应为英文: {mode2!r}"
        assert "【英文】" in subtext, f"翻译未使用所选语言(英文): {subtext!r}"
        dlname = page.evaluate("document.querySelector('#subDl').getAttribute('download')")
        print(f"[check] 字幕下载文件名 = {dlname}")
        assert dlname.startswith("字幕-英文."), f"文件名未含目标语言: {dlname}"
        with page.expect_download() as dl:
            page.click("#subDl")
        d = dl.value
        print(f"[check] 字幕下载事件 filename={d.suggested_filename}")
        assert d.suggested_filename == dlname, (d.suggested_filename, dlname)
        c1 = Path(d.path()).read_text(encoding="utf-8")
        print(f"[check] 字幕下载内容(前30)={c1[:30]!r}")
        assert "【英文】" in c1, "字幕下载内容未含译文"

        # 5) 切回「🎬 字幕」回原字幕；切语言「日语」+翻译 → 重翻到日语
        page.click("#subBtn")
        wait_busy(page)
        assert page.inner_text("#subModeLabel") == "原字幕", "点「字幕」应回到原字幕视图"
        set_lang(page, "日语")
        page.click("#subTranslateBtn")
        wait_busy(page)
        subtext2 = page.inner_text("#subText")
        print(f"[check] 重翻(日语) 内容={subtext2[:50]!r}")
        assert "【日语】" in subtext2, f"切换语言后点翻译未重翻: {subtext2!r}"

        # 5) 章节：生成 + 下载 .md
        page.click("#chaptersBtn")
        wait_busy(page)
        n = page.evaluate("document.querySelectorAll('#sumChapters > *').length")
        print(f"[check] 章节渲染卡片数 = {n}")
        assert n >= 2, "章节卡片未渲染"
        with page.expect_download() as dl:
            page.click("#chaptersDownload")
        cd = dl.value
        print(f"[check] 章节下载 filename={cd.suggested_filename}")
        assert cd.suggested_filename == "章节时间轴.md", cd.suggested_filename
        cc = Path(cd.path()).read_text(encoding="utf-8")
        print(f"[check] 章节md前80 = {cc[:80]!r}")
        assert "章节一" in cc and "要点A" in cc, "章节下载缺少标题/要点"

        # 6) 摘要：渲染 + 下载 .md
        page.click("#sumBtn")
        wait_busy(page)
        summd = page.inner_text("#sumMd").strip()
        print(f"[check] 摘要md长度 = {len(summd)}")
        assert len(summd) > 5, f"摘要未渲染: {summd!r}"
        with page.expect_download() as dl:
            page.click("#sumDownload")
        sd = dl.value
        print(f"[check] 摘要下载 filename={sd.suggested_filename}")
        assert sd.suggested_filename == "video-summary.md", sd.suggested_filename
        sc = Path(sd.path()).read_text(encoding="utf-8")
        assert "主题T" in sc, "摘要下载内容缺失"

        # 7) 问答：SSE → 气泡 + 导出对话
        page.click("#askOpenBtn")
        page.wait_for_timeout(200)
        page.fill("#askInput", "这个视频讲了什么？")
        page.click("#askBtn")
        page.wait_for_function(
            "() => { const el = document.querySelector('#askOutput'); return el && el.textContent.trim().length > 5; }",
            timeout=20000)
        page.wait_for_function("() => document.querySelector('#askBtn').textContent === '发送'", timeout=20000)
        page.wait_for_timeout(300)
        aq = page.inner_text("#askOutput")
        print(f"[check] 问答输出 = {aq[:60]!r}")
        assert "Mock 回答" in aq, f"问答 SSE 未渲染: {aq!r}"
        with page.expect_download() as dl:
            page.click("#askExport")
        ad = dl.value
        print(f"[check] 问答导出 filename={ad.suggested_filename}")
        assert ad.suggested_filename == "视频问答记录.md", ad.suggested_filename
        ac = Path(ad.path()).read_text(encoding="utf-8")
        print(f"[check] 问答导出前80 = {ac[:80]!r}")
        assert "这个视频讲了什么" in ac and "Mock 回答" in ac, "问答导出未含问题/回答"

        page.screenshot(path=str(SHOTS / "e2e_features.png"), full_page=True)
        browser.close()

    real = [e for e in console_errors if not any(k in e for k in BAD_KEYS)]
    print("\n=== console errors ===")
    for e in real:
        print("  ", e)
    if real:
        print(f"CONSOLE ERRORS PRESENT: {len(real)}")
        sys.exit(2)
    print("E2E FEATURES PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # 不捕获 SystemExit，让 run() 内的 sys.exit 正常生效
        print(f"\nE2E FEATURES FAILED: {exc}")
        sys.exit(1)
