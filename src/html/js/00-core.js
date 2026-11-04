// 状态 / 请求封装 / 工具函数
// 从 index.html 拆出（原第 1039-1128 行）。
"use strict";

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------
const S = {
  token: localStorage.getItem("agent_token") || "",
  // 鉴权代次：每次「换 token / 清 token」都 +1。
  // 服务重启时页面上有 5~6 个在途请求会同时收到 401，它们各自都想去 logout。
  // 光靠「S.token 非空就 logout」不够：用户输完密码拿到新 token 之后，
  // 某个在重启瞬间挂起的旧请求才姗姗返回 401，此时 S.token 是有值的，
  // 于是又把刚拿到的新 token 清掉、再弹一次登录框 —— 用户看到的就是
  // 「密码要输好几次」。改成发请求时记下代次，401 回来时代次已变就忽略。
  authEpoch: 0,
  model: "",
  models: [],
  // ---- 模型端点（自定义网关）----
  // 见 src/agent_endpoints.py：一个端点 = 一套 base_url + 它自己的模型名。
  // 模型名属于某个网关，切模型必须连端点一起切（下拉的 value 是 `端点id::模型`）。
  endpoints: [],
  endpoint: "",          // 当前选中的端点 id，"" = 默认网关
  // ---- 智能体（模式）----
  // 清单来自后端 src/modes/*.json（GET /api/modes）。前端**不认识**任何具体
  // 模式名：加一个模式 = 往那个目录丢一个 json，刷新页面就多一个按钮。
  agents: [],
  agentDefault: "",      // 没指定时用哪个（后端 DEFAULT_ID）
  // 服务端能力开关（/api/config 的 caps）：决定某些菜单项显不显示。
  // 必须问服务端而不是看浏览器 —— 远程访问时两端系统可能不一样，
  // 而这些能力（剪贴板、资源管理器）是在**服务端那台机器**上执行的。
  caps: {},
  // 界面记忆开关（/api/config 的 remember_ui，全局存 .ai/ui.json）：
  // false = 不读也不写各工作区的 .ai/save.json（见 10-login.js / 20-workspace.js）。
  // 这里先默认 true —— 配置请求失败时宁可照旧恢复布局，也不能因为一次网络
  // 错误就把用户的树根、终端、标签全清空。
  rememberUi: true,
  workspace: "",         // **活动根**：会话 / 记忆 / 缓存都跟着它
  trees: [],             // 打开的文件树（可以多棵）
  // 工作区别名：path -> 用户起的显示名（为空/没有该键 = 用末级目录名）。
  // 两个末级同名的目录（两个 gp）靠它区分；只影响显示，路径不变。
  labels: {},
  memoryPath: "",
  sessions: [],
  activeSid: "",
  treeRootPath: "",
  showHidden: false,     // 文件树里显不显示以 . 开头的（.ai / .git …），记住到 .ai/save.json
  attachments: [],       // 待加入对话的文件
  autoScroll: true,      // 是否自动跟随滚动
  wxTimer: null,

  // ---- 多标签页会话 ----
  // 每个会话一个独立面板（DOM + 运行态），切换只是显示/隐藏，不重建。
  // 这样 A 标签在生成时切到 B 发消息，A 的流不会被打断，回复也不会串会话。
  // key 是「会话 id @ 工作区」：两个工作区里都有 s1 这种同名会话时，
  // 只按 sid 做 key 会互相覆盖面板和运行态。
  tabs: {},              // "sid@ws" -> { key, sid, ws, box, input, ... }
  tabOrder: [],          // 打开顺序（决定标签栏排列）

  // ---- 两个"表"的分工（用户要求：关掉的会话不许自己再冒出来）----
  // sessions   = **历史会话表**：后端 store.list_meta() 给的全量，只用于会话弹窗
  //              「全部会话」那半屏；它**不驱动标签栏**。
  // tabs/tabOrder = **当前会话表**：真正开着的标签，标签栏只画这一批。
  // 持久化：tabOrder 按工作区存进 <工作区>/.ai/save.json 的 openTabs 字段，
  // 刷新/重登/切工作区时只恢复这一批。早先 openAllTabs() 无脑把所有会话
  // 都建成标签，于是"叉掉的标签一刷新又全回来了" —— 就是这个原因。
  // 恢复用的键是纯 sid（不带 @ws），因为 save.json 本身就是按工作区存的。
  // null = 「还没从 save.json 读到过」，和「读到了但是空数组（用户全关光了）」
  // 语义不同：null 才允许兜底打开后端 active 那个会话，空数组必须一个都不开。
  openTabs: null,        // 上次开着的 sid 列表（null=未加载 / []=用户关光了）
};


