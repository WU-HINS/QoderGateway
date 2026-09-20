"""
Qoder 注册机服务（多线程版）：后台并行运行多个「注册 + Device 授权拉凭据」任务，完成后自动入库。

设计：
  - 多任务并行：每个任务独立线程 + 独立浏览器实例 + 独立临时 profile
  - 错峰启动：任务 1 立即，后续任务延迟 stagger 秒启动，让人工验证时间错开
  - 窗口调度：浏览器平时隐藏后台；进入人机验证阶段时置顶显示一次，
    划完自动隐藏；同一时刻只有一个任务置顶（VerifierQueue），划完一个自动轮到下一个
  - 状态机 stage：idle | registering | waiting_slider | waiting_otp | device_auth | saving | success | failed
  - 任务级状态与日志，供网页版轮询展示
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import shutil
import string
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

import httpx

from . import mailbox
from .accounts import db_get_settings, db_set_settings
from .database import get_db
from .env import (
    chromium_extra_args,
    chromium_headless,
    chromium_path,
    httpx_client_kwargs,
    proxy_url,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
REGISTER_URL = "https://qoder.com/users/sign-up"
SUCCESS_URL_MARK = "/download"
DEVICE_CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
DEVICE_VERIFIER_CHARS = string.ascii_letters + string.digits + "-._~"

# 容器内运行 Chromium 的必需参数：
#   no-sandbox          —— 容器通常无 CAP_SYS_ADMIN / 无 user namespace
#   disable-dev-shm-usage —— /dev/shm 默认 64MB，容易导致渲染进程崩溃
#   disable-gpu         —— 无显卡环境避免 GPU 初始化失败
DEFAULT_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
]

# DrissionPage 连接浏览器的重试次数与间隔（秒）。
# 默认值偏短：Chromium 冷启动在机械硬盘或低配环境下可能耗时数十秒，
# 太短会直接报 BrowserConnectError。10 次 × 3 秒 ≈ 最长等待 30 秒。
BROWSER_CONNECT_RETRY = (10, 3)

# 单个母线程每批并发的子任务数。每个子任务 = 一个独立 Chromium 实例，
# 并发越高磁盘压力越大；机械硬盘上建议保持 1~3。
BROWSER_WORKERS_PER_PARENT = 3

# 人机验证的等待上限（秒）。超时后会打印诊断信息，而不是静默继续。
SLIDER_WAIT_TIMEOUT = 300

# 服务状态（单例，多任务）
_REGISTRAR: dict[str, Any] = {
    "running": False,
    "stop_requested": False,
    "parents": 0,
    "started_at": None,
    "active": {},   # 运行中的子任务
    "recent": {},   # 最近完成的子任务（最多 30 个）
    "stats": {"success": 0, "failed": 0, "total": 0},
}
_LOCK = threading.Lock()


def _log(task_id: str | None, line: str) -> None:
    key = task_id or "sched"
    with _LOCK:
        task = _REGISTRAR["active"].get(task_id) or _REGISTRAR["recent"].get(task_id) if task_id else None
        if task:
            task["logs"].append(line)
            if len(task["logs"]) > 200:
                task["logs"] = task["logs"][-200:]
    print(f"[{key}] {line}", flush=True)


def _set_task(task_id: str, stage: str, result: dict | None = None, error: str | None = None) -> None:
    with _LOCK:
        t = _REGISTRAR["active"].setdefault(task_id, {
            "stage": "idle", "logs": [], "result": None, "error": None, "started_at": time.time(),
        })
        t["stage"] = stage
        if result is not None:
            t["result"] = result
        if error is not None:
            t["error"] = error


def _bind_bot(task_id: str, bot: Any) -> None:
    """把任务当前的浏览器实例挂到状态上，供远程画面桥（remotebrowser）取用。

    注意：bot 持有 ChromiumPage，不可序列化，_dump() 已显式排除该字段。
    """
    with _LOCK:
        task = _REGISTRAR["active"].get(task_id) or _REGISTRAR["recent"].get(task_id)
        if task is not None:
            task["bot"] = bot


def _finish_task(task_id: str, stage: str, result: dict | None = None, error: str | None = None) -> None:
    """子任务完成：归档到 recent 并计入统计。"""
    with _LOCK:
        task = _REGISTRAR["active"].pop(task_id, None)
        if task is None:
            task = {"stage": stage, "logs": [], "result": None, "error": None, "started_at": 0}
        task["stage"] = stage
        if result is not None:
            task["result"] = result
        if error is not None:
            task["error"] = error
        _REGISTRAR["recent"][task_id] = task
        if len(_REGISTRAR["recent"]) > 30:  # 只保留最近 30 个完成记录
            oldest = sorted(_REGISTRAR["recent"])[0]
            _REGISTRAR["recent"].pop(oldest, None)
        _REGISTRAR["stats"]["total"] += 1
        if stage == "success":
            _REGISTRAR["stats"]["success"] += 1
        else:
            _REGISTRAR["stats"]["failed"] += 1
        stats = dict(_REGISTRAR["stats"])
    _log("sched", f"统计: 成功 {stats['success']} / 失败 {stats['failed']} / 总计 {stats['total']}")


# ---------------------------------------------------------------------------
# 人机验证调度：同一时刻只置顶一个任务，划完自动轮到下一个
# ---------------------------------------------------------------------------
class VerifierQueue:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._current: str | None = None

    def acquire(self, task_id: str) -> None:
        """等待获得焦点（前一个任务验证完成前阻塞）。"""
        with self._cv:
            while self._current is not None:
                self._cv.wait()
            self._current = task_id

    def release(self, task_id: str) -> None:
        with self._cv:
            if self._current == task_id:
                self._current = None
                self._cv.notify_all()

    @property
    def current(self) -> str | None:
        with self._cv:
            return self._current


_VQ = VerifierQueue()


# ---------------------------------------------------------------------------
# 临时邮箱（provider 抽象层，见 mailbox.py）
#
# cloudflare: CF_TEMP_EMAIL_BASE / CF_TEMP_EMAIL_ADMIN_PASSWORD / ...
# yyds      : YYDS_API_KEY
# 选择逻辑  : QODER_MAIL_PROVIDER=auto|cloudflare|yyds
# ---------------------------------------------------------------------------
def yyds_create_mailbox(prefix: str = "qoder", task_id: str | None = None) -> str:
    """创建临时邮箱并返回地址（向后兼容的旧函数名）。"""
    return mailbox.create_mailbox(
        prefix=prefix,
        task_id=task_id,
        log=lambda msg: _log(task_id, msg),
    ).address


def yyds_wait_code(address: str, task_id: str | None = None, timeout: float = 120.0) -> str:
    """轮询等待邮箱验证码（向后兼容的旧函数名）。"""
    return mailbox.wait_verification_code(
        address,
        task_id=task_id,
        timeout=timeout,
        log=lambda msg: _log(task_id, msg),
    )


# ---------------------------------------------------------------------------
# Device flow（来自 qodercli 逆向：docs/qoder-protocol-research.md §4）
# ---------------------------------------------------------------------------
def device_flow_params(machine_id: str | None = None) -> dict:
    length = random.randint(43, 128)
    verifier = "".join(random.choices(DEVICE_VERIFIER_CHARS, k=length))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    nonce = str(uuid.uuid4())
    mid = machine_id or str(uuid.uuid4())
    auth_url = (
        f"https://qoder.com/device/selectAccounts?challenge={challenge}"
        f"&challenge_method=S256&nonce={nonce}&machine_id={mid}&client_id={DEVICE_CLIENT_ID}"
    )
    poll_url = (
        f"https://openapi.qoder.sh/api/v1/deviceToken/poll"
        f"?nonce={nonce}&verifier={verifier}&challenge_method=S256"
    )
    return {"verifier": verifier, "nonce": nonce, "auth_url": auth_url, "poll_url": poll_url}


def poll_device_token(poll_url: str, task_id: str | None = None, timeout: float = 300.0, proxy: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(poll_url, headers={"Accept": "application/json"}, timeout=20, proxy=proxy)
        if r.status_code == 404:
            _log(task_id, "[device] waiting for user authorization...")
        elif r.status_code == 200:
            _log(task_id, "[device] credential received")
            return r.json()
        else:
            _log(task_id, f"[device] unexpected status {r.status_code}")
        time.sleep(1)
    raise TimeoutError("device authorization timeout")


# ---------------------------------------------------------------------------
# 按钮识别（规则来自 scripts/buttons_dump.json 实测：button + '继 续' + ant-btn-primary）
# ---------------------------------------------------------------------------
def _normalize_text(text: Any) -> str:
    return (text or "").replace("\u00a0", " ").replace(" ", "").replace("\n", "").replace("\r", "").strip().lower()


def _score_button(feat: dict[str, Any]) -> int:
    score = 0
    tag = (feat.get("tag") or "").lower()
    text = _normalize_text(feat.get("text"))
    cls = feat.get("class") or ""
    type_ = feat.get("type") or ""
    if feat.get("displayed") is False:
        score -= 1000
    if tag == "button":
        score += 10
    for pts, kw in ((100, "继续"), (100, "continue"), (90, "同意"), (90, "授权"), (80, "authorize")):
        if kw in text:
            score += pts
            break
    if "ant-btn-primary" in cls:
        score += 30
    if type_ == "submit":
        score += 20
    return score


def _pick_button(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not features:
        return None
    best = max(features, key=_score_button)
    return best if _score_button(best) >= 50 else None


def _free_port() -> int:
    """分配空闲端口：每个浏览器实例独立调试端口，防止多实例复用一个浏览器。"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


