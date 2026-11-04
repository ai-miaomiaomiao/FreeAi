// ---------------------------------------------------------------------------
// 工作空间任务台（独立窗口 `/?detached=board`）
//
// 任务台只有这一种形态：**永远开在独立窗口里**。放到主界面右栏里看着像个
// 分栏，但会话的流、终端的输出都是"发起连接的那个页面"私有的，另一个窗口
// 拿不到 —— 真要跨窗口看同一个后台任务，只能走后端那份账本。
//
// 数据来源（全是后端）：
//   · 主窗口的 DOM 一概用不上，这里是另一个 JS 上下文；
//       /api/sessions         所有工作区的会话列表
//       /api/chat/active      哪些会话此刻还在生成
//       /api/chat/live        某个会话正在写的正文（后端 tee 了一份）
//       /api/chat/stop        停掉某个会话的生成
//       /api/term/jobs        所有终端命令（含别的窗口发起的）
//       /api/term/tail        某条命令的增量输出
//       /api/term/kill        停掉某条命令
//
// 换句话说：**这个窗口能看、能发、能停，但停和看都走后端那条账本**，
// 而不是靠"把主窗口的 DOM 搬过来"。
//
// 边界：终端命令的**存续**仍取决于发起它的那条连接 —— 后端探到 EOF 会杀
// 进程树（见 _client_gone）。所以主窗口一关，正在跑的命令也会结束；这里
// 跟的是"还活着的任务"，不是"能独立续命的任务"。
// ---------------------------------------------------------------------------
"use strict";

const DT = {
  on: false,
  items: [],
  remotes: [],              // 远程工作区（ssh://...），dtLoadItems 每次刷新
  sig: "",
  key: "",                  // "chat|ws|sid" / "term|jobid"
  timer: 0,                 // 下一拍的 setTimeout id（见 dtSchedule）
  busy: false,              // 上一拍还在飞：这一拍直接跳过，别让请求叠起来
  ownJob: "",               // 本窗口发起的终端命令 id（它的输出走 SSE，不靠 tail）
  // 顶部这几个字段只描述**当前正在看**的那个会话；切走时它的运行态存进 bg，
  // 切回来再装回去。以前只有一套字段，会话4 还在生成时切到会话6：
  //   · 会话4 的流回调继续往 live 里追加 → 会话6 的窗口里蹦出会话4 的正文；
  //   · sending 一直是 true → 会话6 点发送走 `if (sending) return`，静默发不出去。
  chat: {
    ws: "", sid: "", seq: -1, running: false, sending: false,
    // 这个会话用的智能体（src/modes/*.json 的 id）。空 = 用默认模式；
    // 真正的值以会话文件为准（dtLoadHistory 拿到 /api/session 的 agent 后覆盖）。
    agent: "",
    reasoning: "",          // 本轮模型的思考过程（只给界面，见 /api/chat/live）
    trace: "",              // 本轮工具轨迹（后端 /api/chat/live 带回来的）
    traceSig: "",           // 上次画过的 trace，变了才重建工具行（免得每秒闪）
    segs: [],               // 有序段：[{kind:"text",text} | {kind:"tool",id,name,args,result,ok}]
    // 段身份数组，和 segs 一一对应。以前是一个拼起来的 sig 字符串：任意一段
    // 变了就整行 `textContent=""` 重铺，旧元素上的 _mdSrc 缓存全丢 → 每多一段
    // 就把前面所有段（最多 300）的 markdown 重算一遍，复杂度 O(n²)。交付那一步
    // segs 一次涌进上百段，全在这一帧里跑完，界面就死了。
    // 现在按前缀比对：只有新段才建节点、才算 markdown。
    segKeys: [],
    segEls: [],             // 和 segs 一一对应的 DOM（被折掉的早期工具行是 null）
    segMoreEl: null,        // 「⋯ 还有 N 条更早的工具记录」那颗按钮
    toolCap: 0,             // 工具行折叠上限；0 = 用默认 DT_MAX_TOOLS（展开后置成大数）
    // 实时路径的工具行账本，**时间序**（最早的在前）：超上限时从头部摘节点。
    // 只留 segEls 不够 —— 那儿按下标存，找不出"哪几行最早"。
    toolRows: [],
    // ---- 历史重铺的增量账本 ----
    // 每轮结束（running -> 不 running）都会 dtPaintChat(true)。以前那条路径是
    // `log.textContent=""` 再照 history 全量重建：节点一换，挂在节点上的 _mdSrc
    // 缓存全失效，一页 20 条里每段正文 / 每块阶段结论都要重跑 mdToHtml，一轮
    // 150~300 次；翻过页之后按 pages 线性放大。现在比对历史前缀，只摘尾巴、
    // 只建新增的那几条。
    histKeys: [],           // 历史消息的稳定身份（dtMsgKey），和第几条一起比
    histEls: [],            // 和 histKeys 一一对应的 .dt-msg 节点
    histSid: "",            // 上面这两份属于哪个会话键：跨会话一律整块重铺
    histMore: null,         // 顶部「加载更早」按钮的引用（增量时要复用/更新文案）
    busy: "",               // 临时状态提示（如「校验中…」）：来自 /api/chat/live，不落历史
    // ---- 历史分页 ----
    // 首屏只铺最后 DT_PAGE 条，更早的点日志区顶部的「加载更早」往回翻。
    // 以前 /api/session 不带任何参数 = 全量：一个 8MB 的界面流水要一次铺出
    // 两万来个节点，切一次会话就是几秒白屏 —— 任务台"转圈很久"的主因就在这。
    more: false,            // 前面还有更早的历史
    start: 0,               // 下一页的 before（后端给的**绝对**下标）
    total: 0,               // 服务端一共多少条（按钮上写"还有 N 条"）
    anchorH: 0,             // 「加载更早」插入前的 scrollHeight，重铺后靠它钉住视野
    pages: 1,               // 已经铺了几页（每轮结束会重拉历史，按这个数重新要）
    // 一次性标记：这一次是**用户切换过来的**（点会话 / 新建 / 其他窗口选中）。
    // 置上之后 dtPaintChat 强制贴底、dtLoadHistory 只要最新一页，铺完即清。
    // 为什么不复用 sameSid：点**同一个**会话时 sameSid 为真，stick 会退化成
    // dtNearBottom，用户上翻过就再也不回到底部 —— 「点一下会话没滚到最新」。
    forceBottom: false,
    bg: {},                 // 会话键 -> { live, trace, running, seq }（后台会话的运行态）
    sendingSet: new Set(),  // 每个会话键各自一把发送锁
    // /api/chat/live 的分频用：上次拉的时间 + 那次是哪个会话。
    // 正文一秒拉一次太浪费 —— 空闲时没人看它，只是用它发现"别处又开跑了"。
    pollAt: 0,
    pollKey: "",
    // 会话键 -> [{id,text,files,urgent}]：这个会话的排队消息，urgent=点过「立即发送」
    q: {},
  },
  //: 任务台终端是"一条命令一个进程"，`cd` 不会留在 shell 里 —— 所以当前目录
  //: 得前端自己记（后端把纯 cd 接住并回 {cwd}，见 _handle_exec）。
  term: { job: "", ws: "", off: 0, running: false, cwd: "", prevCwd: "",
        // sgr：输出区当前的 ANSI 颜色状态（termSgrNew() 的实例，null = 还没开始）。
        // 终端输出经常"这一段设色、下一段才复位"，跨推送必须接着走。
        sgr: null,
        // ---- 流式（/api/term/shell/stream）----
        stream: null,     // { job, abort }：当前那条长连接（null = 没连）
        pending: "",      // 已收到、还没画的文本（rAF 一次性画，别每条都动 DOM）
        raf: 0,           // 上面那笔的 requestAnimationFrame 句柄
        outChars: 0,      // #dtOut 里大致的字符数（触发截断用，粗算即可）
        // atBottom：用户是不是正看着最底部。只在 scroll 事件里更新（见
        // dtTrackBottom）—— 每帧现读 scrollHeight 会强制同步重排，是最贵的一步。
        atBottom: true,
        cwdBuf: "",       // 尾部 4KB 缓冲：[工作目录] 行可能被切在两块之间
        retryAt: 0,       // 下次允许重连的时刻（断开后退避，别每拍都撞）
      },
  built: false,
  seen: new Set(),          // 已经收过的附件 id（广播 + 队列会各来一次）
};

function dtOn() {
  return (typeof DETACHED_BOARD !== "undefined") && DETACHED_BOARD;
}

//: 首屏铺多少条历史。口径和主界面一致（agent_llm.UI_PAGE_MESSAGES = 20）：
//: 用户要的是"接着上一条说"，不是一进来就铺满几百条。
const DT_PAGE = 20;
//: 一个工具行容器最多直接铺多少行，超过的折成一颗「展开」（见 dtFillTools）。
//: 只按"条数"分页挡不住这个：一条消息就能带 800 多个 segs（实测 s4 有 891），
//: 一页 20 条里只要夹着这么一条，建节点还是要跑上千次。
//: **露出来的是最新的 cap 行，折起来的是更早的**：任务台就是拿来看"刚跑了什么"的，
//: 折掉最新的等于把用户在等的那几行藏起来（以前方向反了，被反馈过）。
const DT_MAX_TOOLS = 30;

/**
 * 任务台里「一个会话」的唯一键：**必须带工作区**。
 *
 * 会话是按工作区编号的，每个工作区都有自己的 `s1`。以前 bg（后台运行态）、
 * 发送锁、排队队列全都只用 sid 当键 —— p60 的 `s1` 和 gp 的 `s1` 撞在同一个
 * 槽位上，表现就是"切到 gp 的会话 1，看到的却是 p60 正在跑的内容"。
 */
function dtSessKey(ws, sid) {
  // 工作区部分用 dtWsKey 归一化：同一个目录可能一处写 `D:\a\b`、另一处写
  // `d:/a/b`，不归一化就会出现"明明是同一个会话，却被当成两个槽位"。
  return dtWsKey(ws) + "|" + String(sid || "");
}

/** 当前任务台正在看的那个会话的键。 */
function dtCurKey() {
  return dtSessKey(DT.chat.ws, DT.chat.sid);
}

function dtKey(kind, ws, id) {
  return kind + "|" + (ws || "") + "|" + id;
}

/**
 * 工作区路径的比对键。
 *
 * 后端 `/api/chat/active` 回的是 `_STORES` 的键 —— 那是 `os.path.normcase`
 * 过的（Windows 上 `/` 转 `\` 且**转小写**）。前端 `S.trees` 里是原始写法
 * （`D:\workspace\demo`）。不归一化直接拼 key，就永远对不上，列表里所有会话
 * 都显示成"空闲"，耗时和停止键自然也不出现。
 */
function dtWsKey(ws) {
  return String(ws || "")
    .replace(/[\\/]+/g, "\\")
    .replace(/\\+$/, "")
    .toLowerCase();
}

function dtEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

//: 超过这个长度就不再走 Markdown。mdToHtml 是一条条正则替换，几万字要跑几百
//: 毫秒，而这段文本在任务台里每秒都可能被重算一次（轮询），在主界面流式时
//: 每来一段也要算一次 —— 交付步的结论动辄几万字，那就是"交付的时候卡一下"。
//: 降级成 <pre> 只是排版朴素，内容一个字不少，比让界面停住强。
const DT_MD_MAX = 40000;

function dtMd(s) {
  // 和主界面一样过一层净化：<trace> 块 / 裸工具标记不该出现在气泡里
  const text = (typeof sanitizeText === "function")
    ? sanitizeText(s || "") : (s || "");
  if (text.length > DT_MD_MAX) {
    return '<pre class="dt-md-long">' + dtEsc(text) + "</pre>";
  }
  return (typeof mdToHtml === "function") ? mdToHtml(text) : dtEsc(text);
}

//: 消息里能点开文件的路径。两种写法都认：
//:   D:\workspace\demo\temp\md\x.md   （绝对）
//:   temp\md\x.md / src/main/X.kt   （相对会话所属工作区）
//: 中文要显式排除，否则 `D:\a\b.md分析` 会把"分析"一起吃进去。
const DT_FILE_RE = new RegExp(
  "(?:[A-Za-z]:[\\\\/][^\\s\"'<>|，。；：、）】\\u4e00-\\u9fa5]+"
  + "|(?:[\\w.\\-]+[\\\\/])+[\\w.\\-]+\\.(?:md|txt|kt|java|xml|json|py|js|ts|html|css|gradle|properties|ya?ml|log|png|jpe?g))",
  "g"
);

/** 把一段消息里的文件路径变成可点元素。 */
function dtLinkify(root, ws) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const s = node.nodeValue || "";
    if (s.length < 5) continue;
    const parent = node.parentNode;
    if (!parent || parent.tagName === "A"
        || (parent.classList && parent.classList.contains("dt-file"))) continue;
    DT_FILE_RE.lastIndex = 0;
    let frag = null, last = 0, m;
    while ((m = DT_FILE_RE.exec(s))) {
      const prev = m.index > 0 ? s[m.index - 1] : "";
      // 跳过 URL 尾巴（http://x/a.md）和已经带盘符的重复匹配
      if (prev === ":" || prev === "/" || prev === "\\") continue;
      if (!frag) frag = document.createDocumentFragment();
      if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
      // 必须在这里把路径存成常量：`m` 是循环变量，循环一结束它就被
      // 重新赋值成 null（exec 返回 null），闭包里再读就是
      // "Cannot read properties of null (reading '0')"。
      const hit = m[0];
      const a = document.createElement("span");
      a.className = "dt-file";
      a.textContent = hit;
      a.title = "在主窗口编辑器打开";
      a.addEventListener("click", (ev) => {
        ev.stopPropagation();
        dtOpenInMain(hit, ws);
      });
      frag.appendChild(a);
      last = m.index + m[0].length;
    }
    if (frag) {
      if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
      parent.replaceChild(frag, node);
    }
  }
}

//: 发起过的「打开文件」请求：id -> 超时定时器。等主窗口回执用。
const DT_OPEN_WAITS = new Map();
let _dtOpenSeq = 0;

//: 发起过的「在文件树里定位」请求：id -> 回调。和上面那份分开 ——
//: 一个等的是 open-file-ack，一个等的是 reveal-tree-ack，混在一起会把
//: 别人的回执当成自己的。
const DT_REVEAL_WAITS = new Map();
let _dtRevealSeq = 0;

/**
 * 在**主窗口**的编辑器里打开一个文件。
 *
 * 任务台窗口里编辑器是藏着的（CSS 隐藏），在这儿调 openInEditor 只会打开在
 * 一个看不见的容器里。所以走广播让主窗口去开 —— 那才是用户看代码的地方。
 *
 * 两个要点：
 * 1. **先把主窗口提到前台**。任务台是主窗口 window.open 出来的，`opener`
 *    就是它；从用户点击的手势里调 `opener.focus()` 是浏览器允许的，
 *    比让主窗口自己在广播回调里 focus 自己可靠得多。
 * 2. **等回执再报结果**。以前发完广播就无条件弹「已在主窗口打开」，
 *    主窗口其实没开 / 文件不存在时用户只能看到一句假的成功提示。
 */
function dtOpenInMain(path, ws) {
  // 先给一句反馈：点下去到底有没有触发，全靠这一句判断。
  // （没有它就只能看到"什么都没发生"，分不清是没点到还是主窗口没接住。）
  if (typeof toast === "function") toast("正在请主窗口打开 " + baseName(path) + " …");
  if (typeof rtLog === "function") rtLog("board.open", "请主窗口打开", { path: path, ws: ws });
  try {
    if (window.opener && !window.opener.closed) window.opener.focus();
  } catch (e) { /* 跨源/已关闭：忽略，广播照样能到 */ }

  if (typeof BOARD_CH === "undefined" || !BOARD_CH) {
    toast("主窗口没连上，打不开文件", true);
    return;
  }
  const id = "of" + (++_dtOpenSeq);
  const timer = setTimeout(() => {
    DT_OPEN_WAITS.delete(id);
    toast("主窗口没有响应 —— 它多半已经被关掉了", true);
  }, 1500);
  DT_OPEN_WAITS.set(id, timer);
  try {
    BOARD_CH.postMessage({
      type: "open-file", id, path,
      ws: ws || DT.chat.ws || S.workspace || "",
    });
  } catch (e) {
    clearTimeout(timer);
    DT_OPEN_WAITS.delete(id);
    toast("打不开：" + e.message, true);
  }
}

/** 右栏两块详情区二选一（会话 / 终端）。 */
function boardPane(kind) {
  const chat = $("bdChat"), term = $("bdTerm");
  if (chat) chat.classList.toggle("hidden", kind !== "chat");
  if (term) term.classList.toggle("hidden", kind !== "term");
}

//: 任务台窗口的引用。有它才能"已开着就只聚焦" —— 直接 window.open 同一个
//: name 会**重新加载**那个窗口，正在看的会话和刚加进去的附件全丢。
let BOARD_WIN = null;
//: 任务台窗口是否活着。主窗口一刷新 BOARD_WIN 就没了，光靠引用判断会以为
//: 没开过、又去 window.open 一次 —— 表现出来就是"按钮只有打开，点几次开几次"。
//: 所以再加一条心跳：任务台启动/应答时报 hello/pong，关掉时报 bye。
let BOARD_ALIVE = false;

function boardIsOpen() {
  return BOARD_ALIVE || !!(BOARD_WIN && !BOARD_WIN.closed);
}

/**
 * 问一声任务台还在不在（等 pong）。
 *
 * 为什么需要：bye 广播可能没送到（窗口被强杀、浏览器直接关掉上下文），
 * 那样按钮会一直亮着，点"关闭"发出去的 board-close 也没人接 —— 看起来就是
 * "按钮卡死了"。点击时先探一次，超时没回应就当它已经没了。
 *
 * 超时 2000ms（原 600ms）：任务台在跑任务时每秒重铺日志 + 每秒 /api/chat/live，
 * 事件循环被占住 600ms 以上是常态。600ms 会把「正忙」误判成「已关」，
 * 而迟到的 pong 又立刻把状态翻回来 —— 一次抖动应用两次 DOM，表现出来就是
 * 主界面会话栏「展开又马上隐藏」。（详见 md/任务台开着时会话栏闪烁.md）
 */
function boardPing(ms) {
  return new Promise((resolve) => {
    if (typeof BOARD_CH === "undefined" || !BOARD_CH) { resolve(false); return; }
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { BOARD_CH.removeEventListener("message", onMsg); } catch (e) { /* 忽略 */ }
      resolve(v);
    };
    const timer = setTimeout(() => finish(false), ms || 2000);
    const onMsg = (e) => {
      const d = e.data || {};
      if (d.type === "board-pong") finish(true);
    };
    try {
      BOARD_CH.addEventListener("message", onMsg);
      BOARD_CH.postMessage({ type: "board-ping" });
    } catch (e) {
      finish(false);
    }
  });
}

/** 新开一个独立窗口跑任务台（已开着就复用并聚焦）。返回是否拿到了窗口。 */
function boardOpenDetached() {
  if (BOARD_WIN && !BOARD_WIN.closed) {
    try { BOARD_WIN.focus(); return true; } catch (e) { BOARD_WIN = null; }
  }
  if (BOARD_ALIVE) {
    // 窗口是"上次页面"开的：没有引用，让那边自己 focus
    if (BOARD_CH) { try { BOARD_CH.postMessage({ type: "board-focus" }); } catch (e) { /* 忽略 */ } }
    return true;
  }
  const url = location.pathname + "?detached=board";
  const w = window.open(url, "board-detached",
                        "width=1280,height=860,menubar=no,toolbar=no");
  if (!w) {
    toast("浏览器拦住了弹窗 —— 允许本站弹出窗口后再点一次", true);
    return false;
  }
  BOARD_WIN = w;
  BOARD_ALIVE = true;
  _boardDeadByPing = false;      // 新开的窗口，跟旧的判死结论无关
  try { w.focus(); } catch (e) { /* 跨窗口受限，忽略 */ }
  boardSyncButton(true);      // 顶栏按钮亮 + 会话栏立即让位（用户刚点的，别等去抖）
  return true;
}

/**
 * 状态同步的总入口：顶栏按钮亮不亮，以及**主界面会话栏显不显示**。
 *
 * 规则很简单：任务台开着 → 会话都在那边，主界面这一栏让位；任务台关了 →
 * 原版会话界面回来。用户不用记"我刚才把会话栏藏哪了"。
 *
 * **会话栏的显隐默认去抖 600ms**：判活偶尔会抖一下（ping 超时 → 判死；
 * 紧接着迟到的 pong 到 → 又判活），抖一次就白藏白显一次，还要连带
 * boardAfterClose 整块重画会话。
 *
 * `now = true` 表示「确定结论」：bye 广播、窗口引用显示 closed、用户点按钮。
 * 这类信号不会抖，必须立即应用 —— 否则用户点关任务台，会话栏还要愣 600ms
 * 才回来。顶栏按钮本身永远不去抖，它闪一下不刺眼，点击要即时反馈。
 */
let _chatApplied = null;   // 会话栏上一次真正应用的显隐（null = 首帧还没应用过）
let _chatTimer = 0;        // 去抖定时器
//: 上一次「任务台没了」的结论是不是 ping 超时判出来的（而不是 bye / closed 这种确定信号）。
//: 只用来决定「复活」要不要去抖：ping 判死可能误判，迟到的 pong 立刻把会话栏
//: 藏回去就是用户看到的「会话栏展开马上又隐藏」。确定信号判死的才立即应用。
let _boardDeadByPing = false;

function applyChatHidden(on) {
  if (dtOn()) return;
  if (_chatApplied === on) return;    // 状态没变，别白改一次 DOM
  _chatApplied = on;
  const chat = $("chatPanel");
  const rz = $("resizeChat");
  if (chat) chat.classList.toggle("hidden", on);
  if (rz) rz.classList.toggle("hidden", on);
}

function boardSyncButton(now) {
  if (dtOn()) return;              // 任务台窗口里没有主界面那套布局
  const on = boardIsOpen();
  const b = $("boardBtn");
  if (b) {
    b.classList.toggle("on", on);
    b.title = on
      ? "关闭 TASKS（工作空间任务台）"
      : "打开 TASKS（工作空间任务台，新窗口）：所有工作区的会话和终端";
  }
  // 首帧 / 确定结论：立即应用（首帧不限的话启动时会话栏会先显再藏，白闪一下）
  if (_chatApplied === null || now) {
    if (_chatTimer) { clearTimeout(_chatTimer); _chatTimer = 0; }
    applyChatHidden(on);
    return;
  }
  // 已应用值没变 → 顺手取消挂起的定时器（抖动期间又回到原值）
  if (on === _chatApplied) {
    if (_chatTimer) { clearTimeout(_chatTimer); _chatTimer = 0; }
    return;
  }
  if (_chatTimer) return;          // 已经挂着了，等它到点
  _chatTimer = setTimeout(() => {
    _chatTimer = 0;
    applyChatHidden(boardIsOpen());   // 到点再取一次：这 600ms 内可能又变回去了
  }, 600);
}

/** 任务台关掉之后：主界面会话栏回来了，把那边聊出来的内容重新拉一遍。 */
async function boardAfterClose() {
  if (dtOn()) return;
  try {
    const r = await api("/api/workspace");
    S.sessions = (r && r.sessions) || S.sessions;
  } catch (e) { /* 拉不到就沿用旧的 */ }
  if (typeof loadCurrentSession === "function") {
    try { await loadCurrentSession(); } catch (e) { /* 忽略 */ }
  }
  // 关窗口那一刻就去问「后端还有谁在跑」：任务台里那一轮没了连接，但**还在跑**。
  // 不在这儿叫一次，主界面要等自己那 15 秒一轮的状态同步才发现，中间这段时间
  // 用户的观感就是"关掉任务台，任务没了"。叫醒之后 30-chat.js 的接续轮询
  // （liveMirrorTick）会把这个会话正在写的正文铺出来（见那边的注释）。
  if (typeof syncActiveSessions === "function") syncActiveSessions();
}

/**
 * 任务台点了消息里的文件路径：在主窗口编辑器里打开（见 dtOpenInMain）。
 *
 * 消息里给的多数是相对路径（`temp\md\x.md`），按会话所属工作区补全；
 * 已经是盘符开头或 `/` 开头的就原样用。
 */
async function boardOpenFile(path, ws, id) {
  if (dtOn()) return;
  let ok = false;
  let err = "";
  let p = String(path || "").trim();
  try {
    if (!p) throw new Error("路径为空");
    if (!/^[A-Za-z]:[\\/]/.test(p) && p.charAt(0) !== "/") {
      const base = String(ws || S.workspace || "").replace(/[\\/]+$/, "");
      if (base) p = base + "\\" + p.replace(/^[\\/]+/, "");
    }
    // 会话可能在**别的工作区**里 —— 任务台是跨工作区的。主窗口要是还停在
    // 另一个项目上，文件虽然能用绝对路径打开，但左侧树、.ai/ 上下文、插件
    // 面板全都是那个项目的，等于"开了个文件，环境还是错的"。
    // 所以先把这个工作区选中，再谈打开。
    if (ws && dtWsKey(ws) !== dtWsKey(S.workspace)
        && typeof applyWorkspace === "function") {
      try {
        // 切走前把编辑器状态写进**旧**工作区。不用额外等它：applyWorkspace
        // 里已经把这一条和 /api/workspace 并发发出去了（见 20-workspace.js），
        // 这里再 await 一次只会白等一趟往返。脏文件仍然要在这里存 ——
        // 那是"改到一半的内容",不能靠状态快照。
        for (let i = 0; i < EDITORS.length; i++) {
          if (!EDITORS[i].dirty) continue;
          switchEditor(i);
          await doSave();
        }
        await applyWorkspace(ws, { quiet: true });   // quiet：别弹"已切到 xxx"
      } catch (e) {
        // 切不过去也继续往下走：绝对路径照样能开，只是周边视图对不上
        if (typeof rtLog === "function") {
          rtLog("board.open", "切工作区失败", { ws: ws, err: String((e && e.message) || e) });
        }
      }
    }
    // 先把主窗口提到前台：任务台窗口通常盖在上面，不提上来用户会以为没反应。
    // 这里的 window.focus() 等于空操作（广播回调里没有用户手势），真正管用的
    // 是后端那条 Win32 路径（和「⌂」按钮同一条）。
    // 放在 openInEditor **之前**：大文件加载要好几秒，先切窗口别让用户对着
    // 任务台干等 —— 也避免 openInEditor 抛错时窗口没被提起来。
    try { window.focus(); } catch (e) { /* 浏览器可能不让 */ }
    try {
      const fr = await api("/api/app/focus", {});
      if (typeof rtLog === "function") rtLog("board.open", "focus", fr || {});
    } catch (e) {
      if (typeof rtLog === "function") {
        rtLog("board.open", "focus failed", { err: String((e && e.message) || e) });
      }
    }
    // 主窗口可能正停在 logcat 视图上 —— 编辑器面板那时是藏起来的，
    // 光 expandPanel 不够，得先切回文件视图
    if (typeof switchView === "function") {
      try { switchView("files"); } catch (e) { /* 视图切不过去也不该挡着开文件 */ }
    }
    if (typeof openInEditor !== "function") throw new Error("编辑器还没加载");
    await openInEditor(p, baseName(p));
    ok = true;
    // 打开过程本身会让 Chrome 重新分配焦点（渲染、跳行、滚动都会触发），
    // 抢在前面那一次激活经常被它盖掉。等它安静下来再补一次。
    setTimeout(() => {
      Promise.resolve(api("/api/app/focus", {})).catch(() => { /* 忽略 */ });
    }, 400);
  } catch (e) {
    err = (e && e.message) || String(e);
    // 主窗口的 toast 用户可能看不到（被任务台盖着），回执那边也会再提示一次
    toast("打开失败：" + err, true);
  }
  if (typeof BOARD_CH !== "undefined" && BOARD_CH && id) {
    try {
      BOARD_CH.postMessage({ type: "open-file-ack", id, ok, error: err, path: p });
    } catch (e) { /* 忽略 */ }
  }
}

/**
 * 任务台 → 主窗口：在**左侧文件树**里展开并选中一个路径（不打开文件）。
 *
 * 和 boardOpenFile 的区别只有一个：那个是"打开这个文件"，这个是"把树定位到
 * 这个目录"。`.ai/history` 要看的是"里面有哪些 md"，写成打开文件没有意义，
 * 而弹系统资源管理器又会把用户甩出应用（还得自己在别处找回来）。
 *
 * 四步顺序有讲究：
 *   ① 先切工作区 —— 树、`.ai` 都是跟着工作区走的，切晚一步就是在旧的树上找；
 *   ② 打开隐藏文件 —— `.ai` 这类点开头的项默认不在树里（见 toggleTreeHidden）；
 *   ③ 切回「文件」视图并展开左栏 —— 主窗口可能停在 logcat / SVN 上；
 *   ④ 才去展开到那一行。
 */
async function boardRevealInTree(path, ws, id) {
  if (dtOn()) return;
  let ok = false;
  let err = "";
  const p = String(path || "").trim();
  try {
    if (!p) throw new Error("路径为空");
    if (ws && dtWsKey(ws) !== dtWsKey(S.workspace)
        && typeof applyWorkspace === "function") {
      try {
        // 脏文件先落盘（内容优先）；状态快照由 applyWorkspace 内部与
        // /api/workspace 并发写出，这里不再多等一趟（见 20-workspace.js）。
        if (typeof prepareEditorSwitch === "function") {
          try { await prepareEditorSwitch(); } catch (e) { /* 存盘失败也继续切 */ }
        }
        await applyWorkspace(ws, { quiet: true });
      } catch (e) {
        // 切不过去也往下走：路径是绝对路径，树上的根还在就有机会定位到
        if (typeof rtLog === "function") {
          rtLog("board.reveal", "切工作区失败", { ws: ws, err: String((e && e.message) || e) });
        }
      }
    }
    // 提到前台：任务台通常盖在上面，不提上来用户会以为"点了没反应"。
    // 广播回调里 window.focus() 是空操作，真正管用的是后端那条 Win32 路径。
    try { window.focus(); } catch (e) { /* 浏览器可能不让 */ }
    try { await api("/api/app/focus", {}); } catch (e) { /* 提不上来也照样定位 */ }
    if (!S.showHidden) {
      S.showHidden = true;
      if (typeof paintTreeHidden === "function") paintTreeHidden();
      // 记住这个选择：不然下次刷新 `.ai` 又不见了，用户会以为定位坏了
      try { await api("/api/state", { showHidden: true }); } catch (e) { /* 记不住不算事 */ }
    }
    if (typeof switchView === "function") {
      try { switchView("files"); } catch (e) { /* 视图切不过去也不该挡着定位 */ }
    }
    if (typeof expandPanel === "function") expandPanel("files");
    if (typeof treeRevealFile !== "function") throw new Error("文件树还没加载");
    ok = await treeRevealFile(p, true);        // true = 目标是目录
    if (!ok) err = "树里找不到这个目录（不在工作区内，或已经被删了）";
  } catch (e) {
    err = (e && e.message) || String(e);
  }
  if (typeof BOARD_CH !== "undefined" && BOARD_CH && id) {
    try {
      BOARD_CH.postMessage({ type: "reveal-tree-ack", id, ok, error: err, path: p });
    } catch (e) { /* 忽略 */ }
  }
}

/**
 * 按钮的开关语义：开着就关，关着就开。
 *
 * 关之前先问一句，但**只拦终端命令**：任务台窗口一关，它里面正在跑的命令会
 * 随着连接断开被后端杀掉（`_client_gone`），这不是"最小化"—— 窗口没了就没
 * 人可以按 Ctrl+C。
 *
 * 聊天那一轮**不受影响**：连接断开不等于取消（见 web_agent.py 里
 * `should_cancel` 的注释 —— 断连只是不再往这条死连接写事件，后端照旧跑完
 * 并写进会话）。所以这里不再把两者合起来数、也不再说「会把它们中断」：
 * 那句话让用户以为关窗口会把聊天弄丢，实际上那一轮一直在跑，主界面接到
 * 状态之后会接着显示（`boardAfterClose` + 主界面的接续轮询
 * `liveMirrorTick`，见 30-chat.js）。
 *
 * 「开着但在后台」怎么办：点击仍然是关（用户明确要的语义），另有一条
 * `boardRaiseDetached()` 挂在右键 / 长按上，只把窗口提到前台不动状态。
 */