const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// 请求封装
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// 运行日志
//
// 目标：用户说"点了没反应"时，不用来回问"你点的是哪、有没有报错" ——
// 关键动作直接写进 <应用根>/.ai/runtime.log，看一眼就知道断在哪。
// fire-and-forget：失败静默，日志不能影响业务。
// ---------------------------------------------------------------------------
// 未登录（登录页）时也要能记 —— 否则"输密码没反应"这类问题永远没有痕迹。
// 只放行下列前缀，其余照旧拦掉，避免登录前产生大量噪声。
// 后端 /api/log/client 对无 token 请求另有限流（见 web_agent._handle_client_log）。
const RT_LOG_ANON = ["boot.", "login.", "js.error", "js.reject"];

function rtLog(kind, msg, data) {
  try {
    const k = String(kind || "");
    let anon = false;
    if (!S.token) {
      for (let i = 0; i < RT_LOG_ANON.length; i++) {
        if (k === RT_LOG_ANON[i] || k.indexOf(RT_LOG_ANON[i]) === 0) {
          anon = true;
          break;
        }
      }
      if (!anon) return;
    }
    const headers = { "Content-Type": "application/json" };
    if (S.token) headers["Authorization"] = "Bearer " + S.token;
    fetch("/api/log/client", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ kind: kind, msg: msg, data: data }),
      keepalive: true,
    }).catch(function () { /* 忽略 */ });
  } catch (e) { /* 忽略 */ }
}

// 未捕获的异常 / promise 也进去：很多时候"没反应"是前面某个监听抛了错，
// 用户不会去开 Console，但日志里能看见。
window.addEventListener("error", function (e) {
  rtLog("js.error", String((e && e.message) || e), {
    src: (e && e.filename) || "", line: (e && e.lineno) || 0,
  });
});
window.addEventListener("unhandledrejection", function (e) {
  const r = (e && e.reason) || {};
  rtLog("js.reject", String(r.message || r), {
    stack: String(r.stack || "").slice(0, 800),
  });
});

