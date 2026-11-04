# -*- coding: utf-8 -*-
"""直接调后端函数，排查 /api/workspace?ws= 为什么回活动根。"""
import io
import os
import sys
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)

import agent_tools            # noqa: E402
import web_agent              # noqa: E402

print("ROOT =", ROOT)
ws0 = web_agent.current_workspace()
print("活动根 =", ws0.get("path"))

Handler = web_agent.Handler
Handler.roots = agent_tools.normalize_roots(["*"])
print("roots =", Handler.roots)

q = urllib.parse.parse_qs("ws=" + urllib.parse.quote(os.path.join(ROOT, "src")))
print("query =", q)

ws, info, store = Handler._resolver("", q)
print("_resolver -> ws =", ws)
print("             info =", info)
print("             store.path =", getattr(store, "path", None))