async function boardToggleDetached() {
  if (dtOn()) return;                     // 任务台窗口自己：按钮是隐藏的
  // 有心跳但没引用（主窗口刷新过）：先探一次，别把按钮卡在"已开"上
  if (BOARD_ALIVE && !(BOARD_WIN && !BOARD_WIN.closed)) {
    BOARD_ALIVE = await boardPing(2000);
    if (!BOARD_ALIVE) _boardDeadByPing = true;   // 这次是 ping 判的，复活时据此去抖
    boardSyncButton(true);      // 用户点的按钮：不管结论如何都立即反映
  }
  if (boardIsOpen()) {
    // 终端任务和聊天轮次**分开数**，因为它们的结局不一样：
    //   · 终端命令跑在这个窗口的连接上，断了就被杀 —— 必须拦一下；
    //   · 聊天那一轮后端会自己跑完并落盘（见函数注释），关窗口没有损失，
    //     拿它去弹确认框只会让人以为"关掉就白跑了"。
    let termBusy = 0;
    try {
      const j = await api("/api/term/jobs");
      termBusy = ((j && j.jobs) || []).filter((x) => x.running).length;
    } catch (e) { /* 查不到就当没有 */ }
    let chatBusy = 0;
    try {
      const a = await api("/api/chat/active");
      chatBusy = ((a && a.active) || []).length;
    } catch (e) { /* 同上 */ }
    if (termBusy && !confirm("还有 " + termBusy + " 个终端命令在跑，关闭任务台会把它们中断。确定关闭？")) {
      return;
    }
    // 只有聊天在跑：不拦，说清楚"关掉也不影响"就放行。
    // 为什么不再弹确认框：它拦的不是损失，而是一件本来就能随便做的事。
    if (!termBusy && chatBusy && typeof toast === "function") {
      toast("还有 " + chatBusy + " 个会话在后台生成，关掉不影响；主界面会接着显示");
    }
    // 有引用就自己关；刷新过没引用就让那边（收到 board-close）自己关
    if (BOARD_WIN && !BOARD_WIN.closed) {
      try { BOARD_WIN.close(); } catch (e) { /* 已经关了 */ }
    }
    if (BOARD_CH) { try { BOARD_CH.postMessage({ type: "board-close" }); } catch (e) { /* 忽略 */ } }
    BOARD_WIN = null;
    BOARD_ALIVE = false;
    _boardDeadByPing = false;   // 用户主动关的，是确定信号
    boardSyncButton(true);  // 主界面会话栏立刻回来（确定关闭，不去抖）
    boardAfterClose();      // 任务台里聊出来的内容拉一遍
    return;
  }
  boardOpenDetached();
  boardSyncButton(true);
}

/**
 * 只把任务台窗口提到前台，不改变开关状态（主界面顶栏按钮的右键 / 长按）。
 *
 * 为什么不直接用 BOARD_WIN.focus()：Chrome 会拒绝对"用户当前没在操作的
 * 窗口"抢前台 —— 调用成功、窗口纹丝不动，表现就是点了没反应。真正生效的
 * 是后端那条 Win32 路径（_activate_app_window(board=True)），它借前台窗口
 * 的输入队列资格去 SetForegroundWindow。
 *
 * 任务台没开着就什么都不做：这个入口的语义是"把已经在跑的拉到眼前"，
 * 不是"顺手开一个"（开窗口是左键点击的事）。
 */
async function boardRaiseDetached() {
  if (dtOn()) return;                  // 任务台窗口里没有这个按钮
  // 有心跳没引用（主窗口刷新过）：先探一次，别对着一扇已经关掉的窗较劲
  if (BOARD_ALIVE && !(BOARD_WIN && !BOARD_WIN.closed)) {
    BOARD_ALIVE = await boardPing(2000);
    if (!BOARD_ALIVE) _boardDeadByPing = true;   // 这次是 ping 判的，复活时据此去抖
    boardSyncButton(true);      // 用户点的按钮：不管结论如何都立即反映
  }
  if (!boardIsOpen()) {
    toast("任务台没开着 —— 左键点一下可以打开它");
    return;
  }
  // 先试同源那两条：任务台能自己 focus 的时候最快，也不会闪
  if (BOARD_WIN && !BOARD_WIN.closed) {
    try { BOARD_WIN.focus(); } catch (e) { /* 跨窗口受限，忽略 */ }
  }
  if (BOARD_CH) {
    try { BOARD_CH.postMessage({ type: "board-focus" }); } catch (e) { /* 忽略 */ }
  }
  // 兜底（通常也是唯一真正管用的）：后端 Win32 提窗口
  let ok = false;
  try {
    const r = await api("/api/app/focus", { board: true });
    ok = !!(r && r.ok);
  } catch (e) {
    ok = false;
  }
  if (!ok) toast("任务台在后台，请点一下任务栏", true);
}

/**
 * 顶栏按钮的三种点法。
 *
 * · 左键（点击）→ 开关：开着就关，关着就开（boardToggleDetached）
 * · 右键         → 只提到前台，不动状态
 * · 长按 550ms   → 同右键（触屏 / 触控板没有右键可用）
 *
 * 长按要在 550ms 内没抬手才算；抬起前先 cancel，避免"长按完又触发一次
 * click 顺手把窗口关了"——那是最难受的失败模式。
 */
function boardBindButton() {
  const b = $("boardBtn");
  if (!b || b.dataset.bound === "1") return;
  b.dataset.bound = "1";
  b.addEventListener("click", (e) => {
    // 长按后紧跟的那次 click 要吞掉（e.detail 判断不了，用标记位）
    if (b.dataset.longpressed === "1") {
      b.dataset.longpressed = "";
      return;
    }
    boardToggleDetached();
  });
  b.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    boardRaiseDetached();
  });
  let timer = null;
  const cancel = () => {
    if (timer) { clearInterval(timer); timer = null; }
    b.dataset.longpressed = "";
  };
  b.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;        // 右键走 contextmenu
    cancel();
    const t0 = Date.now();
    timer = setInterval(() => {
      if (Date.now() - t0 >= 550) {
        cancel();
        b.dataset.longpressed = "1";   // 吃掉随后的 click
        boardRaiseDetached();
      }
    }, 60);
  });
  b.addEventListener("pointerup", cancel);
  b.addEventListener("pointerleave", cancel);
  b.addEventListener("pointercancel", cancel);
}

boardBindButton();

// 主窗口这边兼听心跳：任务台启动 / 应答 ping 就认为它活着，关掉时报 bye。
// 这条通路不依赖窗口引用，所以主窗口刷新之后按钮状态照样是准的。
if (typeof BOARD_CH !== "undefined" && BOARD_CH && !dtOn()) {
  BOARD_CH.onmessage = (e) => {
    const d = e.data || {};
    if (d.type === "board-hello" || d.type === "board-pong") {
      // 从「ping 判死」翻回活着 = 复活，交给去抖：单次超时不代表真关了，
      // 立刻把会话栏藏回去就是用户看到的「展开马上隐藏」。
      // 其余情况（启动后的首个 hello/pong、重复 pong、bye 之后重开）都立即应用，
      // 否则刷新后会话栏会先显示、再等 600ms 才消失，也像闪一下。
      const revive = !BOARD_ALIVE && _boardDeadByPing;
      _boardDeadByPing = false;
      BOARD_ALIVE = true;
      boardSyncButton(!revive);
    } else if (d.type === "board-bye") {
      BOARD_ALIVE = false;
      BOARD_WIN = null;
      _boardDeadByPing = false;
      boardSyncButton(true);      // 会话栏跟着回来（确定关闭，不去抖）
      boardAfterClose();          // 任务台那边聊的新内容拉一遍
    } else if (d.type === "open-file") {
      // 任务台点了消息里的文件路径 → 在本窗口编辑器打开，并回执成败
      boardOpenFile(d.path, d.ws, d.id);
    } else if (d.type === "reveal-tree") {
      // 任务台点了「md 目录」→ 在本窗口文件树里定位那个目录（不打开文件）
      boardRevealInTree(d.path, d.ws, d.id);
    } else if (d.type === "model-changed") {
      // 任务台切了模型。模型是**全局**的，本窗口顶栏那个下拉还停在旧值上，
      // 用户会以为没生效。直接改内存状态重画，不重新请求（对面已经请求过了）。
      S.model = d.model || S.model;
      S.endpoint = d.endpoint || "";
      if (typeof renderModels === "function") renderModels();
      if (typeof dtRenderModel === "function") dtRenderModel();
    }
  };
  // 主窗口可能是刷新后加载的：问一声任务台还在不在（在的话它回 pong，
  // boardSyncButton 会把会话栏让出去）
  try { BOARD_CH.postMessage({ type: "board-ping" }); } catch (e) { /* 忽略 */ }
  boardSyncButton();
}

// 兜底同步：用户直接点任务台的 ✕ / 按 Esc 关掉时，bye 广播可能晚到或没到。
// 有引用时看 closed；没引用（主窗口刷新过）就隔几拍 ping 一次，别让按钮和
// 会话栏一直停在"任务台还开着"的状态。
//
// 判死要**连续 2 次** ping 没回应：任务台在跑任务时每秒重铺日志 + 每秒
// /api/chat/live，单次超时很可能只是它正忙着，不是关了。间隔也从 6 秒放宽到
// 12 秒 —— 正常关闭有 bye 广播，ping 只兜"窗口被强杀、bye 没送到"这一种。
let _boardTick = 0;
let _boardMiss = 0;          // 连续 ping 失败次数
setInterval(() => {
  if (dtOn()) return;
  // 有引用且已经 closed：这是确定关闭（bye 可能没送到），不等去抖、不等 ping
  if (BOARD_WIN && BOARD_WIN.closed) {
    BOARD_WIN = null;
    BOARD_ALIVE = false;
    _boardMiss = 0;
    _boardDeadByPing = false;      // 确定信号，不是误判
    boardSyncButton(true);
    boardAfterClose();
  }
  _boardTick += 1;
  if (BOARD_ALIVE && !BOARD_WIN && _boardTick % 8 === 0) {
    boardPing(2000).then((alive) => {
      if (alive) { _boardMiss = 0; return; }
      _boardMiss += 1;
      if (_boardMiss < 2 || !BOARD_ALIVE) return;
      _boardMiss = 0;
      BOARD_ALIVE = false;
      _boardDeadByPing = true;     // 记下「这次是 ping 判的」，复活时据此去抖
      // 只翻转状态：会话栏的显隐交给 boardSyncButton 去抖 600ms，
      // 迟到 pong 能在去抖窗口内把状态翻回来，不会闪。
      // boardAfterClose() 也**不放这里** —— 误判时它会白重画一次会话
      // （用户看到的就是"会话栏展开"）。确定关闭的三条路径各自调它。
      boardSyncButton();
    });
  }
  boardSyncButton();
}, 1500);
window.addEventListener("focus", () => { if (!dtOn()) boardSyncButton(); });

/**
 * 主窗口的「加入对话」往哪儿走：**任务台开着才转过去**，否则留在本地会话栏。
 *
 * 为什么加 boardIsOpen() 判断：任务台开着时，主界面的会话栏被藏起来了
 * （boardSyncButton 把 chatPanel 加 hidden），附件塞进 S.attachments 用户
 * 看不见，所以必须转到任务台。但任务台**没开**的时候正好相反 —— 会话栏
 * 是显示着的，这时候还硬转过去，用户右键一下就多弹出一个任务台窗口，
 * 附件跑到那边的另一个会话里，主界面反而什么都没有。
 *
 * 判据必须和 boardSyncButton 的显隐规则一致（都看 boardIsOpen），否则
 * 就会出现"附件去了一个看不见的地方、看得见的那个地方没有附件"。
 *
 * 返回 true = 已经转走，调用方（30-chat 的 add*Attachment）不要再本地加。
 */
function boardRouteAttach(item) {
  if (!item) return false;
  if (dtOn()) return false;          // 自己就在任务台窗口里：正常加本地
  // 任务台没开 → 主界面会话栏是显示着的，走本地（S.attachments + renderAttachments）
  if (!boardIsOpen()) return false;
  boardSendAttach(item);             // 广播 + 队列兜底（见 00-core.js）
  const opened = boardOpenDetached();   // 已经开着会复用并聚焦
  const label = item.name || (item.path ? baseName(item.path) : "附件");
  // 窗口被拦时不额外报"已加入"：附件已经在队列里，下次开窗口会出现
  if (opened) toast("已加入任务台：" + label);
  return true;
}

/**
 * 一个路径属于哪个工作区（拿已打开会话的 ws 前缀去比，取最长命中的那个根）。
 *
 * 为什么要它：用户从 gu 的文件树右键「添加到对话框」，而任务台里正选着 jv 的
 * 会话 —— 附件确实加进去了，但发出去时模型拿到的是 **jv 的上下文**（工作区记忆、
 * .ai、相对路径的基准全是 jv 的），那些绝对路径对了、周边环境全错。
 * 所以顺着附件把对话切到它自己那个工作区（见 dtAddAttach）。
 */
function dtWsOfPath(path) {
  const p = dtWsKey(path);
  if (!p) return "";
  let best = "";
  for (const r of DT.items || []) {
    if (r.kind !== "chat" || !r.ws) continue;
    const k = dtWsKey(r.ws);
    if (!k) continue;
    if (p === k || p.indexOf(k + "\\") === 0) {
      if (k.length > dtWsKey(best).length) best = r.ws;
    }
  }
  return best;
}

/** 任务台收到附件：去重后塞进本窗口的 S.attachments 并刷新 chip。 */
function dtAddAttach(items) {
  const list = Array.isArray(items) ? items : [items];
  let added = false;
  for (const it of list) {
    if (!it) continue;
    const id = it._id || (it.kind + "|" + (it.path || it.name || ""));
    if (DT.seen.has(id)) continue;
    DT.seen.add(id);
    const copy = Object.assign({}, it);
    delete copy._id;
    S.attachments.push(copy);
    added = true;
  }
  if (!added) return;
  dtRenderAttn();

  // 附件来自别的工作区 → 先把对话切过去（见 dtWsOfPath 的注释）。
  // 优先切到那个工作区的**第一个会话**（列表顺序 = 最近活跃在前）。
  const firstPath = list.map((x) => (x && x.path) || "").find(Boolean) || "";
  const fromWs = dtWsOfPath(firstPath);
  const onChat = DT.key.startsWith("chat|");
  if (fromWs && dtWsKey(DT.chat.ws) !== dtWsKey(fromWs)) {
    const want = DT.items.find((r) => r.kind === "chat"
      && dtWsKey(r.ws) === dtWsKey(fromWs));
    if (want) {
      dtSelect(want).then(() => {
        const ip = $("dtInput");
        if (ip) ip.focus();
      }).catch(() => { /* 切不过去就当没切，附件还在 */ });
      if (typeof toast === "function") {
        toast("已切到「" + wsLabel(fromWs) + "」的会话："
              + (want.name || want.id));
      }
      return;
    }
  }

  // 没选中会话就自动选一个：附件加进来了总得有地方发
  if (!onChat) {
    const first = DT.items.find((r) => r.kind === "chat");
    if (first) dtSelect(first);
  } else {
    const ip = $("dtInput");
    if (ip) ip.focus();
  }
}

/** 把队列里攒着的（窗口没开时加的）一次性取走。 */
function dtTakeAttachAll() {
  const q = boardTakeAttach();
  if (q.length) dtAddAttach(q);
}

// ---- 智能体（模式）----
// 清单来自 /api/modes（见 10-login.js 的 loadAgents），这里只把它画成一个下拉。
// 选择存在 DT.chat.agent，发送时带给后端，后端负责写回会话文件。

/**
 * 会话用的模式（`{id,name,icon}`），用来在左列表里一眼看出"这行是什么模式"。
 *
 * 只读 S.agents（/api/modes 的清单），**不写死任何模式名** —— 加一个模式
 * 就是往 src/modes/ 丢一个 json，这里跟着变。
 * id 空串 = 会话没存过模式，按后端的语义落到默认模式（S.agentDefault），
 * 所以老会话显示的是 🧭 标准而不是空白。
 * 清单还没拿到（/api/modes 失败）时退回写死的 💬，至少不空一格。
 */
function dtModeOf(id) {
  const list = (typeof S !== "undefined" && S.agents) || [];
  if (!list.length) return { id: "", name: "", icon: "💬" };
  const want = id
    || (typeof modeDefault === "function" ? modeDefault() : "")
    || (typeof S.agentDefault === "string" ? S.agentDefault : "");
  const m = want ? list.find((x) => x.id === want) : null;
  return m || list[0] || { id: "", name: "", icon: "💬" };
}

function dtRenderAgent() {
  const sel = $("dtAgent");
  if (!sel) return;
  const list = (typeof S !== "undefined" && S.agents) || [];
  if (!list.length) {
    // 没拿到清单就藏起来，别留一个空格子：发送时后端会用会话里的 / 默认模式
    sel.innerHTML = "";
    sel.classList.add("hidden");
    return;
  }
  sel.classList.remove("hidden");
  // 会话里存的优先；老会话（空串）落到默认模式
  const cur = DT.chat.agent
    || (typeof modeDefault === "function" ? modeDefault() : "");
  sel.innerHTML = "";
  for (const m of list) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = (m.icon ? m.icon + " " : "") + m.name;
    o.title = m.desc || "";
    o.selected = (m.id === cur);
    sel.appendChild(o);
  }
  DT.chat.agent = sel.value || cur;
}

// ---- 模型（含自定义网关）----
// 任务台窗口里没有顶栏（见 style.css 的 body.detached-board .topbar），主界面
// 那个 #modelSel 是看不见的，所以这里单独画一个。取值和主界面**完全一致**：
// `端点id::模型名`，默认网关是 `::模型名`（见 10-login.js 的 modelKey）。
// 模型是全局偏好（.ai/ui.json），不跟会话走 —— 两个下拉动的是同一个设置。
function dtRenderModel() {
  const sel = $("dtModel");
  if (!sel) return;
  const eps = (typeof S !== "undefined" && S.endpoints) || [];
  const models = (typeof S !== "undefined" && S.models) || [];
  const curModel = (typeof S !== "undefined" && S.model) || "";
  const curEp = (typeof S !== "undefined" && S.endpoint) || "";
  const cur = (typeof modelKey === "function")
    ? modelKey(curEp, curModel) : (curEp + "::" + curModel);

  sel.innerHTML = "";
  const addGroup = (label, items) => {
    if (!items.length) return;
    const g = document.createElement("optgroup");
    g.label = label;
    for (const it of items) {
      const o = document.createElement("option");
      o.value = it.key;
      o.textContent = it.model;
      if (it.key === cur) o.selected = true;
      g.appendChild(o);
    }
    sel.appendChild(g);
  };
  const std = models.length ? models : (curModel && !curEp ? [curModel] : []);
  addGroup("默认网关", std.map((m) => ({ key: "::" + m, model: m })));
  for (const ep of eps) {
    addGroup(ep.name || ep.id,
      (ep.models || []).map((m) => ({ key: ep.id + "::" + m, model: m })));
  }
  if (!sel.options.length) {
    // 一个都配不出来（配置里 models 空、也没端点）：藏起来，别留个空格子
    sel.classList.add("hidden");
    return;
  }
  sel.classList.remove("hidden");
  // 当前模型不在任何一组里（改过配置、模型被删）：补一个占位项，否则
  // select 会自动选第一项，界面显示的模型和实际在用的悄悄对不上。
  if (!Array.from(sel.options).some((o) => o.value === cur)) {
    const o = document.createElement("option");
    o.value = cur;
    o.textContent = (curModel || "（未配置）") + "（当前）";
    o.selected = true;
    sel.appendChild(o);
  }
}

// ---------------------------------------------------------------------------
// 任务台聊天输入框的高度：拖动 composer 顶边那条线调整，双击复位，记在
// save.json（dtInputH）。行为对齐终端高度那套（90-tools.js 的 applyTermHeight）。
// ---------------------------------------------------------------------------
let dtInputH = 0;      // 0 = 没拖过 / 复位过，用 CSS 默认（rows=2 的自然高度）
let dtInputBase = 0;   // 两行时的自然高度，当拖动的下限

/**
 * 应用输入框高度。
 * 上下限都夹住：低于两行会把字挤没，高于半个窗口则日志区别想看了
 * （主窗口的 .composer 是反过来压上面那个的，两处互不影响）。
 */
function dtApplyInputH(px) {
  const el = $("dtInput");
  if (!el) return 0;
  const lo = dtInputBase || 34;
  const hi = Math.max(lo, Math.round(window.innerHeight * 0.5));
  dtInputH = Math.max(lo, Math.min(Math.round(px) || 0, hi));
  el.style.height = dtInputH + "px";
  return dtInputH;
}

function dtBindInputResize() {
  const bar = $("dtInputResize");
  const el = $("dtInput");
  if (!bar || !el) return;
  let startY = 0, startH = 0, dragging = false, moved = false;

  bar.addEventListener("mousedown", (e) => {
    dragging = true;
    moved = false;
    startY = e.clientY;
    startH = el.getBoundingClientRect().height;
    bar.classList.add("dragging");
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";   // 一路划过去别把整屏文字选蓝
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const log = $("dtLog");
    // 贴底判定要在改高度**之前**取：输入框一高、日志区就矮，之后
    // scrollHeight - scrollTop - clientHeight 立刻变成「没贴底」，
    // 再判就永远不跟随了（同款坑见 md/选择题卡片重复绘制滚动.md）。
    const wasBottom = dtNearBottom(log);
    dtApplyInputH(startH + (startY - e.clientY));   // 往上拖 = 变高
    moved = true;
    if (wasBottom && log) {
      log.scrollTop = log.scrollHeight;
      if (typeof dtPaintDown === "function") dtPaintDown();
    }
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    // 点一下没拖：不落盘，免得把上次拖出来的存档顺手清成 0
    if (!moved) return;
    // 拖回两行高度等于「没改」，存 0 让语义和复位一致
    const val = dtInputH === dtInputBase ? 0 : dtInputH;
    api("/api/state", { dtInputH: val }).catch(() => {});
  });

  // 双击复位：去掉内联高度回到 CSS 默认，存档一起清掉
  // （只清内存不清存档的话，下次刷新又弹回拖过的高度，看着像"复位没生效"）
  bar.addEventListener("dblclick", () => {
    el.style.height = "";
    dtInputH = 0;
    api("/api/state", { dtInputH: 0 }).catch(() => {});
    const log = $("dtLog");
    if (log) {
      log.scrollTop = log.scrollHeight;
      if (typeof dtPaintDown === "function") dtPaintDown();
    }
  });
}

// ---------------------------------------------------------------------------
// 右栏骨架：只建一次
// ---------------------------------------------------------------------------
function dtBuild() {
  if (DT.built) return;
  const chat = $("bdChat");
  const term = $("bdTerm");
  if (!chat || !term) return;

  chat.innerHTML = ""
    // 流程进度条：任务台看不到主界面 SSE 的内部事件，只能靠轮询 /api/chat/live
 // 拿 stage/stages。放在日志区**上方**而不是塞进 dtLog：dtLog 每轮重铺
 // （reset 时 textContent=""），进度条跟着重铺会闪，而且滚走就看不见了。
 + '<div class="dt-stage hidden" id="dtStage"></div>'
 + '<div class="dt-log" id="dtLog"></div>'
    // 思考框的停靠位：思考期间那一块挪到这里，钉在输入框上方不跟日志滚；
    // 思考一结束就归位回这一轮（见 dtPaintChat / 主界面同款）。
 + '<div class="think-dock dt-think-dock hidden" id="dtThinkDock"></div>'
    // 往上翻之后新内容还在下面涨，得给一个明确的"回到底部"入口。
    // 行为对齐 Logcat 工具栏那个 ⇣：在底部时隐藏，翻上去就露出来。
    + '<button class="dt-down hidden" id="dtDown"'
    + ' title="跳到最新（恢复自动跟随）">↓</button>'
    + '<div class="dt-composer">'
    // 拖动条：压在这条 composer 的顶边上（跨满宽、8px 高），往上拖 = 输入框变高。
    // **没给 textarea 开原生 `resize: vertical`** —— 那个手柄画在右下角，正好被
    // 下面 .dt-tools 那行压住，等于看不见；而且终端高度（90-tools.js 的
    // applyTermHeight）也是「自绘条 + 双击复位」，两处行为对齐着用不容易记错。
    + '  <div class="dt-input-resize" id="dtInputResize"'
    + '       title="拖动调整输入框高度（双击复位）"></div>'
    + '  <div class="dt-attn hidden" id="dtAttn"></div>'
    + '  <div class="dt-queue hidden" id="dtQueue"></div>'
    + '  <div class="dt-inputrow">'
    // 两个下拉**上下叠成一列**（.dt-selcol），这一列再和输入框同一排：
    // 独立窗口横向也不宽，两个下拉并排会把输入框挤成一条缝。
    // 用下拉而不是胶囊 —— 这一列放不下三个胶囊（主界面用胶囊，见 .modebar）。
    + '    <div class="dt-selcol">'
    + '      <select id="dtAgent" class="dt-agentsel" title="智能体：决定提示词、可用工具和流程。对应文件 src/modes/*.json"></select>'
    // 模型下拉：任务台是独立窗口，`body.detached-board` 把 .topbar 整个藏掉了，
    // 主界面那个 #modelSel 在这里根本看不见 —— 所以这一排要自带一个。
    // 选项按网关分组（默认网关 + 每个自定义端点），value 是 `端点id::模型名`，
    // 切的时候连 base_url 一起换（见 20-workspace.js 的 applyModelKey）。
    + '      <select id="dtModel" class="dt-agentsel dt-modelsel" title="模型：全局生效，主界面顶栏那个下拉和它是同一个设置"></select>'
    + '    </div>'
    + '    <textarea id="dtInput" rows="2" placeholder="继续这个会话…（Enter 发送，Shift+Enter 换行；可直接粘贴截图或拖文件进来）"></textarea>'
    + '    <div class="dt-sendcol">'
    + '      <div class="sendcol-top">'
    + '        <button class="tbtn mini" id="dtCompact" title="压缩上下文：早期对话摘要化（只影响这个会话）">压缩</button>'
    + '        <label class="sendchk" id="dtNewSessWrap" title="勾上后，点发送会先新建一个会话并把这条消息发进去（发完自动取消勾选）">'
    + '          <input type="checkbox" id="dtNewSessChk"><span>新会话</span>'
    + '        </label>'
    + '      </div>'
    + '      <button class="tbtn" id="dtSend">发送</button>'
    + '    </div>'
    + '  </div>'
    + '  <div class="dt-tools">'
    + '    <button class="dt-ico" id="dtFile" title="加入文件（上传到工作区后引用）">📎</button>'
    + '    <button class="dt-ico" id="dtDir" title="加入目录（只把路径给模型）">📁</button>'
    + '    <span class="dt-tip" id="dtTip">可粘贴截图 / 拖文件到这里</span>'
    + '  </div>'
    + "</div>";

  /* 终端输入是 <textarea rows=1>：粘贴多行时换行保得住（input 的 value 不含换行，
     浏览器会把几行命令压成一条超长单行）。高度交给 autoGrowBox，见 00-core.js。 */
  term.innerHTML = ""
    + '<pre class="dt-out" id="dtOut"></pre>'
    + '<div class="dt-composer">'
    + '  <div class="dt-inputrow">'
    + '    <textarea id="dtCmd" rows="1" spellcheck="false"'
    + '              placeholder="回车执行 · Shift+回车换行 · Tab 补全 · ↑↓ 历史"></textarea>'
    + '    <button class="tbtn" id="dtRun">执行</button>'
    + '  </div>'
    + "</div>";

  $("dtSend").addEventListener("click", () => dtSendChat());
  // 输出区滚动：只在这里算一次「用户是不是贴底」。
  // 每帧现读 out.scrollHeight 会强制同步重排 —— 20 万字符的 pre 一次几十毫秒，
  // 输出快的时候这个成本比画内容本身还高（见 dtTrackBottom / dtPaintPending）。
  $("dtOut").addEventListener("scroll", dtTrackBottom, { passive: true });
  $("dtInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      dtSendChat();
    }
  });
  $("dtCompact").addEventListener("click", () => dtCompact());
  // 智能体下拉：只改本窗口这一轮的选择，不发请求（下一轮发送时后端落盘）
  const dtAgentSel = $("dtAgent");
  if (dtAgentSel) {
    dtAgentSel.addEventListener("change", () => {
      // 只改本窗口这一轮的选择，不发请求（下一轮发送时后端落盘）
      DT.chat.agent = dtAgentSel.value;
    });
  }
  dtRenderAgent();
  // 模型下拉：切的是**全局**模型（和主界面顶栏同一个设置），所以这里要发请求，
  // 不能像智能体那样只改本窗口状态。实际请求统一走 applyModelKey（20-workspace.js），
  // 两个窗口的 toast / 跨窗口同步才一致。
  dtRenderModel();
  const dtModelSel = $("dtModel");
  if (dtModelSel) {
    dtModelSel.addEventListener("change", async () => {
      if (typeof applyModelKey === "function") await applyModelKey(dtModelSel.value);
    });
  }
  // 新会话是**勾选框**：勾上后点发送 = 先建会话再发（见 dtSendChat）。
  // 这里只管视觉状态；建会话的工作区优先取当前会话的归属，退活动根。
  (() => {
    for (const [id, wrapId] of [["dtNewSessChk", "dtNewSessWrap"]]) {
      const chk = $(id), wrap = $(wrapId);
      if (!chk || !wrap) continue;
      wrap.classList.toggle("on", chk.checked);
      chk.addEventListener("change", () => wrap.classList.toggle("on", chk.checked));
    }
  })();
  // 「跳到最新」：翻上去看历史时不再被自动拽回（dtPaintChat 的 stick 判断），
  // 这条按钮负责把人送回去并恢复跟随。
  const dtLogEl = $("dtLog");
  const dtDownBtn = $("dtDown");
  if (dtLogEl) dtLogEl.addEventListener("scroll", dtPaintDown);
  if (dtDownBtn) {
    dtDownBtn.addEventListener("click", () => {
      if (dtLogEl) dtLogEl.scrollTop = dtLogEl.scrollHeight;
      dtPaintDown();
      const ip = $("dtInput");
      if (ip) ip.focus();
    });
  }
  $("dtFile").addEventListener("click", () => dtPickFiles());
  // 目录没法用文件选择器（浏览器不给绝对路径），只能手输/粘
  $("dtDir").addEventListener("click", () => {
    const p = prompt("加入目录（只把路径给模型，让它自己按需读）：",
                     S.workspace || DT.chat.ws || "");
    if (!p || !p.trim()) return;
    const path = p.trim();
    if (typeof addDirAttachment === "function") {
      addDirAttachment(path, baseName(path));
      dtRenderAttn();
    }
  });
  // 粘贴截图：和主界面输入框同一套（剪贴板里的图没路径，只能 base64 内联）
  $("dtInput").addEventListener("paste", (e) => dtOnPaste(e));
  // 拖文件进来：上传一份到工作区，再按路径引用
  chat.addEventListener("dragover", (e) => { e.preventDefault(); });
  chat.addEventListener("drop", (e) => {
    e.preventDefault();
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) dtUploadFiles(files);
  });

  $("dtRun").addEventListener("click", () => dtRunCmd());
  const dtCmdEl = $("dtCmd");
  // 多行粘贴后跟着长高；不绑这个的话 textarea 停在 rows=1 的高度，几行命令
  // 挤在一条缝里，看得见换行却看不见后半截。
  if (dtCmdEl) dtCmdEl.addEventListener("input", () => autoGrowBox(dtCmdEl));
  dtCmdEl.addEventListener("keydown", (e) => {
    if (e.isComposing) return;                    // 输入法选词中，别抢 Enter
    // 回车执行、Shift+回车换行 —— 和聊天输入框反过来（那边 Enter 发送、
    // Shift+Enter 换行也是这个规矩），终端这边多一条：粘贴多行后要能逐行改。
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      dtRunCmd();
      return;
    }
    // Tab 补全：常驻 shell 没有 TTY，cmd.exe 自己的补全不会触发，只能前端补
    if (e.key === "Tab") { e.preventDefault(); dtComplete(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); dtHistStep(-1); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); dtHistStep(1); return; }
    if ((e.ctrlKey || e.metaKey) && (e.key === "l" || e.key === "L")) {
      e.preventDefault();
      const out = $("dtOut");
      if (out) out.textContent = "";
      DT.term.sgr = null;
      DT.term.atBottom = true;         // 清屏之后又贴底了，别让旧状态挡住自动跟随
      return;
    }
    // Ctrl+C / Esc 单键直通（nano / vim / less 在前台时的唯一出路）。
    // 只在输入框**为空**时接管：有内容时 Ctrl+C/V 是用户自己的复制粘贴，
    // 有选中文字时更是，抢了就等于把剪贴板弄坏。Esc 同理（有半截命令时
    // 按 Esc 可能是想放弃这一行，先保留浏览器默认，别抢）。
    if (!e.composing) {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === "c" || e.key === "C")) {
        if (dtCmdEl.selectionStart !== dtCmdEl.selectionEnd) return;
        if (!dtCmdEl.value) {
          e.preventDefault();
          dtRawKey("\x03");
          return;
        }
      }
      if (e.key === "Escape" && !dtCmdEl.value) {
        e.preventDefault();
        dtRawKey("\x1b");
        return;
      }
    }
  });

  // 输入框高度：先量下两行的自然高度当拖动下限，再恢复上次拖出来的值。
  // 顺序不能反 —— dtApplyInputH 拿 dtInputBase 夹下限，先恢复会把下限当成 0；
  // 而 `#dtInput` 只有 dtBuild 跑过才存在，所以这个恢复点只能在这里，不能在
  // 10-login.js 读 save.json 的地方应用（那边只把值登记进 S.dtInputH）。
  dtInputBase = $("dtInput").offsetHeight;
  const savedInputH = (typeof S !== "undefined" && S.dtInputH) || 0;
  if (savedInputH) dtApplyInputH(savedInputH);
  dtBindInputResize();

  // 「跳到最新」是浮在输入区上方的绝对定位按钮，bottom 以前写死 104px。
  // 输入区高度会变（拖高、附件 chip 行 / 排队行出现或消失），写死迟早压进
  // 输入框里 —— 用 ResizeObserver 把实测高度喂给 #bdChat 上的 CSS 变量
  // （style.css 的 `#bdChat > .dt-down { bottom: calc(var(--dt-cm-h) + 8px) }`）。
  // 选 ResizeObserver 是为了同时覆盖「拖动」和「附件行自己冒出来」两种来源，
  // 否则每个改变高度的调用点都得记着补一次。
  const cm = chat.querySelector(":scope > .dt-composer");
  if (cm && typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      chat.style.setProperty("--dt-cm-h", Math.round(cm.getBoundingClientRect().height) + "px");
    });
    ro.observe(cm);
  }

  DT.built = true;
}

