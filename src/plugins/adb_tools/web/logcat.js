// Logcat
// 从 index.html 拆出（原第 4470-6386 行）。
// ---------------------------------------------------------------------------
// 视图：Logcat
// ---------------------------------------------------------------------------
const LOG_COLOR = { V: "var(--muted)", D: "#58a6ff", I: "#3fb950",
                    W: "#d29922", E: "#f85149", F: "#f85149" };

// ---------------------------------------------------------------------------
// 实时日志（常驻长连接 + 增量轮询）
// ---------------------------------------------------------------------------
// 后端起一个常驻 `adb logcat`，日志进本地环形缓冲；这里按 cursor 增量拉新行。
// 比 SSE 简单，也不用动 web_agent 的路由。
const LOGSTREAM = {
  on: false,
  cursor: 0,
  //: 后端还有多少行没取过来（单次 poll 只回 2000 条，cursor 停在本批最后）。
  //: 积压只是延迟变大，**不丢数据**；点状态栏「跳到最新」可一次性追平。
  backlog: 0,
  timer: null,
  last: null,
  paused: false,        // 面板暂停刷新（引擎照收，恢复时整块补回来）
  follow: true,         // 跟随最新：追加时自动滚到底
  // （原来这里有个 autoStarted：标记「这条流是打开面板时自动起的」。
  //   已废弃 —— 归属标记搬到了后端引擎上（status().origin），因为放前端内存里
  //   刷新一下就没了、别的窗口也看不到，而引擎是进程内单例。见 logStop。）
  procTimer: null,      // 定时刷 ps 的定时器
  counts: {},           // 各级别条数（后端 counts()）
  status: null,         // 最近一次 status（侧边栏那行状态用它）
  devices: [],          // 设备清单（侧边栏「设备」节）
  procs: [],            // 累计进程清单（侧边栏「进程列表」节）
  psInfo: null,         // 最近一轮 ps 的信息（命令 / 进程数 / 报错）
  followProcs: [],      // 正在跟随的**进程名** —— 它换了 PID 会自动追加新 PID
  serial: "",           // 当前用的设备序列号
  refilterTimer: null,  // 打字去抖

  // ---- 虚拟滚动 ----
  rows: [],             // 收到的可见行（entry 数组）。**面板的真身在这**，
                        // DOM 里那几十个节点只是它的一个窗口
  pool: [],             // 复用的行元素池（视口里有多少格就多少个）
  sel: new Set(),       // 选中的**下标**（不是 DOM 节点，见下面的说明）
  anchor: -1,           // Shift 连选的固定端
  focus: -1,            // ↑↓ 移动的那一端
  maxChars: 0,          // 最长一行多少字符（定横向滚动宽度用）
  rafId: 0, rafAt: 0,   // 排的那一帧（rafAt 用来判断它是不是被丢了）
  viewH: 0, viewW: 0,   // 上一次量到的视口尺寸
};

/** 定时刷 ps，让日志行挂的包名跟得上进程的生死（原版 refreshPidsMs 默认 5 秒）。 */
const LOG_PROC_REFRESH_MS = 5000;

// ---------------------------------------------------------------------------
// 虚拟滚动的三个常量
// ---------------------------------------------------------------------------
//
// 为什么必须虚拟化：那台车机上 2 万行 **3 秒**就写满一轮（现在上限 10 万，
// 也就十几秒）。每行一个 DOM 节点的话
// 几千行就开始卡（每次布局都要重算这一堆）。之前是给 DOM 设 3000 行上限、
// 超了砍最旧的 —— 代价是"往上翻看不到更早的"。
//
// 现在照原版：**只画视口里那几十行**。行按「下标 × 行高」绝对定位，
// 一个撑高的空 div 把滚动条长度做出来。于是 20 万行也只是数组大，DOM 恒定。

/** 行高。**必须和 CSS 里的 --row-h 一样** —— 两处不一致就是整体错位。 */
const LOG_ROW_H = 20;

/** 视口上下各多画几行，快滚时不至于露白（原版是 8）。 */
const LOG_OVERSCAN = 8;

/** 前端最多留多少行，超了从最旧的砍。跟 DOM 无关 —— 纯粹是别让数组无限涨。
 *
 *  后端环形缓冲默认就是 10 万行（`_logcat.DEFAULT_MAX_LINES`），前端留得比它
 *  多没意义（拿不到更多内容），留得少又会让「显示」永远追不上「缓冲」。
 *  每行是一个对象（date/time/pid/tid/level/tag/msg 七八个字符串），10 万行
 *  约 40–60MB，还是能长时间挂着的量级。两边对齐成 10 万。 */
const LOG_MAX_ROWS = 100000;

/** 空态那几行提示。清屏/换工作区时会换成别的文案，所以留一份原样的能还原。 */
const LOG_HINT_DEFAULT =
  "面板一打开就自动开抓\n\n" +
  "日志行右键：结束进程 / 强制停止应用 / 按 tag 过滤 / 复制\n" +
  "空白处右键：复制选中 / 全选 / 清屏\n" +
  "↑↓ 选行，Ctrl+C 复制选中行";

// ---------------------------------------------------------------------------
// 过滤条件
// ---------------------------------------------------------------------------
//
// 引擎那边每一项都是**多值集合**（原版也是），所以：
//
//   · 一个框里用逗号分隔 = 同一项内的多个值，之间是「或」
//   · 不同框之间是「与」
//
// 之前只发了单值 + 一个"排除含"，白白浪费了引擎一半的能力
// （tag_exclude / msg_exclude / case_sensitive / show_markers / strict_packages
//  这些引擎全支持，界面上一个都没接）。
function logSplit(v) {
  return String(v == null ? "" : v).split(",")
    .map((s) => s.trim()).filter(Boolean);
}

/** 级别：**逐项开关**（原版是集合，不是一个「I+」阈值）。
 *
 * 阈值表达不了"只看 E 和 F，I 不要"这种组合，而排查时恰恰常这么用。
 * A(assert) 必须在 —— 崩溃前兆就是它（`libc: Fatal signal`）。
 */
const LOG_LEVELS = ["V", "D", "I", "W", "E", "A", "F", "S"];

function logLevelsOn() {
  return LOG_LEVELS.filter((k) => {
    const b = $("lv-" + k);
    return b && b.classList.contains("on");
  });
}

function renderLogLevels() {
  const box = $("logLevels");
  if (!box) return;
  box.innerHTML = "";
  for (const k of LOG_LEVELS) {
    const b = document.createElement("span");
    b.className = "lvbtn on";
    b.id = "lv-" + k;
    b.textContent = k;
    b.style.color = LOG_COLOR[k] || "";
    b.title = "级别 " + k + "：点一下开 / 关";
    b.addEventListener("click", () => {
      // 全关掉 = 一条日志都看不到，而且看不出是自己关的 —— 卡一个底线
      if (b.classList.contains("on") && logLevelsOn().length <= 1) {
        toast("至少要留一个级别（全关掉就什么都看不到了）", true);
        return;
      }
      b.classList.toggle("on");
      logRefilterSoon();
    });
    box.appendChild(b);
  }
}

function logStreamFilters() {
  const lv = logLevelsOn();
  return {
    // 一个都没选中时按"全都要"处理，理由同上（宁可多给，不要莫名其妙空面板）
    levels: lv.length ? lv : LOG_LEVELS.slice(),
    tag_include: logSplit($("logTag").value),
    tag_exclude: logSplit($("logTagEx").value),
    msg_include: logSplit($("logMsg").value),
    msg_exclude: logSplit($("logMsgEx").value),
    // 按应用过滤靠 PID→包名表（后端的 logcat_procs 维护），
    // 表没刷过的话这里筛不出东西 —— 所以进面板时会自动刷一次
    packages: logSplit($("logPackage").value),
    pids: logSplit($("logPid").value),
    follow_procs: LOGSTREAM.followProcs.slice(),
    regex: $("logRegex").checked,
    case_sensitive: $("logCase").checked,
    show_markers: $("logMarkers").checked,
    strict_packages: $("logStrict").checked,
  };
}

/**
 * logcat 系列接口的公共入参 —— **一律带上设备序列号**。
 *
 * 后端那个引擎是「按序列号复用」的。不带序列号过去会被当成"要换一台设备"，
 * 于是 `logcat_poll` / `logcat_procs` 这些不带序列号的调用每 900ms
 * 就把正在跑的流掐掉重建一次 —— 面板写着"运行中"却永远 **0/0 行**，
 * 而且一句报错都没有。
 *
 * 后端已经修掉那个误判了；这里再统一带上，让这类事没机会再发生。
 */
function logArgs(extra) {
  return Object.assign({ serial: LOGSTREAM.serial || "" }, extra || {});
}

/** 打字去抖：停手 260ms 才重筛（原版 uiIntervalMs 那套的意图 —— 别每敲一个键算一遍）。 */
function logRefilterSoon() {
  if (LOGSTREAM.refilterTimer) clearTimeout(LOGSTREAM.refilterTimer);
  LOGSTREAM.refilterTimer = setTimeout(() => {
    LOGSTREAM.refilterTimer = null;
    logRefilter();
  }, 260);
}

/** 刷 PID↔包名表，并把结果喂给侧边栏的「进程列表」。
 *
 * `reset` 只在**打开面板**时传 —— 进程清单是累计的（死掉的先留着标「已退出」），
 * 攒着能让你认出"刚才那个进程死过"，但一直不清会越来越长。
 * 原版的取舍就是"下次打开面板才清"。
 */
