# 架构

QoderGate 把 OpenAI 兼容客户端请求桥接到 Qoder 会话。

## 请求流程

```text
Client
  -> FastAPI /v1/chat/completions
  -> API Key 校验
  -> SQLite 账号路由器
  -> 出站请求（可选 QODER_PROXY）
  -> Qoder 上游 API
  -> OpenAI 兼容响应
```

## 后端模块

| 模块 | 职责 |
| --- | --- |
| `app.py` | FastAPI 路由、UI 鉴权、请求路由与账号轮转。 |
| `accounts.py` | SQLite 账号 CRUD 和活跃会话选择。 |
| `auth.py` | PAT 交换、本地 auth 导入、用户状态查询。 |
| `bridge.py` | 上游 `/model/v1/chat/completions` 调用与 OpenAI 兼容流式/非流式响应转换。 |
| `mailbox.py` | 临时邮箱抽象层：cloudflare_temp_email / yyds。 |
| `registrar.py` | 注册机服务：浏览器自动化、人机验证调度、账号入库。 |
| `remotebrowser.py` | CDP 画面桥：把注册机浏览器画面推给控制台并回放输入事件。 |
| `tokens.py` | token 刷新、配额查询与 6 小时定时刷新线程。 |
| `database.py` | SQLite schema 和连接帮助函数。 |
| `config.py` | 网关配置与 API Key 读写。 |
| `env.py` | 环境变量加载与出站代理/邮箱配置读取。 |
| `encoding.py` | 上游自定义 base64 编码（新版协议不使用）。 |
| `signature.py` | COSY 签名常量与实现（老版协议路径）。 |

## 出站网络

所有出站请求统一经 `env.httpx_client_kwargs()` 构造，代理**仅**由 `QODER_PROXY` 决定，不读取 `HTTP_PROXY` 等环境变量。

## 前端模块

WebUI 使用 Vite、React、Tailwind CSS、GSAP 和 Markdown 渲染构建。

构建产物会输出到：

```text
src/qoder2api/static
```

FastAPI 会直接服务编译后的 `index.html`、`console.html`、`docs.html` 和静态资源。

## 容器

`Dockerfile` 为多阶段构建：Node 阶段编译前端，Python 阶段安装网关并拷入前端产物。构建期会执行冒烟测试，验证 `StaticFiles` 挂载的静态资源可解析。

镜像包含注册机所需的 Chromium 与 Xvfb。人机验证在有头浏览器中人工完成，默认通过 `remotebrowser.py` 的 CDP 画面桥在 Web 控制台内直接操作（无需 VNC）；设置 `QODER_ENABLE_VNC=1` 可改用传统 VNC。

仓库的 GitHub Actions 工作流使用原生 amd64 与 arm64 runner 分别构建，再合并为多架构 manifest 推送到 GHCR。