async function api(path, body, opts = {}) {
  // 每个请求都带上它属于哪个工作区（见 web_agent._resolver）。
  // opts.ws 显式给了就用它；否则用活动根 —— 老调用点一行都不用改。
  // 两个工作区并行时，靠的就是这个参数把请求路由到各自的 .ai/。
  //
  // **显式传空串 `""` = 我知道我不知道，别替我猜**：不回落活动根。
  // 任务台里 DT.chat.ws 为空时靠这条表达"这个会话归属不明"，否则请求会
  // 带上任务台窗口自己的活动根（= gu），后端拿 gu 去过滤 web 的题 ——
  // 正是「web 的选择题弹到 gu」那条链的最后一环（见 dtPollChat 的早退）。
  const ws = (opts && opts.ws === "") ? "" : ((opts && opts.ws) || S.workspace || "");
  if (ws) {
    const sep = path.indexOf("?") >= 0 ? "&" : "?";
    if (!/[?&]ws=/.test(path)) path = path + sep + "ws=" + encodeURIComponent(ws);
    if (body !== undefined && body && typeof body === "object" && !body.ws) {
      body = Object.assign({}, body, { ws: ws });
    }
  }
  // 记下发请求这一刻的鉴权代次，401 回来时用它判断是不是上一轮的旧请求。
  const epoch = S.authEpoch;
  const init = { method: body === undefined ? "GET" : "POST", headers: {} };
  if (S.token) init.headers["Authorization"] = "Bearer " + S.token;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  // 超时：没有它的话，一个卡住的后端请求会让 await 永远挂着 ——
  // 界面上表现就是"点了没反应、按钮一直转"，看着像整页卡死。
  // 插件重载这类重活给宽一点（30s），普通请求 15s。
  // **timeout: 0 = 不设超时** —— 搜索这类"一直跑到用户点停止"的请求用它。
  const timeout = (opts && opts.timeout !== undefined && opts.timeout !== null)
    ? Number(opts.timeout) : 15000;
  const ctrl = (timeout > 0 && typeof AbortController === "function")
    ? new AbortController() : null;
  if (ctrl) {
    init.signal = ctrl.signal;
    setTimeout(() => { try { ctrl.abort(); } catch (e) { /* 已结束 */ } }, timeout);
  }
  if (opts.signal) init.signal = opts.signal;

  // fetch 抛异常 = 网络层就没拿到响应，跟 HTTP 状态码无关。最常见的两种：
  // ① 服务刚重启，内存里的 token 全失效（`Failed to fetch` 其实是 401 被
  //    连接重置吃掉了，见 web_agent._drain_body 的注释）；
  // ② 服务根本没起来。
  // 直接抛 "Failed to fetch" 用户看不出要干什么，这里换成能操作的提示。
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (e) {
    if (e && e.name === "AbortError") {
      if (opts && opts.signal) throw e;    // 用户主动停止，原样抛
      const te = new Error("请求超时（" + Math.round(timeout / 1000) + "s），服务可能卡住了");
      te.timeout = true;
      throw te;
    }
    const err = new Error("连不上本地服务（可能刚重启或没启动），请刷新页面重新登录");
    err.network = true;
    throw err;
  }
  if (resp.status === 401) {
    // 服务重启后所有在途请求会**同时**收到 401（配置/状态/会话/工作区/微信轮询
    // 各一个）。每个都调一次 logout 会把登录框反复重绘，用户看着就是
    // "输对了又弹回来、要多输几次"。第一个登出时会清空 S.token，后面的
    // 看到 S.token 已空就直接抛错，不再碰界面。
    //
    // 但这还不够：用户重新登录拿到新 token 之后，那个在重启瞬间挂起的旧请求
    // 可能才返回 401。此时 S.token 非空（是新 token），下面就会把新的清掉，
    // 于是又要再输一次。所以发请求前记下代次，回来时代次变了说明这是上一轮
    // 的旧请求，直接丢弃，不碰界面。
    if (epoch !== S.authEpoch || !S.token) throw new Error("未授权");
    logout("登录已失效，请重新输入密码");
    throw new Error("未授权");
  }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || ("HTTP " + resp.status);
    const err = new Error(msg);
    err.data = data;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------
function fmtNum(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function fmtMs(ms) {
  const v = Number(ms) || 0;
  return v >= 1000 ? (v / 1000).toFixed(1) + "s" : v + "ms";
}
function fmtSize(b) {
  const v = Number(b) || 0;
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / 1024 / 1024).toFixed(1) + " MB";
}
// 秒级时间戳 → "2026-02-03 14:05"；0 / 空 / 非法一律返回空串（调用方据此不显示）。
// 单位约定：本地 list_dir 与远程 _remote_fs_list 都给**秒**；顺手兼容毫秒
// （> 1e12 视为毫秒），免得以后某一路换了单位就静默错位 41 年。
function fmtTime(ts) {
  const v = Number(ts) || 0;
  if (v <= 0) return "";
  const d = new Date(v > 1e12 ? v : v * 1000);
  if (isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
    + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function shortPath(p, keep = 2) {
  const s = String(p || "").replace(/\\/g, "/");
  const parts = s.split("/").filter(Boolean);
  return parts.length <= keep ? s : ".../" + parts.slice(-keep).join("/");
}
function baseName(p) {
  const s = String(p || "").replace(/\\/g, "/");
  const parts = s.split("/").filter(Boolean);
  return parts[parts.length - 1] || s;
}

/**
 * 工作区显示名：优先用用户起的别名，否则退回末级目录名。
 *
 * 为什么需要它：两个 `gp` 目录在标签上长得一模一样，点错了就是
 * 一整轮会话切到别的仓库去。别名只改显示，**路径不变** ——
 * 真实改目录名要跟着改 .ai/edits、会话归属、fs_roots，一处漏掉
 * 就变成"历史会话凭空消失"，风险远大于收益。
 *
 * 别名表存在后端 .ai/workspace.json（见 /api/workspace 的 labels），
 * 不在 localStorage：换个浏览器打开应该还是同一个名字。
 */
function wsLabel(p) {
  const k = String(p || "");
  const lb = (S.labels || {})[k];
  return String(lb || "") || baseName(k);
}

/**
 * 让「单行文本域」随内容长高 —— 粘贴多行命令时能一眼看全，到 maxPx 出滚动条。
 *
 * 为什么不用 `<input>`：按 HTML 规范 input 的 value **不含换行**，粘贴多行时
 * 浏览器直接把换行剥掉，几行命令会被压成一条超长单行（后端本来是按 `\n` 拆行
 * 发的，等于这段逻辑白写）。textarea 才保得住换行，代价是要自己管高度。
 *
 * 调用点：两个终端的输入框（`#termIn` / `#dtCmd`）的 input 事件，以及命令发出去
 * 清空 value 之后（这时 scrollHeight 缩回去了，得重新量一次，否则留一大块空白）。
 */
function autoGrowBox(ta, maxPx) {
  if (!ta) return;
  const max = maxPx || Math.max(80, Math.round(window.innerHeight * 0.4));
  ta.style.height = "auto";
  const h = Math.min(ta.scrollHeight, max);
  ta.style.height = h + "px";
  ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
}

function toast(msg, isErr) {
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:26px;z-index:200;" +
    "padding:8px 16px;border-radius:8px;font-size:13px;box-shadow:0 6px 20px rgba(0,0,0,.2);" +
    "background:" + (isErr ? "var(--danger)" : "var(--accent)") + ";color:#fff;opacity:0;transition:opacity .18s";
  document.body.appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = "1"; });
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, 2200);
}


