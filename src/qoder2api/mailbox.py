"""
邮件 provider 抽象层：为注册机提供「创建临时邮箱 + 轮询验证码」的统一能力。

支持后端（由 QODER_MAIL_PROVIDER 选择：auto | cloudflare | yyds）：

cloudflare —— cloudflare_temp_email（dreamhunter2333/cloudflare_temp_email）
    POST {base}/api/new_address               {"name","domain","cf_token"} -> {"address","jwt","address_id"}
    POST {base}/admin/new_address             需 x-admin-auth；可绕过 Turnstile 与匿名创建限制
    GET  {base}/api/parsed_mails?limit&offset 需 Authorization: Bearer <address JWT>
         -> {"results":[{"id","subject","text","html","source","created_at"}],"count"}
    站点启用 SITE_PASSWORD 时，所有请求需带 x-custom-auth。

yyds —— maliapi.215.im（原实现，需 YYDS_API_KEY）
    POST {base}/accounts          {"localPart"} -> {"data":{"address"}}
    GET  {base}/messages/next     ?address&wait -> {"data":{"message":{...}}}

对外统一入口：
    create_mailbox()            -> Mailbox
    wait_verification_code()    -> str（6 位数字验证码）
"""
from __future__ import annotations

import html as _html_mod
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .database import get_db
from .env import dotenv_value, httpx_client_kwargs

# ---------------------------------------------------------------------------
# 配置来源
#
# 数据库优先（控制台可在线修改并立即生效），环境变量 / .env 作为回退。
# 这样改一个域名或密钥不必去改 .env 再重启服务。
# ---------------------------------------------------------------------------
YYDS_DEFAULT_BASE = "https://maliapi.215.im/v1"

# 配置项 -> 环境变量名（数据库中统一加 mail_ 前缀存放）
SETTING_ENV: dict[str, str] = {
    "provider": "QODER_MAIL_PROVIDER",
    "cf_base": "CF_TEMP_EMAIL_BASE",
    "cf_admin_password": "CF_TEMP_EMAIL_ADMIN_PASSWORD",
    "cf_site_password": "CF_TEMP_EMAIL_SITE_PASSWORD",
    "cf_domain": "CF_TEMP_EMAIL_DOMAIN",
    "cf_cf_token": "CF_TEMP_EMAIL_CF_TOKEN",
    "yyds_api_key": "YYDS_API_KEY",
    "yyds_api_base": "YYDS_API_BASE",
}

# 敏感项：回传控制台时只给掩码，绝不返回明文
SECRET_SETTINGS = ("cf_admin_password", "cf_site_password", "cf_cf_token", "yyds_api_key")

_DB_PREFIX = "mail_"


