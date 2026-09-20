#!/bin/sh
# ---------------------------------------------------------------------------
# 容器入口：启动虚拟显示与网关进程
#
# 为什么需要 Xvfb：
#   Qoder 注册的人机验证是阿里云滑块，必须在有头浏览器中由人拖动完成，
#   而有头 Chromium 需要 X display，因此 Xvfb 始终启动。
#
# 人机验证如何完成：
#   在 Web 控制台 Register 页签的「远程浏览器」面板里直接看画面并拖动。
#   该面板复用 DrissionPage 的 CDP 通道推送画面并回放输入事件，已验证可
#   穿透滑块所在的跨源 iframe，因此容器不再暴露 VNC 端口。
#
# 环境变量：
#   QODER_SCREEN    虚拟屏分辨率，默认 1440x900x24
#   QODER_DISPLAY   显示号，默认 :99
# ---------------------------------------------------------------------------
set -e

DATA_DIR="${QODER_DATA_DIR:-/data}"
DISPLAY_NUM="${QODER_DISPLAY:-:99}"
SCREEN="${QODER_SCREEN:-1440x900x24}"

XVFB_PID=""
WM_PID=""
APP_PID=""

log() { echo "[entrypoint] $*"; }

cleanup() {
    log "shutting down..."
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
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
# 网关
# ---------------------------------------------------------------------------
log "starting QoderGateway on ${QODER_HOST:-0.0.0.0}:${QODER_PORT:-5050}"
python -m qoder2api.app &
APP_PID=$!

wait "$APP_PID"
cleanup