// ---- 附件（与主界面共用 S.attachments，但这个窗口有自己的一份） ----
function dtRenderAttn() {
  const box = $("dtAttn");
  if (!box) return;
  box.innerHTML = "";
  if (!S.attachments.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  S.attachments.forEach((a, i) => {
    const d = document.createElement("div");
    d.className = "chip";
    if (a.kind === "image" && a.dataUrl) {
      const img = document.createElement("img");
      img.className = "chip-img";
      img.src = a.dataUrl;
      d.appendChild(img);
    }
    const s = document.createElement("span");
    s.textContent = (typeof attachLabel === "function")
      ? attachLabel(a) : (a.name || a.path || "");
    d.appendChild(s);
    const x = document.createElement("button");
    x.textContent = "✕";
    x.title = "移除";
    x.addEventListener("click", () => {
      S.attachments.splice(i, 1);
      dtRenderAttn();
    });
    d.appendChild(x);
    box.appendChild(d);
  });
}

function dtOnPaste(e) {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  const files = [];
  for (const it of items) {
    if (it.kind === "file" && (it.type || "").startsWith("image/")) {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return;
  e.preventDefault();
  const stamp = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const tag = `${pad(stamp.getHours())}${pad(stamp.getMinutes())}${pad(stamp.getSeconds())}`;
  (async () => {
    for (let i = 0; i < files.length; i++) {
      try {
        const dataUrl = await fileToScaledDataUrl(files[i]);
        addImageAttachment(dataUrl, `截图-${tag}${files.length > 1 ? "-" + (i + 1) : ""}.jpg`);
      } catch (err) {
        toast("图片处理失败：" + err.message, true);
      }
    }
    dtRenderAttn();
  })();
}

/** 选文件 → 上传到当前会话的工作区 → 按路径加入对话。 */
function dtPickFiles() {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.multiple = true;
  inp.addEventListener("change", () => {
    if (inp.files && inp.files.length) dtUploadFiles(Array.from(inp.files));
  });
  inp.click();
}

async function dtUploadFiles(files) {
  const ws = (DT.chat.ws || S.workspace || "").replace(/[\\/]+$/, "");
  if (!ws) { toast("先选一个工作区", true); return; }
  for (const f of files) {
    // 用 `/` 拼：后端 os.path 在 Windows 上也认，Linux 上照样对
    const target = ws + "/" + f.name;
    try {
      const r = await fetch("/api/fs/upload?path=" + encodeURIComponent(target) + "&overwrite=1",
        { method: "POST",
          headers: { "Authorization": "Bearer " + S.token,
                     "Content-Type": "application/octet-stream" },
          body: f });
      if (!r.ok) {
        const t = await r.text().catch(() => "");
        throw new Error(t || ("HTTP " + r.status));
      }
      addAttachment(target, f.name);
    } catch (e) {
      toast("上传失败 " + f.name + "：" + e.message, true);
    }
  }
  dtRenderAttn();
}

function dtUsage(u) {
  const el = $("dtCompact");
  if (!el || !u) return;
  const pct = u.context_tokens
    ? Math.min(100, Math.round((u.estimated_tokens || 0) / u.context_tokens * 100)) : 0;
  el.textContent = "压缩 " + pct + "%";
  el.title = "当前上下文约 " + (u.estimated_tokens || 0) + " tokens"
    + (u.context_tokens ? " / 上限 " + u.context_tokens : "");
}

async function dtCompact() {
  if (!DT.chat.sid) return;
  if (!confirm("立即压缩上下文？早期对话会被摘要化。")) return;
  const btn = $("dtCompact");
  const oldText = btn ? btn.textContent : "";
  // 摘要请求本身就要几十秒：按钮得看得出来在干活，否则用户会重复点，
  // 每点一次后端就再摘要一遍（force=True），白烧 token。
  if (btn) { btn.disabled = true; btn.textContent = "压缩中…"; }
  try {
    // timeout：这个接口同步等模型摘要（最多分 12 块，一块一次上游请求），
    // 默认 15s 的 api 超时必然先 abort（报「压缩失败：请求超时（15s）」），
    // 后端却还在压、压完照样写回历史。
    const r = await api("/api/session/compact",
      { sid: DT.chat.sid, ws: DT.chat.ws }, { ws: DT.chat.ws, timeout: 600000 });
    if (r.compacted) {
      toast("已压缩");
      await dtLoadHistory();
      dtPaintChat(true);
    } else {
      toast(r.message || "没有可压缩的内容");
    }
    if (r.usage) dtUsage(r.usage);
  } catch (e) {
    toast("压缩失败：" + e.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      if (btn.textContent === "压缩中…") btn.textContent = oldText;
    }
  }
}

// ---------------------------------------------------------------------------
// 左栏列表
// ---------------------------------------------------------------------------
/**
 * 任务台左列表要的数据一次拿全（`GET /api/board/state`）。
 *
 * 以前这里是 `1 (ssh/workspace) + 1 (chat/active) + N (每个工作区一个
 * sessions) + 1 (term/jobs)` 个请求：一秒一轮，而服务端是 HTTP/1.0、没有
 * keep-alive，**每轮十几个新 TCP**。跑几个会话之后本机 TIME_WAIT 堆到一千
 * 多（实测 1707 个 TimeWait），界面越用越涩 —— 这就是"任务台好卡"的主因，
 * 不是刷新丢了东西。合并成一个请求之后这层开销基本消失。
 *
 * 兜底：后端还没重启（页面先刷新了）时新路由不存在，这时退回逐个问 ——
 * 慢，但列表不会变成空的。
 */
async function dtFetchBoardState(trees) {
  try {
    const qs = trees.map((w) => "ws=" + encodeURIComponent(w)).join("&");
    const st = await api("/api/board/state" + (qs ? "?" + qs : ""));
    if (st && st.sessions) return st;
  } catch (e) { /* 老服务端：走下面的兜底 */ }

  const out = { workspaces: trees.slice(), remotes: [], sessions: {},
                active: [], asks: [], jobs: [], now: Date.now() };
  // 远程工作区（ssh://）：会话/记忆存在**本地** .ai/remote/ 下，命令跑在远端。
  // 它们不在文件树列表里，得单独问一次。
  try {
    const r = await api("/api/ssh/workspace");
    out.remotes = ((r && r.workspaces) || []).map((x) => x.ws);
  } catch (e) { /* 拿不到就当没有远程工作区 */ }
  const all = trees.concat(out.remotes.filter((w) => trees.indexOf(w) < 0));
  // 在途会话 / 待答选择题：后端给的是**全局**快照
  try {
    const a = await api("/api/chat/active");
    out.active = (a && a.active) || [];
    out.asks = (a && a.asks) || [];
  } catch (e) { /* 拿不到就当没有在跑的 */ }
  try {
    const j = await api("/api/term/jobs");
    out.jobs = (j && j.jobs) || [];
  } catch (e) { /* 同上 */ }
  const lists = await Promise.all(all.map((ws) =>
    api("/api/sessions", undefined, { ws })
      .then((r) => ({ ws, r }))
      .catch(() => ({ ws, r: null }))));
  for (const { ws, r } of lists) {
    if (!r) continue;
    out.sessions[ws] = r.sessions || [];
  }
  return out;
}

async function dtLoadItems() {
  const trees = (S.trees && S.trees.length) ? S.trees.slice() : [S.workspace].filter(Boolean);
  const st = await dtFetchBoardState(trees);
  DT.remotes = (st.remotes || []).slice();
  const all = trees.concat(DT.remotes.filter((w) => trees.indexOf(w) < 0));
  const items = [];

  // 在途会话：按 (ws|sid) 索引，给列表标「运行中 + 耗时」
  // 待答的选择题：同样按 (ws|sid) 索引，给那一行标「!」
  // ★ 待答项必须是**全局**的（后端给的是全部会话的快照）。只问当前会话的话，
  //   用户坐在 gu 里就永远看不到 web 那边在等他答题 —— 那一轮只能等到超时。
  const active = {};
  const asks = {};
  for (const x of (st.active || [])) {
    active[dtWsKey(x.ws) + "|" + x.sid] = x;
  }
  for (const q of (st.asks || [])) {
    // ws 为空的老服务端 / 映射缺失：跳过，别把标记标到别的项目头上
    // （理由同 syncActiveSessions：sid 按工作区各自编号，猜不得）
    if (!q || !q.sid || !q.ws) continue;
    asks[dtWsKey(q.ws) + "|" + q.sid] = q;
  }

  for (const ws of all) {
    // 拿不到（老服务端没返回这个工作区 / 路径刚被删）就当这个工作区没会话，
    // 不影响别的组
    for (const s of (st.sessions[ws] || [])) {
      const k = dtWsKey(ws) + "|" + s.id;
      const act = active[k];
      items.push({
        kind: "chat", ws: ws || "", id: s.id,
        name: s.title || s.id,
        note: (s.messages || 0) + " 条",
        // 会话级的模式 id（空 = 默认模式）。后端 list_meta 给 lite 模式也带这个
        // 字段，所以任务台那一份数据里就有，不用另外问一次 /api/session。
        agent: s.agent || "",
        running: !!act,
        t0: act ? (Date.now() - Math.round((act.secs || 0) * 1000)) : 0,
        // 这一行里模型正在等我答题（可能是**别的**工作区的会话）
        ask: !!asks[k],
      });
    }
  }

  for (const t of (st.jobs || [])) {
    // 常驻 shell 的进程一直活着（running 恒 true），要看 busy：此刻有没有
    // 命令在跑。**一次性命令反过来** —— 只认 running：老服务端的 busy 在
    // 命令结束后没复位，一律优先 busy 的话，列表里会永远挂着一个
    // 「运行中 xx 秒」、点停止也没反应。
    const busy = t.shell
      ? ((t.busy === undefined) ? !!t.running : !!t.busy)
      : !!t.running;
    items.push({
      kind: "term", ws: t.ws || "", id: t.id,
      // 常驻终端不是"一条命令"，别把内部名 shell 亮给用户
      name: t.shell ? "终端（常驻）" : (t.cmd || t.id),
      note: t.cwd ? baseName(t.cwd) : "",
      // 一次性命令的原文（右键「重新运行」直接重跑它）
      cmd: t.cmd || "",
      cwd: t.cwd || "",
      // 常驻终端最后跑过的那条命令（服务端记的），右键「重新运行」用
      shell: !!t.shell,
      lastCmd: t.last_cmd || "",
      running: busy,
      t0: busy ? t.t0 : 0,
      code: t.code,
    });
  }

  DT.items = items;
  //: 已知的全部工作区（本地 + 远程）。列表按 items 分组渲染，但**分组头不能
  //: 只由 items 推导** —— 一个工作区把会话全删光之后就一条 item 都没有，
  //: 组头跟着消失，右键都没地方点（见 dtPaintList 的 keys）。
  DT.workspaces = all;
  dtPaintList();
}

function dtPaintList() {
  const box = $("bdList");
  if (!box) return;
  const rows = DT.items;
  // 签名里**带上工作区清单**：空工作区（一条会话都没有）也要出组头，
  // 只按行算签名的话，新增一个空空间 / 删光最后一个会话时不会重画，
  // 组头就永远不出现（"这个空间没了"）。
  // 签名里带上 ask 位：待答题状态变了（问了 / 答了 / 超时）也要重画，
  // 否则「!」只能等下一次别的变化撞上来才出现。
  // 签名里带上 agent：切了模式（emoji 变了）得跟着重画，不然图标停在上一个模式。
  // 模式值以会话文件为准，所以真正变化发生在"发送之后"的下一次轮询。
  const sig = rows.map((r) => [r.kind, r.ws, r.id, r.name,
    r.running ? 1 : 0, r.ask ? 1 : 0, r.agent || ""].join("~")).join("|")
    + "#" + (DT.workspaces || []).join(",")
    + "#" + (DT.remotes || []).join(",");
  DT.nodes = DT.nodes || new Map();

  if (sig !== DT.sig) {
    DT.sig = sig;
    DT.nodes = new Map();
    box.textContent = "";

    const groups = new Map();
    for (const r of rows) {
      const ws = r.ws || "";
      if (!groups.has(ws)) groups.set(ws, []);
      groups.get(ws).push(r);
    }
    // 纯按路径排：任务台里没有"当前工作区"这回事 —— 每个会话和终端都带着
    // 自己的归属，点谁就是谁，不存在"在这边干活得先切过去"。所以也不需要
    // 把某个项目顶到最前面，位置稳定反而好找。
    const keys = Array.from(new Set([
      ...groups.keys(),
      // 工作区即使**一条会话/终端都没有**也要出组头。否则把某个项目的会话
      // 删光之后，整组连同组头一起消失 —— 界面上就像"这个空间没了"，
      // 右键也找不到落脚点（只能重启才回来）。本地和远程一视同仁。
      ...(DT.workspaces || []),
      ...(DT.remotes || []),
    ])).sort((a, b) => {
      if (!a) return 1;
      if (!b) return -1;
      return a.localeCompare(b);
    });

    if (!keys.length) {
      const e = document.createElement("div");
      e.className = "bd-empty";
      e.textContent = "还没有会话或终端任务。\n"
        + "在主窗口里开会话、跑命令，这里会自动出现。\n"
        + "也可以用上面的 ＋ 先加一个工作区（本地目录 / 远程 SSH）。";
      const btns = document.createElement("div");
      btns.className = "bd-empty-btns";
      const add = document.createElement("button");
      add.className = "tbtn mini";
      add.textContent = "＋ 添加工作区";
      add.addEventListener("click", (ev) => {
        ev.stopPropagation();
        dtAddMenu(ev.currentTarget);
      });
      btns.appendChild(add);
      e.appendChild(btns);
      box.appendChild(e);
    }

    for (const ws of keys) {
      const head = document.createElement("div");
      head.className = "bd-group-head";
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = ws ? wsLabel(ws) : "未归属";
      nm.title = ws || "";
      head.appendChild(nm);
      box.appendChild(head);

      const rows = groups.get(ws) || [];
      if (!rows.length) {
        // 空工作区（多半是刚加的远程空间）：给一行入口，不然这儿什么都没有
        const add = document.createElement("div");
        add.className = "bd-item bd-item-add";
        add.textContent = "＋ 新建会话";
        add.title = "在 " + (ws || "当前工作区") + " 新建一个会话（右键可新建终端）";
        // 带上归属字段：右键菜单靠 dataset.ws 判断"对哪个工作区操作"，
        // 空组里只有这一行，没有它右键就只能落到当前活动根上。
        add.dataset.kind = "ws";
        add.dataset.id = "";
        add.dataset.ws = ws || "";
        add.addEventListener("click", () => dtNewSession(ws));
        box.appendChild(add);
        continue;
      }
      for (const r of rows) {
        const key = dtKey(r.kind, r.ws, r.id);
        const it = document.createElement("div");
        it.className = "bd-item" + (r.running ? " run" : "") + (key === DT.key ? " on" : "");
        it.title = (r.kind === "chat" ? "会话" : "终端") + (r.note ? "\n" + r.note : "");
        if (r.kind === "chat") {
          const mo = dtModeOf(r.agent);
          if (mo.name) it.title += "\n模式：" + mo.icon + " " + mo.name;
        }
        // 右键菜单靠这三个字段判断"对谁操作"
        it.dataset.kind = r.kind;
        it.dataset.id = r.id;
        it.dataset.ws = r.ws || "";

        // 「!」= 这个会话里模型在等我答题。放在 💬 之前（行首），一眼就能看出
        // 该点哪一行 —— 用户可能正坐在**别的**会话里，不标出来他就只能干等到超时。
        if (r.ask) {
          const ask = document.createElement("span");
          ask.className = "bd-ask";
          ask.textContent = "!";
          ask.title = "模型在等你答题，点进这个会话去选";
          it.appendChild(ask);
        }

        const ico = document.createElement("span");
        ico.className = "bd-ico";
        if (r.kind === "chat") {
          // 会话行画**模式**图标（⚡自由 / 💬聊天 / 🧭标准 / 🎨设计 / 🐞调试 …），
          // 一眼就能看出这个会话在什么模式下跑。终端行还是 🖥。
          const mo = dtModeOf(r.agent);
          ico.className = "bd-ico bd-mode";
          ico.textContent = mo.icon || "💬";
          ico.title = mo.name || "";
        } else {
          ico.textContent = "🖥";
        }
        it.appendChild(ico);

        const nmEl = document.createElement("span");
        nmEl.className = "bd-name";
        nmEl.textContent = r.name;
        it.appendChild(nmEl);

        const tm = document.createElement("span");
        tm.className = "bd-time";
        it.appendChild(tm);

        // 正在跑的任务：最右边给个停止键，不用先点进去再找标题栏那个
        if (r.running) {
          const st = document.createElement("button");
          st.className = "bd-stop";
          st.textContent = "■";
          st.title = "停止这个" + (r.kind === "chat" ? "会话的生成" : "命令");
          st.addEventListener("click", (ev) => {
            ev.stopPropagation();       // 别顺带把这一项选中
            dtStopItem(r);
          });
          it.appendChild(st);
        }

        it.addEventListener("click", () => dtSelect(r));
        box.appendChild(it);
        DT.nodes.set(key, { el: it, timeEl: tm });
      }
    }
  }

  for (const r of rows) {
    const n = DT.nodes.get(dtKey(r.kind, r.ws, r.id));
    if (!n) continue;
    const txt = r.running ? fmtDur(Date.now() - (r.t0 || Date.now())) : "";
    // 值没变就别写：给 textContent 赋值会换掉一个 CharacterData 子节点、
    // 让这一行布局失效。空闲时全列表都是空串，以前每秒对每一行都写一遍，
    // 等于每秒把整个左列表做一次无效的 DOM 改动。
    if (n.timeEl._last === txt) continue;
    n.timeEl._last = txt;
    n.timeEl.textContent = txt;
  }
}

function dtMark() {
  if (!DT.nodes) return;
  for (const [k, n] of DT.nodes) n.el.classList.toggle("on", k === DT.key);
}

// ---------------------------------------------------------------------------
// 右键菜单：新增工作区 / 会话 / 终端
//
// 复用主界面那个 #ctx 浮层（样式、点外面关闭都是现成的）。hideCtx 来自
// 40-tree.js，比本文件后加载，但只在点击时才调用，那时早就就绪了。
// ---------------------------------------------------------------------------
function dtMenu(x, y, items) {
  const menu = $("ctx");
  if (!menu) return;
  menu.innerHTML = "";
  for (const it of items) {
    if (it.sep) {
      menu.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
      continue;
    }
    const b = document.createElement("button");
    b.textContent = it.label;
    if (it.danger) b.style.color = "var(--danger)";
    b.addEventListener("click", (ev) => { ev.stopPropagation(); hideCtx(); it.fn(); });
    menu.appendChild(b);
  }
  menu.classList.remove("hidden");
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

/** 添加一个工作区（只加进列表，不切走当前这个）。 */
function dtAddWorkspace() {
  if (typeof pickDirectory !== "function") {
    toast("当前版本没有目录选择器", true);
    return;
  }
  pickDirectory({
    title: "添加工作区",
    hint: "加进来之后它的会话和终端任务就会出现在左边列表里。",
    label: "加进来",
    start: S.workspace || "",
    onPick: async (path) => {
      try {
        await api("/api/workspace/add", { path });
        const r = await api("/api/workspace");
        S.trees = (r && r.trees) || S.trees;
        S.labels = (r && r.labels) || S.labels || {};
        S.workspace = (r && r.workspace) || S.workspace;
      } catch (e) {
        toast("添加失败：" + e.message, true);
      }
      DT.sig = "";
      await dtLoadItems();
    },
  });
}

/** 在指定工作区新建会话并选中它。 */
async function dtNewSession(ws) {
  ws = ws || "";
  if (!ws) { toast("先右键某个工作区组头，再新建", true); return; }
  try {
    // ws 同时放 body 和 query：后端 /api/sessions/new 走 _resolver(query)，
    // 只认 query 里的 ws —— 只放 body 会被当成"没带"，建到活动根上去。
    const r = await api("/api/sessions/new", { ws }, { ws });
    DT.sig = "";
    await dtLoadItems();
    const gotWs = (r && r.ws) || ws;
    // 优先按 (ws, id) 找；路径写法不一致时退回只按 id 找，别让人以为没建出来
    let item = DT.items.find((x) => dtKey(x.kind, x.ws, x.id) === dtKey("chat", gotWs, r.id));
    if (!item) item = DT.items.find((x) => x.kind === "chat" && x.id === r.id);
    if (item) await dtSelect(item);
    else { DT.key = dtKey("chat", gotWs, r.id); dtMark(); boardPane("chat"); }
    // 后端真把它建到别处了就说清楚，别让人对着列表猜
    if (gotWs && ws && gotWs.toLowerCase() !== ws.toLowerCase()) {
      toast("会话建在 " + wsLabel(gotWs) + "（" + gotWs + "）", true);
    }
  } catch (e) {
    toast("新建会话失败：" + e.message, true);
  }
}

/**
 * 停止列表里某一项（会话的一轮生成 / 终端的一条命令）。
 *
 * 走的是后端那两条明路：/api/chat/stop 立停止标记（不看网络）、
 * /api/term/kill 停终端。后者分两种：一次性命令置停止位（run_command 的
 * stop_when 读到就杀进程树）；常驻 shell 则整棵树 taskkill（管道模式没法
 * 给它送 Ctrl+C），所以那条终端会消失、下一条命令自动开新的。
 * 都不依赖发起连接还活着，所以任务台这边点得动主窗口那边跑的东西。
 */
async function dtStopItem(item) {
  if (!item || !item.running) return;
  try {
    if (item.kind === "chat") {
      const r = await api("/api/chat/stop", { sid: item.id, ws: item.ws });
      // 后端会把「这一轮到底在不在跑」如实带回来（_INFLIGHT 里有没有这个
      // 会话）。以前不管 busy 真假都报「已请求停止」，用户点完界面毫无变化，
      // 分不清是"根本没找到那一轮"还是"正在中断、再等等"。
      if (r && r.busy === false) {
        toast("没找到正在跑的那一轮（可能已经结束）", true);
      } else {
        toast("已请求停止，正在中断当前工具…");
      }
    } else {
      const r = await api("/api/term/kill", { job: item.id });
      // 常驻终端是**整棵树被杀掉**（cmd.exe 里的命令没法单独中断），
      // 所以这条 terminal 本身没了 —— 本地状态要跟着清，否则下一条命令
      // 会被写进一条已经死掉的 shell（表现就是"点发送没反应"）。
      if (r && r.shell) {
        if (DT.term.job === item.id) {
          dtStopTail();             // 这条终端没了，常驻流也该断
          DT.term.job = "";
          DT.term.off = 0;
          DT.term.running = false;
          DT.term.prevCwd = "";
        }
        toast("已停止，终端已重启（cd / 环境变量会丢）");
      } else {
        toast("已请求停止");
      }
    }
  } catch (e) {
    toast("停止失败：" + e.message, true);
  }
  DT.sig = "";
  await dtLoadItems();
  dtHead();
}

/**
 * 把一条终端从列表里摘掉（右键「关闭终端 / 从列表移除」）。
 *
 * 常驻终端由后端连进程树一起 taskkill；一次性命令只置停止位后摘记录 ——
 * 直接杀进程会让 run_command 那边的退出码和收尾全错（见 _handle_term_drop）。
 */
async function dtDropTerm(item) {
  if (!item) return;
  try {
    const r = await api("/api/term/drop", { job: item.id });
    // 正开着的就是这一条：本地引用必须一起清，否则下一条命令会写进一个
    // 已经不在的 shell（表现就是"点发送没反应"）。
    if (DT.term.job === item.id) {
      dtStopTail();               // 这条终端没了，常驻流也该断
      DT.term.job = "";
      DT.term.off = 0;
      DT.term.running = false;
      DT.term.prevCwd = "";
      const out = $("dtOut");
      if (out) out.textContent = "";
      DT.term.sgr = null;
      DT.term.atBottom = true;
    }
    // 选中项没了：清掉 DT.key，dtHead 会退回「终端 · 新命令」那一屏，
    // 面板不切走 —— 用户多半是想在这儿接着敲下一条命令
    if (DT.key === dtKey("term", item.ws, item.id)) DT.key = "";
    DT.sig = "";
    await dtLoadItems();
    dtHead();
    toast(r && r.stopping ? "已停止并移除" : "已移除");
  } catch (e) {
    toast("移除失败：" + e.message, true);
  }
}

/**
 * 确保有一个常驻 shell 接着用。
 *
 * 任务台终端现在是**一个常驻 cmd.exe**：cd、set、环境变量、连续命令都留在
 * 同一个进程里（以前每条命令新起一个进程，cd 完下一条又回原地）。
 * 输出仍然走 /api/term/tail 轮询，和一次性命令共用同一套增量协议。
 */
async function dtEnsureShell(ws, cwd) {
  if (DT.term.job) return DT.term.job;
  const targetWs = ws || DT.term.ws || S.workspace || "";
  const targetCwd = cwd || DT.term.cwd || targetWs;
  try {
    const r = await api("/api/term/shell", { ws: targetWs, cwd: targetCwd });
    DT.term.job = r.id;
    DT.term.ws = targetWs;
    DT.term.cwd = r.cwd || targetCwd;
    DT.term.off = 0;
    // 新 job 一定清掉 gone 那条留下的退避闸，否则「终端没了 → 用户敲下一条命令
    // 开了新 shell」之后，常驻流要干等 30s 不连，第一条命令的输出全看不见。
    DT.term.retryAt = 0;
    // 刚开出来的 shell 是**空闲**的（进程活着 ≠ 有命令在跑）。
    // 写成 true 的话，标题栏一开新终端就显示"运行中 0 秒"、停止键还亮着。
    DT.term.running = false;
    DT.ownJob = "";                 // 输出靠 tail，不走 SSE
    DT.key = dtKey("term", targetWs, r.id);
    await dtLoadItems();
    dtMark();
    dtHead();
    return r.id;
  } catch (e) {
    toast("开终端失败：" + e.message, true);
    return "";
  }
}

/**
 * 添加一个远程工作区（ssh://user@host:port/path）。
 *
 * 会话、记忆、save.json 全部落在**本地** `.ai/remote/<hash>`；远程只负责
 * 跑命令和读写文件 —— 它可能是只读的编译容器，状态散过去换个容器就全丢。
 */
/**
 * 「添加远程 SSH 工作区」对话框。
 *
 * `onAdded(r)` 是可选回调：任务台那边加完要刷新左边的会话/终端列表，
 * 主窗口那边加完要把它塞进顶部标签栏并切过去 —— 同一套表单，两种收尾。
 */
/**
 * 远程路径选择器（**悬浮树**，贴着「浏览…」按钮展开）。
 *
 * 之前是个全屏对话框、点一层进一层：看完子目录回不到兄弟目录，想比较两个
 * 分支就得关掉重开，而且它会把底下的 SSH 表单顶掉。
 *
 * 现在照 Windows 的"选文件夹"来：多层同时可见、点箭头展开、点行选中，
 * 表单还留在旁边。节点是**懒加载**的（展开哪层才请求哪层），所以打开很快。
 *
 * opts: {
 *   conn: {host, port, user, password, key},   // 表单里当前填的，没保存也能连
 *   mode: "dir" | "file",                      // file = 选文件（比如 rg 那个可执行）
 *   start: 起始路径（空 = 登录用户家目录；file 模式下是文件路径，取它所在目录）,
 *   anchor: 定位用的按钮元素,
 *   onPick: (remotePath) => void,
 * }
 */
function pickRemoteTree(opts) {
  const mode = opts.mode === "file" ? "file" : "dir";
  const conn = () => Object.assign({}, opts.conn || {});

  const panel = document.createElement("div");
  panel.className = "card";
  panel.style.cssText = "position:fixed;z-index:90;display:flex;flex-direction:column;"
    + "width:min(430px,92vw);max-height:min(72vh,560px);padding:10px;"
    + "border:1px solid var(--border);box-shadow:0 12px 36px rgba(0,0,0,.45)";

  // ---- 头部：标题 + 关闭 ----
  const head = document.createElement("div");
  head.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:8px";
  const title = document.createElement("div");
  title.style.cssText = "flex:1;font-size:13px;font-weight:600";
  title.textContent = opts.title || (mode === "file" ? "选择远程文件" : "选择远程目录");
  const xBtn = document.createElement("button");
  xBtn.className = "tbtn mini";
  xBtn.textContent = "✕";
  xBtn.title = "关闭";
  head.appendChild(title);
  head.appendChild(xBtn);

  // ---- 路径行：可直接粘贴 ----
  const pathRow = document.createElement("div");
  pathRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px";
  const crumb = document.createElement("input");
  crumb.type = "text";
  crumb.className = "crumb";
  crumb.spellcheck = false;
  crumb.autocomplete = "off";
  crumb.placeholder = "远程路径（可粘贴），回车跳过去";
  crumb.style.cssText = "flex:1;min-width:0;box-sizing:border-box;font-size:12px;"
    + "font-family:var(--mono);padding:4px 8px";
  const go = document.createElement("button");
  go.className = "tbtn mini";
  go.textContent = "去";
  pathRow.appendChild(crumb);
  pathRow.appendChild(go);

  // ---- 树 ----
  const tree = document.createElement("div");
  tree.style.cssText = "flex:1;min-height:120px;overflow:auto;"
    + "border:1px solid var(--border);border-radius:6px;padding:3px";

  // ---- 底部 ----
  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:8px";
  const pickedEl = document.createElement("span");
  pickedEl.style.cssText = "flex:1;min-width:0;font-size:12px;color:var(--muted);"
    + "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
  const okBtn = document.createElement("button");
  okBtn.className = "tbtn";
  okBtn.textContent = mode === "file" ? "用这个文件" : "用这个目录";
  foot.appendChild(pickedEl);
  foot.appendChild(okBtn);

  panel.appendChild(head);
  panel.appendChild(pathRow);
  panel.appendChild(tree);
  panel.appendChild(foot);

  // ---- 状态 ----
  const cache = new Map();       // path -> 接口返回（同一个目录只连一次）
  let picked = null;             // 当前选中项 {name, path, is_dir}
  let rootPath = "";             // 树根（当前浏览到的目录）

  const note = (text, color) => {
    const d = document.createElement("div");
    d.style.cssText = "font-size:12px;padding:6px 6px;white-space:pre-wrap"
      + (color ? ";color:" + color : ";color:var(--muted)");
    d.textContent = text;
    return d;
  };

  async function listOf(path) {
    const key = path || "";
    if (cache.has(key)) return cache.get(key);
    const r = await api("/api/ssh/browse",
      Object.assign(conn(), { path: key }), { timeout: 25000 });
    if (!r || r.ok === false) throw new Error((r && r.error) || "打不开");
    cache.set(key, r);
    return r;
  }

  function highlight(path) {
    for (const el of tree.querySelectorAll("[data-p]")) {
      el.style.background = el.getAttribute("data-p") === path
        ? "rgba(127,127,127,.18)" : "";
    }
  }

  function select(item) {
    picked = item;
    crumb.value = item.path;
    pickedEl.textContent = item.path;
    pickedEl.title = item.path;
    // 目录模式下根目录本身也能选（"就用这里"）；文件模式必须点中文件
    okBtn.disabled = item.is_dir && mode === "file";
    okBtn.style.opacity = okBtn.disabled ? "0.45" : "";
    highlight(item.path);
  }

  /** 往 parent 里加一行（目录带展开箭头，展开时懒加载子目录）。 */
  function addNode(parent, item, depth) {
    const row = document.createElement("div");
    row.className = "node";
    row.setAttribute("data-p", item.path);
    row.style.cssText = "display:flex;align-items:center;gap:6px;"
      + "padding:3px 6px 3px " + (6 + depth * 14) + "px;"
      + "cursor:pointer;border-radius:4px;font-size:12px";

    const arrow = document.createElement("span");
    arrow.style.cssText = "width:12px;flex:0 0 12px;text-align:center;"
      + "color:var(--muted)";
    arrow.textContent = item.is_dir ? "▸" : "";
    const ico = document.createElement("span");
    ico.textContent = item.is_dir ? "📁" : "📄";
    const nm = document.createElement("span");
    nm.textContent = item.name;
    nm.style.cssText = "flex:1;min-width:0;overflow:hidden;"
      + "text-overflow:ellipsis;white-space:nowrap";

    row.appendChild(arrow);
    row.appendChild(ico);
    row.appendChild(nm);
    parent.appendChild(row);

    const kids = document.createElement("div");
    kids.style.cssText = "display:none";
    parent.appendChild(kids);

    let open = false;
    let loaded = false;
    async function toggle() {
      if (!item.is_dir) return;
      open = !open;
      arrow.textContent = open ? "▾" : "▸";
      kids.style.display = open ? "block" : "none";
      if (!open || loaded) return;
      loaded = true;
      kids.textContent = "";
      kids.appendChild(note("读取中…"));
      try {
        const r = await listOf(item.path);
        kids.textContent = "";
        const list = (r.entries || []).filter(
          (e) => e.is_dir || mode === "file");
        if (!list.length) {
          kids.appendChild(note("（空）"));
          return;
        }
        for (const en of list) addNode(kids, en, depth + 1);
      } catch (e) {
        kids.textContent = "";
        kids.appendChild(note("打不开：" + (e.message || e),
                             "var(--danger)"));
        loaded = false;
      }
    }

    arrow.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(); });
    row.addEventListener("click", () => {
      select(item);
      // 点目录顺手展开一层，省一次点击（再点箭头可以收起来）
      if (item.is_dir && !open) toggle();
    });
  }

  /** 重新从某个目录开始画树（根目录本身也算"可选"）。 */
  async function renderRoot(path) {
    tree.textContent = "";
    tree.appendChild(note("读取中…"));
    let r;
    try {
      r = await listOf(path || "");
    } catch (e) {
      tree.textContent = "";
      tree.appendChild(note("打不开：" + (e.message || e), "var(--danger)"));
      return;
    }
    rootPath = r.path || "";
    tree.textContent = "";

    if (r.parent) {
      const up = document.createElement("div");
      up.style.cssText = "font-size:12px;padding:3px 6px;cursor:pointer;"
        + "color:var(--muted)";
      up.textContent = "⬆ ..";
      up.addEventListener("click", () => renderRoot(r.parent));
      tree.appendChild(up);
    }
    const list = (r.entries || []).filter((e) => e.is_dir || mode === "file");
    if (!list.length) {
      tree.appendChild(note(mode === "file" ? "这里没有目录或文件" : "这里没有子目录"));
    } else {
      for (const en of list) addNode(tree, en, 0);
    }
    // 根目录默认选中：很多时候点进来就是为了"就用这里"
    select({ name: rootPath || "/", path: rootPath, is_dir: true });
    crumb.value = rootPath;
  }

  // ---- 交互 ----
  function close() {
    panel.remove();
    document.removeEventListener("click", onDoc, true);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", close);
  }
  function onDoc(ev) {
    if (panel.contains(ev.target)) return;
    if (opts.anchor && opts.anchor.contains(ev.target)) return;
    close();
  }
  function onKey(ev) {
    if (ev.key !== "Escape") return;
    // 全局 Esc 会 closeModal()，那会把底下的 SSH 设置窗一起关掉
    ev.stopPropagation();
    close();
  }
  xBtn.addEventListener("click", close);
  go.addEventListener("click", () => renderRoot(cleanPath(crumb.value)));
  crumb.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    renderRoot(cleanPath(crumb.value));
  });
  okBtn.addEventListener("click", () => {
    if (!picked) return;
    close();
    if (typeof opts.onPick === "function") opts.onPick(picked.path);
  });

  // 点面板内部不要把点击冒泡到 document —— 有全局的"点空白关弹窗"逻辑，
  // 不拦的话在浮层里点一下就会把底下的 SSH 设置窗一起关掉
  panel.addEventListener("click", (ev) => ev.stopPropagation());

  document.body.appendChild(panel);

  // 定位：贴在按钮右下角；右边/下边放不下就翻到另一侧
  const a = opts.anchor ? opts.anchor.getBoundingClientRect() : null;
  const pr = panel.getBoundingClientRect();
  let left = a ? (a.right - pr.width) : (window.innerWidth - pr.width) / 2;
  let top = a ? (a.bottom + 6) : 100;
  left = Math.max(8, Math.min(left, window.innerWidth - pr.width - 8));
  if (top + pr.height > window.innerHeight - 8) {
    top = a ? Math.max(8, a.top - pr.height - 6) : Math.max(8, window.innerHeight - pr.height - 8);
  }
  panel.style.left = left + "px";
  panel.style.top = top + "px";

  // 延时挂监听：打开它的那次点击还在冒泡，立刻挂会当场把自己关掉
  setTimeout(() => document.addEventListener("click", onDoc, true), 0);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("resize", close);

  // 起始位置：file 模式下 start 多半是"文件"，取它所在目录当根
  let startPath = String(opts.start || "").trim();
  if (mode === "file" && startPath) {
    startPath = startPath.replace(/\/[^/]*$/, "") || "/";
  }
  renderRoot(startPath);
}

