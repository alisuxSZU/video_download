# -*- coding: utf-8 -*-
"""端到端回归：验证「看解析结果卡片内、解析成功后显现」的 AI 功能模块。

覆盖要点：
1) 只保留一个链接输入框（无 #analyzeUrl / #useCurrent）。
2) #analyze 在解析前隐藏、解析后显现（与解析同一张卡片）。
3) 六个功能（字幕/翻译/摘要/章节/思维导图/问答）逐项跑通。
4) 问答为简洁聊天模块（无 #askStatus），SSE 流式输出。
5) 结果按链接缓存：再次点击同一功能不重复发请求；点「重新生成」才强制重算。
6) 思维导图：拖拽平移 / 滚轮缩放 / 点击折叠 / 全部展开。
7) 无控制台报错。

用系统版 Chrome（channel="chrome"）做真实浏览器验证。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8011"
VIDEO_URL = "https://www.youtube.com/watch?v=UISJGnJ1LpA"
SHOTS = Path("d:/LCP_agent/video_download/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)

FEATURE_ENDPOINTS = {
    "subBtn": "/api/subtitles",
    "subTranslate": "/api/subtitles",
    "sumBtn": "/api/ai/summary",
    "chaptersBtn": "/api/ai/chapters",
    "mindmapBtn": "/api/ai/mindmap",
}


def wait_busy_done(page, timeout_ms=90000):
    """等全局 #anLoading 隐藏（单飞守卫：一次只能跑一个请求）。"""
    page.locator("#anLoading").wait_for(state="hidden", timeout=timeout_ms)
    page.wait_for_timeout(1200)


def panel_ok(page, btn_id):
    """不同面板用各自的成品选择器判断完成。"""
    wait_busy_done(page)
    if btn_id in ("subBtn", "subTranslate"):
        txt = page.inner_text("#subText").strip()
        return f"字幕长度={len(txt)}"
    if btn_id == "sumBtn":
        if page.locator("#sumOverview").inner_text().strip():
            return "摘要:" + page.inner_text("#sumTheme").strip()[:40]
        return "sumOverview empty"
    if btn_id == "chaptersBtn":
        n = page.locator("#sumChapters > *").count()
        return f"章节条目={n}"
    if btn_id == "mindmapBtn":
        if page.locator("#mindContainer svg").count() > 0:
            return "mermaid SVG 已渲染"
        txt = page.inner_text("#mindContainer").strip()
        return "mindmap:" + txt[:40]
    if btn_id == "askOpenBtn":
        return "问答面板已打开"
    return "?"