OTP_SELECTOR = 'css:input[aria-label^="OTP Input"]'


def _find_otp_input(page: Any) -> Any:
    """在主页与各 iframe 中查找 OTP 输入框。

    验证通过后 OTP 输入框未必出现在主文档里——也可能落在某个 iframe 中，
    只查主页面会一直等不到，表现为「验证完成了但状态不更新」。
    """
    try:
        element = page.ele(OTP_SELECTOR, timeout=0)
        if element:
            return element
    except Exception:
        pass
    try:
        for frame in page.get_frames():
            try:
                element = frame.ele(OTP_SELECTOR, timeout=0)
                if element:
                    return element
            except Exception:
                continue
    except Exception:
        pass
    return None


def _describe_inputs(page: Any, task_id: str, limit: int = 8) -> None:
    """把页面上的 input 概况写进日志，便于判断是不是选择器过期。"""
    try:
        inputs = page.eles("tag:input")[:limit]
    except Exception:
        return
    _log(task_id, f"[verify] 当前页面共 {len(inputs)} 个 input（最多列 {limit} 个）：")
    for index, element in enumerate(inputs):
        try:
            _log(task_id, f"[verify]   [{index}] id={element.attr('id')!r} "
                          f"type={element.attr('type')!r} "
                          f"aria-label={element.attr('aria-label')!r}")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# DrissionPage 注册机（每任务一个实例）
