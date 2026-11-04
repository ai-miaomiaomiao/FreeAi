"""智能体（模式）注册表。

一个模式就是 `modes/` 下的**一个 json 文件**，文件名就是它的 id：

    modes/
      easy.json      自由：不排流程，全量工具一次给齐，AI 自己决定怎么干
      chat.json      聊天：只回答，不出交付卡片
      standard.json  标准：第一步先判断这一轮该做什么（问用户 / 改代码 / 改文档 /
                     只给结论），其余流程同「设计」
      task.json      任务：查证 → 改代码 → 交付，不写设计文档
      docs.json      文档：标准去掉「改代码验证」一步，只分析问题、只写文档
      debug.json     调试：查证 → 改编译验（改代码 + 编译推送 + 实机验证合成一轮）→ 写文档
                     → 交付，直到问题消失
      design.json    设计：先方案后代码，改动落 .ai/md/（比任务多一步「改文档」）

为什么是 json 而不是像插件那样的 `__init__.py`：模式**没有可执行逻辑**，
它只是一组声明 —— 提示词、流程档位、工具白名单。能声明的东西就别给代码，
用户加一个模式只要复制一个 json 改几行，不用懂 Python，也不会因为某个模式
写崩了把服务带下去。

字段（全部可选，缺省即"和以前一样"）：

    name        str     显示名，默认取文件名
    icon        str     选择器上的小图标（一个字符，可省略）
    desc        str     一句话说明，鼠标悬停显示
    order       int     选择器里的顺序，小的在前，默认 100
    flow        str     "task" / "chat" —— 直接就是 stream_agent 的 mode 参数：
                        chat = 不出交付卡片、不做交付核对（见 agent_llm 的说明）
    prompt      str     追加到 system 末尾的提示词
    tools       list    工具名白名单；null / 省略 = 全部可用
    model       str     可选：这个模式固定用哪个模型，空 = 跟全局配置
    stages      list    流程编排（可选）。写了就是多步流程，没写就是老的单阶段行为。
                       **注意别和上面的 flow 混了**：flow 是"出不出卡片"的档位，
                       stages 是"分几步做、每步用什么工具和提示词"。每一项：
                         id      str   阶段标识，缺省 s1/s2…；next 里引用它
                         name    str   显示名（进度条上给人看）
                         tools   list  这一步的工具白名单；null / 省略 = 用智能体级
                         prompt  str   这一步的职责说明（追加到 system 末尾）。
                                       实际文案优先读 modeltips/stages.json 的
                                       `模式id.阶段id`，这里这份是兜底
                         next    list  允许跳转到的阶段 id；省略 = 顺序走下一项，
                                       [] = 终点（这一步做完就交付）
                       最后一步（next 为空）才出交付卡片，中间几步的卡片会被丢掉。

两条与插件一致的规矩：

  ① **加载失败只跳过并记日志**，绝不影响服务起来。
  ② 改完文件**立刻生效**（按 (名称, mtime, 大小) 指纹热加载），不用重启。
"""

import json
import os
import threading

import model_tips   # 阶段提示词可外置到 modeltips/stages.json（改文案不用重启）

#: 模式目录（跟本文件同级）
MODES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modes")

#: 没有任何模式可用时的退路，也是前端「没选过模式」时的默认值。
#: 用 "standard" 而不是 "design"：标准模式第一步先判断这一轮该做什么
#: （问用户 / 改代码 / 改文档 / 只给结论），适用面最广；design 的
#: 「先定方案再动手 + 强制落文档」对纯答疑、纯改文档是多余的一步。
DEFAULT_ID = "standard"

#: 只认这两种流程档位，其它一律按 task 处理（拼错不该让整轮不出卡片）
FLOWS = ("task", "chat")

#: 一个智能体最多声明多少个阶段。不是技术上做不到，是每多一步就多一次
#: 完整上游调用（历史 + 记忆 + 工具表全发一遍）—— 超过这个数的流程基本都该
#: 拆成两个智能体，而不是让用户在一条消息里等十分钟。
MAX_STAGES = 12

#: 一条消息里最多跳多少次阶段（含回到自己）。
#:
#: 和 MAX_STAGES 分开是因为自循环：调试模式的「改编译验」（loop）一轮解决不了就
#: next 指回自己，一轮一跳。用 MAX_STAGES 当跳转上限的话，
#: 12 跳里还要扣掉查证、写文档、交付，剩不到十轮修复 ——
#: 而修一个不好查的 bug 十几轮很正常。
#: 仍然要有硬上限：模型原地打转时得有人踩刹车，不能靠它自觉。
MAX_HOPS = 24