// ---------------------------------------------------------------------------
// 通用弹窗（原在 50-views.js，2026 挪到内核：confirmBox / infoBox 是各插件
// 都会用的基础件，塞在某个插件的实现文件里会让别的插件都得依赖它）
// ---------------------------------------------------------------------------
function confirmBox(title, text, okLabel) {
  return new Promise((resolve) => {
    const body = document.createElement("div");
    const hint = document.createElement("div");
    hint.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:4px;" +
      "word-break:break-all;white-space:pre-wrap;line-height:1.6";
    hint.textContent = text;
    body.appendChild(hint);

    const foot = document.createElement("div");
    const sp = document.createElement("span");
    sp.className = "spacer";
    const cancel = document.createElement("button");
    cancel.className = "tbtn";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.className = "tbtn";
    ok.textContent = okLabel || "确定";
    foot.appendChild(sp);
    foot.appendChild(cancel);
    foot.appendChild(ok);

    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      closeModal();
      resolve(v);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") finish(false);
      if (ev.key === "Enter") finish(true);
    };

    ok.addEventListener("click", () => finish(true));
    cancel.addEventListener("click", () => finish(false));
    document.addEventListener("keydown", onKey);

    openModal(title, body, foot);
    setTimeout(() => ok.focus(), 30);
  });
}

// ---------------------------------------------------------------------------
// 投屏 / 提权（搬自 device-file 的 scrcpy 与 rootRemount 两个命令）
// ---------------------------------------------------------------------------

/** 纯提示框（失败时把后端的完整说法摆出来 —— 输出、用的哪个 scrcpy、命令）。 */
function infoBox(title, text) {
  return new Promise((resolve) => {
    const body = document.createElement("div");
    const pre = document.createElement("pre");
    pre.style.cssText = "font-size:12px;color:var(--muted);white-space:pre-wrap;" +
      "word-break:break-all;line-height:1.6;margin:0;max-height:340px;" +
      "overflow:auto;user-select:text";
    pre.textContent = text;
    body.appendChild(pre);

    const foot = document.createElement("div");
    const sp = document.createElement("span");
    sp.className = "spacer";
    const ok = document.createElement("button");
    ok.className = "tbtn";
    ok.textContent = "知道了";
    foot.appendChild(sp);
    foot.appendChild(ok);

    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      closeModal();
      resolve(true);
    };
    const onKey = (ev) => { if (ev.key === "Escape" || ev.key === "Enter") finish(); };
    ok.addEventListener("click", finish);
    document.addEventListener("keydown", onKey);
    openModal(title, body, foot);
    setTimeout(() => ok.focus(), 30);
  });
}

