import argparse
import collections
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncio

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import SessionContext, create_session, load_local_session
from .bridge import complete_openai_response, stream_openai_response
from .config import load_config, save_config
from .database import get_db
from .env import env_bool
from .accounts import (
    db_load_accounts,
    db_get_settings,
    db_set_settings,
    import_current_auth,
    get_active_session,
    rotate_next_account,
    batch_import_accounts,
)
from .registrar import get_registrar_status, start_registration, stop_registration
from . import mailbox, remotebrowser
from .tokens import (
    refresh_all_account_tokens,
    refresh_one_account,
    get_account_quota,
    get_all_accounts_quota,
    start_refresh_loop,
)

BASE_DIR = os.path.dirname(__file__)
INDEX_HTML = Path(BASE_DIR) / "static" / "index.html"
CONSOLE_HTML = Path(BASE_DIR) / "static" / "console.html"
DOCS_HTML = Path(BASE_DIR) / "static" / "docs.html"

app = FastAPI(title="qoder2api-python")

# 静态资源只有在前端构建后（frontend/npm run build）才存在。
# 这里刻意不做无条件 mount：StaticFiles 默认 check_dir=True，目录缺失时会在
# 导入期直接抛 RuntimeError，导致「服务起不来」且报错难以定位。
# 缺失时降级为警告，API 仍可用，同时给出可操作的提示。
_static_dir = Path(BASE_DIR) / "static"
_assets_dir = _static_dir / "assets"
if _assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")
else:
    print(
        f"[WARNING] 静态资源目录不存在: {_assets_dir}\n"
        "          WebUI / 文档站将不可用（API 不受影响）。请先构建前端：\n"
        "            cd frontend && npm install && npm run build"
    )

_session: SessionContext | None = None
_local_auth_error: str | None = None

logs_queue = collections.deque(maxlen=150)


def add_log(msg: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] [{level}] {msg}"
    logs_queue.append(formatted)
    print(formatted)


# Add initial logs
add_log("Qoder2API Python Bridge initialized.")



def check_gateway_token(x_gateway_token: str | None = Header(default=None)):
    config = load_config()
    gateway_token = config.get("gateway_token", "admin")
    if not x_gateway_token or x_gateway_token != gateway_token:
        raise HTTPException(status_code=401, detail="Unauthorized gateway access")


@app.post("/ui/verify")
async def verify_gateway(payload: dict[str, Any]) -> dict[str, Any]:
    token = payload.get("token", "").strip()
    config = load_config()
    if token == config.get("gateway_token", "admin"):
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid Gateway Token")


