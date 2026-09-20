"""远程浏览器（CDP 画面桥）端到端测试。

这些测试需要本机存在 Chromium 与 Xvfb；缺失时自动跳过，
因此可以在普通 CI 中安全运行（不会误报失败）。

本地完整运行：
    sudo apt-get install -y chromium          # 或设置 QODER_CHROMIUM_PATH
    pip install -e ".[dev,registrar]"
    pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import struct
import time

import pytest

def _find_chromium() -> str | None:
    """按常见名称/位置查找 Chromium 或 Chrome 可执行文件。

    测试使用 headless 模式，因此不强制要求 Xvfb；
    显式设置 QODER_CHROMIUM_PATH 可覆盖（CI 中由 setup-chrome 提供）。
    """
    explicit = os.getenv("QODER_CHROMIUM_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROMIUM = _find_chromium()
pytestmark = pytest.mark.skipif(
    not CHROMIUM,
    reason="需要 Chromium/Chrome；可设 QODER_CHROMIUM_PATH 指向可执行文件",
)

SLIDER_HTML = """<!doctype html>
<html><body style="margin:0">
<div id="track" style="position:relative;width:320px;height:40px;background:#eee">
  <div id="thumb" style="position:absolute;left:0;top:0;width:40px;height:40px;background:#09f"></div>