async function refreshLogProcs(reset, quiet) {
  try {
    const r = await api("/api/plugin/adb_tools/logcat_procs",
                        logArgs(reset ? { reset: true, reason: "打开面板" }
                                      : { reason: "定时刷新" }));
    const d = (r.result || {}).data || {};
    LOGSTREAM.procs = d.procs || [];
    LOGSTREAM.psInfo = { command: d.command || "", count: d.count || 0,
                         error: d.error || "", at: Date.now() };
    // 跟随的进程换了 PID → 后端已经并进条件了，这里把输入框同步过来，
    // 不然界面显示的和实际生效的对不上（下次重筛会把新 PID 冲掉）
    if (d.pids) $("logPid").value = (d.pids || []).join(",");
    if (d.newPids && d.newPids.length) {
      toast("跟随的进程换号了，自动加上 PID " + d.newPids.join("、"));
    }
    renderLogSide();
    if (reset && !quiet) {
      toast(d.count
        ? ("进程表已刷新：" + (d.alive || 0) + " 活"
           + ((d.dead || 0) ? " / " + d.dead + " 已退出" : "")
           + "（" + (d.command || "ps") + "）")
        : "没读到进程表");
    }
  } catch (e) {
    if (reset && !quiet) toast("刷新进程表失败：" + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// 左栏：Logcat 侧边栏（设备 / 操作 / 进程列表）
// ---------------------------------------------------------------------------
//
// 原版侧边栏就这三节，**日志本体不在这儿** —— 那在编辑区（`#logcatView`）。
// 过滤条件也不在这儿，在面板自己的工具栏上。
//
// 这个拆分是刻意的：侧边栏是"入口和清单"（低频、长时间不动），
// 面板是"看日志"（高频、占地方）。原来把两者塞在同一栏里，
// 结果日志只能看到十来行、条件倒占了四行。

/** 侧边栏的一行（复用文件树那个 .node 样式）。 */
function sideRow(o) {
  const d = document.createElement("div");
  d.className = "node" + (o.cls ? " " + o.cls : "");
  d.style.paddingLeft = (8 + (o.depth || 0) * 13) + "px";
  d.innerHTML = '<span class="arrow"></span><span class="ico"></span><span class="nm"></span>';
  d.querySelector(".ico").textContent = o.ico || "";
  d.querySelector(".nm").textContent = o.name || "";
  if (o.desc) {
    const s = document.createElement("span");
    s.className = "desc";
    s.textContent = o.desc;
    d.appendChild(s);
  }
  if (o.title) d.title = o.title;
  if (o.onClick) d.addEventListener("click", o.onClick);
  if (o.onMenu) d.addEventListener("contextmenu", o.onMenu);
  return d;
}

function sideSection(box, title, desc) {
  const d = document.createElement("div");
  d.className = "node sect";
  d.style.paddingLeft = "8px";
  d.textContent = "▼ " + title + (desc ? "　" + desc : "");
  box.appendChild(d);
}

/** 把整个侧边栏重画一遍。 */
function renderLogSide() {
  // 重画 = innerHTML 清空再补回来，浏览器会抖出一次 scroll 事件。内核的
  // hideCtx 认 scroll，于是右键菜单每 5 秒（ps 刷新的节奏）自己消失一次。
  // 重画期间把 scroll 屏蔽掉（见 40-tree.js 的 CTX_SCROLL_MUTE）。
  if (typeof CTX_SCROLL_MUTE === "number") CTX_SCROLL_MUTE++;
  try {
    paintLogSide();
  } finally {
    // scroll 事件是异步派发的，同步减回去挡不住，给一帧多一点
    setTimeout(() => {
      if (typeof CTX_SCROLL_MUTE === "number") {
        CTX_SCROLL_MUTE = Math.max(0, CTX_SCROLL_MUTE - 1);
      }
    }, 80);
  }
}

function paintLogSide() {
  const box = $("logSide");
  if (!box) return;
  const keep = box.scrollTop;
  box.innerHTML = "";

  const st = LOGSTREAM.status || {};
  $("logSideState").textContent = LOGSTREAM.on
    ? ("运行中 · " + (LOGSTREAM.serial || "设备"))
    : "已停止";

  // ① 打开面板
  box.appendChild(sideRow({
    ico: "▷", name: "打开 Logcat 面板",
    title: "在编辑区打开（和原版那个 webview panel 一样）",
    onClick: () => openLogcatPanel(true),
  }));

  // ② 状态行
  box.appendChild(sideRow({
    ico: LOGSTREAM.on ? "●" : "○",
    cls: LOGSTREAM.on ? "" : "dim",
    name: LOGSTREAM.on
      ? ((LOGSTREAM.serial || "设备") + " · " + LOGSTREAM.rows.length + "/"
         + (st.total || 0) + " 行")
      : "已停止",
    desc: [st.proc_alive ? st.proc_alive + " 活" : "",
           st.restarts ? "重连 " + st.restarts : "",
           st.log_file ? "落盘 " + fmtNum(st.received || 0) : ""]
          .filter(Boolean).join(" · "),
    title: st.log_file || "",
    onClick: () => openLogcatPanel(true),
  }));

  // ③ 设备
  const devs = LOGSTREAM.devices || [];
  sideSection(box, "设备", devs.length ? devs.length + " 台" : "");
  if (!devs.length) {
    box.appendChild(sideRow({ ico: "⚠", cls: "dim", depth: 1,
                              name: "没有检测到设备" }));
  }
  for (const d of devs) {
    const live = (d.state === "device");
    box.appendChild(sideRow({
      ico: live ? "●" : "○",
      cls: live ? "" : "dim",
      depth: 1,
      name: d.serial,
      desc: [d.model, d.android ? "Android " + d.android : "",
             live ? "" : d.state].filter(Boolean).join(" · "),
      title: [d.model, d.product, d.device].filter(Boolean).join("\n"),
      onClick: () => selectLogDevice(d.serial),
      onMenu: (ev) => logDeviceMenu(ev, d),
    }));
  }
  box.appendChild(sideRow({
    ico: "⟳", depth: 1, name: "重新枚举 / 选择设备…",
    onClick: () => ensureDevices(true).then(() => loadLogDevices()),
  }));

  // ④ 操作
  sideSection(box, "操作");
  const act = (ico, name, fn) => sideRow({ ico, depth: 1, name, onClick: fn });
  box.appendChild(act("⟳", "刷新 PID↔包名 + 覆盖写 ps.txt",
                      () => refreshLogProcs(true)));
  box.appendChild(act("📄", "打开 ps 快照", () => logDataPanel("ps.txt")));
  box.appendChild(act("📄", "打开快照 JSON", () => logDataPanel("snapshot.json")));
  box.appendChild(act("📄", "打开当前原始日志", () => logDataPanel("current.log")));
  box.appendChild(act("🧹", "清空面板缓冲", logClearLocal));
  box.appendChild(act("🧹", "清空设备日志缓冲", logClearDevice));
  box.appendChild(act("💾", "导出当前过滤结果…", logExport));
  box.appendChild(act("↺", "重置过滤条件", logResetFilters));
  box.appendChild(act("🔍", "按关键字过滤…（用编辑器选中的词）", logFilterBySelection));

  // ⑤ 进程列表
  const procs = LOGSTREAM.procs || [];
  const nAlive = procs.filter((p) => p.alive).length;
  const nDead = procs.length - nAlive;
  sideSection(box, "进程列表", procs.length
    ? (nAlive + " 活" + (nDead ? " / " + nDead + " 已退出" : ""))
    : "还没有快照");
  if (!procs.length) {
    box.appendChild(sideRow({ ico: "ⓘ", cls: "dim", depth: 1,
                              name: "还没有进程快照" }));
  }
  // 活着的排前面，退出的压暗排后面（原版也是这么排的）
  const ordered = [...procs.filter((p) => p.alive), ...procs.filter((p) => !p.alive)];
  for (const p of ordered.slice(0, 400)) {
    const following = LOGSTREAM.followProcs.includes(p.name);
    box.appendChild(sideRow({
      ico: following ? "◉" : (p.alive ? "●" : "⊘"),
      cls: p.alive ? "" : "dim",
      depth: 1,
      name: p.name || p.pid,
      desc: "PID " + p.pid + (following ? " · 跟随中" : (p.alive ? "" : " · 已退出")),
      title: (p.name || "") + "\nPID " + p.pid
             + "\n\n左键：只看它（并跟随它的名字）\n右键：复制 / 结束进程 / 强制停止",
      onClick: () => toggleFollowProc(p.name),
      onMenu: (ev) => logProcMenu(ev, p),
    }));
  }
  if (ordered.length > 400) {
    box.appendChild(sideRow({ depth: 1, cls: "dim",
                              name: "（还有 " + (ordered.length - 400) + " 个没列出来）" }));
  }

  box.scrollTop = keep;
}

/** 读设备清单（带型号 + Android 版本），供侧边栏用。 */
async function loadLogDevices() {
  try {
    const r = await api("/api/plugin/adb_tools/devices", {});
    LOGSTREAM.devices = (r.result || {}).devices || [];
  } catch (e) {
    LOGSTREAM.devices = [];
  }
  renderLogSide();
}

/** 选中一台设备（切过去就重开一条流 —— 不重开的话日志还是旧设备在冒）。 */
async function selectLogDevice(serial) {
  if (!serial || serial === LOGSTREAM.serial) return;
  const wasOn = LOGSTREAM.on;
  LOGSTREAM.serial = serial;
  $("devSel").value = serial;
  renderLogSide();
  if (!wasOn) { toast("已选 " + serial); return; }
  await logStop();
  await logStart(serial);
}

/**
 * 点进程：只看它 + **跟随它的名字**（原版那一招）。
 *
 * 应用崩了重启会换 PID，只认老 PID 等于"日志突然没了"。跟随之后，
 * 每轮 ps 都会把新 PID 并进条件（**旧的保留**）—— 重启前后同时可见。
 * 再点一次取消（PID 条件与跟随一起清）。
 */
async function toggleFollowProc(name) {
  if (!name) return;
  const i = LOGSTREAM.followProcs.indexOf(name);
  if (i >= 0) {
    LOGSTREAM.followProcs.splice(i, 1);
    // 取消跟随 = 连 PID 条件一起清掉（否则"看不见但生效"）
    $("logPid").value = "";
    toast("已取消跟随 " + name);
  } else {
    LOGSTREAM.followProcs.push(name);
    const p = (LOGSTREAM.procs || []).find((x) => x.name === name && x.alive);
    if (p) $("logPid").value = p.pid;
    toast("跟随 " + name + "（它换 PID 后日志不会断）");
  }
  renderLogSide();
  await logRefilter();
}

/** 进程行右键：复制 / 复制 PID / 结束 / 强制停止。 */
function logProcMenu(ev, p) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";
  const info = document.createElement("div");
  info.className = "title";
  info.textContent = (p.name || "") + "　PID " + p.pid + (p.alive ? "" : "　已退出");
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  item(LOGSTREAM.followProcs.includes(p.name) ? "取消跟随" : "只看它（并跟随）",
       () => toggleFollowProc(p.name));
  sep();
  item("复制进程名", () => copyText(p.name || ""));
  item("复制 PID", () => copyText(String(p.pid)));
  sep();
  if (p.pid && p.pid !== "0") item("结束进程（kill -9）", () => logKillProcess(p.pid));
  // 只有名字像包名的才有这一项（HAL 服务 / 内核线程 force-stop 没意义）
  if (_looksLikePackage(p.name)) item("强制停止应用", () => logForceStop(p.name));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

function _looksLikePackage(name) {
  return /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+/.test(String(name || ""));
}

/** 设备行右键：投屏 / adb 终端。 */
function logDeviceMenu(ev, d) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";
  const info = document.createElement("div");
  info.className = "title";
  info.textContent = d.serial + (d.state === "device" ? "" : "　（" + d.state + "）");
  ctx.appendChild(info);

  const item = (label, fn, dis) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (dis) { b.disabled = true; b.style.opacity = ".45"; }
    else b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  item("只看这台设备的日志", () => selectLogDevice(d.serial), d.state !== "device");
  // 顺手在这台设备上跑 adb：命令自动带 -s（多设备时不可能猜错）
  item("adb 终端（自动 -s）", () => {
    if (typeof devAdbTerm !== "function") {
      toast("adb 插件还没加载，刷新页面再试", true);
      return;
    }
    devAdbTerm(d.serial);
  }, d.state !== "device");
  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("投屏（scrcpy）…", () => devScrcpy(d.serial));
  item("提权（adb root + remount）", () => devRootRemount(d.serial), d.state !== "device");
  // 无线调试：切一次就能拔线（端口固定 5555，重启也保持）
  item("固定 TCP/IP 5555（无线 adb）", () => devFixTcpip(d.serial),
       d.state !== "device");
  item("复制序列号", () => copyText(d.serial));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/**
 * 面板工具栏那个「设备」按钮的菜单：换设备 / 固定 5555 / 重新枚举。
 *
 * 这个按钮以前**根本没绑事件** —— 点它什么都不会发生（看着像界面卡住了）。
 * 左键和右键都走这里：设备相关的动作全收进这个菜单，工具栏就不用再挤第二个按钮。
 */
async function logDevicePickMenu(ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  if (!(LOGSTREAM.devices || []).length) await loadLogDevices();

  ctx.innerHTML = "";
  const info = document.createElement("div");
  info.className = "title";
  info.textContent = "设备";
  ctx.appendChild(info);

  const item = (label, fn, cur, dis) => {
    const b = document.createElement("button");
    b.textContent = (cur ? "● " : "") + label;
    if (cur) b.style.fontWeight = "600";
    if (dis) { b.disabled = true; b.style.opacity = ".45"; }
    else b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  const cur = LOGSTREAM.serial || $("devSel").value || "";
  const online = (LOGSTREAM.devices || []).filter((d) => d.state === "device");
  if (!online.length) {
    item("（没有在线设备）", () => {}, false, true);
  }
  for (const d of online) {
    item(d.serial + (d.model ? "　" + d.model : ""),
         () => selectLogDevice(d.serial), d.serial === cur);
  }

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("固定 TCP/IP 5555（无线 adb）", () => devFixTcpip(cur), false, !cur);
  item("重新枚举设备", () => ensureDevices(true).then(() => loadLogDevices()));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  const br = $("logDevPick").getBoundingClientRect();
  ctx.style.left = Math.min(br.left, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(br.bottom + 4, window.innerHeight - r.height - 8) + "px";
}

/** 清空所有过滤条件（原版「重置过滤条件」）。 */
async function logResetFilters() {
  for (const id of ["logTag", "logTagEx", "logMsg", "logMsgEx",
                    "logPackage", "logPid"]) {
    $(id).value = "";
  }
  $("logRegex").checked = false;
  $("logCase").checked = false;
  $("logMarkers").checked = true;
  $("logStrict").checked = true;
  for (const k of LOG_LEVELS) {
    const b = $("lv-" + k);
    if (b) b.classList.add("on");
  }
  LOGSTREAM.followProcs = [];
  renderLogSide();
  await logRefilter();
  toast("过滤条件已重置");
}

/** 按关键字过滤 —— 优先用编辑器里选中的词（原版的 logcat.filterByText）。 */
async function logFilterBySelection() {
  let word = "";
  const ta = $("edArea");
  try {
    if (ta && document.activeElement === ta
        && ta.selectionEnd > ta.selectionStart) {
      word = ta.value.slice(ta.selectionStart, ta.selectionEnd).trim();
    }
  } catch (e) { /* 忽略 */ }
  if (!word) word = ($("logMsg").value || "").trim();
  if (!word) {
    const w = window.prompt("按关键字过滤（内容包含）：", "");
    if (!w) return;
    word = w.trim();
  }
  if (!word) return;
  $("logMsg").value = word;
  await logRefilter();
  toast("只看内容含「" + word + "」");
}

function logNearBottom() {
  const box = $("logList");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 40;
}

// ---------------------------------------------------------------------------
// 「跟随最新」与暂停
// ---------------------------------------------------------------------------
// 原版把这两个做成独立开关；这里同样分开：
//   · 跟随最新 —— 追加新行时自动滚到底。**往上翻就自动关掉**（不然你想看
//     上面的内容，新行一直把你拽回底部，根本没法看）
//   · 暂停     —— 面板不再刷新，但引擎照收（它是独立线程），恢复时整块补齐

function paintLogFollow() {
  const b = $("logTail");
  if (!b) return;
  b.classList.toggle("on", LOGSTREAM.follow);
  b.style.color = LOGSTREAM.follow ? "var(--accent)" : "";
  b.title = LOGSTREAM.follow
    ? "正在跟随最新（往上翻会自动停止跟随）"
    : "已停止跟随 —— 点一下跳到最新并恢复跟随";
}

function setLogFollow(on, force) {
  const next = !!on;
  const changed = next !== LOGSTREAM.follow;
  LOGSTREAM.follow = next;
  paintLogFollow();
  // 跟随不只是"滚不滚到底"：后端据此决定缓冲满了是继续淘汰还是**冻结**。
  // 只在真的变化时同步，滚动到底不会每帧发请求；`force` 给"主动点 ⇣ /
  // 打开面板"用 —— 那时 JS 以为是跟随，后端可能还冻结着（页面刷新过）。
  if (changed || force) syncLogFollow(next);
}

/**
 * 把跟随状态同步给后端。
 *
 * 从「关」切回「开」时，如果冻结期间挡掉过行，后端会从 `current.log` 回灌，
 * 返回 `loaded`；这里随后整块重拉一次（`logResync`），面板才和缓冲对上。
 */
async function syncLogFollow(on) {
  if (!LOGSTREAM.on) return;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_follow",
                        logArgs({ follow: !!on }));
    const res = r.result || {};
    const d = res.data || {};
    if (typeof d.cursor === "number") LOGSTREAM.cursor = d.cursor;
    if (d.status) updateLogStatus(d.status);
    if (d.loaded) {
      toast("已恢复跟随：从 current.log 同步 " + d.loaded + " 行");
      await logResync();
    } else if (on && d.frozenDropped && res.content) {
      // 冻结期间挡掉过行，但回灌失败（比如没开落盘）—— 别装作补齐了
      toast(res.content, true);
    }
  } catch (e) {
    // 跟随同步失败不影响滚动本身，下一轮状态栏仍能看到冻结计数
  }
}

function paintLogPause() {
  const b = $("logPause");
  if (!b) return;
  b.textContent = LOGSTREAM.paused ? "▶ 恢复" : "暂停";
  b.classList.toggle("on", LOGSTREAM.paused);
  b.style.color = LOGSTREAM.paused ? "var(--accent)" : "";
  b.title = LOGSTREAM.paused
    ? "已暂停（后台还在收）—— 点一下恢复并补齐这段时间的内容"
    : "暂停面板刷新（后台照收，恢复后补齐）";
}

/** 跳到最新一条并恢复自动滚动（工具栏那个 ⇣）。 */
function logJumpTail() {
  // force：即使 UI 已经是"跟随"，也要问一次后端 —— 它可能还冻结着
  setLogFollow(true, true);
  logScrollToEnd();
}

/**
 * 暂停 / 恢复面板刷新。
 *
 * **恢复时不能接着老的 cursor 增量拉** —— `logcat_poll` 一次最多推 2000 行，
 * 暂停久了中间那段就断了。正确做法是拉一次 snapshot 整块补回来，
 * 再把 cursor 接到最新（`logResync`）。
 */
async function setLogPaused(on) {
  LOGSTREAM.paused = !!on;
  paintLogPause();

  if (LOGSTREAM.paused) {
    stopLogPolling();                       // 停止刷新，但引擎那边照收
    if (LOGSTREAM.on) $("logSummary").textContent = "⏸ 已暂停（后台还在收）";
    return;
  }
  if (!LOGSTREAM.on) return;
  await logResync();
  if (!LOGSTREAM.timer) startLogPolling();
}

/**
 * 整块补回来：拉 snapshot 替换面板内容，并把游标接到最新。
 *
 * 拉多少：以前是 4000（那时缓冲才 2 万）。现在缓冲 10 万，只补 4000 的话
 * 暂停回来往上翻就只剩一屏多一点。取 2 万 —— 约 3.5MB JSON、恢复时多花
 * 半秒左右，是"翻得动"和"别等太久"之间的折中。再往上的历史在 current.log。
 */
async function logResync() {
  try {
    const r = await api("/api/plugin/adb_tools/logcat_snapshot", logArgs({ lines: 20000 }));
    const d = (r.result || {}).data || {};
    if (typeof d.cursor === "number") LOGSTREAM.cursor = d.cursor;
    LOGSTREAM.backlog = 0;          // 整块重建 = 已经是最新，积压清零
    logSetRows(d.entries || []);
    if (d.status) updateLogStatus(d.status);
    if (d.counts) { LOGSTREAM.counts = d.counts; }
  } catch (e) {
    // 拉不到就算了：下一轮轮询自己会补上
  }
}

// ---------------------------------------------------------------------------
// 行选择（Ctrl+C 复制的是选中的这些行）
// ---------------------------------------------------------------------------
// 为什么不做"划选即普通文本"就完事：日志是可滚动的密集文本，想复制**连续的几行**
// 时用鼠标划很容易多带半行、少带半行。选中行再复制是稳的。
// 同时保留原生划选 —— 要按字符复制随便拖就是（面板右键菜单也会认划选）。

// 选中的是**下标**，不是 DOM 节点。
//
// 虚拟滚动下"没画出来的行"根本没有元素可以挂 `.sel` —— 但它们照样该在选中范围内
// （Ctrl+A 全选 20 万行、或者选了一段之后往回滚，都不该只剩看得见的那些）。
// 所以选中态存在 LOGSTREAM.sel 里，重绘时再往已经画出来的元素上贴。
//
// 代价是所有"按 DOM 数行"的老写法都得改成按下标算：Shift 连选、↑↓、复制、
// 删掉最旧的行时把下标前移，全是这一件事。

/** 把选中态贴到**已经画出来**的行上（不动 DOM 结构，重绘时才调）。 */
function logPaintSel() {
  const rowsEl = $("logRows");
  if (!rowsEl) return;
  for (const el of rowsEl.children) {
    const i = Number(el.dataset.i);
    el.classList.toggle("sel", el.style.display !== "none"
      && Number.isFinite(i) && LOGSTREAM.sel.has(i));
  }
}

function logSelClear() {
  LOGSTREAM.sel.clear();
  LOGSTREAM.anchor = -1;
  LOGSTREAM.focus = -1;
  logPaintSel();
}

/** 选中的下标，从小到大。 */
function logSelIndexes() {
  return Array.from(LOGSTREAM.sel).sort((a, b) => a - b);
}

/** 选中的那些行（entry），复制用。 */
function logSelRows() {
  return logSelIndexes().map((i) => LOGSTREAM.rows[i]).filter(Boolean);
}

/** 单击选一行；Ctrl/Cmd+单击加/减一行；Shift+单击从锚点连选。 */
function logRowClick(ev, row) {
  const box = $("logList");
  try { box.focus({ preventScroll: true }); } catch (e) { /* 老浏览器忽略 */ }
  const i = Number(row.dataset.i);
  if (!Number.isFinite(i)) return;

  if (ev.ctrlKey || ev.metaKey) {
    if (LOGSTREAM.sel.has(i)) LOGSTREAM.sel.delete(i);
    else LOGSTREAM.sel.add(i);
    LOGSTREAM.anchor = i;
    LOGSTREAM.focus = i;
    logPaintSel();
    return;
  }
  if (ev.shiftKey && LOGSTREAM.anchor >= 0) {
    for (let k = Math.min(LOGSTREAM.anchor, i); k <= Math.max(LOGSTREAM.anchor, i); k++) {
      LOGSTREAM.sel.add(k);
    }
    LOGSTREAM.focus = i;
    logPaintSel();
    return;
  }
  // 点已经选中的那一行 = 取消选中（方便"点一下撤掉"）
  const was = LOGSTREAM.sel.has(i) && LOGSTREAM.sel.size === 1;
  LOGSTREAM.sel.clear();
  if (!was) LOGSTREAM.sel.add(i);
  LOGSTREAM.anchor = i;
  LOGSTREAM.focus = i;
  logPaintSel();
}

function logSelAll() {
  const n = LOGSTREAM.rows.length;
  if (!n) { toast("还没有日志", true); return; }
  LOGSTREAM.sel.clear();
  for (let i = 0; i < n; i++) LOGSTREAM.sel.add(i);
  LOGSTREAM.anchor = n - 1;
  LOGSTREAM.focus = n - 1;
  logPaintSel();
  toast("已选中 " + n + " 行，Ctrl+C 复制");
}

function logCopySelected() {
  const rows = logSelRows();
  if (!rows.length) { toast("先选几行（↑↓ 或点一下）", true); return; }
  copyText(rows.map((e) => logTextOf(e)).join("\n"));
}

/** 复制到剪贴板（带失败提示，几个地方都用得上）。 */
function copyText(s) {
  const t = String(s == null ? "" : s);
  if (!t) { toast("没东西可复制", true); return; }
  if (!navigator.clipboard) { toast("这个环境不支持剪贴板", true); return; }
  navigator.clipboard.writeText(t)
    .then(() => toast("已复制"))
    .catch(() => toast("复制失败", true));
}

/** 把一行日志还原成能贴出去的文本。 */
function logTextOf(e) {
  if (e.kind !== "log") return e.msg || "";
  return [e.date, e.time, e.pid, e.tid, e.level,
          (e.tag || "?") + ":", e.msg || ""].filter((x) => x !== "" && x != null).join(" ");
}

// （原来这里有个 setSelectValue —— 给包名那个下拉框设值用的。
//  包名现在是文本输入框，候选走「＋」弹窗，这个助手就没人用了。）

// ---- 进程操作（搬自 logcat-filter 的右键菜单那几条）----
async function logKillProcess(pid) {
  const ok = await confirmBox("结束进程",
    "对 PID " + pid + " 执行 kill -9？\n\n"
    + "如果是系统关键进程，设备可能黑屏或重启 —— 只杀你自己在调的那个应用。",
    "结束进程");
  if (!ok) return;
  try {
    const r = await api("/api/plugin/adb_tools/kill_process", { pid });
    const res = r.result || {};
    toast(res.ok ? (res.summary || "已结束") : (res.summary || res.content || "失败"),
          !res.ok);
  } catch (e) { toast("失败：" + e.message, true); }
}

async function logForceStop(procName) {
  const ok = await confirmBox("强制停止应用",
    "对进程「" + procName + "」执行 am force-stop？\n\n"
    + "包名会自动从进程名推出来（会砍掉 `:remote` 这类子进程后缀）。", "强制停止");
  if (!ok) return;
  try {
    // 传 name 让后端推包名 —— 它那套 process_package 认得 HAL 服务 / 内核线程
    const r = await api("/api/plugin/adb_tools/force_stop", { name: procName });
    const res = r.result || {};
    toast(res.ok ? (res.summary || "已停止") : (res.summary || res.content || "失败"),
          !res.ok);
  } catch (e) { toast("失败：" + e.message, true); }
}

/** 日志行的右键菜单。 */
function logItemMenu(ev, e) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = (e.tag || "?") + "　pid " + (e.pid || "?") +
    (e.pkg ? "　" + e.pkg : "");
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  const pid = String(e.pid || "").trim();
  const proc = String(e.pkg || "").trim();

  // 从系统日志里挑出真正可用的名字时，这两项才有意义
  // （内核线程 pid 是 0/3，force-stop 也推不出包名）
  if (pid && pid !== "0") item("结束进程（kill -9 " + pid + "）", () => logKillProcess(pid));
  if (proc) item("强制停止 " + proc, () => logForceStop(proc));

  sep();
  if (pid && pid !== "0") {
    item("只看这个 PID", () => {
      // 手动点了 PID = 停止跟随（不然过几秒跟随又把别的 PID 加回来）
      LOGSTREAM.followProcs = [];
      $("logPid").value = pid;
      renderLogSide();
      logRefilter();
    });
  }
  if (proc) {
    item("只看 " + proc, () => {
      $("logPackage").value = proc;
      logRefilter();
    });
  }
  item("按这个 tag 过滤", () => {
    $("logTag").value = e.tag || "";
    logRefilter();
  });
  item("排除这个 tag", () => {
    const parts = logSplit($("logTagEx").value);
    if (e.tag && parts.indexOf(e.tag) < 0) parts.push(e.tag);
    $("logTagEx").value = parts.join(",");
    logRefilter();
  });

  sep();
  item("复制这一行", () => copyText(logTextOf(e)));
  if (pid) item("复制 PID", () => copyText(pid));
  if (proc) item("复制进程名", () => copyText(proc));

  // 行上右键以前只有"复制/过滤"，全选和清屏得挪到空白处才有 —— 手里正指着
  // 某一行时没人会再去空白处找。破坏性的「清空」排最后，和空白处菜单一致。
  sep();
  item("全选", logSelAll);
  item("清空", logClearLocal);

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

// ---- 缓冲清理与导出 ----
async function logClearLocal() {
  try {
    const r = await api("/api/plugin/adb_tools/logcat_clear", logArgs());
    const res = r.result || {};
    logSetHint("已清空（本地缓冲也清了）\n设备那边的缓冲没动");
    logSetRows([]);
    LOGSTREAM.counts = {};
    LOGSTREAM.backlog = 0;
    if (res.data && res.data.status) updateLogStatus(res.data.status);
    toast(res.summary || "已清空");
  } catch (e) {
    toast("清空失败：" + e.message, true);
  }
}

async function logClearDevice() {
  const ok = await confirmBox("清空设备日志缓冲",
    "在设备上执行 adb logcat -c？\n\n"
    + "设备里已有的日志会被丢掉，之后抓到的从这一刻开始 ——\n"
    + "复现问题前先清一下，日志里就不会混着上一次的。\n\n"
    + "（本地面板里的内容不受影响）", "清空设备缓冲");
  if (!ok) return;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_clear_device", logArgs());
    const res = r.result || {};
    toast(res.ok ? (res.summary || "已清空") : (res.summary || res.content || "失败"),
          !res.ok);
  } catch (e) { toast("失败：" + e.message, true); }
}

/** 这一段的上一级目录（`C:\a\b` → `C:\a`）。 */
function dirOf(p) {
  const s = String(p || "").replace(/[\\/]+$/, "");
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  return i > 0 ? s.slice(0, i) : "";
}

/**
 * 在文件树里把这个文件**露出来**：把它的各级父目录标记成展开，再重画。
 *
 * 为什么不能只展开直接父级：树是按需渲染的，上层没展开时下层根本不会被画出来。
 * `a/b/c/logcat.log` 得把 `a`、`a/b`、`a/b/c` 全标上，文件才会真的出现在屏幕上。
 *
 * 返回是否落在某一棵树里 —— 不在任何树里（比如存到了工作区外面）就露不出来，
 * 但文件已经在编辑器里打开了，那才是主要的。
 */
async function revealInTree(path) {
  const root = String(S.workspace || "").replace(/[\\/]+$/, "");
  if (!root) return false;
  const base = root.toLowerCase();
  const target = String(path || "").replace(/[\\/]+$/, "").toLowerCase();
  // 左侧只画活动根这一棵 —— 存到工作区外面的话，树里根本没有它的位置
  if (target !== base && !target.startsWith(base + "\\")
      && !target.startsWith(base + "/")) {
    return false;
  }

  let cur = dirOf(path);
  while (cur && cur.length > root.length) {
    expanded.add(cur);
    const up = dirOf(cur);
    if (!up || up === cur) break;
    cur = up;
  }
  await loadTree();
  return true;
}

/**
 * 导出当前过滤结果。
 *
 * 三步：**取内容 → 选目录 → 存盘并打开**。以前是浏览器下载，
 * 现在存到用户自己选的目录，然后直接在编辑器里打开 ——
 * 因为导出日志之后你下一步永远是"看它 / 挑几行贴给别人"，而不是收一个下载文件。
 *
 * 写盘走 `/api/fs/write`，不是让插件自己写：那条接口已经按 `fs_roots` 白名单
 * 校验、还会自动建父目录，和编辑器 Ctrl+S 是同一套守卫。插件拿不到 roots，
 * 给它一个"往任意目录写"的参数等于把那个守卫开了个后门。
 */
async function logExport() {
  let d;
  try {
    // save:false —— 不再往 .ai/temp 里额外留一份。
    // 一份导出落两个文件，下次谁也说不清该看哪个。
    const r = await api("/api/plugin/adb_tools/logcat_export",
                        logArgs({ save: false }));
    const res = r.result || {};
    if (!res.ok) {
      toast(res.summary || res.content || "没有可导出的内容", true);
      return;
    }
    d = res.data || {};
    if (!d.text) { toast("没有可导出的内容", true); return; }
  } catch (e) {
    toast("导出失败：" + e.message, true);
    return;
  }

  // 默认存 <工作区>/temp：导出的日志多半只是临时看一眼，放根目录旁边最好找
  // （`<工作区>/.ai/temp/logcat/` 是引擎的落盘目录，不是给人挑的）。
  // temp 不在就先建 —— pickDirectory 一进去就要 list 它，不存在会直接报错；
  // 建不出来（只读 / 权限）就退回工作区根，让用户自己挑。
  let start = S.workspace || "";
  if (start) {
    const temp = joinPath(start, "temp");
    // 先 list：上次导出过的话 temp 就在，省掉一次必然失败的 mkdir。
    const ls = () => api("/api/fs/list?scope=roots&dirs_only=1&path="
      + encodeURIComponent(temp));
    try {
      const t = await ls();
      if (t && t.path) start = t.path;
    } catch (e) {
      // 不在：建出来再确认一次。建不出来（只读 / 权限）就退回工作区根，
      // 让用户自己在弹窗里挑别处 —— 不能因此把导出整个卡住。
      try {
        await api("/api/fs/mkdir", { path: temp });
        const t2 = await ls();
        if (t2 && t2.path) start = t2.path;
      } catch (_) { /* 退回工作区根 */ }
    }
  }

  // 外面再包一层：建弹窗这一步真出错时至少能看到提示，而不是"点了没反应"。
  try {
    pickDirectory({
      title: "导出日志",
      hint: "把 " + fmtNum(d.lines || 0) + " 行（" + fmtBytes(d.bytes) + "）存到哪个目录？\n"
          + "文件名可以改（同名会覆盖）。默认是工作区下的 temp（不存在会自动建），\n"
          + "也可以换成别的目录。存完会在编辑器里打开，右键可以加进对话框。",
      label: "存到这里",
      start,
      fileName: d.name || "logcat.log",
      onPick: (dir, name) => saveExportedLog(dir, d, name),
    });
  } catch (e) {
    toast("打不开保存对话框：" + e.message, true);
  }
}

/**
 * 把导出的日志在**有编辑器的那个窗口**里打开。
 *
 * 独立窗口（`?detached=log`）里编辑器整个是藏起来的（style.css 里
 * `body.detached-log #edTabs { display:none }`）—— 在那儿调 openInEditor
 * 只会往一个看不见的标签栏里塞一格，用户看到的就是"文件存下来了，
 * 但编辑器里没有它"。所以独立窗口一律交给 opener（主窗口）去开。
 *
 * 返回 "main" / "here"，调用方据此决定在哪边展开文件树。
 */
async function openExportedFile(path, name) {
  if (DETACHED_LOG) {
    const w = window.opener;
    try {
      if (w && !w.closed && typeof w.openInEditor === "function") {
        await w.openInEditor(path, name);
        try { w.focus(); } catch (_) { /* 跨窗口聚焦可能被拦，忽略 */ }
        // 真的建出标签了才算"开了"：读文件那一步失败时，主窗口也只是
        // toast 一句「打开失败」，不会当异常抛回来 —— 这里得自己看一眼。
        const ok = !w.EDITORS || w.EDITORS.some((e) => e.path === path);
        return ok ? "main" : "failed";
      }
    } catch (_) {
      // 主窗口已经关了 / 跨源拿不到 —— 下面退化成在本窗口开
    }
  }
  await openInEditor(path, name);
  return "here";
}

/** 把导出的文本写到 dir/name，然后打开 + 在树里露出来。 */
async function saveExportedLog(dir, d, name) {
  // name 是弹窗里改过的（可能为空 = 用户没动）。空就退回后端给的默认名。
  name = String(name || "").trim() || d.name || "logcat.log";
  const path = joinPath(dir, name);

  try {
    await api("/api/fs/write", { path, text: d.text || "", eol: "lf" });
  } catch (e) {
    toast("保存失败：" + e.message, true);
    return;
  }

  // 先开编辑器（这是用户真正要看的东西），树那边随后刷新
  const where = await openExportedFile(path, name);

  // 树要在**开文件的那个窗口**里展开：另一个窗口的树没人看，
  // 而主窗口那边看不见文件在哪的话，标签一关就再找不着了。
  try {
    if (where === "main") await window.opener.revealInTree(path);
    else if (where === "here") await revealInTree(path);
  } catch (_) { /* 树没展开不影响"文件已经打开" */ }

  // 「存好了却没打开」是最容易让人以为导出没生效的情况 —— 当场验一次，
  // 没开就把路径明明白白给出来（openInEditor 自己只 toast 一句「打开失败」）。
  const opened = where === "main"
    || (where === "here" && typeof EDITORS !== "undefined"
        && EDITORS.some((e) => e.path === path));
  if (!opened) {
    await infoBox("已保存，但没能自动打开",
                  "文件已经写好了：\n" + path
                  + "\n\n可以到文件树里双击它，或者用「打开文件」自己打开。");
    return;
  }

  // 独立窗口且没拿到 opener（直接访问的那个 URL）：这边开出来的标签看不见，
  // 只能把路径交出去，别让人以为导出没成功。
  if (DETACHED_LOG && where === "here") {
    await infoBox("已保存",
                  "独立窗口里没有编辑器，文件在：\n" + path
                  + "\n\n回主窗口打开它，或者在文件树里双击。");
    return;
  }

  toast("已保存 " + name + "（" + fmtNum(d.lines || 0) + " 行）"
        + (where === "main" ? "，已在主窗口打开" : ""));
}

// ---------------------------------------------------------------------------
// 落盘文件（<工作区>/.ai/temp/logcat/）
// ---------------------------------------------------------------------------
//
// 环形缓冲是「看的窗口」不是存储：面板里只留得下最近 10 万行，DOM 更少。
// 实测那台车机 3 秒就写满一轮 —— 所以「刚才到底打了什么」这种问题，
// 答案只在文件里。这个弹窗就是那几个文件的入口。

const LOGCAT_FILES = ["current.log", "ps.txt", "snapshot.json",
                      "state.json", "filter.log"];

const LOGCAT_FILE_HELP = {
  "current.log":    "未过滤的全量原始日志。每次「启动」覆盖重建、文件名固定，外面可以一直 tail -f。",
  "ps.txt":         "每轮 ps 的原始输出 + 头部注释。头部写清了每个包名解析成了哪些 PID —— 面板说「显示 0 条」时靠这句分辨是「包名没匹配到进程」还是「匹配上了但没打日志」。",
  "snapshot.json":  "同一时刻的结构化快照：设备 / ps 命令与进程数 / 完整过滤条件 / 包名→PID / 当前日志文件。",
  "state.json":     "当前过滤条件 + 选中设备。故意不含 PID —— 那是这次会话里手动点出来的，不该跨会话挂着。",
  "filter.log":     "过滤条件变更历史，一行一条 JSON。",
};

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

/**
 * 候选值弹窗（对应原版点 `＋` 弹出的那个列表）。
 *
 * 原版用 VS Code 原生 QuickPick（能多选、能模糊搜）。web 这边没有那个控件，
 * 用一个简单列表 —— 但"**从列表里挑**"这个动作本身值得保留：
 * 车机上的包名又长又像（`com.server.androidauto` / `com.server.androidauto:remote`），
 * 手打很容易打成另一个，而且打错的表现是"一条日志都没有"，很难往这上面想。
 *
 * `what=tags` 的候选取自**日志里出现过的 Tag 并带行数** ——
 * "哪个 Tag 在刷屏"一眼就看出来了。
 */
async function logPick(what, targetId) {
  let items;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_candidates", logArgs({ what }));
    const res = r.result || {};
    if (!res.ok) {
      toast(res.summary || res.content || "拿不到候选列表", true);
      return;
    }
    items = (res.data || {}).items || [];
  } catch (e) {
    toast("读取候选失败：" + e.message, true);
    return;
  }
  if (!items.length) {
    toast(what === "packages" ? "设备上没读到包名" : "日志里还没出现过 Tag", true);
    return;
  }

  const chosen = new Set(logSplit($(targetId).value));

  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:6px;height:100%";
  const want = document.createElement("input");
  want.type = "text";
  want.placeholder = "过滤候选（模糊匹配）";
  const list = document.createElement("div");
  list.style.cssText = "flex:1;overflow:auto;min-height:240px";
  body.appendChild(want);
  body.appendChild(list);

  function paint() {
    const q = want.value.trim().toLowerCase();
    list.innerHTML = "";
    let shown = 0;
    for (const it of items) {
      if (q && it.value.toLowerCase().indexOf(q) < 0) continue;
      if (++shown > 400) break;
      const picked = chosen.has(it.value);
      const d = document.createElement("div");
      d.className = "node" + (picked ? " sel" : "");
      d.innerHTML = '<span class="arrow"></span><span class="ico"></span><span class="nm"></span>';
      d.querySelector(".ico").textContent = picked ? "☑" : "☐";
      d.querySelector(".nm").textContent = it.value;
      const s = document.createElement("span");
      s.className = "desc";
      s.textContent = it.hits ? (it.hits + " 行") : (it.third ? "第三方" : "系统");
      d.appendChild(s);
      d.addEventListener("click", () => {
        if (picked) chosen.delete(it.value); else chosen.add(it.value);
        paint();
      });
      list.appendChild(d);
    }
    if (!shown) list.appendChild(emptyBox("没有匹配的候选"));
  }
  want.addEventListener("input", paint);
  paint();

  const foot = document.createElement("div");
  const hint = document.createElement("span");
  hint.style.cssText = "font-size:12px;color:var(--muted)";
  hint.textContent = "同一项里多个值是「或」，不同项之间是「与」";
  const sp = document.createElement("span");
  sp.className = "spacer";
  const clear = document.createElement("button");
  clear.className = "tbtn";
  clear.textContent = "清空";
  const ok = document.createElement("button");
  ok.className = "tbtn";
  ok.textContent = "用这些";
  foot.appendChild(hint);
  foot.appendChild(sp);
  foot.appendChild(clear);
  foot.appendChild(ok);

  clear.addEventListener("click", () => { chosen.clear(); paint(); });
  ok.addEventListener("click", () => {
    $(targetId).value = Array.from(chosen).join(",");
    closeModal();
    logRefilter();
  });

  openModal(what === "packages" ? "选择包名" : "选择 Tag", body, foot);
  setTimeout(() => want.focus(), 30);
}

async function logDataPanel(prefer) {
  let d;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_data", logArgs());
    d = (r.result || {}).data || {};
  } catch (e) {
    toast("读取失败：" + e.message, true);
    return;
  }

  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:8px;height:100%";

  const head = document.createElement("div");
  head.style.cssText = "font-size:12px;color:var(--muted);word-break:break-all;flex-shrink:0";
  head.textContent = d.dir
    ? ("目录：" + d.dir
       + "\n已收 " + fmtNum(d.received || 0) + " 行"
       + (d.saveToFile ? "" : "　⚠ save_to_file = false，原始日志没落盘"))
    : "还没有工作区，没法落盘";
  if (d.error) head.textContent += "\n落盘出错：" + d.error;
  body.appendChild(head);

  const list = document.createElement("div");
  list.style.cssText = "flex-shrink:0";
  const pre = document.createElement("pre");
  pre.style.cssText = "flex:1;min-height:200px;max-height:340px;overflow:auto;" +
    "background:var(--bg);border:1px solid var(--border);border-radius:8px;" +
    "padding:8px 10px;font-size:11px;line-height:1.6;margin:0;" +
    "white-space:pre;user-select:text";
  pre.textContent = "点上面任一文件看它的内容。";

  let cur = "";

  async function show(name) {
    pre.textContent = "读取中…";
    try {
      const r = await api("/api/plugin/adb_tools/logcat_data_read",
                          logArgs({ name, lines: 2000 }));
      const res = r.result || {};
      pre.textContent = res.ok ? (res.content || "（空文件）")
                               : (res.content || res.summary || "读不了");
    } catch (e) {
      pre.textContent = "读取失败：" + e.message;
    }
  }

  for (const name of LOGCAT_FILES) {
    const f = (d.files || {})[name] || {};
    const b = document.createElement("div");
    b.className = "node";
    b.style.cssText = "display:flex;align-items:center;gap:6px";
    b.title = (LOGCAT_FILE_HELP[name] || "") + "\n\n" + (f.path || "");
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.style.fontWeight = "500";
    nm.textContent = name;
    const sz = document.createElement("span");
    sz.style.cssText = "color:var(--muted);font-size:11px";
    sz.textContent = f.bytes ? (fmtBytes(f.bytes) + "　" + fmtTime(f.at)) : "还没写";
    b.appendChild(nm);
    b.appendChild(sz);
    b.addEventListener("click", () => {
      for (const c of list.children) c.classList.remove("sel");
      b.classList.add("sel");
      cur = name;
      show(name);
    });
    list.appendChild(b);
  }
  body.appendChild(list);
  body.appendChild(pre);

  const foot = document.createElement("div");
  const hint = document.createElement("span");
  hint.style.cssText = "font-size:12px;color:var(--muted)";
  hint.textContent = "current.log 只显示尾部 2000 行";
  const sp = document.createElement("span");
  sp.className = "spacer";
  const copyPath = document.createElement("button");
  copyPath.className = "tbtn";
  copyPath.textContent = "复制路径";
  copyPath.addEventListener("click", () => {
    const f = (d.files || {})[cur] || {};
    copyText(f.path || d.dir || "");
  });
  const refresh = document.createElement("button");
  refresh.className = "tbtn";
  refresh.textContent = "刷新";
  refresh.addEventListener("click", () => { closeModal(); logDataPanel(); });
  foot.appendChild(hint);
  foot.appendChild(sp);
  foot.appendChild(copyPath);
  foot.appendChild(refresh);

  openModal("日志数据文件", body, foot);

  // 默认看哪个：侧边栏点名了就用它；否则在写日志时 current.log 最有用，
  // 没在写就看 ps.txt（那个一眼能看出"包名解析成了什么"）
  const first = (prefer && LOGCAT_FILES.includes(prefer)) ? prefer
    : ((d.files && d.files["current.log"] && d.files["current.log"].bytes)
       ? "current.log" : "ps.txt");
  const node = list.children[LOGCAT_FILES.indexOf(first)];
  if (node) node.click();
}

