"""
远程浏览器（CDP 画面桥）：把注册机的 Chromium 画面实时推到 Web 控制台，
并把控制台里的鼠标/键盘事件回放到该浏览器。

设计取舍：
  - 不引入 VNC。DrissionPage 本身就是通过 CDP 驱动 Chromium 的，
    这里复用同一条调试通道（page.address 指向的 devtools 端口），
    额外开销仅为截图与事件转发。
  - 后端用一条 WebSocket 同时承载「画面下行」与「事件上行」，
    前端一个 <canvas> 即可渲染，无需 noVNC 那套 RFB 协议。
  - 截图走 CDP Page.captureScreenshot；输入走 Input.dispatchMouseEvent /
    Input.dispatchKeyEvent，坐标以 CSS 像素为单位，与前端 canvas 对齐。

限制：
  - 该通道具备浏览器完全控制能力，等同于把注册机浏览器交给控制台使用者，
    因此接口必须仅在管理员鉴权后开放。
  - 帧率受限于截图耗时，默认 8 FPS，足够看清页面并拖动滑块。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from typing import Any

from urllib.parse import urlparse, urlunparse

import httpx

from .env import (
    remote_browser_enabled,
    remote_browser_fps,
    remote_browser_quality,
)


class RemoteBrowserError(RuntimeError):
    pass


def _pin_ws_host(ws_url: str, debug_address: str) -> str:
    """把 CDP WebSocket URL 的 host:port 固定为调试端口实际监听的地址。

    Chromium 的 /json/version 常返回 ws://localhost:PORT/devtools/browser/...，
    但容器里 localhost 可能优先解析到 IPv6 ::1，而 Chromium 只监听 IPv4
    127.0.0.1 —— WebSocket 握手会一直挂起直到超时
    （表现为 "timed out during opening handshake"）。

    这里统一改写成我们刚刚确认可达的调试地址，避免依赖容器内的名字解析。
    """
    try:
        target = urlparse(
            debug_address if "://" in debug_address else f"http://{debug_address}"
        )
        parsed = urlparse(ws_url)
        host = target.hostname or "127.0.0.1"
        port = target.port or parsed.port
        netloc = f"{host}:{port}" if port else host
        return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        return ws_url


def _cdp_target(debug_address: str) -> str:
    """把 DrissionPage 的调试地址解析为可直接发 CDP 的 WebSocket URL。"""
    address = (debug_address or "").strip()
    if not address:
        raise RemoteBrowserError("浏览器调试地址为空")
    if not address.startswith("http"):
        address = f"http://{address}"
    address = address.rstrip("/")

    # /json/version 返回 browser 级 webSocketDebuggerUrl，
    # 用它建 Browser 会话后可按 targetId 附加到具体页面。
    #
    # trust_env=False：调试端口是本机回环，绝不应走代理；同时规避
    # HTTP_PROXY/no_proxy（含 [::1] 时 httpx 会抛 InvalidURL）的干扰。
    try:
        response = httpx.get(f"{address}/json/version", timeout=5, trust_env=False)
        response.raise_for_status()
        info = response.json()
    except Exception as exc:
        raise RemoteBrowserError(f"无法连接浏览器调试端口 {address}: {exc}") from exc

    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RemoteBrowserError(f"调试端口未返回 webSocketDebuggerUrl: {info}")
    # 固定 host:port，规避 localhost -> IPv6 导致的握手超时
    return _pin_ws_host(ws_url, address)


def _page_target_id(debug_address: str, tab_id: str | None) -> str | None:
    """找到要投屏的页面 targetId。"""
    address = debug_address if debug_address.startswith("http") else f"http://{debug_address}"
    try:
        response = httpx.get(f"{address.rstrip('/')}/json/list", timeout=5, trust_env=False)
        response.raise_for_status()
        targets = response.json()
    except Exception as exc:
        raise RemoteBrowserError(f"无法列出浏览器页面: {exc}") from exc

    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RemoteBrowserError("浏览器中没有可用页面")
    if tab_id:
        for page in pages:
            if page.get("id") == tab_id:
                return page["id"]
    # 优先返回当前激活的页面
    for page in pages:
        if page.get("url") and not page["url"].startswith("devtools://"):
            return page["id"]
    return pages[0]["id"]


def get_debug_address(page: Any) -> str:
    """从 DrissionPage 页面对象取调试地址（容器内为 127.0.0.1:<port>）。"""
    try:
        return str(page.address)
    except Exception as exc:
        raise RemoteBrowserError(f"无法获取浏览器调试地址: {exc}") from exc


def snapshot(page: Any, quality: int | None = None) -> bytes:
    """取一张当前页面截图（PNG/JPEG 字节）。"""
    try:
        data = page.get_screenshot(as_bytes="png")
    except TypeError:
        data = page.get_screenshot(as_bytes=True)
    except Exception as exc:
        raise RemoteBrowserError(f"截图失败: {exc}") from exc
    if isinstance(data, bytes):
        return data
    raise RemoteBrowserError(f"截图返回了非预期类型: {type(data)!r}")


# ---------------------------------------------------------------------------
# CDP WebSocket 客户端（最小实现，只覆盖本模块所需命令）
# ---------------------------------------------------------------------------
class CdpSession:
    """极简 CDP 客户端：连接 browser 级 WS，附加到指定页面 target。"""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RemoteBrowserError(
                "缺少 websockets 依赖：请安装 uvicorn[standard] 或 pip install websockets"
            ) from exc
        self._ws = await websockets.connect(self._ws_url, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        if "error" in msg:
                            future.set_exception(RemoteBrowserError(str(msg["error"])))
                        else:
                            future.set_result(msg.get("result", {}))
        except Exception:
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RemoteBrowserError("CDP 连接已关闭"))
            self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None,
                   session_id: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if self._ws is None:
            raise RemoteBrowserError("CDP 未连接")
        self._msg_id += 1
        msg_id = self._msg_id
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        await self._ws.send(json.dumps(payload))
        return await asyncio.wait_for(future, timeout=timeout)

    async def attach(self, target_id: str) -> str:
        """附加到页面 target，返回 sessionId。"""
        result = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = result.get("sessionId")
        if not session_id:
            raise RemoteBrowserError(f"附加页面失败: {result}")
        return session_id

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None


def mouse_event_params(kind: str, x: float, y: float, button: str = "left",
                       click_count: int = 0, modifiers: int = 0) -> dict[str, Any]:
    """构造 Input.dispatchMouseEvent 参数（坐标为 CSS 像素）。"""
    return {
        "type": kind,
        "x": float(x),
        "y": float(y),
        "button": button,
        "clickCount": click_count,
        "modifiers": modifiers,
    }


def key_event_params(kind: str, key: str, text: str = "", code: str = "",
                     windows_virtual_key_code: int = 0, modifiers: int = 0) -> dict[str, Any]:
    """构造 Input.dispatchKeyEvent 参数。"""
    params: dict[str, Any] = {
        "type": kind,
        "key": key,
        "modifiers": modifiers,
    }
    if text:
        params["text"] = text
    if code:
        params["code"] = code
    if windows_virtual_key_code:
        params["windowsVirtualKeyCode"] = windows_virtual_key_code
        params["nativeVirtualKeyCode"] = windows_virtual_key_code
    return params


# ---------------------------------------------------------------------------
# 与注册机任务对接：按 task_id 找到对应浏览器
# ---------------------------------------------------------------------------
def _find_bot_page(task_id: str) -> Any:
    """从注册机运行时状态里取出该任务当前使用的 ChromiumPage。"""
    from . import registrar

    with registrar._LOCK:
        task = registrar._REGISTRAR["active"].get(task_id) or registrar._REGISTRAR["recent"].get(task_id)
    if task is None:
        raise RemoteBrowserError(f"未找到任务 {task_id}")
    bot = task.get("bot")
    page = getattr(bot, "page", None) if bot is not None else None
    if page is None:
        raise RemoteBrowserError(f"任务 {task_id} 当前没有可用浏览器")
    return page


def list_browsers() -> list[dict[str, Any]]:
    """列出当前有浏览器可投屏的任务。"""
    from . import registrar

    items: list[dict[str, Any]] = []
    with registrar._LOCK:
        groups = (("active", registrar._REGISTRAR["active"]),
                  ("recent", registrar._REGISTRAR["recent"]))
        for group, tasks in groups:
            for tid, task in tasks.items():
                bot = task.get("bot")
                page = getattr(bot, "page", None) if bot is not None else None
                if page is None:
                    continue
                try:
                    address = str(page.address)
                except Exception:
                    address = ""
                items.append({
                    "task_id": tid,
                    "group": group,
                    "stage": task.get("stage"),
                    "address": address,
                    "url": getattr(page, "url", ""),
                    "enabled": remote_browser_enabled(),
                })
    return items


def bridge_info() -> dict[str, Any]:
    return {
        "enabled": remote_browser_enabled(),
        "fps": remote_browser_fps(),
        "quality": remote_browser_quality(),
        "browsers": list_browsers(),
    }