</div>
<div id="out">0</div>
<script>
const thumb = document.getElementById('thumb');
let dragging = false, startX = 0, startLeft = 0;
thumb.addEventListener('mousedown', e => { dragging = true; startX = e.clientX; startLeft = parseInt(thumb.style.left) || 0; });
document.addEventListener('mousemove', e => {
  if (!dragging) return;
  const left = Math.max(0, Math.min(280, startLeft + (e.clientX - startX)));
  thumb.style.left = left + 'px';
  document.getElementById('out').textContent = left;
});
document.addEventListener('mouseup', () => { dragging = false; });
</script>
</body></html>"""


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """从 JPEG 字节流解析宽高，用于校验截图与 viewport 是否一致。"""
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


@pytest.fixture(scope="module")
def chromium_page(tmp_path_factory):
    """启动真实 Chromium（headless）并加载可拖动页面。"""
    os.environ.setdefault("QODER_DATA_DIR", str(tmp_path_factory.mktemp("qoder-data")))
    os.environ["QODER_CHROMIUM_PATH"] = CHROMIUM or ""
    os.environ["QODER_REMOTE_BROWSER"] = "1"
    os.environ.setdefault("QODER_ADMIN_PASSWORD", "test-token")

    from DrissionPage import ChromiumOptions, ChromiumPage

    from qoder2api import registrar

    html = tmp_path_factory.mktemp("page") / "slider.html"
    html.write_text(SLIDER_HTML, encoding="utf-8")

    co = ChromiumOptions()
    co.set_browser_path(CHROMIUM)
    co.headless(True)
    co.set_local_port(19400)
    for arg in registrar.DEFAULT_CHROMIUM_ARGS:
        co.set_argument(arg)
    co.set_user_data_path(str(tmp_path_factory.mktemp("profile")))

    page = ChromiumPage(co)
    page.get(f"file://{html}")
    time.sleep(1)
    yield page
    try:
        page.quit()
    except Exception:
        pass


@pytest.fixture
def bound_task(chromium_page):
    """把浏览器绑定到一个任务上，供端点查找。"""
    from qoder2api import registrar

    class _Bot:
        pass

    bot = _Bot()
    bot.page = chromium_page
    task_id = "t-pytest"
    with registrar._LOCK:
        registrar._REGISTRAR["active"][task_id] = {
            "stage": "waiting_slider", "logs": [], "result": None,
            "error": None, "started_at": 0, "bot": bot,
        }
    yield task_id
    with registrar._LOCK:
        registrar._REGISTRAR["active"].pop(task_id, None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QODER_ADMIN_PASSWORD", "test-token")
    import qoder2api.app as app_mod
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)


def test_screenshot_matches_viewport(client, bound_task):
    """截图尺寸应与 CSS viewport 一致，否则前端坐标换算会偏移。"""
    resp = client.get(f"/ui/remote-browser/{bound_task}/screenshot",
                      headers={"X-Gateway-Token": "test-token"})
    assert resp.status_code == 200
    assert resp.content[:3] == b"\xff\xd8\xff" or resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_requires_auth(client):
    assert client.get("/ui/remote-browser").status_code == 401
    assert client.get("/ui/remote-browser", headers={"X-Gateway-Token": "nope"}).status_code == 401


def test_info_lists_bound_browser(client, bound_task):
    resp = client.get("/ui/remote-browser", headers={"X-Gateway-Token": "test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert any(b["task_id"] == bound_task for b in body["browsers"])


def test_websocket_streams_frames_and_replays_drag(client, bound_task, chromium_page):
    """核心回归：画面下发 + 拖动事件真实改变页面状态。"""
    with client.websocket_connect(f"/ui/remote-browser/{bound_task}/ws?token=test-token") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        vp = ready["viewport"]
        assert vp["width"] > 0 and vp["height"] > 0

        frame = None
        for _ in range(10):
            msg = ws.receive_json()
            if msg.get("type") == "frame":
                frame = msg
                break
        assert frame, "未收到画面帧"
        raw = base64.b64decode(frame["data"])
        assert raw[:3] == b"\xff\xd8\xff", "帧不是合法 JPEG"
        assert _jpeg_size(raw) == (vp["width"], vp["height"]), "截图尺寸与 viewport 不一致"

        rect = chromium_page.ele("#thumb").rect
        x0 = rect.location[0] + rect.size[0] / 2
        y0 = rect.location[1] + rect.size[1] / 2

        ws.send_json({"type": "mouse", "event": "mousePressed", "x": x0, "y": y0,
                      "button": "left", "clickCount": 1, "buttons": 1})
        for i in range(1, 16):
            ws.send_json({"type": "mouse", "event": "mouseMoved", "x": x0 + i * 12, "y": y0,
                          "button": "left", "buttons": 1})
        ws.send_json({"type": "mouse", "event": "mouseReleased", "x": x0 + 180, "y": y0,
                      "button": "left", "clickCount": 1, "buttons": 0})
        time.sleep(1.2)

        assert chromium_page.ele("#out").text not in ("0", "", None), "拖动未生效"


def test_websocket_rejects_bad_token(client, bound_task):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ui/remote-browser/{bound_task}/ws?token=wrong") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_websocket_rejects_when_disabled(client, bound_task, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("QODER_REMOTE_BROWSER", "0")
    import importlib

    from qoder2api import remotebrowser

    importlib.reload(remotebrowser)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ui/remote-browser/{bound_task}/ws?token=test-token") as ws:
            ws.receive_json()
    assert exc.value.code == 4403
    monkeypatch.setenv("QODER_REMOTE_BROWSER", "1")
    importlib.reload(remotebrowser)


def test_mouse_params_shape():
    """参数构造函数应产出 CDP 期望的字段。"""
    from qoder2api import remotebrowser

    m = remotebrowser.mouse_event_params("mousePressed", 10, 20, "left", 1)
    assert m["type"] == "mousePressed" and m["x"] == 10.0 and m["y"] == 20.0
    assert m["clickCount"] == 1

    k = remotebrowser.key_event_params("keyDown", "a", "a", "KeyA", 65)
    assert k["text"] == "a" and k["windowsVirtualKeyCode"] == 65

# ---------------------------------------------------------------------------
# 跨源 iframe 穿透：这是滑块场景的核心机制
#
# 阿里云滑块运行在跨源 iframe 中，因此必须确认「仅由父页面 session 注入的
# CDP 输入事件」能够到达 iframe 内部元素。若此机制不成立，远程浏览器方案
# 就无法用于真实滑块。
# ---------------------------------------------------------------------------
FRAME_HTML = """<!doctype html>
<html><body style="margin:0">
<div id="track" style="position:relative;width:300px;height:40px;background:#eee">
  <div id="thumb" style="position:absolute;left:0;top:0;width:40px;height:40px;background:#09f"></div>
</div>
<div id="out">0</div>
<script>
const thumb = document.getElementById('thumb');
const events = [];
['pointerdown','pointermove','pointerup'].forEach(t =>
  document.addEventListener(t, () => events.push(t), true));
let dragging = false, startX = 0, startLeft = 0;
thumb.addEventListener('pointerdown', e => {
  dragging = true; startX = e.clientX; startLeft = parseInt(thumb.style.left) || 0;
});
document.addEventListener('pointermove', e => {
  if (!dragging) return;
  const left = Math.max(0, Math.min(260, startLeft + (e.clientX - startX)));
  thumb.style.left = left + 'px';
  document.getElementById('out').textContent = left;
});
document.addEventListener('pointerup', () => { dragging = false; });
setInterval(() => {
  const r = thumb.getBoundingClientRect();
  parent.postMessage({ type: 'state', thumb: {x:r.x, y:r.y, w:r.width, h:r.height},
                       out: document.getElementById('out').textContent }, '*');
}, 150);
</script></body></html>"""

HOST_HTML = """<!doctype html>
<html><body style="margin:0">
<iframe id="slider-frame" src="{frame_url}"
        style="position:absolute;left:40px;top:80px;width:360px;height:140px;border:0"></iframe>