// ---------------------------------------------------------------------------
// 独立窗口模式（`/?detached=log` / `/?detached=board`）
// ---------------------------------------------------------------------------
// Logcat 的「⧉ 独立」会开一个同源新窗口。那个窗口会跑一遍完整的登录态恢复，
// 如果它照常往 /api/state 写 openTabs / editors / terminal，关掉它的时候就会
// 把主窗口的存档覆盖成"空" —— 表现为刷新后标签没了、打开的文件也没了。
// 所以独立窗口只读：所有 /api/state 的**写**请求直接吞掉。
const DETACHED_LOG = /[?&]detached=log(?:&|$)/.test(location.search);
//: 任务台的独立窗口（见 37-detached.js）
const DETACHED_BOARD = /[?&]detached=board(?:&|$)/.test(location.search);

//: 每个页面实例一个随机前缀。终端的 job id 必须跨窗口唯一 —— 两个窗口的
//: 终端标签都叫 t1/t2，只按标签 id 生成会被互相覆盖。
const CLIENT_ID = "p" + Math.random().toString(36).slice(2, 8);

// ---------------------------------------------------------------------------
// 任务台的跨窗口附件通道
//
// 任务台开着时，主界面的会话栏会被藏起来（会话都在任务台窗口里），所以
// 「加入对话」不能只往本窗口的 S.attachments 里塞 —— 那是个看不见的地方。
// 这条路只在任务台开着时才走（判据在 boardRouteAttach）：先广播（窗口开着
// 就立刻到），同时写一份 localStorage 队列（窗口还没加载完 /
// BroadcastChannel 不可用时靠它兜底，启动时读）。任务台没开时附件留在本地。
// ---------------------------------------------------------------------------
const BOARD_CH = (typeof BroadcastChannel === "function")
  ? new BroadcastChannel("agent-board") : null;
const BOARD_QUEUE_KEY = "agent_board_attach";
const BOARD_QUEUE_MAX = 20;

/**
 * 广播"模型变了"（模型是全局的，主窗口和任务台两个下拉是同一个设置）。
 * 两个窗口都会收到并重画；发的一方自己改完状态再发。
 * 不传参数（BroadcastChannel 收不到自己发的）—— 调用方先改自己的 S 再调它。
 */
function boardSendModelChanged(r) {
  try {
    if (!BOARD_CH) return;
    BOARD_CH.postMessage({ type: "model-changed",
                           model: (r && r.model) || S.model || "",
                           endpoint: (r && r.endpoint) || "" });
  } catch (e) { /* 忽略：没有通道时只是另一侧不跟着变，不影响本次切换 */ }
}

/** 主窗口 → 任务台：附上唯一 id，任务台按它去重（广播和队列可能各来一次）。 */
function boardSendAttach(item) {
  if (!item) return;
  const msg = Object.assign({}, item, { _id: CLIENT_ID + ":" + Date.now().toString(36) });
  // 图片是 base64 内联的，动辄几 MB，塞 localStorage 会把配额顶爆。
  // 所以图片只走广播：任务台窗口没开时这张图就丢了（重新粘一次即可）。
  if (item.kind !== "image") {
    try {
      const q = JSON.parse(localStorage.getItem(BOARD_QUEUE_KEY) || "[]");
      const arr = Array.isArray(q) ? q : [];
      arr.push(msg);
      localStorage.setItem(BOARD_QUEUE_KEY, JSON.stringify(arr.slice(-BOARD_QUEUE_MAX)));
    } catch (e) { /* 隐私模式写不进去：那就只靠广播，窗口开着也能到 */ }
  }
  if (BOARD_CH) {
    try { BOARD_CH.postMessage({ type: "attach", item: msg }); } catch (e) { /* 忽略 */ }
  }
}

/** 任务台取走待处理的附件（读走即清，避免下次启动又加一遍）。 */
function boardTakeAttach() {
  try {
    const raw = localStorage.getItem(BOARD_QUEUE_KEY);
    if (!raw) return [];
    localStorage.removeItem(BOARD_QUEUE_KEY);
    const q = JSON.parse(raw);
    return Array.isArray(q) ? q : [];
  } catch (e) {
    return [];
  }
}