# ---------------------------------------------------------------------------
class RegistrarBot:
    def __init__(self, task_id: str = "t1", verifier_queue: VerifierQueue | None = None,
                 profile_dir: str | None = None, cleanup_profile: bool = False,
                 proxy: str | None = None) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage

        self.task_id = task_id
        self.vq = verifier_queue or _VQ
        self.proxy = proxy
        co = ChromiumOptions()
        co.set_local_port(_free_port())  # 独立调试端口，杜绝实例串扰

        # Chromium 路径：容器/自定义安装必须显式指定（DrissionPage 默认只找 Windows 路径）
        browser_path = chromium_path()
        if browser_path:
            co.set_browser_path(browser_path)
            _log(task_id, f"[browser] executable = {browser_path}")

        if chromium_headless():
            co.headless(True)
            _log(task_id, "[browser] headless mode (人机验证可能无法完成)")

        # 容器/root 环境必需：无沙箱、共享内存、禁用 /dev/shm 限制与 GPU
        for arg in DEFAULT_CHROMIUM_ARGS:
            co.set_argument(arg)
        for arg in chromium_extra_args():
            co.set_argument(arg)

        if proxy:
            co.set_proxy(proxy)
            _log(task_id, f"[browser] using proxy {proxy[:48]}...")
        if profile_dir:
            self.profile_dir = profile_dir
        else:
            self.profile_dir = tempfile.mkdtemp(prefix=f"qoder_reg_{task_id[:8]}_")
            _log(task_id, f"[browser] new temp profile: {self.profile_dir}")
        self._cleanup_profile = cleanup_profile
        co.set_user_data_path(self.profile_dir)

        # 加长连接重试：HDD / 低配环境下 Chromium 冷启动可能很慢
        if hasattr(co, "set_retry"):
            try:
                co.set_retry(*BROWSER_CONNECT_RETRY)
            except Exception:
                pass

        try:
            self.page = ChromiumPage(co)
        except Exception as exc:
            # 原始异常信息对容器/HDD 场景缺乏指向性，这里补充排查方向
            _log(task_id, f"[browser] 启动 Chromium 失败: {type(exc).__name__}")
            _log(task_id, f"[browser] profile = {self.profile_dir}")
            _log(task_id, "[browser] 排查方向：")
            _log(task_id, "  1) 确认 Xvfb 在运行且 DISPLAY 正确（容器入口脚本会自动启动）")
            _log(task_id, "  2) 机械硬盘：Chromium 冷启动很慢，请把并发降到 1（母线程数）")
            _log(task_id, "  3) 容器请加 --shm-size=1g，否则渲染进程易崩溃")
            _log(task_id, "  4) 确认 /usr/bin/chromium 可执行且未被安全策略拦截")
            raise

    # ---- 窗口控制 ----
    #
    # DrissionPage 的窗口控制方法在 Linux 上支持不完整，部分会抛
    # "This method can be used only on Windows"（如 hide/show）。
    # 容器里窗口跑在 Xvfb 虚拟屏上，本就没有真实桌面的可见性可言，
    # 因此按平台分支：Windows 做真正的隐藏/置顶；Linux 只做尽力而为的
    # 最大化，任何失败都静默忽略——窗口尺寸不影响 CDP 画面与事件注入。
    def window_hide(self) -> None:
        if sys.platform != "win32":
            return  # Linux 不支持隐藏，虚拟屏上也无意义
        try:
            self.page.set.window.hide()
            _log(self.task_id, "[browser] window hidden")
        except Exception as e:
            _log(self.task_id, f"[browser] hide error: {e}")

    def window_show_top(self) -> None:
        if sys.platform == "win32":
            try:
                self.page.set.window.show()
            except Exception:
                pass
            self._window_top_windows()
        else:
            self._window_focus_linux()

    def _window_focus_linux(self) -> None:
        """Linux/Xvfb：尽力最大化窗口，便于在控制台画面里操作滑块。

        该方法在 Linux 上可能不可用，失败即静默忽略。
        """
        try:
            self.page.set.window.max()
            _log(self.task_id, "[browser] window maximized")
        except Exception:
            pass

    def _window_top_windows(self) -> None:
        try:
            import win32con
            import win32gui
            title = self.page.title or ""
            wins: list[int] = []

            def _find(hwnd: int, acc: list[int]) -> bool:
                if win32gui.IsWindowVisible(hwnd) and title and title in win32gui.GetWindowText(hwnd):
                    acc.append(hwnd)
                return True

            win32gui.EnumWindows(_find, wins)
            if wins:
                hwnd = wins[0]
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                _log(self.task_id, "[browser] window shown & top")
        except Exception as e:
            _log(self.task_id, f"[browser] show_top error: {e}")

    def close(self) -> None:
        try:
            self.page.quit()
        except Exception:
            pass
        if self._cleanup_profile:
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                _log(self.task_id, f"[browser] profile destroyed: {self.profile_dir}")
            except Exception:
                pass

    @staticmethod
    def _html_fp(page) -> str:
        return hashlib.md5((page.html or "").encode("utf-8", errors="ignore")).hexdigest()

    def _locate(self, selector: str, timeout: float | None = None,
                desc: str = "", displayed: bool = False) -> Any:
        """定位元素；找不到时在控制台输出具体是哪个元素找不到，并抛出带定位符的错误。"""
        from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError
        try:
            if displayed:
                self.page.wait.ele_displayed(selector, timeout=timeout or 10)
                return self.page.ele(selector)
            if timeout is None:
                return self.page.ele(selector)
            return self.page.ele(selector, timeout=timeout)
        except (ElementNotFoundError, WaitTimeoutError):
            label = f"（{desc}）" if desc else ""
            msg = f"找不到元素: {selector}{label}"
            _log(self.task_id, f"[locate] {msg}")
            raise ElementNotFoundError(msg) from None

    def _find_submit_button(self, timeout: float = 10.0):
        pairs: list[tuple] = []
        for sel in ('css:button', 'css:a[href]', 'css:[role="button"]'):
            try:
                els = self.page.eles(sel, timeout=timeout)
            except Exception:
                continue
            for el in els:
                try:
                    feat = {
                        "tag": el.tag,
                        "text": (el.text or "").strip()[:80],
                        "id": el.attr("id") or "",
                        "class": el.attr("class") or "",
                        "type": el.attr("type") or "",
                        "aria-label": el.attr("aria-label") or "",
                        "displayed": bool(el.states.is_displayed),
                    }
                    pairs.append((el, feat))
                except Exception:
                    continue
        if not pairs:
            return None
        picked = _pick_button([f for _, f in pairs])
        if not picked:
            return None
        for el, feat in pairs:
            if feat is picked:
                return el
        return None

    def _click_submit(self, timeout: float = 10.0) -> None:
        btn = self._find_submit_button(timeout)
        if btn is not None:
            btn.click()
            return
        _log(self.task_id, "[submit] 未识别到提交按钮（已尝试 css:button / css:a[href] / css:[role=button]），回退尝试 css:button[type=\"submit\"]")
        self._locate('css:button[type="submit"]', desc="提交按钮").click()

    # ---- 填表（身份断言 + 清空 + 输入后值验证，防串扰） ----
    def _fill(self, selector: str, value: str, must_id: str | None = None,
              retries: int = 3) -> None:
        for attempt in range(retries):
            el = self._locate(selector, timeout=10, desc="填表输入框")
            el_id = el.attr("id") or ""
            if must_id and el_id != must_id:
                raise RuntimeError(f"填表定位错误: 期望 #{must_id}，实际 #{el_id} ({selector})")
            try:
                el.clear()
            except Exception:
                pass
            el.input(value)
            try:
                got = el.value or ""
            except Exception:
                got = el.attr("value") or ""
            if got == value:
                return
            _log(self.task_id, f"[fill] {selector} 值验证失败(尝试{attempt + 1}): 期望 {value!r} 实际 {got!r}，重试")
        raise RuntimeError(f"多次填表失败: {selector}")

    # ---- 打开页面且全程隐藏（仅人机验证时 show_top 显示） ----
    def _open_hidden(self, url: str) -> None:
        self.window_hide()  # get 前尽量隐藏（窗口刚建立即可）
        self.page.get(url)
        try:
            self.window_hide()  # 加载后兜底隐藏，避免闪现
        except Exception:
            pass
        self.page.wait.load_start()

    # ---- 注册 ----
    def register(self) -> dict:
        page = self.page
        tid = self.task_id
        address = yyds_create_mailbox(task_id=tid)
        first, last = _random_name()
        password = _random_password()
        _log(tid, f"[reg] name={first} {last}  mail={address}")

        self._open_hidden(REGISTER_URL)
        self._locate("#basic_firstName", timeout=60, displayed=True, desc="注册页姓输入框")
        _log(tid, "[reg] page loaded (hidden)")

        self._fill("#basic_firstName", first, must_id="basic_firstName")
        self._fill("#basic_lastName", last, must_id="basic_lastName")
        self._fill("#basic_email", address, must_id="basic_email")
        _log(tid, "[reg] name & email filled")

        cb = self._locate("css:.ant-checkbox-input", desc="同意条款复选框")
        cb.parent().click()
        _log(tid, "[reg] checkbox checked")
        self._click_submit()
        _log(tid, "[reg] submitted email step")

        self._locate("#basic_password", timeout=60, displayed=True, desc="密码输入框")
        self._fill("#basic_password", password, must_id="basic_password")
        self._click_submit()
        _log(tid, "[reg] submitted password step")

        # 人机验证：排队 + 置顶显示一次，划完自动隐藏（轮询等待，可被 stop 中断）
        _set_task(tid, "waiting_slider")
        self.vq.acquire(tid)
        self.window_show_top()
        _log(tid, ">>> 请在本机浏览器完成人机验证（窗口已置顶）<<<")
        try:
            deadline = time.time() + SLIDER_WAIT_TIMEOUT
            otp_seen = False
            jumped = False
            last_report = 0.0
            while time.time() < deadline:
                if _REGISTRAR["stop_requested"]:
                    raise RuntimeError("用户请求停止注册")
                # OTP 可能出现在主文档，也可能在某个 iframe 里
                if _find_otp_input(page):
                    otp_seen = True
                    break
                if SUCCESS_URL_MARK in page.url:
                    jumped = True
                    break
                now = time.time()
                if now - last_report >= 15:
                    last_report = now
                    _log(tid, f"[verify] 等待验证通过… 剩余 {int(deadline - now)}s "
                              f"url={page.url[:70]}")
                time.sleep(0.5)

            if otp_seen:
                _log(tid, "[reg] OTP input appeared")
            elif jumped:
                _log(tid, "[reg] page jumped directly to download (no OTP)")
            else:
                # 此前这里把「超时」误报成「直接跳转，无需 OTP」，
                # 掩盖了验证其实没有通过的事实，导致后续流程白等验证码。
                _log(tid, f"[verify] 等待验证通过超时（{SLIDER_WAIT_TIMEOUT}s）", "WARNING")
                _log(tid, f"[verify] 当前 url={page.url[:100]}", "WARNING")
                _describe_inputs(page, tid)
        finally:
            self.window_hide()
            self.vq.release(tid)
            _log(tid, "[verify] slider done, focus released")

        _set_task(tid, "waiting_otp")
        code = yyds_wait_code(address, task_id=tid, timeout=120)

        otp_inputs = page.eles(OTP_SELECTOR)
        if otp_inputs:
            for i, ch in enumerate(code[: len(otp_inputs)]):
                otp_inputs[i].input(ch)
            _log(tid, f"[reg] OTP filled: {code}")
        else:
            self._locate(OTP_SELECTOR, desc="OTP 输入框").input(code)

        deadline = time.time() + 30
        while time.time() < deadline:
            if SUCCESS_URL_MARK in page.url:
                _log(tid, f"[reg] SUCCESS -> {page.url}")
                break
            time.sleep(1)

        return {"email": address, "password": password, "name": f"{first} {last}"}

    # ---- Device 授权（全程后台隐藏，自动点"继 续"，点击后 2s 无变化重试） ----
    def device(self) -> dict:
        page = self.page
        tid = self.task_id
        flow = device_flow_params()
        _log(tid, f"[dev] auth URL:\n  {flow['auth_url']}")

        self._open_hidden(flow["auth_url"])  # 全程隐藏，不弹窗
        time.sleep(1)

        deadline = time.time() + 300
        clicked = False
        while time.time() < deadline:
            try:
                btn = self._find_submit_button(timeout=3)
                if btn is not None:
                    before_url = page.url
                    before_fp = self._html_fp(page)
                    before_tabs = len(page.get_tabs())
                    _log(tid, f"[dev] found button '{btn.text.strip()}', clicking...")
                    btn.click()
                    changed = False
                    for _ in range(4):  # 2s
                        time.sleep(0.5)
                        if (page.url != before_url or self._html_fp(page) != before_fp
                                or len(page.get_tabs()) > before_tabs):  # target=_blank 会在新标签页打开
                            changed = True
                            break
                    if changed:
                        clicked = True
                        _log(tid, "[dev] click took effect (page changed)")
                        break
                    _log(tid, "[dev] click did not change page, retrying...")
            except Exception:
                pass
            time.sleep(1)

        if not clicked:
            raise TimeoutError("认证页未出现可点击的'继 续'按钮（可能未登录或页面结构变化）")

        _log(tid, ">>> 已点击'继 续'，等待服务端完成认证并 pull <<<")
        cred = poll_device_token(flow["poll_url"], task_id=tid, timeout=300, proxy=self.proxy)
        return {
            "token": cred.get("token"),
            "refresh_token": cred.get("refresh_token"),
            "user_id": cred.get("user_id"),
            "expires_at": cred.get("expires_at"),
            "refresh_token_expires_at": cred.get("refresh_token_expires_at"),
        }