def _db_value(key: str) -> str | None:
    """读取数据库中的覆盖值；不存在或为空则返回 None。"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (_DB_PREFIX + key,)
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    value = (row[0] or "").strip()
    return value or None


def _cfg(key: str) -> str | None:
    """读取配置：数据库（控制台）优先，其次环境变量 / .env。"""
    return _db_value(key) or dotenv_value(SETTING_ENV[key])


def set_setting(key: str, value: str | None) -> None:
    """写入或清除配置；空值表示删除覆盖，回退到环境变量。"""
    if key not in SETTING_ENV:
        raise KeyError(f"未知配置项: {key}")
    with get_db() as conn:
        if value is None or not str(value).strip():
            conn.execute("DELETE FROM settings WHERE key = ?", (_DB_PREFIX + key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (_DB_PREFIX + key, str(value).strip()),
            )


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def describe_config() -> dict[str, Any]:
    """供控制台读取：敏感项只返回掩码与是否已设置。"""
    items: dict[str, Any] = {}
    for key, env_name in SETTING_ENV.items():
        value = _cfg(key)
        from_db = _db_value(key) is not None
        items[key] = {
            "env_var": env_name,
            "source": "database" if from_db else ("env" if value else "unset"),
            "secret": key in SECRET_SETTINGS,
            "set": bool(value),
            "value": _mask(value) if key in SECRET_SETTINGS else (value or ""),
        }
    try:
        items["_active_provider"] = active_provider()
    except RuntimeError:
        items["_active_provider"] = None
    return items


def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    """供控制台保存。

    敏感项的语义（避免表单留空时误删已有密钥）：
      - 字段不存在      -> 不修改
      - 值为 None       -> 清除该项
      - 值为空字符串    -> 不修改（用户没填）
      - 值为非空字符串  -> 写入
    """
    changed: list[str] = []
    for key in SETTING_ENV:
        if key not in payload:
            continue
        raw = payload[key]
        if key in SECRET_SETTINGS:
            if raw is None:
                set_setting(key, None)
                changed.append(key)
            elif str(raw).strip():
                set_setting(key, str(raw))
                changed.append(key)
            continue
        set_setting(key, None if raw is None else str(raw))
        changed.append(key)
    return {"updated": changed}

# Qoder 邮箱验证码为 6 位纯数字
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_TAG_RE = re.compile(r"<[^>]+>")

Logger = Callable[[str], None]


@dataclass
class Mailbox:
    """一个已创建的临时邮箱。token 为 cloudflare 的 address JWT（yyds 用不到）。"""

    address: str
    provider: str
    token: str = ""
    address_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# address -> Mailbox，供「只有 address 字符串」的旧调用点反查 JWT
_CACHE: dict[str, Mailbox] = {}


def _noop(_msg: str) -> None:
    pass


def _log_with(log: Logger | None, task_id: str | None, msg: str) -> None:
    if log is not None:
        log(msg)
        return
    print(f"[{task_id or 'mail'}] {msg}")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _html_mod.unescape(_TAG_RE.sub(" ", text))


def first_code(text: str | None) -> str | None:
    if not text:
        return None
    found = CODE_RE.findall(text)
    return found[0] if found else None


def extract_code(msg: dict[str, Any] | None) -> str | None:
    """从一条邮件/消息记录里提取验证码，尽量覆盖不同 provider 的字段命名。"""
    if not isinstance(msg, dict):
        return None
    for key in ("verificationCode", "verification_code", "code"):
        value = msg.get(key)
        if value:
            code = first_code(str(value)) or str(value).strip()
            if code:
                return code
    parts: list[str] = []
    for key in ("subject", "text", "html", "content", "preview", "summary"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            parts.append(strip_html(value) if key in ("html", "content") else value)
    return first_code("\n".join(parts))


# ---------------------------------------------------------------------------
# cloudflare_temp_email
# ---------------------------------------------------------------------------
def _cf_base() -> str:
    base = _cfg("cf_base")
    if not base:
        raise RuntimeError(
            "CF_TEMP_EMAIL_BASE 未配置：请设置为 cloudflare_temp_email 部署地址，"
            "例如 https://mail.example.com"
        )
    return base.rstrip("/")


def _cf_headers(token: str = "", admin: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    site_password = _cfg("cf_site_password")
    if site_password:
        headers["x-custom-auth"] = site_password
    if admin:
        admin_password = _cfg("cf_admin_password")
        if admin_password:
            headers["x-admin-auth"] = admin_password
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _cf_unwrap(payload: Any) -> dict[str, Any]:
    """兼容 {..} 与 {"data": {..}} 两种包裹形态。"""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            merged = dict(inner)
            for key, value in payload.items():
                merged.setdefault(key, value)
            return merged
        return payload
    return {}


def _cf_create(prefix: str, task_id: str | None, log: Logger | None) -> Mailbox:
    base = _cf_base()
    admin_password = _cfg("cf_admin_password")
    use_admin = bool(admin_password)

    name = f"{prefix}{uuid.uuid4().hex[:8]}"
    body: dict[str, Any] = {"name": name, "enableRandomSubdomain": False}
    domain = _cfg("cf_domain")
    if domain:
        body["domain"] = domain
    turnstile = _cfg("cf_cf_token")
    if turnstile:
        body["cf_token"] = turnstile

    url = f"{base}{'/admin/new_address' if use_admin else '/api/new_address'}"
    response = httpx.post(
        url,
        json=body,
        headers=_cf_headers(admin=use_admin),
        timeout=25,
        **httpx_client_kwargs(),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"cloudflare_temp_email 创建地址失败 HTTP {response.status_code}: {response.text[:200]}"
        )

    data = _cf_unwrap(response.json())
    address = str(data.get("address") or "").strip()
    token = str(data.get("jwt") or data.get("token") or "").strip()
    if not address:
        raise RuntimeError(f"cloudflare_temp_email 响应缺少 address 字段: {response.text[:200]}")

    mailbox = Mailbox(
        address=address,
        provider="cloudflare",
        token=token,
        address_id=data.get("address_id") if isinstance(data.get("address_id"), int) else None,
        meta={"mode": "admin" if use_admin else "anonymous", "password": data.get("password")},
    )
    _log_with(log, task_id, f"[mail] created {address} (cloudflare/{mailbox.meta['mode']})")
    return mailbox


def _cf_fetch_code(mailbox: Mailbox, seen: set[Any], task_id: str | None, log: Logger | None) -> str | None:
    base = _cf_base()
    response = httpx.get(
        f"{base}/api/parsed_mails",
        params={"limit": 20, "offset": 0},
        headers=_cf_headers(mailbox.token),
        timeout=25,
        **httpx_client_kwargs(),
    )
    if response.status_code == 401:
        raise RuntimeError(
            "cloudflare_temp_email 鉴权失败(401)：address JWT 无效/过期，"
            "或站点启用了 SITE_PASSWORD 但 CF_TEMP_EMAIL_SITE_PASSWORD 未配置"
        )
    if response.status_code == 429:
        _log_with(log, task_id, "[mail] 429 rate limited, backing off 5s")
        time.sleep(5)
        return None
    if response.status_code != 200:
        _log_with(log, task_id, f"[mail] parsed_mails HTTP {response.status_code}")
        return None

    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    for item in results:
        if not isinstance(item, dict):
            continue
        mail_id = item.get("id")
        if mail_id in seen:
            continue
        seen.add(mail_id)
        code = extract_code(item)
        if code:
            _log_with(log, task_id, f"[mail] verification code = {code}")
            return code
        _log_with(log, task_id, f"[mail] mail id={mail_id} 未发现验证码，继续轮询")
    return None


# ---------------------------------------------------------------------------
# yyds（maliapi.215.im）
# ---------------------------------------------------------------------------
def _yyds_create(prefix: str, task_id: str | None, log: Logger | None) -> Mailbox:
    key = _cfg("yyds_api_key")
    if not key:
        raise RuntimeError(
            "YYDS_API_KEY 未配置：请在项目根 .env 或系统环境变量中设置 YYDS_API_KEY（AC- 开头）"
        )
    local = f"{prefix}{uuid.uuid4().hex[:8]}"
    response = httpx.post(
        f"{(_cfg('yyds_api_base') or YYDS_DEFAULT_BASE)}/accounts",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        json={"localPart": local},
        timeout=20,
        **httpx_client_kwargs(),
    )
    response.raise_for_status()
    address = response.json()["data"]["address"]
    _log_with(log, task_id, f"[mail] created {address} (yyds)")
    return Mailbox(address=address, provider="yyds", token=key)


def _yyds_fetch_code(mailbox: Mailbox, task_id: str | None, log: Logger | None) -> str | None:
    key = _cfg("yyds_api_key") or mailbox.token
    response = httpx.get(
        f"{(_cfg('yyds_api_base') or YYDS_DEFAULT_BASE)}/messages/next",
        params={"address": mailbox.address, "wait": 30},
        headers={"X-API-Key": key},
        timeout=45,
        **httpx_client_kwargs(),
    )
    if response.status_code == 200:
        msg = response.json().get("data", {}).get("message")
        code = extract_code(msg)
        if code:
            _log_with(log, task_id, f"[mail] verification code = {code}")
        return code
    if response.status_code == 204:
        return None
    _log_with(log, task_id, f"[mail] unexpected status {response.status_code}")
    return None


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------
def active_provider() -> str:
    """解析实际使用的 provider（auto 时按配置可用性决定）。"""
    configured = ((_cfg("provider") or "auto") or "auto").strip().lower()
    if configured in ("cloudflare", "cf", "cf_temp_email"):
        return "cloudflare"
    if configured == "yyds":
        return "yyds"
    if configured != "auto":
        raise RuntimeError(
            f"QODER_MAIL_PROVIDER 取值非法: {configured}（可选 auto | cloudflare | yyds）"
        )
    if _cfg("cf_base"):
        return "cloudflare"
    if _cfg("yyds_api_key"):
        return "yyds"
    raise RuntimeError(
        "未配置任何邮件后端：请设置 CF_TEMP_EMAIL_BASE（cloudflare_temp_email）"
        "或 YYDS_API_KEY，并将 QODER_MAIL_PROVIDER 设为 auto/cloudflare/yyds"
    )


def create_mailbox(
    prefix: str = "qoder",
    task_id: str | None = None,
    log: Logger | None = None,
) -> Mailbox:
    """创建临时邮箱，并登记到缓存供后续只传 address 的调用点使用。"""
    provider = active_provider()
    mailbox = _cf_create(prefix, task_id, log) if provider == "cloudflare" else _yyds_create(prefix, task_id, log)
    _CACHE[mailbox.address] = mailbox
    return mailbox


def recall(address: str) -> Mailbox | None:
    return _CACHE.get(address)


def wait_verification_code(
    mailbox: Mailbox | str,
    task_id: str | None = None,
    timeout: float = 120.0,
    log: Logger | None = None,
) -> str:
    """轮询等待验证码。mailbox 可传 Mailbox，也可传 address 字符串（走缓存反查）。"""
    if isinstance(mailbox, Mailbox):
        box = mailbox
    else:
        box = _CACHE.get(mailbox) or Mailbox(address=mailbox, provider=active_provider())

    deadline = time.time() + timeout
    seen: set[Any] = set()
    interval = 2.0
    while time.time() < deadline:
        try:
            code = (
                _cf_fetch_code(box, seen, task_id, log)
                if box.provider == "cloudflare"
                else _yyds_fetch_code(box, task_id, log)
            )
            if code:
                return code
        except httpx.HTTPError as exc:
            _log_with(log, task_id, f"[mail] poll error: {exc}")
        except RuntimeError:
            raise
        time.sleep(interval)
        interval = min(interval + 0.5, 5.0)
    raise TimeoutError(f"no verification code within {timeout}s for {box.address}")


# ---------------------------------------------------------------------------
# 兼容旧函数名（原 yyds_* 调用点）
# ---------------------------------------------------------------------------
def yyds_create_mailbox(prefix: str = "qoder", task_id: str | None = None) -> str:
    return create_mailbox(prefix=prefix, task_id=task_id).address


def yyds_wait_code(address: str, task_id: str | None = None, timeout: float = 120.0) -> str:
    return wait_verification_code(address, task_id=task_id, timeout=timeout)


def probe() -> dict[str, Any]:
    """诊断当前邮件配置，供 --check / 控制台展示。"""
    info: dict[str, Any] = {
        "configured_provider": (_cfg("provider") or "auto"),
        "cf_base": _cfg("cf_base"),
        "cf_admin": bool(_cfg("cf_admin_password")),
        "cf_site_password": bool(_cfg("cf_site_password")),
        "cf_domain": _cfg("cf_domain"),
        "yyds_key": bool(_cfg("yyds_api_key")),
    }
    try:
        info["active_provider"] = active_provider()
        info["ok"] = True
    except RuntimeError as exc:
        info["active_provider"] = None
        info["ok"] = False
        info["error"] = str(exc)
    return info