// 包一层 api：`/api/state` 的**写**请求在两种情况下一律吞掉。
// 1) 独立窗口：`?detached=log` / `?detached=board` 会跑一遍完整的登录态恢复，
//    它照常写 openTabs / editors / terminal 的话，关掉它就会把主窗口的存档
//    覆盖成"空" —— 表现为刷新后标签没了、打开的文件也没了。所以只读。
// 2) 用户关掉了「记忆界面布局」（⚙ 菜单，S.rememberUi）：关掉就等于不写存档。
//    只关"读"不关"写"是没用的 —— 每开一次页面都会把当前状态写回去。
// 两种都只拦写；带 body 的 GET 不存在，判 body !== undefined 足够。
{
  const _apiWriteGuard = api;
  api = function (path, body, opts) {
    if (body !== undefined && String(path).indexOf("/api/state") === 0
        && (DETACHED_LOG || DETACHED_BOARD || S.rememberUi === false)) {
      return Promise.resolve({});
    }
    return _apiWriteGuard(path, body, opts);
  };
  if (DETACHED_LOG) document.body.classList.add("detached-log");
  if (DETACHED_BOARD) document.body.classList.add("detached-board");
}

// ---------------------------------------------------------------------------
// 界面缩放：每个窗口各存各的
// ---------------------------------------------------------------------------
// 浏览器自带的 Ctrl+滚轮缩放是**按站点**记的。主窗口和独立 Logcat / 任务台
// 同源（同一个 127.0.0.1:端口），所以调一个另一个跟着变，也没法分开调 ——
// 这正是"两个窗口同时变大变小"的来源。
//
// 这里把缩放接过来自己做：拦掉 Ctrl+滚轮，用 CSS `zoom` 只作用于本窗口，
// 比例存 localStorage 且 key 带上窗口类型，三个窗口互相独立。
//
// 只拦滚轮，不拦键盘：Chrome 的 Ctrl+加号/0 是浏览器级快捷键，网页
// preventDefault 不了，硬做只会得到一个"有时灵有时不灵"的体验。
const UI_SCALE_KEY = "ai.uiScale"
  + (DETACHED_LOG ? ":log" : (DETACHED_BOARD ? ":board" : ""));

let UI_SCALE = 1;
try {
  UI_SCALE = Number(localStorage.getItem(UI_SCALE_KEY)) || 1;
} catch (e) { /* 隐私模式：不记就不记，本次仍然能调 */ }

function applyUiScale(v) {
  const s = Math.round(Math.min(2, Math.max(0.6, Number(v) || 1)) * 100) / 100;
  UI_SCALE = s;
  // 100% 时把样式删掉，别在 DOM 上留个 zoom:1 —— 有些浏览器对它有额外合成层
  document.documentElement.style.zoom = (s === 1) ? "" : String(s);
  try { localStorage.setItem(UI_SCALE_KEY, String(s)); } catch (e) { /* 忽略 */ }
}

applyUiScale(UI_SCALE);

// passive:false 必须显式给：默认的 passive 监听里调不了 preventDefault，
// 浏览器会照旧缩放整个站点，等于这段白写。
document.addEventListener("wheel", (ev) => {
  if (!ev.ctrlKey) return;
  ev.preventDefault();
  // 用 deltaY 的方向而不是数值：不同设备（触控板 / 滚轮）一次差 1 或 100 都有
  applyUiScale(UI_SCALE + (ev.deltaY < 0 ? 0.1 : -0.1));
}, { passive: false });

// ---------------------------------------------------------------------------
// 终端 Tab 补全（**唯一**一份实现，两个终端共用）
// ---------------------------------------------------------------------------
// 界面上有两个终端，执行模型不同，但补全不该有两套行为：
//
//   · 主窗口终端面板（90-tools.js，`#termIn`）—— 一条命令一个进程（/api/exec）；
//   · 任务台常驻终端（37-detached.js，`#dtCmd`）—— 一个常驻 shell（/api/term/shell）。
//
// 以前两边各抄一份。任务台那份有两个致命细节没跟上：cwd 来自服务端哨兵行
// （`[工作目录] /home/dev/project`，是服务器上的**裸路径**，没有 ssh:// 前缀），
// 而且没带 ws —— 后端就把它当本地路径，Windows 上 abspath 成 `C:\home\work\gp`，
// listdir 必然失败。表现出来就是"刚开终端能补，敲一条命令之后 Tab 全废"。
//
// 所以规则只留这一份：调用方只负责给出 (输入框, cwd, ws, 多候选往哪打)。

