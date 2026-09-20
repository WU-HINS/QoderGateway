<h1 align="center">QoderGate</h1>

<p align="center">
  把多个 Qoder 账号统一转换成 OpenAI 兼容接口的本地网关。<br>
  A local gateway that turns multiple Qoder accounts into one OpenAI-compatible API.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue?logo=python&logoColor=white" alt="Python >= 3.11">
  <img src="https://img.shields.io/badge/fastapi-0.115+-green?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="License">
  <a href="https://linux.do"><img src="https://img.shields.io/badge/LINUX_DO-%E7%A4%BE%E5%8C%BA-blue" alt="LINUX DO"></a>
</p>

---

## 致谢 / Acknowledgment

本项目思路来源于 [cubk1/qoder2api](https://github.com/cubk1/qoder2api/)，在此基础上用 Python 重写了后端并新增了 WebUI 管理控制台、SQLite 持久化、多账号池轮转和独立文档站。

This project is inspired by [cubk1/qoder2api](https://github.com/cubk1/qoder2api/). We rewrote the backend in Python and added a WebUI management console, SQLite persistence, multi-account pool rotation, and a standalone documentation site.

特别感谢 [LINUX DO](https://linux.do) 社区提供的交流与推广平台。

Special thanks to the [LINUX DO](https://linux.do) community for the platform of exchange and promotion.

## 功能 / Features

- **OpenAI 兼容接口** — 通过 `/v1/chat/completions` 向客户端提供标准 Chat Completions API
- **多账号池** — 导入多个 Qoder 账号，按 UID 自动去重，请求失败时自动轮转
- **两层鉴权** — 管理后台密钥与外部 API Key 分开配置
- **SQLite 持久化** — 账号、API Key、全局配置全部存入本地数据库
- **WebUI 控制台** — Dashboard、账号管理、API Key 管理、Playground、服务日志
- **独立文档站** — `/documents` 提供中英文 Wiki，支持本地搜索和目录跳转
- **自动检测语言** — 根据浏览器地区自动切换中文/英文

## 快速开始 / Quickstart

### 安装 / Install

```bash
git clone https://github.com/WU-HINS/QoderGateway.git
cd QoderGateway
uv sync
```

### 前端构建 / Build Frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

构建产物会输出到 `src/qoder2api/static/`，后端启动时直接托管 WebUI 与文档站。

### 配置 / Configure

```bash
cp .env.example .env
```

编辑 `.env`，修改管理员密码：

```env
QODER_ADMIN_PASSWORD=your-strong-password
```

> **默认密码是 `admin`，强烈建议第一次登录后立即修改。**

### 启动 / Start

```bash
uv run qoder2api
```

服务默认运行在 `http://127.0.0.1:5050/`。

| 路径 | 说明 |
|------|------|
| `/` | Landing Page |
| `/console` | 管理控制台 |
| `/documents` | 文档站 / Wiki |
| `/v1/chat/completions` | OpenAI 兼容 API |

### 第一次 API 调用 / First API Call

在控制台导入账号后：

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "lite",
    "messages": [{ "role": "user", "content": "Hello" }],
    "stream": false
  }'
```

## Docker / Container

多架构镜像（`linux/amd64` + `linux/arm64`）由 GitHub Actions 构建并推送到 GHCR。

> **镜像名规则**：GHCR 要求仓库路径**全小写**。CI 会用 `tr '[:upper:]' '[:lower:]'` 把
> `github.repository` 归一化后再拼接，因此本仓库 `WU-HINS/QoderGateway` 对应
> `ghcr.io/wu-hins/qodergateway`。Fork 后请按同样规则替换下方命令中的镜像名。

**构建触发策略（一次合并 = 一次镜像构建）：**

| 事件 | verify + test | 构建镜像 | 推送 |
|---|---|---|---|
| PR 到 `main` | ✅ | ❌ | ❌ |
| 合并到 `main` | ✅ | ✅ 一次 | `:edge` `:main` `:sha` |
| push tag `v*` | ✅ | ✅ 一次 | 版本号标签 |
| 手动触发 | ✅ | ✅ 一次 | 同上 |

PR 阶段只做质量门禁、不构建镜像，避免同一次改动在 PR 与合并后各构建一轮；镜像只在代码进入 `main` 或打 tag 时产出。main/tag 的构建不会被并发取消，保证每次合并都真实产出一个镜像。

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  -e QODER_PROXY=http://host.docker.internal:7890 \
  ghcr.io/wu-hins/qodergateway:latest
```

或用 compose（自动读取同目录 `.env`）：

```bash
cp .env.example .env   # 按需填写
docker compose up -d
```

镜像特性（全部在构建阶段预装，运行期无需联网安装）：

| 组件 | 用途 |
|---|---|
| Chromium 153（Debian 官方包，amd64/arm64 均有） | 注册机浏览器 |
| Xvfb | 虚拟 X 显示（有头 Chromium 必需） |
| openbox | 轻量窗口管理器 |
| fonts-noto-cjk | 中文页面渲染 |
| DrissionPage（`[registrar]` extra） | 浏览器自动化 |
| 前端产物（Landing / Console / Docs） | 构建阶段编译进镜像 |

- 数据持久化在 `/data`（SQLite）。容器以 root 启动入口脚本，先修正数据卷属主，
  再降权到非 root（uid 10001）运行全部进程——因此**挂载全新命名卷也能直接写入**，
  不需要事先 `chown`
- 容器内已设置 `QODER_HOST=0.0.0.0`，直接映射端口即可访问
- 构建期会逐项校验上述组件；任一缺失则**构建失败**，不会把问题带到运行期

### 注册机与人机验证（控制台内直接完成）

Qoder 注册的人机验证是阿里云滑块，**必须由人拖动**，headless 下无法完成。本项目的做法是把注册机浏览器的**实时画面直接嵌进 Web 控制台**：

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  --shm-size=1g \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  ghcr.io/wu-hins/qodergateway:latest
```

在控制台 **Register** 页签启动注册机，页面会出现「远程浏览器」面板。等滑块出现时，直接在面板画面上拖动即可完成验证——**不需要打开 VNC，也不需要额外端口**。

实现方式：DrissionPage 本身就是通过 CDP 驱动 Chromium 的，这里复用同一条调试通道，把 `Page.captureScreenshot` 的画面经 WebSocket 推给前端 `<canvas>`，并把鼠标/键盘事件用 `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` 回放。容器内会启动 Xvfb（有头 Chromium 需要 X display），但**不安装也不暴露 VNC**。

- 帧率与画质可调：`QODER_REMOTE_BROWSER_FPS`（默认 8）、`QODER_REMOTE_BROWSER_QUALITY`（默认 60）
- 关闭该功能：`QODER_REMOTE_BROWSER=0`
- 必须加 `--shm-size=1g`：默认 64MB 的 `/dev/shm` 会导致 Chromium 渲染进程崩溃
- **并发建议**：每个子任务都会拉起一个独立 Chromium。机械硬盘或低配环境
  请保持 `parents=1`（默认值）——并发启动多个浏览器会打满磁盘 IO，
  进而导致浏览器连接超时（`BrowserConnectError`）

> **安全提示**：远程浏览器等同于把注册机浏览器的完全控制权交给控制台使用者（可访问该浏览器中的所有已登录会话）。请务必修改默认管理员密码，不要将控制台暴露到公网。

## 临时邮箱 / Temp Mail

注册机需要一个能收验证码的临时邮箱后端。

**推荐直接在控制台配置**：Register 页签 →「临时邮箱配置」，填写后保存即生效，
无需重启服务。设置写入本地 SQLite；敏感项（密码/Token）在界面上以掩码显示，
不会回传明文，留空表示保持原值。环境变量仅作为首次部署的默认值/回退。

后端通过 `QODER_MAIL_PROVIDER`（或控制台的下拉框）选择：

| 取值 | 说明 |
|------|------|
| `auto` | 配了 `CF_TEMP_EMAIL_BASE` 则用 cloudflare，否则回退 yyds |
| `cloudflare` | 自建 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)（推荐） |
| `yyds` | [maliapi.215.im](https://maliapi.215.im)（需 `YYDS_API_KEY`） |

### 为什么推荐 cloudflare_temp_email

- 基于 Cloudflare 免费额度自建，地址与域名完全自主，不依赖第三方临时邮箱服务
- 官方提供 `/api/parsed_mails` 解析接口，直接返回 `text`/`html`/`subject`，无需自行解析 MIME
- 配置 `ADMIN_PASSWORDS` 后可走 `/admin/new_address`，**绕过 Turnstile 人机验证与"禁止匿名创建"限制**，适合无人值守批量注册

### 配置要点

1. 按[官方文档](https://temp-mail-docs.awsl.uk)部署 worker，记下访问地址（`CF_TEMP_EMAIL_BASE`）
2. 强烈建议在 `wrangler.toml` 配置 `ADMIN_PASSWORDS`，并同步填入 `CF_TEMP_EMAIL_ADMIN_PASSWORD`
3. 若启用了 `SITE_PASSWORD`，需一并配置 `CF_TEMP_EMAIL_SITE_PASSWORD`
4. 可选 `CF_TEMP_EMAIL_DOMAIN` 固定使用某个域名

> `worker.dev` 默认域名在中国大陆无法访问，请绑定自定义域名。

## 环境变量 / Environment Variables

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QODER_HOST` | 服务绑定地址 | `127.0.0.1` |
| `QODER_PORT` | 服务端口 | `5050` |
| `QODER_ADMIN_PASSWORD` | 管理员密码（覆盖 SQLite 存储值） | `admin` |
| `QODER_PROXY` | 出站代理地址 | 空 |
| `QODER_ENABLE_DOCUMENTS` | 是否启用文档页 | `1` |
| `QODER_ENABLE_LANDING` | 是否启用 Landing Page | `1` |
| `QODER_PAT` | 首次启动时自动导入的 PAT | 空 |
| `QODER_DATA_DIR` | 数据目录（SQLite 位置） | `~/.qoder` |
| `QODER_MAIL_PROVIDER` | 临时邮箱后端（控制台可覆盖）：`auto` / `cloudflare` / `yyds` | `auto` |
| `CF_TEMP_EMAIL_BASE` | cloudflare_temp_email 部署地址 | 空 |
| `CF_TEMP_EMAIL_ADMIN_PASSWORD` | cloudflare_temp_email 管理员密码 | 空 |
| `CF_TEMP_EMAIL_SITE_PASSWORD` | 站点私有密码（`x-custom-auth`） | 空 |
| `CF_TEMP_EMAIL_DOMAIN` | 创建地址使用的域名 | 随机 |
| `CF_TEMP_EMAIL_CF_TOKEN` | Turnstile token（未用管理员模式时需要） | 空 |
| `YYDS_API_KEY` | yyds 临时邮箱 API Key（旧方案） | 空 |
| `QODER_CHROMIUM_PATH` | Chromium 可执行文件路径 | 自动查找 |
| `QODER_CHROMIUM_HEADLESS` | 是否 headless（滑块验证需设为 0） | `0` |
| `QODER_CHROMIUM_ARGS` | 追加的 Chromium 启动参数 | 空 |
| `QODER_REMOTE_BROWSER` | 控制台内嵌远程浏览器（免 VNC） | `1` |
| `QODER_REMOTE_BROWSER_FPS` | 远程浏览器帧率上限（1-30） | `8` |
| `QODER_REMOTE_BROWSER_QUALITY` | 远程浏览器 JPEG 画质（10-95） | `60` |
| `QODER_SCREEN` | 虚拟屏分辨率 | `1440x900x24` |

## 项目结构 / Project Structure

```
├── src/qoder2api/          # Python 后端
│   ├── app.py              # FastAPI 路由
│   ├── accounts.py         # SQLite 账号管理
│   ├── auth.py             # Qoder 鉴权与签名
│   ├── bridge.py           # OpenAI 兼容响应转换
│   ├── config.py           # 配置读写
│   ├── database.py         # SQLite schema
│   ├── env.py              # 环境变量加载
│   └── static/             # 前端构建产物
├── frontend/               # React 前端源码
│   ├── src/App.tsx         # 管理控制台
│   ├── src/docs-main.tsx   # 文档站
│   ├── src/landing-main.tsx# Landing Page
│   └── src/docs/           # 中英文 Markdown 文档
├── .env.example            # 环境变量模板
└── pyproject.toml          # 项目配置
```

## 测试 / Tests

远程浏览器（CDP 画面桥）有真实浏览器端到端测试，覆盖画面下发、拖动滑块、鉴权与降级：

```bash
# 需要本机有 Chromium/Chrome；缺失时相关用例会自动跳过
pip install -e ".[dev,registrar]"
pytest tests/ -v
```

CI 中由 `browser-actions/setup-chrome` 提供浏览器，测试通过后才会构建镜像。

## License

MIT