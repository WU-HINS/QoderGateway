#!/bin/sh
# ---------------------------------------------------------------------------
# 容器入口
#
# 为什么以 root 启动：
#   镜像声明了 VOLUME /data。Docker 挂载新卷时属主是 root，会覆盖构建期
#   chown 的结果，导致非 root 用户无法写数据库
#   （sqlite3.OperationalError: unable to open database file）。
#   因此这里先以 root 修正属主，再降权重新执行本脚本；
#   之后 Xvfb、窗口管理器、网关全部以非 root 运行。
#
# 为什么需要 Xvfb：
#   Qoder 注册的人机验证是阿里云滑块，必须在有头浏览器中由人拖动完成，
#   而有头 Chromium 需要 X display。
#
# 人机验证如何完成：
#   在 Web 控制台 Register 页签的「远程浏览器」面板里直接看画面并拖动。
#   该面板复用 DrissionPage 的 CDP 通道，已验证可穿透滑块所在的跨源
#   iframe，因此容器不安装也不暴露 VNC。
#
# 环境变量：
#   QODER_UID / QODER_GID  运行身份，默认 10001
#   QODER_SCREEN           虚拟屏分辨率，默认 1440x900x24
#   QODER_DISPLAY          显示号，默认 :99
# ---------------------------------------------------------------------------
set -e

DATA_DIR="${QODER_DATA_DIR:-/data}"
DISPLAY_NUM="${QODER_DISPLAY:-:99}"
SCREEN="${QODER_SCREEN:-1440x900x24}"
RUN_UID="${QODER_UID:-10001}"
RUN_GID="${QODER_GID:-10001}"

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------------------
# root 阶段：修正数据卷属主，然后降权重新执行本脚本
# ---------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    chown -R "$RUN_UID:$RUN_GID" "$DATA_DIR" 2>/dev/null || true
    # /tmp/.X11-unix 保持 root 属主（Xvfb 会检查），只放开写权限
    mkdir -p /tmp/.X11-unix
    chmod 1777 /tmp/.X11-unix 2>/dev/null || true

    # 确定运行用户的 HOME；用户不存在时（如本地开发环境）退化为临时目录
    RUN_HOME=$(getent passwd "$RUN_UID" 2>/dev/null | cut -d: -f6)
    if [ -z "$RUN_HOME" ]; then
        RUN_HOME=/tmp/qoder-home
        mkdir -p "$RUN_HOME" && chown -R "$RUN_UID:$RUN_GID" "$RUN_HOME" 2>/dev/null || true
    fi
    export HOME="$RUN_HOME"

    log "prepared data dir $DATA_DIR, dropping to uid=$RUN_UID gid=$RUN_GID"
    # --init-groups 要求系统内存在该 uid 的用户；否则清空附加组即可
    if getent passwd "$RUN_UID" >/dev/null 2>&1; then
        exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --init-groups "$0" "$@"
    fi
    exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups "$0" "$@"
fi

# ---------------------------------------------------------------------------
# 以下均以非 root 运行
# ---------------------------------------------------------------------------
if [ ! -w "$DATA_DIR" ]; then
    log "ERROR: 数据目录不可写: $DATA_DIR"
    log "       绑定挂载时请先设置属主：sudo chown -R $RUN_UID:$RUN_GID <宿主目录>"
    exit 1
fi

XVFB_PID=""
WM_PID=""
APP_PID=""

cleanup() {
    log "shutting down..."
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
    [ -n "$WM_PID" ] && kill "$WM_PID" 2>/dev/null || true
    [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

# 容器重启时 /tmp 会保留，可能残留上次的 X lock，导致 Xvfb 启动失败
LOCK_FILE="/tmp/.X${DISPLAY_NUM#:}-lock"
if [ -e "$LOCK_FILE" ] && ! pgrep -f "Xvfb ${DISPLAY_NUM}" >/dev/null 2>&1; then
    log "removing stale X lock: $LOCK_FILE"
    rm -f "$LOCK_FILE"
fi

# ---------------------------------------------------------------------------
# 虚拟显示（有头 Chromium 必需）
# ---------------------------------------------------------------------------
export DISPLAY="$DISPLAY_NUM"

log "starting Xvfb $DISPLAY_NUM ($SCREEN)"
Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp &
XVFB_PID=$!

i=0
while [ $i -lt 50 ]; do
    # 先确认进程存活：若 Xvfb 已退出（如 lock 冲突），立即失败而不是误报 ready
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        log "ERROR: Xvfb 启动失败（见上方错误输出）"
        exit 1
    fi
    if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ]; then
        break
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
