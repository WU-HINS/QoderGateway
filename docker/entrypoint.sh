#!/bin/sh
# ---------------------------------------------------------------------------
# 容器入口：编排虚拟显示、可选 VNC、以及网关进程
#
# 为什么需要 Xvfb：
#   Qoder 注册的人机验证是阿里云滑块，必须在有头浏览器中由人拖动完成。
#   有头 Chromium 需要 X display，因此 Xvfb 始终启动。
#
# 两种「人工验证」方式：
#   1) 远程浏览器（推荐，默认）：在 Web 控制台 Register 页签直接看画面并拖动，
#      走 CDP 通道，不需要 VNC。由 QODER_REMOTE_BROWSER 控制。
#   2) VNC（后备）：额外启动 x11vnc + noVNC，用 VNC 客户端连 6080 端口。
#      由 QODER_ENABLE_VNC=1 显式开启。
#
# 环境变量：
#   QODER_ENABLE_VNC=1       开启 VNC（默认 0，仅用远程浏览器）
#   QODER_VNC_PASSWORD       VNC 密码；留空则随机生成并打印
#   QODER_SCREEN             分辨率，默认 1440x900x24
#   QODER_DISPLAY            默认 :99
#   QODER_NOVNC_PORT         默认 6080
# ---------------------------------------------------------------------------
set -e

DATA_DIR="${QODER_DATA_DIR:-/data}"
DISPLAY_NUM="${QODER_DISPLAY:-:99}"
VNC_PORT="${QODER_VNC_PORT:-5900}"
NOVNC_PORT="${QODER_NOVNC_PORT:-6080}"
SCREEN="${QODER_SCREEN:-1440x900x24}"
ENABLE_VNC="${QODER_ENABLE_VNC:-0}"

XVFB_PID=""
WM_PID=""
X11VNC_PID=""
WS_PID=""
APP_PID=""

log() { echo "[entrypoint] $*"; }

cleanup() {
    log "shutting down..."
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
    [ -n "$WS_PID" ] && kill "$WS_PID" 2>/dev/null || true
    [ -n "$X11VNC_PID" ] && kill "$X11VNC_PID" 2>/dev/null || true
    [ -n "$WM_PID" ] && kill "$WM_PID" 2>/dev/null || true
    [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

mkdir -p "$DATA_DIR"

# ---------------------------------------------------------------------------
# 虚拟显示：始终启动（有头 Chromium 必需）
# ---------------------------------------------------------------------------
export DISPLAY="$DISPLAY_NUM"

log "starting Xvfb $DISPLAY_NUM ($SCREEN)"
Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp &
XVFB_PID=$!

i=0
while [ $i -lt 50 ]; do
    if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ]; then break; fi
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        log "ERROR: Xvfb exited unexpectedly"; exit 1
    fi
    sleep 0.2
    i=$((i + 1))
done
log "X server ready on $DISPLAY_NUM"

# 轻量窗口管理器：无 WM 时 Chromium 窗口无焦点、交互异常
if command -v openbox >/dev/null 2>&1; then
    openbox --sm-disable >/dev/null 2>&1 &
    WM_PID=$!
    log "window manager: openbox"
fi

# ---------------------------------------------------------------------------
# VNC：可选后备方案（默认关闭，优先用控制台的远程浏览器）
# ---------------------------------------------------------------------------
if [ "$ENABLE_VNC" = "1" ]; then
    if [ -z "${QODER_VNC_PASSWORD:-}" ]; then
        QODER_VNC_PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)
        log "############################################################"
        log " VNC password (generated, please keep it): $QODER_VNC_PASSWORD"
        log "############################################################"
    fi
    mkdir -p "$HOME/.vnc"
    x11vnc -storepasswd "$QODER_VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1

    log "starting x11vnc on :$VNC_PORT"
    x11vnc -display "$DISPLAY_NUM" -forever -shared -quiet \
        -rfbauth "$HOME/.vnc/passwd" -rfbport "$VNC_PORT" >/dev/null 2>&1 &
    X11VNC_PID=$!

    # 以 vnc.html 为准，而非仅目录存在：websockify --web 需要入口文件
    NOVNC_WEB=/usr/share/novnc
    [ -f "$NOVNC_WEB/vnc.html" ] || NOVNC_WEB=/usr/share/webapps/novnc
    if [ -f "$NOVNC_WEB/vnc.html" ]; then
        log "starting noVNC on :$NOVNC_PORT (http://<host>:$NOVNC_PORT/vnc.html)"
        websockify --web="$NOVNC_WEB" "$NOVNC_PORT" "localhost:$VNC_PORT" >/dev/null 2>&1 &
        WS_PID=$!
    else
        log "WARN: noVNC web root not found; connect a VNC client to :$VNC_PORT instead"
    fi
else
    log "VNC disabled; use the console's Register tab to view/interact with the browser"
fi

# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------
log "starting QoderGateway on ${QODER_HOST:-0.0.0.0}:${QODER_PORT:-5050}"
python -m qoder2api.app &
APP_PID=$!

wait "$APP_PID"
cleanup