// ---------------------------------------------------------------------------
// 行渲染（虚拟滚动）
// ---------------------------------------------------------------------------
//
// 行元素是**复用**的：视口里同一格反复装不同的行（滚一下就走，谁也不属于谁）。
// 所以监听只在建元素时挂一次，处理函数通过 el._entry / el.dataset.i 读
// "这一格现在装的是哪条" —— 挂到具体某条日志上的话，格子换行之后监听还指着旧的。

function makeLogRow() {
  const el = document.createElement("div");
  el.className = "vrow lrow";
  el._entry = null;
  el.addEventListener("click", (ev) => logRowClick(ev, el));
  el.addEventListener("contextmenu", (ev) => {
    if (el._entry) logItemMenu(ev, el._entry);
  });
  return el;
}

/** 把第 i 行画进（复用的）元素 el。 */
function fillLogRow(el, e, i) {
  el._entry = e;
  el.dataset.i = String(i);
  // 选中后 Ctrl+C 复制的就是这一行（还原成 logcat 的 threadtime 样子）
  el.dataset.line = logTextOf(e);
  el.classList.toggle("sel", LOGSTREAM.sel.has(i));
  el.innerHTML = "";
  el.style.color = "";

  if (e.kind === "sys") {
    el.style.color = "var(--accent)";
    el.textContent = "· " + (e.msg || "");
    return;
  }
  if (e.kind === "marker") {
    el.style.color = "var(--muted)";
    el.textContent = e.msg || "";
    return;
  }

  // 设备时间（threadtime 里的 `date time`）。以前只存了却没画出来，界面上
  // 只剩 level + tag + msg —— 同一秒里几十条日志根本分不出先后，也没法和
  // 车机上的操作时刻对上。
  const tm = document.createElement("span");
  tm.className = "ltime";
  tm.textContent = ((e.date ? e.date + " " : "") + (e.time || "")).trim();
  if (tm.textContent) el.appendChild(tm);

  const lv = document.createElement("span");
  lv.className = "vtag";
  lv.textContent = e.level || "?";
  lv.style.color = LOG_COLOR[e.level] || "var(--muted)";
  el.appendChild(lv);

  const head = document.createElement("span");
  head.style.color = "var(--muted)";
  head.textContent = (e.pkg ? shortPath(e.pkg, 1) + " " : "") +
    (e.tag || "?") + ": ";
  el.appendChild(head);

  // 正文：把 (Foo.kt:42) 渲染成可点链接（搬自原插件的 srclink）
  const msg = e.msg || e.message || "";
  const links = e.src || [];
  if (!links.length) {
    el.appendChild(document.createTextNode(msg));
    return;
  }
  let pos = 0;
  for (const l of links) {
    if (l.start > pos) {
      el.appendChild(document.createTextNode(msg.slice(pos, l.start)));
    }
    const a = document.createElement("span");
    a.textContent = msg.slice(l.start, l.end);
    a.style.cssText = "color:#58a6ff;text-decoration:underline;" +
      "cursor:pointer;text-underline-offset:2px";
    a.title = "点击跳到 " + l.file + ":" + l.line;
    a.addEventListener("click", (ev) => {
      ev.stopPropagation();
      jumpToSource(l.file, l.line);
    });
    el.appendChild(a);
    pos = l.end;
  }
  if (pos < msg.length) {
    el.appendChild(document.createTextNode(msg.slice(pos)));
  }
}

