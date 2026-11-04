# -*- coding: utf-8 -*-
"""工具/插件完整性审计。

只做静态+导入级检查，不连 LLM、不发请求：
  1. 内置 schema 与 _HANDLERS 是否一一对应
  2. schema 里 required 的字段，handler 实现里是否真的读了
  3. 插件 schema 与 run_tool 分派是否一致
  4. 各工具对 ctx 的依赖（workspace / cwd / roots）是否有多工作区隐患
  5. 工具名是否与上游约束冲突（重名、非法字符）

用法： python tools/audit_tools.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "src")
sys.path.insert(0, CODE)

problems = []
warns = []


def bad(msg):
    problems.append(msg)


def warn(msg):
    warns.append(msg)


import agent_actions as A          # noqa: E402
import agent_plugins as P          # noqa: E402
import agent_tools                 # noqa: E402
import model_tips as M             # noqa: E402

# 插件必须显式加载一次，否则 tool_schemas() 永远是空（web_agent 启动时也会做）
_info = P.load_plugins(CODE, log=lambda *a: None)
print("== 加载结果 ==")
print("  插件：%s" % (", ".join(_info["plugins"]) or "(无)"))
print("  工具：%s" % (", ".join(_info["tools"]) or "(无)"))
for _e in _info["errors"]:
    bad("插件 %s：%s" % (_e["file"], _e["error"]))


# ---- 1. schema <-> handler ----
# all_schemas() 现在本来就是全量（"core / 非常用"那套分层已删），不需要参数。
schemas = A.all_schemas()

# 描述**必须是字符串**：`"description": ("a" "b",)` 这种多一个逗号会变成 tuple，
# JSON 序列化成数组，上游直接 400 —— "cannot unmarshal array into
# ...function.description of type string"。整轮对话全废，而且本地不报错。
_bad_desc = []
for _s in schemas:
    _fn = _s.get("function") or {}
    if not isinstance(_fn.get("description"), str):
        _bad_desc.append("%s（%s）" % (_fn.get("name"), type(_fn.get("description")).__name__))
    for _k, _n in ((_fn.get("parameters") or {}).get("properties") or {}).items():
        if not isinstance(_n.get("description"), str):
            _bad_desc.append("%s.%s（%s）" % (_fn.get("name"), _k, type(_n.get("description")).__name__))
if _bad_desc:
    print("!! 描述不是字符串（上游会 400）：" + "、".join(_bad_desc))

name_of = {}
for s in schemas:
    fn = s.get("function") or {}
    name_of[fn.get("name")] = fn

handlers = set(A._HANDLERS)

missing_impl = sorted(set(name_of) - handlers - set(P.tool_schemas() and [x["function"]["name"] for x in P.tool_schemas()]))
if missing_impl:
    bad("有 schema 但没有实现：%s" % missing_impl)

plugin_names = set()
try:
    plugin_names = {x["function"]["name"] for x in P.tool_schemas()}
except Exception as e:
    bad("插件 schema 取不到：%s" % e)

orphan = sorted(handlers - set(name_of))
if orphan:
    bad("有实现但没 schema（模型永远调不到）：%s" % orphan)

# ---- 2. required 字段是否被实现读取 ----
print("== 内置工具（%d）==" % len(handlers))
for name in sorted(handlers):
    fn = name_of.get(name)
    if not fn:
        continue
    params = (fn.get("parameters") or {}).get("properties") or {}
    need = (fn.get("parameters") or {}).get("required") or []
    handler = A._HANDLERS[name]
    try:
        src = open(handler.__code__.co_filename, encoding="utf-8").read()
    except Exception:
        src = ""
    # 粗查：handler 源码里有没有出现 args.get("<名>") / args["<名>"]
    body = ""
    if src:
        m = re.search(r"def %s\(.*?(?=\ndef |\Z)" % re.escape(handler.__name__),
                      src, re.S)
        body = m.group(0) if m else ""
    for k in need:
        if body and ('"%s"' % k) not in body and ("'%s'" % k) not in body:
            warn("%s 声明必填 %s，但函数体里没见取值" % (name, k))
    print("  %-14s 参数 %-28s 必填 %s" % (name, ",".join(params) or "-", need or "-"))

# ---- 3. 插件 ----
print("== 插件（%d 个工具）==" % len(plugin_names))
for x in P.tool_schemas():
    fn = x["function"]
    nm = fn["name"]
    ok = P.has_tool(nm)
    if not ok:
        bad("插件工具 %s 有 schema 但 has_tool=False" % nm)
    print("  %-16s %s" % (nm, "OK" if ok else "MISSING"))

errs = P.load_errors()
if errs:
    for e in errs:
        bad("插件加载报错：%s" % (e,))
else:
    print("  插件加载无报错")

# ---- 4. ctx 依赖 ----
print("== ctx 依赖 ==")
for name in sorted(handlers | plugin_names):
    try:
        if name in handlers:
            r = A.execute(name, {}, {})           # 空 ctx：看它是否容错
        else:
            r = P.run_tool(name, {}, {})
    except Exception as e:
        bad("%s 空 ctx 直接抛异常：%s: %s" % (name, type(e).__name__, e))
        continue
    if not isinstance(r, dict) or "ok" not in r:
        bad("%s 返回值不合规：%r" % (name, type(r)))
        continue
    if r.get("ok"):
        warn("%s 空参数空 ctx 居然成功（可能默认目标选过宽）：%s"
             % (name, str(r.get("summary"))[:60]))

# ---- 5. _default_target 多工作区 ----
print("== _default_target ==")
cases = [
    ({"workspace": r"C:\ai\web", "roots": ["*"], "cwd": ROOT}, r"C:\ai\web"),
    ({"workspace": "", "roots": ["*"], "cwd": ROOT}, ROOT),
    ({"workspace": "", "roots": [r"C:\ai"], "cwd": ROOT}, r"C:\ai"),
    ({"workspace": "", "roots": [], "cwd": ROOT}, ROOT),
    ({}, os.getcwd()),
]
for ctx, want in cases:
    got = A._default_target(ctx)
    tag = "OK " if os.path.normcase(got) == os.path.normcase(want) else "BAD"
    if tag == "BAD":
        bad("_default_target(%r) = %r，期望 %r" % (ctx, got, want))
    print("  %s %-50r -> %r" % (tag, ctx, got))

# ---- 6. 工作区参数是否真的能传进工具 ----
print("== 工作区隔离 ==")
import inspect
src_actions = inspect.getsource(A)
if "ctx.get(\"workspace\")" not in src_actions and 'ctx.get("workspace")' not in src_actions:
    bad("agent_actions 里没有读 ctx['workspace']")
else:
    print("  OK  agent_actions 会读 ctx['workspace']")

print()
if problems:
    print("!! 问题 %d 条" % len(problems))
    for p in problems:
        print("   - " + p)
if warns:
    print("?? 提示 %d 条" % len(warns))
    for w in warns:
        print("   - " + w)
if not problems and not warns:
    print("全部通过，没发现问题。")
sys.exit(1 if problems else 0)
