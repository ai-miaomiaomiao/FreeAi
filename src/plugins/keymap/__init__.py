"""keymap 插件：可自定义的快捷键。

**为什么是插件而不是写在内核里**
    "Ctrl+S 存档""Ctrl+D 删行"这类东西，用的人各有一套习惯。写死在
    `80-editor.js` 里的问题是：想改一个键就得改内核代码、还得重启。
    做成插件之后，键位是一份 JSON，界面上点开就能改。

**分工**
    内核（`50-views.js`）只提供 `keymapRegister(plugin, {id,label,when,run})`
    这张**空表**和一个按键分发器 —— 它不认识任何具体动作。
    动作由插件自己登记，所以别的插件也能往这张表里加键位，
    这个插件只负责"默认的那几个动作 + 配置界面 + 存盘"。

**键位怎么存**
    `<工作区>/.ai/keymap.json`，形如 `{"editor.save": "Ctrl+S", ...}`。
    按工作区存（不是全局）—— 理由和 save.json 一样：不同项目可以有不同
    习惯，而且跟着工作区走，换电脑拷目录就带走了。
    没有这个文件 = 用默认键位（DEFAULTS），所以"恢复默认"就是删掉文件。

**动作 id 不能随便改**
    配置是按 id 存的。改了 id 等于老配置失配，用户自定义过的键会静默变回
    默认值。要改就得在 `_migrate()` 里补一条映射。
"""

from __future__ import annotations

import json
import os

NAME = "keymap"
DESCRIPTION = "可自定义快捷键：Ctrl+A 全选 / Ctrl+D 删行 / Ctrl+S 保存"

#: 左侧活动栏的图标。`after="files"` 让它紧跟在文件树后面。
# hidden=True：不占活动栏，从 ⚙「设置」菜单进入（"⌨ 快捷键"）。
# 快捷键是"配一次就不管"的东西，常驻图标没必要。
VIEWS = [
    {"view": "keymap", "icon": "⌨", "title": "快捷键", "after": "files",
     "hidden": True},
]

#: 前端资源。有 VIEWS 本来就会加载，但配置界面也要在活动栏之外能用
#: （比如以后从设置里点进来），显式声明 `load: "always"` 更稳。
WEB = {"js": ["keymap.js"], "css": ["keymap.css"], "load": "always"}

#: 默认键位。**前端也有一份同名默认值**（web/keymap.js 的 DEFAULTS），
#: 两处不一致时以这里为准（前端拿到 /get 的结果会整份覆盖）。
DEFAULTS = {
    "editor.selectAll": "Ctrl+A",
    "editor.deleteLine": "Ctrl+D",
    "editor.save": "Ctrl+S",
}

#: 给界面用的动作说明。key 是动作 id，值给人看。
#: 顺序有意义 —— 配置界面按这个顺序列，不按字典序（字典序会把 save 排前面）。
CATALOG = [
    {"id": "editor.selectAll", "label": "全选", "scope": "编辑器 / 终端 / 会话"},
    {"id": "editor.deleteLine", "label": "删除当前行", "scope": "编辑器"},
    {"id": "editor.save", "label": "保存当前文件", "scope": "编辑器"},
]

CONFIG_NAME = "keymap.json"


def _cfg_path(payload: dict) -> str:
    """键位配置的路径：`<工作区>/.ai/keymap.json`。

    `ai_dir` 由 web_agent 的插件调用通道塞进来（见 `_handle_plugin_call`），
    是当前**请求所属工作区**的 .ai 目录 —— 不是页面上高亮的那个。
    两个工作区并行时，从 B 的界面改键位必须落到 B。
    """
    ai_dir = str(payload.get("ai_dir") or "").strip()
    if not ai_dir:
        # 没有 ai_dir 说明是工作区还没准备好（或老版本内核），
        # 这时候读写全局默认，别抛异常 —— 前端只要拿到一份能用的配置就行。
        return ""
    return os.path.join(ai_dir, CONFIG_NAME)