/** 一个字符多宽（等宽字体下量一次就够）。横向滚动宽度靠它算。 */
let logCharW = 0;
function logMeasureChar() {
  if (logCharW) return logCharW;
  const p = document.createElement("span");
  p.style.cssText = "position:absolute;visibility:hidden;white-space:pre;" +
    "font-family:" + getComputedStyle(document.documentElement)
      .getPropertyValue("--mono").trim() + ";font-size:12px";
  p.textContent = "0".repeat(50);
  document.body.appendChild(p);
  logCharW = p.getBoundingClientRect().width / 50 || 7.2;
  p.remove();
  return logCharW;
}

/**
 * 这一行画出来大概占多少个字符宽。
 *
 * 只在来新行时算一次（存进 maxChars），所以走**便宜**的加法而不是
 * `logTextOf(...).length` —— 那个是给复制用的，每来一行拼一次字符串太浪费，
 * 而这里只要个够用的上界（算宽了只是横向多点余量）。
 */
function logCharLen(e) {
  if (e.kind !== "log") return (e.msg || "").length + 2;
  return (e.date || "").length + (e.time || "").length + (e.pid || "").length +
    (e.tid || "").length + (e.level || "").length + (e.tag || "").length +
    (e.msg || "").length + 8;
}

/** 排一帧重绘。 */
function logSchedule() {
  const now = Date.now();
  if (LOGSTREAM.rafId && now - LOGSTREAM.rafAt < 400) return;   // 刚排上，等它跑
  // 排了很久还没跑 = 那一帧被挂起或丢了（标签页切到后台时浏览器会这么干）。
  // 不复位的话 rafId 一直非 0、所有调度都被上面那句挡掉 ——
  // 渲染循环**再也醒不过来**：状态栏照常刷、画面却停在那一刻。
  if (LOGSTREAM.rafId) LOGSTREAM.rafId = 0;
  LOGSTREAM.rafAt = now;
  LOGSTREAM.rafId = requestAnimationFrame(() => {
    LOGSTREAM.rafId = 0;
    logRender();
  });
}

