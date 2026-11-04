#!/usr/bin/env bash
# Web AI Agent - Linux 启动脚本（局域网可访问）
#
# 用法：
#   ./start.sh                 # 后台启动，监听 0.0.0.0:<端口>（默认值见下方 PORT）
#   ./start.sh 8000            # 后台启动，换端口（等价于 AGENT_PORT=8000）
#   ./start.sh fg              # 前台启动：日志打在终端，Ctrl+C 停
#   ./start.sh stop            # 停止后台服务
#   ./start.sh status          # 看运行状态（PID / 监听 / 日志尾巴）
#   ./start.sh restart         # 重启（等价于再跑一次 start）
#
# 后台模式的 stdout/stderr 落到 .ai/server.log（可用 AGENT_LOG=... 改）
# 服务自己的业务日志仍在 <工作区>/.ai/agent.log
#
# 访问：http://<本机IP>:<端口>
set -u

# 始终以脚本所在目录为工作目录
cd "$(dirname "$0")" || exit 1

HOST="${AGENT_HOST:-0.0.0.0}"
PORT="${AGENT_PORT:-8000}"
PY="${PYTHON:-python3}"
LOG="${AGENT_LOG:-$PWD/.ai/server.log}"
PATTERN='web_agent\.py'

# ---- 参数 ---------------------------------------------------------------
# 第一个参数可以是子命令，也可以是纯数字端口（只认纯数字，其它照旧透传给
# web_agent.py，比如 --restart 那种由 python 自己处理的参数）。
MODE="bg"
case "${1:-}" in
    start | --start)        MODE="bg";      shift ;;
    fg | foreground | --fg) MODE="fg";      shift ;;
    stop | --stop)          MODE="stop";    shift ;;
    status | --status)      MODE="status";  shift ;;
    restart | --restart)    MODE="restart"; shift ;;
esac
case "${1:-}" in
    "" | *[!0-9]*) ;;
    *) PORT="$1"; shift ;;
esac

# ---- 小工具 -------------------------------------------------------------
# 端口上有人在听吗。没有 ss 命令时返回 1（当作"查不了"，由调用方退化处理）。
port_taken() {
    command -v ss >/dev/null 2>&1 || return 1
    ss -ltn 2>/dev/null | grep -q ":${PORT}[[:space:]]"
}

# 只处理 web_agent.py，避免误杀其它 python（比如编辑器自带的解释器）
find_pids() {
    pgrep -f "$PATTERN" 2>/dev/null || true
}

# 先温柔后强硬：SIGTERM 让会话落盘，5 秒不退就 kill -9
stop_all() {
    local pids pid
    pids="$(find_pids)"
    if [ -z "$pids" ]; then
        echo "      无旧实例"
        return 0
    fi
    for pid in $pids; do
        [ "$pid" = "$$" ] && continue
        kill "$pid" 2>/dev/null || true
    done
    for _ in $(seq 1 50); do
        [ -z "$(find_pids)" ] && break
        sleep 0.1
    done
    pids="$(find_pids)"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            [ "$pid" = "$$" ] && continue
            kill -9 "$pid" 2>/dev/null || true
        done
    fi
    echo "      已清理旧实例"
}

print_url() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "      本机：http://127.0.0.1:${PORT}"
    [ -n "$ip" ] && echo "      局域网：http://${ip}:${PORT}"
}

# ---- 子命令：status / stop ---------------------------------------------
if [ "$MODE" = "status" ]; then
    PIDS="$(find_pids)"
    if [ -n "$PIDS" ]; then
        echo "运行中    : 是（PID $(echo $PIDS | tr '\n' ' ')）"
    else
        echo "运行中    : 否"
    fi
    if command -v ss >/dev/null 2>&1; then
        echo "监听      :"
        ss -ltnp 2>/dev/null | grep ":${PORT}[[:space:]]" || echo "          端口 ${PORT} 上没有服务"
    fi
    echo "日志      : ${LOG}"
    if [ -f "$LOG" ]; then
        echo "最近日志  :"
        tail -n 5 "$LOG" | sed 's/^/          /'
    fi
    exit 0
fi

if [ "$MODE" = "stop" ]; then
    echo "============================================"
    echo "  Web AI Agent  停止服务"
    echo "============================================"
    stop_all
    if port_taken; then
        echo "[warn] 端口 ${PORT} 上还有别的进程在听（不是 web_agent.py）"
    else
        echo "      已停止"
    fi
    exit 0
fi

# ---- 以下都是"启动/重启" ------------------------------------------------
echo "============================================"
echo "  Web AI Agent  启动脚本  (端口 ${PORT}，$([ "$MODE" = "fg" ] && echo 前台 || echo 后台))"
echo "============================================"

# 1) 找解释器
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[error] 找不到 $PY，请先安装 Python 3（或设置 PYTHON=/path/to/python3）"
    exit 1
fi

# 2) 停掉旧实例（单实例锁：不先停的话新进程会直接退出，看起来像"启动失败"）
echo "[1/3] 停止旧实例..."
stop_all

# 3) 端口检查（被别的程序占用就提示，别让服务静默起不来）
echo "[2/3] 检查端口 ${PORT}..."
if port_taken; then
    echo "[error] 端口 ${PORT} 被占用："
    ss -ltnp 2>/dev/null | grep ":${PORT}[[:space:]]" || true
    echo "        请先释放该端口，或用 ./start.sh 8000 换端口。"
    exit 1
fi

echo "[3/3] 启动服务..."
echo "      监听 ${HOST}:${PORT}"

if [ "$MODE" = "fg" ]; then
    # 前台：日志留在终端，Ctrl+C 停止
    print_url
    echo "      Ctrl+C 停止"
    echo "---------------------------------------------"
    echo
    AGENT_HOST="$HOST" AGENT_PORT="$PORT" exec "$PY" src/web_agent.py "$@"
fi

# 后台：nohup + 重定向，终端退出/SSH 断开都不影响
mkdir -p "$(dirname "$LOG")"
echo "      日志：${LOG}"
AGENT_HOST="$HOST" AGENT_PORT="$PORT" nohup "$PY" src/web_agent.py "$@" >>"$LOG" 2>&1 &
PID=$!

# 等它把端口监听起来（最多 15 秒）。进程提前退出 = 启动失败，
# 直接把日志尾巴打出来 —— 让用户在这里就看到 traceback，而不是去翻文件。
READY=0
for _ in $(seq 1 75); do
    if port_taken; then
        READY=1
        break
    fi
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
done
# 没有 ss 的机器上 port_taken 永远返回 1：退化成"等 2 秒看进程还在不在"
if [ "$READY" != "1" ] && ! command -v ss >/dev/null 2>&1; then
    sleep 2
    kill -0 "$PID" 2>/dev/null && READY=1
fi

if [ "$READY" = "1" ]; then
    echo "      已启动：PID ${PID}"
    print_url
    echo "      看状态：./start.sh status   停止：./start.sh stop"
else
    echo "[error] 没能起来（端口 ${PORT} 未监听）"
    echo "        最近的日志："
    tail -n 20 "$LOG" 2>/dev/null | sed 's/^/          /'
    exit 1
fi
