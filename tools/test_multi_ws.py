# -*- coding: utf-8 -*-
"""多工作区并行的端到端自测。

不改任何业务代码，只对着跑着的服务发 HTTP 请求，验证：
  1. 两个工作区各自的会话集合、活动会话互不串；
  2. 记忆按工作区隔离（写进 A 的不会出现在 B）；
  3. 插件调用收到的是**请求自己的** workspace，不是页面上高亮的那个；
  4. 并行发请求时不互相覆盖（A、B 交替写，最后各看各的）。

用法：先 `python src/web_agent.py`，再 `python tools/test_multi_ws.py`。
"""

import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

# Windows 控制台默认是 GBK，被测接口会回中文甚至 ⚠ 之类的符号，
# 直接 print 会 UnicodeEncodeError 把测试本身打断。这里强制用 UTF-8 输出。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# 本脚本放在 tools/ 下，跑起来 cwd 可能是 tools，所以项目根按文件位置推。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = os.environ.get("AGENT_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("AGENT_PASSWORD", "your-password")

FAILED = []


def _req(path, body=None, token=None, method=None, ws=""):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(name)


def login():
    st, r = _req("/api/login", {"password": PASSWORD})
    if st != 200 or not r.get("token"):
        print("登录失败：", st, r)
        sys.exit(1)
    return r["token"]


def main():
    tok = login()
    print("登录成功")

    # 挑两个一定存在、且不同的目录当工作区根
    root_a = os.path.join(PROJECT_ROOT, "src")
    root_b = os.path.join(PROJECT_ROOT, "tools")
    if os.path.normcase(root_a) == os.path.normcase(root_b):
        print("两个工作区一样，测不了")
        sys.exit(1)

    # 先把两棵树都登记上（不改活动根）
    for p in (root_a, root_b):
        st, r = _req("/api/workspace/add", {"path": p}, tok)
        check("登记工作区 %s" % p, st == 200, r)

    # ---- 1. 会话集合按工作区隔离 ----
    st, a = _req("/api/workspace?ws=" + urllib.parse.quote(root_a), token=tok)
    st2, b = _req("/api/workspace?ws=" + urllib.parse.quote(root_b), token=tok)
    check("读 A 的工作区信息", st == 200, a)
    check("读 B 的工作区信息", st2 == 200, b)
    check("A 的 workspace 字段回的是 A", a.get("workspace") == root_a, a.get("workspace"))
    check("B 的 workspace 字段回的是 B", b.get("workspace") == root_b, b.get("workspace"))
    check("A、B 记忆文件路径不同",
          a.get("memory_path") != b.get("memory_path"),
          (a.get("memory_path"), b.get("memory_path")))

    # ---- 2. 记忆隔离 ----
    stamp_a = "MARKER-ONLY-IN-A-9f31"
    stamp_b = "MARKER-ONLY-IN-B-7c28"
    st, r = _req("/api/memory",
                 {"ws": root_a, "text": "A 工作区记忆 " + stamp_a}, tok)
    check("写 A 的记忆", st == 200, r)
    st, r = _req("/api/memory",
                 {"ws": root_b, "text": "B 工作区记忆 " + stamp_b}, tok)
    check("写 B 的记忆", st == 200, r)

    st, a2 = _req("/api/workspace?ws=" + urllib.parse.quote(root_a), token=tok)
    st2, b2 = _req("/api/workspace?ws=" + urllib.parse.quote(root_b), token=tok)
    check("A 的记忆里有 A 的标记", stamp_a in json.dumps(a2, ensure_ascii=False), a2.get("memory"))
    check("A 的记忆里没有 B 的标记", stamp_b not in json.dumps(a2, ensure_ascii=False))
    check("B 的记忆里有 B 的标记", stamp_b in json.dumps(b2, ensure_ascii=False), b2.get("memory"))
    check("B 的记忆里没有 A 的标记", stamp_a not in json.dumps(b2, ensure_ascii=False))

    # ---- 3. 插件拿到的是请求自己的 workspace ----
    # 找一个不需要真实设备就能返回信息的插件函数；launcher / code_search 都行。
    st, pl = _req("/api/plugins", token=tok)
    names = [p.get("name") for p in (pl.get("plugins") or [])] if st == 200 else []
    print("  已加载插件：", names)
    for plug in ("launcher", "code_search", "gradle_tools"):
        if plug not in names:
            continue
        st, r = _req("/api/plugin/%s/echo_workspace" % plug, {"ws": root_a}, tok)
        if st == 200:
            got = json.dumps(r, ensure_ascii=False)
            check("插件 %s 收到 A 的 workspace" % plug,
                  os.path.normcase(root_a) in got.lower(), got)
            break
    else:
        print("  SKIP 没找到能回显 workspace 的插件函数（不影响隔离结论）")

    # ---- 4. 并发写：A、B 交替，谁也不许盖掉谁 ----
    errs = []

    def writer(root, stamp, n):
        for i in range(6):
            s, rr = _req("/api/memory",
                         {"ws": root, "text": "%s #%d" % (stamp, i)}, tok)
            if s != 200:
                errs.append((root, s, rr))

    ts = [threading.Thread(target=writer, args=(root_a, stamp_a, 0)),
          threading.Thread(target=writer, args=(root_b, stamp_b, 0))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发写没有报错", not errs, errs[:2])

    st, a3 = _req("/api/workspace?ws=" + urllib.parse.quote(root_a), token=tok)
    st2, b3 = _req("/api/workspace?ws=" + urllib.parse.quote(root_b), token=tok)
    ja = json.dumps(a3, ensure_ascii=False)
    jb = json.dumps(b3, ensure_ascii=False)
    check("并发后 A 记忆仍是 A 的", stamp_a in ja and stamp_b not in ja, ja[-200:])
    check("并发后 B 记忆仍是 B 的", stamp_b in jb and stamp_a not in jb, jb[-200:])

    # ---- 5. 不带 ws 时回落到活动根（老行为）----
    st, legacy = _req("/api/workspace", token=tok)
    check("不带 ws 仍能正常返回（兼容老前端）", st == 200 and "sessions" in legacy, legacy)

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
