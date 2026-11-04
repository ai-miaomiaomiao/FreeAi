#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ai.sh - Web AI Agent 命令行入口（Linux / macOS，转交 src/cli.py）
#
#   ai "你好"               新建会话，问一句
#   ai -s "接着说"          复用当前工作区的活动会话
#   ai -s --sid s3 "..."    复用会话 s3
#   ai -m debug "..."       选智能体模式（默认 standard）
#   ai --help               全部参数
#
# 为什么需要这个文件：ai.bat 是 cmd 批处理，bash 根本不认 .bat，放进 PATH
# 也没用，只会得到 "ai: command not found"。真正干活的是 src/cli.py（纯标准
# 库、跨平台），这里只做「定位目录 + 转交解释器」两件事。
#
# 注意：本文件必须是 LF 换行。被 Windows 编辑器存成 CRLF 后会报
#   bad interpreter: /usr/bin/env bash^M
# 修复：sed -i 's/\r$//' ai.sh
#
# 前提：服务已经跑起来（./start.sh）。想在任何目录直接用 ai：
#   chmod +x ai.sh
#   mkdir -p ~/.local/bin
#   ln -s "$PWD/ai.sh" ~/.local/bin/ai      # 确认 ~/.local/bin 在 PATH 里
# ---------------------------------------------------------------------------
set -u

# 不 cd：要保持调用者的当前目录，这样 -w ./相对路径 才有意义。
# src/cli.py 自己按 __file__ 找项目根，所以从哪个目录调用都能读到
# agent_config.json。
#
# 但软链进来的情况（~/.local/bin/ai -> 项目/ai.sh）$0 是软链路径，
# 直接 dirname 会得到 ~/.local/bin，那儿没有 src/。所以先尽量解析真身。
SELF="$0"
if command -v readlink >/dev/null 2>&1; then
    _rl="$(readlink -f "$SELF" 2>/dev/null || true)"
    [ -n "$_rl" ] && SELF="$_rl"
fi
DIR="$(cd "$(dirname "$SELF")" && pwd)"

# 解释器探测：PYTHON 显式指定优先，其次 python3，最后退回 python。
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    elif command -v python >/dev/null 2>&1; then
        PY=python
    else
        echo "[错误] 找不到 Python 3。装一个，或用 PYTHON=/path/to/python3 指定。" >&2
        exit 9009
    fi
fi

if [ ! -f "$DIR/src/cli.py" ]; then
    echo "[错误] 没找到 $DIR/src/cli.py。" >&2
    echo "       目录结构变了？请让 ai.sh 与 src/ 保持在项目根同一层。" >&2
    exit 1
fi

# -X utf8：强制 UTF-8 标准输入输出，中文参数和回复不乱码（Linux 下 locale
# 常是 C/POSIX，不加这句中文参数会被当成 latin-1 塞给服务端）。
# exec：让 python 顶替当前进程，Ctrl+C 和退出码原样透传，不留一层 shell。
exec "$PY" -X utf8 "$DIR/src/cli.py" "$@"