/** 重画视口里那几十行。数据永远看 LOGSTREAM.rows，DOM 只是它的窗口。 */
function logRender() {
  const box = $("logList");
  const rowsEl = $("logRows");
  const spacer = $("logSpacer");
  if (!box || !rowsEl || !spacer) return;

  const n = LOGSTREAM.rows.length;
  const hint = $("logHint");
  if (hint) hint.classList.toggle("hidden", n > 0);

  if (!n) {
    rowsEl.innerHTML = "";
    LOGSTREAM.pool.length = 0;
    spacer.style.height = "0px";
    rowsEl.style.width = "";
    logShowTailNote(false);
    return;
  }

  // 滚动条长度 = 行数 × 行高。这个空 div 就是"20 万行"的全部代价。
  spacer.style.height = (n * LOG_ROW_H) + "px";
  // 宽度按最长的一行算，于是长行能横向拉过去看全（单行截断的补偿）。
  // 比视口宽就出横向滚动条 —— 别拿 clientWidth 当上限，那会把长行彻底切掉。
  rowsEl.style.width = Math.max(box.clientWidth,
    Math.ceil(LOGSTREAM.maxChars * logMeasureChar()) + 40) + "px";

  const top = box.scrollTop;
  const h = box.clientHeight || 400;
  let from = Math.max(0, Math.floor(top / LOG_ROW_H) - LOG_OVERSCAN);
  let to = Math.min(n, Math.ceil((top + h) / LOG_ROW_H) + LOG_OVERSCAN);
  if (to <= from) {
    // 滚动位置跑到内容之外了（整份替换后内容变短、或者滚动高度还没更新）：
    // 直接贴到最后一屏。不兜这一下就是 0 行可画 —— 画面全空，
    // 看着和"缓冲里有 10 万行却什么都不显示"一模一样。
    from = Math.max(0, n - Math.ceil(h / LOG_ROW_H) - LOG_OVERSCAN);
    to = n;
  }

  const need = Math.max(0, to - from);
  const pool = LOGSTREAM.pool;
  while (pool.length < need) {
    const el = makeLogRow();
    rowsEl.appendChild(el);
    pool.push(el);
  }
  for (let k = 0; k < pool.length; k++) {
    const el = pool[k];
    if (k >= need) { el.style.display = "none"; el._entry = null; continue; }
    el.style.display = "";
    el.style.top = ((from + k) * LOG_ROW_H) + "px";
    fillLogRow(el, LOGSTREAM.rows[from + k], from + k);
  }
  logShowTailNote(n > 0 && (n * LOG_ROW_H + LOG_ROW_H) < box.clientHeight);
}