/**
 * 按工作区标识打开远程选择器：连接参数从 /api/ssh/workspace 现查。
 *
 * 右键菜单那条路手上只有 `ssh://…`，没有表单。密码不用带 —— 后端
 * /api/ssh/browse 在密码为空时会回落到已登记的那份（见 _handle_ssh_browse）。
 */
async function pickRemoteTreeForWs(ws, opts) {
  let conn = (opts && opts.conn) || null;
  let o = opts || {};
  if (!conn) {
    try {
      const r = await api("/api/ssh/workspace");
      const hit = ((r && r.workspaces) || []).find((w) => w.ws === ws);
      if (!hit) { toast("找不到这个 SSH 工作区的连接信息", true); return; }
      conn = { host: hit.host, port: hit.port, user: hit.user,
               password: "", key: "" };
      // 起点落在现有 rg 旁边，改的时候不用从家目录一路点下去
      if (!o.start && hit.rg) o = Object.assign({}, o, { start: hit.rg });
    } catch (e) {
      toast("查连接信息失败：" + (e.message || e), true);
      return;
    }
  }
  pickRemoteTree(Object.assign({}, o, { conn }));
}


/**
 * 任务台「＋」→ 加一个**本地**工作区。
 *
 * 刻意不复用 20-workspace.js 的 `addTree`：那个在任务台窗口里还会
 * renderWorkspace / renderWsTabs / loadTree —— 这些 DOM 在独立窗口里被
 * `body.detached-board` 的 CSS 藏着（style.css 的 topbar/panel-*），白跑一遍
 * 还多一次 readdir。这边只要把路径交给 /api/workspace/add，再重画左栏。
 *
 * 也刻意**不**复用 `openWorkspacePicker`：那条的语义是"切换活动根"
 * （onPick 走 activateTree）。而任务是跨工作区总控台，没有"当前工作区"这个
 * 概念（见 dtLoadItems 上面那段注释）—— 在任务台点一下就把主窗口的活动根
 * 改掉，副作用太大。这一点与主工作台刻意不一致，详见
 * md/任务台添加工作区-与主工作台一致.md。
 */
async function dtAddLocal() {
  // 当前在远程工作区时不要把 ssh:// 当起始目录：那是远端路径，
  // 本地目录选择器打不开。退回到"空"，让用户自己粘或从盘符点。
  const start = /^ssh:\/\//i.test(String(S.workspace || "")) ? "" : (S.workspace || "");
  pickDirectory({
    title: "添加本地工作区",
    label: "加进列表",
    start,
    hint: "只加进左栏的工作区列表，**不会**切走主窗口的当前工作区。\n"
        + "路径可以直接粘进上面那个框。",
    onPick: async (p) => {
      try {
        const r = await api("/api/workspace/add", { path: p });
        if (r && r.trees) S.trees = r.trees;
        if (r && r.labels) S.labels = r.labels;
        // 列表签名变了才重铺；不置空的话 dtPaintList 会认为"没变化"而不画
        DT.sig = "";
        await dtLoadItems();
        toast(r && r.already ? "它已经在列表里了" : "已加入工作区：" + baseName(p));
      } catch (e) {
        toast("添加失败：" + (e.message || e), true);
      }
    },
  });
}

/**
 * 任务台「＋」菜单：和主工作台一样两项（本地目录 / 远程 SSH）。
 *
 * 借用文件树那套 `#ctx` 浮层（它在 detached 窗口里没被 CSS 藏掉）。
 * 点按钮时必须 stopPropagation —— document 上挂着 `click -> hideCtx`
 * （40-tree.js 的 `document.addEventListener("click", hideCtx)`），这次
 * 点击继续冒到 document 就会被立刻收起来，表现就是"点 ＋ 没反应"。
 */
function dtAddMenu(anchorEl) {
  const box = document.getElementById("ctx");
  // 老页面没有浮层（或不认识 #ctx）：退化成直接开本地目录那个，别点了没反应
  if (!box) { dtAddLocal(); return; }

  box.innerHTML = "";
  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => {
      if (typeof hideCtx === "function") hideCtx();
      fn();
    });
    box.appendChild(b);
  };
  item("打开本地目录…", () => dtAddLocal());
  item("添加远程 SSH 工作区…", () => {
    if (typeof dtAddRemoteDialog !== "function") {
      toast("当前版本没有远程工作区功能", true);
      return;
    }
    dtAddRemoteDialog();
  });

  box.classList.remove("hidden");
  const anchor = (anchorEl && anchorEl.getBoundingClientRect)
    ? anchorEl.getBoundingClientRect()
    : { left: 120, bottom: 120 };
  const r = box.getBoundingClientRect();
  box.style.left = Math.min(anchor.left, window.innerWidth - r.width - 8) + "px";
  box.style.top = Math.min(anchor.bottom + 4,
    window.innerHeight - r.height - 8) + "px";
}

async function dtAddRemoteDialog(onAdded, preset) {
  const editing = !!(preset && preset.ws);
  const pre = { host: "", port: 22, user: "", root: "", rg: "" };
  if (editing) {
    // 从标识里拆回来：ssh://user@host:port/path（密码不回显，留空即不改）
    const m = /^ssh:\/\/(?:([^@/]+)@)?([^:/]+)(?::(\d+))?(\/.*)?$/.exec(preset.ws || "");
    if (m) {
      pre.user = m[1] || "";
      pre.host = m[2] || "";
      pre.port = Number(m[3] || 22);
      pre.root = m[4] || "";
    }
    try {
      const r = await api("/api/ssh/workspace");
      const hit = ((r && r.workspaces) || []).find((w) => w.ws === preset.ws);
      if (hit) {
        pre.rg = hit.rg || "";
        pre.root = hit.root || pre.root;
      }
    } catch (e) { /* 老后端没有这个接口：用从标识里拆出来的值 */ }
  }

  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:9px;font-size:13px";

  const tip = document.createElement("div");
  tip.style.cssText = "color:var(--muted);font-size:12px;line-height:1.8;white-space:pre-wrap";
  tip.textContent = "会话和记忆存在本地 .ai/remote/ 下，远程不写任何状态。\n"
    + "命令、文件读写、搜索都通过 SSH 在远端执行。\n"
    + "远程目录留空 = 登录用户的家目录（进去之后再在文件树里往下走）。";
  body.appendChild(tip);

  // key -> <input>。必须自己存引用，不能在点「添加」时现查
  // `body.querySelectorAll("input")` —— 弹窗内容会被 openModal 搬进
  // #modalBody，那边一旦重绘，查出来就是空的，后端直接收到 {} 并回
  // 「缺少 host / user」，表单明明填好了却提交不上去。
  const inputs = {};

  const field = (label, key, value, type, ph, btn) => {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:center;gap:8px";
    const sp = document.createElement("span");
    sp.textContent = label;
    sp.style.cssText = "flex:0 0 84px;color:var(--muted)";
    const inp = document.createElement("input");
    inp.dataset.key = key;
    inputs[key] = inp;
    inp.type = type || "text";
    inp.value = value == null ? "" : String(value);
    if (ph) inp.placeholder = ph;
    inp.style.cssText = "flex:1;min-width:0;background:var(--bg);"
      + "border:1px solid var(--border);border-radius:6px;padding:5px 8px;"
      + "color:var(--text);font-size:12px;outline:none";
    row.appendChild(sp);
    row.appendChild(inp);
    if (btn) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tbtn mini";
      b.textContent = btn.label;
      if (btn.title) b.title = btn.title;
      // row 是 <label>：不拦的话点按钮会顺带聚焦输入框、还可能触发默认行为
      b.addEventListener("click", (ev) => { ev.preventDefault(); btn.onClick(ev); });
      row.appendChild(b);
    }
    body.appendChild(row);
    return inp;
  };

  /** 浏览用的连接参数：直接用表单当前填的，还没保存也能连 */
  const connNow = () => ({
    host: inputs.host.value.trim(),
    port: Number(inputs.port.value) || 22,
    user: inputs.user.value.trim(),
    password: inputs.password.value,
    key: "",
  });
  const needConn = () => {
    const c = connNow();
    if (!c.host || !c.user) { toast("先填主机和用户名，才能连上去浏览", true); return null; }
    return c;
  };

  field("主机", "host", pre.host, "text", "example.com");
  field("端口", "port", pre.port || 22, "number");
  field("用户名", "user", pre.user, "text");
  field("密码", "password", "", "password",
        editing ? "留空 = 不改密码" : "留空则用私钥 / 免密");
  field("远程目录", "root", pre.root, "text", "留空 = 家目录；也可点「浏览」挑",
        { label: "浏览…", title: "在服务器上展开树来选目录",
          onClick: (ev) => {
            const c = needConn();
            if (!c) return;
            pickRemoteTree({
              conn: c, mode: "dir", start: inputs.root.value.trim(),
              anchor: ev.currentTarget,
              title: "选择远程目录",
              onPick: (p) => { inputs.root.value = p; },
            });
          } });
  // 每台机器 rg 的位置不一定一样：这里填的是**服务器上**的绝对路径。
  // 留空就让工具自己按 PATH / 常见目录找（见 agent_actions 的 _rmt_find_files）。
  field("ripgrep 路径", "rg", pre.rg, "text",
        "留空自动找；也可点「浏览」挑",
        { label: "浏览…", title: "在服务器上展开树来选 rg 那个文件",
          onClick: (ev) => {
            const c = needConn();
            if (!c) return;
            pickRemoteTree({
              conn: c, mode: "file", start: inputs.rg.value.trim(),
              anchor: ev.currentTarget,
              title: "选择远程 ripgrep",
              onPick: (p) => { inputs.rg.value = p; },
            });
          } });

  const status = document.createElement("div");
  status.style.cssText = "color:var(--muted);font-size:12px;min-height:18px";
  body.appendChild(status);

  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;gap:8px;align-items:center;width:100%";
  const spacer = document.createElement("span");
  spacer.style.cssText = "flex:1";
  const save = document.createElement("button");
  save.className = "tbtn";
  save.textContent = editing ? "保存" : "添加";
  save.addEventListener("click", async () => {
    const payload = {};
    for (const k in inputs) payload[k] = inputs[k].value;
    payload.port = Number(payload.port) || 22;
    // 编辑时把旧标识带上：改完目录 / 端口标识就变了，后端要拿它清掉旧条目，
    // 否则标签栏里会多出一格一模一样的机器。
    payload.old_ws = editing ? preset.ws : "";
    status.style.color = "var(--muted)";
    status.textContent = "连接中…";
    try {
      const r = await api("/api/ssh/workspace", payload, { timeout: 25000 });
      if (!r || r.ok === false) throw new Error((r && r.error) || "添加失败");
      closeModal();
      // r.rg_auto = 后端在「表单没填 ripgrep」时自动探测 / 上传的结果。
      // 只在真发生了事时多说一句，常态下不加噪音。
      const ra = r.rg_auto || {};
      let extra = "";
      if (ra.uploaded) extra = "，已上传内置 ripgrep";
      else if (ra.found) extra = "，已用远端已有的 ripgrep";
      else if (ra.reused) extra = "，复用已上传的 ripgrep";
      else if (ra.error) extra = "；ripgrep 未自动配置：" + ra.error;
      toast((editing ? "已保存 " : "已添加 ") + (r.name || r.ws) + extra,
            !!ra.error);
      if (typeof onAdded === "function") {
        await onAdded(r);
        return;
      }
      DT.sig = "";
      await dtLoadItems();
    } catch (e) {
      // 404 是最常见的一种：代码更新了但服务还是旧的
      const msg = /not found|404/i.test(e.message || "")
        ? "服务还是旧版本 —— 重启一次（start.bat）再试"
        : (e.message || "添加失败");
      status.textContent = "失败：" + msg;
      status.style.color = "var(--danger)";
    }
  });
  foot.appendChild(spacer);
  foot.appendChild(save);

  openModal(editing ? "SSH 设置" : "添加远程工作区（SSH）", body, foot);
}

/** 新建终端 = 开一个常驻 shell，接下来的命令都在这个进程里跑。 */
async function dtNewTerm(ws) {
  boardPane("term");
  dtStopTail();
  DT.term.job = "";
  DT.term.ws = ws || S.workspace || "";
  DT.term.off = 0;
  DT.term.running = false;
  DT.term.cwd = DT.term.ws;
  DT.term.prevCwd = "";
  DT.ownJob = "";
  DT.key = "";
  const out = $("dtOut");
  if (out) out.textContent = "";
  DT.term.sgr = null;
  DT.term.atBottom = true;           // 清屏后输出区又是空的，自动跟随重新生效
  dtMark();
  dtHead();
  await dtEnsureShell(DT.term.ws, DT.term.ws);
  const ip = $("dtCmd");
  if (ip) ip.focus();
}

$("bdList").addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const wsCur = S.workspace || "";
  const itemEl = e.target.closest(".bd-item");
  const headEl = e.target.closest(".bd-group-head");

  // 先定位"右键落在谁身上"：项 → 它自己的工作区；组头 → 那个组；
  // 空白 → 用活动根当默认落脚点
  let ws = wsCur;
  let item = null;
  if (itemEl) {
    ws = itemEl.dataset.ws || "";
    const kind = itemEl.dataset.kind || "";
    const id = itemEl.dataset.id || "";
    item = DT.items.find((x) => x.kind === kind && x.id === id
      && (x.ws || "") === ws) || null;
  } else if (headEl) {
    const nm = headEl.querySelector(".nm");
    ws = (nm && nm.title) || wsCur;
  }

  const where = ws ? wsLabel(ws) : "当前";
  // **不管右键在哪，新建会话 / 新建终端都给全** —— 以前项上只给对应的那一个，
  // 右键会话就看不到"新建终端"，看着像没有这个功能。
  const items = [
    { label: "新建会话（" + where + "）", fn: () => dtNewSession(ws) },
    { label: "新建终端（" + where + "）", fn: () => dtNewTerm(ws) },
  ];
  // 会话的删除：**软删除**，点完不提示、不确认 —— 文件只是挪到
  // `<工作区>/.ai/sessions/old/`，列表里看不到但还能捞回来，想彻底删自己去文件夹删。
  if (item && item.kind === "chat") {
    items.push({ sep: true });
    items.push({
      label: "删除会话（挪到 sessions/old）",
      danger: true,
      fn: () => dtDeleteSession(item),
    });
  }
  if (item && item.kind === "term") {
    items.push({ sep: true });
    // 「重新运行」两种都支持，但取命令的方式不同：
    //   · 常驻终端 —— 服务端记着的"最后一条命令"（别的窗口跑过的也算）；
    //   · 一次性命令 —— 它自己那条 cmd，重新起一个作业跑一遍。
    const last = String(item.shell ? (item.lastCmd || "") : (item.cmd || "")).trim();
    const brief = last.length > 26 ? last.slice(0, 26) + "…" : last;
    items.push({
      label: !last ? "重新运行（这条没有命令记录）"
        : (item.shell ? "重新运行：" : "重新运行这条命令：") + brief,
      fn: () => (item.shell ? dtRerunLast(item) : dtRerunOnce(item)),
    });
    // 复制和重跑共用上面那个 last：**重跑的是什么就复制什么**。
    // 有个场景重跑顶不上：那条命令要加个参数、或者只是想贴到别处（对话、工单），
    // 以前只能自己从终端输出里重新划一遍。
    items.push({
      label: "复制这条命令",
      fn: () => dtCopyCmd(last),
    });
    // 终端以前只能「停止」，停完那条记录还挂在列表里（要等半小时才被清），
    // 右键也找不到删除 —— 现在按状态给对应的一项，动作都是把它摘掉。
    items.push({
      label: item.running ? "关闭终端" : "从列表移除",
      danger: true,
      fn: () => dtDropTerm(item),
    });
  }
  items.push({ sep: true });
  items.push({ label: "添加工作区…", fn: () => dtAddWorkspace() });
  dtMenu(e.clientX, e.clientY, items);
});

// ---------------------------------------------------------------------------
// 选中与详情
// ---------------------------------------------------------------------------
async function dtSelect(item) {
  if (!item) return;
  // 先断开上一条终端的常驻流：切走之后它只会往隐藏的 #dtOut 里白写，
  // 还占着一条连接和一个服务端线程。真要再看那条终端时下面会重建。
  dtStopTail();
  DT.key = dtKey(item.kind, item.ws, item.id);
  dtMark();
  boardPane(item.kind === "chat" ? "chat" : "term");
  dtHead();

  if (item.kind === "chat") {
    // 切走前把上一个会话的运行态（正在写的正文、是否在跑、seq）收进 bg。
    // 不收的话它还在后台生成，回调却继续往 live 里追加 —— 切到的那个会话
    // 窗口里就会蹦出别人的正文。
    const prevKey = dtCurKey();
    const nextKey = dtSessKey(item.ws, item.id);
    if (DT.chat.sid && prevKey !== nextKey) {
      DT.chat.bg[prevKey] = {
        live: DT.chat.live,
        reasoning: DT.chat.reasoning,
        trace: DT.chat.trace,
        segs: DT.chat.segs,
        running: DT.chat.running,
        seq: DT.chat.seq,
      };
    }
    const b = (prevKey === nextKey) ? null : DT.chat.bg[nextKey];
    DT.chat.ws = item.ws;
    DT.chat.sid = item.id;
    DT.chat.seq = b ? b.seq : -1;
    DT.chat.live = b ? (b.live || "") : "";
    DT.chat.reasoning = b ? (b.reasoning || "") : "";
    DT.chat.trace = b ? (b.trace || "") : "";
    // 流程轨迹快照跟着会话走：切到别的会话还挂着上一会话的"流程已完成"，
    // 会让人以为那是当前会话走过的步骤。
    if (prevKey !== nextKey) {
      DT.chat.stageKeep = null;
      DT.chat.stageSig = "";
      const sb = $("dtStage");
      if (sb) { sb.classList.add("hidden"); sb.innerHTML = ""; }
    }
    DT.chat.segs = b ? (b.segs || []) : [];
    DT.chat.running = b ? !!b.running : false;
    // 切过来的这一下必须看到最新那条：置位后 dtLoadHistory 只拉最新一页，
    // dtPaintChat 强制贴底，铺完自己清掉（见 DT.chat.forceBottom 注释）。
    DT.chat.forceBottom = true;
    DT.chat.sending = DT.chat.sendingSet.has(nextKey);
    const cb = $("dtCompact");
    if (cb) cb.textContent = "压缩";      // 别留着上一个会话的百分比
    dtRenderAttn();
    dtQueueRender();                      // 队列也按会话显示，别留着上一个的

    await dtLoadHistory(item.id, item.ws);
    // 拉历史的这段时间里可能又点开了别的会话，别把它刚铺好的内容冲掉
    if (dtCurKey() === nextKey) dtPaintChat(true);
  } else {
    DT.term.job = item.id;
    DT.term.ws = item.ws || "";
    DT.term.off = 0;
    DT.term.running = !!item.running;
    // 接着这条任务的目录往下敲，而不是永远退回工作区根
    DT.term.cwd = item.cwd || DT.term.ws;
    DT.term.prevCwd = "";
    const out = $("dtOut");
    if (out) out.textContent = "";
    DT.term.sgr = null;               // 换了一条 job，输出区整个重来
    DT.term.atBottom = true;
    dtHead();
    // 输出走常驻流（dtEnsureTail）：连上之后它自己推，tick 不再每拍拉增量。
    // 这里不 await —— 首块数据 50ms 内就到，等它反而把「切过去」的手感拖住。
    dtEnsureTail();
  }
}

/**
 * 打开这条会话的 md 归档目录（`<ai_dir>/history`）。
 *
 * 里面是按「时间-会话 id.md」存的 markdown —— 压缩时被摘要掉的那些原文都
 * 留在那儿（见 `agent_llm._archive_history`）。界面上只显示最近 200 条，
 * 想翻更早的、想看完整原文，都得来这个目录。
 *
 * 路径不自己拼：`.ai` 不一定在工作区目录下（远程工作区落在
 * `.ai/remote/<hash>`，自定义 ai_dir 也可能在别处），所以问后端要
 * `/api/ai/traces` 里那条历史项 —— 它给的就是这个会话真实的目录。
 */
const DT_MD_DIRS = new Map();          // ws → { dir, ai }（路径稳定，存在性现查）

/**
 * 请主窗口在它的文件树里定位 path（只展开 + 选中，不打开文件）。
 *
 * 返回："ok" 定位到了 / "fail" 主窗口回了话但树里没有 / "no-main" 没连上或
 * 没响应。超时给 8 秒：treeRevealFile 内部逐层 loadTree，深目录一两秒很正常；
 * 主窗口还可能要先存盘再切工作区。
 */
async function dtRevealTreeInMain(path, ws) {
  if (typeof BOARD_CH === "undefined" || !BOARD_CH) return "no-main";
  // 先提主窗口：任务台通常盖在上面，不提上来就是"点了没反应"
  try { if (window.opener && !window.opener.closed) window.opener.focus(); } catch (e) { /* 忽略 */ }
  const id = "rt" + (++_dtRevealSeq);
  return await new Promise((resolve) => {
    const timer = setTimeout(() => {
      DT_REVEAL_WAITS.delete(id);
      resolve("no-main");
    }, 8000);
    DT_REVEAL_WAITS.set(id, (status) => {
      clearTimeout(timer);
      DT_REVEAL_WAITS.delete(id);
      resolve(status);
    });
    try {
      BOARD_CH.postMessage({ type: "reveal-tree", id, path, ws });
    } catch (e) {
      clearTimeout(timer);
      DT_REVEAL_WAITS.delete(id);
      resolve("no-main");
    }
  });
}

async function dtOpenMdDir(ws) {
  if (!ws) return;
  const canReveal = typeof revealInExplorer === "function";
  let info = DT_MD_DIRS.get(ws);
  if (!info) {
    try {
      // 不带 ws 的 api() 会用**主窗口**的活动根，这里必须点名这条会话的 ws
      const r = await api("/api/ai/traces", undefined, { ws });
      const it = ((r && r.items) || []).find((x) => x.key === "history");
      info = {
        dir: (it && it.path) || "",
        exists: !!(it && it.exists),
        ai: (r && r.ai_dir) || "",
      };
    } catch (e) {
      if (typeof toast === "function") toast("找不到这个工作区的 .ai：" + e.message, true);
      return;
    }
    // 只缓存"确实存在"的结果：还没压缩过的会话，下次点还得重新问，
    // 否则刚压完一次，按钮还是"目录不存在"。
    if (info.exists) DT_MD_DIRS.set(ws, { dir: info.dir, ai: info.ai });
  }
  if (info.exists && info.dir) {
    // 首选：让主窗口在它的文件树里展开到这一行（不跳出应用、不打开编辑器）。
    // 主窗口没开 / 没响应 / 树里定位不到时，才退化成系统资源管理器 ——
    // 这条退路要留着：任务台可以单独开着，主窗口关掉是常见用法。
    if (typeof toast === "function") toast("正在主窗口文件树里定位 …");
    const r = await dtRevealTreeInMain(info.dir, ws);
    if (r === "ok") return;
    if (canReveal) {
      if (typeof toast === "function") {
        toast(r === "fail"
          ? "主窗口的树里没找到这个目录，改用资源管理器打开"
          : "主窗口没响应，改用资源管理器打开");
      }
      revealInExplorer(info.dir);
    } else if (typeof toast === "function") {
      toast(r === "fail" ? "树里没找到这个目录" : "主窗口没开，打不开这个目录", true);
    }
    return;
  }
  // 没压缩过 → 目录都还没建。退一步打开 .ai，并说清为什么是空的
  if (info.ai) {
    if (typeof toast === "function") {
      toast("这条会话还没压缩过，暂时没有 md 归档（先打开 .ai）");
    }
    if (canReveal) revealInExplorer(info.ai);
    return;
  }
  if (typeof toast === "function") toast("这个工作区还没有 .ai 目录", true);
}

function dtHead() {
  const title = $("bdTitle"), status = $("bdStatus"), stop = $("bdStop");
  const item = DT.items.find((r) => dtKey(r.kind, r.ws, r.id) === DT.key);

  // 「md 目录」按钮：打开这条会话的归档目录（`<ai_dir>/history`，见
  // dtOpenMdDir）。ws 记在 dataset 上而不是闭包变量：dtHead 每次状态变化都会
  // 重跑，闭包里拿到的会是上一轮那条会话的路径。
  // 终端没有会话历史，不显示；远程工作区的 .ai 也在本地，照样能用。
  const reveal = $("bdReveal");
  if (reveal) {
    const ws = (item && item.kind === "chat") ? String(item.ws || "") : "";
    reveal.classList.toggle("hidden", !ws);
    reveal.dataset.ws = ws;
    reveal.title = ws
      ? "在主窗口的文件树里定位这条会话的 md 归档目录（.ai/history）" : "";
  }

  // 命令行提示里带上当前目录：`cd` 之后能立刻看出跑在哪儿，
  // 而不是只看到一句「退出码 0」，不知道到底切没切过去。
  const cp = $("dtCmd");
  if (cp && (!item || item.kind === "term")) {
    const dir = DT.term.cwd || (item && item.cwd) || DT.term.ws || S.workspace || "";
    cp.placeholder = (dir ? "回车执行（" + dir + "）" : "回车执行")
      + " · Tab 补全 · ↑↓ 历史 · Ctrl+L 清屏";
    cp.title = dir ? "当前目录：" + dir : "";
  }

  if (!item) {
    // 没选中已存在的任务时：如果刚点了「新建终端」，标题照样得说明在哪儿跑
    if (title) {
      title.textContent = DT.term.ws
        ? "终端 · 新命令（" + wsLabel(DT.term.ws) + "）" : "—";
      // `.bd-title` 是 ellipsis + nowrap（窄窗口下「终端 · 修…」就没了），
      // 全文只能靠 title 兜底 —— 见 md/终端头部被截断-悬浮看全文.md
      title.title = title.textContent;
    }
    if (status) { status.textContent = ""; status.title = ""; status.classList.remove("run"); }
    if (stop) stop.classList.add("hidden");
    return;
  }
  if (title) {
    title.textContent = (item.kind === "chat" ? "会话 · " : "终端 · ") + item.name
      + (item.ws ? "　（" + wsLabel(item.ws) + "）" : "");
    title.title = title.textContent;
  }
  const running = item.kind === "chat" ? item.running : DT.term.running;
  if (status) {
    status.textContent = running
      ? "运行中 " + fmtDur(Date.now() - (item.t0 || Date.now()))
      : "空闲";
    // 口径跟主窗口的 updateTermStatus 一致：title 兜底，截断也能看全文
    status.title = status.textContent;
    status.classList.toggle("run", running);
  }
  if (stop) stop.classList.toggle("hidden", !running);
}

// ---- 会话 ----
/**
 * 拉会话历史。
 *
 * 必须带上目标 sid 并在回来时核对：请求在飞的时候用户可能已经切到别的会话
 * （会话4 在生成、历史大、响应慢，切到会话6 之后它才回来）。老代码直接写
 * `DT.chat.history`，于是标题是会话6、正文却是会话4 的 —— 典型的"会话串了"。
 */