async def get_session() -> SessionContext:
    global _local_auth_error
    data = db_load_accounts()
    if not data["accounts"]:
        # Try importing environment PAT if available
        pat = os.getenv("QODER_PAT", "").strip()
        if pat:
            add_log("No accounts stored. Importing QODER_PAT from environment...")
            try:
                sess = await create_session(pat)
                with get_db() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO accounts (
                            uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                            enabled, last_status, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, 'ok', NULL)
                        """,
                        (sess.identity.uid, sess.identity.name or "Environment PAT", sess.identity.user_type,
                         sess.identity.security_oauth_token, sess.identity.refresh_token, sess.machine_id)
                    )
                db_set_settings("active_uid", sess.identity.uid)
                add_log(f"Imported environment PAT as account: {sess.identity.name}")
                _local_auth_error = None
            except Exception as exc:
                add_log(f"Failed to import environment PAT: {exc}", "ERROR")

        data = db_load_accounts()
        if not data["accounts"]:
            add_log("No accounts stored. Attempting to auto-import current local Qoder auth session...")
            try:
                await import_current_auth()
                add_log("Auto-imported current local Qoder session successfully.")
                _local_auth_error = None
            except Exception as exc:
                _local_auth_error = str(exc)
                add_log(f"Auto-import of local session failed: {exc}", "WARNING")

    try:
        return get_active_session()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"No active session available: {exc}. Please configure/import an account first."
        )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not env_bool("QODER_ENABLE_LANDING", True):
        raise HTTPException(status_code=404, detail="Landing page is disabled")
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/console", response_class=HTMLResponse)
async def console() -> HTMLResponse:
    return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))


@app.get("/documents", response_class=HTMLResponse)
async def documents() -> HTMLResponse:
    if not env_bool("QODER_ENABLE_DOCUMENTS", True):
        raise HTTPException(status_code=404, detail="Documents page is disabled")
    return HTMLResponse(DOCS_HTML.read_text(encoding="utf-8"))


@app.get("/ui/status")
async def status(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    global _local_auth_error
    try:
        await get_session()
    except Exception:
        pass

    data = db_load_accounts()
    active_uid = data.get("active_uid")
    active_acc = None
    for acc in data["accounts"]:
        if acc["uid"] == active_uid:
            active_acc = acc
            break

    if active_acc is not None:
        return {
            "ready": True,
            "mode": "accounts",
            "username": active_acc["name"],
            "uid": active_acc["uid"],
            "user_type": active_acc["user_type"],
            "error": None,
            "accounts_count": len(data["accounts"])
        }
    return {
        "ready": False,
        "mode": "none",
        "username": None,
        "uid": None,
        "user_type": None,
        "error": _local_auth_error,
        "accounts_count": len(data["accounts"])
    }


@app.get("/ui/accounts")
async def get_accounts(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    return db_load_accounts()


@app.post("/ui/accounts/import")
async def import_account(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    try:
        acc = await import_current_auth()
        add_log(f"Imported local Qoder session account: {acc['name']}")
        return {"status": "ok", "account": acc}
    except Exception as exc:
        add_log(f"Failed to import local session account: {exc}", "ERROR")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ui/accounts/batch-import")
async def batch_import(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """批量导入注册机导出的 JSON：{"accounts": [{user_id, token, refresh_token, ...}]}。"""
    records = payload.get("accounts") or payload.get("records") or []
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=400, detail="accounts 数组为空")
    result = batch_import_accounts(records)
    add_log(f"Batch imported {result['imported']} accounts (skipped {result['skipped']})")
    return {"status": "ok", **result}


@app.post("/ui/accounts/select")
async def select_account(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")
    with get_db() as conn:
        res = conn.execute("SELECT uid FROM accounts WHERE uid = ?", (uid,)).fetchone()
        if not res:
            raise HTTPException(status_code=404, detail="Account not found")
    db_set_settings("active_uid", uid)
    add_log(f"Selected active account UID: {uid}")
    return {"status": "ok"}


@app.post("/ui/accounts/toggle")
async def toggle_account(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    uid = payload.get("uid")
    enabled = bool(payload.get("enabled", True))
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")
    enabled_val = 1 if enabled else 0
    with get_db() as conn:
        res = conn.execute("UPDATE accounts SET enabled = ? WHERE uid = ?", (enabled_val, uid))
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
    add_log(f"Account toggle enabled={enabled} for UID: {uid}")
    return {"status": "ok"}


@app.post("/ui/accounts/refresh-tokens")
async def refresh_account_tokens(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """手动触发：刷新所有账号的 token（drt- → deviceToken/refresh）。"""
    result = refresh_all_account_tokens()
    add_log(f"Token refresh: ok={result['ok']} failed={result['failed']} total={result['total']}")
    return {"status": "ok", **result}


@app.get("/ui/accounts/quota")
async def accounts_quota(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """查看所有启用账号的限额（GET /api/v2/quota/usage）。"""
    return get_all_accounts_quota()


@app.delete("/ui/accounts/{uid}")
async def delete_account(uid: str, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    with get_db() as conn:
        res = conn.execute("DELETE FROM accounts WHERE uid = ?", (uid,))
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
            
    active_uid = db_get_settings("active_uid")
    if active_uid == uid:
        data = db_load_accounts()
        new_active = data["accounts"][0]["uid"] if data["accounts"] else None
        if new_active:
            db_set_settings("active_uid", new_active)
        else:
            with get_db() as conn:
                conn.execute("DELETE FROM settings WHERE key = 'active_uid'")
    add_log(f"Deleted account UID: {uid}")
    return {"status": "ok"}


@app.get("/ui/logs")
async def get_logs(verify: None = Depends(check_gateway_token)) -> list[str]:
    return list(logs_queue)


# ---------------------------------------------------------------------------
# 远程浏览器：在控制台里直接查看/操作注册机的 Chromium（无需 VNC）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 临时邮箱配置：控制台在线修改（存 SQLite，保存即生效，无需重启）
# ---------------------------------------------------------------------------
@app.get("/ui/mail-config")
async def get_mail_config(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """读取临时邮箱配置。敏感项（密码/token）只返回掩码，不回传明文。"""
    return mailbox.describe_config()


@app.post("/ui/mail-config")
async def post_mail_config(
    payload: dict[str, Any],
    verify: None = Depends(check_gateway_token),
) -> dict[str, Any]:
    """保存临时邮箱配置。

    敏感项语义：字段缺省=不修改；空字符串=不修改（表单留空）；
    null=清除该项。避免用户没填密码时误删已有配置。
    """
    try:
        result = mailbox.update_config(payload)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    add_log(f"Mail config updated: {', '.join(result['updated']) or '(none)'}")
    return {"status": "ok", **result}


@app.post("/ui/mail-config/test")
async def test_mail_config(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """按当前配置真实创建一个临时邮箱，用于验证配置是否正确。"""
    try:
        box = await asyncio.to_thread(mailbox.create_mailbox, "qodertest", None, None)
    except Exception as exc:
        add_log(f"Mail config test failed: {exc}", "WARNING")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    add_log(f"Mail config test ok: {box.address} via {box.provider}")
    return {"ok": True, "address": box.address, "provider": box.provider}


@app.get("/ui/remote-browser")
async def remote_browser_info(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """列出可投屏的浏览器任务与当前配置。"""
    return remotebrowser.bridge_info()


@app.get("/ui/remote-browser/{task_id}/screenshot")
async def remote_browser_screenshot(
    task_id: str,
    verify: None = Depends(check_gateway_token),
) -> Response:
    """单张截图（轮询模式兜底；WebSocket 不可用时前端会退化到它）。"""
    if not remotebrowser.remote_browser_enabled():
        raise HTTPException(status_code=403, detail="远程浏览器已关闭（QODER_REMOTE_BROWSER=0）")
    try:
        page = await asyncio.to_thread(remotebrowser._find_bot_page, task_id)
        data = await asyncio.to_thread(remotebrowser.snapshot, page)
    except remotebrowser.RemoteBrowserError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.websocket("/ui/remote-browser/{task_id}/ws")
async def remote_browser_ws(websocket: WebSocket, task_id: str) -> None:
    """画面下行 + 输入上行，共用一条 WebSocket。

    鉴权：浏览器 WebSocket 无法自定义请求头，因此 token 通过查询参数传入，
    并与控制台登录凭据做同一套校验。
    """
    token = websocket.query_params.get("token")
    expected = load_config().get("gateway_token", "admin")
    if not token or token != expected:
        await websocket.close(code=4401)
        return
    if not remotebrowser.remote_browser_enabled():
        await websocket.close(code=4403)
        return

    await websocket.accept()

    try:
        page = await asyncio.to_thread(remotebrowser._find_bot_page, task_id)
        debug_address = await asyncio.to_thread(remotebrowser.get_debug_address, page)
        target_id = await asyncio.to_thread(remotebrowser._page_target_id, debug_address, None)
        ws_url = await asyncio.to_thread(remotebrowser._cdp_target, debug_address)
    except remotebrowser.RemoteBrowserError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    session = remotebrowser.CdpSession(ws_url)
    try:
        await session.connect()
        session_id = await session.attach(target_id)
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"CDP 连接失败: {exc}"})
        await session.close()
        await websocket.close()
        return

    # 告知前端 CSS viewport 尺寸：前端据此把点击坐标换算为页面坐标。
    # 不能假定截图尺寸等于 CSS 尺寸——若用户通过 QODER_CHROMIUM_ARGS 设置了
    # --force-device-scale-factor，截图会按 devicePixelRatio 放大。
    viewport: dict[str, Any] = {}
    try:
        metrics = await session.send("Page.getLayoutMetrics", {}, session_id=session_id)
        for key in ("cssVisualViewport", "cssLayoutViewport", "visualViewport", "layoutViewport"):
            candidate = metrics.get(key)
            if isinstance(candidate, dict) and candidate.get("clientWidth"):
                viewport = {
                    "width": candidate.get("clientWidth"),
                    "height": candidate.get("clientHeight"),
                }
                break
    except Exception:
        viewport = {}

    await websocket.send_json({"type": "ready", "task_id": task_id, "viewport": viewport})
    stop = asyncio.Event()

    async def pump_frames() -> None:
        """按配置帧率持续推送 JPEG 帧。"""
        quality = remotebrowser.remote_browser_quality()
        interval = 1.0 / max(1, remotebrowser.remote_browser_fps())
        while not stop.is_set():
            try:
                raw = await session.send("Page.captureScreenshot",
                                         {"format": "jpeg", "quality": quality},
                                         session_id=session_id, timeout=15)
                data = raw.get("data")
                if data:
                    await websocket.send_json({"type": "frame", "data": data})
            except Exception:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def pump_events() -> None:
        """接收前端输入事件并转发到浏览器。"""
        while not stop.is_set():
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            kind = message.get("type")
            try:
                if kind == "mouse":
                    params = remotebrowser.mouse_event_params(
                        message.get("event", "mouseMoved"),
                        message.get("x", 0), message.get("y", 0),
                        message.get("button", "left"),
                        int(message.get("clickCount", 0)),
                        int(message.get("modifiers", 0)),
                    )
                    await session.send("Input.dispatchMouseEvent", params, session_id=session_id)
                elif kind == "wheel":
                    await session.send("Input.dispatchMouseEvent", {
                        "type": "mouseWheel",
                        "x": float(message.get("x", 0)),
                        "y": float(message.get("y", 0)),
                        "deltaX": float(message.get("deltaX", 0)),
                        "deltaY": float(message.get("deltaY", 0)),
                    }, session_id=session_id)
                elif kind == "key":
                    params = remotebrowser.key_event_params(
                        message.get("event", "keyDown"),
                        message.get("key", ""),
                        message.get("text", ""),
                        message.get("code", ""),
                        int(message.get("keyCode", 0)),
                        int(message.get("modifiers", 0)),
                    )
                    await session.send("Input.dispatchKeyEvent", params, session_id=session_id)
                elif kind == "navigate":
                    await session.send("Page.navigate", {"url": message.get("url", "")},
                                       session_id=session_id)
            except Exception:
                continue

    frame_task = asyncio.create_task(pump_frames())
    event_task = asyncio.create_task(pump_events())
    try:
        done, pending = await asyncio.wait(
            {frame_task, event_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    finally:
        stop.set()
        frame_task.cancel()
        event_task.cancel()
        await session.close()
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/ui/registrar/start")
async def registrar_start(payload: dict[str, Any] | None = None, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """启动注册机（无限循环：parents 个母线程 × 每批 3 个子任务，直到调用 stop）。

    body 可选：{"parents": 2}  —— 母线程数（1-6），每母线程 3 子任务并发。
    """
    payload = payload or {}
    try:
        parents = int(payload.get("parents", 2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="parents 参数无效")
    return start_registration(parents=parents)


@app.post("/ui/registrar/stop")
async def registrar_stop(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """请求停止：当前批次完成后停止，返回本次注册统计。"""
    return stop_registration()


@app.get("/ui/registrar/status")
async def registrar_status(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """查询注册机任务状态（stage / logs / result）。"""
    return get_registrar_status()


@app.get("/ui/config")
async def get_ui_config(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    return load_config()


@app.post("/ui/config")
async def post_ui_config(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    save_config(payload)
    add_log("API Key configuration updated.")
    return {"status": "ok"}


@app.post("/ui/session")
async def set_session(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    global _local_auth_error
    pat = str(payload.get("pat") or os.getenv("QODER_PAT", "")).strip()
    if not pat:
        raise HTTPException(status_code=400, detail="PAT is required")
    try:
        add_log("Attempting to save session from PAT...")
        sess = await create_session(pat)
        
        # Insert or update in SQLite
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO accounts (
                    uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                    enabled, last_status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 'ok', ?)
                """,
                (sess.identity.uid, sess.identity.name or "PAT Account", sess.identity.user_type,
                 sess.identity.security_oauth_token, sess.identity.refresh_token, sess.machine_id, None)
            )
            
        db_set_settings("active_uid", sess.identity.uid)
        
        add_log(f"Session saved from PAT. User: {sess.identity.name}")
        _local_auth_error = None
        return {"ready": True, "id": sess.identity.uid, "name": sess.identity.name, "user_type": sess.identity.user_type}
    except Exception as exc:
        msg = f"Failed to authenticate with provided PAT: {exc}"
        add_log(msg, "ERROR")
        raise HTTPException(status_code=502, detail=msg) from exc