/**
 * 内容比视口短时（过滤后只剩几行）在最后一行下面补一句说明。
 *
 * 不补的话下面就是**一大片空白**，看着和"画不出来/坏了"一个样 ——
 * 原版加它就是因为用户实测报过两次。有了这句就能一眼分清
 * "是过滤剩得少"还是"没画出来"，还顺手告诉你怎么办。
 */
function logShowTailNote(on) {
  const el = $("logTailNote");
  if (!el) return;
  if (!on) { el.classList.add("hidden"); return; }
  const n = LOGSTREAM.rows.length;
  const st = LOGSTREAM.status || {};
  el.textContent = "—— 下面没有更多了：面板里有 " + n + " 行"
    + (st.total != null ? "（后端缓冲共 " + st.total + " 行）" : "")
    + "。要全量就清掉过滤条件";
  el.style.top = (n * LOG_ROW_H + 2) + "px";
  el.classList.remove("hidden");
}

/** 滚到底（跟随最新时的每一步都走它）。 */
function logScrollToEnd() {
  const box = $("logList");
  if (!box) return;
  logRender();
  // 别用 scrollHeight：它含 padding/子元素溢出的零头，用下标算出来的才是准的
  box.scrollTop = Math.max(0, LOGSTREAM.rows.length * LOG_ROW_H - box.clientHeight);
  logSchedule();
}

/** 把第 i 行滚进视口（↑↓ 走它）。 */
function logScrollIntoView(i) {
  const box = $("logList");
  if (!box) return;
  const top = i * LOG_ROW_H;
  if (top < box.scrollTop) box.scrollTop = top;
  else if (top + LOG_ROW_H > box.scrollTop + box.clientHeight) {
    box.scrollTop = top + LOG_ROW_H - box.clientHeight;
  }
}

/** 换空态那句话（清屏、换工作区各有一句）。 */
function logSetHint(text) {
  const el = $("logHint");
  if (el) el.textContent = text;
}

async function jumpToSource(file, line) {
  try {
    const r = await api("/api/fs/find?name=" + encodeURIComponent(file));
    const hits = r.matches || [];
    if (!hits.length) {
      toast("工作区里没找到 " + file, true);
      return;
    }
    let target = hits[0];
    if (hits.length > 1) {
      // 同名多份：让用户选（原版也是这么做的）
      const pick = window.prompt(
        "找到多个同名文件，输编号：\n" +
        hits.map((h, i) => (i + 1) + ". " + h).join("\n"), "1");
      const n = parseInt(pick, 10);
      if (!n || n < 1 || n > hits.length) return;
      target = hits[n - 1];
    }
    await openInEditor(target, baseName(target), line);
  } catch (e) {
    toast("跳转失败：" + e.message, true);
  }
}

function setLogLive(on) {
  LOGSTREAM.on = !!on;
  // 面板上的和侧边栏上的那个得**一起**变 —— 少一个就会出现
  // "面板写着停止、侧边栏还写着运行中"这种自相矛盾
  const panel = $("logLive");
  if (panel) {
    panel.textContent = on ? "■ 停止" : "● 实时";
    panel.classList.toggle("on", !!on);
  }
  const side = $("logSideToggle");
  if (side) {
    side.textContent = on ? "■" : "▶";
    side.classList.toggle("on", !!on);
  }
}

function updateLogStatus(st) {
  const el = $("logSummary");
  if (!el || !st) return;
  LOGSTREAM.status = st;
  const parts = [];
  if (LOGSTREAM.paused) parts.push("⏸ 已暂停");
  // 「显示」报**面板里真正有多少行**，不是后端那 10 万行缓冲 —— 虚拟滚动之后
  // 面板留的比后端缓冲更多（后端的环形缓冲会淘汰，面板不会），
  // 照旧报 st.visible 的话，你会看到"显示 100000"但明明滚得动更多
  parts.push("显示 " + LOGSTREAM.rows.length + " / 缓冲 " + (st.total || 0));

  // 各级别条数（后端 counts()）—— 一眼看出 E/F 有没有在冒
  const order = ["V", "D", "I", "W", "E", "A", "F", "S"];
  const c = LOGSTREAM.counts || {};
  const lv = order.filter((k) => c[k]).map((k) => k + " " + c[k]).join(" ");
  if (lv) parts.push(lv);

  if (st.evicted) parts.push("已淘汰 " + st.evicted);
  // 单次 poll 最多 2000 行，超出的**不丢**，留在后端等下一轮。积压说明
  // 面板暂时落后于设备 —— 点这一行就跳到最新（放弃积压段，见 logJumpToLatest）。
  if (LOGSTREAM.backlog) {
    parts.push("积压 " + LOGSTREAM.backlog + " 行（点此跳到最新）");
  }
  if (st.frozen) parts.push("冻结中（未跟随，丢了 " + st.frozen_dropped + "）");
  if (st.restarts) parts.push("重连 " + st.restarts);
  if (st.error) parts.push("错误：" + String(st.error).slice(0, 30));
  if (st.proc_alive || st.proc_dead) {
    parts.push("进程 " + (st.proc_alive || 0) + " 活"
      + (st.proc_dead ? " / " + st.proc_dead + " 已退出" : ""));
  }
  // 落盘活着没 —— 缓冲只是"看的窗口"，历史在这个文件里。
  // 这一格同时也是"文件里到底写了多少行"的唯一凭据。
  if (st.log_file) parts.push("落盘 " + fmtNum(st.received || 0) + " 行");
  else if (st.save_to_file === false) parts.push("不落盘");
  if (st.file_error) parts.push("落盘失败：" + String(st.file_error).slice(0, 30));

  el.textContent = parts.join(" · ");
  const tips = [];
  if (LOGSTREAM.backlog) {
    tips.push("「积压」= 设备输出比面板拉取快，还有 " + LOGSTREAM.backlog
      + " 行在缓冲里没取过来（不丢，只是慢）；点这一行跳到最新。");
    tips.push("跳过的积压段仍然在 current.log 里（导出 / grep 能看）。");
  }
  el.title = parts.join("\n")
    + (tips.length ? "\n\n" + tips.join("\n") : "")
    + (st.log_file ? "\n\n" + st.log_file : "");
}

function stopLogPolling() {
  if (LOGSTREAM.timer) {
    clearTimeout(LOGSTREAM.timer);
    LOGSTREAM.timer = null;
  }
}

function startLogPolling() {
  stopLogPolling();
  const tick = async () => {
    // 暂停中 / 已停止就不要再轮询了（暂停时引擎照收，恢复走 snapshot 补齐）
    if (!LOGSTREAM.on || LOGSTREAM.paused) return;
    await logPollOnce();
    if (LOGSTREAM.on && !LOGSTREAM.paused) LOGSTREAM.timer = setTimeout(tick, 900);
  };
  LOGSTREAM.timer = setTimeout(tick, 150);
}

async function logPollOnce() {
  if (LOGSTREAM.paused) return;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_poll",
                        logArgs({ since: LOGSTREAM.cursor }));
    const d = (r.result || {}).data || {};
    if (typeof d.cursor === "number") LOGSTREAM.cursor = d.cursor;
    // 积压 ≠ 丢弃：没取完的行还在后端缓冲里，下一轮接着取
    // （cursor 只推进到本批最后一条，见 do_logcat_poll）
    LOGSTREAM.backlog = d.backlog || 0;
    appendLogEntries(d.entries || []);
    if (d.status) updateLogStatus(d.status);
    // 后端已经停了（设备拔了 / 重连彻底失败）：界面不能继续挂着「运行中」，
    // 否则看着像还连着，其实一条日志都不会再来。
    if (d.status && d.status.stopped_reason) {
      const why = d.status.stopped_reason;
      await logStop();
      logSetHint("日志采集已停止：" + why);
      toast("日志采集已停止：" + why, true);
    } else if (d.status && d.status.running === false) {
      // 引擎悄没声地没了、而且**没有 stopped_reason**：最常见的是服务重启
      // —— 前端页面没刷新，后端已经是新进程，那边的引擎从没启动过。
      // 以前这里只认 stopped_reason，于是界面永远挂着「■ 停止」、面板永远
      // 不出新日志，看着像插件坏了。
      await logStop();
      logSetHint("日志采集已停止（后端引擎不在了，多半是服务重启过）\n"
                 + "点上面的「● 实时」重新开始");
    }
  } catch (e) {
    // 轮询失败不打扰用户，下一轮自己会恢复
  }
}

/**
 * 追平积压：cursor 直接抬到最新，并用尾部一屏重建面板。
 *
 * 这是**用户主动放弃**中间那段（它们还在 current.log 里），换立刻回到实时。
 * 只由状态栏「积压 N 行（点此跳到最新）」触发。
 */
async function logJumpToLatest() {
  if (!LOGSTREAM.on || LOGSTREAM.paused) return;
  if (!LOGSTREAM.backlog) {          // 没积压：点一下就当"滚到底"
    if (LOGSTREAM.follow) logScrollToEnd();
    return;
  }
  try {
    const r = await api("/api/plugin/adb_tools/logcat_poll",
                        logArgs({ since: LOGSTREAM.cursor, jump: true }));
    const d = (r.result || {}).data || {};
    if (typeof d.cursor === "number") LOGSTREAM.cursor = d.cursor;
    LOGSTREAM.backlog = 0;
    if (d.entries && d.entries.length) logSetRows(d.entries);
    if (d.status) updateLogStatus(d.status);
    if (LOGSTREAM.follow) logScrollToEnd();
    toast("已跳到最新"
      + (d.skipped ? "，跳过积压 " + d.skipped + " 行" : "")
      + "（跳过的仍在 current.log 里）");
  } catch (e) {
    toast("跳到最新失败：" + e.message, true);
  }
}

function appendLogEntries(list) {
  if (!list || !list.length) return;
  for (const e of list) {
    LOGSTREAM.rows.push(e);
    const len = logCharLen(e);
    if (len > LOGSTREAM.maxChars) LOGSTREAM.maxChars = len;
  }
  if (LOGSTREAM.rows.length > LOG_MAX_ROWS) {
    logEvictRows(LOGSTREAM.rows.length - LOG_MAX_ROWS);
  }
  if (LOGSTREAM.follow) logScrollToEnd();
  else logSchedule();
}

/**
 * 砍掉最旧的 count 行。
 *
 * 下标是选中态的全部依据，所以砍完得把 anchor / focus / 选中的那些**整体前移** ——
 * 不挪的话你会看到"选中的行自己漂到别的内容上去了"，那比没选中还糟糕。
 */
function logEvictRows(count) {
  if (!count) return;
  LOGSTREAM.rows.splice(0, count);
  LOGSTREAM.anchor = LOGSTREAM.anchor < 0 ? -1
    : Math.max(-1, LOGSTREAM.anchor - count);
  LOGSTREAM.focus = LOGSTREAM.focus < 0 ? -1
    : Math.max(-1, LOGSTREAM.focus - count);
  if (LOGSTREAM.sel.size) {
    const next = new Set();
    for (const i of LOGSTREAM.sel) if (i >= count) next.add(i - count);
    LOGSTREAM.sel = next;
  }
  // 没跟随时用户正盯着上面某一行，内容整体上移了就得把他看的位置一起挪，
  // 不然那一行会从他眼皮子底下滑走
  const box = $("logList");
  if (box && !LOGSTREAM.follow) {
    box.scrollTop = Math.max(0, box.scrollTop - count * LOG_ROW_H);
  }
}

/** 整份替换（重筛 / 重新同步 / 清空 / 开抓）。列表视图下这是几个入口的公共动作。 */
function logSetRows(list) {
  LOGSTREAM.rows = [];
  LOGSTREAM.sel = new Set();
  LOGSTREAM.anchor = -1;
  LOGSTREAM.focus = -1;
  LOGSTREAM.maxChars = 0;
  const box = $("logList");
  if (box) box.scrollTop = 0;
  appendLogEntries(list || []);
  if (LOGSTREAM.follow) logScrollToEnd();
  else logSchedule();
}

/**
 * 启动常驻抓取。
 *
 * 请求体固定带 `origin: "panel"`：后端据此把「这条流是面板起的」记在引擎上。
 * 关面板时前端带 `only_origin="panel"` 去停，后端只有归属对得上才真停 ——
 * 于是 AI 用工具起的流不会被用户关个面板顺手带走。
 *
 * 返回是否成功（false = 启动失败）。注意它不再表示「这次是自动起的」，
 * 归属由后端记，见上面 LOGSTREAM 里那段说明。
 */