def _load_raw(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # 配置坏了不该让整个快捷键系统瘫掉 —— 静默回默认值。
        # 这里刻意不写日志：本模块会被高频调用（每次打开视图都读一次）。
        return {}
    return data if isinstance(data, dict) else {}


def _clean_bindings(raw: dict) -> dict:
    """把磁盘上的配置收敛成"只含已知 id、值是字符串"的样子。

    多出来的键（插件被删了、手改错了）直接丢弃 —— 留着会让配置界面
    显示一堆改不了也删不掉的幽灵行。
    """
    out = {}
    known = {c["id"] for c in CATALOG}
    for k, v in (raw or {}).items():
        if k not in known:
            continue
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def _merge(payload, kw: dict) -> dict:
    """把内核塞进来的 payload 和显式关键字参数合成一份。

    内核一律 `fn(payload)` 位置传参，所以 `payload` 是常态来源；
    `**kw` 只在有人直接 `import` 本模块调用时才有值。
    两者都没有时返回空 dict，上层照 "没工作区" 处理。
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    for k, v in (kw or {}).items():
        out.setdefault(k, v)
    return out


def bindings(payload: dict = None, **kw) -> dict:
    """返回 `{动作id: 键位}`，缺的用默认值补齐。

    前端启动时调这个（`api("/api/plugin/keymap/bindings")`）。返回的是
    **完整表**而不是"只有自定义的几条"，前端不用自己做合并 —— 少一处
    两边逻辑不一致的机会。

    ⚠️ 第一个参数必须是 `payload`（位置参数）。内核 `call_exposed` 是
    `fn(payload or {})` —— **位置传参**，只写 `**kw` 会直接
    `TypeError: takes 0 positional arguments but 1 was given`，
    接口整体 400。第一版就是这么写的，表现是配置界面列表空 + 键位存不住。
    """
    kw = _merge(payload, kw)
    path = _cfg_path(kw)
    seen = _clean_bindings(_load_raw(path))
    out = dict(DEFAULTS)
    out.update(seen)
    return out


def catalog(payload: dict = None, **kw) -> dict:
    """动作清单 + 当前键位 + 默认键位，配置界面一次拿全。

    签名注意事项同 `bindings`：`payload` 必须是第一个位置参数。
    """
    kw = _merge(payload, kw)
    return {
        "actions": CATALOG,
        "bindings": bindings(kw),
        "defaults": dict(DEFAULTS),
    }


def save(payload: dict = None, **kw) -> dict:
    """保存键位。传进来的按 id 收，只存**和默认不同**的那几条。

    为什么只存差异：以后加了新动作（默认键位表变长），老用户配置文件里
    没有那条，自动拿到新默认值；要是把整份表都存下去，新动作会被"存盘时
    还不存在"这件事漏掉，用户看到的就是"新功能默认没键位"。

    ⚠️ 前端提交的键位在 `payload["bindings"]` 里 —— 内核把整个请求体
    当成 payload 原样丢进来，所以第二参数 `new_bindings` 只服务于
    `import` 直调的场景。
    """
    kw = _merge(payload, kw)
    new_bindings = kw.get("bindings")
    path = _cfg_path(kw)
    if not path:
        raise RuntimeError("当前没有工作区，键位没地方存")
    src = new_bindings if isinstance(new_bindings, dict) else {}
    keep = {}
    for k, v in _clean_bindings(src).items():
        if v != DEFAULTS.get(k):
            keep[k] = v
    # 全默认时删文件而不是写空对象：保持 .ai 目录干净，
    # 也让"恢复默认"在磁盘上看得见效果。
    if not keep:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"ok": True, "bindings": dict(DEFAULTS), "saved": 0}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 原子替换：写一半断电不会留半个 JSON
    return {"ok": True, "bindings": bindings(kw), "saved": len(keep)}


def reset(payload: dict = None, **kw) -> dict:
    """恢复默认：删掉配置文件。签名注意事项同 `bindings`。"""
    kw = _merge(payload, kw)
    path = _cfg_path(kw)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"ok": True, "bindings": dict(DEFAULTS)}


EXPOSE = {
    "bindings": "读当前键位（缺的用默认值补齐）",
    "catalog": "动作清单 + 当前键位 + 默认键位",
    "save": "保存键位",
    "reset": "恢复默认键位",
}

TOOLS = {}
SCHEMA = {}