def is_quota_error(exc: Exception) -> bool:
    """判断是否为 quota/限流类错误（429 / quota / rate limit）。
    这类错误需先查询真实限额确认，不能直接跳过账户。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(k in msg for k in ("http 429", "quota", "rate limit", "insufficient"))
    return False


def is_account_error(exc: Exception) -> bool:
    """判断是否'账号级'错误（token 无效/限额/服务端拒绝）。只有这类才应跳过账户。

    网络/流中断/超时（如 httpx.ReadError 的 incomplete chunk read）是临时性问题，
    换账户也无效，不应触发 rotate。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403, 429)
    if isinstance(exc, httpx.HTTPError):
        return False  # 连接/超时/读错误等网络问题
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if any(code in msg for code in ("http 401", "http 403", "http 429")):
            return True
        for kw in ("unauthorized", "invalid token", "quota", "rate limit",
                   "insufficient", "personal token", "credit"):
            if kw in msg:
                return True
    return False


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any], authorization: str | None = Header(default=None)):
    config = load_config()
    if config.get("auth_required", False):
        allowed_keys = config.get("allowed_keys", [])
        incoming_key = None
        if authorization and authorization.startswith("Bearer "):
            incoming_key = authorization[len("Bearer "):].strip()
        
        if not incoming_key or incoming_key not in allowed_keys:
            add_log("Access denied: Invalid or missing API Key in request header.", "WARNING")
            raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    model = payload.get("model", "lite")
    stream = bool(payload.get("stream", False))
    messages_count = len(payload.get("messages", []))
    add_log(f"Incoming completion request: model={model}, stream={stream}, messages={messages_count}")
    
    accounts_data = db_load_accounts()
    enabled_count = sum(1 for acc in accounts_data["accounts"] if acc.get("enabled", True))
    max_retries = max(1, enabled_count)
    
    for attempt in range(max_retries):
        try:
            sess = await get_session()
            add_log(f"Request routing via account: {sess.identity.name} ({sess.identity.uid})")
            if stream:
                gen = stream_openai_response(payload, sess)
                try:
                    first_item = await gen.__anext__()
                except StopAsyncIteration:
                    first_item = None
                
                async def stream_success_wrapper(first, g):
                    if first is not None:
                        yield first
                    async for chunk in g:
                        yield chunk
                
                add_log(f"Streaming response initiated (Attempt {attempt+1}/{max_retries}).")
                return StreamingResponse(
                    stream_success_wrapper(first_item, gen),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"}
                )
            else:
                add_log(f"Generating full completion response (Attempt {attempt+1}/{max_retries})...")
                resp = await complete_openai_response(payload, sess)
                add_log("Completion request finished successfully.")
                return resp
        except Exception as exc:
            current_uid = sess.identity.uid if 'sess' in locals() else "unknown"
            if is_account_error(exc):
                if is_quota_error(exc):
                    # quota 类错误：先发一次请求确认是否真正 exceeded，而不是直接跳过
                    q = get_account_quota(current_uid)
                    if q.get("ok"):
                        quota = q["quota"]
                        truly_exceeded = bool(quota.get("isQuotaExceeded")) or (quota.get("userQuota") or {}).get("remaining", 1) <= 0
                        if not truly_exceeded:
                            add_log(f"Quota check on {current_uid}: NOT exceeded (remaining={quota.get('userQuota', {}).get('remaining')}), not rotating.", "WARNING")
                            raise HTTPException(status_code=502, detail=f"{exc}")
                        add_log(f"Quota confirmed exceeded for {current_uid}: {exc}. Rotating...", "WARNING")
                    else:
                        # 限额查询失败：无法确认，保守不跳过账户
                        add_log(f"Quota check failed for {current_uid} ({q.get('error')}), not rotating.", "WARNING")
                        raise HTTPException(status_code=502, detail=f"{exc}")
                else:
                    add_log(f"Account-level error on {current_uid}: {exc}. Rotating to next account...", "WARNING")
                try:
                    rotate_next_account(current_uid, str(exc))
                except Exception as e:
                    add_log(f"Failed to rotate account: {e}", "ERROR")
                    raise HTTPException(status_code=502, detail=f"Request failed and no other account is available. Error: {exc}")
            else:
                add_log(f"Transient error on account {current_uid}: {exc}. Not rotating account.", "WARNING")
                raise HTTPException(status_code=502, detail=str(exc))
                
    raise HTTPException(status_code=502, detail="Request failed on all available accounts.")


def main() -> None:
    import uvicorn

    start_refresh_loop()  # 启动 token 定时刷新线程（每 6 小时）

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("QODER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QODER_PORT", "5050")))
    args = parser.parse_args()
    uvicorn.run("qoder2api.app:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    main()