/** 远端工作区的裸路径（`/home/dev/project`）拼回 `ssh://user@host:port/abs`。
 *
 *  主窗口终端的 cwd 本来就是 ssh://（后端 _remote_fs_ws 拼的），任务台的 cwd
 *  来自哨兵行，是服务器上的绝对路径 —— 在这里归一化，两边行为才完全一致。
 *  本地路径（`D:\…`、`C:\…`）原样返回。
 */
function termCwdForComplete(cwd, ws) {
  const c = String(cwd || "");
  if (!c.startsWith("/") || c.startsWith("ssh://")) return c;
  const m = /^(ssh:\/\/[^/]+)/.exec(String(ws || ""));
  return m ? m[1] + c : c;
}

/** 多候选且补不动时打给用户看的那行：`vendor/  vscode/`。 */
function termCandidatesText(items) {
  return (items || []).map((it) => it.text + (it.dir ? "/" : "")).join("  ");
}

/**
 * 对输入框做一次 Tab 补全（和 bash 一样的三条规则）：
 * 唯一候选直接补上（目录补 `/`）；多候选先补到公共前缀；前缀也走不动就把候选打出来。
 *
 * 返回补全后的整行内容；没有候选 / 等结果期间用户又改了输入 → 返回 null（静默）。
 * `onMulti(items)`：多候选且公共前缀走不动时回调 —— 两套终端的输出区 DOM 不一样，
 * 这是唯一需要分叉的地方（主窗口打到 #termOut，任务台打到 #dtOut）。
 * `onEmpty(usedCwd, note)`：0 候选时回调，`usedCwd` 是后端**实际用来列目录**的那个
 * 目录，`note` 是后端给的原因（`"目录不存在"`，没有则为空串）。补全失败本身是
 * 静默的，不给这一行的话，用户分不清"目录错了"和"这个前缀真没有文件"
 * （远程终端的 cwd 不同步就是这么被埋了很久的）。
 */
async function termCompleteInBox(inp, cwd, ws, onMulti, onEmpty) {
  if (!inp) return null;
  const original = inp.value;
  if (!original.trim()) return null;
  const tail = /[^\s]*$/.exec(original);
  const token = tail ? tail[0] : "";
  const lead = original.slice(0, original.length - token.length);
  let items = [];
  let usedCwd = termCwdForComplete(cwd, ws);
  let note = "";
  try {
    const r = await api("/api/complete?cwd=" + encodeURIComponent(usedCwd) +
                        "&ws=" + encodeURIComponent(ws || "") +
                        "&input=" + encodeURIComponent(original));
    items = (r && r.items) || [];
    if (r && r.cwd) usedCwd = r.cwd;   // 远程兜底时后端改用了 ws，以它为准
    // 后端只在「远程目录压根不存在」时才给 note；没有就是真空目录（真的没匹配）
    if (r && r.note) note = r.note;
  } catch (e) { return null; }
  // 0 候选：先报目录（用户在等结果期间改了输入的话，报出来也没意义）
  if (!items.length) {
    if (onEmpty && inp.value === original) onEmpty(usedCwd, note);
    return null;
  }
  // 等结果期间用户又改了输入：别拿旧候选往新内容上贴
  if (inp.value !== original) return null;

  if (items.length === 1) {
    inp.value = lead + items[0].text + (items[0].dir ? "/" : " ");
  } else {
    let common = items[0].text;
    for (const it of items) {
      let i = 0;
      while (i < common.length && i < it.text.length && common[i] === it.text[i]) i++;
      common = common.slice(0, i);
    }
    if (common.length > token.length) inp.value = lead + common;
    else if (onMulti) onMulti(items);
    else return null;
  }
  inp.setSelectionRange(inp.value.length, inp.value.length);
  return inp.value;
}