# ---------------------------------------------------------------------------
# 随机数据
# ---------------------------------------------------------------------------
def _random_name() -> tuple[str, str]:
    consonants = "bcdfghjklmnpqrstvwxyz"
    vowels = "aeiou"

    def syllable() -> str:
        return random.choice(consonants) + random.choice(vowels) + random.choice(consonants)

    return (syllable() + syllable()).capitalize(), (syllable() + syllable()).capitalize()


def _random_password(length: int = 12) -> str:
    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit = random.choice(string.digits)
    symbol = random.choice("!@#$%^&*()-_=+")
    rest = "".join(random.choices(string.ascii_letters + string.digits + "!@#$%^&*()-_=+", k=length - 4))
    pool = list(lower + upper + digit + symbol + rest)
    random.shuffle(pool)
    return "".join(pool)


# ---------------------------------------------------------------------------
# 服务层：无限循环注册（母线程批次 × 每批 3 子任务）+ 停止 + 统计
# ---------------------------------------------------------------------------
def get_registrar_status() -> dict[str, Any]:
    with _LOCK:
        def _dump(t: dict) -> dict:
            return {
                "stage": t["stage"],
                "logs": t["logs"][-60:],
                "result": t["result"],
                "error": t["error"],
                "started_at": t["started_at"],
            }
        return {
            "running": _REGISTRAR["running"],
            "stop_requested": _REGISTRAR["stop_requested"],
            "parents": _REGISTRAR["parents"],
            "started_at": _REGISTRAR["started_at"],
            "verification": _VQ.current,
            "stats": dict(_REGISTRAR["stats"]),
            "active": {tid: _dump(t) for tid, t in _REGISTRAR["active"].items()},
            "recent": {tid: _dump(t) for tid, t in _REGISTRAR["recent"].items()},
        }