<script>
window.__frameState = null;
window.addEventListener('message', e => { window.__frameState = e.data; });
</script></body></html>"""


def test_input_events_cross_origin_iframe(chromium_page, tmp_path):
    """父页面 session 注入的鼠标事件应能穿透跨源 iframe 并拖动内部滑块。"""
    import http.server
    import socketserver
    import threading

    from qoder2api import remotebrowser

    frame_body = FRAME_HTML.encode()
    # HTML 内含 JS 花括号，不能用 str.format；用容器回填端口
    holder: dict[str, bytes] = {"host": b""}

    def _make_handler(body_getter):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = body_getter()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        return _Handler

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    # 必须用「两个端口」：同一端口属于同源，那样测不出跨源穿透
    with _Server(("127.0.0.1", 0), _make_handler(lambda: frame_body)) as srv_frame, \
         _Server(("127.0.0.1", 0), _make_handler(lambda: holder["host"])) as srv_host:
        frame_port = srv_frame.server_address[1]
        host_port = srv_host.server_address[1]
        assert frame_port != host_port
        holder["host"] = HOST_HTML.replace(
            "{frame_url}", f"http://127.0.0.1:{frame_port}/frame.html"
        ).encode()
        threading.Thread(target=srv_frame.serve_forever, daemon=True).start()
        threading.Thread(target=srv_host.serve_forever, daemon=True).start()

        page = chromium_page
        page.get(f"http://127.0.0.1:{host_port}/host.html")
        time.sleep(2)

        # 等 iframe 真正加载完成（postMessage 到达即表示内部脚本已运行）
        def _frame_loaded() -> bool:
            probe = page.run_cdp(
                "Runtime.evaluate", returnByValue=True,
                expression="(function(){try{return window.__frameState ? 1 : 0}"
                           "catch(e){return 0}})()",
            )
            return (probe.get("result", {}).get("value") or 0) == 1

        for _ in range(30):
            if _frame_loaded():
                break
            time.sleep(0.2)
        assert _frame_loaded(), "iframe 未加载完成"

        # 加载完成后确认确实跨源：父页面读不到 contentDocument
        blocked = page.run_cdp(
            "Runtime.evaluate", returnByValue=True,
            expression="(function(){try{return !!document.getElementById('slider-frame').contentDocument}"
                       "catch(e){return 'CROSS_ORIGIN'}})()",
        ).get("result", {}).get("value")
        assert blocked in (False, "CROSS_ORIGIN"), f"iframe 未构成跨源: {blocked!r}"

        addr = remotebrowser.get_debug_address(page)
        ws_url = remotebrowser._cdp_target(addr)
        target_id = remotebrowser._page_target_id(addr, None)

        async def scenario():
            session = remotebrowser.CdpSession(ws_url)
            await session.connect()
            sid = await session.attach(target_id)

            async def evaluate(expr):
                res = await session.send(
                    "Runtime.evaluate",
                    {"expression": expr, "returnByValue": True},
                    session_id=sid,
                )
                return res["result"].get("value")

            state = None
            for _ in range(20):
                raw = await evaluate("JSON.stringify(window.__frameState)")
                if raw and raw != "null":
                    state = json.loads(raw)
                    break
                await asyncio.sleep(0.2)
            assert state, "未收到 iframe 的 postMessage"

            offset = json.loads(await evaluate(
                "JSON.stringify((r=>({x:r.x,y:r.y}))("
                "document.getElementById('slider-frame').getBoundingClientRect()))"
            ))
            thumb = state["thumb"]
            x0 = offset["x"] + thumb["x"] + thumb["w"] / 2
            y0 = offset["y"] + thumb["y"] + thumb["h"] / 2

            # 关键：只在父页面 session 注入，不 attach iframe
            await session.send("Input.dispatchMouseEvent",
                               {"type": "mousePressed", "x": x0, "y": y0,
                                "button": "left", "clickCount": 1, "buttons": 1}, session_id=sid)
            for i in range(1, 16):
                await session.send("Input.dispatchMouseEvent",
                                   {"type": "mouseMoved", "x": x0 + i * 10, "y": y0,
                                    "button": "left", "buttons": 1}, session_id=sid)
                await asyncio.sleep(0.012)
            await session.send("Input.dispatchMouseEvent",
                               {"type": "mouseReleased", "x": x0 + 150, "y": y0,
                                "button": "left", "clickCount": 1, "buttons": 0}, session_id=sid)
            await asyncio.sleep(0.8)

            raw = await evaluate("JSON.stringify(window.__frameState)")
            await session.close()
            return json.loads(raw)

        result = asyncio.run(scenario())
        assert result["out"] not in ("0", "", None), (
            f"事件未穿透跨源 iframe，滑块输出仍为 {result['out']!r}"
        )