async function dtLoadHistory(sid, ws) {
  const target = sid || DT.chat.sid;
  if (!target) return;
  // 会话键带上工作区：两个工作区都有 s1，只比 sid 会把 p60 的 s1 的历史
  // 铺到 gp 的 s1 上（"任务台里串会话"就是这个）。
  const targetWs = (ws === undefined) ? DT.chat.ws : ws;
  const targetKey = dtSessKey(targetWs, target);
  // 同一会话重拉（每轮结束都会走一次）时按**已加载的页数**要，别把用户刚翻出来
  // 的老记录又收回去；切会话才回到一页。targetKey 上面已经算好，直接比。
  // 切换过来的（forceBottom）一律只要最新一页：用户要的是"最新那条"，
  // 不是把上次翻出来的老记录重新铺一遍（那是「加载旧的」）。
  const sameSess = (DT.chat.paintedSid === targetKey) && (DT.chat.pages > 1)
    && !DT.chat.forceBottom;
  const pages = sameSess ? DT.chat.pages : 1;
  let r = null;
  try {
    r = await api("/api/session?sid=" + encodeURIComponent(target)
                  + "&tail=" + (DT_PAGE * pages),
                  undefined, { ws: targetWs });
  } catch (e) {
    if (dtCurKey() === targetKey) { DT.chat.history = []; dtResetPage(); }
    return;
  }
  if (dtCurKey() !== targetKey) return;    // 期间切走了，这份结果作废
  // 后端必须确认这个会话属于这个工作区（否则宁可不铺，别把别人的历史画进来）
  if (r && r.ws && dtWsKey(r.ws) !== dtWsKey(targetWs)) {
    DT.chat.history = [];
    dtResetPage();
    toast("会话工作区对不上（" + r.ws + " ≠ " + targetWs + "），已取消显示", true);
    return;
  }
  DT.chat.history = (r && r.history) || [];
  // 只铺一页：首屏给最近 DT_PAGE 条，更早的由「加载更早」往回翻。
  // `ui.start` 是后端给的**绝对**下标（正好是下一条的 before），`ui.total`
  // 是总条数。用相对下标翻页不行：翻的过程中会话还在往后追加，位置整体 +1，
  // 翻第二次就会跳条。
  const ui = (r && r.ui) || {};
  DT.chat.more = !!(r && r.history && r.history.length && ui.has_more);
  DT.chat.start = (typeof ui.start === "number") ? ui.start : 0;
  DT.chat.total = (typeof ui.total === "number") ? ui.total : DT.chat.history.length;
  DT.chat.anchorH = 0;
  DT.chat.pages = pages;
  if (r && r.usage) dtUsage(r.usage);      // 压缩按钮上的百分比
  // 会话级智能体以服务端为准（可能是主窗口 / 别的窗口切过的）。
  // 空串 = 老会话，保留当前选择 —— 刚新建、还没发过消息的会话就是这种。
  if (r && r.agent) DT.chat.agent = r.agent;
  dtRenderAgent();
}

/**
 * segs 里是否已经画过正文（画过就别再按 content 补一份）。
 *
 * 和主界面 30-chat.js 的 `segsPaintedText` 是**同一个判据，必须同步**：两套
 * 渲染器各画各的 DOM，只改一处就会出现「主界面好了、任务台照样重复一遍」。
 * 优先复用主界面的实现（index.html:519 的 30-chat.js 在 :521 的
 * 37-detached.js 之前加载），拿不到时退回本地同款判断。
 *
 * 判据不能再用 `segsEndWith`（「最后一段正文 == content」）：现在 content 是
 * 这一轮**所有旁白拼起来的全文**，和最后一段永远不相等 → 恒 false → 末尾
 * 照旧按 content 全文重画一遍，刷新后同一段话就出现两次。
 */
function dtSegsPainted(segs) {
  if (typeof segsPaintedText === "function") return segsPaintedText(segs);
  return (segs || []).some((s) =>
    s && s.text && s.kind !== "tool" && s.kind !== "stage" && s.kind !== "stage_done");
}

// ---- 工具轨迹 ----
// 历史里存的是纯文本轨迹（agent_llm._make_trace），主窗口的 parseTrace /
// makeTraceRow 就在同一个页面里，直接复用 —— 否则两边工具行的样式和展开
// 行为会慢慢走偏。
function dtParseTrace(text) {
  if (!text) return [];
  try {
    return (typeof parseTrace === "function") ? parseTrace(text) : [];
  } catch (e) { return []; }
}

function dtTraceRow(t) {
  try {
    if (typeof makeTraceRow === "function") return makeTraceRow(t);
  } catch (e) { /* 落到下面的兜底 */ }
  const d = document.createElement("div");
  d.className = "tool-row";
  d.textContent = t.name + (t.args ? "(" + t.args + ")" : "")
    + (t.result ? " \u2192 " + t.result : "");
  return d;
}

/**
 * 按 `segs` 交错铺：正文气泡 / 工具行，顺序和主界面实时看到的一致。
 *
 * segs 是后端从事件流里抄下来的"这一轮真实画过的段"（现在跟着界面文件一起
 * 落盘）。没有它就退回 `trace`（压缩过的轨迹，工具全堆在开头）。
 */
function dtPaintSegs(row, segs) {
  let tools = null;
  const boxes = [];            // 这一条里所有工具容器：收尾时统一铺（超上限要折）
  for (const s of segs) {
    if (!s) continue;
    if (s.kind === "tool") {
      if (!tools) {
        tools = document.createElement("div");
        tools.className = "tools";
        row.appendChild(tools);
        boxes.push({ box: tools, rows: [] });
      }
      // 先攒着不建节点：一条消息可能带几百个工具 segs（实测 s4 有 891 个），
      // dtTraceRow 每行一次 innerHTML + 三次 querySelector，上千行就是切会话
      // 那一下卡顿的一半。见 dtFillTools 的折叠上限。
      boxes[boxes.length - 1].rows.push({
        name: s.name || "", args: s.args || "", result: s.result || "" });
    } else if (s.kind === "stage" || s.kind === "stage_done") {
      tools = null;
      row.appendChild(dtStageLine(s));
    } else if (s.text) {
      tools = null;              // 下一批工具另起容器，顺序才不会被打乱
      dtLinkify(dtBubble(row, s.text), DT.chat.ws);
    }
  }
  for (const b of boxes) dtFillTools(b.box, b.rows);
}

/**
 * 把一批工具行铺进容器，超过 DT_MAX_TOOLS 的折成一颗「展开」。
 *
 * 折的是**单个容器**（一段连续的工具调用），不是整条消息：一段文字会把
 * 工具切成好几段，按容器折才不会打乱「工具→文字→工具」的顺序。
 * 展开只补这一个容器剩下的行，也不动别处。
 *
 * 折的是**更早的**那批，最新的 cap 行直接铺：任务台就是拿来看"刚跑了什么"的，
 * 以前留旧折新，最新的全被按钮压住，用户以为工具没执行。
 */
function dtFillTools(box, rows) {
  const cap = Math.min(rows.length, DT_MAX_TOOLS);
  if (rows.length <= cap) {
    for (const t of rows) box.appendChild(dtTraceRow(t));
    return;
  }
  const hidden = rows.length - cap;
  const head = rows.slice(0, hidden);   // 更早的：折起来
  const tail = rows.slice(hidden);      // 最新 cap 行：直接铺
  const b = document.createElement("button");
  b.className = "dt-more";
  b.type = "button";
  b.textContent = "\u22ef 还有 " + hidden + " 条更早的工具记录（点击展开）";
  b.addEventListener("click", () => {
    // 插在按钮原位（也就是最新那几行之前），顺序才对：早 → 晚
    const frag = document.createDocumentFragment();
    for (const t of head) frag.appendChild(dtTraceRow(t));
    box.insertBefore(frag, b);
    b.remove();
  });
  // 按钮先入容器成为首个子节点，再把最新的拼在后面：末尾 append 会压在
  // 最新那几行下面，等于没露出来。
  box.appendChild(b);
  for (const t of tail) box.appendChild(dtTraceRow(t));
}

/**
 * 某一段的稳定身份（`kind` + 工具 id + 下标）。
 *
 * 下标也要带上：后端 300 段上限是从**头部**删，删完同一位置换了内容，靠下标
 * 立刻能判出来（该重铺）；只比 kind + id 的话，前面被删掉几段后剩下的 id 还是
 * 对得上，会把旧节点当成新段的内容去更新，画出串位的轨迹。
 */
function dtSegKey(s, i) {
  const k = (s && s.kind) ? s.kind : "text";
  const id = (s && s.id !== undefined && s.id !== null) ? String(s.id) : "";
  return i + ":" + k + ":" + id;
}

/** 摘掉这一行里所有「段」元素（不碰思考块 / busy 行）。 */
function dtSegClear(el) {
  for (const n of (DT.chat.segEls || [])) {
    if (n && n.el && n.el.parentNode === el) el.removeChild(n.el);
  }
  DT.chat.segEls = [];
  DT.chat.segKeys = [];
  // 工具行账本必须一起清：被折掉的那几行早就不在 DOM 上了，segEls 里也是 null，
  // 只清 segEls 会让下一轮从头铺时把总数算多（折叠量偏大，露出行数不足）。
  DT.chat.toolRows = [];
  const b = DT.chat.segMoreEl;
  if (b) {
    if (b.parentNode) b.remove();
    DT.chat.segMoreEl = null;
  }
}

/** 最后一步（没有下一跳）+ 聊天模式：这段结论就是要看的答案本体，铺开。 */
function dtStagePlain(s) {
  return s.kind === "stage_done" && !s.auto
    && !!s.final && String(s.flow || "") === "chat";
}

/** 阶段行的标题（实时重绘时也要用同一份措辞）。 */
function dtStageLabel(s) {
  const txt = String(s.text || "");
  const done = s.kind === "stage_done";
  return (done ? "本步结论" : "本步开始")
    + (s.name ? " · " + s.name : "")
    + (done && txt ? "（" + fmtNum(txt.length)
      + (dtStagePlain(s) ? " 字）" : " 字，点开看）") : "");
}

/**
 * 流程的「这一步开始了 / 这一步的结论」：**一行**，点开才看全文。
 *
 * 以前 conclusion 是当正文整段铺开的 —— 交付步的结论常常就是模型正文的
 * 复述，于是一轮里同一段话出现三遍（finish_stage 的工具参数、这里、
 * 卡片「总结」），用户会以为交付跑了三次。
 *
 * 例外是聊天模式的**最后一步**：那一步的结论就是交付内容本身，折起来等于把
 * 答案藏进「点开看」。所以那一块默认铺开、并且按 Markdown 渲染（`mdToHtml`），
 * 和主界面的 addStageDone 保持同一套规则 —— 两边各写一份迟早会分叉。
 */
function dtStageLine(s) {
  const d = document.createElement("details");
  d.className = "stage-done";
  const sum = document.createElement("summary");
  sum.textContent = dtStageLabel(s);
  d.appendChild(sum);
  const txt = String(s.text || "");
  if (txt) {
    const p = document.createElement("div");
    p.className = "stage-done-body";
    const html = dtMd(txt) || "";
    p.innerHTML = html;
    // 把缓存一起种上：实时更新那段（dtPaintChat 的更新循环）靠 _mdSrc 判
    // 「这段文字没变就别重跑 markdown」。不种的话刚建好就被当成"没算过"，
    // 交付结论那种几万字的正文会立刻被重新解析一遍 —— 白算一次。
    p._mdSrc = txt;
    p._mdHtml = html;
    d.appendChild(p);
  }
  // 展开态跨重绘保留。虽然 dtPaintChat 现在按前缀只追加（不再每来一段就整行
  // 重铺），但节点仍会重造：后端 300 段上限删头、点「还有 N 条工具记录」放开
  // 上限、换会话重铺 —— 这些时候 details 的 open 只在 DOM 上，重建即丢，用户
  // 刚展开的结论就被关上了。账本定义在 30-chat.js（先于本文件加载）。
  // 用户点过就以他的选择为准；没点过才用默认（最后一步铺开）。
  d.open = stageOpenSeen(s) ? stageOpenGet(s) : dtStagePlain(s);
  d.addEventListener("toggle", () => stageOpenSet(s, d.open));
  return d;
}

/**
 * 「思考过程」块：放到这一行的最上面（历史回放和实时都走它）。
 * 只给界面看 —— 后端的 reasoning 不进 content / 历史。
 */
function dtThinkBlock(row, text) {
  const t = String(text || "");
  if (!row || !t) return null;
  const d = document.createElement("div");
  d.className = "think";          // 默认折叠，和主界面一致
  d.innerHTML = '<div class="think-h"></div><pre class="think-body"></pre>';
  d.querySelector(".think-body").textContent = t;
  // 滚轮 / 拖动条由块自己接管，别再冒泡给 #dtLog：会话一被滚走，
  // dtNearBottom 就判「没贴底」，日志不再跟随，看着像块自己塌了。
  thinkBindScroll(d);
  d.querySelector(".think-h").addEventListener("click", () => {
    d.classList.toggle("open");
    // 展开即贴到最新（和主界面 thinkBlock 同一套规矩）：用户点开就是想看
    // 现在写到哪了，停在第 1 行等于让他自己往下拖。折叠时先停跟随，
    // 下次展开重新贴底；展开后他一滚，thinkBindScroll 把 _follow 置 false。
    if (d.classList.contains("open")) thinkScrollEnd(d);
    else d._follow = false;
    dtThinkHead(d, t, false);
  });
  dtThinkHead(d, t, false);
  row.insertBefore(d, row.firstChild);
  return d;
}

/**
 * 思考块标题 + 折叠态。默认折叠：reasoning 动辄几千字，展开就把答案挤出屏幕。
 *
 * thinking 为真（这一轮还在跑、正文和工具都还没开始）时写「思考中…」，
 * 给用户一个"它还活着"的信号 —— 折叠着只看那一行，不占地方。
 */
function dtThinkHead(d, text, thinking) {
  const n = fmtNum((text || "").length);
  const head = d.querySelector(".think-h");
  const open = d.classList.contains("open");
  head.textContent = thinking
    ? "思考中…（" + n + " 字，" + (open ? "点这里折叠）" : "点开看）")
    : "思考过程（" + n + " 字，" + (open ? "点这里折叠）" : "点这里展开）");
  head.classList.toggle("live", !!thinking);
}

/** 这一轮还在思考吗：还在跑，正文和工具都还没开始。 */
function dtThinking() {
  return !!(DT.chat.running && String(DT.chat.reasoning || "").length
            && !String(DT.chat.live || "").trim()
            && !DT.chat.trace && !(DT.chat.segs || []).length);
}

/**
 * 把一段轨迹铺成一个工具行容器（没有工具就什么都不加）。
 * 注：以前这里还有个 dtThinkScrollEnd，只做 `scrollTop = scrollHeight`。
 * 那样贴完底不记 _progTop、不置 _follow，scroll 监听把程序性赋值当用户
 * 拖动、_follow 立刻被清掉 —— 正是"展开不跟随"的病根，已统一到
 * 30-chat.js 的 thinkScrollEnd。
 */
function dtPaintTools(el, trace) {
  const rows = dtParseTrace(trace);
  if (!rows.length) return;
  const box = document.createElement("div");
  // 用主界面那个 .tools 容器（纵向排、有上限宽度）。**不能叫 .dt-tools** ——
  // 那是输入框底下那排 📎/📁 按钮的类名，会把工具行排成横着一串。
  box.className = "tools";
  // 老数据（没有 segs，只有压缩轨迹）同样要限：工具全堆在一个容器里，
  // 一条就可能上百行。
  dtFillTools(box, rows);
  el.appendChild(box);
}

/**
 * 一条消息的外壳。
 *
 * 形状和主界面 .row 里那套「inner 列 + bubble」一致（纵向排：工具行 →
 * 正文气泡 → 统计行），所以样式直接借 .bubble / .tool-row / .stat-row，
 * 不必在这边再维护一份"简化版排版" —— 两份迟早走偏，看起来就像两个产品。
 */
function dtMsgRow(kind) {
  const row = document.createElement("div");
  row.className = "dt-msg " + kind;   // kind: "user" | "bot"
  return row;
}

/** 一段正文气泡。Markdown 和主界面走同一个 mdToHtml。 */
function dtBubble(row, text) {
  const b = document.createElement("div");
  b.className = "bubble dt-body";
  b.innerHTML = dtMd(text || "");
  row.appendChild(b);
  return b;
}

/**
 * 用户气泡：正文和主界面走同一套（paintUserBubble），附件块默认折叠成一行，
 * 另外这里多一步「路径变可点」。历史里的 content 是后端拼好的、带附件全文的，
 * 原样展开会把任务台刷成一整屏日志。
 */
function dtUserBubble(row, text) {
  const b = dtBubble(row, "");
  const link = (el) => dtLinkify(el, DT.chat.ws);
  if (typeof paintUserBubble === "function") return paintUserBubble(b, text || "", link);
  b.innerHTML = dtMd(text || "");
  dtLinkify(b, DT.chat.ws);
  return b;
}

/**
 * 一轮结束后的统计行（⏱ 耗时 · tokens · 缓存命中 · 工具次数…）。
 *
 * 直接借主界面的 addStatRow：文案必须和原版一模一样，各写一份迟早对不上。
 * 它只往 h.inner 里 append 一次，末尾那次 scrollToBottom() 在没有活动标签时
 * 会自己返回 —— 所以在独立窗口里借用是安全的。
 */
function dtStatRow(stats) {
  if (!stats || typeof stats !== "object") return null;
  if (!stats.elapsed_ms && !stats.total_tokens) return null;
  const box = document.createElement("div");
  if (typeof addStatRow === "function") {
    try {
      addStatRow({ inner: box, stat: null }, stats);
      const row = box.firstElementChild;
      if (row) return row;
    } catch (e) { /* 落到下面的兜底 */ }
  }
  const row = document.createElement("div");
  row.className = "stat-row";
  row.textContent = "⏱ " + fmtMs(stats.elapsed_ms) + "  ·  "
    + fmtNum(stats.total_tokens) + " tokens";
  return row;
}

/**
 * ask_user 的选项卡：任务台自己发起的会话里，模型要用户拍板的话，选项必须
 * 在这边也能点 —— 否则那一轮会一直卡到超时，用户只看到"生成中"不动。
 *
 * 卡片本身借主界面的 renderAsk（样式 / 提交逻辑只有一份），这里只给它一个
 * 能 append 的壳：`inner` 指一条新的 bot 行，`bubble`/`tools` 给 null
 * （renderAsk 只用它们做分段判断，null 是安全的）。
 */
function dtAskRow(ask) {
  const log = $("dtLog");
  if (!log || typeof renderAsk !== "function") return;
  // 去重必须在**建行之前**：以前是先 dtMsgRow + append，再交给 renderAsk —— 而
  // renderAsk 按 ask.id 去重后直接 return，于是第二次起每秒往 #dtLog 里塞一个
  // 空行，`.dt-msg{margin-bottom:12px}` 照样占位。等 180 秒就是 180 个空行
  // （≈2000px 纯空白），卡片被一路顶走、滚动条自己往下爬，用户根本点不中。
  //
  // 这一问已经收尾过（答过 / 失效），折叠成的一行记录还在日志里 —— 直接走开。
  // 与 renderAsk 里那道判据同源（askFoldedIn），放这儿是为了连"建个空行再回收"
  // 都不做：轮询每秒来一次，append + remove 虽然在同一帧里看不见，但没必要。
  if (typeof askFoldedIn === "function" && askFoldedIn(log, ask && ask.id)) return;
  const prev = (ask && ask.id) ? askNodes[ask.id] : null;
  if (prev) {
    // 卡片还在文档里 → 这一秒什么都没变，一个节点都别动
    if (prev.isConnected) return;
    // 卡片被重铺清掉了（dtPaintChat(true) 会 log.textContent = ""）：删掉失效
    // 记录让它重画一次，否则这张卡片从此永远消失
    delete askNodes[ask.id];
  }
  // 贴不贴底要在 append 之前判 —— 先 append 已经把 scrollHeight 撑大了，之后
  // 再判永远"没贴底"。不贴底就一个字都别碰滚动位置：用户正往上翻，别每秒
  // 把他拽回底部（这才是"啥都没选却一直在滚"的直接来源）。
  const stick = dtNearBottom(log);
  const row = dtMsgRow("bot");
  log.appendChild(row);
  renderAsk({ inner: row, bubble: null, tools: null, text: "" }, ask);
  // renderAsk 没建出卡片（options 为空）就把空壳收回来，别在流里留 12px 空行
  if (!row.firstChild) { row.remove(); return; }
  if (stick) {
    log.scrollTop = log.scrollHeight;
    if (typeof dtPaintDown === "function") dtPaintDown();
  }
}

/**
 * 人工作业卡片（handoff）在任务台里的补画。
 *
 * 和 ask 一样：那张卡是 SSE 推的，独立窗口只轮询 `/api/chat/live`，收不到。
 * 借着 renderHandoff（样式和提交逻辑只有一份），这里只给它一个能 append 的壳。
 *
 * 它比 ask 更需要补画：ask 卡过了那一轮就作废，这张卡可能在人做完之前一直
 * 挂着 —— 补不回来，用户就没地方点「完成」，AI 也就永远不会接着跑。
 */
function dtHandoffRow(ho) {
  const log = $("dtLog");
  if (!log || typeof renderHandoff !== "function") return;
  const prev = (ho && ho.id) ? handoffNodes[ho.id] : null;
  if (prev) {
    if (prev.isConnected) return;      // 这一秒什么都没变，一个节点都别动
    delete handoffNodes[ho.id];        // 被 dtPaintChat(true) 重铺清了，重画一次
  }
  const stick = dtNearBottom(log);
  const row = dtMsgRow("bot");
  log.appendChild(row);
  renderHandoff({
    inner: row, bubble: null, tools: null, text: "",
    tab: { ws: DT.chat.ws, sid: DT.chat.sid },
  }, ho);
  if (!row.firstChild) { row.remove(); return; }   // 没建出卡片就别留空行
  if (stick) log.scrollTop = log.scrollHeight;
}

/** 消息区是不是还贴着底部（用户往上翻之后就别再把他拽回去）。 */
function dtNearBottom(log) {
  if (!log) return true;
  return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
}

/** 钉底的校正窗口：这段时间内每帧贴一次，直到内容高度稳定。 */
const DT_PIN_MS = 300;

/** 用户一动鼠标 / 手指就停手，只挂一次。
 *  刚打开会话想往上翻，却被每帧拽回底部，比"没滚到底"更难受。 */
function dtPinHook(log) {
  if (log._pinHooked) return;
  log._pinHooked = true;
  const stop = () => {
    log._pinGen = (log._pinGen || 0) + 1;   // 世代一推，正在跑的校正循环下一帧就退出
    log._pinQueued = false;                 // 不放开的话后面再也排不进来
    log.classList.remove("cv-hold");
  };
  log.addEventListener("wheel", stop, { passive: true });
  log.addEventListener("touchstart", stop, { passive: true });
  log.addEventListener("mousedown", stop);
  log.addEventListener("keydown", stop);
}

/**
 * 把日志区钉到底部。
 *
 * 为什么一句 `scrollTop = scrollHeight` 加一次 rAF 不够：`.dt-msg` 带
 * `content-visibility: auto`（style.css:1890），屏外行按
 * `contain-intrinsic-size: auto 120px` **估高**参与布局。首屏 DT_PAGE 条刚
 * append 上去时，视口内那几行还没被记住真实高度，scrollHeight 是按 120px
 * 估出来的；等它们真渲染出来，高度从 120px 变成实际值 —— scrollHeight 变大、
 * scrollTop 不动，人就停在离底部一截的地方。「打开会话没有自动滚到最新」
 * 就是这一步。主界面早有兜底（30-chat.js:105 `scrollBoxToEnd` 双 rAF +
 * :128 ResizeObserver），任务台以前只有单次 rAF，所以这里补齐。
 *
 * 三件事：
 *   · **帧合并**（`_pinQueued`）：流式每个 delta 都调一次，不合并的话一帧能排
 *     十几个 rAF，每个都读 scrollHeight（强制同步布局），和用户正在做的滚动
 *     抢主线程，手感就是"划一下顿一下"。
 *   · **短窗口内每帧校正**，直到 scrollHeight 连续两帧不变；轻量档（`light`）
 *     只补两帧就收，够流式用，不会把帧排长。
 *   · **cv-hold**：完整档给 .dt-log 加这个 class，把这一层的 content-visibility
 *     关掉，这一瞬拿到真实高度；对齐主界面 `.sess-panel > .row.cv-hold`
 *     （style.css:2309）的既有做法。**只在换会话 / 首次打开用**；流式走轻量档
 *     —— 那会让所有屏外行每帧重新参与布局，滚动反而发涩。
 */