#: 阶段提示里最多回看前面几条结论。
#: 自循环时这条链会一直长，全塞进去等于每轮都为已经不成立的老结论付费。
#:
#: 为什么是 20 而不是更小：apply 自循环后进「改文档」时，最早那几轮改了哪些文件
#: 只存在于各自那一条 conclusion 里 —— 系统提示的「本轮实际改动」按行号游标只给
#: 增量，doc 那次注入拿不到前面几轮的清单。8 条时 apply 循环 6 轮以上就会丢，
#: 20 与跳转硬上限 MAX_HOPS = 24 基本齐平：极端 24 跳最多省 4 条，正常流程一条不丢。
MAX_NOTES = 20

_lock = threading.Lock()
_cache = {"fp": None, "modes": {}, "ids": []}


def _num(value, default=100.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_stages(raw, mid):
    """把 `stages` 规整成阶段表。没写 / 写坏成一坨 → None（= 老的单阶段行为）。

    归一化的三条规则，都是"别让用户的一次笔误把流程堵死"：
      · next 省略 = 顺序走下一项，最后一项自动是终点。写 `next: []` 才叫显式终点。
      · next 里提到不存在的 id：丢掉并告警。留着它的话，模型选了就等于跳进虚空，
        而报错信息又指不回是哪个 json 写错了。
      · tools 写成 [] 按"没有这一步的限制"处理（同智能体级白名单的处理，
        空数组的本意几乎从来不是"禁掉所有工具"）。
    """
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for i, item in enumerate(raw[:MAX_STAGES]):
        if not isinstance(item, dict):
            print("[warn] 模式 %s 的第 %d 个阶段不是对象，已跳过" % (mid, i + 1))
            continue
        sid = str(item.get("id") or "").strip() or "s%d" % (i + 1)
        if any(s["id"] == sid for s in out):
            print("[warn] 模式 %s 的阶段 id 重复：%s，已跳过后一个" % (mid, sid))
            continue
        tools = item.get("tools")
        if tools is not None:
            tools = [str(t).strip() for t in tools if str(t).strip()]
            # 空数组**保留为空**，意思是"这一步不用任何工具，只留交棒"。
            # 以前这里把 [] 归一成 None（= 不限制），于是交付阶段写 [] 反而
            # 拿到全量工具 —— 想在配置里表达"没工具可用"，只能靠把工具名
            # 一个个列全，列漏一个就多一份越权。阶段级的空表是安全的：
            # filter_schemas 总会补上 finish_stage，不会真的一无所有。
            # （智能体顶层 tools 的空数组仍然按"不限制"处理，见 _load_one。）
        nxt = item.get("next")
        if isinstance(nxt, str):
            nxt = [nxt]
        elif nxt is not None:
            nxt = [str(x).strip() for x in (nxt or []) if str(x).strip()]
        #: need：这一步必须**真做过**什么，才允许 finish_stage。
        #:   read = 至少一次只读取证（read_file / search_text / adb / probe …）
        #:   work = 至少一次动手（写文件 / 跑命令 / gradle …）
        #:   verify = 至少真跑过一次验证（编译 / 测试 / 日志 / 静态检查）
        #:   ask = 至少调过一次 ask_user（把选择权真交给用户，见 ASK_TOOLS）
        #: 空 = 不检查。为什么需要它：模型可以一步都不查、直接 finish_stage
        #: 交一段凭历史记忆写的结论，五步流程"形式全对、实质空转"。
        need = str(item.get("need") or "").strip().lower()
        # 组合：`work+verify` = 又要动手、又要真跑过一遍。
        _parts = [x.strip() for x in need.split("+") if x.strip()]
        if any(x not in ("read", "work", "verify", "ask") for x in _parts):
            print("[warn] 模式 %s 的阶段 %s 写了不认识的 need=%r，已忽略"
                  % (mid, sid, need))
            need = ""
        out.append({"id": sid,
                    "name": str(item.get("name") or sid).strip() or sid,
                    "tools": tools,
                    "prompt": str(item.get("prompt") or "").strip(),
                    "need": need,
                    "next": nxt})
    if not out:
        return None
    ids = [s["id"] for s in out]
    for i, s in enumerate(out):
        if s["next"] is None:
            s["next"] = [ids[i + 1]] if i + 1 < len(ids) else []
            continue
        bad = [x for x in s["next"] if x not in ids]
        if bad:
            print("[warn] 模式 %s 的阶段 %s 指向了不存在的 next：%s，已忽略"
                  % (mid, s["id"], "、".join(bad)))
        s["next"] = [x for x in s["next"] if x in ids]
    return out


def _load_one(path, mid):
    """读一个 json。字段脏了就按缺省值纠正，不抛异常。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")

    flow = str(data.get("flow") or "task").strip().lower()
    if flow not in FLOWS:
        print("[warn] 模式 %s 的 flow=%r 不认识，按 task 处理" % (mid, flow))
        flow = "task"

    tools = data.get("tools")
    if tools is not None:
        tools = [str(t).strip() for t in tools if str(t).strip()]
        # 空数组按"没有限制"处理：写错成 [] 就等于把工具全禁掉，
        # 模型会退化成只会写字、连 list_dir 都调不了，排查起来很难指向这里。
        if not tools:
            tools = None

    return {
        "id": mid,
        "name": str(data.get("name") or mid),
        "icon": str(data.get("icon") or ""),
        "desc": str(data.get("desc") or ""),
        "order": _num(data.get("order")),
        "flow": flow,
        "prompt": str(data.get("prompt") or "").strip(),
        "tools": tools,
        "model": str(data.get("model") or "").strip(),
        "stages": _parse_stages(data.get("stages"), mid),
    }


def _fingerprint():
    """目录里所有 json 的 (名字, mtime, 大小)。变没变只看这个。"""
    fp = []
    try:
        for name in sorted(os.listdir(MODES_DIR)):
            if not name.lower().endswith(".json"):
                continue
            try:
                st = os.stat(os.path.join(MODES_DIR, name))
            except OSError:
                continue
            fp.append((name, st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    return tuple(fp)


def _reload_if_changed():
    """必要时重扫目录。返回 (modes, ids)，两个都是缓存里的**同一份对象**。

    绝大多数调用只花一次 listdir + 几次 stat，跟 `model_tips` 的热加载同款。
    """
    fp = _fingerprint()
    if _cache["fp"] == fp:
        return _cache["modes"], _cache["ids"]
    with _lock:
        if _cache["fp"] == fp:          # 等锁期间别人已经刷过了
            return _cache["modes"], _cache["ids"]
        modes = {}
        try:
            names = sorted(os.listdir(MODES_DIR))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".json"):
                continue
            mid = name[:-5]
            if mid.startswith("_"):
                continue
            try:
                modes[mid] = _load_one(os.path.join(MODES_DIR, name), mid)
            except Exception as e:      # noqa: BLE001
                # 单个模式写坏了只是这个模式不出现，不影响其它模式、
                # 更不影响服务（这里在请求线程里跑，抛出去就是 500）。
                print("[warn] 模式 %s 加载失败，已跳过：%s" % (mid, e))
        ids = sorted(modes, key=lambda k: (modes[k]["order"], k))
        _cache.update(fp=fp, modes=modes, ids=ids)
        return modes, ids


def all_modes():
    """给前端的模式清单（按 order 排好）。"""
    modes, ids = _reload_if_changed()
    return [dict(modes[i]) for i in ids]


#: 「改文档」这一步的标准 id。模式流程里声明了它，这个模式就有一步专门用来写文档。
DOC_STAGE_ID = "doc"





def get(mode_id):
    """按 id 取模式；空的 / 不认识的都退回默认模式。

    退回而不是报错：前端可能缓存着已经被删掉的模式名，一条消息不该因为
    "模式文件删了"而发不出去。
    """
    modes, _ = _reload_if_changed()
    mid = str(mode_id or "").strip()
    m = modes.get(mid) or modes.get(DEFAULT_ID)
    if m:
        return m
    # 目录里一个模式都没有（被删光了）：给一份能用的兜底，行为等于以前的任务模式。
    # name 必须跟着上面的 DEFAULT_ID 改：目录被清空时前端拿到的显示名就是它，
    # 对不上会变成「id=standard、名字写着设计」。
    return {"id": DEFAULT_ID, "name": "标准", "icon": "", "desc": "",
            "order": 0, "flow": "task", "prompt": "", "tools": None,
            "model": "", "stages": None}


def resolve(mode_id):
    """把前端传来的模式名规整成真实存在的 id。"""
    return get(mode_id)["id"]


def flow_of(mode_id):
    return get(mode_id)["flow"]


def prompt_block(mode_id):
    """模式提示词，拼到 system 末尾用。空字符串 = 不加。"""
    m = get(mode_id)
    body = m.get("prompt") or ""
    if not body:
        return ""
    return ("===== 智能体：%s =====\n%s\n===== 智能体结束 ====="
            % (m.get("name") or m["id"], body))


def stages_of(mode_id):
    """这个智能体声明的阶段表；没写 `stages` 就是空列表（老路径）。"""
    return get(mode_id).get("stages") or []


def entry_stage(mode_id):
    """流程的入口阶段 id；没有流程就返回空串（调用方据此走老路径）。"""
    st = stages_of(mode_id)
    return st[0]["id"] if st else ""


def stage_of(mode_id, stage_id):
    """按 id 取阶段；不认识就返回 None（拼错的 id 不该让整轮请求 500）。"""
    sid = str(stage_id or "").strip()
    if not sid:
        return None
    for s in stages_of(mode_id):
        if s["id"] == sid:
            return s
    return None


#: 只读取证类工具：算出「need=read」够不够。只收**能拿到事实**的 ——
#: todo / remind / finish_stage 不产生任何新证据，不能拿来凑数。
#:
#: ask_user 算：它是**阻塞式**的，发出去就得等用户真答（固定 600 秒，
#: 无入参，见 agent_actions._t_ask_user 与 ASK_MAX_WAIT），
#: 拿到的是"哪台设备、哪一步、什么现象"这类查代码查不出来的事实。不算它
#: 反而出事：实测调试第一步只问了用户一句就被闸门打回，模型在结论里抱怨
#: "工具表里没有 adb"，然后反复重试交棒 —— 它明明有 adb，只是那一轮问了人。
READ_EVIDENCE_TOOLS = frozenset({
    "read_file", "search_text", "find_files", "list_dir", "locate_symbol",
    "check_files", "image", "fetch_url", "web_search", "remote_fetch",
    # 这里的 "adb" 是**工具名**（白名单按名字比对），它同时是能力 device /
    # dumpsys 的提供者。插件哪天改工具名，这份白名单和 modes/*.json 要一起改；
    # 内核里按能力找工具的地方（agent_plugins.find_by_cap）不用动。
    "adb", "diff_text",
    "ask_user",
    # svn status/diff/log 是"工作副本现在长什么样"的第一手证据，比翻源码准。
    # 归纳在 work 里会让 apply 蒙混过关，归纳在这里才对。
    "svn",
    # run_read 是只读命令通道（查看类），拿它取证才算数；真·run_command 仍归 work。
    "run_read",
})

#: 动手类工具：满足「need=work」。
WORK_TOOLS = frozenset({
    "write_file", "edit_file", "run_command", "run_script",
    "gradle", "remote_push", "code_complete", "colorize",
})
# 注意 svn **不算**动手：它 17 个 action 里只有 add/revert/commit/ignore/delete
# 会改东西，status/diff/log/cat 全是只读。放进 WORK_TOOLS 的直接后果是
# 「落代码」那一步只要跑一次 `svn status` 就过闸门 —— 零写入也能交棒，
# 正是"五步走完一个文件没改"的一条通路。


#: 验证类工具：满足「need=verify」。只认**真跑起来**的 —— 编译、测试、日志、
#: 静态检查。read_file / search_text 不在这里：再看一遍代码看出去的东西，
#: 和上一步"我觉得没问题"是同一句话，验证的意义就在"不看代码也能判断"。
VERIFY_TOOLS = frozenset({
    "run_command", "run_script", "gradle", "adb", "probe", "check_files",
    "svn",
})


#: 「必须先问用户」类工具：满足「need=ask」。
#:
#: 为什么要单开一档而不是复用 read：ask_user **本来就在** READ_EVIDENCE_TOOLS
#: 里（它确实能拿到查代码查不到的事实），于是 need=read 的阶段只要问一句人
#: 就过闸 —— 想强制「第一步必须把选择权交给用户」的流程，写 read 是无效的，
#: 模型可以直接查完就交棒。要的是"真问过"，所以判定必须只看 ask_user 本身。
ASK_TOOLS = frozenset({"ask_user"})


# 关于「校验」：这里**曾经**有一道 need=check（要求交棒前本阶段改过的文件
# 全部校验通过，配套 stage_files 账本 / stage_check 汇总 / 交棒被拒的逃生口）。
# 已经整条撤掉 —— 校验只留两处真正必要的：
#   ① edit_file 落盘后立刻自校验（agent_actions._auto_check），改坏的当场知道；
#   ② 交付核对 agent_llm._audit 整轮跑一次批量校验。
# 为什么不再做阶段闸：闸门要成立就必须让每一次写入都记账（write_file 也得验），
# 于是同一份改动被验两遍，还拖出三个 ctx 字段和一个逃生口。冗余本身就是故障源。


#: 与流程无关、任何模式任何阶段都得留着的工具（裁剪时自动并进白名单）。
#:
#:   · finish_stage —— 它不在任何人的白名单里（写阶段配置时想不到它），少了它
#:     模型没法交棒，整个流程卡在第一步。没有流程的模式末尾会把它摘掉。
#:   · handoff —— 人工作业。`modes/*.json` 是显式白名单，一个都没写 handoff，
#:     于是它每一轮都被裁掉：模型**根本看不见**这个工具，用户要的"AI 一边跑
#:     一边喊人、人做完接着跑"从来就没成立过（它会退回 probe，或者干脆停下来
#:     等）。它跟阶段无关，也不能靠某个模式临时打开 —— 用户随时可能派活。
ALWAYS_TOOLS = ("finish_stage", "handoff")


def _one_need(need, stage, names) -> str:
    """单一需求的判定。返回空串 = 满足。"""
    who = (stage or {}).get("name") or (stage or {}).get("id") or "当前步骤"
    # 本阶段工具表里压根没有能过这道闸的工具 → 放行。
    # 不改的话就是死循环：模型调不动那个工具，每次交棒都被同一句话打回，
    # 只能一遍遍重试或者干脆编一句结论。真出现这种情况是模式配置写错了
    #（比如阶段白名单里漏了 adb），该报出来修配置，不该让模型去撞墙。
    tools = {str(x) for x in ((stage or {}).get("tools") or ()) if x}
    cap = {"read": READ_EVIDENCE_TOOLS, "work": WORK_TOOLS,
           "verify": VERIFY_TOOLS, "ask": ASK_TOOLS}.get(need)
    if cap and tools and not (tools & cap):
        print("[warn] 阶段「%s」的 need=%s 在本阶段工具表里没有任何对应工具，"
              "跳过这道闸（检查模式配置）" % (who, need))
        return ""
    if need == "ask":
        # ask_user 在 READ_EVIDENCE_TOOLS 里（它确实算取证），所以这一档
        # **不能**写成 `names & READ_EVIDENCE_TOOLS` —— 那样查一轮文件就过了，
        # 「必须问用户」等于没要求。只认 ask_user 这一个名字。
        if names & ASK_TOOLS:
            return ""
        return ("这一步（%s）要求先把选择权交给用户：本阶段一次 ask_user 都没调。"
                "请先用 ask_user 给出 2-4 条**互不等价**的路（不同的做法，"
                "不是「做 / 不做」的补集），选项里写清各自改什么；"
                "用户答完当成确定句继续，超时/被停止就按最合理的一条继续，"
                "并在 conclusion 里写明「按 X 继续」。" % who)
    if need == "read":
        if names & READ_EVIDENCE_TOOLS:
            return ""
        return ("这一步（%s）要求先取证，但本阶段一次取证工具都没调"
                "（read_file / search_text / adb / probe 都行）。"
                "结论若只凭历史记忆写出来，行号一过时就全错。" % who)
    if need == "verify":
        if names & VERIFY_TOOLS:
            return ""
        return ("这一步（%s）要求验证，但本阶段没有跑任何东西。"
                "编译 / 测试 / adb logcat / svn diff 任选一条真跑，"
                "把输出贴进 conclusion；只看代码不算验证。" % who)
    if need == "work":
        if names & WORK_TOOLS:
            return ""
        return ("这一步（%s）要求动手，但本阶段没有写文件 / 跑命令。"
                "如果确实不需要改动，请在 conclusion 里写明原因再交棒。" % who)
    return ""


def missing_parts(stage, used) -> list:
    """这一步哪些 need 没满足（返回名字列表，空 = 全过）。"""
    need = str((stage or {}).get("need") or "")
    if not need:
        return []
    names = {str(n) for n in (used or []) if n}
    out = []
    for part in (x.strip() for x in need.split("+")):
        if not part:
            continue
        if _one_need(part, stage, names):
            out.append(part)
    return out


def stage_missing(stage, used) -> str:
    """这一步要求的实质动作做了没有。返回空串 = 满足（或没要求）。

    `used` 是这个**阶段内部**真正执行过的工具名（跨阶段累计的不算：
    上一步查过的东西，不能替这一步的结论作保）。

    need 可以组合，用 `+` 连起来：
    - 「work+verify」= 既要动手、又要真跑一遍。「改代码验证」是一步，只改不跑
      （改完自称没问题）和只跑不改都过不了。
    """
    names = {str(n) for n in (used or []) if n}
    for part in missing_parts(stage, used):
        msg = _one_need(part, stage, names)
        if msg:
            return msg
    return ""


def stage_index(mode_id, stage_id):
    """第几步（0 起）。认不出返回 -1。"""
    for i, s in enumerate(stages_of(mode_id)):
        if s["id"] == stage_id:
            return i
    return -1


def stage_block(mode_id, stage_id, notes=None, evidence=None):
    """当前这一步的提示词块，由调用方以 `stage_note` 挂到最后一条 user 消息
    的尾巴上（**不进 system**：它每步都换，塞进 system 会让后面整段历史的
    前缀缓存全部作废；老路径返回空串）。

    notes 是前面几步的结论 `[{"id","name","conclusion"}]`。**只传结论、不传
    全文**：前面几步的工具过程留在界面 segs 里给人看，对模型来说那几百行
    调用记录只是噪音，而这一步真正需要的是"上一步得出了什么"。

    evidence 是前面几步**真正取到的工具结果原文**（读文件 / 搜代码，见
    web_agent._note_evidence）。只传结论时，下一步要写文档就得把上一步读过
    的文件整套重读 —— 实测 design 第 3 步把第 1 步读过的 8 个文件又读了一遍，
    十几次工具调用白烧。原文给到这里，它就能直接引用。
    """
    st = stage_of(mode_id, stage_id)
    if not st:
        return ""
    allst = stages_of(mode_id)
    idx = stage_index(mode_id, stage_id)
    title = get(mode_id).get("name") or mode_id
    lines = ["===== 流程：%s（第 %d/%d 步 · %s）====="
             % (title, idx + 1, len(allst), st["name"]),
             # 交棒要求放在最前面：放在末尾时，模型读到"只做这一步"就开始干活，
             # 干完往往忘记收尾（实测 flash 档模型 100% 漏掉末尾那句）。
             ("这一轮你只做「%s」这一步 —— 这是最后一步，前面几步已经做完；"
              "收尾时调用 finish_stage。"
              % st["name"]) if idx == len(allst) - 1 else
             ("这一轮你只做「%s」这一步，不要替后面几步干活；做完必须调用 "
              "finish_stage 交棒 —— 不调用就等于这一步没结束。"
              % st["name"])]
    # 全局路线图。只告诉模型"第 1/6 步"没用 —— 它不知道另外五步是什么，
    # 只看到"这一小步要干活"，就会本能地把整个任务一次做完（实测：scan
    # 阶段读完源码直接写 md 结论文档，因为"写文档"在它看来是任务的一部分）。
    # 把六步全摆出来、标出当前位置，它才知道写文档是第 5 步的事。
    marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
    def _mk(i):
        return marks[i] if i < len(marks) else "(%d)" % (i + 1)
    lines.append("全流程：" + " → ".join(
        "%s %s" % (_mk(i), x["name"]) for i, x in enumerate(allst)))
    if idx == len(allst) - 1:
        lines.append(
            "你现在在 %s「%s」——**最后一步**，前面几步的活都做完了，"
            "这一轮只汇总、不再回头改任何东西。" % (_mk(idx), st["name"]))
    else:
        # 只在流程里**真有**那一步时才提它：没有 doc 阶段的模式（任务）若照旧说
        # 「写文档只在「改文档」」，等于把模型往一个不存在的步骤上引。
        # 当前这一步自己就是那个角色时（调试的「改代码并验证」既改又验）不要
        # 自指 ——「写文件只在「改代码并验证」」这句等于什么也没说，还会让它以为
        # 该把活留给别人。注意不能改成「往下找另一个有写工具的」，design 的
        # doc 阶段也有写工具，那样会指到错的步骤上。
        writers = [x for x in allst
                   if "write_file" in (x.get("tools") or [])
                   or "edit_file" in (x.get("tools") or [])]
        docer = next((x for x in allst if x["id"] == DOC_STAGE_ID), None)
        scope = []
        if writers and writers[0]["id"] != st["id"]:
            scope.append("写文件只在「%s」" % writers[0]["name"])
        if docer and docer["id"] != st["id"]:
            scope.append("写文档只在「%s」" % docer["name"])
        lines.append(
            "你现在在 %s「%s」。**后面几步各有各的活，这一轮一件都不要替它们做**——"
            "越界产出的东西不会被采纳（%s）。"
            % (_mk(idx), st["name"], "，".join(scope) or "只做本步该做的事"))
        # 光说"越界不会被采纳"不够：实测 design 第 1 步（查证定方案）没有写文件
        # 工具，模型就把整篇 md 贴进正文，末尾还写「这一轮我没有写文件的能力，
        # 请你保存为 xxx.md」—— 它知道本步不能写文件，但把"贴出来"当成了
        # 唯一能把内容交给用户的通道，于是第 3 步（改文档）再产出一遍同样的东西。
        # 必须把「正文」这个通道也点明：正文只报进展，成品全文归负责那一步。
        lines.append(
            "**正文只写给人看的进展**（看了什么、结论是什么、下一步走哪），"
            "不要在这里输出最终交付物的全文（整篇文档 / 大段代码 / 完整总结）："
            "这一轮贴出来不会被保存成文件，「%s」那步还会再产出一次，"
            "用户看到的就是同一件事做了两遍。"
            % allst[-1]["name"])
    # 这一步到底能用什么，写成一句人话。
    # 有实测：带着 adb 的阶段里，模型在 finish_stage 的结论中写「工具表里没有
    # 给我 adb / probe，我动不了手」然后反复重试交棒。工具表本身它看得到，
    # 但它会按"阶段名字"想象能力边界（"这一步叫复现，大概是只读的"）。
    # 把「给不给你看设备 / 能不能改代码」摊开说，这种自我设限就没借口了。
    # 阶段没写 tools = 沿用模式级白名单（跟 filter_schemas:667 判 `is not None`
    # 是同一套语义）。**不在这里回落的话这段会误判**：读到的空集被当成"这一步
    # 一个工具都没有"，于是往提示词里写「别读文件、别跑命令」，可它实际有
    # adb / run_read —— 模型照着提示词自我设限，工具在表里也不敢调。
    _st_tools = st.get("tools")
    if _st_tools is None:
        _st_tools = get(mode_id).get("tools") or []
    tools_set = {str(x) for x in _st_tools if x}
    if tools_set:
        caps = []
        if tools_set & {"adb", "probe", "image"}:
            caps.append("看设备（adb / 截屏 / 时间线）")
        if tools_set & {"write_file", "edit_file"}:
            caps.append("改代码")
        if "gradle" in tools_set:
            caps.append("编译装包")
        if "run_command" in tools_set or "run_script" in tools_set:
            caps.append("跑命令")
        lines.append("这一步给你的能力：%s。别自己想象边界，工具表里没有的才是真没有。"
                     % ("、".join(caps) if caps else "只读查证"))
    else:
        # 工具表为空（专指交付这种只交棒的步骤）：不点明的话，模型会照着自己
        # 对"交付"的想象去读文件/跑命令（历史里这些形状都在），然后被阶段闸门
        # 一个个拒掉，白烧好几轮才想起交棒。
        lines.append("这一步**没有工具**（工具表里只有 finish_stage）："
                     "别读文件、别跑命令，把结论写进 finish_stage 交棒即可。")
    # 提示词优先读 modeltips/stages.json（键 `模式.阶段`），没配才用 modes/*.json
    # 里那份。外置的收益是改一句话不用碰模式配置 —— 那份 json 同时写着工具表、
    # 跳转关系、闸门，为了调文案去动它，手滑一次整步流程就变了。
    #
    # ★ 三层分工，改文案前先想清这句该放哪一层：
    #   能力层 = 插件 SCHEMA 的 description：工具「做了什么、有什么客观后果」，
    #            例如日志落到哪个文件、哪个 action 会毁现场、两种取法差多少字节；
    #   纪律层 = 用户的记忆：跨工作区的长期约定，例如「日志先落盘再分析」；
    #   判断层 = 这里：这一轮该不该动手、走哪条路、交棒给谁。
    #   以前把「怎么调 adb」抄进阶段提示，同一段话散在 7 处，还漏了没有 key 的
    #   standard.doc（它实际下发的是 modes/standard.json），改完照样漏一条。
    #   现在阶段提示只说「用 adb 拿证据」，具体调法一句「见 adb 工具描述」。
    #
    # ★ 阶段提示里**不要**写参数级细节（落盘路径、容量数字、action 名堆砌）。
    # ★ 改文案前先确认这一阶段有没有 key：stages.json 只覆盖 14 个阶段，没有 key
    # 的那几步（chat.deliver、standard.apply / standard.doc /
    # standard.deliver、docs.doc / docs.deliver）实际下发的是 modes/*.json 里的
    # prompt。只改 stages.json 会漏。
    lines.append(model_tips.text("stages", "%s.%s" % (mode_id, stage_id),
                                 st.get("prompt") or ""))
    if lines[-1] == "":
        lines.pop()
    if notes:
        # 只留最近几条：自循环（调试的 loop 指回自己）会让这个列表一直长下去，
        # 24 轮后光「前面几步的结论」就几千字，把真正有用的信息挤掉。
        # 早期的结论本来也已经被后来的覆盖了，留一行说明省了几步就够。
        shown = notes[-MAX_NOTES:]
        lines.append("前面几步的结论：")
        if len(notes) > len(shown):
            lines.append("（更早的 %d 步已省略）" % (len(notes) - len(shown)))
        for n in shown:
            lines.append("- %s：%s" % (n.get("name") or n.get("id") or "上一步",
                                       (n.get("conclusion") or "").strip() or "（无）"))
    # 最后一步（交付）不塞原文：它只汇总结论，没有工具需要这些原文，
    # 塞进去只会把结论挤出上下文窗口。
    if evidence and idx < len(allst) - 1:
        lines.append("前面几步取到的工具结果原文（**直接引用，不要再调一次同样的工具**；"
                     "标了「无结果」的是已经查过、确实没有的，别再去撞一遍）：")
        for e in evidence:
            lines.append("- [%s · %s%s] %s"
                         % (e.get("stage") or "前面",
                            e.get("tool") or "",
                            "" if e.get("ok") else " · 无结果",
                            e.get("args") or ""))
            txt = (e.get("text") or "").strip()
            if txt:
                lines.append(txt)
    if st["next"]:
        labels = []
        for x in st["next"]:
            nm = next((s["name"] for s in allst if s["id"] == x), x)
            labels.append("%s（%s）" % (x, nm))
        lines.append("finish_stage 怎么填：conclusion 写这一步的结论（具体到文件、行号、"
                     "结论、待办 —— 后面几步只看结论，看不到你的过程）；"
                     "next 填下一步的 id，可选：%s。" % "、".join(labels))
    else:
        lines.append("这是最后一步：收尾时调用 finish_stage，conclusion 写最终交付结论，"
                     "next 留空。")
    lines.append("===== 流程结束 =====")
    return "\n".join(lines)


def cfg_for(cfg, mode_id):
    """按模式覆盖模型（必要时连网关一起换）。没配 model 就原样返回同一个 cfg 对象。

    模式 json 里写的 model 可能是**别的网关**上的名字（比如某个模式固定用
    自建的 `deepseek-flash`）。只换 model 不换 base_url，请求就会拿着这个名字
    去打默认网关，上游回 404 / model not found —— 看着像"这个模式坏了"。
    所以这里按模型名反查它属于哪个端点（agent_endpoints.endpoint_of_model），
    查得到就一整套换过去。
    """
    model = get(mode_id).get("model") or ""
    if not model or model == (cfg or {}).get("model"):
        return cfg
    out = dict(cfg or {})
    out["model"] = model
    try:
        import agent_endpoints        # 放函数里 import：模块级会绕成循环引用
        ep_id = agent_endpoints.endpoint_of_model(cfg, model)
        if ep_id:
            target = agent_endpoints.resolve(cfg, ep_id)
            out["base_url"] = target["base_url"]
            out["api_key"] = target["api_key"]
            out["endpoint"] = target["endpoint"]
    except Exception as e:            # noqa: BLE001
        # 端点解析坏了不能让整轮对话失败：退回"只换模型名"的老行为
        print("[warn] 模式 %s 的端点解析失败：%s" % (mode_id, e))
    return out


def _by_names(schemas, allowed):
    names = set(allowed or ())
    return [s for s in schemas
            if str((s.get("function") or {}).get("name") or "") in names]


def filter_schemas(schemas, mode_id, stage_id=""):
    """按智能体（可选：再按阶段）的白名单裁剪工具表。

    兜底：裁完一个都不剩就退回原表 —— 空工具表是这套系统最贵的故障
    （模型只能把调用写进正文，整轮静默失效，见 agent_actions.all_schemas
    里那三段注释），白名单写错不值得拿它去换。

    `stage_id` 是当前流程阶段：它的 tools 只在智能体白名单**之内**再收窄一次，
    不会越权放出智能体本身没允许的工具。

    有两类工具不受白名单约束，见 ALWAYS_TOOLS：`finish_stage`（少了它流程卡在
    第一步）和 `handoff`（人工作业，跟阶段无关；不常驻就等于模型看不见它）。
    唯一例外是**没有流程**的模式（`stages` 为空，如自由 / 对话）：那里把
    `finish_stage` 摘掉 —— 没有阶段就没有"交棒"，留着它只会让模型空调一次
    （`_t_finish_stage` 只能回「这一轮没有启用多步流程」）。`handoff` 不摘。
    """
    kept = schemas
    allowed = get(mode_id).get("tools")
    if allowed:
        # 常驻工具无条件并进白名单（见 ALWAYS_TOOLS）：
        #   · finish_stage 只在有流程时才有意义，没有流程的话函数末尾会再摘掉；
        #   · handoff 跟流程无关 —— 它没写进任何 modes/*.json，不在这里补，
        #     模型就永远看不见它，只能退回 probe 或干脆停下来等人。
        allowed = list(allowed) + [n for n in ALWAYS_TOOLS if n not in allowed]
    if allowed:
        picked = _by_names(kept, allowed)
        if picked:
            kept = picked
        else:
            print("[warn] 模式 %s 的工具白名单一个都没命中，本轮退回全量"
                  % get(mode_id)["id"])
    st = stage_of(mode_id, stage_id) if stage_id else None
    # 判 `is not None` 而不是真假值：`"tools": []` 是明确表达"本步不给工具"，
    # 只有**没写**这个字段（None）才表示沿用智能体级白名单。
    if st and st.get("tools") is not None:
        # 阶段级也要补 handoff：`modes/*.json` 的阶段工具表是按"这一步能干
        # 哪些活"写死的，同样没人想到人工作业 —— 不补的话模式级补了也白补，
        # 阶段一收窄又把它裁掉（标准模式的 apply 就是这么丢的）。
        # 但收尾那一步（`"next": []`，或 `"tools": []` 明确不给工具，如各模式的
        # 「交付」）不加：那一步只该交棒，不该再派新活。
        extra = ["finish_stage"]
        if st["tools"] and st.get("next") != []:
            extra.append("handoff")
        picked = _by_names(kept, list(st["tools"])
                           + [n for n in extra if n not in st["tools"]])
        if picked:
            kept = picked
        else:
            print("[warn] 模式 %s 的阶段 %s 工具白名单没命中，退回智能体级"
                  % (get(mode_id)["id"], st["id"]))
    if not stages_of(mode_id):
        # 没有流程的模式（自由）：全量工具里那个 finish_stage 是纯噪音 ——
        # 模型会把它当"收尾动作"调一次，只能拿回一句"这一轮没有启用多步流程"，
        # 白烧一轮，还容易误以为任务已经交出去了。工具表里没有它，模型才会
        # 老老实实把结论写在正文里。
        kept = [s for s in kept
                if str((s.get("function") or {}).get("name") or "") != "finish_stage"]
    return kept