def start_registration(parents: int = 1) -> dict[str, Any]:
    """启动 parents 个母线程；每个母线程无限循环：每批并发子任务，直到 stop_registration()。

    默认 1 是保守值：每个子任务都会拉起一个独立 Chromium，
    机械硬盘或低配环境下并发启动多个会导致磁盘 IO 饱和、浏览器连接超时。
    """
    parents = max(1, min(int(parents), 6))
    with _LOCK:
        if _REGISTRAR["running"]:
            return {"ok": False, "error": "已有注册任务在运行"}
        _REGISTRAR["running"] = True
        _REGISTRAR["stop_requested"] = False
        _REGISTRAR["parents"] = parents
        _REGISTRAR["active"] = {}
        _REGISTRAR["recent"] = {}
        _REGISTRAR["stats"] = {"success": 0, "failed": 0, "total": 0}
        _REGISTRAR["started_at"] = time.time()
    for i in range(parents):
        pid = f"P{i + 1}"
        _log("sched", f"启动母线程 {pid}（每批 3 个子任务，无限循环）")
        threading.Thread(target=_run_parent, args=(pid,), daemon=True).start()
    return {"ok": True, "parents": parents}


def stop_registration() -> dict[str, Any]:
    """请求停止：母线程在完成当前批次后不再启动新批次，统计本次注册数。"""
    with _LOCK:
        if not _REGISTRAR["running"]:
            return {"ok": False, "error": "没有正在运行的注册任务"}
        _REGISTRAR["stop_requested"] = True
    _log("sched", "停止请求已收到：当前批次完成后停止")
    return {"ok": True}