function dtPinBottom(log, light) {
  if (!log) return;
  if (log._pinQueued) {
    // 同一帧只排一次 —— 但**轻量档不能让完整档排队**。
    // 以前这里无条件 return：上一个会话流式时排的那次轻量档还没跑完，用户就
    // 切到了另一个会话，reset 分支发起的完整档被这句丢掉，只剩轻量档的两帧、
    // 而且没加 cv-hold —— 屏外行按 contain-intrinsic-size 的 120px 估高，
    // 于是停在离底部一截，看着就是「切会话没滚到最新」。
    if (light) return;                           // 轻量档之间合并掉，本来就是一帧内重复调用
    // 完整档（换会话 / 首次打开）抢占：世代号一推，旧循环下一帧自己退出。
    // 旧循环被世代挡住时**不会**清 _pinQueued（它只在正常收尾时清），
    // 所以这里必须自己清，否则后面再也排不进来。
    log._pinGen = (log._pinGen || 0) + 1;
    log._pinQueued = false;
  }
  log._pinQueued = true;
  const gen = (log._pinGen = (log._pinGen || 0) + 1);
  const now = () => (typeof performance === "object" && performance.now
    ? performance.now() : Date.now());
  const until = now() + DT_PIN_MS;
  if (typeof requestAnimationFrame !== "function") {
    log._pinQueued = false;
    setTimeout(() => { log.scrollTop = log.scrollHeight; }, 0);
    return;
  }
  if (!light) {
    log.classList.add("cv-hold");
    dtPinHook(log);
  }
  let last = -1, steady = 0, frames = 0;
  const step = () => {
    // 世代变了 = 被用户打断，或者又开了一轮钉底（它自己会接着贴），这里就别再动
    if (log._pinGen !== gen) return;
    log.scrollTop = log.scrollHeight;
    frames++;
    if (light) {
      if (frames >= 2) { log._pinQueued = false; return; }
      requestAnimationFrame(step);
      return;
    }
    const h = log.scrollHeight;
    if (h === last) steady++; else { steady = 0; last = h; }
    // 高度连续两帧不变 = 屏外行都渲染完了，不用再排帧
    if (steady >= 2 || now() >= until) {
      log.classList.remove("cv-hold");
      log._pinQueued = false;
      return;
    }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** 刷新「跳到最新」按钮：贴着底部就藏起来，翻上去了才露出来。 */
function dtPaintDown() {
  const log = $("dtLog");
  const b = $("dtDown");
  if (!log || !b) return;
  b.classList.toggle("hidden", dtNearBottom(log));
}

/**
 * 任务台重铺之后，把这一会话里**已经收尾**的选择题按账本补回成一行记录。
 *
 * `dtPaintChat(true)` 是 `log.textContent = ""` 整块清空再照历史重建，而卡片不在
 * 历史里 —— 切个会话回来，用户刚点过的「✓ 已选择：X」就凭空消失，看着像没选。
 * 账本由 `collapseAsk` 写（`30-chat.js`），键是 ws|sid|ask_id，所以这里只要
 * 当前会话的这两项；取不到就一个字都不画（认不出归属就别往日志里塞东西）。
 *
 * 只补折叠态（一行文字）。还在等作答的那张由 `dtPollChat` 走 live 补画 ——
 * 那边有 id/options 能画出可点的活卡片，账本里没有。
 */
function askReplayBoard(log) {
  if (!log) return;
  if (typeof askDoneTail !== "function" || typeof askFlatCard !== "function") return;
  const ws = String(DT.chat.ws || "");
  const sid = String(DT.chat.sid || "");
  if (!ws || !sid) return;
  const list = askDoneTail(ws, sid);
  if (!list.length) return;
  // 和主界面一样攒成一批：去重一次查完，行先攒着再一次性 append。
  // 以前逐条 `askFoldedIn(log, e.id)` —— 每条都把 log 的全部子节点遍历一遍（N×M），
  // 而这是**重铺**路径（切会话、每秒轮询也可能走到），长会话里这笔开销很显眼。
  const have = (typeof foldedIdSet === "function") ? foldedIdSet(log) : null;
  const rows = [];
  for (const e of list) {
    const dup = have ? have.has(String(e.id))
                     : (typeof askFoldedIn === "function" && askFoldedIn(log, e.id));
    if (dup) continue;
    const card = askFlatCard(e);
    if (!card) continue;
    const row = dtMsgRow("bot");
    // 打标记：增量重铺时要把这批补画的行一起摘掉重排 —— 它们 append 在历史
    // 之后，不摘的话新追加的历史行会排到它们后面（顺序就乱了）。
    row.classList.add("dt-ask-replay");
    row.appendChild(card);
    rows.push(row);
  }
  if (!rows.length) return;
  for (const row of rows) log.appendChild(row);
  // 补画完统一刷一次「跳到最新」。askFlatCard 内部把 collapseAsk 的这次同步
  // 关掉了（noPaint），否则每补一条就 toggle 一次 classList。
  if (typeof dtPaintDown === "function") dtPaintDown();
}

/** 清掉分页游标（历史没了 / 换会话时用，别让「加载更早」留着一个假入口）。 */
function dtResetPage() {
  DT.chat.more = false;
  DT.chat.start = 0;
  DT.chat.total = 0;
  DT.chat.anchorH = 0;
  DT.chat.pages = 1;
}

/**
 * 日志区顶部的「加载更早」。
 *
 * 只在前面还有历史时出现（`DT.chat.more`）。首屏只铺最近 DT_PAGE 条，
 * 这条是往回翻的唯一入口 —— 没有它，超过一页的老记录就等于看不到了。
 * 必须在铺消息**之前** append，否则它会跑到最底下。
 *
 * `existing` 是上一次建的那颗（`DT.chat.histMore`）：增量重铺时不能每次新建，
 * 否则用户刚好把鼠标压在按钮上、或者正在等 `dtLoadEarlier` 的回包，节点就被
 * 换掉了。传进来就地改文案，返回同一个元素。
 * 返回 null 表示当前不该有这颗按钮（已经往回翻到最前面了）。
 */
function dtPaintMore(log, existing) {
  if (!log) return null;
  const left = Math.max(0, (DT.chat.total || 0) - (DT.chat.history || []).length);
  if (!DT.chat.more) {
    if (existing && existing.parentNode) existing.remove();
    return null;
  }
  const txt = left ? "\u2191 加载更早（还有 " + left + " 条）" : "\u2191 加载更早";
  let b = existing || null;
  if (!b) {
    b = document.createElement("button");
    b.className = "dt-more";
    b.type = "button";
    b.addEventListener("click", () => dtLoadEarlier(b));
    // 插到最前：追加到末尾就跑到最新消息下面去了
    if (log.firstChild) log.insertBefore(b, log.firstChild);
    else log.appendChild(b);
  }
  if (b.textContent !== txt) b.textContent = txt;
  return b;
}

/**
 * 一条历史消息的稳定身份。
 *
 * 只用来**判断"还是不是同一条"**，不做展示。口径：位置 + 角色 + 正文长度 +
 * segs 条数 + 末段的身份 + 卡片 / 思考长度 + 有没有 stats。
 *
 * 为什么带上**长度**而不是整段内容：一轮结束后最后一条 assistant 消息是
 * 从"写到一半"变长的（content 涨了、segs 多几段、末尾多出统计行），长度一变
 * 就能判出"这条要重建"，不用把整段正文拿去拼字符串比较（一页 20 条、每条
 * 几万字，每秒拼一遍比 markdown 本身还贵）。
 *
 * `i` 必须参与：后端 ui_slice 会整体挪位（翻页后 start 前移），不带上就会把
 * 新位置的旧节点当成同一条，内容更新到错的节点上。
 */
function dtMsgKey(m, i) {
  if (!m) return i + ":";
  const segs = Array.isArray(m.segs) ? m.segs : [];
  const last = segs.length ? dtSegKey(segs[segs.length - 1], segs.length - 1) : "-";
  return i + ":" + String(m.role || "")
    + ":" + String(m.content || "").length
    + ":" + segs.length + ":" + last
    + ":" + String(m.card || "").length
    + ":" + String(m.reasoning || "").length
    + ":" + (m.stats ? "s" : "-");
}

/**
 * 一条历史消息 → 一个 `.dt-msg` 行。
 *
 * 整块重铺和增量重铺两处共用这一份：以前是写在 `dtPaintChat` 的循环体里，
 * 加增量路径时必然要复制一遍，而这段有八九个分支（卡片 / segs / 老 trace /
 * 统计行），复制出去两份迟早走偏。
 */
function dtHistRow(m) {
  const row = dtMsgRow(m.role === "user" ? "user" : "bot");
  if (m.role === "user") {
    // 附件正文折叠成一行；正文里提到的文件路径变成可点（点一下在主窗口打开）
    dtUserBubble(row, m.content || "");
  } else {
    // 思考过程排在这一轮最上面（刷新 / 重开任务台后照样在）
    if (m.reasoning) dtThinkBlock(row, m.reasoning);
    // 有 segs 就按"当时画过的顺序"交错铺（旁白 / 工具行），没有才退回
    // 压缩过的 trace（工具全堆开头，中间的过程旁白已经没了）。
    if (Array.isArray(m.segs) && m.segs.length) dtPaintSegs(row, m.segs);
    else dtPaintTools(row, m.trace);
    // 只扫正文：工具行的参数里也有路径，全扫会把点击展开抢掉。
    // 过程 + 结果并存：卡片里的「总结」就是上面那段正文（m.content），
    // 所以先画正文气泡，再画**去掉总结段**的卡片（工具调用 / 改动文件 / 核验）。
    // 已停止 / 已中断的卡片没有「总结」段，整张画出来即可。
    const card = m.card || "";
    const proc = card ? (m.content || "") : "";
    // segs 是否已经把正文铺过了：铺过就不能再按 content 补。
    // 上面 dtPaintSegs 每段 text 都单独画了一个气泡，而 content 是这些段
    // 拼起来的全文 —— 不判断就会在统计行上面多出一整个大气泡（截图那个）。
    const painted = dtSegsPainted(m.segs);
    const keepProc = !!proc && !isClippedReply(proc) && !painted;
    if (card && cardHasSummary(card) && keepProc) {
      dtLinkify(dtBubble(row, proc), DT.chat.ws);
      dtLinkify(dtBubble(row, stripCardSummary(card)), DT.chat.ws);
    } else if (card && cardHasSummary(card) && proc && !isClippedReply(proc)) {
      dtLinkify(dtBubble(row, stripCardSummary(card)), DT.chat.ws);
    } else if (card) {
      // 有卡片：画卡片本身。以前这里是 `card || m.content`，content 会被
      // 当成卡片的替代品混进去，卡片存在时反而把旁白又铺一遍。
      dtLinkify(dtBubble(row, card), DT.chat.ws);
    } else if (!painted) {
      // 既没卡片、segs 也没画过正文（老数据没有 segs）：才按 content 补一份
      dtLinkify(dtBubble(row, m.content || ""), DT.chat.ws);
    }
    // 这一轮的用量：后端把 stats 存进历史了，所以刷新 / 重开任务台
    // 之后统计行照样在（以前只有主界面流式那一刻能看到）
    const st = dtStatRow(m.stats);
    if (st) row.appendChild(st);
  }
  return row;
}

/**
 * 往回翻一页历史。
 *
 * 拼在头上再整段重铺（历史是连续的一串，插一条就重排一次 DOM 反而更麻烦），
 * 视野靠 `DT.chat.anchorH` 钉住：记下插入前的 scrollHeight，重铺后按高度差
 * 把 scrollTop 往下推同样的距离 —— 头部长了 N 像素、滚动也跟着 +N，
 * 用户看到的就是"位置没动，上面的内容补出来了"。
 */
async function dtLoadEarlier(btn) {
  if (!DT.chat.more) return;
  const key = dtCurKey();
  const before = DT.chat.start;
  if (!before) return;
  // 「加载更早」是用户主动往回看，不能顺手贴底：切换那一刻的 forceBottom 若还
  // 没来得及清（比如切过来之后就立刻点了加载更早），这里直接清掉。
  DT.chat.forceBottom = false;
  if (btn) { btn.disabled = true; btn.textContent = "加载中…"; }
  let r = null;
  try {
    r = await api("/api/session?sid=" + encodeURIComponent(DT.chat.sid || "")
                  + "&before=" + before + "&tail=" + DT_PAGE,
                  undefined, { ws: DT.chat.ws });
  } catch (e) { r = null; }
  // 期间用户可能已经切到别的会话：这份结果作废，别往新会话里拼
  if (dtCurKey() !== key) return;
  if (!r) { dtPaintChat(true); return; }        // 失败：按钮恢复成可点
  const older = r.history || [];
  // 后端保证这一页从 user 开始（ui_slice 会把半轮的 assistant 让掉），
  // 所以直接拼在头上不会出现"没有提问的回答"。
  DT.chat.history = older.concat(DT.chat.history || []);
  const ui = r.ui || {};
  DT.chat.start = (typeof ui.start === "number") ? ui.start : 0;
  DT.chat.total = (typeof ui.total === "number") ? ui.total : (DT.chat.total || 0);
  // 回空页还留着 more 会变成"点了没反应"的死按钮
  DT.chat.more = !!ui.has_more && older.length > 0;
  // 记下页数：这一轮结束重拉历史时会按它要多倍，不会把刚翻出来的收回去
  if (older.length) DT.chat.pages = (DT.chat.pages || 1) + 1;
  const log = $("dtLog");
  if (log) DT.chat.anchorH = log.scrollHeight || 1;
  dtPaintChat(true);
}

/** 把历史 + 「正在写的正文」铺进日志区。`reset` 为假时只追加/更新尾部。 */
function dtPaintChat(reset) {
  const log = $("dtLog");
  if (!log) return;
  // 跟随与否要**在改动 DOM 之前**判断：这函数每秒都会被轮询叫一次，
  // 以前末尾无条件 `scrollTop = scrollHeight`，用户往上翻看历史时
  // 下一秒就被拽回底部 —— 表现出来就是"最新那个框一直钉在底部、不跟着我滚"。
  //
  // 重铺（reset）也分两种：
  //   · **换会话** → 必须落到底（新内容，用户要看开头）
  //   · **同一会话收尾 / 轮询重画** → 尊重当前位置，否则用户正翻着历史，
  //     这一轮一结束就被拽回最底下（就是"每次都自动滚动"的来源）
  // 用会话键比：两个工作区都有 s1，只比 sid 会把"同一个会话"判错（滚动位置乱跳）
  // 「加载更早」引起的重铺单独一档：用户正在往回看，必须强制不跟底。
  // 会话短的时候 scrollHeight ≈ clientHeight，dtNearBottom 会如实判成"贴底"，
  // 一跟底就把刚翻出来的内容甩到看不见的地方。
  const anchorH = DT.chat.anchorH || 0;
  const sameSid = (DT.chat.paintedSid === dtCurKey());
  // forceBottom 排最前：用户刚刚切过来（点会话 / 新建 / 别处选中），这一下必须
  // 落到最新那条。排在后面的话，点**同一个**会话时 sameSid 为真、stick 退化成
  // dtNearBottom，用户上翻过就停在原处 —— 这就是「点一下没滚到最新」。
  const stick = DT.chat.forceBottom ? true
    : (anchorH ? false
      : (reset ? (!sameSid || dtNearBottom(log)) : dtNearBottom(log)));
  const keepTop = stick ? 0 : log.scrollTop;
  if (reset) {
    // 停靠位不属于任何一轮：重铺（换会话）时清掉，否则上一个会话的
    // 「思考中…」会赖在输入框上方
    const tdock = $("dtThinkDock");
    if (tdock) {
      tdock.textContent = "";
      tdock.classList.add("hidden");
    }
    DT.chat.traceSig = "";
    // 折叠上限按会话重置：在上一个会话点过"展开"，不该让新会话也全铺出来
    DT.chat.toolCap = 0;
    // 这一轮已经结束：历史才是权威。live 缓冲是后端留下的"最后一份正文"，
    // 留着的话下面会照着它再画一条，与刚铺好的历史完全重复 —— 就是
    // "同一句话回了两遍"。只在 running 时保留（那才是"正在写"）。
    if (!DT.chat.running) {
      DT.chat.live = "";
      DT.chat.trace = "";
      DT.chat.segs = [];
      DT.chat.busy = "";
    }
    // ---- 历史：只重铺变化的那一段 ----
    // 每轮结束（running -> 不 running）都会走到这儿。以前是 `log.textContent=""`
    // 再照 history 全量重建：节点一换，挂在节点上的 _mdSrc 缓存全丢，一页 20 条
    // 里每段正文 / 每块阶段结论都要重跑 mdToHtml，一轮 150~300 次，翻过页之后
    // 按 pages 线性放大 —— 这就是"交付那一下卡死"的主因（R1）。
    const hist = DT.chat.history || [];
    const keys = hist.map(dtMsgKey);
    const curKey = dtCurKey();
    // 跨会话一律整块重铺：两个会话的第一条消息很容易长得一样（都发一句"你好"），
    // 只比 key 会把上一个会话的行留下来。
    const sameSess = (DT.chat.histSid === curKey);
    const oldKeys = sameSess ? (DT.chat.histKeys || []) : [];
    const oldEls = sameSess ? (DT.chat.histEls || []) : [];
    let same = 0;
    while (same < oldKeys.length && same < keys.length && oldKeys[same] === keys[same]) same++;
    // 前缀一条都对不上（换会话、压缩过、翻页后整体挪位）→ 退回整块重铺。
    // 只少了尾巴（same === keys.length）算能增量：把那几条摘掉就行。
    const canIncr = oldEls.length > 0 && same > 0;
    if (!canIncr) {
      log.textContent = "";
      DT.chat.histEls = [];
      DT.chat.histMore = null;
      oldEls.length = 0;
    } else {
      // 摘掉不再需要的尾巴（历史行）
      for (let i = same; i < oldEls.length; i++) {
        const e = oldEls[i];
        if (e && e.parentNode === log) log.removeChild(e);
      }
      oldEls.length = same;
      // live 那一行和补画的选择题行都排在历史之后：一起摘掉，下面按新历史重排。
      // 不摘的话它们夹在中间，新追加的历史行会跑到它们后面去。
      const liveEl = DT.chat.liveEl;
      if (liveEl && liveEl.parentNode === log) log.removeChild(liveEl);
      for (const n of Array.from(log.querySelectorAll(".dt-ask-replay"))) n.remove();
    }
    // 实时行的账本跟着 liveEl 一起重来：节点摘了还留着引用，就会去更新一个
    // 已经不在文档里的节点（白跑一次 innerHTML）。
    DT.chat.liveEl = null;
    DT.chat.segKeys = [];
    DT.chat.segEls = [];
    DT.chat.segMoreEl = null;
    DT.chat.toolRows = [];
    DT.chat.histMore = dtPaintMore(log, DT.chat.histMore);   // 顶部「加载更早」
    const els = oldEls;
    for (let i = same; i < keys.length; i++) {
      const row = dtHistRow(hist[i]);
      log.appendChild(row);
      els.push(row);
    }
    DT.chat.histKeys = keys;
    DT.chat.histEls = els;
    DT.chat.histSid = curKey;
    // 选择题卡片不在历史里（它属于某一步工具调用，历史里只留那条 ask_user 轨迹行）。
    // 不补的话，重铺一次那张「✓ 已选择：X」就没了 —— 用户以为没选上，再问一遍。
    askReplayBoard(log);
    // 换会话落到底；同一会话回到用户原来的位置。
    // 「加载更早」再叠一层高度差：头部长高了 N 像素，scrollTop 也得 +N，
    // 否则内容会整体上移，用户看着就像"跳到上面去了"。
    if (stick) {
      // 换会话 / 首次打开时，屏外行全是"没记住真实高度"的新行，一次 rAF 补不到底
      // （见 dtPinBottom 注释）；同一会话内的重铺走轻量档，别把帧排长。
      // 切换过来的那次一定要完整档 —— 轻量档两帧撑不住 120px 估高回填。
      dtPinBottom(log, sameSid && !DT.chat.forceBottom);
    } else {
      log.scrollTop = anchorH ? keepTop + Math.max(0, log.scrollHeight - anchorH) : keepTop;
    }
    DT.chat.anchorH = 0;
    DT.chat.paintedSid = curKey;
    // 那一次"切过来必须落到底"已经兑现，后续轮询 / 收尾重画回到 dtNearBottom
    // 的常规判据（用户上翻时不该被拽回底部）。
    DT.chat.forceBottom = false;
  }
  const live = DT.chat.live || "";
  const segs = DT.chat.segs || [];
  if (!DT.chat.running && !live && !segs.length) return;
  if (!DT.chat.liveEl) {
    DT.chat.liveEl = dtMsgRow("bot");
    DT.chat.liveEl.classList.add("live");
    log.appendChild(DT.chat.liveEl);
  }
  const el = DT.chat.liveEl;

  // 有 segs 就按它铺「工具→文字→工具→文字」，和主界面一样穿插；
  // 没有（后端还没更新 / 老数据）退回"工具全堆上面 + 一整段正文"。
  if (segs.length) {
    // ---- 结构：前缀没变就只追加 ----
    // 以前是一个拼起来的 sig：stage / stage_done / text 全映射成 "x"，只有工具
    // 带 id。于是"新来一段"（哪怕只是正文又长了一行）就让 sig 变 → 整行
    // `textContent=""` 重铺 → 旧元素上的 _mdSrc 缓存全丢 → 下面那段"文字没变就
    // 别重跑 markdown"整个失效，所有段每秒重算一遍。段数一多就是 O(n²)，交付那
    // 一步一次涌进上百段，全压在同一帧里跑完，界面就卡死了。
    const keys = segs.map(dtSegKey);
    const old = DT.chat.segKeys || [];
    let same = 0;
    while (same < old.length && same < keys.length && old[same] === keys[same]) same++;
    if (same < old.length) {
      // 前缀对不上：只可能是后端 300 段上限删了头部（换会话走的是上面的 reset
      // 分支，那条已经清干净了）。把多出来的段摘掉就行，别整行清空 —— 这一行里
      // 还挂着思考块和 busy 行，一起清会把它们的展开态吞掉。
      for (let i = same; i < DT.chat.segEls.length; i++) {
        const n = DT.chat.segEls[i];
        if (n && n.el && n.el.parentNode === el) el.removeChild(n.el);
      }
      DT.chat.segEls.length = same;
      // 工具行账本跟着收缩：被摘掉的旧行不能还算在总数里，否则折叠量算多，
      // 本该露出的最新几条会被误删。按下标过滤就够 —— 后端是从**头部**删的，
      // 剩下的段整体前移，前缀一个都对不上（same=0），账本自然清空。
      DT.chat.toolRows = (DT.chat.toolRows || []).filter((r) => r && r.i < same);
    }
    // 工具行折叠：和 dtPaintSegs 一样按 DT_MAX_TOOLS 收口，**折的是更早的那些**。
    // 露出最新的 cap 条 —— 任务台就是拿来看"刚跑了什么"的，折掉最新的等于把
    // 用户在等的那几行藏起来。
    //
    // 以前是"先全建、再把超额的头部摘掉"：一轮 300 段工具全建出来（dtTraceRow
    // 每行 3 次 createElement + 几个文本节点），紧接着又摘掉 270 个 —— 白跑
    // 270 次建 DOM + 270 次 detach。后端交付前同步核对 3 秒不发事件，之后一口气
    // 涌进上百段，那一帧的峰值全压在这上面（R2）。现在先数一遍工具段总数，
    // 注定要被折掉的那些**根本不建节点**，账本里记一条 el=null 占位。
    //
    // 必须真删节点（或干脆不建），不能 display:none：节点留着等于没折，一轮 300
    // 行工具照样全压在交付那一帧（md/任务台交付卡顿.md 记录的那个卡顿源）。
    const cap = DT.chat.toolCap || DT_MAX_TOOLS;
    const start = DT.chat.segEls.length;
    // 工具段总数（已铺过的 + 新来的）。只读 .kind，比建 DOM 便宜两个数量级。
    // ti 顺带记下"已铺过几个工具段"，新段的序号从它接着数。
    let toolTotal = 0;
    let ti = 0;
    for (let i = 0; i < segs.length; i++) {
      if ((segs[i] || {}).kind !== "tool") continue;
      toolTotal++;
      if (i < start) ti++;
    }
    // 前 foldBefore 个工具段注定被折（它们是最早的那批）
    const foldBefore = Math.max(0, toolTotal - cap);
    for (let i = start; i < segs.length; i++) {
      const s = segs[i] || {};
      if (s.kind === "tool") {
        if (ti < foldBefore) {
          // 注定要折的早期行：不建节点，账本留一条 el=null（折叠量还要数它）
          DT.chat.segEls[i] = null;
          DT.chat.toolRows.push({ i: i, el: null });
        } else {
          const row = dtTraceRow({ name: s.name, args: s.args, result: s.result });
          if (s.ok === false) row.classList.add("failed");
          el.appendChild(row);
          DT.chat.segEls[i] = { kind: "tool", el: row };
          DT.chat.toolRows.push({ i: i, el: row });
        }
        ti++;
      } else if (s.kind === "stage" || s.kind === "stage_done") {
        // 阶段分隔 / 结论：一行（点开看全文），不当正文铺
        const d = dtStageLine(s);
        el.appendChild(d);
        DT.chat.segEls[i] = { kind: "stage", el: d };
      } else {
        // 每段正文一个气泡：和主界面「工具→文字→工具→文字」长得一样
        const d = document.createElement("div");
        d.className = "bubble dt-body";
        el.appendChild(d);
        DT.chat.segEls[i] = { kind: "text", el: d };
      }
    }
    DT.chat.segKeys = keys;
    const rowsAll = DT.chat.toolRows;
    // 预跳过管不了"以前露着、现在变成更早的"那批：新段进来后超出的部分还得摘。
    // 从最早的开始摘，el 已经是 null 的（预跳过的）直接跳过。
    let shown = 0;
    for (const r of rowsAll) if (r && r.el) shown++;
    let over = Math.max(0, shown - cap);
    if (over > 0) {
      for (const r of rowsAll) {
        if (over <= 0) break;
        if (!r || !r.el) continue;
        if (r.el.parentNode === el) el.removeChild(r.el);
        // 账本一起置空：下面「内容逐段更新」按 segEls[i] 找节点，留着引用就会去
        // 更新一个已经不在 DOM 上的节点（白跑一次 innerHTML）。
        if (DT.chat.segEls[r.i]) DT.chat.segEls[r.i] = null;
        r.el = null;               // 标记成"已折"，下面的 folded 数它
        over--;
      }
    }
    // 按钮上的数字 = 所有被折掉的（预跳过 + 事后摘），不只是这一帧摘的那几个
    let folded = 0;
    for (const r of rowsAll) if (!r || !r.el) folded++;
    // 被折掉的更早工具行给一颗展开按钮，插在**第一个还露着的工具行之前**：
    // append 到末尾会压在最新那几行下面，用户还是看不到最新执行了什么。
    // 点开就放开上限重铺这一行 —— 那是用户主动点的，一次性代价可以接受；
    // 不点就永远只建 DT_MAX_TOOLS 个节点。
    if (folded > 0) {
      if (!DT.chat.segMoreEl) {
        const b = document.createElement("button");
        b.className = "dt-more";
        b.type = "button";
        b.addEventListener("click", () => {
          DT.chat.toolCap = 1000000;
          dtSegClear(el);
          dtPaintChat(false);
        });
        DT.chat.segMoreEl = b;
      }
      const b = DT.chat.segMoreEl;
      let anchor = null;
      for (const r of rowsAll) {
        if (r && r.el && r.el.parentNode === el) { anchor = r.el; break; }
      }
      if (anchor) el.insertBefore(b, anchor);
      else if (b.parentNode !== el) el.appendChild(b);
      b.textContent = "\u22ef 还有 " + folded + " 条更早的工具记录（点击展开）";
    } else if (DT.chat.segMoreEl) {
      if (DT.chat.segMoreEl.parentNode) DT.chat.segMoreEl.remove();
      DT.chat.segMoreEl = null;
    }
    // 内容逐段更新：段结构没变时只改文字，免得把展开的工具行收起来
    segs.forEach((s, i) => {
      const n = DT.chat.segEls[i];
      if (!n) return;
      if (s.kind === "tool") {
        const tn = n.el.querySelector(".tn");
        const ta = n.el.querySelector(".ta");
        const tr = n.el.querySelector(".tr");
        if (tn) tn.textContent = s.name || "";
        if (ta) ta.textContent = s.args ? "(" + s.args + ")" : "";
        if (tr) tr.textContent = s.result ? "\u2192 " + s.result : "";
        n.el.classList.toggle("failed", s.ok === false);
      } else if (n.kind === "stage") {
        const sum = n.el.querySelector("summary");
        const lb = dtStageLabel(s);
        if (sum && sum.textContent !== lb) sum.textContent = lb;
        // 以前这里用 textContent 直接覆盖：把 dtStageLine 里 mdToHtml 渲染好的
        // .stage-done-body 打回纯文本，下一拍重铺又渲染回 md，再下一拍再被打回 ——
        // 长结论在两种形态之间来回切换，每切一次整块重排。任务台上看到的就是
        // 交付那一刻"文字在抖 + 卡"。改成和正文段同一套 _mdSrc/_mdHtml 缓存。
        const body = n.el.querySelector(".stage-done-body");
        if (body) {
          const src = String(s.text || "");
          if (body._mdSrc !== src) {
            body._mdSrc = src;
            const html = src ? (dtMd(src) || "\u2026") : "";
            if (body._mdHtml !== html) {
              body._mdHtml = html;
              body.innerHTML = html;
            }
          }
        }
      } else {
        // 这一段的文字没变就别重跑 markdown：dtMd = sanitizeText + mdToHtml，
        // 是轮询里最贵的一步。以前只比"算完之后 html 一不一样"，等于每秒把
        // **整篇正文**（每段都算）重新解析一遍才得出结论 —— 会话越长越卡。
        const src = s.text || "";
        if (n.el._mdSrc === src) return;
        n.el._mdSrc = src;
        const html = dtMd(src) || "…";
        // 比的是上次写进去的那份 html，不是 innerHTML —— 末尾那个闪烁光标
        // 是后插进这个元素的，读 innerHTML 会永远对不上，于是每秒重绘一遍。
        if (n.el._mdHtml !== html) {
          n.el._mdHtml = html;
          n.el.innerHTML = html;
          // 生成途中也能点路径：不然非得等这一轮结束、重铺历史之后才行
          if ((s.text || "").length < 60000) dtLinkify(n.el, DT.chat.ws);
        }
      }
    });
  } else {
    // 兜底：整轮正文一段，工具行全放上面
    const trace = DT.chat.trace || "";
    if (trace !== DT.chat.traceSig) {
      DT.chat.traceSig = trace;
      let box = el.querySelector(".tools");
      if (!box) {
        box = document.createElement("div");
        box.className = "tools";
        el.insertBefore(box, el.firstChild);
      }
      box.textContent = "";
      for (const t of dtParseTrace(trace)) box.appendChild(dtTraceRow(t));
    }
    let body = el.querySelector(".dt-body");
    if (!body) {
      body = document.createElement("div");
      body.className = "bubble dt-body";
      el.appendChild(body);
    }
    // 同上：文字没变就别重跑 markdown（兜底分支只在没有 segs 的老服务端下走）
    if (body._mdSrc !== live) {
      body._mdSrc = live;
      const html = dtMd(live) || "…";
      if (body._mdHtml !== html) {
        body._mdHtml = html;
        body.innerHTML = html;
        if (live.length < 60000) dtLinkify(body, DT.chat.ws);
      }
    }
  }
  // 临时状态提示（「校验中…」）：交付前的核对是同步的（前端全量检查
  // 2.7~3.1 秒 + 扫版本库），那两三秒里这一轮一个事件都不来。不摆一行字，
  // 任务台上看起来就是「跑着跑着不动了」—— 其实它在核对，没卡。
  // 后端一有真事件就把它清掉（见 web_agent 的 _chat_live_reset），所以这行会自己消失。
  const busyTxt = DT.chat.busy || "";
  let busyEl = el.querySelector(".dt-busy");
  if (busyTxt) {
    if (!busyEl) {
      busyEl = document.createElement("div");
      busyEl.className = "dt-busy";
      el.appendChild(busyEl);
    }
    if (busyEl.textContent !== busyTxt) busyEl.textContent = busyTxt;
  } else if (busyEl) {
    busyEl.remove();
  }
  // 思考过程：思考期间**挪到输入框上方的停靠位**，钉着不跟日志滚；
  // 思考一结束归位到这一轮最上面（和主界面同一套规矩）。
  const rsn = DT.chat.reasoning || "";
  if (rsn) {
    const dock = $("dtThinkDock");
    let t = (dock && dock.querySelector(".think")) || el.querySelector(".think");
    if (!t) {
      t = document.createElement("div");
      t.className = "think";      // 默认折叠，标题里给「思考中…」
      t.innerHTML = '<div class="think-h"></div><pre class="think-body"></pre>';
      // 同上：滚轮归块自己，别把日志区滚走
      thinkBindScroll(t);
      t.querySelector(".think-h").addEventListener("click", () => {
        t.classList.toggle("open");
        // 展开一律贴到最新 —— 以前只在「还在流」（dtThinking）时才贴，
        // 于是思考已经结束的块点开后停在第一行，用户报的就是这个。
        if (t.classList.contains("open")) thinkScrollEnd(t);
        else t._follow = false;
        dtThinkHead(t, DT.chat.reasoning || "", dtThinking());
      });
      el.insertBefore(t, el.firstChild);
    }
    // 还在想、正文和工具都还没开始 —— 只有这时候标题才是「思考中…」。
    // 一旦有正文（live）或工具行（trace / segs），说明思考阶段过去了。
    const live = dtThinking();
    const inDock = dock && t.parentNode === dock;
    const moved = (live && !inDock) || (!live && inDock);
    if (live && dock) {
      if (!inDock) dock.appendChild(t);
    } else if (inDock) {
      el.insertBefore(t, el.firstChild);
    }
    const tb = t.querySelector(".think-body");
    if (tb.textContent !== rsn) {
      // 跟随状态由用户动作决定（展开置 true，滚一下就置 false），不再用几何
      // 阈值猜：`textContent = rsn` 整体重写会把 scrollTop 打回 0，非跟随态
      // 从此永远判「没贴底」，内容再也不跟 —— 与主界面 paintThinking 同一套。
      // 非跟随：先记住位置，写完原样恢复；跟随：才贴到最新。
      const keep = t.classList.contains("open") && !t._follow ? tb.scrollTop : 0;
      tb.textContent = rsn;
      if (t.classList.contains("open")) {
        if (t._follow) thinkScrollEnd(t);
        else { tb.scrollTop = keep; t._progTop = tb.scrollTop; }
      }
    }
    // 刚搬进停靠位：重插会把内部滚动清零，跟随态的话再贴回最新一次
    if (moved && live && t.classList.contains("open") && t._follow) thinkScrollEnd(t);
    dtThinkHead(t, rsn, live);
    if (dock) dock.classList.toggle("hidden", !dock.children.length);
  }

  // 生成中的光标：只挂在**最后一段**正文末尾（和主界面 .bubble .cursor 同款）。
  //
  // 以前是「最后一段没有就补一个」，从头到尾没有 remove。而增量渲染
  // （下面 segs 那段，每来一段新正文就新建一个 .dt-body 气泡）会让段数一直涨：
  // 模型每调工具前写一句旁白就多一段 → 每多一段就多留一个 cursor，上一段的
  // 没人清 → 十几个 7×15 的蓝方块同时 blink，就是用户截图那一屏。
  //
  // 现在每帧**先清后补**：非最后一段里的 cursor 一律摘掉；这一轮已经结束时
  // 全清（以前收尾那一帧补的那个会一直留着，停下后还在闪）。
  const bodies = el.querySelectorAll(".dt-body");
  const lastBody = DT.chat.running ? bodies[bodies.length - 1] : null;
  for (const c of el.querySelectorAll(".dt-body .cursor")) {
    if (!lastBody || c.parentNode !== lastBody) c.remove();
  }
  if (lastBody && !lastBody.querySelector(".cursor")) {
    const c = document.createElement("span");
    c.className = "cursor";
    lastBody.appendChild(c);
  }
  if (stick) dtPinBottom(log, true);   // 流式：只补两帧，够用且不排长帧
  dtPaintDown();          // 内容变高后重新判断：在底部就藏起「跳到最新」
}

/** 判定类型的中文名：老会话的 stages 里带 kind（code / doc / query / ask），
 *  新的一轮不再产生 —— 阶段流程自己就是判定，不需要模型先报一个。 */
const DT_KIND = { code: "代码", doc: "文档", query: "查询", ask: "询问" };

/** 画流程进度条：走过的点亮、当前的高亮、打回过的标出来。
 *
 *  `data.stages` 是**历史**（含每一段耗时和 back 次数），`data.stage` 是当前步。
 *
 *  这里以前是「固定 4 格 + 按 id 去重」：打回后 loop 会再出现一次，于是只看
 *  最后一次所在的格子，用户看到的轨迹永远是 评审 › 执行 › 校验 › 交付，
 *  "中间被驳回重来"整段消失 —— 实际发生的是 操作1 → 打回 → 操作1 → 操作2，
 *  界面上却显示成 操作1 → 操作2，看起来像漏了一步。
 *
 *  现在改成**按 stages 历史顺序平铺**：同一阶段出现几次就画几个格子，打回
 *  （id=back）是独立的一格，夹在两段执行之间。后端已经把 id=back 作为独立
 *  阶段推进账本了（见 _deliver 的打回分支），所以这里直接按顺序画即可。
 */
function dtPaintStage(data) {
  const box = $("dtStage");
  if (!box) return;
  const stages = (data && Array.isArray(data.stages)) ? data.stages : [];
  const cur = (data && data.stage) || {};
  const running = !!(data && data.running);
  // 跑完之后**不再收起**：它记录的是"这一轮走了哪些流程"，属于结果的一部分。
  // 以前这里 `!running` 就 hidden + 清空，于是任务台一交付，整条轨迹（含打回
  // 重来的那几格）瞬间消失，用户只看到最后一句结论，无从回溯到底做了什么。
  // 现在改成收尾形态：全格置为已完成，末尾补一个总耗时，并允许折叠。
  if (!stages.length) {
    // 这一轮已经收尾、后端账本清空了 stages —— 但用户要看的是"刚才走过哪些
    // 流程"，所以**固定显示上一轮的轨迹**，直到下一次任务开始才换掉。
    // 以前这里直接 hidden + 清空，交付一瞬整条轨迹消失，只剩一句结论。
    const old = DT.chat.stageKeep;
    if (old && old.length) {
      dtPaintStage({ stages: old, stage: {}, running: false, kept: true });
    } else {
      box.classList.add("hidden");
      box.innerHTML = "";
      DT.chat.stageSig = "";
    }
    return;
  }
  // 还在跑：把这轮的 stages 记成快照，等它结束后接着显示（上面的分支用）
  if (running) DT.chat.stageKeep = stages;
  const backs = stages.filter((s) => s.id === "back" || s.back).length;
  // kept：画的是"上一轮留下的快照"，不是正在跑的这一轮。进 sig 一起判等，
  // 否则同一份 stages 从 running 变 kept 时 sig 不变、画面不更新。
  const kept = !!(data && data.kept);
  const sig = stages.map((s) => s.id + "@" + (s.t || 0) + (s.back || 0)
    + (s.out || 0)).join(",")
    + "|" + (cur.id || "") + "|" + backs + "|" + (stages.length || 0)
    + "|" + (running ? "R" : "D") + "|" + (kept ? "K" : "");
  if (sig === DT.chat.stageSig) return;   // 一秒一次轮询，没变就别重画
  DT.chat.stageSig = sig;

  box.classList.remove("hidden");
  box.innerHTML = "";

  // 收尾后的标题行：让人知道这是一条"已完成"的轨迹，可以点开/收起。
  // 运行中不显示标题（进度条本身就是标题）。
  if (!running) {
    const head = document.createElement("div");
    head.className = "dt-stage-head";
    const total = stages.reduce((a, s) => a + (Number(s.out) || 0), 0);
    // kept 是"上一轮留下的快照"：标题点明是上一次任务，别让人误以为是当前这轮
    head.textContent = (kept ? "上一轮流程" : "流程已完成")
      + (backs ? "（打回 " + backs + " 次）" : "")
      + " · 共 " + Math.max(1, Math.round(total)) + "s"
      + (DT.chat.stageFold ? " ▸" : " ▾");
    head.title = "点一下折叠/展开这条流程轨迹";
    head.addEventListener("click", () => {
      DT.chat.stageFold = !DT.chat.stageFold;
      DT.chat.stageSig = "";       // 强制重画（折叠状态变了，sig 不变也得重来）
      dtPaintStage({ stages: stages, stage: cur, running: false, kept: kept });
    });
    box.appendChild(head);
    if (DT.chat.stageFold) return;   // 折叠：只留标题行
  }
  // 同一个 id 的第几次出现：用来给重复的「执行」加序号，读起来才分得清
  const seen = {};
  stages.forEach((s, idx) => {
    const id = s.id || "";
    seen[id] = (seen[id] || 0) + 1;
    // 收尾后没有"当前步"：running 为 false 时一律不标 now，
    // 否则最后一格会一直挂着「▸ 交付 0s」的跑动形态，看着像还没结束。
    const isCur = running && idx === stages.length - 1 && (cur.id || "") === id;
    const isBack = (id === "back");
    // 判定标签：后端在 recon / loop 上带 kind（code / doc / query / ask）。
    // 只标「评审」「执行」看不出这轮到底在改代码还是在查东西，尤其
    // ask → code 这种升级，用户需要看到"评审当时判的是 ask，执行按 code 跑"。
    const kind = s.kind ? (DT_KIND[s.kind] || s.kind) : "";
    const label = (s.label || id) + (kind ? "(" + kind + ")" : "");
    const chip = document.createElement("span");
    chip.className = "dt-stage-item";
    if (isBack) chip.classList.add("back");
    if (isCur) chip.classList.add(isBack ? "back-now" : "now");
    else chip.classList.add("done");
    // 重复出现（打回后重跑）标个 ×2 / ×3，否则连着两个「执行」看不出区别
    const dup = (seen[id] > 1 && !isBack) ? " ×" + seen[id] : "";
    // 耗时：当前步按"从进入到现在"算（后端给的是秒级浮点）；已完成
    // 的格子改用 out（进入→离开的时长），否则结束后每一格都显示 0s。
    let extra = "";
    if (isCur && cur.t) {
      extra = " " + Math.max(0, Math.round(Date.now() / 1000 - cur.t)) + "s";
    } else if (!isCur && Number(s.out) > 0) {
      extra = " " + Math.round(Number(s.out)) + "s";
    }
    chip.textContent = (isCur && !isBack ? "▸ " : "") + label + dup + extra;
    // 打回那格把原因挂在 title 上：进度条位置窄，正文塞不下，鼠标停一下能看到
    if (isBack && s.reason) chip.title = s.reason;
    box.appendChild(chip);
    if (idx < stages.length - 1) {
      const sep = document.createElement("span");
      sep.className = "dt-stage-sep";
      sep.textContent = "›";
      box.appendChild(sep);
    }
  });
  if (backs) {
    const b = document.createElement("span");
    b.className = "dt-stage-back";
    b.textContent = "已打回 " + backs + " 次";
    box.appendChild(b);
  }
}

async function dtPollChat() {
  const sid = DT.chat.sid;
  if (!sid) return;
  // 记下发起这次轮询时的**会话键**（工作区 + sid）：只比 sid 的话，切到另一个
  // 工作区的同名会话时这份结果会被当成"还在这个会话上"，把别人的正文铺进来。
  const myKey = dtCurKey();
  // 正文轮询分频：**正在生成**时才 1s（正文在长、耗时秒数要走），空闲 3s
  // 就够 —— 它只是用来发现"别处又开跑了"。dtTick 空闲时本来就是 3s，这里
  // 再挡一次是因为只要**别的**会话在跑，整轮就会回到 1s，而当前这个会话
  // 可能早就停了，没必要一秒拉一次它的全文（上限 120KB 的 text + 300 段）。
  // 切了会话就从"没拉过"算起，别让上一个会话的时间戳把首帧往后拖。
  if (DT.chat.pollKey !== myKey) {
    DT.chat.pollKey = myKey;
    DT.chat.pollAt = 0;
  }
  const gap = DT.chat.running ? 1000 : 3000;
  const nowMs = Date.now();
  if (DT.chat.pollAt && nowMs - DT.chat.pollAt < gap) { dtHead(); return; }
  DT.chat.pollAt = nowMs;
  let data = null;
  try {
    // 显式传 `DT.chat.ws || ""`：为空时是**空串**（不是 undefined），
    // api() 就不会把它回落成任务台窗口自己的活动根（= gu），后端
    // pending_asks 也按 fail-closed 返回空 —— 题不会串过来，正文照常刷新。
    // 以前这里传 undefined：空 ws 被悄悄补成 gu，再叠上后端「用发起方 ws
    // 兜底过滤」，正好合成一条链，把 web 的题画到了 gu 上。
    // 不在这儿早退（方案原本这么写）：真不发轮询，正文/轨迹也跟着停，
    // 看起来像会话死了 —— 让后端 fail-closed 挡住题，代价更小。
    // 带上当前已经画到哪一版（seq）：后端见 seq 没变且这一轮已结束，就只回
    // 「没变」，不重传上限 120KB 的正文和最多 300 段的 segs。`-1`（刚切会话）
    // 表示没同步过，后端按"没带"处理。
    data = await api("/api/chat/live?sid=" + encodeURIComponent(sid)
                     + "&seq=" + (Number(DT.chat.seq) >= 0 ? DT.chat.seq : ""),
                     undefined, { ws: DT.chat.ws || "" });
  } catch (e) { return; }
  if (!data) return;
  // 请求在飞的这一秒里用户可能已经切到别的会话：这份是**上一个**会话的
  // 运行态和正文，写进来就把界面串了
  if (dtCurKey() !== myKey) return;
  const wasRunning = DT.chat.running;
  DT.chat.running = !!data.running;
  // 后端说「和你手上那一版一模一样」（unchanged）：响应里没有 text / stage /
  // segs，只剩 running=false 和待答项。这时候**一个都别拿去画** —— 尤其是
  // dtPaintStage，喂它一份没有 stages 的空对象会把进度条清掉。下面 asks 那段
  // 照跑：待答项本来就不随正文变，是每秒补画一次的。
  const unchanged = !!data.unchanged;
  // 流程进度条跟着每次轮询刷新：stage 变化时后端会 +1 seq，但"这一步跑了多久"
  // 是随时间涨的，所以**每轮都画**（内部按 sig 判等，没变就不动 DOM）。
  if (!unchanged) dtPaintStage(data);
  // 模型正在等用户点选项：ask 事件是走 SSE 的，这个窗口只轮询，收不到 ——
  // 不在这里补画，用户看到的就是「生成中」一直不动，直到后端超时（以前
  // 就是这个表现，看着像卡死）。renderAsk 内部按 ask.id 去重，重复画安全。
  // 画之前再核一遍归属：后端已经按 ws/sid 过滤过了，这一层不能省 ——
  // 后端漏传 ws 时过滤会整体失效（以前就是这样，题库直接推给了所有窗口）。
  //
  // **缺 ws 就不画（fail-closed）**，不再是「字段缺失时照画」。这条是补画：
  // 它把后端所有待答项一次摊到当前会话上，判不出归属就可能往别人的会话塞卡。
  // 真属于这个会话的那张由 SSE 画（那条有确定的 myKey，见下面的 obj.ask 分支）。
  const askWs = dtWsKey(DT.chat.ws);
  const askSid = String(DT.chat.sid || "");
  for (const ask of (data.asks || [])) {
    if (!ask) continue;
    if (!ask.ws) continue;
    if (dtWsKey(ask.ws) !== askWs) continue;
    if (ask.sid !== undefined && String(ask.sid) !== askSid) continue;
    dtAskRow(ask);
  }
  // 人工作业卡片：同样靠这里补画（SSE 这个窗口收不到）。
  // 归属判据和上面一问一模一样：ws 对不上就不画（fail-closed）。
  for (const ho of (data.handoffs || [])) {
    if (!ho || !ho.id || !ho.ws) continue;
    if (dtWsKey(ho.ws) !== askWs) continue;
    if (ho.sid !== undefined && String(ho.sid) !== askSid) continue;
    dtHandoffRow(ho);
  }
  // 到点提醒（"该接听了 / 该挂了"）：后端 tick 出来的，不经过模型
  if (typeof noticeHandoffDue === "function") {
    noticeHandoffDue(data.handoff_due || []);
  }
  // trace 和正文不一定同步变（工具调用期间没有 delta），所以分开比
  // trace / segs 都随 seq 一起推（后端每个工具事件也 +1），比一次就够了
  if (data.seq !== DT.chat.seq) {
    DT.chat.seq = data.seq;
    // 只在**还在跑**的时候才把后端缓冲当成"正在写的正文"。这一轮已经结束时，
    // 正文和轨迹都已经进历史了；再塞进 live，dtPaintChat 会在历史下面
    // **再画一条一模一样的回复** —— 用户看到的就是"同一句话回了两遍"。
    // （后端 /api/chat/live 结束后仍保留 text，方便迟到的窗口补看，
    //   所以这个判断留给前端做。）
    DT.chat.live = data.running ? (data.text || "") : "";
    DT.chat.reasoning = data.running ? (data.reasoning || "") : "";
    DT.chat.trace = data.running ? (data.trace || "") : "";
    DT.chat.segs = (data.running && Array.isArray(data.segs)) ? data.segs : [];
    // 临时状态提示（「校验中…」）：交付前的核对是同步的，那两三秒里后端
    // 发不出任何真事件，任务台不接着一行就只剩"转圈不动"（见 dtPaintChat）
    DT.chat.busy = data.running ? (data.busy || "") : "";
    dtPaintChat(false);
  }
  if (wasRunning && !DT.chat.running) {
    // 这一轮写完了：把正文换成正式历史（带工具行之外的东西都在里面）
    DT.chat.live = "";
    DT.chat.trace = "";
    DT.chat.reasoning = "";
    DT.chat.segs = [];
    DT.chat.busy = "";
    await dtLoadHistory(sid, DT.chat.ws);
    if (dtCurKey() === myKey) dtPaintChat(true);
  }
  dtHead();
}

// ---------------------------------------------------------------------------
// 发送排队（和主界面 #sendQueue 同一套意思，见 30-chat.js 的「发送排队」）
// ---------------------------------------------------------------------------
// 以前这个窗口遇到「同一会话还在生成」只回一句提示就把消息丢了 —— 用户打的字
// 还在输入框里，但按回车毫无进展。主界面早就有排队，任务台却没有：聊天搬到这个
// 窗口之后，排队功能看起来就"消失"了。
// 队列挂在 sid 上（主界面挂在标签上，同一个道理）：切到别的会话看到的是那个
// 会话自己的队列，不会串。
let DT_Q_SEQ = 0;

function dtQueuePush(key, text, files) {
  if (!key) return;
  const q = DT.chat.q[key] || (DT.chat.q[key] = []);
  q.push({ id: "dq" + (++DT_Q_SEQ), text: text || "", files: (files || []).slice() });
}

/** 队列条：只画**当前会话**的队列。 */
function dtQueueRender() {
  const box = $("dtQueue");
  if (!box) return;
  const q = DT.chat.q[dtCurKey()] || [];
  if (!q.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "dt-q-head";
  head.textContent = "排队中 " + q.length + " 条（这轮结束后自动发送）";
  box.appendChild(head);

  q.forEach((it, idx) => {
    const row = document.createElement("div");
    row.className = "dt-q-row";
    const n = document.createElement("span");
    n.className = "dt-q-n";
    // 标了「立即发送」的那条用箭头代替序号：这一轮停完就发它，不是按序排的
    n.textContent = it.urgent ? "⏫" : (idx + 1) + ".";
    if (it.urgent) row.classList.add("urgent");
    const tx = document.createElement("span");
    tx.className = "dt-q-text";
    tx.textContent = (it.text || "").replace(/\s+/g, " ")
      || ("（" + (it.files || []).length + " 个附件）");
    tx.title = it.text || "";

    const acts = document.createElement("span");
    acts.className = "dt-q-acts";
    const mk = (label, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (cls) b.className = cls;
      b.addEventListener("click", fn);
      acts.appendChild(b);
    };
    mk("立即发送", "", () => dtQueueSendNow(it.id));
    mk("编辑", "", () => dtQueueEdit(it.id));
    mk("取消", "danger", () => dtQueueDrop(it.id));

    row.appendChild(n);
    row.appendChild(tx);
    row.appendChild(acts);
    box.appendChild(row);
  });
}

function dtQueueDrop(id) {
  const q = DT.chat.q[dtCurKey()];
  if (!q) return;
  const i = q.findIndex((x) => x.id === id);
  if (i < 0) return;
  q.splice(i, 1);
  dtQueueRender();
  toast("已取消这条排队");
}

/** 编辑 = 拿回输入框改；输入框里已有草稿时把草稿压回队首，不覆盖。 */
function dtQueueEdit(id) {
  const q = DT.chat.q[dtCurKey()];
  if (!q) return;
  const i = q.findIndex((x) => x.id === id);
  if (i < 0) return;
  const ip = $("dtInput");
  const draft = ip ? ip.value.trim() : "";
  const item = q.splice(i, 1)[0];
  if (draft) {
    q.unshift({ id: "dq" + (++DT_Q_SEQ), text: draft, files: S.attachments.slice() });
    S.attachments = [];
    dtRenderAttn();
  }
  if (ip) {
    ip.value = item.text || "";
    ip.style.height = "auto";
    ip.focus();
  }
  if ((item.files || []).length) {
    S.attachments = item.files.slice();
    dtRenderAttn();
  }
  dtQueueRender();
}

/**
 * 把一条排队消息摆进输入框并发出去。
 *
 * 只处理**当前会话**（切走了就别抢用户眼前的输入框），输入框里有草稿时也不动
 * —— 用户正在打字，别把人写的字冲掉。这两种情况下把条目塞回队首，
 * 等下次出队再来（`urgent` 标记跟着条目走，插队意愿不会丢）。
 */
function dtQueuePlay(key, item) {
  const ip = $("dtInput");
  if (dtCurKey() !== key || !ip || ip.value.trim()) {
    const q = DT.chat.q[key] || (DT.chat.q[key] = []);
    q.unshift(item);                 // 切走了 / 有草稿：放回队首，等下次出队
    dtQueueRender();
    return;
  }
  ip.value = item.text || "";
  ip.style.height = "auto";
  if ((item.files || []).length) {
    S.attachments = item.files.slice();
    dtRenderAttn();
  }
  dtQueueRender();
  dtSendChat();
}

/**
 * 一轮结束：把队列里的下一条摆进输入框接着发。
 *
 * 点过「立即发送」的那条（`item.urgent`）优先，不管它在队里排第几 ——
 * 插队就是插队，不能因为前面还有两条就排到后面去。
 */
function dtQueueDrain(key) {
  const q = DT.chat.q[key];
  if (!q || !q.length) return;
  if (DT.chat.sendingSet.has(key)) return;
  const i = q.findIndex((x) => x.urgent);
  dtQueuePlay(key, (i >= 0) ? q.splice(i, 1)[0] : q.shift());
}

/**
 * 立即发送 = 先停掉正在跑的这一轮，收尾后优先发它。
 *
 * 为什么要先停：后端同一会话只允许一轮在跑（_claim_session），不停就发只会
 * 被判成 busy。停止标记立起来后后端在下一个步骤边界退出，SSE 自己收尾，
 * `finally` 里释放发送锁并调用 dtQueueDrain —— 所以这里**不自己发**：
 * 那一轮的 finally 还要 `dtPaintChat(true)` 重铺日志，抢在前面发会把刚贴上去
 * 的提问行擦掉。这里只负责"标记插队 + 立停止标记"。
 *
 * 任务台的 fetch 没挂 AbortController，所以也不用 abort 连接。
 */
async function dtQueueSendNow(id) {
  const key = dtCurKey();
  const q = DT.chat.q[key];
  if (!q) return;
  const item = q.find((x) => x.id === id);
  if (!item) return;
  const ip = $("dtInput");
  if (ip && ip.value.trim()) {
    toast("输入框里还有没发出去的草稿，先发它或清空再插队", true);
    return;
  }
  if (!DT.chat.sendingSet.has(key)) {   // 没在生成：直接发，不用停
    q.splice(q.indexOf(item), 1);
    dtQueuePlay(key, item);
    return;
  }
  for (const x of q) x.urgent = false;  // 只保留这一条插队标记
  item.urgent = true;
  dtQueueRender();
  toast("已插队：先停这一轮，收尾后立刻发它");
  try {
    await api("/api/chat/stop", { sid: DT.chat.sid, ws: DT.chat.ws || S.workspace || "" });
  } catch (e) {
    // 停止请求没送达：撤掉标记，别让它一直占着"下一轮先发它"
    item.urgent = false;
    dtQueueRender();
    toast("插队失败：" + ((e && e.message) || e), true);
  }
}

/** 这个会话在**服务端**还在生成吗？查不到就乐观地当"还在跑"。 */
async function dtChatRunningOnServer(sid, ws) {
  try {
    const r = await api("/api/chat/active?ws="
      + encodeURIComponent(ws || S.workspace || ""));
    const list = (r && r.active) || [];
    return list.some((it) => String((it && it.sid) || it) === String(sid));
  } catch (e) {
    return true;      // 接口挂了就别乱解锁，宁可让用户刷新
  }
}

async function dtSendChat() {
  const ip = $("dtInput");
  const text = (ip ? ip.value : "").trim();
  if (!ip) { toast("找不到输入框（界面没建好），刷新一下这个窗口", true); return; }
  // 空内容当误触直接返回。放在排队判断和"新建会话"**之前**：否则空输入
  // 会被排进队列，或者白建一个空会话。
  if (!text && !S.attachments.length) {
    // 以前这里静默 return —— 用户点了发送"什么反应都没有"，无从排查。
    toast("输入框是空的（或者附件没带上）", true);
    if (ip) ip.focus();
    return;
  }
  // 「新会话」勾着：先建一个会话，这条消息发进去（发完自动取消勾选，
  // 否则每按一次发送就会又开一个会话）。建失败就整条不发，别偷偷发进旧会话。
  const newChk = $("dtNewSessChk");
  if (newChk && newChk.checked) {
    newChk.checked = false;
    const wrap = $("dtNewSessWrap");
    if (wrap) wrap.classList.remove("on");
    const before = DT.chat.sid;
    await dtNewSession(DT.chat.ws || S.workspace || "");
    if (!DT.chat.sid || DT.chat.sid === before) {
      toast("新会话没建出来，这条没发出去", true);
      return;
    }
  }
  // 绑定发起时的会话：这一轮生成可能要几分钟，中途用户完全可能切去别处。
  // 以前全程读 DT.chat.*，切走之后正文就写到新会话的窗口里了。
  const sid = DT.chat.sid;
  const ws = DT.chat.ws || S.workspace || "";
  if (!sid) { toast("先选一个会话", true); return; }
  // 这一轮的会话键：发送锁 / 排队 / 后台正文缓冲都按它存（见 dtSessKey）
  const myKey = dtSessKey(ws, sid);
  // 发送锁按会话算。以前是一个全局 sending：会话4 还在生成时切到会话6，
  // 这里直接 `return` —— 表现就是"点了发送，一点反应都没有"。
  if (DT.chat.sendingSet.has(myKey)) {
    // 锁还在，但**后端可能早就结束了**：SSE 在浏览器侧挂住（代理、休眠、
    // 网络闪断）时 `await readSSE` 永远不返回，`finally` 里的 delete 就跑不到。
    // 之后这个会话每次点发送都在这里静默 return —— 表现就是"发不了消息了"，
    // 而且刷新页面才恢复（锁在内存里）。这里问一次服务端：它已经不在跑了就
    // 把锁解开重发，别让一条挂死的连接封死整个会话。
    if (await dtChatRunningOnServer(sid, ws)) {
      // 真在跑：排进队列，这一轮结束后自动发（见 dtQueueDrain）。
      // 以前这里只弹一句提示就把消息丢了，用户还得重新打一遍。
      dtQueuePush(myKey, text, S.attachments);
      S.attachments = [];
      dtRenderAttn();
      if (ip) { ip.value = ""; ip.style.height = "auto"; }
      dtQueueRender();
      toast("已排队 " + (DT.chat.q[myKey] || []).length + " 条，这一轮结束后自动发送");
      return;
    }
    DT.chat.sendingSet.delete(myKey);
  }

  DT.chat.sendingSet.add(myKey);
  DT.chat.sending = true;
  if (ip) ip.value = "";

  const files = S.attachments.slice();
  S.attachments = [];
  dtRenderAttn();
  const paths = files.filter((f) => f.kind === "file").map((f) => f.path);
  const dirs = files.filter((f) => f.kind === "dir").map((f) => f.path);
  const snippets = files.filter((f) => f.kind === "snippet").map((f) => ({
    path: f.path, start_line: f.startLine, end_line: f.endLine, text: f.text,
  }));
  const images = files.filter((f) => f.kind === "image").map((f) => f.dataUrl);

  //: 还在看这个会话吗？不是的话正文攒进 bg，切回来还能看到
  const mine = () => dtCurKey() === myKey;
  const bump = (delta, replace) => {
    if (mine()) {
      if (replace !== undefined) DT.chat.live = replace;
      else DT.chat.live += delta;
      dtPaintChat(false);
      return;
    }
    const b = DT.chat.bg[myKey]
      || (DT.chat.bg[myKey] = { live: "", running: true, seq: -1 });
    if (replace !== undefined) b.live = replace;
    else b.live = (b.live || "") + delta;
  };

  const log = $("dtLog");
  if (log && mine()) {
    // 先把上一轮那个 live 容器丢掉：它是**上次**生成时插进日志里的节点，
    // 留着的话这一轮的正文会写进它、显示在这条提问**上面**（提问跑到下面去了，
    // 看着就是"这个框钉在底部"）。置空后 dtPaintChat 会在提问之后新建一个。
    DT.chat.liveEl = null;
    const u = dtMsgRow("user");
    const names = (typeof attachLabel === "function") ? files.map(attachLabel) : [];
    // 用户消息也走 Markdown + 气泡（和主界面 addUserTo 一样），
    // 否则这边是灰块、那边是强调色气泡，同一个会话换窗口看像两个人做的
    dtUserBubble(u, (names.length ? names.join("、") + (text ? "\n\n" : "") : "") + text);
    log.appendChild(u);
    // 刚发出去：这一下必须落到最新那条上（此时还没开始滚，别被 stick 判断挡掉）
    log.scrollTop = log.scrollHeight;
  }
  if (mine()) {
    DT.chat.running = true;
    DT.chat.live = "";
    DT.chat.reasoning = "";
    DT.chat.trace = "";
    DT.chat.traceSig = "";
    DT.chat.segs = [];
    // 新一轮从头铺：上一轮的段元素还挂在**旧的** liveEl 上（那一条留着不删），
    // 账本必须一起清空，否则下面的创建循环会从旧长度接着跑，新段全落空。
    DT.chat.segKeys = [];
    DT.chat.segEls = [];
    DT.chat.segMoreEl = null;
    DT.chat.toolRows = [];
    DT.chat.seq = -1;
    dtPaintChat(false);
    dtHead();
  } else {
    const b = DT.chat.bg[myKey]
      || (DT.chat.bg[myKey] = { live: "", running: true, seq: -1 });
    b.running = true;
  }

  const body = {
    sid, ws, message: text, attachments: paths, dirs, snippets, images,
    // 智能体（会话级）：后端 _resolve_mode 会把它写回会话文件，并按这个
    // 模式决定提示词、工具表和「要不要写设计文档」（看流程有没有 doc 阶段）。
    // 空串 = 沿用会话里存的 / 默认模式。
    agent: DT.chat.agent || "",
  };
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + S.token },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
    await readSSE(resp.body, (obj) => {
      if (obj.reasoning !== undefined) {
        // 思考过程：只给界面。切走了就攒进 bg，切回来还能看
        if (mine()) {
          DT.chat.reasoning = (DT.chat.reasoning || "") + obj.reasoning;
          dtPaintChat(false);
        } else {
          const bb = DT.chat.bg[myKey]
            || (DT.chat.bg[myKey] = { live: "", running: true, seq: -1 });
          bb.reasoning = (bb.reasoning || "") + obj.reasoning;
        }
      } else if (obj.delta !== undefined) bump(obj.delta);
      else if (obj.reset_text !== undefined) bump("", obj.reset_text || "");
      else if (obj.ask) {
        // 选择题：不画出来就永远等不到人点。
        //
        // 归属是确定的 —— SSE 只写到发起这条请求的那个连接，也就是 myKey 那个会话。
        // 但 emit 那侧可能漏带 ws（后端补字段的改动落盘了、进程没重启时就是这样），
        // 那样标题会退化成「未知空间询问」。既然来源已经确定，就用它补上：
        // 标错来源比不标更误导，而这里并没有在猜。
        const a = obj.ask;
        // `!a.ws` 而不是 `=== undefined`：后端送的是 `str(ws or "")`，ctx 里
        // 没有工作区时下发的是**空串**。空串漏过去的话，下面 dtAskRow 造的是个
        // 临时 host（没有 h.tab），renderAsk 只能把 dataset.askWs 记成空串
        // → finally 里 expireAsks 按 ws/sid 认领时「认不出归属的宁可不收」
        // → 这一轮的卡永远不折叠，留在流里还能点，点了只回「那一轮已结束」。
        if (!a.ws) {
          a.ws = (mine() && DT.chat.ws)
            ? DT.chat.ws
            : (String(myKey || "").split("|")[0] || "");
        }
        // **只在还看着这个会话时才画**。#dtLog 是这一个窗口唯一的一条日志，
        // 而上一轮的 SSE 在用户切走之后并不中断（fetch 没 abort）—— 它一推 ask
        // 就照画，会话 4 的题就冒到会话 2 的窗口里了（用户看到的是"我在会话 2，
        // 会话 4 的框弹出来了"）。切回来时 dtPollChat 的补画会按真实 ws/sid
        // （fail-closed）把它画回来，所以这里不画不等于丢。
        if (mine()) dtAskRow(a);
      }
      else if (obj.error) bump("\n\n[错误] " + obj.error);
    });
  } catch (e) {
    bump("\n\n[请求失败] " + (e.message || e));
  } finally {
    // 这一轮完了：这一轮没答过的选择卡片收起。**必须带上 ws/sid** —— 收尾是
    // 按会话认领的（expireAsks 的判据），不带就等于"判不出归属"，一张都不会收；
    // 以前它不带参数、又不分归属，反过来把别的会话还在等作答的卡也折了。
    if (typeof expireAsks === "function") expireAsks(null, ws, sid);
    DT.chat.sendingSet.delete(myKey);
    DT.chat.sending = DT.chat.sendingSet.has(dtCurKey());
    if (mine()) {
      DT.chat.running = false;
      DT.chat.live = "";
      await dtLoadHistory(sid, ws);
      dtPaintChat(true);
      dtHead();
    } else {
      // 切走了：丢掉后台缓冲。切回来时 dtSelect 会重新拉历史，
      // 那时这一轮的正文已经落盘了，不需要也不该再单独拼一段。
      delete DT.chat.bg[myKey];
    }
    dtLoadItems();
    // 队列里还有就接着发下一条（必须在上面把发送锁 delete 掉之后）
    dtQueueDrain(myKey);
  }
}

// ---- 终端输入：Tab 补全 / ↑↓ 历史 / Ctrl+L 清屏 ----
// 任务台的终端是**常驻 shell + 管道**，没有 TTY —— cmd.exe 自己的 Tab 补全在
// 这种模式下根本不会触发（它只在真控制台里工作），所以补全只能在前端做：
// 走 /api/complete（和主窗口终端那颗 Tab 是同一个接口、同一套补全规则）。
const DT_HIST_MAX = 300;
let dtHistIdx = -1;          // -1 = 正在编辑一条新命令
let dtHistDraft = "";

function dtHistLoad() {
  try {
    const raw = localStorage.getItem("termHistory");
    return raw ? (JSON.parse(raw) || []).filter((x) => typeof x === "string") : [];
  } catch (e) { return []; }
}

/** 记一条历史。和主窗口终端共用 localStorage 那一份，两边都能翻到。 */
function dtHistPush(cmd) {
  if (!cmd) return;
  const hist = dtHistLoad();
  if (hist[hist.length - 1] === cmd) return;      // 连着敲同一条不重复记
  hist.push(cmd);
  try {
    localStorage.setItem("termHistory", JSON.stringify(hist.slice(-DT_HIST_MAX)));
  } catch (e) { /* 隐私模式下写不进去，不影响这条命令本身 */ }
}

/** ↑(-1) / ↓(+1) 翻历史：回到"新命令"那一格时把打了一半的内容还回来。 */
function dtHistStep(delta) {
  const inp = $("dtCmd");
  if (!inp) return;
  const hist = dtHistLoad();
  if (!hist.length) return;
  if (dtHistIdx === -1) {
    if (delta > 0) return;                        // 已经是最新那格了
    dtHistDraft = inp.value;
    dtHistIdx = hist.length - 1;
  } else {
    dtHistIdx += delta;
    if (dtHistIdx >= hist.length) {               // 越过最新 → 回到草稿
      dtHistIdx = -1;
      inp.value = dtHistDraft;
      inp.setSelectionRange(inp.value.length, inp.value.length);
      return;
    }
    if (dtHistIdx < 0) dtHistIdx = 0;
  }
  inp.value = hist[dtHistIdx];
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

/** 往输出区追加一段（会跟到底）。
 *
 * 文本可能带 ANSI 颜色，切段走和主终端**同一套** `termAnsiSeg`（90-tools.js）：
 * 两个窗口颜色口径必须一致，各写一份迟早会对不上。
 * 用 `lastChild` 判结尾而不是 `textContent` —— 后者会把整块几百 KB 拷一遍。
 */
function dtWriteOut(text) {
  const out = $("dtOut");
  if (!out || !text) return;
  const s = termClean(text);
  if (!s) return;
  const last = out.lastChild;
  const tail = last ? (last.textContent || "") : "";
  const body = (tail && !/\n$/.test(tail)) ? "\n" + s : s;
  const st = DT.term.sgr || (DT.term.sgr = termSgrNew());
  for (const seg of termAnsiSeg(body, st)) termAppendSeg(out, seg);
  DT.term.outChars += body.length;
  dtTrimOut();
  out.scrollTop = out.scrollHeight;
}

/** Tab 补全。规则和主窗口终端是**同一份**（00-core.js 的 termCompleteInBox），
 *  这里只把常驻终端的上下文喂过去：cwd 来自 `[工作目录]` 哨兵，远端时是服务器
 *  上的裸路径 —— 归一化成 `ssh://…` 由 termCompleteInBox 统一做，两套终端才一致。
 */
async function dtComplete() {
  const inp = $("dtCmd");
  if (!inp) return;
  await termCompleteInBox(inp, DT.term.cwd || "", DT.term.ws || "",
    (items) => dtWriteOut("候选：" + termCandidatesText(items) + "\n"),
    // 0 候选时把**实际补全用的目录**报出来。远程终端的 cwd 从前是不同步的，
    // 补全失败却一声不响，只能靠猜（见 md/远程终端目录不同步导致补全错.md）。
    // note 是后端给的「目录不存在」—— cwd 错了和真没这个前缀得能分开看。
    (used, note) => dtWriteOut("补全目录：" + used +
                               "（" + (note || "没有匹配") + "）\n"));
}

// ---- 终端 ----
// #dtOut 最多留多少字符 / 每次超限砍到多少。
// 后端 _TERM_MAX_CHARS 也是 200000，两边同口径；KEEP 比 MAX 小一截是故意的 ——
// 砍到刚好 200000 的话下一块 1KB 就再次超限，等于每一帧都整块重画，比不截断更卡。
const DT_OUT_MAX = 200000;
const DT_OUT_KEEP = 150000;
//: 一帧最多往 #dtOut 画多少字符（剩下的留到下一帧）。
//:
//: 为什么不是"攒多少画多少"：输出高峰时一个 rAF 里可能攒着 64KB（流式单块）甚至
//: 更多，其中大半带 SGR，切出来是上千个 <span>。一帧塞进去，浏览器要在那一次
//: 排版里做完全部节点的布局 —— 那一帧里页面什么都不响应，滚轮都跟手不上。
//: 32KB 大约是"一屏半"的日志量，分帧之后单帧排版量有上界，观感上只觉得刷得快，
//: 看不出是分两次画的（见 md/终端输出高峰整页卡顿.md）。
const DT_PAINT_MAX = 32768;

/** 输出区涨过上限就从头砍掉一批（整块重画一次）。
 *
 * 任务台原来完全没有这一步：`dtWriteOut` 和轮询都是纯 append，只有后端
 * `reset:true`（落后 20 万字符）才清一次。改成流式之后每秒能来几十块，
 * 一个 gradle build 跑下来 #dtOut 里能堆几十万字符的 span，滚动和重排都开始拖。
 * 主窗口那边早就有 termCompact 做同一件事（90-tools.js），这里补齐。
 */
function dtTrimOut() {
  const out = $("dtOut");
  if (!out) return;
  // ⚠️ 先看记账，别直接读 textContent：那是把整棵子树序列化一遍，几十万字符
  // 时就是一次全量拷贝。每块输出都读一次的话，流式省下的时间全吐回去了
  // （这正是主窗口 termCompact 只按 count 判断的原因）。
  if (DT.term.outChars <= DT_OUT_MAX) return;
  const text = out.textContent || "";
  if (text.length <= DT_OUT_MAX) { DT.term.outChars = text.length; return; }
  // 正贴底看最新：重画后继续贴底；往上翻着看历史：保持「离底部多远」
  const gap = out.scrollHeight - out.scrollTop - out.clientHeight;
  const atBottom = gap < 30;
  let cut = text.length - DT_OUT_KEEP;
  // 尽量切在行首，别把一行劈成两半（就近找换行，太远就算了）
  const nl = text.indexOf("\n", cut);
  if (nl >= 0 && nl - cut < 4096) cut = nl + 1;
  const kept = text.slice(cut);
  out.textContent = "";
  // 砍掉的头里可能正好是"设了颜色还没复位"，留下的尾巴就没有配对的 SGR。
  // 从干净状态重来，代价是开头那段没颜色 —— 好过整块串色。
  const st = DT.term.sgr = termSgrNew();
  for (const seg of termAnsiSeg(kept, st)) termAppendSeg(out, seg);
  DT.term.outChars = kept.length;
  if (atBottom) out.scrollTop = out.scrollHeight;
  else out.scrollTop = Math.max(0, out.scrollHeight - out.clientHeight - gap);
}

/** 从一段输出里找 `[工作目录] xxx` 更新 cwd。
 *
 * 常驻 shell 每条命令后的哨兵会带回当前目录（`cd` 之后就是新目录），输入框
 * 提示和 Tab 补全都靠它。**不能只在这一块里 match**：流式会把输出切块，
 * `[工作目录] /home/dev/project` 完全可能被劈成两块，那样一行都认不出来。
 * 所以留 4KB 尾巴跟下一块拼着找 —— 和后端 `_shell_track_cwd` 的做法一致。
 */
function dtTrackCwd(text) {
  const buf = (DT.term.cwdBuf + text).slice(-4096);
  DT.term.cwdBuf = buf;
  const all = buf.match(/\[工作目录\]\s*[^\n]+/g);
  if (!all) return;
  const m = all[all.length - 1].match(/\[工作目录\]\s*([^\n]+)/);
  if (!m) return;
  const dir = m[1].trim();
  // 最后一道闸：值里含 `@`（或「以 / 开头却含 :」）说明这是把提示符整段
  // 当成了路径 —— 老服务端、以及内存里已被污染的那条 shell 都会这么打。
  // 采纳它会让 Tab 补全去 listdir 一个不存在的目录，表现就是「按 Tab 没反应」。
  // 只挡这两种形态：本地 cmd 的 `C:\Users\…` 也含 `:`，不能一并杀掉。
  const bad = dir.includes("@") || (dir.startsWith("/") && dir.includes(":"));
  if (!dir || bad) return;
  if (dir === DT.term.cwd) return;
  DT.term.cwd = dir;
  dtHead();
}

/** 把攒下的 pending 一次性画进 #dtOut（由 rAF 驱动）。
 *
 * 流式一秒能来几十条，每条都动一次 DOM 就是几十次强制同步布局 —— 这正是
 * 主窗口终端当初加 termSchedulePaint 的原因（90-tools.js），这里照抄同一套。
 *
 * 两处额外的限制，都是为了"跑 build 的时候整个任务台不跟着卡"：
 *  · **单帧最多画 `DT_PAINT_MAX` 字符**，剩下的留到下一帧。一次画 64KB
 *    纯文本本身还好，但 build 的输出里大半带 SGR，切出来是上千个 <span>，
 *    一帧塞进去浏览器要排一次又长又贵的版，那一帧里别的什么都不响应。
 *    分帧之后每帧的排版量有上界，滚动条都跟得上手。
 *  · **判贴底不读 `scrollHeight`**：读它会强制把刚 append 的东西先排版一遍。
 *    改成读 scroll 事件维护好的 `atBottom` 缓存（见 dtTrackBottom）。
 */
function dtPaintPending() {
  DT.term.raf = 0;
  const out = $("dtOut");
  const all = DT.term.pending;
  DT.term.pending = "";
  if (!out || !all) return;
  // 这一帧画多少：留一截的话下一帧接着画，观感上就是"刷得快一点"，没人看得出来
  let buf = all;
  if (buf.length > DT_PAINT_MAX) {
    // 别切在多字节字符中间 —— 从末尾往前退到换行处；退太多就算了，直接切
    let cut = buf.lastIndexOf("\n", DT_PAINT_MAX);
    if (cut < DT_PAINT_MAX - 4096) cut = DT_PAINT_MAX;
    DT.term.pending = buf.slice(cut) + DT.term.pending;
    buf = buf.slice(0, cut);
  }
  const st = DT.term.sgr || (DT.term.sgr = termSgrNew());
  for (const seg of termAnsiSeg(termClean(buf), st)) termAppendSeg(out, seg);
  DT.term.outChars += buf.length;
  dtTrimOut();
  if (DT.term.atBottom) {
    out.scrollTop = out.scrollHeight;
    // 贴底跟随之后浏览器会再发一次 scroll 事件，那一次会把 atBottom 重新算成
    // true（值没变），所以这里不用手动维护它。
  }
  if (DT.term.pending && !DT.term.raf) {
    DT.term.raf = requestAnimationFrame(dtPaintPending);
  }
}

/** 维护 `DT.term.atBottom`：用户是不是正看着最底部。
 *
 * 为什么不每次现算：`out.scrollHeight` 是**强制同步布局** —— 浏览器必须先把
 * 刚 append 的内容整个排一遍才能回答。输出快的时候一帧算一次，等于每帧多
 * 一次全量排版（20 万字符的 pre 一次几十毫秒）。scroll 事件里算一次就够：
 * 内容长高不会触发 scroll，但那种情况下"用户原本贴底"这个判断仍然成立。
 */
function dtTrackBottom() {
  const out = $("dtOut");
  if (!out) return;
  DT.term.atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
}


/** 收一条流式数据（字段与 /api/term/tail 完全一致）。 */
function dtOnTail(d) {
  // gone = 这条 job 在服务端已经不在了（进程退了 / 被回收）。这时立刻重连毫无
  // 意义：服务端只会再回一条 gone，`retryAt = 0` 就等于每秒撞一次空流。
  // 退避 30s 兜住；期间用户发新命令会走 dtEnsureShell 开新 job，那里会清这个闸。
  if (d.gone) { DT.term.retryAt = Date.now() + 30000; return; }
  const out = $("dtOut");
  // 常驻 shell 的 running 恒为 true（进程还活着），状态要看 busy：
  // 有没有命令在跑。一次性命令反之 —— 只认 running（老服务端的 busy 结束后
  // 没复位，会把"已结束"显示成"一直在跑"）。
  const busy = d.shell
    ? ((d.busy === undefined) ? !!d.running : !!d.busy)
    : !!d.running;
  const runningChanged = DT.term.running !== busy;
  DT.term.running = busy;
  if (d.reset && out) {
    // reset 意味着"前面那段已经被砍掉、你手上的全作废"：没画的也一起丢
    DT.term.pending = "";
    out.textContent = "";
    DT.term.outChars = 0;
    DT.term.sgr = null;               // 清屏就得重置颜色状态，否则接着上一段串色
    DT.term.cwdBuf = "";
    DT.term.atBottom = true;          // 已经清空了，自动跟随照旧
  }
  if (d.text) {
    dtTrackCwd(d.text);              // 目录解析用**原始**文本：染色不影响 `[`
    DT.term.pending += d.text;
    if (!DT.term.raf) DT.term.raf = requestAnimationFrame(dtPaintPending);
  }
  // 不能用 `d.off || DT.term.off`：off 的合法值可以是 0（刚开出来、还没输出的
  // shell），用 || 会把 0 当成"服务端没给"而沿用旧值。旧值一旦大过服务端累计量，
  // `_term_job_tail` 里 `text[off - start:]` 取到空串，那边就不再有 text 可发，
  // 这条流从此只会空转心跳 —— 界面上就是"输出停在半途，后面永远不来"。
  // reset 也不能靠 ||：reset 那条 data 的 text 可能为空，`d.text` 为假照样要吃
  // reset（下面单独判断）。
  if (typeof d.off === "number") DT.term.off = d.off;
  if (runningChanged) dtHead();
}

/** 停掉当前的常驻流（切会话 / 切终端 / job 没了 / 断线重连）。
 *
 * `keepPending`：断线重连（同一条 job）传 true —— **已经从流里收到、但还没画到
 * 屏幕上的那一段必须留着**。`DT.term.off` 已经越过它了，服务端按累计偏移取增量，
 * 绝不会再发第二次；这里清掉就等于永久丢字。大输出时 pending 里随时压着几万
 * 字符（DT_PAINT_MAX 一帧只画 32KB），丢一次就正好是用户看到的
 * "刷到一半就没了，跑不到底"（见 md/任务台终端刷新没跑到底.md）。
 * 切终端 / 切会话才真的该清：那两处输出区本来就要整体重建。
 */
function dtStopTail(keepPending) {
  const cur = DT.term.stream;
  DT.term.stream = null;
  if (cur) { try { cur.abort.abort(); } catch (e) { /* 已经断了 */ } }
  if (!keepPending) {
    DT.term.pending = "";
    if (DT.term.raf) { cancelAnimationFrame(DT.term.raf); DT.term.raf = 0; }
  }
  // 保留 pending 时连 raf 一起留着：那一帧照常把尾巴画完，off 与屏幕才对得上
}

/**
 * 确保当前选中的终端有一条在跑的常驻流（没有就连一条）。
 *
 * 为什么改成常驻流：以前是每 1s 拉一次 `/api/term/tail`，节奏由 dtNextDelay()
 * 定。build 这类输出是一块一块跳出来的，命令结束后还要多等最多 3s 才看到尾巴，
 * 和主窗口那条 SSE 终端的观感差得远 —— 同一个界面上两套节奏，一眼就看出
 * "任务台不流畅"。
 *
 * 这条流自己会推进，所以 tick 每拍调它只是"确认"：已经在连就什么都不做。
 * 断开（服务端重启 / 网络抖动 / job 被回收）后，只要还看着这条终端就 1s 后
 * 自动重连；`retryAt` 是退避闸，别让 tick 每拍都去撞一个坏连接。
 */
function dtEnsureTail() {
  const job = DT.term.job;
  if (!job || job === DT.ownJob) return;
  const cur = DT.term.stream;
  if (cur && cur.job === job && !cur.abort.signal.aborted) return;
  // 同一条 job 走到这里 = 上一轮流自己结束了（断线 / 服务端重启 / 心跳超时）：
  // 这是"重连"，未画完的 pending 必须带过去（见 dtStopTail 的说明）。
  if (cur) dtStopTail(cur.job === job);
  if (DT.term.retryAt && Date.now() < DT.term.retryAt) return;
  DT.term.retryAt = 0;
  const abort = new AbortController();
  const mine = { job: job, abort: abort };
  DT.term.stream = mine;
  (async () => {
    try {
      const resp = await fetch(
        "/api/term/shell/stream?job=" + encodeURIComponent(job)
          + "&off=" + DT.term.off,
        { headers: { "Authorization": "Bearer " + S.token }, signal: abort.signal });
      if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
      await readSSE(resp.body, (d) => {
        // 中途切走了：这条流的数据一律丢弃（切回来会新起一条，off 从 0 起）
        if (DT.term.stream !== mine) return;
        dtOnTail(d);
      });
    } catch (e) {
      /* abort / 断网 / 404 都落这里，统一在下面决定要不要重连 */
    }
    if (DT.term.stream === mine) DT.term.stream = null;
    if (abort.signal.aborted) return;
    if (DT.term.job === job) DT.term.retryAt = Date.now() + 1000;
  })();
}

/**
 * 单键直通：把 `^C` / `ESC` **原样**送进远程 PTY（`raw: true`）。
 *
 * 为什么必须有这条路：远程终端是真 PTY，整行发送的 `dtRunCmd` 在 nano / vim /
 * less 占着前台时，只等于往它们的编辑缓冲区里打字 —— 用户实测 `git log -2`
 * 被写进了提交信息正文，标题变成 `Modified`。要打断这类程序只能发裸字节。
 *
 * 失败**不重建 shell**：raw 只是打断/回显，重建会把 cd 和环境变量一起丢掉，
 * 比"这次没发出去"糟得多。
 */
async function dtRawKey(data) {
  let id = DT.term.job;
  if (!id) id = await dtEnsureShell();
  if (!id) return false;
  try {
    await api("/api/term/shell/input", { id: id, text: data, raw: true });
    return true;
  } catch (e) {
    return false;
  }
}

/**
 * 敲一条命令：写进常驻 shell 的 stdin，输出由常驻流（dtEnsureTail）推回来。
 *
 * 不再走 /api/exec（那是"一条命令一个进程"），所以 `cd`、`set`、环境变量
 * 都留在同一个 cmd.exe 里 —— 下一条命令还在新目录。
 */
async function dtRunCmd() {
  const ip = $("dtCmd");
  const cmd = (ip ? ip.value : "").trim();
  // 空回车：`git diff .` / `less` / `pause` 这类程序停在分页器里等按键时，
  // 用户按 Enter 就是想喂一个空行进去。以前这里直接 return，一个字节都发不出去，
  // 界面上的观感就是"按了没反应"，只能点停止把整条 shell 杀掉。
  // 只在「这条终端有活干」时放行；空闲时空 Enter 仍然什么都不做
  // （否则每次都发一行空命令，还会多冒一个提示符）。
  const blank = !cmd;
  if (blank && !(DT.term.job && DT.term.running)) return;
  if (ip) {
    ip.value = "";
    // 清空后要重新量高度：不然 textarea 停在"装了五行"的高度，留一大块空白
    if (typeof autoGrowBox === "function") autoGrowBox(ip);
  }
  if (!blank) {
    dtHistPush(cmd);        // 进历史（和主窗口终端共用同一份）
    dtHistIdx = -1;
  }

  let id = DT.term.job;
  if (!id) id = await dtEnsureShell();
  if (!id) return;
  // 确认常驻流连着：刚开出来的 shell、或上一次连断了，都要在这一下发出去
  // 之前接上，否则第一批输出要等到下一拍 tick 才会连（观感是"命令跑了但没反应"）。
  dtEnsureTail();

  const out = $("dtOut");
  // 管道模式没有提示符，自己回显一行，不然看不出哪条命令对应哪段输出。
  // 多行粘贴时**每行都带 `>` 前缀**：后端是按行发的（_shell_send 拆 \n），
  // 回显也得按行，否则浏览器把整段折成视觉换行，分不清到底发出去几条。
  const lines = cmd.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  // 回显必须走 dtWriteOut：`#dtOut` 里现在全是 <span>（SGR 染色分段），
  // 直接 `textContent +=` 会把所有 span 拍平成纯文本 —— 之前跑出来的颜色全丢，
  // 而且整块几百 KB 重新序列化一遍，长输出下会卡。
  // 空回车**不回显**：那行 `> ` 后面直接换行，等于一条空消息，纯噪声。
  //   · 分页器（`git diff .` / `less` / `pause`）收到空行后自己会往下翻一屏，输出照常回来；
  //   · 发没发出去由 `dtHead()` 的「运行中」反馈，不靠这行字；
  //   · 主窗口终端的 Enter 分支（`90-tools.js:1759` 的 `if (!cmds.length) return`）
  //     本来就不回显，改完两个窗口口径一致。
  // 之前那版刻意回显 `"> \n"`，是怕用户看不出到底发出去没有 —— 实测下来只多冒一行空行。
  if (out && !blank) {
    dtWriteOut(lines.map((l) => "> " + l + "\n").join("") +
               (lines.length > 1 ? "[一次发送 " + lines.length + " 条]\n" : ""));
  }
  DT.term.running = true;
  dtHead();

  // 空回车必须送一个**真正的换行**（不是空串）：后端 _shell_send 靠它判断
  // "喂一个空行给 stdin"，空串会在入口被判成缺少参数。
  const send = async (target) =>
    api("/api/term/shell/input", { id: target, text: blank ? "\n" : cmd });

  try {
    const r = await send(id);
    if (r && r.ok === false) {
      // 后端预检拒发（引号/括号没闭合、行尾是续行符 ^）：**不是**终端没了。
      // 绝不能走下面那条「重建 shell 再发一次」—— 同一段坏命令会被再拒一次，
      // 用户看到的就是提示刷两遍。这里只把原因打出来，把运行态复位。
      DT.term.running = false;
      dtHead();
      if (out) dtWriteOut((r.error || "这一条没有执行") + "\n");
      dtLoadItems();
      return;
    }
  } catch (e) {
    // 进程被别的窗口关了 / 自己退了：重建一个再发一次
    DT.term.job = "";
    DT.term.off = 0;
    id = await dtEnsureShell();
    if (id) {
      try {
        await send(id);
      } catch (e2) {
        if (out) dtWriteOut("[发送失败] " + (e2.message || e2) + "\n");
      }
    } else if (out) {
      dtWriteOut("[发送失败] " + (e.message || e) + "\n");
    }
  }
  dtLoadItems();
}

/**
 * 删除一个会话：**软删除**，不弹确认。
 *
 * 后端只是把 `sessions/<sid>.json` 挪进 `sessions/old/` —— 列表里立刻消失，
 * 文件还在，想捞回来自己去文件夹拿。所以这里不拦一道 confirm：拦了反而像"真删"。
 */
async function dtDeleteSession(item) {
  if (!item) return;
  try {
    await api("/api/sessions/delete", { id: item.id, ws: item.ws }, { ws: item.ws });
    if (DT.key === dtKey("chat", item.ws, item.id)) {
      DT.key = "";
      DT.chat.sid = "";
      DT.chat.ws = "";
      DT.chat.ws = "";
      // 会话被删了，它那条流程轨迹也没有归属了，跟着清掉
      DT.chat.stageKeep = null;
      DT.chat.stageSig = "";
      const sb = $("dtStage");
      if (sb) { sb.classList.add("hidden"); sb.innerHTML = ""; }
      const log = $("dtLog");
      if (log) log.textContent = "";
      const cb = $("dtCompact");
      if (cb) cb.textContent = "压缩";
      dtHead();
      boardPane("chat");
    }
    DT.sig = "";
    await dtLoadItems();
    toast("已挪到 sessions/old（文件还在）");
  } catch (e) {
    toast("删除失败：" + e.message, true);
  }
}

/**
 * 复制一段文本到剪贴板（任务台自己用）。
 *
 * 内核没有全局的 copyText —— adb / svn 两个插件各带一份私有的（见
 * `plugins/adb_tools/web/logcat.js` 的 copyText、`plugins/svn_tools/web/svn.js`
 * 的 svnCopyText）。任务台在**内核**这一层，不该依赖某个插件是否启用，
 * 所以这里再写一份最小的：有 clipboard API 就走它，没有（老浏览器、
 * 非安全上下文）退回临时 textarea + execCommand，两种都失败就直说，
 * 别假装复制成功了。
 */
function dtCopyCmd(text) {
  const t = String(text == null ? "" : text).trim();
  if (!t) { toast("这条没有命令记录，没东西可复制", true); return; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t)
      .then(() => toast("已复制"))
      .catch(() => toast("复制失败，请手动选中复制", true));
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = t;
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  toast(ok ? "已复制" : "复制失败，请手动选中复制", !ok);
}

/**
 * 重新运行这条常驻终端最后跑过的那条命令。
 *
 * 命令来自**服务端**（`_term_job_last_cmd` 在往 shell 写命令时记的），所以
 * 别的窗口、甚至上次刷新之前在这台终端里跑过的也能重跑 —— 只记在前端的话，
 * 换个窗口就不知道了。
 *
 * 执行复用 dtRunCmd 那条通道：回显、历史、shell 掉了自动重建，行为完全一致。
 */
async function dtRerunLast(item) {
  const cmd = String((item && item.lastCmd) || "").trim();
  if (!cmd) { toast("这条终端还没跑过命令"); return; }
  await dtSelect(item);            // 先切过去，DT.term.job 才指向这台终端
  const ip = $("dtCmd");
  if (ip) ip.value = cmd;
  await dtRunCmd();
}

/**
 * 重新运行一条**一次性命令**（列表里那种 `gradlew.bat …` 的记录）。
 *
 * **原地重跑**：复用这条记录自己的 job id。后端 `_term_job_begin` 会把账本里
 * 那一条重置（清空输出、t0、seq 归零），所以列表里还是那一行、输出从头再来 ——
 * 以前每次开新 id，右键三次就多出三行 `gradlew.bat …`，看着像点了三次构建。
 *
 * 走和主窗口终端同一个接口 `/api/exec`，输出照旧由 `/api/term/tail` 轮询。
 * 但响应流必须**读空**：不读的话后端往 SSE 写会被 TCP 背压卡住，命令看着像
 * 卡死不动。这里用 readSSE 空回调把流抽干，界面上一行都不画。
 */
async function dtRerunOnce(item) {
  const cmd = String((item && item.cmd) || "").trim();
  if (!cmd) { toast("这条命令已经不在记录里了"); return; }
  if (item.running) {
    // 复用同一个 job id 时不能和旧那轮重叠：旧进程的收尾会把新记录的
    // running/busy 复位、退出码写成旧的。要重跑先让它停。
    toast("这条还在跑，先停止再重跑", true);
    return;
  }
  const ws = (item && item.ws) || DT.term.ws || S.workspace || "";
  const cwd = (item && item.cwd) || ws;
  const job = item.id;              // 复用：原地刷新，不再新增一行
  try {
    const resp = await fetch("/api/exec", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + S.token,
      },
      body: JSON.stringify({ cmd, cwd, ws, job }),
    });
    if (!resp.ok || !resp.body) {
      const t = await resp.text().catch(() => "");
      toast("重新运行失败：" + (t || ("HTTP " + resp.status)), true);
      return;
    }
    readSSE(resp.body, () => {}).catch(() => {});
    DT.sig = "";
    await dtLoadItems();
    const fresh = DT.items.find((x) => x.kind === "term" && x.id === job);
    if (fresh) await dtSelect(fresh);
    else toast("已提交，刷新一下列表");
  } catch (e) {
    toast("重新运行失败：" + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// 停止 / 关闭 / 启动
// ---------------------------------------------------------------------------
async function dtStop() {
  const item = DT.items.find((r) => dtKey(r.kind, r.ws, r.id) === DT.key);
  if (!item) return;
  // 标题栏那颗 ■ 和列表里那颗 ■ 走同一条路（会话 → 停生成；终端 → 停命令）。
  // 以前这里硬编码 /api/term/shell/kill，只会整壳杀，一次性命令反而停不掉。
  if (item.running) await dtStopItem(item);
  else dtHead();
}

/**
 * 下一拍隔多久（毫秒）。
 *
 * 上一版是写死的 1000ms：不管有没有东西在动，每秒都重拉一遍左列表 + 当前会话
 * 正文。跑几个会话时这一秒就是十几条短连接（服务端 HTTP/1.0，每条一个新 TCP），
 * 界面越用越涩。现在按"有没有东西在动"分档：
 *   · 有会话在跑 / 有题在等答 → 1s（耗时秒数在涨、正文在长，用户正盯着看）
 *   · 空闲 → 3s（只用来发现"别处又开跑了"）
 *   · 窗口在后台 → 5s（没人看）
 */
function dtNextDelay() {
  if (document.hidden) return 5000;
  const live = !!DT.chat.running || DT.items.some((r) => r.running || r.ask);
  return live ? 1000 : 3000;
}

/** 自排下一拍（用 setTimeout 而不是 setInterval：每拍之间按当时的忙闲重新定时）。 */
function dtSchedule(ms) {
  if (DT.timer) { clearTimeout(DT.timer); DT.timer = 0; }
  const delay = (ms === undefined) ? dtNextDelay() : ms;
  DT.timer = setTimeout(async () => {
    DT.timer = 0;
    if (!DT.on) return;          // 窗口关了就不再排（重开时 dtBuild 会重新起）
    try { await dtTick(); } catch (e) { /* 这一拍失败无所谓，下一拍照旧 */ }
    if (!DT.on) return;
    dtSchedule();
  }, delay);
}

async function dtTick() {
  // 防重入：上一拍还没回来（网络慢 / 服务端正忙）就跳过这一拍。
  // 以前是裸的 setInterval(1000)，没有这个判断 —— 服务端一忙，请求就一拍接
  // 一拍地叠上去，越忙越叠、越叠越忙（每轮还是一条新 TCP）。
  if (DT.busy) return;
  DT.busy = true;
  try {
    await dtTickInner();
  } finally {
    DT.busy = false;
  }
}

async function dtTickInner() {
  // 兜底：窗口启动那一刻漏掉的广播（主窗口在订阅前发的）会躺在队列里
  dtTakeAttachAll();
  // 左列表和当前会话正文**并行发**：两者互不依赖（dtPollChat 只认
  // DT.chat.sid / ws，而这两样只会在下面那条附件分支的 dtSelect 里变，
  // 那时这份 chat 结果早就回来了）。串行时一拍耗时 = 两段之和，服务端一忙
  // （几个会话同时生成、抢 GIL）就拖成几百毫秒，观感是一顿一顿。
  const pItems = dtLoadItems();
  // 只在这个窗口选中的是会话时才先发正文：选中终端时 DT.chat.sid 还留着
  // 上一个会话的值，照发就是一条无用的请求。
  const pChat = DT.key.startsWith("chat|") ? dtPollChat() : null;
  await pItems;
  // 附件先到了、但当时列表还没加载出来（选不了会话）：补一次自动选中。
  // 优先按附件**所属工作区**选第一个会话（见 dtWsOfPath），否则退回全局第一个。
  if (S.attachments.length && !DT.key.startsWith("chat|")) {
    const fromWs = dtWsOfPath(
      (S.attachments.find((a) => a && a.path) || {}).path || "");
    const pick = (fromWs
      ? DT.items.find((r) => r.kind === "chat"
        && dtWsKey(r.ws) === dtWsKey(fromWs))
      : null) || DT.items.find((r) => r.kind === "chat");
    if (pick) { await dtSelect(pick); return; }
  }
  const item = DT.items.find((r) => dtKey(r.kind, r.ws, r.id) === DT.key);
  if (item && item.kind === "term") {
    // 已经发出去的那条正文请求还是要等回来（它按 myKey 自校验，切走了就丢弃），
    // 不能留一个没人 await 的 Promise：DT.busy 会被提前放开，下一拍就叠上来。
    if (pChat) await pChat;
    // 本窗口自己那条命令的输出走 SSE，不要再 tail 一遍（会重复）。
    // 常驻终端这条：dtEnsureTail 只是"确认流还连着"，不连才建 —— 输出不再
    // 依赖这一拍的节奏（以前是这里 await 一次 /api/term/tail，最快 1s 一批）。
    if (item.id !== DT.ownJob) dtEnsureTail();
  } else if (pChat) {
    await pChat;
  } else {
    dtHead();
  }
}

/** enterApp 末尾调用：把界面切成"只有任务台"。 */
function openDetachedBoard() {
  if (!dtOn() || DT.on) return;
  const el = $("board");
  if (!el) return;
  DT.on = true;
  // 标题必须和主窗口区分开：后端和托盘靠"Chrome_WidgetWin_1 + 标题前缀"
  // 认那个 --app 主窗口，而任务台的页面标题本来一模一样。于是「⌂」找到的
  // 是**自己这个窗口**，把它提到前台 —— 它本来就在前台，看着就是"点了没
  // 反应"，还一本正经地报告成功。
  // ⚠️ 这里的 "FREE_AI" 前缀和 "TASKS" 标记都是**硬耦合**：
  // web_agent.py 的 _APP_WINDOW_TITLE / tray.py 的 APP_TITLE 认前缀，
  // 后端和托盘都靠 "TASKS" 把任务台和主窗口区分开。改一处而漏另一处
  // 不报错，症状是「⌂ 点了没反应」「托盘双击每次新开一个窗口」。
  // 全大写是用户指定的显示风格（与 FREE_AI 一致），匹配是区分大小写的。
  document.title = "FREE_AI · TASKS";
  el.classList.remove("hidden");
  dtBuild();
  boardPane("chat");

  // 「＋」= 菜单两项（本地目录 / 远程 SSH），与主工作台的 #wsAdd 口径一致。
  // stopPropagation 见 dtAddMenu 的注释：document 上的 hideCtx 会把菜单收掉。
  const addRemote = $("bdAddRemote");
  if (addRemote) {
    addRemote.classList.remove("hidden");
    addRemote.addEventListener("click", (ev) => {
      ev.stopPropagation();
      dtAddMenu(ev.currentTarget);
    });
  }
  // 主界面入口：主窗口被关掉之后，任务台这边没有任何回去的路
  // （之前只能自己敲 http://127.0.0.1:<端口>/ 或翻收藏夹）。
  const mainBtn = $("bdMain");
  if (mainBtn) {
    mainBtn.classList.remove("hidden");
    mainBtn.addEventListener("click", async () => {
      const note = (msg, data) => {
        try { if (typeof rtLog === "function") rtLog("board.home", msg, data); } catch (e) { /* 忽略 */ }
      };
      // opener 就是打开本窗口的那个主窗口。有就顺手试一次 —— 但**绝不就此
      // return**：Chrome 对跨窗口 focus 经常是"调用成功、窗口纹丝不动"，
      // 提前 return 的结果就是用户点了没反应，而真正有效的后端那条路没走。
      let openerOk = false;
      if (window.opener && !window.opener.closed) {
        try { window.opener.focus(); openerOk = true; } catch (e) { /* 跨源，忽略 */ }
      }
      // 让主窗口自己 focus 一次：同源广播一定能到，它聚焦自己通常被允许
      if (typeof BOARD_CH !== "undefined" && BOARD_CH) {
        try { BOARD_CH.postMessage({ type: "board-focus" }); } catch (e) { /* 忽略 */ }
      }
      // 真正能生效的那条：后端用 Win32 枚举窗口 + 借前台资格提上来。
      // 必须给个 body —— api() 没 body 就发 GET，而这接口只在 POST 分支上。
      let r = null;
      try {
        r = await api("/api/app/focus", {});
      } catch (e) {
        note("接口失败", { err: String((e && e.message) || e) });
      }
      note("结果", { opener: openerOk, ok: !!(r && r.ok) });
      if (r && r.ok) {
        if (typeof toast === "function") toast("已切回主窗口");
        return;
      }
      if (openerOk) {
        // 主窗口确实开着，只是系统不让抢前台：别再多开一个标签页，
        // 让用户点任务栏那一下更省事。
        if (typeof toast === "function") toast("主窗口在后台，请点一下任务栏", true);
        return;
      }
      // 连引用都没有（主窗口被关了 / 手动打开的任务台）：只能新开。
      window.open("/", "agent-main");
    });
  }
  $("bdClose").addEventListener("click", () => window.close());
  $("bdStop").addEventListener("click", () => dtStop());
  // 标题栏「md 目录」：打开这条会话的 md 归档目录（.ai/history）。
  // ws 由 dtHead 写在 dataset 上（每轮都会刷新），这里只负责转给 dtOpenMdDir。
  const revealBtn = $("bdReveal");
  if (revealBtn) {
    revealBtn.addEventListener("click", () => {
      dtOpenMdDir(revealBtn.dataset.ws || "");
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !e.isComposing) window.close();
  });

  // 主窗口「加入对话」→ 广播立刻到（队列是窗口还没加载完时的兜底）。
  // 先订阅再读队列：这中间的广播能收到，队列里重复的靠 DT.seen 去重。
  if (typeof BOARD_CH !== "undefined" && BOARD_CH) {
    BOARD_CH.onmessage = (e) => {
      const d = e.data || {};
      if (d.type === "attach") {
        dtAddAttach([d.item]);
        boardTakeAttach();          // 广播已送达，队列里那份没用了
      } else if (d.type === "board-ping") {
        try { BOARD_CH.postMessage({ type: "board-pong" }); } catch (err) { /* 忽略 */ }
      } else if (d.type === "board-close") {
        try { window.close(); } catch (err) { /* 浏览器不让关就留着 */ }
      } else if (d.type === "board-focus") {
        try { window.focus(); } catch (err) { /* 忽略 */ }
      } else if (d.type === "model-changed") {
        // 主窗口切了模型（顶栏那个下拉）→ 本窗口跟着重画
        S.model = d.model || S.model;
        S.endpoint = d.endpoint || "";
        dtRenderModel();
      } else if (d.type === "reveal-tree-ack") {
        // 主窗口说它在文件树里有没有定位到（fail 时本地再退回资源管理器）
        const cb = DT_REVEAL_WAITS.get(d.id);
        if (cb) cb(d.ok ? "ok" : "fail");
      } else if (d.type === "open-file-ack") {
        // 主窗口告诉我们那个文件到底开没开
        const timer = DT_OPEN_WAITS.get(d.id);
        if (timer) {
          clearTimeout(timer);
          DT_OPEN_WAITS.delete(d.id);
        }
        if (d.ok) {
          toast("已在主窗口打开：" + baseName(d.path || ""));
        } else {
          toast("打开失败：" + (d.error || "未知原因"), true);
        }
      }
    };
    // 告诉主窗口"我活着"（主窗口可能比我们晚加载，它会自己发 ping）
    try { BOARD_CH.postMessage({ type: "board-hello" }); } catch (err) { /* 忽略 */ }
  }
  // 关窗口时告诉主窗口把按钮熄灭。beforeunload 在点 ✕ / Esc / window.close 时都会走。
  window.addEventListener("beforeunload", () => {
    try {
      if (typeof BOARD_CH !== "undefined" && BOARD_CH) {
        BOARD_CH.postMessage({ type: "board-bye" });
      }
    } catch (e) { /* 忽略 */ }
  });
  dtTakeAttachAll();
  dtHead();          // 先把标题栏画出来（含「目录」按钮的显隐），别等第一次轮询

  // 切回前台时立刻补一拍：后台是 5s 一拍，回来的第一秒不该还看着旧数据
  document.addEventListener("visibilitychange", () => {
    if (!DT.on || document.hidden) return;
    dtSchedule(0);
  });
  dtTick().catch(() => {});
  dtSchedule();
}
