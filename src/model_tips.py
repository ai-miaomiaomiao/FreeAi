"""模型提示语的外置表（`src/modeltips/*.json`）。

为什么要外置：这些文案改得比代码勤 —— 调一句工具描述、换一段系统提示，
不该为此改 .py、更不该为此重启服务。放进 JSON 后，**改完下一次请求就生效**
（按文件 mtime 热加载）。

约定（很重要，照着写才不会把服务改坏）：

  · JSON 里**只放文案**。"有哪些工具、哪些参数、哪个工具挂哪个 handler"
    仍然由代码定义 —— 表是覆盖，不是定义。
  · 文件缺失 / JSON 语法错 / 键写错 → 一律回退到代码里的内置默认值，
    只在控制台打一行 warn。绝不让"手滑改错一个逗号"变成服务起不来。
  · 所以代码里那些默认文本**不要删**：它们就是兜底。

当前的几张表：

    tools.json   每个工具的 description + 各参数 description
    system.json  系统提示里的固定段落（工具使用约定、本机可写目录说明）
    init.json    首次进工作区时"整理记忆"用的 prompt 与记忆模板
    stages.json  多步流程里每一步的提示词，键是 `<模式id>.<阶段id>`；
                 没配的阶段回退到 modes/*.json 里那份。值为数组 = 一行一项
"""

from __future__ import annotations

import json
import os
import threading

#: 表所在的目录（与模块同级的 modeltips/）
TIPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modeltips")

_LOCK = threading.Lock()
#: name -> 上次读到的 mtime；data 同理。按需加载，没读过的表不占内存。
_MTIME: dict = {}
_DATA: dict = {}
_WARNED: set = set()


def _load(name: str) -> dict:
    """读一张表（带 mtime 缓存）。读不到返回空 dict —— 调用方全部走默认值。"""
    path = os.path.join(TIPS_DIR, name + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _LOCK:
        if _MTIME.get(name) == mtime:
            return _DATA.get(name) or {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            # 只警告一次：这个函数每轮都会调，坏文件不该刷屏
            if name not in _WARNED:
                _WARNED.add(name)
                print(f"[warn] {path} 读不了（{e}），先用代码内置文案")
            data = {}
        if not isinstance(data, dict):
            if name not in _WARNED:
                _WARNED.add(name)
                print(f"[warn] {path} 顶层必须是对象，已忽略")
            data = {}
        _MTIME[name] = mtime
        _DATA[name] = data
        return data


def text(section: str, key: str, default: str = "") -> str:
    """取一段文案；没配 / 配成空串就返回 default。

    调用方的写法是 `text("system", "tool_rules", _内置的那段)` —— 默认值永远
    留在代码里，JSON 只是"允许被覆盖"。

    值写成**数组**时按行拼成多行文本：多行提示词在 JSON 字符串里塞满 `\n`
    几乎没法读、也没法改，拆成一行一个元素才是人写的东西。
    """
    val = _load(section).get(key)
    if isinstance(val, list):
        val = "\n".join(str(x) for x in val).strip("\n")
    return val if isinstance(val, str) and val else default


def _copy_schema(schema: dict) -> dict:
    """浅拷贝一层到"可安全改"的深度：schema / function / parameters / 各字段。"""
    fn = dict(schema.get("function") or {})
    params = fn.get("parameters")
    if isinstance(params, dict):
        params = dict(params)
        props = params.get("properties")
        if isinstance(props, dict):
            params["properties"] = {
                k: (dict(v) if isinstance(v, dict) else v)
                for k, v in props.items()}
        fn["parameters"] = params
    return {"type": schema.get("type") or "function", "function": fn}


def apply_tools(schemas: list, required=None) -> list:
    """把 `tools.json` 的描述套到工具表上，返回**新列表**（不动入参）。

    结构：`{"工具名": {"description": "...", "params": {"参数名": "..."}}}`

    **严格模式**：描述只有这一份，代码侧不再保留兜底文案（`TOOL_SCHEMAS` 里的
    description 一律是空串）。所以出问题必须立刻喊出来，不能静默退化：

    · 表读不到 / JSON 写坏 → 抛 RuntimeError；
    · `required` 里的工具没在表里、或它的 description 是空串 → 抛，列出缺哪些；
    · `params` 里写了代码里不存在的参数名 → 抛（笔误静默跳过最难查）；
    · 表里有、但本次工具表里没有的工具名 → 抛（多半是工具改名了没同步）。

    插件工具（`agent_plugins.tool_schemas()`）的描述仍写在插件里，不在校验
    范围内 —— 它们不在这张表里。
    """
    table = _load("tools")
    if not table:
        raise RuntimeError(
            "工具描述表 src/modeltips/tools.json 读不到或解析失败。"
            "代码侧已经不留兜底文案，请修好这个文件"
            "（确认文件存在、顶层是对象、JSON 语法正确）再重试。")

    want = set(required or ())
    seen = set()
    missing, bad_params = [], []

    out = []
    for schema in schemas or []:
        if not isinstance(schema, dict):
            continue
        item = _copy_schema(schema)
        fn = item["function"]
        name = str(fn.get("name") or "")
        seen.add(name)
        tip = table.get(name)
        if not isinstance(tip, dict):
            if name in want:
                missing.append(name)
            out.append(item)
            continue

        desc = tip.get("description")
        if isinstance(desc, str) and desc:
            fn["description"] = desc
        elif name in want:
            missing.append(name + "（description 为空）")

        props = ((fn.get("parameters") or {}).get("properties") or {})
        written = set()
        for key, val in (tip.get("params") or {}).items():
            written.add(key)
            node = props.get(key)
            if not isinstance(node, dict):
                bad_params.append("%s.%s" % (name, key))
                continue
            if isinstance(val, str) and val:
                node["description"] = val
        # 参数描述也必须有：漏一个，模型就只能靠参数名猜语义
        if name in want:
            for key in props:
                if key not in written:
                    missing.append("%s.%s（参数描述缺失）" % (name, key))
        out.append(item)

    problems = []
    if missing:
        problems.append("缺少描述：" + "、".join(missing))
    if bad_params:
        problems.append("参数名对不上：" + "、".join(bad_params))
    unknown = [k for k in table if k not in seen]
    if unknown:
        problems.append("表里多出来的工具（代码里没有）：" + "、".join(sorted(unknown)))
    if problems:
        raise RuntimeError(
            "src/modeltips/tools.json 和工具定义不一致 —— " + "；".join(problems)
            + "。代码侧已不再保留兜底文案，请同步这个 JSON。")
    return out


def reload() -> None:
    """丢掉缓存，下次读取重新解析（改了 JSON 想立刻看到时可以用）。"""
    with _LOCK:
        _MTIME.clear()
        _DATA.clear()
        _WARNED.clear()
