#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改动记录（edit 落盘）。

为什么要有它
------------
模型老把「我打算改」说成「我已经改了」，或者编一个不存在的文件名。
根子不在记性，在于**没有任何东西能证明它到底改了没有**。

这里把「编辑工具成功执行」这个事实就地记下来：工具调用的同一时刻写盘，
不存在记忆偏差的空间。之后拼 system prompt 时只读**一小段摘要**塞回去，
模型就能看到自己真正动过哪些文件。

为什么公开接口只有声明式 API
------------------------------
写盘逻辑必须跟着工具走，不能靠模型自觉。所以对外只暴露 `record()`，
由 agent_actions 里的各个 _t_* 直接调用；另外给 agent_llm 一个
`recent_summary()` 拿摘要。

正文为什么要单独存文件
----------------------
old_str / new_str 可能有几十 KB。全塞进一个 jsonl，索引很快就读不动了，
而模型每轮只需要看索引——所以索引和正文分开：

    <工作区>/.ai/edits/<会话id>/
    ├── index.jsonl     每行一条元信息，带正文文件名
    ├── 0001.json       {old_str, new_str}
    └── 0002.json

会话目录跟着**工作区**走：切到别的工程，看不到这里的记录。
"""

import json
import os
import threading
import time

#: 索引文件名。一行一条，纯文本追加，坏了也不影响其它行。
INDEX_FILE = "index.jsonl"

#: 单条 old_str / new_str 的上限。超了就只留前 8KB + 截断标记，
#: 免得有人一次贴进去一整个文件把目录撑爆。
MAX_BODY_CHARS = 8 * 1024

#: 摘要里最多列多少个文件。再多就只是噪音，模型看不过来。
SUMMARY_MAX_ITEMS = 8

_LOCK = threading.Lock()


def edits_dir(ai_dir: str, session_id: str) -> str:
    """`<工作区>/.ai/edits/<会话id>/`。session_id 会做文件名清洗。"""
    sid = _safe_sid(session_id)
    return os.path.join(str(ai_dir or ""), "edits", sid)


def _safe_sid(sid: str) -> str:
    """
    会话 id 来自前端，不能直接当目录名 —— 里面可能有 / \\ : .. 之类。
    只留字母数字和 _-，其余一律换成 _，空就用 default。
    """
    raw = str(sid or "").strip()
    if not raw:
        return "default"
    out = []
    for ch in raw:
        out.append(ch if (ch.isalnum() or ch in "_-") else "_")
    return ("".join(out) or "default")[:64]


def _clip(s: str) -> str:
    t = str(s if s is not None else "")
    if len(t) <= MAX_BODY_CHARS:
        return t
    return t[:MAX_BODY_CHARS] + f"\n...（原长 {len(t)} 字符，已截断）"


def record(ai_dir: str, session_id: str, op: str, path: str = "",
           ok: bool = True, note: str = "", cmd: str = "",
           old_str=None, new_str=None, **extra) -> dict:
    """
    追加一条改动记录。

    op: write_file / edit_file / self_edit / run_command
    失败也记（ok=False）——「改失败了」和「没改」是两件不同的事，
    前者模型必须知道，否则会以为自己改成功了。

    任何异常都吞掉：记日志失败绝不能影响正事。
    """
    try:
        d = edits_dir(ai_dir, session_id)
        os.makedirs(d, exist_ok=True)

        row = {
            "t": time.strftime("%H:%M:%S"),
            "op": op,
            "path": str(path or ""),
            "ok": bool(ok),
        }
        if cmd:
            row["cmd"] = str(cmd)[:500]
        if note:
            row["note"] = str(note)[:300]
        for k, v in extra.items():
            if v not in (None, "", [], {}):
                row[k] = v

        # 正文单独存：只有带 old/new 的编辑才需要
        if old_str is not None or new_str is not None:
            seq = _next_seq(d)
            name = f"{seq:04d}.json"
            body = {"path": str(path or ""),
                    "old_str": _clip(old_str),
                    "new_str": _clip(new_str)}
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            row["body"] = name

        line = json.dumps(row, ensure_ascii=False)
        with _LOCK:
            with open(os.path.join(d, INDEX_FILE), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return row
    except Exception:
        return {}


def _next_seq(d: str) -> int:
    """下一个正文序号。按目录现有 0001.json 往后数。"""
    n = 0
    try:
        for name in os.listdir(d):
            if name.endswith(".json") and name[:4].isdigit():
                n = max(n, int(name[:4]))
    except OSError:
        pass
    return n + 1


def read_index(ai_dir: str, session_id: str, limit: int = 200) -> list:
    """读索引（最近的在后）。给 recent_summary 和以后可能的查询用。"""
    p = os.path.join(edits_dir(ai_dir, session_id), INDEX_FILE)
    rows = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


#: 算「改过文件」的 op。run_command 不在此列 —— 跑命令不等于改文件。
WRITE_OPS = ("write_file", "edit_file")


def written_paths(ai_dir: str, session_id: str) -> list:
    """
    本轮真正成功写过的文件路径（去重，保持首次出现顺序）。

    为什么要有它
    ------------
    交付时的改动清单（changed_files）原本只认 agent_actions 回填的那份，
    但那条链一旦断掉（比如某次编辑没回写 stats），write_docs 就会在
    「not changed」处直接 return，设计文档永远不生成 —— 而 edits 目录里
    明明躺着 89 条记录。索引是磁盘事实，这里把它当成第二来源，
    只管「读」，不回写任何状态，所以不会污染工具层对内核的认知。

    只认 ok 为真且 op 属于 WRITE_OPS 的行：改失败了（ok=False）不算改过，
    run_command 也不算 —— 否则「跑了 svn diff」就会被当成「改了文件」。
    """
    out, seen = [], set()
    for r in read_index(ai_dir, session_id, limit=0):
        if r.get("op") not in WRITE_OPS:
            continue
        if r.get("ok") is False:
            continue
        p = str(r.get("path") or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def batch_summary(ai_dir: str, session_id: str, since_ts: str = "") -> tuple:
    """
    取「已经读过的地方」之后新产生的记录，聚合成一行摘要。

    为什么是「一批」而不是「最后一条」：一轮对话里常连改好几个文件，
    只报最后一条会让模型以为前面的没改成，回头又改一遍。
    但整批只占**一行**，所以历史消息的 token 不会越滚越大。

    第二个返回值是**已读到的行数**（不是时间戳）：
    同一秒内可能连写好几条，时间戳相同就没法切先后了（实测踩过）。
    因此这里必须读全量，不能用 read_index 的 limit 截尾。

    返回 (摘要文本, 新的行数标记)。没新记录就返回 ("", since_ts)。
    """
    rows = read_index(ai_dir, session_id, limit=0)
    if not rows:
        return ("", since_ts)

    start = 0
    if since_ts:
        try:
            start = max(0, int(since_ts))
        except ValueError:
            start = 0
    fresh = rows[start:]
    if not fresh:
        return ("", since_ts)

    edits, cmds, fails = [], [], []
    for r in fresh:
        op = r.get("op", "")
        if op == "run_command":
            cmds.append(r)
        elif r.get("ok") is False:
            fails.append(r)
        else:
            edits.append(r)

    parts = []
    if edits:
        items = []
        for r in edits[:SUMMARY_MAX_ITEMS]:
            name = _short(r.get("path", ""))
            delta = ""
            if r.get("removed") or r.get("added"):
                delta = f"(-{r.get('removed', 0)}+{r.get('added', 0)})"
            items.append(f"{name}{delta}")
        more = f" 等 {len(edits)} 个" if len(edits) > SUMMARY_MAX_ITEMS else ""
        parts.append(f"改了 {len(edits)} 个文件：" + "、".join(items) + more)
    if cmds:
        items = [_short(r.get("cmd", "")) for r in cmds[:4]]
        more = f" 等 {len(cmds)} 条" if len(cmds) > 4 else ""
        parts.append(f"跑了 {len(cmds)} 条命令：" + "、".join(items) + more)
    if fails:
        items = [_short(r.get("path") or r.get("cmd") or "") for r in fails[:3]]
        parts.append(f"**失败** {len(fails)} 次：" + "、".join(items)
                     + "（这些**没改成**，别当成已生效）")

    if not parts:
        return ("", since_ts)

    d = edits_dir(ai_dir, session_id)
    text = ("[本轮实际改动·自动记录]\n" + "\n".join("- " + p for p in parts)
            + f"\n（正文见 {_rel(d)}/*.json；这是磁盘事实，不是计划）")
    # 第二返回值是「已读到第几行」而不是时间戳：同一秒能写好几条，
    # 时间戳切不出先后，行号才是单调的。
    return (text, str(len(rows)))


def _short(s: str, keep: int = 48) -> str:
    t = str(s or "").replace("\n", " ").strip()
    return t if len(t) <= keep else "..." + t[-(keep - 3):]


def _rel(d: str) -> str:
    """摘要里给相对路径，省 token。"""
    p = str(d or "")
    i = p.replace("\\", "/").rfind("/.ai/edits")
    return p[i + 1:] if i >= 0 else p
