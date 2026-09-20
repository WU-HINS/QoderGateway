# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# QoderGateway —— 多阶段构建（含注册机所需的 Chromium）
#   stage 1 (web-build) : Node 构建 React 前端（landing / console / docs）
#   stage 2 (runtime)   : Python + Chromium + Xvfb
# 支持 linux/amd64 与 linux/arm64（Debian bookworm 的 chromium 两架构齐备）
# ---------------------------------------------------------------------------

# ============================ stage 1: frontend ============================
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS web-build
WORKDIR /build/frontend

# 先只拷贝清单，最大化利用层缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 文档站的 *.md 通过 import.meta.glob 在构建期打包进 JS，
# 因此 frontend/src/docs/ 必须在构建阶段存在。
# 产物输出到 ../src/qoder2api/static（见 vite.config.ts）
COPY frontend/ ./
RUN mkdir -p /build/src/qoder2api && \
    npm run build && \
    test -f /build/src/qoder2api/static/index.html && \
    test -f /build/src/qoder2api/static/console.html && \
    test -f /build/src/qoder2api/static/docs.html && \
    test -d /build/src/qoder2api/static/assets && \
    echo "[web-build] static assets ok"

# ============================ stage 2: runtime =============================
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    QODER_HOST=0.0.0.0 \
    QODER_PORT=5050 \
    QODER_DATA_DIR=/data \
    QODER_CHROMIUM_PATH=/usr/bin/chromium \
    DISPLAY=:99

WORKDIR /app

# ---------------------------------------------------------------------------
# 系统依赖
#   chromium          —— 注册机的浏览器（Debian 官方包，amd64/arm64 均有）
#   xvfb              —— 虚拟 X 显示，Chromium 需要
#   openbox           —— 轻量窗口管理器，无 WM 时 Chromium 窗口无法正常交互
#   fonts-noto-cjk    —— 中文页面渲染
#
# 不安装 VNC（x11vnc/noVNC/websockify）：人机验证改由控制台的远程浏览器
# 完成 —— 复用 DrissionPage 的 CDP 通道把画面推到控制台并回放输入事件，
# 已验证可穿透滑块所在的跨源 iframe，无需再暴露一个 VNC 端口。
# ---------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        openbox \
        fonts-noto-cjk \
        fonts-liberation \
        curl \
        tini \
        procps \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存），再放入前端产物
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[registrar]"
COPY --from=web-build /build/src/qoder2api/static/ ./src/qoder2api/static/

# 注意：PYTHONPATH=/app/src 使 /app/src/qoder2api 优先于 site-packages，
# 从而 BASE_DIR 指向 /app/src/qoder2api，静态资源与包同目录，无需额外软链。

# 注册机与入口脚本
COPY docker/ /app/docker/
# 显式 755：降权后需要「读取」该脚本重新执行，仅 +x 在极端权限下可能不可读
RUN chmod 755 /app/docker/entrypoint.sh

# 构建期冒烟测试（缺一即构建失败，确保镜像自包含、运行期无需联网安装）：
#   1) import app 会立即 mount StaticFiles，静态资源缺失即构建失败
#   2) 注册机所需的全部运行时组件已预装：Chromium / Xvfb / 窗口管理器 /
#      中文字体 / DrissionPage
#   3) Chromium 真实拉起一次（--version），排除装上了却跑不起来的情况
# 数据库目录用临时路径，避免与后面 /data 的属主设置产生顺序耦合。
RUN QODER_DATA_DIR=/tmp/smoke-data python -c "\
import pathlib, qoder2api.app as a; \
base = pathlib.Path(a.BASE_DIR); \
print('[smoke] BASE_DIR =', base); \
assert (base / 'static' / 'index.html').exists(), 'index.html missing'; \
assert (base / 'static' / 'console.html').exists(), 'console.html missing'; \
assert (base / 'static' / 'docs.html').exists(), 'docs.html missing'; \
assert (base / 'static' / 'assets').is_dir(), 'assets/ missing'; \
import DrissionPage; \
print('[smoke] DrissionPage ok'); \
print('[smoke] static assets resolvable')" \
    && for bin in Xvfb openbox curl tini; do \
           command -v "$bin" >/dev/null 2>&1 || { echo "[smoke] MISSING binary: $bin"; exit 1; }; \
       done \
    && find /usr/share/fonts -iname '*CJK*' -print -quit | grep -q . \
    && test -x "${QODER_CHROMIUM_PATH}" \
    && "${QODER_CHROMIUM_PATH}" --version \
    && echo "[smoke] all runtime components preinstalled"

# 运行身份。注意：容器以 root 启动 entrypoint —— 因为下面声明了 VOLUME /data，
# Docker 挂载新卷时属主是 root，会覆盖这里 chown 的结果，导致非 root 用户
# 无法写数据库。entrypoint 会先修正属主，再降权到 qoder 运行全部进程。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin qoder && \
    mkdir -p /data && \
    chown -R qoder:qoder /data /app && \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
# /tmp/.X11-unix 必须保持 root 属主（Xvfb 会检查），仅放开写权限

VOLUME ["/data"]
# 5050 网关（人机验证通过控制台内嵌的远程浏览器完成）
EXPOSE 5050

# /console 无开关，最稳定
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${QODER_PORT}/console" >/dev/null || exit 1

# 以 root 启动：entrypoint 需先修正数据卷属主，再降权到 qoder 运行
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