async function logStart(serial) {
  if (LOGSTREAM.on) return true;

  // 设备列表可能还没枚举过（一进来就点「● 实时」）。空字符串是**合法**的
  // —— 后端会自动挑唯一那台在线设备；这里不能塞占位文字进去。
  // 顺带把设备列表拉起来，免得"没进过设备视图就打不开日志"。
  await ensureDevices();

  const s = serial || $("devSel").value || "";
  try {
    const r = await api("/api/plugin/adb_tools/logcat_start",
                        logArgs({ serial: s, filters: logStreamFilters(),
                                  origin: "panel" }));
    const res = r.result || {};
    if (!res.ok) {
      toast(res.summary || res.error || "启动失败", true);
      return false;
    }
    LOGSTREAM.cursor = (res.data && res.data.cursor) || 0;
    LOGSTREAM.paused = false;
    LOGSTREAM.serial = s || (res.data && res.data.status && res.data.status.serial) || "";
    paintLogPause();
    logSetHint(LOG_HINT_DEFAULT);
    // 后端顺带回一屏尾巴：面板一开就有内容，不用干等第一条新日志。
    // （引擎是进程内单例，可能早就收了一堆；cursor 却被抬到最新，
    //   只靠增量的话面板会是空的。）
    logSetRows((res.data && res.data.entries) || []);
    LOGSTREAM.counts = {};
    LOGSTREAM.backlog = 0;      // 新一轮抓取，积压计数从头算
    setLogLive(true);
    setLogFollow(true);
    startLogPolling();
    if (res.data && res.data.status) updateLogStatus(res.data.status);
    renderLogSide();
    return true;
  } catch (e) {
    toast("启动失败：" + e.message, true);
    return false;
  }
}

/**
 * 停止常驻抓取。
 *
 * `opts.onlyOrigin` / `opts.onlyIdle` 透传给后端做**有条件停止**：
 * 关面板走 `{onlyOrigin:"panel"}`，AI 正在抓的那条不会跟着停；页面加载时
 * 回收孤儿流走 `{onlyOrigin:"panel", onlyIdle:10}`（还有窗口在轮询就不停）。
 *
 * 用户手动点「● 实时」停止时不带条件 —— 那是明确要停，不管归属。
 */
async function logStop(opts) {
  const o = opts || {};
  stopLogPolling();
  setLogLive(false);
  LOGSTREAM.paused = false;
  paintLogPause();
  const body = logArgs();
  if (o.onlyOrigin) body.only_origin = o.onlyOrigin;
  if (o.onlyIdle) body.only_idle = o.onlyIdle;
  let res = {};
  try {
    res = ((await api("/api/plugin/adb_tools/logcat_stop", body)) || {}).result || {};
  } catch (e) { /* 停失败不该挡住界面 */ }
  const sum = $("logSummary");
  if (sum) {
    // 后端拒绝停（这条流不是面板起的）时要写清楚，否则用户看着
    // "已停止" 却发现状态栏还在跑，只会以为界面坏了
    sum.textContent = res.skipped ? (res.summary || "后台保留") : "已停止";
  }
  renderLogSide();
}

async function logLiveToggle() {
  if (LOGSTREAM.on) return logStop();
  return logStart();
}

async function logRefilter() {
  // 条件变了：让后端原地重筛（不用重连），然后整块替换
  if (!LOGSTREAM.on) return;
  try {
    const r = await api("/api/plugin/adb_tools/logcat_filters",
                        logArgs({ filters: logStreamFilters(), lines: 800 }));
    const d = (r.result || {}).data || {};
    if (typeof d.cursor === "number") LOGSTREAM.cursor = d.cursor;
    logSetRows(d.entries || []);
    if (d.status) updateLogStatus(d.status);
    if (d.counts) LOGSTREAM.counts = d.counts;
    if (d.bad && d.bad.length) toast("正则写错了：" + d.bad.join(", "), true);
  } catch (e) {
    // 忽略
  }
}

// ---------------------------------------------------------------------------
// 面板生命周期（搬自原插件 attachPanel / autoStartForPanel / stopForPanel）
// ---------------------------------------------------------------------------
//
// 原插件是「打开 webview 面板」；这里「面板」= 活动栏里的 Logcat 视图，
// 所以挂到 VIEW_ONSHOW / VIEW_ONHIDE 上。
//
// 打开面板要做的四件事，顺序有讲究：
//   ① 清累计进程清单 + 立刻补一次 ps（清完不补的话列表先空着）
//   ② 解暂停（上次按着暂停的话，打开面板看不到新日志 —— 看着像坏了）
//   ③ 复位「跟随最新」（面板一关就没人再发 follow 消息了）
//   ④ 没在跑就自动起一条，并记下"这次是面板起的"

function startLogProcTimer() {
  stopLogProcTimer();
  LOGSTREAM.procTimer = setInterval(() => {
    // 没在看就不白跑 adb。注意判据是**面板是不是开着**，
    // 不是"左栏是不是 log 视图" —— 面板开在编辑区，左栏可以切到文件去。
    if (!ED_SHOW_LOGCAT) return;
    refreshLogProcs(false);
  }, LOG_PROC_REFRESH_MS);
}

function stopLogProcTimer() {
  if (LOGSTREAM.procTimer) {
    clearInterval(LOGSTREAM.procTimer);
    LOGSTREAM.procTimer = null;
  }
}

async function logcatPanelOpen() {
  // ① 进程清单是累计的，**只在打开面板时清一次**
  await refreshLogProcs(true);

  // ② 解暂停
  if (LOGSTREAM.paused) await setLogPaused(false);

  // ③ 复位跟随 + 跳到最新
  setLogFollow(true, true);
  logScrollToEnd();

  // ④ 没在跑就起一条
  if (!LOGSTREAM.on) {
    await logStart();
  } else if (!LOGSTREAM.timer) {
    // 引擎还在跑（上次切走时只是停了轮询）：拉一次 snapshot 补上这段时间的，
    // 再接着增量轮询。只靠增量会断档 —— logcat_poll 一次最多 2000 行。
    await logResync();
    // 但「前端以为自己还在收」不等于后端真的在跑：设备重连把进程收了、
    // 别处调过 logcat_stop、换工作区复位过 —— 这几种情况下只补 snapshot
    // 会得到一个「跟随中、却永远不再出新行」的面板（就是不自动运行的那种感觉）。
    // 以 snapshot 带回来的 status.running 为准，没跑就重新起一条。
    const st = LOGSTREAM.status || {};
    if (st.running === false) {
      LOGSTREAM.on = false;              // 先把前端状态摆正，否则 logStart 直接返回 true
      await logStart();
    } else {
      startLogPolling();
    }
  }
  // ⑤ 引擎可能带着上次的"冻结"状态（页面刷新过，JS 这边以为是跟随）：
  //    主动对一次，冻结过的话会从 current.log 回灌
  if (LOGSTREAM.on) syncLogFollow(true);
  startLogProcTimer();
}

/**
 * 在独立窗口打开 Logcat。
 *
 * 用固定窗口名：重复点只把那个窗口提到前面，不会开出一堆。
 * 地址带 `?detached=log`，那个窗口会把除日志面板以外的东西全藏掉
 * （见 style.css 的 body.detached-log 和 10-login.js 的启动分支）。
 *
 * 窗口真开出来之后，顺手把编辑区那个 Logcat 标签收掉 —— 日志已经在另一块
 * 更宽敞的地方显示了，主窗口再挂一格只是白占地方，还得自己点 ✕。收的只是
 * **标签**（closeLogcatPanel → logcatPanelHide），引擎照旧在后台收，独立窗口
 * 那边靠 snapshot + 增量轮询跟上；弹窗被拦下（w 为空）时什么都不动。
 */

//: 由「⧉ 独立」开出去的那个窗口（关上之后 .closed 变成 true）。
//: 关面板（logcatPanelHide）靠它判断「还有没有人在看」——
//: 独立窗口整个靠轮询引擎缓冲存活，引擎一停它就地冻住。
let LOG_DETACHED_WIN = null;

function openLogcatDetached() {
  const url = location.pathname + "?detached=log";
  const w = window.open(url, "logcat-detached",
                        "width=1200,height=820,menubar=no,toolbar=no");
  if (!w) {
    toast("浏览器拦住了弹窗 —— 允许本站弹出窗口后再点一次", true);
    return;
  }
  try { w.focus(); } catch (e) { /* 跨窗口受限，忽略 */ }

  // 记住这个窗口：它是"还有人在看"的证据。主窗口关面板时（logcatPanelHide）
  // 靠它判断能不能停引擎 —— 独立窗口只做 snapshot + 增量轮询，引擎一停它就没内容了。
  LOG_DETACHED_WIN = w;
  // 独立窗口自己那里不关（那里就是主显示区，关了就没东西了）。
  if (!DETACHED_LOG && typeof closeLogcatPanel === "function" && LOG_PANEL_OPEN) {
    closeLogcatPanel();
  }
}

/**
 * 独立窗口接管显示：**只跟随，不启停、不改过滤条件**。
 *
 * 为什么不能直接调 logcatPanelOpen()：它会 `logcat_start`，而 start 会带上
 * **本窗口**的过滤条件（默认值）去覆盖后端共享的那个引擎 —— 主窗口里刚调好的
 * Tag/PID 筛选会被悄悄改掉。所以这里只做 snapshot + 增量轮询。
 */
async function logcatPanelAttach() {
  await refreshLogProcs(true);
  await logResync();

  // 引擎是共享的：独立窗口只负责显示，把「实时」按钮改成状态显示，
  // 避免在这里一点就把主窗口的抓取停掉。
  const live = $("logLive");
  if (live) {
    live.textContent = "跟随中";
    live.disabled = true;
    live.title = "独立窗口只显示日志；启动 / 停止请回主窗口操作";
  }

  const hasRows = Array.isArray(LOGSTREAM.rows) && LOGSTREAM.rows.length > 0;
  LOGSTREAM.on = true;          // 轮询开关（不是"引擎在跑"的意思，见上）
  setLogLive(true);
  startLogPolling();
  if (!hasRows) {
    logSetHint("独立窗口只负责显示 —— 在主窗口点「● 实时」后，这里会自动跟上");
  }
  startLogProcTimer();
}

function logcatPanelHide() {
  stopLogProcTimer();
  // 独立窗口还开着：那个窗口全靠轮询引擎的缓冲活着，这里一停它就地冻住。
  // 只停本窗口的轮询，引擎留给它。
  if (LOG_DETACHED_WIN && !LOG_DETACHED_WIN.closed) {
    if (LOGSTREAM.on && !LOGSTREAM.paused) stopLogPolling();
    return;
  }
  // 其余情况一律停：关面板就是「不想再抓了」。让 adb logcat 一直挂在设备上
  // 既占设备，又一直在往 <工作区>/.ai/temp/logcat/ 里写。
  //
  // 但停之前要让**后端**确认这条流是面板起的：以前那个 autoStarted 只活在
  // 本窗口内存里，用过一次独立窗口就被抹成 false，之后关面板永远不停；
  // 而 AI 用 logcat_start 起的流反倒会被顺手停掉。现在归后端判。
  //
  // 本窗口这条流压根没起来（`on=false`）就别发请求了：多标签下那会去停
  // 另一个标签正在抓的流，而用户关的只是自己这个空面板。
  if (!LOGSTREAM.on) return;
  logStop({ onlyOrigin: "panel" }).catch(() => { /* 停失败也不该挡住界面 */ });
}

/**
 * 面板从「藏着」变回「显示」时接上（内核 showLogcatPanel 走这里）。
 *
 * 和 logcatPanelOpen 的区别：**不改过滤条件、不强制跳到最新** —— 用户只是
 * 切了个文件标签回来，把滚动位置推平是帮倒忙。这里只保证两件事：
 * 引擎真的在跑、轮询在转。关面板时停掉的那条流在这里会重启。
 */
async function logcatPanelResume() {
  // 本来就没在收 / 用户自己按的暂停：不要擅自恢复
  if (!LOGSTREAM.on || LOGSTREAM.paused) return;
  if (!LOGSTREAM.timer) await logResync();
  const st = LOGSTREAM.status || {};
  if (st.running === false) {
    LOGSTREAM.on = false;                 // 摆正前端状态，否则 logStart 直接返回 true
    await logStart();
  } else if (!LOGSTREAM.timer) {
    startLogPolling();
  }
}

// ---------------------------------------------------------------------------
// 换工作区：日志整个复位
// ---------------------------------------------------------------------------
// 这次抓取属于哪个工作区
let LOG_WS = "";

/**
 * 换工作区就把抓取停下、面板复位。
 *
 * 为什么不能照「切走视图」那样留着引擎跑（那是有意为之，见 logcatPanelHide）：
 * 日志是**按工作区落盘**的（`<工作区>/.ai/temp/logcat/current.log`），而引擎在收到带新
 * 工作区的调用后会把落盘目录指过去 —— 于是这次抓取一半写在旧工程、一半写在新
 * 工程，面板上两边混着显示，两个文件都不完整。切工作区是个明确的动作，停掉最干净。
 *
 * 设备与工作区无关的那些（设备清单、进程清单）**不动**。
 */
async function resetLogForWorkspace() {
  if (LOGSTREAM.on) await logStop();   // 停轮询 + 停引擎 + 把那几个按钮摆回来
  LOGSTREAM.cursor = 0;
  LOGSTREAM.counts = {};
  LOGSTREAM.status = null;
  LOGSTREAM.followProcs = [];

  logSetHint("换了工作区 —— 日志的落盘目录也跟着换（<工作区>/.ai/temp/logcat/）\n" +
    "点「● 实时」抓这个工作区的");
  logSetRows([]);
  const sum = $("logSummary");
  if (sum) sum.textContent = "换了工作区，已停止";
  renderLogSide();
}