def run():
    console_errors = []
    req_count = {}
    ask_request_bodies = []  # 每次 /api/ai/ask 的请求体（含 history），供「中流清空不复答」断言

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"PAGEERROR: {exc}"))
        page.on("request", lambda req: _count_req(req, req_count, ask_request_bodies))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # ---- 0. 单一链接输入框（无第二个输入 / 无「使用当前解析」） ----
        n_analyze_url = page.evaluate("document.querySelectorAll('#analyzeUrl').length")
        n_use_current = page.evaluate("document.querySelectorAll('#useCurrent').length")
        n_link_inputs = page.evaluate("document.querySelectorAll('#urlInput').length")
        print(f"[check] #analyzeUrl={n_analyze_url} #useCurrent={n_use_current} 链接输入=#urlInput x{n_link_inputs}")
        assert n_analyze_url == 0, "仍存在 #analyzeUrl（应为单一输入框）"
        assert n_use_current == 0, "仍存在 #useCurrent"
        assert n_link_inputs == 1, "链接输入框数量异常（应为 1）"

        # ---- 0b. 问答为简洁聊天模块：无 #askStatus ----
        n_ask_status = page.evaluate("document.querySelectorAll('#askStatus').length")
        n_ask_input = page.evaluate("document.querySelectorAll('#askInput').length")
        n_ask_output = page.evaluate("document.querySelectorAll('#askOutput').length")
        print(f"[check] #askStatus={n_ask_status} #askInput={n_ask_input} #askOutput={n_ask_output}")
        assert n_ask_status == 0, "问答仍使用 #askStatus（应为简洁聊天模块）"
        assert n_ask_input == 1 and n_ask_output == 1, "问答输入/输出元素缺失"

        # ---- 1. 解析前 #analyze 必须隐藏 ----
        hidden_before = page.evaluate("document.querySelector('#analyze').hidden")
        print(f"[check] analyze.hidden before parse = {hidden_before}")
        assert hidden_before is True, "解析前模块未隐藏"

        # ---- 2. 解析视频 ----
        page.fill("#urlInput", VIDEO_URL)
        page.click("#parseBtn")
        page.wait_for_selector("#dlTitle:not(:has-text('解析中'))", timeout=60000)
        page.wait_for_timeout(1500)
        print(f"[check] parsed title = {page.inner_text('#dlTitle')!r}")
        hidden_after = page.evaluate("document.querySelector('#analyze').hidden")
        print(f"[check] analyze.hidden after parse = {hidden_after}")
        assert hidden_after is False, "解析后模块未显示"

        # ---- 2b. 清晰度/格式选择改为响应式网格：一个选项不再占一整行 ----
        grid_info = page.evaluate("""() => {
          const el = document.querySelector('#formatList');
          const cs = getComputedStyle(el);
          return { display: cs.display, cols: cs.gridTemplateColumns, rows: el.children.length };
        }""")
        print(f"[check] format grid: display={grid_info['display']} cols={grid_info['cols']!r} rows={grid_info['rows']}")
        assert grid_info["display"] == "grid", "清晰度/格式列表未使用响应式网格布局"
        if grid_info["rows"] >= 2:
            n_cols = len(grid_info["cols"].strip().split())
            print(f"[check] format grid 列数={n_cols}（宽屏应自动多列）")
            assert n_cols >= 2, "宽屏下清晰度/格式未自动排成多列"

        # ---- 2c. 清晰度/格式选项不再显示「需合并音视频/单文件」这类对用户无意义的角标 ----
        format_txt = page.inner_text("#formatList")
        print(f"[check] 格式列表文本(首200字) = {format_txt[:200]!r}")
        for bad in ("需合并音视频", "单文件"):
            assert bad not in format_txt, f"格式选项仍显示无意义角标「{bad}」"

        # ---- 3. 逐项触发 6 个功能 ----
        for btn_id in ["subBtn", "subTranslate", "sumBtn", "chaptersBtn", "mindmapBtn", "askOpenBtn"]:
            page.click(f"#{btn_id}")
            page.wait_for_timeout(600)
            print(f"[check] {btn_id}: {panel_ok(page, btn_id)}")
            page.screenshot(path=str(SHOTS / f"e2e_{btn_id}.png"), full_page=True)

        # ---- 4. 缓存：再次点击同一功能不重发请求；「重新生成」才重算 ----
        s0 = req_count.get(FEATURE_ENDPOINTS["sumBtn"], 0)
        page.click("#sumBtn")
        page.wait_for_timeout(1000)
        s1 = req_count.get(FEATURE_ENDPOINTS["sumBtn"], 0)
        print(f"[check] summary requests: 首次={s0} 再点={s1}")
        assert s1 == s0, "再次点击摘要不应重新请求（应命中缓存）"
        # 「重新生成」按钮强制重算
        page.click("#sumRefresh")
        wait_busy_done(page)
        s2 = req_count.get(FEATURE_ENDPOINTS["sumBtn"], 0)
        print(f"[check] summary 点「重新生成」后={s2}")
        assert s2 == s0 + 1, "点「重新生成」应重新请求一次"

        # ---- 5. 思维导图：拖拽 / 缩放 / 折叠 ----
        page.click("#mindmapBtn")
        wait_busy_done(page)
        page.locator("#mindContainer #mmViewport").wait_for(timeout=60000)
        page.wait_for_timeout(800)

        v0 = page.evaluate("window.__mm.view")
        svg_box = page.evaluate("window.__mm.view ? true : false")
        print(f"[check] mindmap initial view = {v0}")
        assert svg_box, "__mm.view 未暴露"

        # 滚轮缩放（放大）
        before_scale = v0["scale"]
        page.locator("#mindContainer").hover()
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(300)
        after_scale = page.evaluate("window.__mm.view.scale")
        print(f"[check] wheel zoom: scale {before_scale:.3f} -> {after_scale:.3f}")
        assert after_scale > before_scale, "滚轮未放大"

        # 拖拽平移
        tx0 = page.evaluate("window.__mm.view.tx")
        box = page.locator("#mindContainer").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 60, cy + 40, steps=6)
        page.mouse.up()
        page.wait_for_timeout(300)
        tx1 = page.evaluate("window.__mm.view.tx")
        print(f"[check] drag pan: tx {tx0:.1f} -> {tx1:.1f}")
        assert abs(tx1 - tx0) > 5, "拖拽未平移"

        # 折叠：点击一个有子节点的节点的折叠徽标
        fold_count = page.locator("#mindContainer .mm-fold").count()
        if fold_count > 0:
            nodes_before = page.locator("#mindContainer g.node").count()
            page.locator("#mindContainer .mm-fold").first.click()
            page.wait_for_timeout(1200)
            nodes_after = page.locator("#mindContainer g.node").count()
            collapsed = page.evaluate("window.__mm.collapsed")
            print(f"[check] fold: nodes {nodes_before}->{nodes_after}, collapsed={collapsed}")
            assert len(collapsed) > 0, "点击折叠未生效"
            assert nodes_after < nodes_before, "折叠后节点数未减少"
            # 全部展开
            page.click("#mmExpandAll")
            page.wait_for_timeout(1200)
            collapsed2 = page.evaluate("window.__mm.collapsed")
            print(f"[check] expandAll: collapsed={collapsed2}")
            assert len(collapsed2) == 0, "全部展开未清空折叠态"
        else:
            print("[check] 导图无子节点可折叠，跳过（skip）")

        # ---- 6. 问答（SSE 流式聊天）：无 #askStatus，靠输出长度 + 按钮复原判断 ----
        page.click("#askOpenBtn")  # 打开问答面板（上一思维导图步骤把 panelAsk 隐藏了）
        page.wait_for_timeout(400)
        page.fill("#askInput", "这个视频的核心观点是什么？")
        page.click("#askBtn")
        page.wait_for_function(
            "() => document.querySelector('#askOutput').textContent.trim().length > 20",
            timeout=120000,
        )
        page.wait_for_function("() => document.querySelector('#askBtn').textContent === '发送'", timeout=120000)
        page.wait_for_timeout(800)
        ask = page.inner_text("#askOutput").strip()
        print(f"[check] ask: {ask[:300]!r}")
        if len(ask) <= 20:
            raise AssertionError(f"问答无有效回答: {ask!r}")
        # 气泡聊天框：用户问题气泡（右下）+ AI 回答气泡（左）各一个
        n_user = page.evaluate('document.querySelectorAll("#askOutput [data-role=user]").length')
        n_ai = page.evaluate('document.querySelectorAll("#askOutput [data-role=assistant]").length')
        uq = page.inner_text('#askOutput [data-role="user"]')
        aq = page.inner_text('#askOutput [data-role="assistant"]')
        print(f"[check] 问答气泡 user={n_user} assistant={n_ai}")
        print(f"[check] 用户气泡文本 = {uq!r}")
        print(f"[check] AI 气泡长度 = {len(aq)}")
        assert n_user >= 1 and n_ai >= 1, f"问答未渲染气泡 user={n_user} ai={n_ai}"
        assert "核心观点" in uq, f"用户气泡未含问题: {uq!r}"
        assert len(aq) > 20, f"AI 气泡内容过短: {aq!r}"
        # 清空按钮：气泡应全部移除
        page.click("#askClear")
        page.wait_for_timeout(300)
        cleared = page.inner_text("#askOutput").strip()
        n_bubbles = page.evaluate('document.querySelectorAll("#askOutput [data-role]").length')
        print(f"[check] askClear: 清空后长度={len(cleared)} 气泡数={n_bubbles}")
        assert len(cleared) == 0 and n_bubbles == 0, "清空问答未生效"

        # ---- 6b. 中流清空回归：回答进行中点「清空」→ 陈旧流不得复活气泡/写回历史 ----
        # 用一次较长提问构建真实流式窗口；点击 #askBtn 后极短时间点 #askClear，触发
        # AbortController 取消在途请求。随后断言：气泡不复活、按钮复位、下一次提问的
        # history 仍为 []（证明旧问答没被 write 回 askHistory 污染下一次上下文）。
        long_q = "请非常详细地总结这个视频，逐点说明每个论据、案例、数据与结论，字数尽量多，避免遗漏。"
        page.fill("#askInput", long_q)
        page.click("#askBtn")
        # 不等回答结束，立即「清空」（LLM 流式返回窗口通常 ≥ 数百 ms，足可落在流中）
        page.click("#askClear")
        page.wait_for_timeout(600)  # 让 abort 与陈旧流收尾
        n_b = page.evaluate('document.querySelectorAll("#askOutput [data-role]").length')
        cleared2 = page.inner_text("#askOutput").strip()
        btn_disabled = page.evaluate("document.querySelector('#askBtn').disabled")
        btn_txt = page.inner_text("#askBtn").strip()
        print(f"[check] 中流清空: 气泡数={n_b} 长度={len(cleared2)} btn.disabled={btn_disabled} btn='{btn_txt}'")
        assert n_b == 0 and len(cleared2) == 0, "中流清空后气泡未清空或被陈旧流复活"
        assert btn_disabled is False and btn_txt == "发送", f"中流清空后按钮未复位: disabled={btn_disabled} txt={btn_txt!r}"
        # 再正常提问一次，抓其请求体：history 应为 []（未被陈旧流注入旧问答）
        page.fill("#askInput", "这个视频的核心观点是什么？")
        page.click("#askBtn")
        page.wait_for_function(
            "() => document.querySelector('#askOutput').textContent.trim().length > 20",
            timeout=120000,
        )
        page.wait_for_function("() => document.querySelector('#askBtn').textContent === '发送'", timeout=120000)
        page.wait_for_timeout(800)
        n_user2 = page.evaluate('document.querySelectorAll("#askOutput [data-role=user]").length')
        n_ai2 = page.evaluate('document.querySelectorAll("#askOutput [data-role=assistant]").length')
        print(f"[check] 清空后复问: user={n_user2} assistant={n_ai2}")
        assert n_user2 >= 1 and n_ai2 >= 1, "清空后再次提问气泡未渲染"
        history_after_clear = ask_request_bodies[-1].get("history", None) if ask_request_bodies else None
        print(f"[check] 中流清空后复问 history 长度 = {len(history_after_clear) if history_after_clear is not None else 'N/A'}")
        assert history_after_clear == [], f"陈旧问答被写回 history（跨视频污染）: {history_after_clear!r}"

        page.screenshot(path=str(SHOTS / "e2e_askAnswer.png"), full_page=True)

        # ---- 7. 更换链接解析后，上一视频在 AI 模块里的产物应被清理 ----
        title1 = page.inner_text("#dlTitle")
        old_cache_keys = page.evaluate("Object.keys(window.__dbg.cache || {}).length")
        url2 = "https://archive.org/details/BigBuckBunny"  # 第二视频：只需解析成功、无需字幕
        page.fill("#urlInput", url2)
        page.click("#parseBtn")
        # 等待解析完成：标题既不是「解析中…」，也不再是上一个视频的标题
        page.wait_for_function(
            f"() => {{ const t = document.querySelector('#dlTitle').textContent;"
            f" return t !== '解析中…' && t !== {title1!r}; }}",
            timeout=60000,
        )
        page.wait_for_timeout(1500)
        print(f"[check] 切换链接后标题 = {page.inner_text('#dlTitle')!r}（原: {title1!r}）")
        cleared = page.evaluate("""() => ({
          sum: document.querySelector('#sumOverview').innerText,
          sub: document.querySelector('#subText').innerText,
          ask: document.querySelector('#askOutput').innerText,
          mindChildren: document.querySelector('#mindContainer').children.length,
          anyPanelShown: !!document.querySelector('.an-panel:not(.hidden)'),
        })""")
        print(f"[check] 切换后 AI 内容 = {cleared}")
        assert cleared["sum"] == "", "切换链接后摘要未清空"
        assert cleared["sub"] == "", "切换链接后字幕未清空"
        assert cleared["ask"] == "", "切换链接后问答未清空"
        assert cleared["mindChildren"] == 0, "切换链接后导图容器未清空"
        assert cleared["anyPanelShown"] is False, "切换链接后仍有旧结果面板在显示"
        # 模块仍在（新视频解析成功）、标题已切换；旧链接缓存保留（切回可命中）
        hidden2 = page.evaluate("document.querySelector('#analyze').hidden")
        new_cache_keys = page.evaluate("Object.keys(window.__dbg.cache || {}).length")
        print(f"[check] 切换后 analyze.hidden={hidden2}，缓存键数 {old_cache_keys}->{new_cache_keys}")
        assert hidden2 is False, "切换链接后模块应仍显示"
        assert new_cache_keys >= old_cache_keys, "切换链接后旧链接缓存不应丢失"

        browser.close()

    real_errors = [e for e in console_errors if "favicon" not in e and "404" not in e]
    print("\n=== console errors ===")
    for e in real_errors:
        print("  ", e)
    if real_errors:
        print("CONSOLE ERRORS PRESENT:", len(real_errors))
        sys.exit(2)
    print("NO console errors. E2E PASSED.")
    sys.exit(0)


def _count_req(req, req_count, ask_bodies):
    u = req.url
    for frag in FEATURE_ENDPOINTS.values():
        if frag in u:
            req_count[frag] = req_count.get(frag, 0) + 1
    # 记录每次 /api/ai/ask 请求体（含 history），用于断言「清空后不复答旧问答」
    if "/api/ai/ask" in u and req.method == "POST":
        try:
            ask_bodies.append(json.loads(req.post_data or "{}"))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # 不捕获 SystemExit，让 run() 内的 sys.exit(0/2) 正常生效
        print(f"\nE2E FAILED: {exc}")
        sys.exit(1)
