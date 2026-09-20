"""临时邮箱配置：存储语义与 API 测试。

重点验证「控制台可改」这一行为：数据库优先于环境变量、
敏感项不回传明文、表单留空不误删。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """隔离数据库与环境变量，避免受开发机 .env 影响。"""
    # 切到临时目录，防止 dotenv_value 读到项目根 .env
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QODER_ADMIN_PASSWORD", "test-token")
    for var in (
        "QODER_MAIL_PROVIDER", "CF_TEMP_EMAIL_BASE", "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_SITE_PASSWORD", "CF_TEMP_EMAIL_DOMAIN", "CF_TEMP_EMAIL_CF_TOKEN",
        "YYDS_API_KEY", "YYDS_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)

    from qoder2api import database, mailbox

    # get_db() 读取模块级 DB_PATH，直接替换即可完成隔离
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return mailbox


def test_describe_returns_all_keys(isolated):
    cfg = isolated.describe_config()
    for key in isolated.SETTING_ENV:
        assert key in cfg, key
        assert cfg[key]["source"] == "unset"
        assert cfg[key]["set"] is False


def test_db_overrides_env(isolated, monkeypatch):
    monkeypatch.setenv("CF_TEMP_EMAIL_BASE", "https://env.example.com")
    assert isolated.describe_config()["cf_base"]["source"] == "env"

    isolated.update_config({"cf_base": "https://db.example.com"})
    cfg = isolated.describe_config()["cf_base"]
    assert cfg["value"] == "https://db.example.com"
    assert cfg["source"] == "database"


def test_secrets_never_returned_in_plaintext(isolated):
    secret = "super-secret-value-9876"
    isolated.update_config({"cf_admin_password": secret})

    dumped = json.dumps(isolated.describe_config())
    assert secret not in dumped, "敏感项明文泄漏"

    entry = isolated.describe_config()["cf_admin_password"]
    assert entry["secret"] is True
    assert entry["set"] is True
    assert entry["value"].endswith("9876")
    assert entry["value"].startswith("****")
    # 但底层仍能取回真实值供请求使用
    assert isolated._cfg("cf_admin_password") == secret


def test_blank_secret_keeps_existing(isolated):
    isolated.update_config({"cf_admin_password": "keep-me-1234"})
    # 表单留空（空字符串）不应覆盖
    isolated.update_config({"cf_admin_password": ""})
    assert isolated._cfg("cf_admin_password") == "keep-me-1234"


def test_null_secret_clears(isolated, monkeypatch):
    monkeypatch.setenv("CF_TEMP_EMAIL_ADMIN_PASSWORD", "from-env-5678")
    isolated.update_config({"cf_admin_password": "db-value-9999"})
    assert isolated._cfg("cf_admin_password") == "db-value-9999"

    # None 表示清除覆盖，回退到环境变量
    isolated.update_config({"cf_admin_password": None})
    assert isolated._cfg("cf_admin_password") == "from-env-5678"


def test_empty_non_secret_clears_override(isolated):
    isolated.update_config({"cf_domain": "mail.example.com"})
    assert isolated.describe_config()["cf_domain"]["value"] == "mail.example.com"
    # 非敏感项允许清空
    isolated.update_config({"cf_domain": ""})
    assert isolated.describe_config()["cf_domain"]["source"] == "unset"


def test_unknown_key_rejected(isolated):
    with pytest.raises(KeyError):
        isolated.set_setting("not_a_key", "x")


def test_provider_resolution(isolated, monkeypatch):
    # 都没配 -> 应报错
    with pytest.raises(RuntimeError):
        isolated.active_provider()

    monkeypatch.setenv("CF_TEMP_EMAIL_BASE", "https://mail.example.com")
    assert isolated.active_provider() == "cloudflare"

    # 显式指定 yyds 时不再看 cf_base
    isolated.update_config({"provider": "yyds"})
    assert isolated.active_provider() == "yyds"

    # 非法取值应报错
    isolated.update_config({"provider": "bogus"})
    with pytest.raises(RuntimeError):
        isolated.active_provider()


def test_api_requires_auth(isolated, monkeypatch):
    from fastapi.testclient import TestClient

    import qoder2api.app as app_mod

    client = TestClient(app_mod.app)
    assert client.get("/ui/mail-config").status_code == 401
    assert client.post("/ui/mail-config/test").status_code == 401


def test_api_roundtrip(isolated):
    from fastapi.testclient import TestClient

    import qoder2api.app as app_mod

    client = TestClient(app_mod.app)
    headers = {"X-Gateway-Token": "test-token"}

    resp = client.post("/ui/mail-config",
                       json={"cf_base": "https://api.example.com", "provider": "cloudflare"},
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    got = client.get("/ui/mail-config", headers=headers).json()
    assert got["cf_base"]["value"] == "https://api.example.com"
    assert got["cf_base"]["source"] == "database"
    assert got["_active_provider"] == "cloudflare"


def test_api_test_endpoint_reports_error_without_config(isolated):
    from fastapi.testclient import TestClient

    import qoder2api.app as app_mod

    client = TestClient(app_mod.app)
    resp = client.post("/ui/mail-config/test", headers={"X-Gateway-Token": "test-token"})
    assert resp.status_code == 200
    body = resp.json()
    # 未配置任何后端时，应给出可读错误而不是抛 500
    assert body["ok"] is False
    assert "error" in body and body["error"]