def _run_parent(parent_id: str, workers: int = BROWSER_WORKERS_PER_PARENT) -> None:
    """母线程：无限循环启动批次，每批 workers 个子任务并发；stop_requested 时停止。"""
    batch = 0
    try:
        while not _REGISTRAR["stop_requested"]:
            batch += 1
            _log(parent_id, f"启动批次 {batch}（{workers} 个子任务并发）")
            threads = []
            for i in range(workers):
                tid = f"{parent_id}-b{batch}-s{i + 1}"
                _log(parent_id, f"启动子任务 {tid}")
                t = threading.Thread(target=_run_one, args=(tid,), daemon=True)
                threads.append(t)
                t.start()
                time.sleep(2)  # 批内小错峰，验证时间互相错开
            for t in threads:
                t.join()
            _log(parent_id, f"批次 {batch} 完成")
        _log(parent_id, "母线程停止（用户请求）")
    finally:
        with _LOCK:
            if _REGISTRAR["running"] and not _REGISTRAR["active"]:
                _REGISTRAR["running"] = False
                stats = dict(_REGISTRAR["stats"])
        _log("sched", f"全部停止。本次共注册 {stats.get('success', 0)} 个账户（失败 {stats.get('failed', 0)}）")


def _run_one(task_id: str) -> None:
    reg: RegistrarBot | None = None
    # 注册机同样遵循 QODER_PROXY：浏览器与 deviceToken poll 都走代理
    proxy = proxy_url()
    if proxy:
        _log(task_id, f"[registrar] using outbound proxy for browser/poll")
    try:
        _set_task(task_id, "registering")
        reg = RegistrarBot(task_id=task_id, cleanup_profile=False, proxy=proxy)
        _bind_bot(task_id, reg)  # 供控制台远程查看/操作浏览器
        acct = reg.register()
        reg.close()
        profile = reg.profile_dir
        _log(task_id, "[registrar] register done")

        _set_task(task_id, "device_auth")
        dev = RegistrarBot(task_id=task_id, profile_dir=profile, cleanup_profile=True, proxy=proxy)
        _bind_bot(task_id, dev)  # device 阶段换用新浏览器，重新绑定
        try:
            cred = dev.device()
        finally:
            dev.close()

        _set_task(task_id, "saving")
        _save_account(task_id, acct, cred)

        result = {
            "email": acct["email"],
            "password": acct["password"],
            "name": acct["name"],
            "device": cred,
        }
        _finish_task(task_id, "success", result=result)
        _log(task_id, "[registrar] ALL DONE, account saved")
    except Exception as e:
        _log(task_id, f"FAILED: {type(e).__name__}: {e}")
        _finish_task(task_id, "failed", error=str(e))
    finally:
        if reg is not None:
            try:
                reg.close()
            except Exception:
                pass


def _save_account(task_id: str, acct: dict, cred: dict) -> str:
    uid = cred.get("user_id") or ""
    if not uid:
        raise ValueError("device 凭据缺少 user_id，无法入库")
    machine_id = str(uuid.uuid4())
    with get_db() as conn:
        existing = conn.execute("SELECT enabled FROM accounts WHERE uid = ?", (uid,)).fetchone()
        enabled = existing[0] if existing else 1
        conn.execute(
            """
            INSERT OR REPLACE INTO accounts (
                uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                enabled, last_status, last_error, quota, is_quota_exceeded, plan, user_tag, next_reset_at, token_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', NULL, 0, 0, 'PLAN_TIER_PRO_TRIAL', 'Pro Trial', NULL, ?)
            """,
            (
                uid, acct.get("name") or "Registered", "personal_standard",
                cred.get("token", ""), cred.get("refresh_token", ""), machine_id,
                enabled, cred.get("expires_at") or "",
            ),
        )
        if not db_get_settings("active_uid"):
            db_set_settings("active_uid", uid)
    _log(task_id, f"[registrar] account saved to DB: {uid} ({acct.get('email')})")
    return uid
