import os
from pathlib import Path


def load_dotenv() -> None:
    """加载当前工作目录的 .env。

    必须容错：.env 是可选配置，目录不可访问或文件不可读时只应跳过，
    不能让整个服务因为一个可选的配置文件而起不来。
    """
    try:
        env_path = Path.cwd() / ".env"
        if not env_path.exists():
            return
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def admin_password() -> str | None:
    value = os.getenv("QODER_ADMIN_PASSWORD", "").strip()
    return value or None


def proxy_url() -> str | None:
    value = os.getenv("QODER_PROXY", "").strip()
    return value or None


def httpx_client_kwargs() -> dict:
    """出站 httpx 参数：仅由 QODER_PROXY 决定是否走代理。

    trust_env=False 是有意为之：
      - 避免容器/宿主机的 HTTP_PROXY、no_proxy 等环境变量隐式生效，
        代理行为完全可预期（只认 QODER_PROXY）。
      - 规避 httpx 解析 no_proxy 中带方括号 IPv6（如 [::1]）时抛
        InvalidURL 的问题。
    """
    return {"proxy": proxy_url(), "trust_env": False}


# ---------------------------------------------------------------------------
# 项目根 .env 兜底读取
#
# load_dotenv() 只加载「当前工作目录」的 .env；服务被 systemd / 容器 / 其他
# 目录启动时读不到项目根 .env。这里为邮件相关配置补一层项目根兜底。
# ---------------------------------------------------------------------------
_PROJECT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"


def dotenv_value(key: str) -> str | None:
    """读取配置：优先进程环境变量，其次当前目录 .env，最后项目根 .env。"""
    value = (os.getenv(key) or "").strip()
    if value:
        return value
    try:
        candidates = (Path.cwd() / ".env", _PROJECT_ENV)
    except OSError:
        candidates = (_PROJECT_ENV,)
    for env_path in candidates:
        try:
            if not env_path.exists():
                continue
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, raw_value = line.partition("=")
                if name.strip() != key:
                    continue
                parsed = raw_value.strip().strip('"').strip("'")
                if parsed:
                    os.environ.setdefault(key, parsed)
                    return parsed
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# 临时邮箱（邮件 provider）配置
# ---------------------------------------------------------------------------
def mail_provider() -> str:
    """邮件后端选择：auto | cloudflare | yyds。"""
    return dotenv_value("QODER_MAIL_PROVIDER") or "auto"


def cf_temp_email_base() -> str | None:
    """cloudflare_temp_email 部署地址，例如 https://mail.example.com。"""
    return dotenv_value("CF_TEMP_EMAIL_BASE")


def cf_temp_email_admin_password() -> str | None:
    """cloudflare_temp_email 的 ADMIN_PASSWORDS，配置后走 /admin/new_address。"""
    return dotenv_value("CF_TEMP_EMAIL_ADMIN_PASSWORD")


def cf_temp_email_site_password() -> str | None:
    """站点私有密码（x-custom-auth），仅在部署启用了 SITE_PASSWORD 时需要。"""
    return dotenv_value("CF_TEMP_EMAIL_SITE_PASSWORD")


def cf_temp_email_domain() -> str | None:
    """指定创建地址使用的域名；留空则由服务端随机选择。"""
    return dotenv_value("CF_TEMP_EMAIL_DOMAIN")


def cf_temp_email_cf_token() -> str | None:
    """Cloudflare Turnstile token；启用匿名创建且开启 Turnstile 时需要。"""
    return dotenv_value("CF_TEMP_EMAIL_CF_TOKEN")


def yyds_api_base() -> str:
    return dotenv_value("YYDS_API_BASE") or "https://maliapi.215.im/v1"


def yyds_api_key() -> str | None:
    return dotenv_value("YYDS_API_KEY")


# ---------------------------------------------------------------------------
# 注册机浏览器配置
# ---------------------------------------------------------------------------
def chromium_path() -> str | None:
    """Chromium 可执行文件路径（容器内通常为 /usr/bin/chromium）。"""
    return dotenv_value("QODER_CHROMIUM_PATH")


def chromium_headless() -> bool:
    """是否以 headless 运行。

    注意：Qoder 注册的人机验证（阿里云滑块）目前无法在 headless 下完成，
    headless 仅适合调试；容器内请配合 Xvfb + VNC 使用有头模式。
    """
    return env_bool("QODER_CHROMIUM_HEADLESS", False)


def chromium_extra_args() -> list[str]:
    """追加的 Chromium 启动参数（空格分隔），便于按环境微调。"""
    raw = dotenv_value("QODER_CHROMIUM_ARGS") or ""
    return [part for part in raw.split() if part]


def remote_browser_enabled() -> bool:
    """是否允许通过控制台查看/操作注册机浏览器（CDP 画面桥）。

    开启后控制台 Register 页签会直接显示浏览器画面并可点击拖动，
    无需 VNC。该接口具备浏览器完全控制能力，务必保持控制台仅管理员可访问。
    """
    return env_bool("QODER_REMOTE_BROWSER", True)


def remote_browser_fps() -> int:
    """画面推送帧率上限（1-30）。越低越省 CPU 与带宽。"""
    try:
        value = int(dotenv_value("QODER_REMOTE_BROWSER_FPS") or "8")
    except ValueError:
        value = 8
    return max(1, min(value, 30))


def remote_browser_quality() -> int:
    """JPEG 画质（10-95）。"""
    try:
        value = int(dotenv_value("QODER_REMOTE_BROWSER_QUALITY") or "60")
    except ValueError:
        value = 60
    return max(10, min(value, 95))
