"""插件数据目录：**统一落在 `<工作区>/.ai/temp/<插件名>/`**。

以前每个插件各写各的，同一个工作区里散着三处：

    gradle_tools  →  <工作区>/vscode/.gradle_task/
    adb_tools     →  <工作区>/.logcat/
    launcher      →  <工作区>/.ai/scripts.json

后果是：想清缓存得记三个位置；`.gitignore` 也得一处一处补；漏一个就把十几 MB
的 tasks.json 提交上去。现在统一成一棵：

    <工作区>/.ai/temp/gradle_tools/     tasks.json / recent.json / run.log / device.json
    <工作区>/.ai/temp/logcat/           current.log / ps.txt / snapshot.json / state.json
    <工作区>/.ai/temp/launcher/         scripts.json

`temp` 这个名字是给用户看的：**里面全是可重建的东西** —— 任务列表能重列、
日志能重抓、脚本记录没了只是少个历史。整个目录随时可以删。

放在 `.ai/` 下面还顺带解决了一件事：`.ai/` 本来就在 `.gitignore` 里（"工作区私有
数据"），不用再为每个插件补规则。

`migrate` / `migrate_file` 负责把老位置的东西搬过来。**只搬一次、只在新位置还空着
时搬**：用户已经在用的历史不该因为改个路径就丢。搬不动（占用 / 权限）也不报错 ——
一个缓存拉不起来，不值得挡住插件干活。
"""

import os
import shutil

#: 所有插件数据目录的根（相对工作区）
TEMP_DIR = os.path.join(".ai", "temp")

#: 已经搬过的目标目录 —— `migrate` 会被调用方高频调用（每次取路径都会调），
#: 不能每次都去 listdir。进程内记着就够：迁移本来就是一锤子买卖。
_DONE = set()


def data_dir(workspace, plugin) -> str:
    """`<工作区>/.ai/temp/<插件名>/`。工作区为空 → 空串（调用方自己决定怎么办）。

    只算路径，**不建目录**：读的时候路径不存在是正常情况（那就是"还没有数据"）。
    """
    ws = str(workspace or "").strip()
    if not ws:
        return ""
    return os.path.join(ws, TEMP_DIR, str(plugin))


def migrate(new_dir, *old_dirs) -> str:
    """把老位置 `old_dirs` 里的东西搬进 `new_dir`，返回 `new_dir`（可当路径直接用）。

    · 新目录**已经有文件** → 什么都不做（那儿才是当前数据，老目录是残留）
    · 老目录不存在 → 跳过
    · 同名文件已在新目录 → 跳过那一个（不覆盖）
    · 每个 `new_dir` 只尝试一次（见 `_DONE`）
    """
    if not new_dir:
        return ""
    key = os.path.normcase(os.path.abspath(new_dir))
    if key in _DONE:
        return new_dir
    _DONE.add(key)

    try:
        if os.path.isdir(new_dir) and os.listdir(new_dir):
            return new_dir
    except OSError:
        return new_dir

    ok = True
    for old in old_dirs:
        if not old:
            continue
        try:
            if not os.path.isdir(old):
                continue
            if os.path.normcase(os.path.abspath(old)) == key:
                continue
            os.makedirs(new_dir, exist_ok=True)
            for name in os.listdir(old):
                src = os.path.join(old, name)
                dst = os.path.join(new_dir, name)
                if os.path.exists(dst):
                    continue
                shutil.move(src, dst)
            # 搬空了就顺手把老目录收掉（收不掉也不管：可能是别的插件的目录）
            try:
                if not os.listdir(old):
                    os.rmdir(old)
            except OSError:
                pass
        except OSError:
            ok = False      # 多半是文件被占着（正在抓的 current.log）—— 下次再试
            continue
    if not ok:
        _DONE.discard(key)  # 别把"没搬成"记成"搬过了"
    return new_dir


def migrate_file(new_path, *old_paths) -> str:
    """单个文件版本（launcher 的历史就是一个 json 文件，不是一个目录）。"""
    if not new_path:
        return ""
    key = os.path.normcase(os.path.abspath(new_path))
    if key in _DONE:
        return new_path
    _DONE.add(key)

    if os.path.exists(new_path):
        return new_path
    for old in old_paths:
        if not old or not os.path.isfile(old):
            continue
        if os.path.normcase(os.path.abspath(old)) == key:
            continue
        try:
            parent = os.path.dirname(new_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(old, new_path)
            break
        except OSError:
            _DONE.discard(key)      # 没搬成（占用/权限）—— 下次再试
            continue
    return new_path