/** 换工作区就复位（没进过这个视图、或者工作区没变，都直接返回）。 */
function ensureLogWorkspace() {
  if (!VIEW_INIT.log) return;
  if (LOG_WS === (S.workspace || "")) return;
  LOG_WS = S.workspace || "";
  resetLogForWorkspace();
}

function initLogView() {
  renderLogLevels();                 // 级别开关是动态生成的

  // 状态栏平时只是统计；有积压时点一下追到最新（见 logJumpToLatest）
  $("logSummary").addEventListener("click", logJumpToLatest);

  $("logLive").addEventListener("click", logLiveToggle);        // 面板上那个
  $("logSideToggle").addEventListener("click", logLiveToggle);  // 侧边栏上那个
  $("logOpen").addEventListener("click", () => openLogcatPanel());

  // 面板上的「设备」按钮：左键 / 右键都出设备菜单（换设备 / 固定 5555 / 重新枚举）。
  // 这个按钮以前**没绑任何事件**，点了毫无反应。
  const devPick = $("logDevPick");
  if (devPick) {
    devPick.addEventListener("click", (ev) => logDevicePickMenu(ev));
    devPick.addEventListener("contextmenu", (ev) => logDevicePickMenu(ev));
  }

  // 独立窗口：同一份界面在**新窗口**里打开，只显示日志面板。
  // 用 window.open 而不是再造一个页面：登录态、插件资源、接口全都在本地
  // localStorage / 同源里，开个新窗口就能直接用。
  const det = $("logDetach");
  if (det) det.addEventListener("click", () => openLogcatDetached());
  $("logPause").addEventListener("click", () => setLogPaused(!LOGSTREAM.paused));
  $("logTail").addEventListener("click", logJumpTail);
  $("logProcs").addEventListener("click", () => refreshLogProcs(true));
  // 「清空」要连后端缓冲一起清 —— 只清 DOM 的话，
  // 一改过滤条件后端把缓冲重新筛一遍，刚清掉的内容又整片回来了
  $("logClear").addEventListener("click", logClearLocal);
  $("logClearDev").addEventListener("click", logClearDevice);
  $("logExport").addEventListener("click", logExport);
  $("logData").addEventListener("click", () => logDataPanel());
  $("logPickPkg").addEventListener("click", () => logPick("packages", "logPackage"));
  $("logMore").addEventListener("click", () => {
    const nowHidden = $("logMoreBox").classList.toggle("hidden");
    $("logMore").classList.toggle("on", !nowHidden);
    $("logMore").textContent = nowHidden ? "▼" : "▲";
  });

  // 条件一改就重筛。用 input + 去抖 —— 原版也是"边打边生效，停手才重算"，
  // 不然连打 `carplay` 会重筛七次。
  for (const id of ["logTag", "logTagEx", "logMsg", "logMsgEx", "logPackage"]) {
    $(id).addEventListener("input", logRefilterSoon);
  }
  // 手动动过 PID 条件 = 停止跟随（原版同）。不停的话过几秒跟随又把 PID 加回来，
  // 你会觉得"我明明删了它怎么又出现了"。
  $("logPid").addEventListener("input", () => {
    if (LOGSTREAM.followProcs.length) {
      LOGSTREAM.followProcs = [];
      renderLogSide();
    }
    logRefilterSoon();
  });
  for (const id of ["logRegex", "logCase", "logMarkers", "logStrict"]) {
    $(id).addEventListener("change", logRefilter);
  }

  const box = $("logList");

  // 往上翻就自动停止跟随 —— 不然你想看上面的内容，新行一直把你拽回底部
  box.addEventListener("scroll", () => {
    if (LOGSTREAM.follow && !logNearBottom()) setLogFollow(false);
    // **必须重画**。行是按「下标 × 行高」绝对定位的，画出来的那一批只覆盖
    // 当时的 scrollTop 那一屏；滚动之后不重画，新露出来的区域里一个节点都没有
    // —— 表现就是"往上滚一片空白"，而且越往上滚空得越多。
    // （这里以前只关了跟随，所以只有"来新日志"时才会重画，手滚是死的。）
    logSchedule();
  }, { passive: true });

  // 点空白处：清掉选中 + 恢复跟随
  box.addEventListener("click", (ev) => {
    if (ev.target === box) { logSelClear(); setLogFollow(true); }
  });

  // 空白处右键：复制划选 / 全选 / 清屏。（点在日志行上由 logItemMenu 处理，
  // 那里会 stopPropagation，所以不会走到这来）
  box.addEventListener("contextmenu", (ev) => {
    if (ev.target.closest(".vrow")) return;
    logPanelMenu(ev);
  });

  // 键盘：↑↓ 选行、Shift+↑↓ 连选、End 跳到最新、Ctrl+C 复制选中、Esc 取消
  //
  // 全部按**下标**算，不按 DOM —— 虚拟滚动下 DOM 里只有视口那几十行，
  // "现在选中第几行"这个问题只有模型答得出来。
  box.addEventListener("keydown", (ev) => {
    const n = LOGSTREAM.rows.length;
    if (!n) return;

    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      const dir = ev.key === "ArrowDown" ? 1 : -1;
      let i = LOGSTREAM.focus;
      if (i < 0) i = dir > 0 ? -1 : n;      // 还没选过：向下从第一行，向上从最后一行
      i = Math.max(0, Math.min(n - 1, i + dir));
      LOGSTREAM.focus = i;

      if (ev.shiftKey && LOGSTREAM.anchor >= 0) {
        // Shift+↑↓ = 从锚点连选（锚点不动，另一端跟着走）
        LOGSTREAM.sel.clear();
        for (let k = Math.min(LOGSTREAM.anchor, i);
             k <= Math.max(LOGSTREAM.anchor, i); k++) LOGSTREAM.sel.add(k);
      } else {
        LOGSTREAM.anchor = i;
        LOGSTREAM.sel.clear();
        LOGSTREAM.sel.add(i);
      }
      logScrollIntoView(i);     // 行可能还没画出来，所以滚的是下标不是元素
      logSchedule();
      return;
    }
    if (ev.key === "End") { ev.preventDefault(); logJumpTail(); return; }
    if (ev.key === "Escape") { logSelClear(); return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "c") {
      // 有划选时让浏览器自己复制（用户要的是那一段字面文本）
      if (String(window.getSelection() || "").trim()) return;
      ev.preventDefault();
      logCopySelected();
    }
  });

  // 尺寸一变就得重画。
  //
  // 行的坐标是「下标 × 行高」的绝对值，视口一变（拖窗口、拉分隔条、切布局、
  // 横向滚动条进出）浏览器会把 scrollTop 收下来贴到底部，可已经画好的行
  // 还留在原坐标上 —— 上面就露白了。以前没人监听尺寸变化，只有"来新日志"
  // 和"滚动"会重画，所以一旦不推新数据（缓冲满了没跟随），露白就一直挂着。
  const logOnResize = () => {
    const b = $("logList");
    if (!b || !b.clientHeight) return;                 // 面板被藏起来（高度 0）不用画
    if (b.clientHeight === LOGSTREAM.viewH && b.clientWidth === LOGSTREAM.viewW) return;
    LOGSTREAM.viewH = b.clientHeight;
    LOGSTREAM.viewW = b.clientWidth;
    if (LOGSTREAM.follow) logScrollToEnd();
    else logSchedule();
  };
  // 让出当前帧再改 DOM：在 ResizeObserver 回调里同步改会报
  // "ResizeObserver loop completed with undelivered notifications"，白刷一条错
  const logDeferredResize = () => setTimeout(logOnResize, 0);
  try {
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(logDeferredResize).observe(box);
    }
  } catch (e) { /* 老 Chromium 没有 ResizeObserver，靠下面的 window.resize 兜底 */ }
  window.addEventListener("resize", logDeferredResize);

  // 标签页切回前台时补一波：切走期间 rAF 被挂起，回来的第一帧可能已经过时了
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (LOGSTREAM.follow) logScrollToEnd();
    else logSchedule();
  });

  // 空态那句话放到 JS 里（清屏/换工作区会改成别的，这里给个初始值）
  logSetHint(LOG_HINT_DEFAULT);

  // 切到左栏的 Logcat **只进侧栏**（设备 / 操作 / 进程列表），不自动打开编辑区面板。
  // 面板是"看日志"的地方：它会在编辑区占掉一屏，还要起一条常驻 adb logcat 流。
  // 只想扫一眼设备清单或进程表时，这两个副作用都不该白付 —— 所以面板改成显式打开
  // （侧栏那行「▷ 打开 Logcat 面板」或状态行，见 paintLogSide）。
  viewRegister({ view: "log", onshow: () => {
    loadLogDevices();
  } });
  // 切走左栏**不关**面板 —— 面板开在编辑区，跟左栏显示哪一页没关系。
  // （原版也一样：面板在编辑器那一栏，侧边栏切走不影响它。）
  VIEW_ONHIDE.log = () => {};

  // 先摆好按钮的状态（还没开抓，但"暂停""⇣"的初始态要对）
  paintLogPause();
  paintLogFollow();
  renderLogSide();
  // 进程定时刷新一直挂着；回调里会判断面板是不是正开着，
  // 没开就不白跑 adb
  startLogProcTimer();

  // 日志是按工作区落盘的，换工作区要停掉重来（见 resetLogForWorkspace）
  WS_HANDLERS.log = ensureLogWorkspace;
  LOG_WS = S.workspace || "";
}

/** 面板空白处的右键菜单。原版就两条，内容看有没有划选。 */
function logPanelMenu(ev) {
  ev.preventDefault();
  ctx.innerHTML = "";
  const sel = String(window.getSelection() || "").trim();

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  if (sel) {
    item("复制", () => copyText(sel));      // 原样复制划选那段，不去头尾不拼整行
  } else {
    item("全选", logSelAll);                // 选中已渲染的日志行，随后 Ctrl+C
  }
  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  // 破坏性的排最后 —— 菜单第一项别做成「清屏」，免得手快点掉
  item("清屏", logClearLocal);

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

// 原来这里还有个 runLogcat（一次性 `adb logcat -d` 抓 300 行）。
// 现在只剩常驻流这一条路 —— 两套并存的时候"点抓取"会把实时流停掉，
// 而过滤、暂停、跟随这些状态又都只对实时流有意义，很容易互相打架。
// 后端那个 logcat action 还在（给命令行 / 脚本用）。

// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 文件里已有的 viewRegister(...) 是 init 内部补 onshow 用的，那是"进了视图之后"；
// 这一条管的是"第一次进视图该跑什么" —— 没有它，按钮点开是空白。
viewRegister({
  view: "log",
  setters: () => initLogView(),
});

// ---------------------------------------------------------------------------
// 关页面 / 刷新的兜底：那条面板流的归属在后端，前端不吭声就没人收
// ---------------------------------------------------------------------------
//
// 正常关面板走 logcatPanelHide，够用。但刷新页面、直接关标签、浏览器被杀
// 这几种情况前端**没机会**跑那段代码，而后端引擎是进程内单例、照旧在抓，
// 于是一条 origin="panel" 的流就那么挂着：占着 adb，还一直往
// <工作区>/.ai/temp/logcat/current.log 里写，用户以为早就停了。

// ① 卸载时补一枪。pagehide 阶段还能发出请求，keepalive 让它在页面没了之后
//    继续送达（不是所有浏览器都保证，所以下面还有 ②）。
window.addEventListener("pagehide", () => {
  if (DETACHED_LOG || !LOGSTREAM.on) return;
  if (LOG_DETACHED_WIN && !LOG_DETACHED_WIN.closed) return;  // 独立窗口还在看
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers["Authorization"] = "Bearer " + S.token;
  const body = Object.assign({}, logArgs(), { only_origin: "panel" });
  if (S.workspace) body.ws = S.workspace;      // 和 api() 的拼法保持一致
  try {
    fetch("/api/plugin/adb_tools/logcat_stop", {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => { /* 忽略 */ });
  } catch (e) { /* 忽略 */ }
});

// ② 加载后收一次**上一次会话**留下的孤儿流（浏览器被直接杀掉时 ① 发不出去）。
//    只在本窗口此刻没在看的时候动手；后端还有 only_idle 兜底 —— 别的窗口
//    正在轮询（10 秒内有 poll / snapshot）就不会停，多标签不会互相踩。
setTimeout(() => {
  if (DETACHED_LOG || LOG_PANEL_OPEN || LOGSTREAM.on) return;
  if (!S.token) return;                        // 还没登录，无权调用
  api("/api/plugin/adb_tools/logcat_stop",
      { only_origin: "panel", only_idle: 10 }).catch(() => { /* 忽略 */ });
}, 3000);
