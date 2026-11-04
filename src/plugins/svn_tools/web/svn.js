

// ---------------------------------------------------------------------------
// 右键菜单的显示判断：这个目录在不在某个工作副本里
// ---------------------------------------------------------------------------
// 以前三条 SVN 菜单（更新 / 提交 / 清理）在任何目录都显示，非 SVN 项目里
// 点了要么报错、要么弹一堆 svn 的 W155007（"不在版本控制内"）。
//
// 数据来自后端 `wc_roots`（走持久化清单，命中缓存时只是一次文件读）。
// **没查到就一律显示**：宁可多一条，也不要在加载完成前把所有 SVN 项藏掉 ——
// 那看起来就像插件坏了。
let SVN_WCS = null;                       // null = 还不知道
const SVN_WCS_STATE = { busy: false, asked: false, rescanned: false };

/** 副本清单多久算「过期」（秒）。清单是"扫一次就一直用"的，新 checkout 出来的
 *  副本不会自己进清单 —— 过期就后台补扫一次，见 svnLoadWcs 末尾。 */
const SVN_WCS_STALE_SEC = 6 * 3600;

/** 路径比较统一口径：正斜杠 + 小写 + 去尾斜杠（Windows 盘符大小写不定）。 */
function svnNormPath(p) {
  return String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

/** 这个路径的 SVN 作用范围 —— 右键菜单据此决定「显不显示」以及「显示成哪种范围」。
 *
 *  - `"dir"` 它本身就在某个工作副本里 → 动作只作用于这一层（老行为）
 *  - `"ws"`  它不是副本，但**下面挂着**副本（典型：工作区根 D:\workspace\demo，
 *            下面几十个各自带 .svn 的模块）→ 动作作用于整棵工作区，
 *            走后端的多副本分支（update / cleanup 都不带 path）
 *  - `null`  都不沾 → 不显示 SVN 项
 *
 *  以前只有「在副本里 / 不在」两态，于是**在工作区根上右键，整条 SVN 菜单凭空
 *  消失** —— 而它恰恰是最想「把整个工作区更新一遍」的那个位置。判据和设备清单
 *  都不认识的目录（既不是副本、下面也没副本）才真的不显示。
 *
 *  未知（清单没到 / 为空）时一律按 `"dir"`：宁可多显示一条，也不要在加载完成前
 *  把所有 SVN 项藏掉 —— 那看起来就像插件坏了。
 */
function svnPathScope(p) {
  // 空数组和 null 一样按"不知道"处理：一个工作副本都没扫到，多半是服务端
  // 还没缓存 / 工作区刚切过去。
  if (!SVN_WCS || !SVN_WCS.length) {
    if (!SVN_WCS_STATE.asked) svnLoadWcs();   // 第一次右键顺手问一次后端
    return "dir";
  }
  const t = svnNormPath(p);
  if (!t) return "dir";
  // ① 落在某个副本里（含副本根自己）
  for (const r of SVN_WCS) {
    const b = svnNormPath(r);
    if (b && (t === b || t.indexOf(b + "/") === 0)) return "dir";
  }
  // ② 自己不是副本，但下面挂着副本：工作区根、或中间的分组目录
  if (svnWcsUnder(t).length) return "ws";
  return null;
}

/** 某个目录**之下**的副本根（口径同 svnNormPath，不含它自己）。 */
function svnWcsUnder(p) {
  const t = svnNormPath(p);
  if (!t || !SVN_WCS || !SVN_WCS.length) return [];
  return SVN_WCS.filter((r) => {
    const b = svnNormPath(r);
    return !!b && b !== t && b.indexOf(t + "/") === 0;
  });
}

/** 这个路径属于某个工作副本吗？（未知时返回 true，先都显示） */
function svnPathInWc(p) {
  return !!svnPathScope(p);
}

async function svnLoadWcs(rescan) {
  if (SVN_WCS_STATE.busy) return;
  SVN_WCS_STATE.busy = true;
  SVN_WCS_STATE.asked = true;
  try {
    const r = await api("/api/plugin/svn_tools/wc_roots",
                        rescan ? { rescan: true } : {});
    const d = (r && r.result && r.result.data) || {};
    SVN_WCS = Array.isArray(d.roots) ? d.roots : [];
    // 清单是"扫一次就一直用"的（见 _svn.wc_cache_age 的注释）：新 checkout 出来的
    // 副本**不会自己进清单**，那些目录右键永远没有 SVN 项，而前端又没有「重扫」
    // 入口 —— 用户无法自愈。所以过期就在后台再问一次（rescan=true），
    // **不阻塞这次右键**：这一次先按旧清单显示，下一次右键就是新的。
    //
    // 后端侧的那个 rescan 现在是**后台线程扫盘 + 立即返回旧清单**（以前同步
    // 扫盘要 10 秒，正好和首屏的 status 抢磁盘），所以这里连 setTimeout 都不必：
    // 调一次就够，扫完下一次 wc_roots 自然是新的。
    // `rescanned` 保证一个会话只自动重扫一次，避免清单一直算"过期"时反复扫盘。
    const age = Number(d.age);
    if (!rescan && !SVN_WCS_STATE.rescanned && isFinite(age)
        && (age < 0 || age > SVN_WCS_STALE_SEC)) {
      SVN_WCS_STATE.rescanned = true;
      svnLoadWcs(true);
    }
  } catch (e) {
    SVN_WCS = null;                 // 查不到 → 保持"都显示"
    SVN_WCS_STATE.asked = false;    // 下次右键再试
  } finally {
    SVN_WCS_STATE.busy = false;
  }
}

/**
 * 让后端**现在**就去扫一遍，把结果放进它的内存快照里。
 *
 * 为什么要有这个：打开面板的十几秒里，大头是后端扫盘 + `svn status`。
 * 后端现在会用内存快照（见 `__init__.py` 的「内存快照」一节），但快照得
 * **先有人算过**才有 —— 否则第一次打开照样要等。所以在真正需要它之前就先
 * 丢一次请求：启动、切工作区、鼠标移到 SVN 图标上（`svnWarm` 在 initSvnView
 * 里挂到了按钮的 mouseenter / focus）。
 *
 * 后端立即返回（扫描在它自己的后台线程里），所以这里不 await 也无所谓；
 * 仍然 await + 吞异常，纯粹是为了不产生 unhandledrejection。
 */
async function svnWarm() {
  try {
    await api("/api/plugin/svn_tools/warm", svnStatusOpts());
  } catch (e) { /* 未登录 / 没工作区：下次打开面板时再算，不影响功能 */ }
}

// ---------------------------------------------------------------------------
// 视图：SVN 本地改动
// ---------------------------------------------------------------------------
function initSvnView() {
  $("svnRefresh").addEventListener("click", () => maybeRefreshSvn(true));
  $("svnAuto").addEventListener("click", () => setSvnAuto(!SVN_AUTO.on));
  // 活动栏那个 ⎇ 按钮：鼠标移上去 / 键盘聚焦就预热一次。
  // 用户从"想点"到"点下去"通常有两三百毫秒，这段时间够后端把扫描跑起来；
  // 真点开时 `do_status` 命中的就是一份新鲜快照 —— 面板直接就出内容。
  // 不用 click：click 说明已经切过来了，onshow 那次刷新会等不到快照。
  const svnAct = document.querySelector('.act[data-view="svn"]');
  if (svnAct) {
    svnAct.addEventListener("mouseenter", () => { if (curView !== "svn") svnWarm(); });
    svnAct.addEventListener("focus", () => { if (curView !== "svn") svnWarm(); });
  }
  // 显示选项（原版的 showUnversioned / showIgnored / excludeDirs 三项）
  const optBtn = $("svnOpts");
  if (optBtn) optBtn.addEventListener("click", svnOptionsDialog);
  // 注意包一层：直接把函数交给 addEventListener，它会把 **click 事件**
  // 当成第一个参数传进来 —— 而第一个参数现在是"只提交哪个目录"。
  // 不包的话工具栏那个按钮会变成 svnCommitPanel(MouseEvent)。
  $("svnCommit").addEventListener("click", () => svnCommitPanel());

  // svn update：会改动工作副本里的文件，先问一句
  //
  // 注意这里**不传 path**：后端看到没指定范围，就会扫出工作区下所有独立仓库
  // 逐个更新（D:\workspace\demo 这种工作区下面挂着几十个各自的 .svn，
  // 以前只更新其中一个，其余都被无视）。右键菜单那条会传 path，只动那一个目录。
  $("svnUpdate").addEventListener("click", async () => {
    const ok = await confirmBox("svn update",
      "从仓库拉取更新到当前工作副本？\n\n"
      + "会把工作区下**所有** SVN 仓库逐个更新一遍。\n"
      + "如果有人改过同一处，会出现冲突（C 状态），需要手动解决。\n\n"
      + "命令在终端里执行：能看实时输出，也能随时点停止。", "更新");
    if (!ok) return;
    await svnPlanRun("update", {});
  });

  // cleanup / 扫描 / 清理 / 撤销全部 —— 四个低频维护动作收进一个「维护…」菜单。
  // 以前是 ✿ 扫 清 撤 四个独立按钮：窄窗口下挤成一排，而且「撤」（撤销全部，
  // 不可恢复）和「清」长得一样近，误点的代价太大。
  const maintainBtn = $("svnMaintain");
  if (maintainBtn) maintainBtn.addEventListener("click", svnMaintainMenu);

  // 打开 / 切回本视图时的那次刷新由文件末尾的 viewRegister 统一负责
  // （onshow → refreshSvnOnShow，**必定刷**，不看「自动」开关）。
  // 这里**不要再注册一次**：viewRegister 是覆盖赋值而不是合并，
  // 两处写同一个钩子只会让"到底谁在刷"变得说不清。

  // 折叠记忆里存的是**绝对路径**（上一个工作区的）。留着不清只会让新工作区里
  // 碰巧同名的目录莫名处于折叠状态。树缓存也得清 —— 否则局部刷新会拿旧根去匹配。
  WS_HANDLERS.svn = () => {
    SVN_OPEN.clear();
    SVN_UNV.cache = {};
    SVN_TREE = [];
    SVN_WC_AGE = -1;
    // 多选集里存的是**上一个工作区**的绝对路径，留着等于给「按 Delete」埋雷
    SVN_SEL.clear();
    SVN_NAV_PEND = null;
    // 工作副本清单是**按工作区**的：先当成未知（右键菜单先都显示），再异步重查
    SVN_WCS = null;
    SVN_WCS_STATE.asked = false;
    SVN_WCS_STATE.rescanned = false;
    svnLoadWcs();
    // 让正在跑的那次扫描作废（结果属于上一个工作区）
    SVN_EPOCH += 1;
    // 面板上还挂着上一个工作区的树 —— 只清缓存不重画的话，看起来就是
    // "工作区切了，SVN 一点反应都没有"。先清空，明确告诉用户在重扫。
    const list = $("svnList");
    if (list) {
      list.innerHTML = "";
      list.appendChild(emptyBox("切换工作区，正在重新扫描…"));
    }
    const sum = $("svnSummary");
    if (sum) sum.textContent = "扫描中…（大工程十几秒）";
    // 新工作区的快照还没人算过：先丢一次预热，让后端那台「后台扫描」跑起来。
    // 紧接着的 refreshSvn 如果快照还没好，仍然会同步算一次（首屏该等还是等），
    // 但用户来回切几次之后就有现成的了 —— 这是"打开即用"的主路径。
    SVN_SNAP = {};
    svnWarm();
    // 切工作区是"必须刷"：不看自动刷新开关，也不吃 SVN_MIN_GAP 的节流
    refreshSvn();
  };

  // quiet：只把开关状态摆好，**不要在这里扫**。紧接着 switchView 会调
  // onshow（refreshSvnOnShow）刷那一次；两边都扫的话首屏会白扫两遍
  // （大工作区一次约 2 秒，用户会以为卡了）。
  setSvnAuto(true, true);
  scheduleSvnIdle();
}

/**
 * 工具栏的「维护…」菜单：把 cleanup / 扫描 / 清理 / 撤销全部收成一个入口。
 *
 * 为什么不再并排放四个按钮：这几个都是低频操作，窄窗口下会挤掉别的按钮
 * （实测 9 个按钮时最后两个直接被挤出可视区）；而且「撤」= 撤销全部本地
 * 改动、不可恢复，跟旁边的「清」只有一字之差，误点代价太大 —— 收进菜单、
 * 单独隔一条分隔线并用危险色标出来。
 */
function svnMaintainMenu(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = "维护";
  ctx.appendChild(info);

  const item = (label, fn, danger) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (danger) b.style.color = "var(--danger)";
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  item("svn cleanup（清锁 / 断事务）", svnDoCleanup);
  item("扫描未纳入版本 / 已忽略（只看）", svnDoCleanScan);
  item("清理未纳入版本 / 已忽略…", svnDoCleanRemove);
  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("撤销全部本地改动…", svnDoRevertAll, true);

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/** svn cleanup：安全动作，不用确认。
 *
 * 走终端（同 update）：能看输出、能停。**不含** `unlock --force` 那一步
 * （解别人持有的锁），需要它时用 `/api/plugin/svn_tools/cleanup` ——
 * 那一步要先 `svn status` 扫一遍锁，是同步磁盘活，放进 plan 里等于又变回
 * "点了没反应"。
 */
async function svnDoCleanup() {
  await svnPlanRun("cleanup", {});
}

/**
 * 查看「未纳入版本 / 已忽略」—— 纯读，不删任何东西。
 * 结果铺成**带勾选**的列表，可以挑着删（原版是 QuickPick 多选 + 摊清单确认）。
 */
async function svnDoCleanScan() {
  $("svnSummary").textContent = "扫描中…";
  const res = await svnCall("clean_scan", {}, null, true);
  if (!res) return;
  if (!res.ok) { $("svnSummary").textContent = res.summary || "扫描失败"; return; }
  showSvnMultiResult(res);
  renderSvnCleanList(res);
}

/**
 * 清理未纳入版本 / 已忽略 —— **先扫**，把真要删的路径摊在确认框里，
 * 再把这份清单当 `paths` 传回去（后端会重新扫一遍取交集，双向兜底）。
 * 以前这里不传 paths，确认框也只是一句笼统的话 —— 等于闭着眼睛删全量。
 */
async function svnDoCleanRemove() {
  $("svnSummary").textContent = "扫描中…";
  const scan = await svnCall("clean_scan", {}, null, true);
  if (!scan || !scan.ok) {
    $("svnSummary").textContent = (scan && scan.summary) || "扫描失败";
    return;
  }
  const entries = (scan.data && scan.data.entries) || [];
  if (!entries.length) {
    $("svnSummary").textContent = "没有可清理的条目";
    toast("没有可清理的条目");
    return;
  }
  await svnDeletePaths(entries.map((e) => e.path));
}

/** 撤销全部仓库的本地改动 —— 不可恢复，确认文案要说到位。 */
async function svnDoRevertAll() {
  const ok = await confirmBox("撤销全部本地改动",
    "会撤销**所有仓库**里未提交的改动，把它们还原成仓库里的版本。\n\n"
    + "⚠ 这个操作**不可恢复**，没提交的代码会直接消失。\n"
    + "⚠ 新增的未纳入版本的文件不会被删，只是不再算改动。\n\n"
    + "确定要撤销全部？", "全部撤销");
  if (!ok) return;
  $("svnSummary").textContent = "撤销中…（仓库多时请稍候）";
  const res = await svnCall("revert_all", { all_workspaces: true }, null, true);
  if (!res) return;
  showSvnMultiResult(res);
  refreshSvn(null, false, true);
}

// ---------------------------------------------------------------------------
// SVN：嵌套树渲染 + 右键操作
// ---------------------------------------------------------------------------
// 后端返回的是「只含有改动的目录」的嵌套树（搬自 svn.js 的 buildTree），
// 不再是扁平一层分组。目录可折叠、按最要紧的状态着色。
const SVN_LETTER = {
  modified: "M", added: "A", deleted: "D", missing: "!", replaced: "R",
  conflicted: "C", merged: "G", obstructed: "~", incomplete: "!",
  external: "X", unversioned: "?", ignored: "I",
};
const SVN_COLOR = {
  modified: "#d29922", added: "#3fb950", deleted: "#f85149",
  missing: "#f85149", replaced: "#58a6ff", conflicted: "#f85149",
  merged: "#a371f7", obstructed: "#f85149", incomplete: "#f85149",
  external: "#8b949e", unversioned: "#3fb950", ignored: "#8b949e",
};
// 目录的展开状态：只记**用户显式点过**的那些，默认值由 autoExpand 决定
// （原版 autoExpand 默认 true，即首屏全展开）。存 Set 的话"默认展开"
// 和"用户展开过"分不开，切 autoExpand 关就不生效。
const SVN_OPEN = new Map();          // fsPath → true(展开) / false(折叠)

function svnIsOpen(path) {
  const v = SVN_OPEN.get(path);
  return v === undefined ? !!SVN_OPTS.autoExpand : !!v;
}

// ---------------------------------------------------------------------------
// 显示选项（原版是 package.json 里的 showUnversioned / showIgnored /
// excludeDirs 三项；web 这边没有设置页，就存在 localStorage 里，ⓘ 按钮打开）
// ---------------------------------------------------------------------------
const SVN_OPTS_KEY = "svn.opts.v1";
const SVN_OPTS = Object.assign(
  {
    hideUnversioned: false,   // 原版 showUnversioned=false 时隐藏 `?`
    showIgnored: false,       // 原版 showIgnored
    excludeDirs: "",          // 原版 excludeDirs（空 = 用后端默认名单）
    requireMessage: true,     // 原版 commitRequireMessage
    useTrash: true,           // 原版 cleanUpUseTrash
    // 首屏是否自动展开所有目录。默认**关**：AOSP 那种 `src/com/hzhbd/ui/aacp/
    // dialog/PairDialog.kt` 一条单链，全展开能把一屏塞满，还看不出哪层有改动。
    // 关掉之后只列出第一层，点 ▸ 逐层展开（展开状态记在 SVN_OPEN 里）。
    autoExpand: false,
    // 单子目录链合并成一行（`src/com/server/ui`）。跟文件树的 compact=1、
    // VS Code 的 explorer.compactFolders 一样 —— AOSP 那种一条链一层一个
    // 目录名，不合并的话头几屏全是空壳目录，真正的改动要滚很久才看见。
    compactDirs: true,
  },
  (() => {
    try { return JSON.parse(localStorage.getItem(SVN_OPTS_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  })());

function svnSaveOpts() {
  try { localStorage.setItem(SVN_OPTS_KEY, JSON.stringify(SVN_OPTS)); } catch (e) { /* 忽略 */ }
}

/** 只对 `status` 有意义的选项（后端 status 认这三个）。 */
function svnStatusOpts() {
  const o = {};
  if (SVN_OPTS.hideUnversioned) o.hide_unversioned = true;
  if (SVN_OPTS.showIgnored) o.include_ignored = true;
  const ex = String(SVN_OPTS.excludeDirs || "").split(/[,;\s]+/).filter(Boolean);
  if (ex.length) o.exclude_dirs = ex;
  return o;
}

/** 凡是会"扫工作副本"的动作都认 exclude_dirs。 */
function svnWsOpts() {
  const ex = String(SVN_OPTS.excludeDirs || "").split(/[,;\s]+/).filter(Boolean);
  return ex.length ? { exclude_dirs: ex } : {};
}

/** 懒展开的结果缓存：fsPath → 子节点数组（刷新时清空，免得看到已删的文件）。 */
const SVN_UNV = { cache: {}, busy: {} };
/** 当前树（本地重渲染用，避免折叠一次就重跑 2 秒的 status）。 */
let SVN_TREE = [];
/** 副本清单是多久前扫的（秒，后端 status 回传的 `wc_age`；-1 = 未知）。
 *
 * 清单「扫一次、之后读文件」，新 checkout 的副本不会自己进来 —— 被漏掉的
 * **嵌套副本**会以 `? xxx/` 的样子出现在树里，和真正的未纳管目录长得一样。
 * 后端已经会在 status 时补探一层，但缓存机制还在，所以这个年龄要摆到界面上。 */
let SVN_WC_AGE = -1;
/** 最近一次 status 结果的「快照信息」（后端 data.snapshot：at / age_ms / ms /
 *  stale）。状态栏用它显示"多少秒前刷的""正在后台更新"，见 svnSnapTail。 */
let SVN_SNAP = {};

/**
 * 左侧列表的多选集合：`svnNormPath(fsPath)` → **原始** fsPath。
 *
 * 为什么 key 用规范化路径、值存原样：比较要走 svnNormPath（Windows 盘符和
 * 分隔符大小写不定），但真正删文件必须用原始大小写 —— 服务器上的 Linux 工作区
 * 区分大小写，拿小写路径去删会「找不到文件」。
 *
 * 为什么不做成 DOM 上的 class：renderSvnTree 每次 `innerHTML = ""` 全量重画
 * （折叠、刷新、懒展开都会触发），选中态只在 DOM 上就必然丢。所以状态放这里，
 * 重画完由 svnMarkSel 按 fsPath 贴回去。
 */
const SVN_SEL = new Map();

/** 跨文件跳转的待落点：新文件画完要落在「首处」还是「末处」。 */
let SVN_NAV_PEND = null;      // { path, dir: 1|-1, hops }

function svnSelHas(p) { return SVN_SEL.has(svnNormPath(p)); }
function svnSelToggle(p) {
  const k = svnNormPath(p);
  if (SVN_SEL.has(k)) SVN_SEL.delete(k); else SVN_SEL.set(k, p);
}
function svnSelAdd(p) { SVN_SEL.set(svnNormPath(p), p); }
function svnSelPaths() { return [...SVN_SEL.values()]; }

/** 把选中态贴回（重画后行都是新的，class 得重新打一遍）。 */
function svnMarkSel() {
  const list = $("svnList");
  if (!list) return;
  for (const row of list.querySelectorAll(".vrow")) {
    row.classList.toggle("sel", !!row._svnPath && svnSelHas(row._svnPath));
  }
}

/**
 * 标出「对比面板现在显示的是哪个文件」（左侧列表那一道左边框）。
 *
 * 只改 class、不重画 —— 点击某行时重画会把刚点的那一行换成新元素，
 * 视觉上闪一下，而且滚动位置也跟着跳。
 */
function svnMarkCur(path) {
  const list = $("svnList");
  if (!list) return null;
  const target = svnNormPath(path);
  for (const r of list.querySelectorAll(".vrow.cur")) r.classList.remove("cur");
  const row = [...list.querySelectorAll(".vrow")]
    .find((r) => r._svnPath && svnNormPath(r._svnPath) === target);
  if (row) row.classList.add("cur");
  return row || null;
}

/**
 * 让左侧列表滚到某个文件那一行，并沿路把祖先目录展开。
 *
 * 用途：对比面板的「↓ 下一处」跳到**别的文件**时，左边那棵树得跟着走到那一行
 * —— 否则面板换了文件，左边的选中高亮还停在原来的位置，看不出"现在在看哪个"。
 *
 * 展开祖先的做法：遍历树把沿途目录的 fsPath 记下来（`SVN_OPEN` 只认链**顶端**
 * 的路径，见 svnIsOpen），逐个置 true 再重画一次。
 * 找不到（文件已经不在树里，比如刚被删）就什么都不做。
 */
function svnRevealPath(fsPath) {
  const target = svnNormPath(fsPath);
  let trail = null;
  (function walk(nodes, acc) {
    for (const n of (nodes || [])) {
      if (n.kind === "file") {
        if (svnNormPath(n.fsPath) === target) { trail = acc.slice(); return true; }
        continue;
      }
      if (n.kind === "info" || n.kind === "unvdir") continue;
      if (walk(n.children, acc.concat([n.fsPath]))) return true;
    }
    return false;
  })(SVN_TREE, []);
  if (!trail) return;
  for (const p of trail) SVN_OPEN.set(p, true);
  renderSvnTree();
  const row = svnMarkCur(fsPath);
  // nearest：只把列表滚到"刚好看见"，别把整页拽走
  if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}

function svnLetter(s) {
  return SVN_LETTER[s] || String(s || "?").slice(0, 1).toUpperCase();
}
function svnColor(s) {
  return SVN_COLOR[s] || "var(--muted)";
}

function svnTag(status) {
  const tag = document.createElement("span");
  tag.className = "vtag";
  tag.textContent = svnLetter(status);
  tag.style.color = svnColor(status);
  return tag;
}

function svnFileRow(n, depth) {
  const row = document.createElement("div");
  row.className = "vrow";
  // 记住自己的路径：多选态在重画后要按它贴回去（见 svnMarkSel）
  row._svnPath = n.fsPath;
  row.style.paddingLeft = (10 + depth * 14) + "px";
  row.title = n.fsPath + (n.propChange ? "\n（属性有变更）" : "");
  row.appendChild(svnTag(n.status));
  row.appendChild(document.createTextNode(n.name));
  if (n.propChange) {
    const p = document.createElement("span");
    p.className = "sub";
    p.textContent = "  属性";
    row.appendChild(p);
  }
  row.addEventListener("click", (ev) => {
    // Ctrl（Mac 上是 Cmd）点击 = 勾选 / 取消勾选，**不**打开对比。
    // 为什么需要：左侧列表一次只能看到一个文件的差异，想批量「删掉这几个
    // 临时产物」原来只能一个个右键。
    if (ev.ctrlKey || ev.metaKey) {
      ev.preventDefault();
      svnSelToggle(n.fsPath);
      row.classList.toggle("sel", svnSelHas(n.fsPath));
      return;
    }
    // 普通点击 = 单选（只选中它自己），再打开对比。
    // 清掉别的选中：否则「刚多选了一堆、又点了一个文件」会留下一个看不见的
    // 多选集，接着按 Delete 就删掉了一批用户以为早就取消了的文件。
    SVN_SEL.clear();
    svnSelAdd(n.fsPath);
    svnMarkSel();
    // 未纳入版本的文件没有 BASE 可对比 —— 直接打开（原版 openFile 那条）。
    // 以前只弹一句 toast，点了等于什么都没发生。
    if (n.status === "unversioned") {
      openInEditor(n.fsPath, n.name);
      return;
    }
    // 其余点一下 = 看「仓库版本 ↔ 本地」的并排对比（跟 VSCode 扩展里选中文件一样）。
    // 想改文件走右键 →「打开」。
    svnShowDiff(n.fsPath, n.name);
  });
  row.addEventListener("contextmenu", (ev) => svnItemMenu(ev, n, false));
  return row;
}

/**
 * 「一路只有一个子目录」的链合并成一行。
 *
 * `src ▸ com ▸ server ▸ ui` 这种只有唯一子目录的链，各占一行纯属占地方 ——
 * 跟文件树的 `compact=1`、VS Code 的 `explorer.compactFolders` 同一个做法。
 *
 * 合并的是**显示**，但动作一律落在**末端那个目录**（`leaf`）：
 * 行上写的是 `500_LinkCommon/aacplib/md`，右键删的就必须是 `.../md`。
 * 以前动作按链条顶端算，结果"想删 md 里的两个文件"点成了删除整个
 * `500_LinkCommon` —— 显示的名字和实际动手的路径不是一个东西，太危险。
 * 计数、状态色仍按链条顶端（它覆盖整条链）。
 * `unvdir`（懒展开的"未纳入版本"目录）不参与 —— 它的子节点还没读出来。
 *
 * `SVN_OPTS.compactDirs` 关掉就退回一层一行。
 */
function svnChain(n) {
  if (!SVN_OPTS.compactDirs || n.kind === "unvdir") return { leaf: n, name: n.name };
  const parts = [n.name];
  let cur = n;
  while (Array.isArray(cur.children) && cur.children.length === 1) {
    const kid = cur.children[0];
    if (!kid || kid.kind === "file" || kid.kind === "info" || kid.kind === "unvdir") break;
    parts.push(kid.name);
    cur = kid;
  }
  return { leaf: cur, name: parts.join("/") };
}

/**
 * 目录行。`chain` 是 `svnChain(n)` 的结果：
 * 行上显示 `chain.name`（整条链），但所有动作都作用在 `chain.leaf` 上 ——
 * 见 svnChain 注释里那次"删 md 结果删掉整个 500_LinkCommon"的事故。
 */
function svnDirRow(n, depth, chain) {
  const isUnv = n.kind === "unvdir";
  const name = chain.name;
  const leaf = chain.leaf;
  const head = document.createElement("div");
  head.className = "vgroup";
  head.style.paddingLeft = (10 + depth * 14) + "px";
  head.style.cursor = "pointer";
  head.title = leaf.fsPath + (isUnv
    ? "\n\n（未纳入版本）\n如果它其实是一个独立的 SVN 工作副本（目录里有 .svn），"
      + "点 ↻ 重新扫描工作副本 —— 清单过期时它会漏扫，从而被显示成这个样子。"
    : "");

  const open = svnIsOpen(n.fsPath);
  const mark = document.createElement("span");
  mark.style.cssText = "display:inline-block;width:10px;color:var(--muted)";
  mark.textContent = open ? "▾" : "▸";
  head.appendChild(mark);

  if (n.dominant) head.appendChild(svnTag(n.dominant));
  else if (isUnv) head.appendChild(svnTag("unversioned"));
  head.appendChild(document.createTextNode(
    name + (isUnv ? "/" : "  (" + (n.total || 0) + ")")));

  // 目录行右侧：单独提交 / 撤销这个目录。
  // 这两个动作原来只在右键菜单里，用户找不到（"我想只提交 vendor 这一块"），
  // 摆在行上、hover 才显形，不占视觉空间。
  if (!isUnv) {
    head.style.display = "flex";
    head.style.alignItems = "center";
    const acts = document.createElement("span");
    acts.style.cssText = "margin-left:auto;display:flex;gap:2px;opacity:0;"
      + "transition:opacity .12s;flex:0 0 auto";
    const mk = (txt, tip, fn) => {
      const b = document.createElement("button");
      b.textContent = txt;
      b.title = tip;
      b.style.cssText = "border:none;background:none;color:var(--muted);"
        + "cursor:pointer;padding:0 3px;font-size:12px;line-height:15px;"
        + "border-radius:4px";
      b.addEventListener("mouseenter", () => { b.style.color = "var(--text)"; });
      b.addEventListener("mouseleave", () => { b.style.color = "var(--muted)"; });
      // 不能让点击冒泡到 head —— 那会顺带把目录折叠起来
      b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
      return b;
    };
    acts.appendChild(mk("⇧", "只提交这个目录（" + name + "）",
                         () => svnCommitPanel(leaf.fsPath)));
    acts.appendChild(mk("↺", "撤销这个目录下的本地改动（" + name + "）",
                         () => svnRevert(leaf.fsPath)));
    head.appendChild(acts);
    head.addEventListener("mouseenter", () => { acts.style.opacity = "1"; });
    head.addEventListener("mouseleave", () => { acts.style.opacity = "0"; });
  }

  head.addEventListener("click", () => {
    // 展开状态按链条**顶端**记：链条长短变化（多了一层目录）不该把折叠状态冲掉
    const willOpen = !svnIsOpen(n.fsPath);
    SVN_OPEN.set(n.fsPath, willOpen);
    // 「未纳入版本」的目录：svn 只报了目录本身，内容要第一次展开时去读
    if (willOpen && isUnv && !SVN_UNV.cache[leaf.fsPath]) { svnExpandUnv(leaf); return; }
    renderSvnTree();
  });
  // 右键只认末级目录：菜单里每一条都是"对这条路径动手"，用链头会把整串都带上
  head.addEventListener("contextmenu", (ev) => svnItemMenu(ev, leaf, true, name));
  return head;
}

/** 按本地缓存的树重画一遍（折叠 / 懒展开都不该再跑一次 status）。 */
function renderSvnTree() {
  const list = $("svnList");
  if (!list) return;
  list.innerHTML = "";
  svnStaleTip(list);
  if (!SVN_TREE.length) {
    list.appendChild(emptyBox("工作副本是干净的"));
    return;
  }
  svnNodes(SVN_TREE, list, 0);
  // 行是刚建出来的，选中 class 得按 fsPath 重新打一遍（见 SVN_SEL 的注释）
  svnMarkSel();
}

/**
 * 副本清单陈旧提示。
 *
 * 清单是「扫一次、之后读文件」，新 checkout 的副本不会自己进清单；被漏掉的
 * **嵌套副本**会在树里显示成 `? xxx/`（跟真正的未纳管目录一模一样）。后端
 * status 已经会补探一层（`_svn.nested_wc_roots`），但缓存机制还在，所以把
 * 「清单多久前扫的」摆到界面上 —— 以前这句话只写给模型看，用户完全看不到，
 * 遇到漏扫只能归因成"插件认错了"。
 *
 * 1 小时以内不提：天天刷新都顶一行提示，比问题本身更烦。
 */
function svnStaleTip(list) {
  const age = SVN_WC_AGE;
  if (!(age >= 3600)) return;
  const tip = document.createElement("div");
  tip.className = "vgroup";
  tip.style.cssText = "padding:4px 10px;color:var(--muted);font-size:11px;"
    + "cursor:default;white-space:pre-wrap";
  tip.textContent = "工作副本清单是 " + Math.floor(age / 3600) + " 小时前扫的，"
    + "新 checkout 的副本可能被显示成未纳入版本的目录（`? xxx/`）\n"
    + "→ 点 ↻ 重新扫描工作副本";
  list.appendChild(tip);
}

/** 懒展开一个「未纳入版本」的目录（只读一层，子目录继续懒展开）。 */
async function svnExpandUnv(node) {
  const key = node.fsPath;
  if (SVN_UNV.busy[key]) return;
  SVN_UNV.busy[key] = true;
  try {
    const r = await api("/api/plugin/svn_tools/expand",
                        Object.assign({ path: key }, svnWsOpts()));
    const res = r.result || {};
    if (!res.ok) {
      toast(res.summary || res.content || "展开失败", true);
      SVN_UNV.cache[key] = [];
      return;
    }
    const d = res.data || {};
    const kids = (d.children || []).map((c) => ({
      kind: c.isDir ? "unvdir" : "file",
      name: c.name, fsPath: c.path, status: "unversioned",
      lazy: !!c.isDir, children: [], total: 0, counts: {},
    }));
    // 被上限挡掉的条数必须显式说出来，否则看起来就像个空目录
    if (d.hidden) kids.push({ kind: "info",
                              name: "（还有 " + d.hidden + " 项未显示）" });
    SVN_UNV.cache[key] = kids;
  } catch (e) {
    toast("展开失败：" + e.message, true);
    SVN_UNV.cache[key] = [];
  } finally {
    SVN_UNV.busy[key] = false;
  }
  renderSvnTree();
}

function svnNodes(nodes, box, depth) {
  for (const n of (nodes || [])) {
    if (n.kind === "file") {
      box.appendChild(svnFileRow(n, depth));
      continue;
    }
    if (n.kind === "info") {                 // 「还有 N 项未显示」这类提示行
      const tip = document.createElement("div");
      tip.className = "vgroup";
      tip.style.cssText = "padding-left:" + (10 + depth * 14)
        + "px;color:var(--muted);font-size:11px;cursor:default";
      tip.textContent = n.name;
      box.appendChild(tip);
      continue;
    }
    // 单子目录链合并成一行（`src/com/server/ui`），展开状态仍按**顶端**这个
    // 目录的 fsPath 记 —— 合并后链条变短变长都不影响已折叠的那些。
    const chain = svnChain(n);
    box.appendChild(svnDirRow(n, depth, chain));
    if (!svnIsOpen(n.fsPath)) continue;
    const leaf = chain.leaf;
    if (leaf.kind === "unvdir") {
      if (SVN_UNV.cache[leaf.fsPath]) leaf.children = SVN_UNV.cache[leaf.fsPath];
      else if (!SVN_UNV.busy[leaf.fsPath]) svnExpandUnv(leaf);
    }
    svnNodes(leaf.children, box, depth + 1);
  }
}

//: 这些动作会扫工作副本 → 都认 `exclude_dirs`（用户填了才带）。
//
// `plan_update` / `plan_cleanup` 也算：它们在**后端**扫一遍副本清单来拼命令，
// 不带的话用户配了「跳过 build」，build 里的副本照样被排进去。
const SVN_WS_ACTIONS = new Set(["update", "cleanup", "clean_scan",
                                "clean_remove", "revert_all",
                                "plan_update", "plan_cleanup"]);

/* ---------------------------------------------------------------------------
 * update / cleanup 走终端（→ 任务台）
 *
 * 为什么不再走 HTTP 同步接口：后端那条路是 `subprocess.run(capture_output)`
 * 阻塞到底，几十个副本要几分钟。这期间界面只有一行「更新中…」——
 * 看不见输出、不能停、报错只在最后弹一个 toast。而同一件事在 gradle 那边
 * 早解决了：命令塞进终端 → `/api/exec` → 后端登记进 `_TERM_JOBS` →
 * 任务台能看实时输出、能点停止、跑完还落一份任务历史。
 *
 * 所以拆成两步：
 *   ① `plan_update` / `plan_cleanup`：后端**只拼命令**，一条 svn 都不启动
 *   ② 前端把命令逐条交给 `runInTerminal`，串行执行
 *
 * 为什么不干脆把插件改成 Popen 流式写账本：那要插件反向依赖内核
 * （`_TERM_JOBS` 在 web_agent 里），或者把账本下沉到 `_shared` ——
 * 都违反「插件不改内核」，还要动任务台协议。走终端零风险。
 * ------------------------------------------------------------------------- */

/**
 * 串行把几条命令跑在同一个终端标签里。
 *
 * **必须串行**：`runInTerminal` 对同一标签会先 abort 上一条 fetch，
 * 后端探到客户端断开就 killpg 整棵进程树 —— 并发发下去等于自己杀自己。
 *
 * 返回最后一条的退出码（同 runInTerminal，null = 异常 / 被停）。
 */
async function svnRunSteps(steps, cwd, name) {
  if (typeof runInTerminal !== "function") {
    toast("当前版本不支持终端（刷新页面试试）", true);
    return null;
  }
  // 当前标签还在跑别的（比如 gradle 构建）→ 另开一个，
  // 否则那一条会被这次的首条命令 abort 掉。规则跟 gradle 插件一致。
  let tab = (typeof termActiveTab === "function") ? termActiveTab() : null;
  if (tab && tab.busy && typeof termNewTab === "function") {
    const t = termNewTab(cwd || S.workspace || "");
    if (t && name) {
      // 标签条上不能全是「终端 3 / 终端 4」—— 一眼看出哪条是 svn
      t.name = name;
      if (typeof renderTermTabs === "function") renderTermTabs();
    }
    tab = t || tab;
  }
  termCwd = cwd || S.workspace || "";
  let code = null;
  for (let i = 0; i < steps.length; i++) {
    code = await runInTerminal(steps[i].cmd, cwd);
    termCwd = cwd || S.workspace || "";
    // **不因为失败就中断**：update 本来就是「每个副本都跑一遍，
    // 让用户看到每一个的结果」，中途 break 会把剩下几十个直接跳过。
  }
  return code;
}

/**
 * plan + 执行 + 刷新的完整流程。
 *
 * `body` 原样给 plan（`{path}` = 只动那一处；空 = 整棵工作区）。
 */
async function svnPlanRun(action, body) {
  const name = action === "update" ? "svn update" : "svn cleanup";
  // quiet=true 是为了不和插件内部已经弹过的错重复弹，但它有个坑：
  // 请求本身失败（400 / 超时 / 断网）时 svnCall 只返回 null，什么都不说。
  // 以前这里就是 `if (!plan) return null;` 静默退出 —— 后端 400 时界面
  // 一点反应都没有，用户只能看到「点了更新，控制台/终端里什么都没有」。
  // 拿不到计划必须说出来，否则按钮等于失效。
  const plan = await svnCall("plan_" + action, body || {}, null, true);
  if (!plan) {
    const sum0 = $("svnSummary");
    const msg = name + " 计划失败：拿不到命令清单（后端返回异常，详见控制台 / .ai 日志）";
    if (sum0) sum0.textContent = msg;
    toast(msg, true);
    if (typeof rtLog === "function") rtLog("svn.plan_fail", action);
    return null;
  }
  const sum = $("svnSummary");
  if (!plan.ok) {
    if (sum) sum.textContent = plan.summary || "无法执行";
    toast(plan.summary || "无法执行", true);
    return plan;
  }
  const steps = plan.steps || [];
  if (!steps.length) return plan;
  // name 在函数开头就算好了（失败提示也要用），这里不再重复声明。
  if (sum) {
    sum.textContent = (plan.summary || name)
      + "｜输出见终端 / 任务台，可随时停止";
  }
  const multi = $("svnSummary");
  if (multi) multi.title = "";
  await svnRunSteps(steps, plan.cwd, name);
  // 跑完必须刷：后端那份内存快照是 update 之前的（成功的副本已经真的变了）。
  // fresh=true 强制同步重算，不吃 TTL。
  refreshSvn(null, false, true);
  return plan;
}

/**
 * 调一个 svn 操作。
 *
 * `quiet=true` 时不自动弹错误 —— 给「提交」这种要自己处理失败（过期重试、
 * 冲突提示、原文展示）的调用方用，免得同一个错误被 toast 两遍。
 *
 * `opts` 原样透传给 api()，目前只有 `{ timeout }` 一个用途（提交要放宽）。
 */
async function svnCall(action, body, okMsg, quiet, opts) {
  let payload = body || {};
  if (SVN_WS_ACTIONS.has(action)) payload = Object.assign(svnWsOpts(), payload);
  else if (action === "status") payload = Object.assign(svnStatusOpts(), payload);
  try {
    const r = await api("/api/plugin/svn_tools/" + action, payload, opts || undefined);
    const res = r.result || {};
    if (!res.ok) {
      if (!quiet) toast(res.summary || res.content || "操作失败", true);
      return res;                      // quiet 时也要把结果给调用方看
    }
    if (okMsg) toast(okMsg);
    return res;
  } catch (e) {
    // 超时要单独交代：以前 quiet 调用方拿到的是 null，跟"请求压根没发出去"
    // 混成同一种，界面只能写"提交失败：请求超时（660s）"。而真实情况可能是
    // **后端还在传**，这时再点一次就是重复提交。所以带 kind 回去，让调用方
    // 能按"未知结果"处理。
    if (e && e.timeout) {
      const res = { ok: false, summary: "请求超时",
                    content: String(e.message || ""),
                    data: { kind: "timeout",
                            hint: "前端等不到响应就放弃了，但后端那条 svn 命令可能还在跑。"
                                + "先别急着点第二次：刷新 SVN 面板看一眼，"
                                + "或在命令行 `svn log -l 1 <路径>` / `svn status` "
                                + "确认这次到底提交上没有。" } };
      if (!quiet) toast(res.summary, true);
      return res;
    }
    if (!quiet) toast("失败：" + e.message, true);
    return null;
  }
}

async function svnRevert(path) {
  // revert 是**不可恢复**的（本地未提交的修改直接没了）—— 原版是模态确认，这里也必须问
  const ok = await confirmBox("撤销本地改动",
    "把它还原成仓库里的版本？\n\n" + path
    + "\n\n⚠ 本地未提交的修改会直接丢失，找不回来。", "撤销改动");
  if (!ok) return;
  const res = await svnCall("revert", { targets: [path] });
  if (res && res.ok) { toast("已撤销改动"); refreshSvn(null, false, true); }
}

/**
 * 删掉一个**未纳入版本控制**（`?` 状态）的文件或目录。
 *
 * 为什么需要它：`?` 的路径在仓库里没有任何版本，`svn revert` 对它们只会打印
 * `Skipped`（退出码还是 0 —— 后端以前就因此报了「已撤销本地改动」），所以
 * 问号节点点「撤销」永远没反应。SVN 语义下「撤销」对它就等于「删掉」，
 * 这一格菜单才是有意义的动作。
 *
 * 走内核的 deleteEntry（`/api/fs/delete`，:40-tree.js 790）：和文件树右键「删除」
 * 同一个入口、同一套路径守卫，进系统回收站能捡回来；它内部已经调了
 * notifySvnFsChanged(path)，SVN 面板会自己重扫，这里不再额外 refreshSvn。
 *
 * 不复用 svnDeletePaths：那条走 clean_remove，后端会重新扫一遍候选清单再和
 * 传入路径取交集，对「就要删这一条」反而多一层不确定性。
 */
async function svnDropUnversioned(path, node, isDir) {
  const shownPath = (typeof relPath === "function")
    ? relPath(path, S.workspace) : path;
  const ok = await confirmBox(
    isDir ? "删除整个未纳入版本的目录" : "删除未纳入版本的文件",
    "它是 `?`（未纳入版本控制）状态，仓库里没有对应版本可以还原 ——\n"
    + "「撤销」对它只能是删掉它。\n\n" + shownPath
    + (isDir ? "\n\n⚠ 会连同目录里的所有内容一起删掉。" : "")
    + "\n\n· 默认进系统回收站，还能还原"
    + "\n· 只是想留着它：改用上面那条「纳入版本控制」", "删除");
  if (!ok) return;
  if (typeof deleteEntry !== "function") {
    toast("当前版本不支持（刷新页面试试）", true);
    return;
  }
  deleteEntry(path, node.name, isDir);
}

/**
 * 文件夹右键：只 update 这一个目录。
 *
 * 跟 SVN 面板工具栏那个 ↓ 是同一个动作，区别只是范围 —— 大工程里
 * 整仓 update 要等很久，而且会把一堆跟你无关的改动一起拉下来。
 *
 * `dir` 为空 = **整棵工作区**：那说明右键的目录自己不是工作副本（典型是工作区根
 * D:\workspace\demo，下面几十个各自带 .svn 的模块）。这时候**不能把 path 传下去** ——
 * `svn update <一个不是副本的目录>` 只会回 E155007；不传 path 后端才走多副本分支，
 * 逐个更新。menu 里那种情况的文案写明了范围（见 svnDirSubmenu）。
 *
 * 还是要问一句：update 会**覆盖工作副本里的文件**，有本地改动的地方
 * 可能直接变成冲突（C 状态）。
 */
async function svnFolderUpdate(dir) {
  const all = !dir;
  const ok = await confirmBox("svn update",
    all
      ? "从仓库更新工作区下的**所有** SVN 仓库？\n\n"
        + "这个目录本身不是工作副本，里面挂着多个独立副本 —— 会逐个更新一遍，"
        + "大工程要等一会儿。\n"
        + "如果有人改过同一处，会出现冲突（C 状态），需要手动解决。"
      : "只更新这个目录：\n" + dir + "\n\n"
        + "如果有人改过同一处，会出现冲突（C 状态），需要手动解决。", "更新");
  if (!ok) return;
  await svnPlanRun("update", all ? {} : { path: dir });
}

/**
 * 文件夹右键：svn cleanup（清锁 / 断掉残留事务）。工作副本卡住时的第一招。
 *
 * 不用确认 —— 它是修复动作，不会改动文件内容（后端会先 unlock --force 再 cleanup）。
 *
 * `dir` 为空 = 整棵工作区（后端多副本分支），理由同 svnFolderUpdate。
 */
async function svnFolderCleanup(dir) {
  await svnPlanRun("cleanup", dir ? { path: dir } : {});
}

async function svnIgnore(path, detach) {
  // 后端要两个东西：target（完整路径）+ name（光名字，不含路径分隔符）——
  // svn:ignore 是挂在**父目录**上的属性，值是名字。
  const clean = String(path).replace(/[\\/]+$/, "");
  const name = clean.split(/[\\/]/).pop();
  if (!name) { toast("算不出要忽略的名字", true); return; }

  // detach=true：已经在版本控制里的条目，光写 ignore 没用（它还是版本化的，
  // 照样出现在 status 里），要先 svn delete --keep-local 把它摘出来。
  const res = await svnCall("ignore", { target: path, name, detach: !!detach });
  if (res && res.ok) { toast(res.summary || "已加进 svn:ignore"); refreshSvn(null, false, true); }
}

function svnItemMenu(ev, node, isDir, shown) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";

  const info = document.createElement("div");
  info.className = "title";
  // 合并链：节点是末级目录，但要让用户看到"我点的是哪一整条路径"
  info.textContent = shown || node.name;
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  const path = node.fsPath;
  const isUnv = node.status === "unversioned";

  if (!isDir) item("打开", () => openInEditor(path, node.name));
  // 对比仓库里的版本（新增/未纳入版本的文件没有 BASE，会提示"没有差异"）
  if (!isUnv) item("对比仓库版本…", () => svnShowDiff(path, node.name));
  if (isUnv) {
    item("纳入版本控制", async () => {
      // 传的是**右键的那个节点**，由后端加 `--parents` 补祖先：
      // 整棵未纳管的子树（如 audio-ai/src）也能一次进去，且兄弟目录不进。
      await svnCall("add", { targets: [path] }, "已纳入版本控制");
      refreshSvn(null, false, true);
    });
  }
  // 提交：**未纳入版本的文件也给**（isUnv 不拦）。后端 do_commit 第 1 步就是
  // plan_commit → 批量 svn add / svn rm 预处理，所以新文件直接提交不会漏。
  // 少了这条，右键里只能先「纳入版本控制」再跑去 SVN 面板点提交，两步。
  item("提交…", () => svnCommitPanel(path));
  if (!isUnv) item("撤销本地改动", () => svnRevert(path));
  // 问号（未纳入版本）节点**没有版本可还原**，`svn revert` 对它只会 Skipped ——
  // 所以「撤销」在 SVN 语义下的等价动作就是删掉它。位置占着原来「撤销本地改动」
  // 那一格（肌肉记忆），紧挨上面的「纳入版本控制」：这是问号节点的唯二出路。
  else item(isDir ? "删除整个未纳入版本的目录…" : "删除未纳入版本的文件…",
            () => svnDropUnversioned(path, node, isDir));
  // 原版的 `delete`：把文件本身丢进回收站（不经过 svn）。已纳入版本的会变成 `!`。
  // 跟「移出版本控制」是两件事：那个是 svn delete，仓库下次提交会删掉它。
  //
  // 目录必须**先确认并摊出完整路径**：文件树的 deleteEntry 特意不弹框（进回收站
  // 能还原），但这里点错一次就是整棵子树没了 —— 曾经"想删 md 里两个文件"点成了
  // 删除整个 500_LinkCommon，就是显示名和实际路径不是一回事造成的。
  //
  // 右键的那一行**在多选集里**时，这条变成"删整批"：Ctrl 选中几个之后右键 →
  // 删除，是最顺手的路径（不然还得回去按 Delete）。不在选中集里就还是老行为，
  // 只删点中的这一条。
  const multi = !isDir && SVN_SEL.size > 1 && svnSelHas(path);
  item(multi ? "删除选中的 " + SVN_SEL.size + " 项到回收站" : "删除到回收站",
    async () => {
      if (multi) { svnDeleteSelected(); return; }
      if (isDir) {
        const shownPath = (typeof relPath === "function")
          ? relPath(path, S.workspace) : path;
        const ok = await confirmBox("删除到回收站",
          "将把整个目录（含里面全部内容）移进系统回收站：\n\n" + shownPath
          + "\n\n只想去掉其中几个文件的话，请展开后逐个删。\n"
          + "（回收站里还能还原）", "删除");
        if (!ok) return;
      }
      if (typeof deleteEntry === "function") deleteEntry(path, node.name, isDir);
      else toast("当前版本不支持（刷新页面试试）", true);
    });
  item("忽略…", () => svnIgnore(path));
  // 已经在版本控制里的条目，「忽略」不足以让它消失 —— 得先 svn delete --keep-local
  // 再写 svn:ignore，否则下次 status 还会把它列出来
  if (!isUnv) item("移除并忽略…", () => svnIgnore(path, true));
  if (!isUnv) {
    item("移出版本控制…", async () => {
      const ok = await confirmBox("移出版本控制",
        "把 " + node.name + " 从版本控制里移除？\n\n本地文件会保留（--keep-local），"
        + "下次提交时仓库里会删掉它。");
      if (!ok) return;
      await svnCall("delete", { targets: [path], keep_local: true }, "已移出版本控制");
      refreshSvn(null, false, true);
    });
  }

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  // 原版的「刷新这个工作副本」：只重扫它所属的那棵子树，不必等 55 个副本全扫完
  item("刷新这个副本", () => maybeRefreshSvn(false, path));
  item("在资源管理器中显示", () => {
    if (typeof revealInExplorer === "function") revealInExplorer(path);
    else toast("当前版本不支持打开资源管理器", true);
  });
  item("复制路径", () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(path).then(() => toast("已复制"))
        .catch(() => toast("复制失败", true));
    }
  });

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/**
 * 显示选项：原版是 package.json 里的 showUnversioned / showIgnored / excludeDirs。
 *
 * web 这边没有设置页，存 localStorage；改完立刻重扫（不然改了看不出效果）。
 */
function svnOptionsDialog() {
  const body = document.createElement("div");
  body.style.cssText = "font-size:13px;line-height:1.9";

  const mk = (checked, label, hint) => {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:flex-start;gap:8px;cursor:pointer";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!checked;
    cb.style.marginTop = "5px";
    const txt = document.createElement("span");
    txt.textContent = label;
    if (hint) {
      const sub = document.createElement("span");
      sub.style.cssText = "color:var(--muted);font-size:11px";
      sub.textContent = "　" + hint;
      txt.appendChild(sub);
    }
    row.appendChild(cb);
    row.appendChild(txt);
    return { row, cb };
  };

  const unv = mk(!SVN_OPTS.hideUnversioned, "显示「未纳入版本」的文件（?）",
                 "关掉后只看已纳入版本控制的改动");
  const ign = mk(SVN_OPTS.showIgnored, "显示「已忽略」的条目（I）");
  body.appendChild(unv.row);
  body.appendChild(ign.row);

  const rmsg = mk(SVN_OPTS.requireMessage, "提交说明必填",
                  "关掉后可以空说明提交（svn 侧会用 --force-log）");
  const trash = mk(SVN_OPTS.useTrash, "清理时删到回收站",
                   "关掉就是永久删除，捡不回来");
  const expand = mk(SVN_OPTS.autoExpand, "首屏自动展开目录",
                    "关掉后默认全部折叠，自己点开");
  const compact = mk(SVN_OPTS.compactDirs, "单子目录合并成一行",
                     "像 src/com/server/ui 这种一条链只占一行");
  body.appendChild(rmsg.row);
  body.appendChild(trash.row);
  body.appendChild(expand.row);
  body.appendChild(compact.row);

  const lab = document.createElement("div");
  lab.style.cssText = "margin-top:10px;font-size:12px";
  lab.textContent = "探测工作副本时额外跳过的目录名（逗号分隔，留空用默认）";
  const ex = document.createElement("input");
  ex.type = "text";
  ex.value = SVN_OPTS.excludeDirs || "";
  ex.placeholder = "默认：.svn .git node_modules build .gradle "
    + "__pycache__ .idea out dist temp tmp";
  ex.style.cssText = "width:100%;box-sizing:border-box;margin-top:4px";
  body.appendChild(lab);
  body.appendChild(ex);

  const foot = document.createElement("div");
  const sp = document.createElement("span");
  sp.className = "spacer";
  const cancel = document.createElement("button");
  cancel.className = "tbtn";
  cancel.textContent = "取消";
  cancel.addEventListener("click", closeModal);
  const save = document.createElement("button");
  save.className = "tbtn";
  save.textContent = "保存并刷新";
  save.addEventListener("click", () => {
    SVN_OPTS.hideUnversioned = !unv.cb.checked;
    SVN_OPTS.showIgnored = ign.cb.checked;
    SVN_OPTS.requireMessage = rmsg.cb.checked;
    SVN_OPTS.useTrash = trash.cb.checked;
    SVN_OPTS.autoExpand = expand.cb.checked;
    SVN_OPTS.compactDirs = compact.cb.checked;
    SVN_OPTS.excludeDirs = ex.value.trim();
    SVN_OPEN.clear();          // 展开状态回到新默认值
    svnSaveOpts();
    closeModal();
    refreshSvn();
  });
  // 副本清单是持久化的（第一次扫完存 <工作区>/.ai/temp/svn_tools/），
  // 新建 / 删除过仓库之后这里手动重扫一次 —— 平时刷新只跑 status。
  const rescan = document.createElement("button");
  rescan.className = "tbtn mini";
  rescan.textContent = "重新扫描工作副本";
  rescan.title = "重新遍历目录找 .svn（新 checkout / 删掉仓库之后点）\n"
    + "平时刷新不会重扫目录，只查文件改动";
  rescan.addEventListener("click", async () => {
    SVN_OPTS.excludeDirs = ex.value.trim();
    svnSaveOpts();
    closeModal();
    await refreshSvn(null, true);
    toast("已重新扫描工作副本");
  });
  foot.appendChild(rescan);
  foot.appendChild(sp);
  foot.appendChild(cancel);
  foot.appendChild(save);
  openModal("SVN 显示选项", body, foot);
}

// ---------------------------------------------------------------------------
// 清理：扫描结果（可勾选）+ 删除（摊清单确认）
// ---------------------------------------------------------------------------

/** 把 clean_scan 的结果铺成带勾选框的列表 —— 可以挑着删。 */
function renderSvnCleanList(res) {
  const list = $("svnList");
  if (!list) return;
  const entries = ((res.data || {}).entries) || [];
  list.innerHTML = "";

  const head = document.createElement("div");
  head.className = "view-bar";
  head.style.cssText = "padding:6px 8px;font-size:11px;color:var(--muted)";
  head.textContent = (res.summary || "")
    + "　（这些不是本地改动，是未纳入版本 / 已忽略的文件）";
  list.appendChild(head);

  if (!entries.length) {
    list.appendChild(emptyBox("没有可清理的条目"));
    return;
  }

  const boxes = [];
  for (const e of entries) {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:center;gap:7px;padding:3px 10px;"
      + "font-size:12px;white-space:nowrap;overflow:hidden";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.value = e.path;
    const tag = document.createElement("span");
    tag.className = "vtag";
    tag.textContent = e.kind === "ignored" ? "I" : "?";
    tag.style.color = e.kind === "ignored" ? "var(--muted)" : "#3fb950";
    const txt = document.createElement("span");
    txt.style.cssText = "overflow:hidden;text-overflow:ellipsis";
    txt.textContent = relPath(e.path, S.workspace) + (e.isDir ? "/" : "");
    txt.title = e.path;
    row.appendChild(cb);
    row.appendChild(tag);
    row.appendChild(txt);
    list.appendChild(row);
    boxes.push(cb);
  }

  if ((res.data || {}).truncated) {
    const more = document.createElement("div");
    more.style.cssText = "padding:6px 10px;font-size:11px;color:var(--muted)";
    more.textContent = "条目过多，只显示了前 " + entries.length + " 项";
    list.appendChild(more);
  }

  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;gap:8px;padding:8px 10px;flex-wrap:wrap";
  const all = document.createElement("button");
  all.className = "tbtn mini";
  all.textContent = "全选";
  all.addEventListener("click", () => boxes.forEach((b) => { b.checked = true; }));
  const none = document.createElement("button");
  none.className = "tbtn mini";
  none.textContent = "全不选";
  none.addEventListener("click", () => boxes.forEach((b) => { b.checked = false; }));
  const del = document.createElement("button");
  del.className = "tbtn mini";
  del.textContent = "删除选中项（进回收站）";
  del.addEventListener("click", () => {
    const paths = boxes.filter((b) => b.checked).map((b) => b.value);
    if (!paths.length) { toast("什么都没勾", true); return; }
    svnDeletePaths(paths);
  });
  bar.appendChild(all);
  bar.appendChild(none);
  bar.appendChild(del);
  list.appendChild(bar);
}

/**
 * 删一批「未纳入版本 / 已忽略」的路径。
 *
 * 确认框把前 8 条摊出来（原版 `cleanConfirmText` 的做法）—— "删除 112 项"
 * 这种只报个数的确认等于没确认。传入的 `paths` 后端会重新扫一遍取交集，
 * 参数被改也删不到清单外的东西。
 */
async function svnDeletePaths(paths) {
  const trash = !!SVN_OPTS.useTrash;
  const preview = paths.slice(0, 8)
    .map((p) => "  " + relPath(p, S.workspace)).join("\n");
  const ok = await confirmBox(
    trash ? "删除这些文件（进系统回收站）" : "永久删除这些文件",
    "将删除 " + paths.length + " 项：\n\n" + preview
    + (paths.length > 8 ? "\n  …… 还有 " + (paths.length - 8) + " 项" : "")
    + "\n\n" + (trash ? "· 默认进回收站，还能捡回来" : "· ⚠ 永久删除，找不回来")
    + "\n· 带 .svn 的嵌套副本会被跳过（删了会丢整份未提交代码）", "删除");
  if (!ok) return;
  $("svnSummary").textContent = "清理中…";
  const res = await svnCall("clean_remove",
                            { recycle: trash, paths }, null, true);
  if (!res) return;
  showSvnMultiResult(res);
  refreshSvn(null, false, true);
}

/**
 * 删掉左侧列表里 **Ctrl 选中**的那些文件 / 目录（Delete 键和右键菜单都走这里）。
 *
 * 为什么不复用上面的 svnDeletePaths（clean_remove 那条通道）：
 * 后端的 `clean_remove` 会**重新扫一遍**未纳入版本 / 已忽略的清单，再和传进去的
 * 路径取交集。已纳入版本控制的文件根本不在那份候选里，所以拿它删「改动列表」
 * 里选中的东西只会得到「选中的条目已不存在」。这是在实现前就查实的，不是猜的。
 *
 * 所以走内核的 `/api/fs/delete`：和文件树右键「删除」是同一个入口、同一套
 * 路径守卫，进系统回收站。已纳入版本的文件被删掉后在工作副本里变成 `!`
 * （missing），下次提交时仓库里那份也会被删 —— 这一点必须在确认框里说清楚。
 *
 * 逐个删（不并发）：一次几百个并发请求会把后端打满，而这里的量级通常是几个。
 * 失败项收集起来一起报，不要第一个失败就把后面的全丢掉。
 */
async function svnDeleteSelected() {
  const paths = svnSelPaths();
  if (!paths.length) return;
  const preview = paths.slice(0, 8)
    .map((p) => "  " + (typeof relPath === "function"
      ? relPath(p, S.workspace) : p)).join("\n");
  const ok = await confirmBox("删除选中的 " + paths.length + " 项",
    "将把下面这些移进系统**回收站**：\n\n" + preview
    + (paths.length > 8 ? "\n  …… 还有 " + (paths.length - 8) + " 项" : "")
    + "\n\n· 回收站里还能还原"
    + "\n· 已纳入版本控制的条目删掉后，工作副本里会变成 !（missing），"
    + "下次提交时仓库里那份也会被删", "删除");
  if (!ok) return;

  $("svnSummary").textContent = "删除中…";
  const fails = [];
  let done = 0;
  for (const p of paths) {
    try {
      await api("/api/fs/delete", { path: p });
      done += 1;
      // 开着这个文件的编辑器标签跟着关掉（和文件树 deleteEntry 一致）
      if (typeof EDITORS !== "undefined" && Array.isArray(EDITORS)
          && typeof closeEditor === "function") {
        const hit = EDITORS.findIndex((x) => x.path === p);
        if (hit >= 0) closeEditor(hit);
      }
    } catch (e) {
      fails.push(p + "：" + e.message);
    }
  }

  SVN_SEL.clear();
  const sum = $("svnSummary");
  sum.textContent = "已删除 " + done + " 项"
    + (fails.length ? "（" + fails.length + " 项失败）" : "");
  sum.title = fails.join("\n");
  if (fails.length) {
    toast("有 " + fails.length + " 项删不掉，见状态栏（悬停看原因）", true);
  } else {
    toast("已把 " + done + " 项移到回收站");
  }
  // 删掉的文件在工作副本里变成 missing，得重扫才能看到新状态
  refreshSvn(null, false, true);
}

/**
 * Delete 键删选中的（只认 SVN 左侧列表）。
 *
 * 为什么另起一个监听、而不是并进文件树那个（40-tree.js:797 附近）：
 * 那个监听的条件是「文件树的 DOM 正在显示」，而 SVN 视图打开时文件树是隐藏的 ——
 * 两边本来就互斥，硬塞进同一个 if 里只会让两边都更难读。这里条件同样写得很紧：
 *   · 键盘得在"没人管"的状态（activeElement 是 body）—— 输入框 / 编辑器里按
 *     Delete 是删字符，绝不能拿去删文件。
 *   · `#svnList` 得正在显示（切到别的视图后那棵树还留在 DOM 里带着选中态）。
 *   · 没开弹窗。
 *   · 选中集非空。
 * 只认 Delete，不认 Backspace：后者在不少场景是"返回上一页"。
 */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Delete" || e.ctrlKey || e.altKey || e.metaKey) return;
  const act = document.activeElement;
  if (act && act !== document.body && act !== document.documentElement) return;
  const list = $("svnList");
  if (!list || !list.offsetParent) return;            // SVN 视图没在显示
  const modal = $("modal");
  if (modal && !modal.classList.contains("hidden")) return;
  if (!SVN_SEL.size) return;
  e.preventDefault();
  svnDeleteSelected();
});

// ---------------------------------------------------------------------------
// 自动刷新（搬自原版的 auto refresh + pauseAuto / resumeAuto）
// ---------------------------------------------------------------------------
//
// 原版有八种触发（保存 / 新建 / 删除 / 重命名 / 窗口聚焦 / 视图可见 / 定时兜底…），
// 还配了 pauseAuto / resumeAuto 两个开关。它注释里特意写了**刻意不用
// FileSystemWatcher 盯整个工作区** —— 大仓库上开销太大。
//
// 这里同理，只保留最有用的三种触发：
//
//   · 打开 / 切回 SVN 视图时**必定刷一次** —— 最容易看出"内容是旧的"就是这里。
//     这一条**不受下面这个开关控制**（见 refreshSvnOnShow）：点进来就是想看
//     现在的改动，对着旧快照提交/撤销的代价比多扫一次大得多。
//   · 编辑器保存后刷 —— 改完就提交，这是最常见的动作序列
//   · 视图开着时的定时兜底 —— 外部工具 / 命令行改过文件也能跟上
//
// 开关（「自动」按钮）只管后两条，也就是"后台偷偷扫"这件事。
const SVN_AUTO = {
  on: true, at: 0, busy: false, timer: null,
  pending: new Set(),        // 等着"只重扫相关副本"的路径
  flushTimer: null,          // 上面那批的合并窗口
  again: false,              // busy 期间又要求刷新了一次，结束后来补
  staleTimer: null,          // 「后端给了旧快照」之后补拉真结果的那次定时
  pickupAt: 0,               // 已经为哪个快照时间戳安排过补拉（防止反复叠）
  pickupTries: 0,            // 同一份快照已经补拉过几次（后台慢时允许再试）
};
// 工作区纪元：切工作区就加一。正在跑的那次 status 是**上一个**工作区的，
// 回来时发现纪元变了就直接丢弃 —— 否则旧结果会把新工作区的树覆盖掉。
let SVN_EPOCH = 0;
// 跟原版对齐：autoRefreshDelayMs 默认 1200、autoRefreshIntervalMs 默认 300000。
// 兜底间隔调大是因为一次全量 status 要遍历几十个副本，太勤只会白烧磁盘。
const SVN_MIN_GAP = 1200;
const SVN_IDLE_MS = 300000;

const SVN_LABEL = {
  modified: "已修改", added: "已添加", deleted: "已删除", missing: "文件丢失",
  replaced: "已替换", conflicted: "冲突", merged: "合并过",
  obstructed: "被遮挡", incomplete: "不完整", external: "外部定义",
  unversioned: "未纳入版本", ignored: "已忽略",
};

/**
 * 按需刷新（节流 + 防重入）。
 *
 * @param force 用户明确点了刷新 —— 不看开关、不看间隔（否则点了没反应像坏了）
 * @param path  刚变动的文件路径：带了就走「只重扫它所属的那棵子树」，
 *              这是原版 scheduleAuto 那套 —— 保存一个文件不该重扫 55 个副本
 */
function maybeRefreshSvn(force, path) {
  if (!SVN_AUTO.on && !force) return;
  // 用户明确点了 ↻：绕过后端快照同步重算（fresh）。别的原因（保存 / 聚焦 /
  // 定时兜底）都走快照：那就地拿现成的，比逼着后端再扫一遍更符合"顺手刷新"。
  if (force) { refreshSvn(null, false, true); return; }
  if (curView !== "svn") return;                    // 视图没显示就别白扫
  if (path) { scheduleSvnPartial(path); return; }
  if (SVN_AUTO.busy) return;                        // 上一次还没回来
  const now = Date.now();
  if (now - SVN_AUTO.at < SVN_MIN_GAP) return;
  SVN_AUTO.at = now;
  refreshSvn();
}

/** 一堆文件可能连着变（保存 + 新建），先攒 700ms 再一起算。 */
function scheduleSvnPartial(path) {
  if (path) SVN_AUTO.pending.add(String(path));
  if (SVN_AUTO.flushTimer) return;
  SVN_AUTO.flushTimer = setTimeout(() => {
    SVN_AUTO.flushTimer = null;
    flushSvnPartial();
  }, 700);
}

/** 把攒下来的路径映射到"已显示的最深那棵树"，只重扫这几棵。 */
async function flushSvnPartial() {
  const paths = Array.from(SVN_AUTO.pending);
  SVN_AUTO.pending.clear();
  if (!paths.length) return;
  if (SVN_AUTO.busy) {                              // 正在全量扫，稍后再来
    paths.forEach((p) => SVN_AUTO.pending.add(p));
    scheduleSvnPartial("");
    return;
  }
  if (!SVN_TREE.length) { maybeRefreshSvn(true); return; }   // 还没有树：全量

  const roots = new Set();
  for (const p of paths) {
    const root = svnRootOf(p);
    if (!root) { maybeRefreshSvn(true); return; }    // 落到任何子树之外 → 只能全量
    roots.add(root);
  }
  SVN_AUTO.at = Date.now();
  await refreshSvn(Array.from(roots));
}

/** 这个路径落在当前显示的哪棵树里（取最深的那个根）。 */
function svnRootOf(path) {
  const p = String(path || "").replace(/\\/g, "/").toLowerCase();
  if (!p) return "";
  let best = "", bestLen = -1;
  for (const n of SVN_TREE) {
    const r = String(n.fsPath || "").replace(/\\/g, "/").toLowerCase();
    if (r && (p === r || p.startsWith(r + "/")) && r.length > bestLen) {
      best = n.fsPath;
      bestLen = r.length;
    }
  }
  return best;
}

/** 用新扫出来的子树替换掉旧的（同一个根），没有这个根就插进去。 */
function replaceSvnTree(root, subtree) {
  const key = String(root || "").replace(/\\/g, "/").toLowerCase();
  if (!key) return;
  const idx = SVN_TREE.findIndex(
    (n) => String(n.fsPath || "").replace(/\\/g, "/").toLowerCase() === key);
  const node = (subtree && subtree[0]) || null;
  if (idx >= 0) {
    if (node) SVN_TREE[idx] = node;
    else SVN_TREE.splice(idx, 1);
    return;
  }
  if (!node) return;
  SVN_TREE.push(node);
  SVN_TREE.sort((a, b) => String(a.name || "")
    .localeCompare(String(b.name || ""), undefined, { numeric: true }));
}

/** 汇总各根节点的 counts（countUp 已经把子孙算进根里了）。 */
function svnCountsOf(nodes) {
  const out = {};
  for (const n of (nodes || [])) {
    for (const k of Object.keys(n.counts || {})) {
      out[k] = (out[k] || 0) + n.counts[k];
    }
  }
  return out;
}

/** 局部刷新之后状态栏得自己重算 —— 没人再给 summary 了。 */
function updateSvnSummaryCounts() {
  const el = $("svnSummary");
  if (!el) return;
  const counts = svnCountsOf(SVN_TREE);
  const parts = [];
  let total = 0;
  for (const k of Object.keys(counts)) {
    total += counts[k];
    parts.push((SVN_LABEL[k] || k) + " " + counts[k]);
  }
  el.textContent = (total
    ? total + " 项改动（" + parts.join("、") + "）"
    : "无改动") + svnSnapTail(SVN_SNAP) + (SVN_AUTO.on ? "" : "　（后台自动刷新已关）");
}

// 窗口重新获得焦点 / 标签页切回来 → 补刷一次（原版的 refreshOnFocus）。
// 别的时候在别的窗口（命令行、别的编辑器）改过文件，切回来看到的就不该是旧的。
window.addEventListener("focus", () => maybeRefreshSvn());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) maybeRefreshSvn();
});

/**
 * 显示「全仓库」操作的结果。
 *
 * 工作区下挂着几十个独立仓库时，后端会逐个跑并返回
 * `{total, okCount, failCount, results}`。全部成功就一行汇总；
 * 有失败则把失败的那几个单独列出来 —— 不然用户只知道「失败了 3 个」，
 * 还得自己去猜是哪 3 个。
 */
function showSvnMultiResult(res) {
  const sum = $("svnSummary");
  if (!res) { sum.textContent = "操作失败"; return; }
  const d = res.data || {};
  // 只有真的逐仓库跑过（带 results 数组）才走"多仓库汇总"这条路。
  // 新加的 clean_scan / clean_remove / revert_all 虽然也有 total，
  // 但没有 results，硬套进来会把失败信息丢掉。
  if (typeof d.total !== "number" || !Array.isArray(d.results)) {
    sum.textContent = res.summary || (res.ok ? "" : "操作失败");
    sum.title = res.content ? String(res.content).slice(0, 800) : "";
    return;
  }
  let text = res.summary || "";
  const failed = (d.results || []).filter((r) => !r.ok);
  if (failed.length) {
    const names = failed.slice(0, 5).map((r) => baseName(r.root)).join("、");
    text += "　失败：" + names + (failed.length > 5 ? " 等" : "");
    sum.title = failed.map((r) => baseName(r.root) + "：" + (r.error || "")).join("\n");
  } else {
    sum.title = "";
  }
  sum.textContent = text;
}

/** 视图开着时低频兜底。 */
function scheduleSvnIdle() {
  if (SVN_AUTO.timer) return;
  SVN_AUTO.timer = setInterval(() => {
    if (curView === "svn") maybeRefreshSvn();
  }, SVN_IDLE_MS);
}

/**
 * 打开（或切回）SVN 视图时**必定刷一次**。
 *
 * 和 `maybeRefreshSvn()` 的区别：不看「自动」开关，也不吃 SVN_MIN_GAP 节流。
 * 理由很直接 —— 点这个图标就是想看**现在**有什么改动；关掉「自动」只是不想让
 * 后台偷偷扫（大工作区一次约 2 秒），不代表愿意对着一份旧快照去提交 / 撤销。
 * 切工作区那条也是同样的口径（见 WS_HANDLERS.svn）。
 *
 * 正在扫的时候进来：记一个 again，那次结束后 refreshSvn 会自己补一遍 ——
 * 不会出现"点了图标还是旧内容"。
 */
function refreshSvnOnShow() {
  if (SVN_AUTO.busy) { SVN_AUTO.again = true; return; }
  SVN_AUTO.at = Date.now();
  // 先丢一次预热：万一启动时那次预热失败（未登录 / 422 工作区还没定），
  // 这一次把后端唤醒。已有一份新鲜快照时它什么都不做（后端单飞去重）。
  svnWarm();
  // 不带 fresh：后端有快照就直接给现成的（"打开即用"），没有就现算。
  // 有旧快照时它会先返回旧的再后台补 —— 比让用户对着"扫描中…"干等十几秒强。
  refreshSvn();
}

/**
 * 自动刷新开关。
 *
 * `quiet=true` 只改外观、不触发扫描（首次进视图时用：紧接着 onshow 就会刷，
 * 这里再扫一遍等于首屏白等两个来回）。
 */
function setSvnAuto(on, quiet) {
  SVN_AUTO.on = !!on;
  const b = $("svnAuto");
  if (!b) return;
  b.classList.toggle("on", SVN_AUTO.on);
  b.style.color = SVN_AUTO.on ? "var(--accent)" : "";
  b.title = SVN_AUTO.on
    ? "自动刷新：开（保存文件 / 窗口回到前台时重扫）—— 点一下关掉"
      + "\n（打开这个视图时总会刷一次，不受此开关影响）"
    : "自动刷新：关（只有打开视图 / 点 ↻ 时才扫）—— 点一下打开";
  if (SVN_AUTO.on && !quiet) maybeRefreshSvn(true);
}

/**
 * 重扫。
 *
 * `targets`（可选）= 只重扫这几棵子树。保存一个文件走这条，别每次都扫
 * 55 个副本（约 2 秒）。没给就全量重扫，状态栏文案也由后端给。
 */
//: 全量 status 要扫**所有**工作副本（gu 这种工程有几十个模块），
//: api() 默认 15 秒根本不够 —— 后端还在跑，前端先报"请求超时，服务可能卡住了"，
//: 看着就像插件死了。后端那条 svn 进程的上限是 120 秒，这里给 3 分钟余量。
const SVN_STATUS_TIMEOUT = 180000;

//: 提交是重活：后端那条 `svn commit` 进程上限 600 秒（`__init__.py:818`），
//: 前端得比它宽。**以前这里没传 timeout**，吃的是 api() 默认的 15 秒 ——
//: 几千项 / 上百 MB 的提交必然先在前端报"请求超时"，而后端其实还在老老实实
//: 地传；用户看报错再点一次，就真成了重复提交。后端 600s + 60s 余量。
const SVN_COMMIT_TIMEOUT = 660000;

//: 「目录自身就是一个提交对象，只能整棵提交」的状态。
//:
//: 这些状态说明**该目录在版本库里不存在或整体消失**，svn 因此要求：只要提交
//: 里出现它下面的子项，就必须把目录自己一起放进清单（否则回
//: `E200009 ... is not part of the commit`）。
//:
//: `modified` **不在**这里：目录被标 M 通常只是属性改动，目录本身早就在版本库
//: 里了，不需要进清单；把它当整棵提交反而会顺手带上用户没勾的文件。
const SVN_WHOLE_STATUS = {
  added: 1, replaced: 1, unversioned: 1, missing: 1, deleted: 1,
};

/** 复制一段文本（不假设内核有 copyText —— 那个目前挂在 adb 插件里）。 */
function svnCopyText(text) {
  const s = String(text || "");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(s)
      .then(() => toast("已复制"))
      .catch(() => toast("复制失败，请手动选中复制", true));
    return;
  }
  // 老浏览器 / 非安全上下文：临时 textarea + execCommand
  const ta = document.createElement("textarea");
  ta.value = s;
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  toast(ok ? "已复制" : "复制失败，请手动选中复制", !ok);
}

//: 后端 `_svn.classify_error` 给的 kind → 界面上那行小字。
const SVN_FAIL_KIND = {
  auth: "认证失败", forbidden: "无提交权限", hook: "服务器 hook 拒绝",
  notfound: "路径不存在", net: "连不上服务器", cert: "证书校验失败",
  locked: "路径被锁", timeout: "超时", unknown: "未归类",
  parent_missing: "提交清单缺父目录",
};

/**
 * 提交失败时，把 svn 的**原文**摆进提交弹窗里。
 *
 * 为什么不能只 toast：后端返回的是 `{summary, content:<svn 原文>}`，而 toast
 * 只显示 summary —— `svn: E175013: Access to '/svn/...' forbidden` 这一行
 * 直接被丢掉，用户看到的永远是"提交失败"四个字，等于没有报错信息。
 *
 * 为什么原地插而不是新开弹窗：新弹窗会把勾好的改动树和刚写的提交说明一起
 * 清掉，报错之后还得从头再选一遍。这里只往 modal body 顶上塞一块。
 */
function svnInlineFail(host, res) {
  if (!host || !host.appendChild) return;
  const r = res || {};
  const data = r.data || {};
  const old = host.querySelector ? host.querySelector(".svn-fail") : null;
  if (old) old.remove();                     // 重试再次失败时替换掉上一次那块

  const box = document.createElement("div");
  box.className = "svn-fail";
  box.style.cssText = "border:1px solid var(--danger);border-radius:6px;"
    + "padding:8px;margin-bottom:8px;background:rgba(220,60,60,.07)";

  const head = document.createElement("div");
  head.style.cssText = "color:var(--danger);font-weight:600;line-height:1.6;"
    + "margin-bottom:4px";
  head.textContent = "提交失败：" + (r.summary || "未知原因");
  box.appendChild(head);

  const hint = String(data.hint || "");
  if (hint) {
    const h = document.createElement("div");
    h.style.cssText = "font-size:12px;color:var(--muted);line-height:1.75;"
      + "margin-bottom:6px;white-space:pre-wrap";
    h.textContent = hint;
    box.appendChild(h);
  }

  const pre = document.createElement("pre");
  pre.style.cssText = "max-height:22vh;overflow:auto;margin:0;font-size:11px;"
    + "line-height:1.5;white-space:pre-wrap;word-break:break-all;"
    + "background:rgba(0,0,0,.18);padding:6px;border-radius:4px;"
    + "user-select:text";
  pre.textContent = String(r.content || "（后端这次没有带回 svn 原文）");
  box.appendChild(pre);

  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;gap:8px;align-items:center;margin-top:6px";
  const cp = document.createElement("button");
  cp.className = "tbtn mini";
  cp.textContent = "复制原文";
  cp.addEventListener("click", () => svnCopyText(String(r.content || "")));
  bar.appendChild(cp);
  const kind = document.createElement("span");
  kind.style.cssText = "font-size:11px;color:var(--muted)";
  kind.textContent = SVN_FAIL_KIND[data.kind] || SVN_FAIL_KIND.unknown;
  bar.appendChild(kind);
  box.appendChild(bar);

  host.insertBefore(box, host.firstChild);
  // 改动树占 38vh，错误块塞在最上面时用户可能还在下面滚动 —— 拽回来
  try { box.scrollIntoView({ block: "nearest" }); } catch (e) { /* 老浏览器 */ }
}

/**
 * 状态栏尾巴：把「快照有多旧 / 后台还在补」说清楚。
 *
 * 为什么必须显示：后端现在会**先把旧快照给界面**（stale-while-revalidate），
 * 于是面板上可能出现"上一次算的结果"。不标注的话，用户刚在命令行改完文件、
 * 切回来看到干净的列表，会以为插件坏了 —— 而其实一秒后就会自己更新。
 */
function svnSnapTail(snap) {
  const s = snap || {};
  const ageMs = Number(s.age_ms);
  if (!isFinite(ageMs)) return "";
  const sec = Math.max(0, Math.round(ageMs / 1000));
  if (s.stale) {
    return "　⟳ " + sec + " 秒前的快照，正在后台更新…";
  }
  if (!s.at) return "";
  return "　· " + sec + " 秒前刷新";
}

async function refreshSvn(targets, rescan, fresh) {
  if (SVN_AUTO.busy) {
    // 切工作区正好撞上一次还在跑的扫描：记下来，结束后自动补一次。
    // 直接 return 的话新工作区就一直停在"正在重新扫描…"。
    SVN_AUTO.again = true;
    return;
  }
  SVN_AUTO.busy = true;
  const list = $("svnList");
  const ep = SVN_EPOCH;          // 这次扫描属于哪个工作区
  const partial = Array.isArray(targets) && targets.length > 0 && SVN_TREE.length > 0;
  // 文案带上预期：几十个副本的工程要扫几秒到十几秒，只写"读取中…"
  // 会让人以为卡死（之前还有 15 秒超时，正好卡在这个心理点上）
  if (!partial) $("svnSummary").textContent = "扫描中…（大工程十几秒）";
  // rescan：重新遍历目录找工作副本（新建 / 删除过仓库时用）。平时刷新只跑
  // status —— 副本清单第一次扫完就落在 `<工作区>/.ai` 下，之后直接读。
  // fresh：绕过后端的内存快照同步重算（写操作后用，别拿旧快照下结论）。
  const extra = {};
  if (rescan) extra.rescan = true;
  if (fresh) extra.fresh = true;
  try {
    if (partial) {
      for (const root of targets) {
        const r = await api("/api/plugin/svn_tools/status",
                            Object.assign(svnStatusOpts(), { path: root }, extra),
                            { timeout: SVN_STATUS_TIMEOUT });
        if (ep !== SVN_EPOCH) { SVN_AUTO.again = true; return; }
        const res = r.result || {};
        replaceSvnTree(root, res.tree || []);
      }
      SVN_UNV.cache = {};        // 内容可能已经变了，展开缓存跟着失效
      renderSvnTree();
      updateSvnSummaryCounts();
    } else {
      const r = await api("/api/plugin/svn_tools/status",
                          Object.assign(svnStatusOpts(), extra),
                          { timeout: SVN_STATUS_TIMEOUT });
      if (ep !== SVN_EPOCH) { SVN_AUTO.again = true; return; }
      const res = r.result || {};
      // 后端走内存快照：可能直接给一份**现成的**结果（打开面板不再等十几秒），
      // 也可能给一份**旧的**再后台重算（snapshot.stale=true）。
      // 旧的也先渲染 —— 用户马上看到上次的改动树，比对着"扫描中…"干等好；
      // 真正的结果由下面那次补拉送回来。
      const snap = res.snapshot || {};
      SVN_SNAP = snap;
      $("svnSummary").textContent = (res.summary || "无改动") + svnSnapTail(snap) +
        (SVN_AUTO.on ? "" : "　（后台自动刷新已关）");
      // 树缓存下来：折叠 / 懒展开只需要重画，不必再扫一遍（55 个副本约 2 秒）
      SVN_TREE = res.tree || [];
      SVN_WC_AGE = (typeof res.wc_age === "number") ? res.wc_age : -1;
      SVN_UNV.cache = {};
      renderSvnTree();
      if (snap.stale) scheduleSvnStalePickup(snap);
    }
  } catch (e) {
    if (ep !== SVN_EPOCH) { SVN_AUTO.again = true; return; }
    if (!partial) {              // 局部刷新失败不打扰用户，下次全量兜底
      $("svnSummary").textContent = "失败";
      list.innerHTML = "";
      list.appendChild(emptyBox("读取失败：" + e.message));
    }
  } finally {
    SVN_AUTO.busy = false;
    SVN_AUTO.at = Date.now();
    if (SVN_AUTO.again) { SVN_AUTO.again = false; refreshSvn(); }
  }
}

/**
 * 后端给了一份**旧快照**（`snapshot.stale=true`）—— 等它后台算完再取一次。
 *
 * 1500ms 而不是立刻：后端那条 `svn status` 在几十个副本的工程上要 1~3 秒，
 * 立刻再来一发只会拿到同一份旧快照（而且又起一个后台线程，白烧磁盘）。
 * 同一份快照最多补拉 3 次（后台特别慢时），之后就交给 5 分钟的定时兜底 ——
 * 无限重试会把前端自己忙成死循环。
 */
function scheduleSvnStalePickup(snap) {
  const at = Number(snap && snap.at) || 0;
  if (!at) return;
  if (at !== SVN_AUTO.pickupAt) {
    SVN_AUTO.pickupAt = at;
    SVN_AUTO.pickupTries = 0;
  }
  if (SVN_AUTO.pickupTries >= 3) return;
  SVN_AUTO.pickupTries += 1;
  if (SVN_AUTO.staleTimer) clearTimeout(SVN_AUTO.staleTimer);
  SVN_AUTO.staleTimer = setTimeout(() => {
    SVN_AUTO.staleTimer = null;
    if (curView === "svn") refreshSvn();
  }, 1500);
}

// ---------------------------------------------------------------------------


/**
 * 提交面板：勾选要提交的项 + 写提交说明。
 *
 * 后端 `commit` 自带 add/rm 预处理（新增的文件先 add、手工删掉的先 rm），
 * 所以这里只要把「勾了什么」传过去就行 —— 不像裸 `svn commit` 那样得自己
 * 先跑一遍 add/rm，否则新文件会被静默漏掉。
 *
 * 撞上 out-of-date 时后端会返回 `retryable` + `outOfDate`，
 * 这里问一句「更新并重试」，而不是把 svn 的报错原样甩给用户。
 */
/**
 * 提交面板。`scopeDir` 有值时只列**那个目录下**的改动（文件夹右键进来走这条），
 * 不传就是整个工作副本（SVN 面板工具栏那个「⇧ 提交」）。
 *
 * 同一个弹窗两处复用：勾选列表、冲突提示、说明框、过期重试全不用写第二遍。
 */
async function svnCommitPanel(scopeDir) {
  const scope = scopeDir ? { path: scopeDir } : {};
  const body = document.createElement("div");
  body.style.cssText = "font-size:13px";
  body.textContent = "读取改动…";
  const foot = document.createElement("div");
  const sp = document.createElement("span");
  sp.className = "spacer";
  const cancel = document.createElement("button");
  cancel.className = "tbtn";
  cancel.textContent = "取消";
  cancel.addEventListener("click", closeModal);
  const go = document.createElement("button");
  go.className = "tbtn";
  go.textContent = "提交";
  go.disabled = true;
  foot.appendChild(sp);
  foot.appendChild(cancel);
  foot.appendChild(go);
  openModal(scopeDir ? "提交 · " + baseName(scopeDir) : "提交改动",
            body, foot, "diff-card");

  let entries = [], tree = [], wcs = [];
  try {
    const r = await api("/api/plugin/svn_tools/status",
                        Object.assign(svnStatusOpts(), scope, { fresh: true }),
                        { timeout: SVN_STATUS_TIMEOUT });
    const res = r.result || {};
    entries = res.entries || [];
    tree = res.tree || [];
    wcs = res.wcs || [];
  } catch (e) {
    body.textContent = "读取失败：" + e.message;
    return;
  }

  if (!entries.length || !tree.length) {
    body.textContent = scopeDir
      ? "「" + baseName(scopeDir) + "」下面没有可提交的改动。"
      : "工作副本是干净的，没有可提交的改动。";
    return;
  }

  // 冲突没解决的话提交一定会被 svn 拒 —— 提前说清楚，别让用户白写说明
  const conflicts = [];
  (function walk(nodes) {
    for (const n of (nodes || [])) {
      if (n.status === "conflicted") conflicts.push(n.fsPath);
      walk(n.children);
    }
  })(tree);
  if (conflicts.length) {
    body.innerHTML = "";
    const warn = document.createElement("div");
    warn.style.cssText = "color:var(--danger);margin-bottom:8px;line-height:1.7";
    warn.textContent = "有 " + conflicts.length + " 个文件处于冲突状态，"
      + "提交前必须先解决：\n"
      + conflicts.map((p) => "  " + relPath(p, S.workspace)).join("\n")
      + "\n\n改完文件之后右键「标记已解决」。";
    body.appendChild(warn);
    const fix = document.createElement("button");
    fix.className = "tbtn";
    fix.textContent = "把冲突全部标记为已解决（保留我的内容）";
    fix.addEventListener("click", async () => {
      const ok = await confirmBox("标记已解决",
        "把这些冲突文件标记为「已解决」，并按当前文件内容算？\n\n"
        + "确认文件里的 <<<<<<< 标记已经手工清掉了再点。", "标记已解决");
      if (!ok) return;
      const res = await svnCall("resolve", { targets: conflicts }, "已标记解决");
      if (res && res.ok) { closeModal(); refreshSvn(); }
    });
    body.appendChild(fix);
    return;
  }

  body.innerHTML = "";

  // ---- 提交到哪个远程 -------------------------------------------------------
  // 每个工作副本各指一个 URL（55 个模块就是 55 个），只能问 svn info；
  // 多副本时显示其中一个 + 总数，避免"以为只提交了一个模块"。
  const remote = document.createElement("div");
  remote.style.cssText = "font-size:11px;color:var(--muted);margin-bottom:8px;"
    + "word-break:break-all;line-height:1.6";
  remote.textContent = "提交到：读取中…";
  body.appendChild(remote);
  (async () => {
    const target = scopeDir || wcs[0] || "";
    if (!target) { remote.textContent = ""; return; }
    try {
      const ir = await api("/api/plugin/svn_tools/info", { path: target });
      const d = ((ir.result || {}).data) || {};
      if (!d.url) { remote.textContent = ""; return; }
      // revision 是**这个路径**的 BASE 版本。刚 commit 过的目录还是旧版本号，
      // 显示 "r0" 只会让人困惑，所以 0 不显示。
      const rev = d.revision && d.revision !== "0" ? "（r" + d.revision + "）" : "";
      remote.textContent = "提交到：" + d.url + rev
        + (wcs.length > 1 ? "　共 " + wcs.length + " 个工作副本" : "");
      remote.title = "点击复制 URL";
      remote.style.cursor = "pointer";
      remote.addEventListener("click", () => {
        if (!navigator.clipboard) { toast(d.url); return; }
        navigator.clipboard.writeText(d.url)
          .then(() => toast("已复制 URL")).catch(() => toast(d.url));
      });
    } catch (e) { remote.textContent = ""; }
  })();

  // ---- 改动树：目录可折叠 + 父子半选 --------------------------------------
  // 勾选的最小单位是「叶子」（文件 / 未纳入版本的目录 / 没有子节点的目录）；
  // 目录的勾是**派生**出来的：全选=勾、部分=半选、全不选=不勾。
  // 提交时整棵子树全选就只传目录一个路径 —— svn commit 对目录是递归的，
  // 顺便把命令行长度压下去（不然几十个副本全选就是上千条路径）。
  const leavesOf = (n) => (n.children && n.children.length)
    ? n.children.flatMap(leavesOf) : [n.fsPath];
  const allLeaves = tree.flatMap(leavesOf);
  const sel = new Set(allLeaves);
  const collapsed = new Set();

  const list = document.createElement("div");
  list.style.cssText = "max-height:38vh;overflow:auto;border:1px solid var(--border);"
    + "border-radius:6px;padding:4px 0;margin-bottom:8px";
  const countEl = document.createElement("span");
  countEl.style.cssText = "font-size:11px;color:var(--muted)";
  // 「会自动 add / rm 什么」的提示：跟着勾选实时算（见下面 paint）
  const note = document.createElement("div");
  note.style.cssText = "font-size:11px;color:var(--muted);margin-top:6px";

  // 叶子 → 它自己的状态（unversioned 要 add、missing 要 rm），提示文案用
  const leafMeta = new Map();
  (function walkMeta(nodes) {
    for (const n of (nodes || [])) {
      if (!n.children || !n.children.length) leafMeta.set(n.fsPath, n.status || "");
      else walkMeta(n.children);
    }
  })(tree);

  const paint = () => {
    list.innerHTML = "";
    const addRow = (n, depth) => {
      const kids = n.children || [];
      const leafs = leavesOf(n);
      const chosen = leafs.filter((p) => sel.has(p)).length;
      const row = document.createElement("label");
      row.style.cssText = "display:flex;align-items:center;gap:6px;padding:3px 8px;"
        + "font-size:12px;cursor:pointer;padding-left:" + (8 + depth * 14) + "px";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = leafs.length > 0 && chosen === leafs.length;
      cb.indeterminate = chosen > 0 && chosen < leafs.length;
      cb.addEventListener("change", () => {
        leafs.forEach((p) => { if (cb.checked) sel.add(p); else sel.delete(p); });
        paint();
      });
      row.appendChild(cb);

      if (kids.length) {
        const caret = document.createElement("span");
        caret.textContent = collapsed.has(n.fsPath) ? "▸" : "▾";
        caret.style.cssText = "width:10px;color:var(--muted);flex:0 0 10px";
        caret.addEventListener("click", (ev) => {
          ev.preventDefault();      // 别让 <label> 把这次点击转成"勾选"
          ev.stopPropagation();
          if (collapsed.has(n.fsPath)) collapsed.delete(n.fsPath);
          else collapsed.add(n.fsPath);
          paint();
        });
        row.appendChild(caret);
      } else {
        const pad = document.createElement("span");
        pad.style.cssText = "width:10px;flex:0 0 10px";
        row.appendChild(pad);
      }

      if (n.status) row.appendChild(svnTag(n.status));
      const txt = document.createElement("span");
      txt.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;"
        + "white-space:nowrap";
      txt.textContent = n.name + (kids.length ? "  (" + (n.total || 0) + ")" : "");
      txt.title = n.fsPath;
      row.appendChild(txt);

      list.appendChild(row);
      if (kids.length && !collapsed.has(n.fsPath)) {
        kids.forEach((c) => addRow(c, depth + 1));
      }
    };
    tree.forEach((n) => addRow(n, 0));
    countEl.textContent = "已选 " + sel.size + " / " + allLeaves.length + " 项";

    // 预处理预估必须跟**勾选**一致：以前是整片范围算一次，勾掉一半也不变，
    // 于是"提交时会自动纳入 12 项"跟实际提交的东西对不上。
    let addN = 0, rmN = 0;
    sel.forEach((p) => {
      const st = leafMeta.get(p);
      if (st === "unversioned") addN += 1;
      else if (st === "missing") rmN += 1;
    });
    const noteText = (addN || rmN)
      ? ("提交时会自动：纳入版本控制 " + addN + " 项、从版本移除 " + rmN + " 项")
      : "没有需要自动预处理的新增/删除";
    // 根目录自身就是一个提交对象（新增目录整棵）时，勾选**没法**再细分：
    // 提交目标就是那个目录，svn 会连同它下面所有东西一起提交。
    // 不说清楚，用户以为取消勾选能把它排除掉。
    const wcWhole = tree.filter((n) => n.kind === "wc" && SVN_WHOLE_STATUS[n.status]);
    note.textContent = wcWhole.length
      ? (noteText + "；「" + wcWhole[0].name + "」本身是新增/删除，"
         + "会整棵纳入本次提交（不能只挑里面的文件）")
      : noteText;
  };
  paint();
  body.appendChild(list);

  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;gap:8px;margin-bottom:8px;align-items:center";
  const all = document.createElement("button");
  all.className = "tbtn mini";
  all.textContent = "全选";
  all.addEventListener("click", () => { allLeaves.forEach((p) => sel.add(p)); paint(); });
  const none = document.createElement("button");
  none.className = "tbtn mini";
  none.textContent = "全不选";
  none.addEventListener("click", () => { sel.clear(); paint(); });
  bar.appendChild(all);
  bar.appendChild(none);
  bar.appendChild(countEl);
  body.appendChild(bar);

  /** 勾选结果 → 提交参数：整棵子树全选的收敛成目录本身。 */
  const collectTargets = () => {
    const out = [];
    const walk = (n) => {
      // wc 节点是**透传**的包装（它自己不是可提交对象）：收集要落到孩子上。
      // 但 children 为空时必须按叶子走 —— 以前无条件 `return`，那种空 wc 节点
      // （scope 是单个文件时后端给的旧形状，现已改成 kind="file"）会被静默吞掉：
      // allLeaves 通过 leavesOf 算得出 1 项、显示「已选 1 / 1」，
      // collectTargets 却一个都不收，点提交弹「什么都没勾」。留着这层防御，
      // 后端再变形状也不会表现成"勾了却提交不了"。
      if (n.kind === "wc") {
        // 目录**自身就是一个提交对象**时（新增 / 已删除 / 未纳入版本，见
        // SVN_WHOLE_STATUS）必须把它自己交上去：只列下面的子项、漏掉父目录，
        // 服务器会当场拒 ——
        //   E200009 '...\audio-ai' is not known to exist in the repository
        //           and is not part of the commit, yet its child '...\src' is
        // 而且这类目录在 svn 层面只能**整棵**提交，逐个子项挑根本提交不了。
        // 所以这里不再往下沉：推父目录，让 svn 自己递归。
        if (SVN_WHOLE_STATUS[n.status]) {
          out.push(n.fsPath);
          return;
        }
        if ((n.children || []).length) {
          n.children.forEach(walk);
          return;
        }
      }
      const leafs = leavesOf(n);
      if (leafs.length && leafs.every((p) => sel.has(p))) {
        out.push(n.fsPath);
        return;
      }
      (n.children || []).forEach(walk);
    };
    tree.forEach(walk);
    return out;
  };

  const msg = document.createElement("textarea");
  msg.rows = 2;
  msg.placeholder = "提交说明（必填）";
  msg.style.cssText = "width:100%;box-sizing:border-box";
  body.appendChild(msg);
  body.appendChild(note);      // note 的内容由 paint() 按勾选实时更新

  go.disabled = false;
  go.addEventListener("click", async () => {
    const targets = collectTargets();
    if (!targets.length) { toast("什么都没勾", true); return; }
    const text = msg.value.trim();
    if (!text && SVN_OPTS.requireMessage) {
      toast("提交说明不能为空", true);
      msg.focus();
      return;
    }
    // 「说明必填」关掉时得让后端知道可以空着提交（svn 要 --force-log）
    const commitBody = {
      message: text,
      targets,
      allow_empty_message: !SVN_OPTS.requireMessage,
    };

    go.disabled = true;
    go.textContent = "提交中…";
    const reset = () => { go.disabled = false; go.textContent = "提交"; };

    try {
      // quiet：失败要自己处理（过期重试 / 冲突提示），不想被自动 toast
      let res = await svnCall("commit", commitBody, null, true,
                              { timeout: SVN_COMMIT_TIMEOUT });

      // 过期是 svn 里最常见的失败，而且有明确下一步 —— 直接问「更新并重试」
      if (res && !res.ok && (res.data || {}).retryable) {
        const stale = ((res.data || {}).outOfDate || []).length;
        const ok = await confirmBox("有文件过期",
          "仓库里有别人先提交的改动" + (stale ? "（" + stale + " 个路径）" : "")
          + "，需要先更新。\n\n要更新并重试提交吗？", "更新并重试");
        if (!ok) { reset(); return; }
        // 只更新**报过期的那几个路径**；后端给了范围就照它走，
        // 没给（老 svn 不报路径）才退回全量 update。
        const stalePaths = (res.data || {}).outOfDate || [];
        await svnCall("update", stalePaths.length ? { targets: stalePaths } : {});
        res = await svnCall("commit", commitBody, null, true,
                            { timeout: SVN_COMMIT_TIMEOUT });
      }

      if (!res) { reset(); return; }               // 请求本身失败（已 toast）
      if (!res.ok) {
        // 原文必须给用户看：summary 只是一句结论，真正的原因
        // （E175013 无权限 / pre-commit hook 拒绝 / E160013 路径不存在…）
        // 全在 content 里。以前这里只 toast summary，等于把报错丢了。
        svnInlineFail(body, res);
        reset();
        return;
      }

      toast(res.summary || "已提交");
      closeModal();
      refreshSvn();
    } catch (e) {
      // 兜底：连 svn 原文都没有的异常，也别只留一句"提交失败："，
      // 至少把 message / stack 摆出来（时间戳和调用栈能看出是哪一类）。
      svnInlineFail(body, { ok: false, summary: "提交请求异常",
        content: String((e && (e.stack || e.message)) || e),
        data: { kind: (e && e.timeout) ? "timeout" : "unknown" } });
      reset();
    }
  });
}

// ---------------------------------------------------------------------------
// 差异对比：左右两列（同 VSCode 的 diff 视图）
// ---------------------------------------------------------------------------
// 后端 `diff_text` 给的是 unified diff 解析后的结构化数据（每行带 `t`：
// `+` / `-` / 空格）。并排显示要把「先一段 -、再一段 +」按顺序一行对一行
// 摊成两列：左边是仓库里的旧行，右边是本地的新行，多出来的一边留空。
function svnHunkStart(header) {
  const m = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(String(header || ""));
  return m ? [parseInt(m[1], 10), parseInt(m[2], 10)] : [0, 0];
}

function svnPairHunk(hunk) {
  const [a, b] = svnHunkStart(hunk.header);
  let oldNo = a, newNo = b;
  const rows = [];
  const lines = hunk.lines || [];
  const row = (l, ln, r, rn, lt, rt) => rows.push({ l, ln, r, rn, lt, rt });

  for (let i = 0; i < lines.length;) {
    const t = lines[i].t;
    if (t === " ") {                       // 上下文行：两边都画同样的内容
      row(lines[i].s, oldNo, lines[i].s, newNo, "ctx", "ctx");
      oldNo++; newNo++; i++;
      continue;
    }
    if (t !== "-" && t !== "+") { i++; continue; }

    // 一段改动：unified 的顺序是「先 - 后 +」。
    // 这里的分块规则必须和后端 `_hunk_blocks` 完全一致（都是「一段 - 紧跟一段 +」），
    // 否则界面上第几块箭头和后端还原的第几块会对不上。
    const del = [], add = [];
    while (i < lines.length && lines[i].t === "-") del.push(lines[i++].s);
    while (i < lines.length && lines[i].t === "+") add.push(lines[i++].s);

    const n = Math.max(del.length, add.length);
    for (let k = 0; k < n; k++) {
      const hasDel = k < del.length, hasAdd = k < add.length;
      row(hasDel ? del[k] : null, hasDel ? oldNo++ : 0,
          hasAdd ? add[k] : null, hasAdd ? newNo++ : 0,
          hasDel ? "del" : "empty", hasAdd ? "add" : "empty");
    }
    // 这一段的第一行打上标记：渲染时在这一行的中缝放「◀ 还原这一处」
    if (n > 0) rows[rows.length - n].start = true;
  }
  return rows;
}

/**
 * 一格 HTML 转义。为什么不复用内核的 `esc`：插件脚本虽然和内核共享作用域，
 * 但插件独立加载/独立版本时那个函数可能不存在，自己带一个才不挑宿主。
 */
function svnHlEsc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 染色上限跟编辑器保持一致（80-editor.js 的 HL_LIMIT / HL_TOKEN_LIMIT）：
// 同一个文件在编辑器里不染、在对比里染了，反而会让人觉得"两边不一样"。
// 超限/没插件/请求失败一律返回 null，调用方退纯文本 —— 颜色丢了不影响 diff。
const SVN_HL_LIMIT = 120000;
const SVN_HL_TOKEN_LIMIT = 20000;
const SVN_HL_CACHE_MAX = 8;
const SVN_HL_CACHE = new Map();

/** 这个文件交给哪个插件染色（内核能力表里没有就返回 null）。 */
function svnHlTarget(path) {
  const caps = (typeof EDITOR_CAPS !== "undefined" && EDITOR_CAPS)
    ? EDITOR_CAPS.highlight : null;
  if (!caps) return null;
  const m = /\.([A-Za-z0-9+]+)$/.exec(path || "");
  if (!m) return null;
  const hit = caps[m[1].toLowerCase()];
  return (hit && hit.plugin && hit.func) ? { plugin: hit.plugin, func: hit.func } : null;
}

/**
 * 拿一侧整段文本的 token 表（失败/不适用返回 null）。
 *
 * 为什么**整段一次请求**：并排对比里一个文件动辄几百行，按行发请求就是几百次
 * HTTP —— 面板会卡死、服务端也会被打爆。token 是相对整段文本的字符偏移，
 * 拿到以后按行切（见 svnLineHtml）。
 *
 * `path` 一定要带上：cfamily 那套靠扩展名区分 c / cpp / java，只给 code
 * 的话 .java 会被当成 C 处理，`import` / `extends` 一个都认不出来。
 */
async function svnHighlight(path, text) {
  const h = svnHlTarget(path);
  if (!h || !text || text.length > SVN_HL_LIMIT) return null;
  // key 里带长度：同一个文件的左右两段（BASE / 本地）长度通常不同，
  // 只按 path+func 存会互相顶掉，来回切文件时白跑请求。
  const key = h.func + "|" + path + "|" + text.length;
  const hit = SVN_HL_CACHE.get(key);
  if (hit && hit.text === text) return hit.tokens;
  try {
    const r = await api("/api/plugin/" + h.plugin + "/" + h.func,
                        { code: text, path: path });
    const tokens = (r.result && r.result.tokens) || null;
    // token 太多的话拼出来的 DOM 比纯文本更卡（编辑器那边同样的取舍）
    if (!tokens || !tokens.length || tokens.length > SVN_HL_TOKEN_LIMIT) return null;
    SVN_HL_CACHE.set(key, { text: text, tokens: tokens });
    // Map 按插入顺序迭代，超了就丢最早那条（够用，不做 LRU 命中重排）
    if (SVN_HL_CACHE.size > SVN_HL_CACHE_MAX) {
      SVN_HL_CACHE.delete(SVN_HL_CACHE.keys().next().value);
    }
    return tokens;
  } catch (e) {
    return null;
  }
}

/**
 * 把一行行的 diff 拼成**整段文本**，同时记下每一行在整段里的 `[start, end)`。
 *
 * 为什么要有：token 的偏移是相对整段的，而界面上是按行画的格子。没有这张
 * 位置表，就只能按行去猜偏移，跨行注释/字符串一定会串位。
 *
 * 空行（对边没有对应行）也占一个位置，末尾补 `\n` —— 行号必须和 rows 严格
 * 一一对应，漏一行后面全错。
 */
function svnJoinSide(rows, key) {
  const parts = [];
  const spans = [];
  let pos = 0;
  for (const row of rows) {
    const v = row[key];
    const s = (v === null || v === undefined) ? "" : String(v);
    spans.push([pos, pos + s.length]);
    parts.push(s);
    pos += s.length + 1;
  }
  return { text: parts.join("\n"), spans: spans };
}

/**
 * 一行（整段里的 `[a, b)`）渲染成带 `tk-*` 的 HTML。
 *
 * 跨行 token（块注释、多行字符串）靠 `[a,b)` 切片自然处理：每行只取和自己
 * 重叠的那一段，所以同一条注释在每一行都各自上一个 span，颜色连得上。
 * 用二分找第一个可能重叠的 token，避免每行都从 tokens[0] 开始扫（几百行 ×
 * 几千 token 会明显拖慢）。
 */
function svnLineHtml(text, a, b, tokens) {
  if (!tokens || !tokens.length || b <= a) return svnHlEsc(text.slice(a, b));
  let lo = 0, hi = tokens.length;
  while (lo < hi) {                    // 第一个 end > a 的 token
    const mid = (lo + hi) >> 1;
    if ((tokens[mid].end | 0) <= a) lo = mid + 1; else hi = mid;
  }
  const out = [];
  let pos = a;
  for (let i = lo; i < tokens.length; i++) {
    const t = tokens[i];
    const ts = t.start | 0, te = t.end | 0;
    if (ts >= b) break;
    const s = Math.max(a, ts), en = Math.min(b, te);
    if (en <= s) continue;
    // 容错：正常不该有重叠 token，真出现了就跳过，否则同一段文字会画两遍
    if (s < pos) continue;
    if (s > pos) out.push(svnHlEsc(text.slice(pos, s)));
    const cls = "tk-" + String(t.type || "unknown").replace(/\./g, "-");
    out.push('<span class="' + cls + '">' + svnHlEsc(text.slice(s, en)) + "</span>");
    pos = en;
  }
  if (pos < b) out.push(svnHlEsc(text.slice(pos, b)));
  return out.join("");
}

/**
 * 一个格子：行号 + 内容。text 为 null 表示这一边没有对应行（留空）。
 *
 * 内容要单独包一层 `.tx`：直接把文本节点丢进容器里，它就是个匿名 flex/grid
 * item，长 token 不收缩、不换行，会顶出格子盖到隔壁列上（实测踩过）。
 *
 * `html` 给了就用它（已经染好色的一行），否则维持纯文本 —— 二进制、没插件、
 * 超限、请求失败都是后者，保证对比本身永远画得出来。
 */
function svnDiffCell(no, text, kind, side, html) {
  const cell = document.createElement("div");
  // 类名必须带 `diff-` 前缀：以前上下文行就叫 `ctx`，正好撞上全局的右键菜单
  // 样式 `.ctx { position: fixed; min-width: 178px }` —— 整页的上下文行全变成
  // 固定定位叠在同一个点上，看着就像"左右两列糊成一团"。
  cell.className = "diff-cell diff-" + kind + (side ? " diff-" + side : "");
  const ln = document.createElement("span");
  ln.className = "ln";
  ln.textContent = (text === null || text === undefined) ? "" : (no ? String(no) : "");
  const tx = document.createElement("span");
  tx.className = "tx";
  if (text !== null && text !== undefined) {
    if (html !== null && html !== undefined) tx.innerHTML = html;
    else tx.textContent = text;
  }
  cell.appendChild(ln);
  cell.appendChild(tx);
  return cell;
}

/**
 * 在**编辑区**打开某个文件「仓库 BASE ↔ 本地」的差异（左右两列）。
 *
 * 不是弹窗：编辑区里那块由内核的 `openEditorDiff(...)` 提供（跟 Logcat 同一
 * 套路 —— 占住编辑区、标签条上有一个格子、点别的文件标签就切走）。这里只负责
 * 把内容画进去，`paint(box)` 会在每次切回这个标签时重画一遍。
 *
 * 左边 = 仓库里的版本，右边 = 本地文件；红底是被删掉的行，绿底是新加的行。
 * 行号各走各的：左列是旧行号，右列是新行号。
 * 中缝的 ◀ 只还原那一处改动（不是整个文件）—— 对应 VSCode diff 里那排箭头。
 */
function svnShowDiff(path, name) {
  if (typeof openEditorDiff !== "function") {
    toast("当前页面还是旧版本，刷新一下才能用编辑器内对比", true);
    return;
  }
  // 左边那棵树跟着标一下「现在看的是这个文件」——点行、右键、跨文件跳转
  // 三条路都经过这里，所以放在这一个点上，别在三处各写一遍。
  svnMarkCur(path);
  openEditorDiff("对比 " + (name || baseName(path)), (box) => paintSvnDiff(box, path));
}

/** 改动块中缝的 ◀：把这一处恢复成仓库里的版本。
 *  `panel` 是对比面板整体（固定条 + 滚动层），重画时原样传回去即可。 */
function svnRevertArrow(path, panel, hi, bi) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "diff-arrow";
  b.textContent = "◀";
  b.title = "还原这一处（用仓库版本替换本地这几行）";
  b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    b.disabled = true;
    // quiet：失败时下面自己给提示，免得同一个错误被 toast 两遍
    const res = await svnCall("revert_hunk", { path, hunk: hi, block: bi }, null, true);
    if (!res) { b.disabled = false; return; }              // 请求本身没发出去
    if (!res.ok) {
      toast(res.summary || res.content || "还原失败", true);
      b.disabled = false;
      return;
    }
    toast(res.summary || "已还原");
    // 重新拉一遍，序号也跟着更新。只有整个文件都还原干净了才去刷左侧列表 ——
    // 这个工作区下有 55 个副本，一次 status 要扫两秒，每点一个箭头都刷太亏。
    const still = await paintSvnDiff(panel, path);
    if (!still && typeof refreshSvn === "function") refreshSvn();
  });
  return b;
}

/**
 * 左侧列表里**所有能出 diff 的文件**，按树的先序排。
 *
 * 用途：对比面板的「↓ 下一处」走到本文件末尾时，顺着这张表去下一个文件，
 * 而不是回环到本文件第一处（以前的行为 —— 得先自己找到左边那行点进去，
 * 一份改动散在七八个文件里时，一晚上都在来回找）。
 *
 * 跳过 `unversioned`：这些文件没有 BASE，`diff_text` 必然返回空，
 * 跳过去只会看到一个空面板。`unvdir` 同理（它下面的全是未纳入版本的），
 * 而且它的 children 还在懒加载缓存里，这儿拿不到。
 */
function svnAllDiffFiles() {
  const out = [];
  (function walk(nodes) {
    for (const n of (nodes || [])) {
      if (n.kind === "file") {
        if (n.status !== "unversioned") out.push(n);
        continue;
      }
      if (n.kind === "info" || n.kind === "unvdir") continue;
      // 目录：顺着合并链的末级往下走（链中间的目录只有这一个孩子）
      walk(svnChain(n).leaf.children);
    }
  })(SVN_TREE);
  return out;
}

/**
 * 对比面板顶部的「↑ 上一处 / ↓ 下一处」。
 *
 * 为什么需要：diff 现在是**全文上下文**（后端带 `-U 999999`），一个 900 行的
 * 文件几乎整篇都摊在面板里，改动块可能就三处、散得很开。没有导航的话只能
 * 一路滚下去用眼睛找红绿。
 *
 * **跨文件**：走到本文件最后一处再按「下一处」，顺着左侧列表的顺序打开
 * 下一个有改动的文件、落在它的**首处**（向上则是上一个文件的**末处**）。
 * 真正的"读下一个文件"这一步交给 `paintSvnDiff` 重画，这里只把「落在哪一头」
 * 通过 `SVN_NAV_PEND` 传过去。这样不用在这里重复一套渲染逻辑 ——
 * 对比面板本来就是"换文件就重调一次 openEditorDiff"（见 svnShowDiff）。
 *
 * 只有一个文件、或邻居都跳不过去时，退回**回环**：在最后一处再按「下一处」
 * 回第一处 —— 比到头变灰更省事（不用判断"是不是到底了"）。
 * `cur` 从 -1 开始，所以第一次按 ↓ 落在第 1 处。
 *
 * 落点用 `block:"center"`：`nearest` 在目标已经贴边时只挪一点点，看着像没反应。
 *
 * `ctx` = { path, files }：`path` 是当前文件，`files` 是 `svnAllDiffFiles()`
 * 的结果。没传就退化成老的单文件回环（插件被单独调用时不会炸）。
 */
function svnDiffNav(anchors, bar, ctx) {
  bar = bar || document.createElement("div");
  bar.className = "diff-nav";
  bar.textContent = "";        // 重画时把旧的按钮/计数清掉，容器本身复用
  const cnt = document.createElement("span");
  const path = (ctx && ctx.path) || "";
  const files = (ctx && ctx.files) || [];
  const idxOf = (p) => files.findIndex(
    (f) => svnNormPath(f.fsPath) === svnNormPath(p));
  let cur = -1;

  // 跨文件跳进来：新文件画完之后要落在首处（向下）还是末处（向上）
  let pend = 0;
  if (SVN_NAV_PEND && svnNormPath(SVN_NAV_PEND.path) === svnNormPath(path)) {
    pend = SVN_NAV_PEND.dir;
    SVN_NAV_PEND = null;
  }

  const focus = (i) => {
    cur = i;
    const a = anchors[cur];
    // 同时描左右两格：只描左边的话，右边那半截改动看着像"没选中"
    for (const x of anchors) {
      x.l.classList.remove("diff-focus");
      x.r.classList.remove("diff-focus");
    }
    a.l.classList.add("diff-focus");
    a.r.classList.add("diff-focus");
    // 两栏各滚一次（scrollIntoView 只管一个滚动容器，另一个会留在原地错行）
    svnScrollToRow(a);
    cnt.textContent = "第 " + (cur + 1) + " / " + anchors.length + " 处";
  };

  /** 顺着 delta 方向打开邻居文件。成了返回 true（本文件的导航到此为止）。 */
  const goFile = (delta) => {
    if (!path || files.length < 2) return false;
    const start = idxOf(path);
    if (start < 0) return false;
    const n = files.length;
    const next = files[((start + delta) % n + n) % n];
    if (!next || svnNormPath(next.fsPath) === svnNormPath(path)) return false;
    // hops：邻居一个个看过去的上限。撞上「只有属性变更 / 二进制」这种
    // 0 处改动的文件时，paintSvnDiff 会顺着同一个方向再跳一次 ——
    // 不给上限的话，一圈文件全 0 处就会无限打开下去。
    SVN_NAV_PEND = { path: next.fsPath, dir: delta > 0 ? 1 : -1, hops: n - 1 };
    svnShowDiff(next.fsPath, next.name);
    return true;
  };

  const jump = (delta) => {
    if (!anchors.length) return;
    const nx = cur + delta;
    // 还在本文件的范围内
    if (nx >= 0 && nx < anchors.length) { focus(nx); return; }
    // 越界：先去下一个文件（有的话）
    if (goFile(delta)) return;
    // 没有邻居（只有这一个文件 / 找不到自己）→ 回环
    focus((nx + anchors.length) % anchors.length);
  };

  const mkBtn = (label, tip, delta) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = tip;
    b.addEventListener("click", () => jump(delta));
    return b;
  };

  cnt.textContent = "共 " + anchors.length + " 处";
  bar.appendChild(mkBtn("↑ 上一处",
    "跳到上一处改动（本文件开头会跳到上一个有改动的文件）", -1));
  bar.appendChild(mkBtn("↓ 下一处",
    "跳到下一处改动（本文件末尾会跳到下一个有改动的文件）", 1));
  bar.appendChild(cnt);
  // 跨文件跳过来：落在这个文件的首处（向下）或末处（向上）
  if (pend) focus(pend > 0 ? 0 : anchors.length - 1);
  return bar;
}

/**
 * 两栏的纵向同步。
 *
 * 为什么横向不一起同步、纵向必须同步：左右是各自独立的滚动容器（用户要的就是
 * 「各自横向滚」），但纵向一旦不同步，同一处改动左右就错行，diff 直接失去意义。
 * 所以横向各滚各的，纵向互相跟随 —— 和 VSCode 的并排比较一致。
 *
 * `lock` 防回环：A 写 B 的 scrollTop 会触发 B 的 scroll 事件，B 再写回 A，
 * 两边互相拽着跳。用 rAF 合并（一次拖动会连发几十个 scroll），且同一帧内只认
 * 第一个事件。
 */
function svnDiffSyncCols(lp, rp, gc) {
  let lock = 0;
  // 中缝那条竖带不滚（它压根没有滚动条），箭头位置只能靠平移跟上左栏。
  // 放在这个 rAF 里一起做：每帧只写一次 transform，和竖向同步同频。
  const follow = (top) => {
    if (gc) gc.style.transform = "translateY(" + (-top) + "px)";
  };
  const bind = (a, b) => {
    a.addEventListener("scroll", () => {
      if (lock) return;
      lock = 1;
      requestAnimationFrame(() => {
        // 差值超过 1px 才写：小于 1px 的抖动（子像素）写回去会自己颤
        if (Math.abs(b.scrollTop - a.scrollTop) > 1) b.scrollTop = a.scrollTop;
        follow(a.scrollTop);
        lock = 0;
      });
    });
  };
  bind(lp, rp);
  bind(rp, lp);
}

/**
 * 拖动条：改左栏宽度（CSS 变量 `--diff-l`），双击恢复各占一半。
 *
 * 用 pointer 事件 + setPointerCapture：鼠标拖出条外、甚至拖出窗口时也能继续
 * 收到 move，松手不会卡在「拖拽中」。宽度存 localStorage —— 调好的分栏比例
 * 下次打开同一文件还在。
 */
const SVN_DIFF_W_KEY = "svnDiffLeftW";

function svnDiffDrag(bar, body, lp) {
  const clampW = (w) => {
    // 下限 120px：再窄行号都放不下；上限留 260px 给右栏，别把右栏拖没了
    const max = Math.max(160, body.clientWidth - 260);
    return Math.max(120, Math.min(Math.round(w), max));
  };
  const apply = (w) => {
    const x = clampW(w);
    body.style.setProperty("--diff-l", x + "px");
    return x;
  };

  const saved = parseInt(localStorage.getItem(SVN_DIFF_W_KEY) || "", 10);
  // clientWidth 为 0 说明面板还没布局（隐藏着）—— 这时候换算宽度会得出
  // 「上限 160」把用户存的比例压扁，所以等它可见再说，反正拖动时还会重算。
  if (saved > 0 && body.clientWidth > 300) apply(saved);

  bar.addEventListener("dblclick", () => {
    body.style.removeProperty("--diff-l");
    localStorage.removeItem(SVN_DIFF_W_KEY);
  });

  bar.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    const startX = ev.clientX;
    const startW = lp.getBoundingClientRect().width;
    let w = startW;
    bar.classList.add("dragging");
    document.body.classList.add("diff-resizing");
    try { bar.setPointerCapture(ev.pointerId); } catch (_) { /* 老浏览器没有也行 */ }

    const move = (e2) => { w = apply(startW + (e2.clientX - startX)); };
    const up = () => {
      bar.classList.remove("dragging");
      document.body.classList.remove("diff-resizing");
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
      localStorage.setItem(SVN_DIFF_W_KEY, String(w));
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
}

/**
 * 把某一行滚到两栏的正中间（「↑/↓ 跳改动」的落点）。
 *
 * 不能用 `scrollIntoView` 了：现在有两个滚动容器，它只会去动「离得最近的那个」，
 * 另一栏留在原地 → 跳过去就错行。这里自己算，两栏各写一次 scrollTop。
 *
 * 用 rect 相减而不是 offsetTop：`offsetTop` 依赖 offsetParent，分栏结构里
 * 中途换一层容器就会算错；rect 差在滚动时恒定（两元素一起位移），更稳。
 */
function svnScrollToRow(a) {
  const box = a && a.lrow;
  if (!box) return;
  const col = box.parentElement;
  if (!col) return;
  const top = box.getBoundingClientRect().top - col.getBoundingClientRect().top;
  const h = box.offsetHeight;
  for (const p of (a.panes || [])) {
    const want = top + h / 2 - p.clientHeight / 2;
    p.scrollTop = Math.max(0, Math.min(want, p.scrollHeight - p.clientHeight));
  }
}

/**
 * 对比面板拆成三层：**顶部常驻导航条** + **说明条** + **左右两栏**（中间拖动条）。
 *
 * 以前导航条是正文里的一个块（后来加过 sticky）。sticky 要求"滚动容器就是
 * 父元素、中间不能有裁切"，换一层容器就会失效 —— 而它存在的意义恰恰是
 * "滚到哪儿都在"。索性别依赖 sticky：外层 `.ed-diff` 改成不滚，上面单独
 * 一条导航栏，下面那个 `.diff-scroll` 才是内容区。
 *
 * `.diff-scroll` 自己也不滚（`overflow:hidden`），它只是把「说明条」和
 * 「两栏本体」竖向排开；真正滚动的是 `.diff-pane-l` / `.diff-pane-r`。
 * 这样左右才能**各自横向滚**（用户要的），而中间那条 6px 的 `.diff-bar`
 * 可以拖动改左右宽度。
 *
 * 所有节点都常驻（重画只清两列 `.diff-col` 里的行），滚动位置和拖好的宽度
 * 因此能保住。
 */
function svnDiffParts(host) {
  let nav = host.querySelector(":scope > .diff-nav");
  let scroll = host.querySelector(":scope > .diff-scroll");
  if (!nav || !scroll) {
    host.textContent = "";
    host.classList.add("diff-host");
    nav = document.createElement("div");
    nav.className = "diff-nav";
    scroll = document.createElement("div");
    scroll.className = "diff-scroll";
    host.appendChild(nav);
    host.appendChild(scroll);
  }
  let head = scroll.querySelector(":scope > .diff-head");
  let body = scroll.querySelector(":scope > .diff-body");
  if (!head || !body) {
    scroll.textContent = "";
    head = document.createElement("div");
    head.className = "diff-head";
    body = document.createElement("div");
    body.className = "diff-body";

    const lp = document.createElement("div");
    lp.className = "diff-pane diff-pane-l";
    const lc = document.createElement("div");
    lc.className = "diff-col";
    lp.appendChild(lc);

    // 中缝竖带：夹在左栏和拖动条之间，专门放「◀ 还原这一处」。
    // 为什么单独立一条，而不是塞进左栏的行里：行内那块只能靠 sticky 贴右沿，
    // 而行宽是 max-content（一行几千 px），元素在正常流里早就跑到可视区外，
    // sticky 拉不回来 —— 表现就是箭头整排不见；另外每行插一块不透明背景，
    // 还会在左栏右沿留一条色带。竖带不参与横向滚动，箭头永远在原位，
    // 竖向靠 .diff-gapcol 的 translateY 跟随左栏（见 svnDiffSyncCols）。
    const gp = document.createElement("div");
    gp.className = "diff-gapbar";
    const gc = document.createElement("div");
    gc.className = "diff-gapcol";
    gp.appendChild(gc);

    const bar = document.createElement("div");
    bar.className = "diff-bar";
    bar.title = "拖动调整左右宽度（双击恢复各占一半）";

    const rp = document.createElement("div");
    rp.className = "diff-pane diff-pane-r";
    const rc = document.createElement("div");
    rc.className = "diff-col";
    rp.appendChild(rc);

    body.appendChild(lp);
    body.appendChild(gp);
    body.appendChild(bar);
    body.appendChild(rp);
    scroll.appendChild(head);
    scroll.appendChild(body);
    // 监听只绑一次（节点是复用的，重画不重建）
    svnDiffSyncCols(lp, rp, gc);
    svnDiffDrag(bar, body, lp);
  }
  const q = (sel) => scroll.querySelector(":scope > .diff-body " + sel);
  return {
    nav, scroll, head,
    lp: q("> .diff-pane-l"),
    rp: q("> .diff-pane-r"),
    lc: q("> .diff-pane-l > .diff-col"),
    rc: q("> .diff-pane-r > .diff-col"),
    gp: q("> .diff-gapbar"),
    gc: q("> .diff-gapbar > .diff-gapcol"),
  };
}

/**
 * 拉一次 diff 并画进 panel（还原一处之后会再调一次）。
 * 返回「还有没有差异」—— 调用方靠它决定要不要重刷左侧列表。
 *
 * `panel` 是内核给的那块 div（#edDiffView）。这里把它拆成「固定条 + 滚动层」，
 * 所以内容一律挂到 `scroll` 上，不能直接写 panel —— 否则会把导航条一起清掉。
 */
async function paintSvnDiff(panel, path) {
  // lc / rc = 左右两列的容器（.diff-col），行一律挂到它们上面；
  // lp / rp = 两个滚动区，换文件时要把它们的滚动位置归零。
  const { nav, scroll, head, lc, rc, lp, rp, gc } = svnDiffParts(panel);

  /**
   * 出不了 diff 时（二进制 / 没差异 / 读取失败）统一走这里：
   * 清空两栏、把话写在顶部说明条里。**不能**再写 scroll.textContent ——
   * 那会把分栏结构（.diff-body）连同拖动条一起抹掉，下次进来得重建，
   * 调好的宽度（--diff-l）和滚动位置全白丢。
   */
  const showMsg = (t) => {
    lc.textContent = "";
    rc.textContent = "";
    if (gc) gc.textContent = "";
    head.textContent = t;
    nav.textContent = "";
    nav.classList.add("hidden");
  };
  // 跨文件跳转的接力：这一趟是因为「上一个文件的 ↓/↑ 越界」才打开的。
  // 记一份快照 —— svnDiffNav 那边也会看一眼并把它消费掉（正常有改动的情况）。
  const pend = (SVN_NAV_PEND
                && svnNormPath(SVN_NAV_PEND.path) === svnNormPath(path))
    ? { path: SVN_NAV_PEND.path, dir: SVN_NAV_PEND.dir, hops: SVN_NAV_PEND.hops }
    : null;

  /**
   * 这个文件没有可跳的改动（二进制 / 只有属性变更 / 取不到 diff）时，
   * 顺着同一个方向**继续**找下一个文件。
   *
   * 为什么要有：svnDiffNav 只知道"有 anchors 的文件"，它把用户送到邻居文件后
   * 才发现那个文件是二进制的。没有这一步的话，用户按一次「下一处」会得到一个
   * 写着「无法显示文本差异」的空面板，还得自己再按一次。
   * `hops` 是圈的圈数上限，防止一圈文件全 0 处时无限打开下去。
   */
  const relay = () => {
    if (!pend || pend.hops <= 0) { SVN_NAV_PEND = null; return false; }
    const files = svnAllDiffFiles();
    const n = files.length;
    const start = files.findIndex(
      (f) => svnNormPath(f.fsPath) === svnNormPath(path));
    if (n < 2 || start < 0) { SVN_NAV_PEND = null; return false; }
    const next = files[((start + pend.dir) % n + n) % n];
    if (!next || svnNormPath(next.fsPath) === svnNormPath(path)) {
      SVN_NAV_PEND = null;
      return false;
    }
    SVN_NAV_PEND = { path: next.fsPath, dir: pend.dir, hops: pend.hops - 1 };
    svnShowDiff(next.fsPath, next.name);
    return true;
  };

  try {
    const r = await api("/api/plugin/svn_tools/diff_text", { path });
    const res = r.result || {};
    if (!res.ok) {
      if (relay()) return false;
      showMsg(res.content || "读取失败");
      nav.textContent = "";
      nav.classList.add("hidden");
      return false;
    }

    const d = res.data || {};
    const files = d.files || [];
    if (!files.length) {
      if (relay()) return false;
      showMsg("与仓库里的版本没有差异");
      nav.textContent = "";
      nav.classList.add("hidden");
      return false;
    }

    // 两栏各自清空：分栏结构（左右 + 拖动条）是常驻的，重画只换两列里的行。
    // 中缝竖带里是绝对定位的箭头，同样得清 —— 不清就会叠在上面重画一层。
    lc.textContent = "";
    rc.textContent = "";
    if (gc) gc.textContent = "";
    // 换了文件就回到顶部。两栏是复用的，位置会留在上一个文件上：新文件一长，
    // 进来就落在中间。同一个文件重画（点 ◀ 还原之后）保留原位置，别把人弹回开头。
    // 横向也归零：上一份文件左侧滚到了 3000px，新文件一进来就是一片空白。
    if (panel._svnPath !== path) {
      panel._svnPath = path;
      lp.scrollTop = 0; rp.scrollTop = 0;
      lp.scrollLeft = 0; rp.scrollLeft = 0;
    }
    head.textContent = res.summary
      + "　（左=仓库版本，右=本地；两栏可各自横向滚，中间拖动条调宽度；"
      + "点中缝 ◀ 还原那一处，↑↓ 在改动之间跳，到头会跳到下一个文件）";

    // 每处改动的落点：左右两个格子 + 它们所在的那一行 + 两个滚动区。
    // 现在 diff 带全文上下文（后端 -U 999999），一个文件几百行、改动散在各处，
    // 光靠滚轮找红绿块太费劲 —— 这个数组就是「下一处」按钮的落点表。
    const anchors = [];
    // 中缝 ◀ 的待定位清单：{ el, row }，画完再统一算 top。
    const arrowJobs = [];
    // 共享同一个数组：每行都 new 一个 [lp, rp] 会在几百行的文件里制造几百份垃圾
    const panes = [lp, rp];
    // 世代号：等染色结果期间用户可能切到别的文件，这一批 DOM 不许再贴上去。
    // 没有它的话，慢文件的颜色回来时会画到新文件的面板上（内容串台）。
    const gen = (panel._svnGen || 0) + 1;
    panel._svnGen = gen;

    /**
     * 左右各放一份的「整行标题」（文件头 / hunk 头 / 提示行）。
     * 右栏那份加 .diff-mirror 隐藏，只用来占高度 —— 两栏是两棵独立的树，
     * 少一份的话左栏有文件头、右栏没有，后面所有行就整体错开一截。
     */
    const pushFull = (el) => {
      lc.appendChild(el);
      const m = el.cloneNode(true);
      m.classList.add("diff-mirror");
      rc.appendChild(m);
      // 返回左右两份的引用：普通标题行用不上，但折叠条要各自挂点击、
      // 还要在展开时拿到自己的 nextSibling 当插入点。
      return [el, m];
    };

    /**
     * 一行 = 左栏 [格子] + 右栏 [格子]。
     * 两栏各推一个**等高**的 .diff-row，逐行镜像就天然对齐：不用算偏移，
     * 也不怕某侧内容长短不一（行高由 CSS 的 --diff-line 固定）。
     * 中缝的 ◀ 不再跟着行挂 —— 它住在独立的 .diff-gapbar 里（见上）。
     */
    const mkRow = (left, right) => {
      const lr = document.createElement("div");
      lr.className = "diff-row";
      lr.appendChild(left);
      const rr = document.createElement("div");
      rr.className = "diff-row";
      rr.appendChild(right);
      return [lr, rr];
    };
    const pushRow = (left, right) => {
      const [lr, rr] = mkRow(left, right);
      lc.appendChild(lr);
      rc.appendChild(rr);
      return [lr, rr];
    };

    // 连续未改动段里，头尾各留几行不折。留 0 行的话改动块悬在中间，没上下文
    // 没法判断；原来只留 3 行，实测改动块上下几乎看不到它落在哪个函数 / 哪个
    // 条件分支里（截图里只剩相邻 3 行），所以放宽到 20 行 —— 大致够覆盖一个
    // 常规函数体的开口。代价是折叠条出现得晚，判据见 flushCtx()。
    const CTX_KEEP = 20;

    /**
     * 一条「⋯ 未改动 N 行，点击展开 ⋯」。
     *
     * 左右**各一条**：右栏那条不能做成隐藏 mirror —— 右栏是独立的一棵树，
     * 少一行两栏从此整体错开。
     *
     * 点开就地把折掉的行**插回来**：数据一直在 `rows` 里，只是没建 DOM，
     * 所以行号、染色字符区间、块序号（后端按第几块还原）全都没变过。
     * 这也是折叠必须发生在输出阶段、不能缩短 `allRows` 的原因。
     *
     * `tkBox.v` 是染色结果的「稍后填」盒子：展开可能发生在染色回来之前
     * （那时铺纯文本并登记进 `fill`）或之后（直接铺带色的行）。
     */
    const pushFoldBar = (fd, rows, gBase, ljoin, rjoin, fill, tkBox) => {
      const mk = () => {
        const el = document.createElement("div");
        el.className = "diff-fold";
        el.textContent = "⋯ 未改动 " + fd.n + " 行，点击展开 ⋯";
        el.title = "折叠了 " + fd.n + " 行未改动内容；行号和差异本身都没变，点开插回来";
        return el;
      };
      const [lrow, rrow] = pushRow(mk(), mk());
      const open = (ev) => {
        if (ev) { ev.preventDefault(); ev.stopPropagation(); }
        if (!lrow.parentNode) return;              // 已经展开过（左右任一条点过）
        // 插入点取折叠条自己的下一个兄弟：每行都插在同一个参照物之前，
        // 顺序天然从旧到新，不用自己算偏移。
        const refL = lrow.nextSibling, refR = rrow.nextSibling;
        const tk = tkBox.v;
        for (let k = fd.from; k < fd.to; k++) {
          const row = rows[k];
          const ls = ljoin.spans[gBase + k], rs = rjoin.spans[gBase + k];
          const left = svnDiffCell(row.ln, row.l, row.lt, "",
            tk && tk[0] ? svnLineHtml(ljoin.text, ls[0], ls[1], tk[0]) : null);
          const right = svnDiffCell(row.rn, row.r, row.rt, "right",
            tk && tk[1] ? svnLineHtml(rjoin.text, rs[0], rs[1], tk[1]) : null);
          const [lr2, rr2] = mkRow(left, right);
          lc.insertBefore(lr2, refL);
          rc.insertBefore(rr2, refR);
          // 染色还在路上：把这批也登记进去，回来时一起补色
          if (!tk && !fill.done) {
            fill.rows.push({ ls: ls, rs: rs, ltx: left.lastChild, rtx: right.lastChild });
          }
        }
        lrow.parentNode.removeChild(lrow);
        rrow.parentNode.removeChild(rrow);
      };
      // 点哪一侧的折叠条都行：两栏各自独立滚动，用户可能在右栏看
      lrow.firstChild.addEventListener("click", open);
      rrow.firstChild.addEventListener("click", open);
    };

    for (const f of files) {
      const fn = document.createElement("div");
      fn.className = "diff-file";
      fn.textContent = f.file;
      fn.title = f.file;      // 路径可能被省略号截掉，鼠标停一下能看全
      pushFull(fn);

      // 二进制文件 svn 只给一句 "Cannot display"，以前那句被丢了，
      // 界面上就是"文件名 + 一片空白"，看着像没差异。
      if (f.binary) {
        const bn = document.createElement("div");
        bn.className = "diff-hunk";
        bn.textContent = "（二进制文件，无法显示文本差异）";
        pushFull(bn);
        continue;
      }

      // 先把整个文件的 hunk 摊成两列行数组，再把左右两侧**各自拼成整段**
      // 去染一次色。必须跨 hunk 一起拼：token 是相对整段文本的字符偏移，
      // 一个 hunk 发一次请求既慢又会让偏移算错（每段都从 0 重新开始）。
      const hunks = f.hunks.map((h, i) => ({ h: h, hi: i, rows: svnPairHunk(h) }));
      const allRows = [];
      for (const it of hunks) {
        for (const r of it.rows) allRows.push(r);
      }
      const ljoin = svnJoinSide(allRows, "l");
      const rjoin = svnJoinSide(allRows, "r");

      // 每行要往哪一格补什么颜色 —— 染色回来逐条应用，不用再翻 DOM 找
      const fill = { rows: [], done: false };
      // 染色结果（左右两份 token）。先把盒子建好：折叠条展开时要读它，
      // 而那时染色可能还没回来（读到 undefined → 铺纯文本，随后一起补色）。
      const tkBox = { v: null };
      // 先铺纯文本：染色是两趟网络请求（整文件拼段送出去），以前先 await 再
      // 建 DOM，900 行的文件点开就是一片空白。现在立刻出现内容，颜色稍后补。
      let gi = 0;                    // 整段里的行序，和 allRows 严格对齐

      /** 铺一行 + 登记它的染色区间（tk 为 null 就是纯文本）。 */
      const emitRow = (row, ls, rs, tk) => {
        const left = svnDiffCell(row.ln, row.l, row.lt, "",
          tk && tk[0] ? svnLineHtml(ljoin.text, ls[0], ls[1], tk[0]) : null);
        const right = svnDiffCell(row.rn, row.r, row.rt, "right",
          tk && tk[1] ? svnLineHtml(rjoin.text, rs[0], rs[1], tk[1]) : null);
        if (!tk && !fill.done) {
          fill.rows.push({ ls: ls, rs: rs, ltx: left.lastChild, rtx: right.lastChild });
        }
        const [lr] = pushRow(left, right);
        return { left: left, right: right, lrow: lr };
      };

      for (const it of hunks) {
        const h = it.h;
        const hd = document.createElement("div");
        hd.className = "diff-hunk";
        hd.textContent = h.header;
        hd.title = h.header;
        pushFull(hd);
        let bi = 0;                                   // 改动块序号：后端按它定位
        // 本 hunk 在整段里的起始行序：折叠展开时要按它取字符区间
        const gBase = gi;
        // 连续未改动行先攒着不铺：只有等这段结束才知道该折中间哪几行
        const ctx = [];
        const flushCtx = () => {
          const n = ctx.length;
          if (!n) return;
          // 折中间、留头尾各 CTX_KEEP 行：全留则全文上下文把改动冲得找不着，
          // 全折则改动上面没有函数名 / 条件分支，等于没上下文。
          // 20 是经验值（见 CTX_KEEP 注释）：超过 41 行才折，否则整段照铺 ——
          // 只折一两行反而比不折更乱。
          const fold = n > CTX_KEEP * 2 + 1;
          const from = fold ? CTX_KEEP : n;
          const to = fold ? n - CTX_KEEP : n;
          for (let k = 0; k < from; k++) {
            const idx = ctx[k];
            emitRow(it.rows[idx], ljoin.spans[gBase + idx], rjoin.spans[gBase + idx], null);
          }
          if (fold) {
            pushFoldBar({ from: ctx[CTX_KEEP], to: ctx[n - CTX_KEEP], n: n - CTX_KEEP * 2 },
              it.rows, gBase, ljoin, rjoin, fill, tkBox);
            for (let k = to; k < n; k++) {
              const idx = ctx[k];
              emitRow(it.rows[idx], ljoin.spans[gBase + idx], rjoin.spans[gBase + idx], null);
            }
          }
          ctx.length = 0;
        };
        for (const row of it.rows) {
          const myBlock = row.start ? bi++ : -1;
          const ls = ljoin.spans[gi], rs = rjoin.spans[gi];
          const myGi = gi;
          gi++;
          // 折叠只跳过**建 DOM**，gi / bi 照常自增 —— 后端还原块序号、
          // 染色字符偏移都按完整 allRows 算，缩了就对不上。
          if (myBlock < 0 && row.lt === "ctx" && row.rt === "ctx") {
            ctx.push(myGi - gBase);
            continue;
          }
          // 上下文段中断了（碰到改动行）：先把这段该折的折掉
          flushCtx();
          const m = emitRow(row, ls, rs, null);
          // 被截断的那个 hunk 不给箭头：后端按完整 diff 算块序号，
          // 截断后界面上看到的块可能只是它的一半，点下去会改错行。
          // 箭头先攥在手里，整篇画完再统一算 top（见循环之后那段）。
          if (myBlock >= 0 && !h.cut) {
            arrowJobs.push({ el: svnRevertArrow(path, panel, it.hi, myBlock), row: m.lrow });
          }
          // panes 一起存：跳转时两栏要各滚一次，只滚一个就错行
          if (myBlock >= 0) anchors.push({ l: m.left, r: m.right, lrow: m.lrow, panes: panes });
        }
        flushCtx();
      }

      // 文本已经铺完，再等染色。左右并行；任一失败（或没有对应语言的染色
      // 插件）就是 null，那些格子保持纯文本 —— 颜色丢了不影响对比本身。
      const pair = await Promise.all([
        svnHighlight(path, ljoin.text),
        svnHighlight(path, rjoin.text),
      ]);
      if (panel._svnGen !== gen) return true;    // 已经切走，这批结果作废
      const ltk = pair[0], rtk = pair[1];
      tkBox.v = [ltk, rtk];
      fill.done = true;
      if (ltk || rtk) {
        for (const g of fill.rows) {
          if (ltk && g.ltx) g.ltx.innerHTML = svnLineHtml(ljoin.text, g.ls[0], g.ls[1], ltk);
          if (rtk && g.rtx) g.rtx.innerHTML = svnLineHtml(rjoin.text, g.rs[0], g.rs[1], rtk);
        }
      }
    }
    // 中缝 ◀ 的落点：竖带不参与滚动，所以 top 必须按左栏行的**实测**位置算。
    // 不能用「行号 × 19px + 头行数」推算：文件头 / hunk 头和代码行虽然都是
    // 单行，但字号、padding 不同，越往下偏得越多，箭头会指到隔壁改动上。
    if (gc && arrowJobs.length) {
      const base = lc.getBoundingClientRect().top;
      // 先一次性把需要的几何全读完，再统一写 style / 插 DOM。
      // 以前是「读一行 → append 一个箭头 → 读下一行」，append 之后那次
      // getBoundingClientRect 会强制同步重排，几十处改动就是几十次 reflow。
      // (19px 行高 - 18px 箭头) / 2 ≈ 0.5px：让箭头在行内垂直居中
      const tops = [];
      for (const j of arrowJobs) tops.push(j.row.getBoundingClientRect().top - base + 0.5);
      for (let i = 0; i < arrowJobs.length; i++) {
        arrowJobs[i].el.style.top = tops[i] + "px";
        gc.appendChild(arrowJobs[i].el);
      }
    }
    // 竖带的纵向偏移按左栏**当前**滚动位置刷一次。只在 scroll 事件里更新是不够的：
    // 同一个文件点 ◀ 还原后会重画，滚动位置被刻意保留（不弹回顶部），
    // 这时候不会触发 scroll，偏移量就停在上一次的值上，箭头整排错位。
    if (gc) gc.style.transform = "translateY(" + (-lp.scrollTop) + "px)";

    // 导航条常驻在底部滚动层**之外**，所以滚到哪儿都看得见；
    // 没有可跳的改动（纯二进制 / 只有属性变更）就整条收起来。
    // 把「当前文件 + 所有能出 diff 的文件」一起交给导航：本文件跳到头时
    // 它靠这张表换到下一个文件（见 svnDiffNav 的 goFile）。
    if (anchors.length) {
      svnDiffNav(anchors, nav, { path, files: svnAllDiffFiles() });
      nav.classList.remove("hidden");
      // 这次是「上一个文件的 ↑/↓ 越界」跳过来的：让左边那棵树跟着走到这一行
      // （连带展开祖先目录），否则面板换了文件、左边界面上看不出在哪儿。
      if (pend) svnRevealPath(path);
    } else {
      nav.textContent = "";
      nav.classList.add("hidden");
    }
    if (d.truncated) {
      const tip = document.createElement("div");
      tip.className = "diff-tip";
      tip.textContent = "改动太大，已截断 —— 完整差异请用命令行 svn diff 或「导出」";
      pushFull(tip);
    }
    return true;
  } catch (e) {
    showMsg("失败：" + e.message);
    return false;
  }
}

// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 这里是**唯一**一处注册（setters 管首次进入、onshow 管每次切回来）。
// 别再在 initSvnView 里补一遍 —— viewRegister 是覆盖赋值，两处写同一个钩子
// 只会让"到底谁在刷"说不清。
// 启动时先问一次工作区的工作副本清单（右键菜单的显示判断要用）。
// 未登录 / 还没有工作区时失败无所谓 —— 第一次右键或切工作区会再问。
svnLoadWcs();
// 再丢一次预热：**这是"打开即用"的关键**。
// 服务器起来 / 页面刷新之后立刻在后台把 status 扫一遍，等用户点 ⎇ 图标时
// `do_status` 命中的就是这份现成的快照 —— 面板直接出内容，不再等十几秒。
// 不 await、失败也无声（未登录时必然 401，等真正打开面板时自然会算）。
svnWarm();

viewRegister({
  view: "svn",
  setters: () => initSvnView(),
  // 每次打开都刷（不看「自动」开关）—— 以前这里走 maybeRefreshSvn()，
  // 它带 SVN_MIN_GAP 节流、且开关关着就直接返回，表现就是"点进来还是旧内容"。
  onshow: () => refreshSvnOnShow(),
});

// ---------------------------------------------------------------------------
// 右键菜单：目录上的三条 SVN 动作
// ---------------------------------------------------------------------------
// 以前写死在 40-tree.js 的目录菜单里（`item("SVN 更新（update）", () =>
// svnFolderUpdate(...))`），svn_tools 一禁用就是三发 ReferenceError ——
// 而且这三句让文件树被迫认识 SVN 这个概念。
//
// **范围收到这个目录**：大工程里整仓 update / commit 又慢，又容易把别人的改动
// 一起牵进来；只想动这一块时就该只动这一块。
// 合成一条「SVN ▸」：文件树右键本来就有新建 / 重命名 / 删除 / 终端…十几条，
// 再平铺四五条 SVN 会把常用的挤下去。而且这几个动作都是"对整个目录动手"，
// 收在一起也更容易看清作用范围。
menuRegister("tree.dir", "svn_tools", {
  label: "SVN ▸",
  order: 20,
  sep: true,
  // 在副本里、或者它下面挂着副本（工作区根这种）都显示（见 svnPathScope）
  when: (c) => !!svnPathScope(c && c.path),
  run: (c) => svnDirSubmenu(c.path),
});

// 文件上也要能提交：以前只有目录有 SVN 入口，于是「改了一个文件想提交」
// 得先跑到左边 SVN 面板、再在一堆改动里找回这一行。槽位由 40-tree.js 的
// `menuRender("tree.file", {path, name, isDir:false})` 提供。
menuRegister("tree.file", "svn_tools", {
  label: "SVN ▸",
  order: 20,
  sep: true,
  when: (c) => svnPathInWc(c && c.path),
  run: (c) => svnFileSubmenu(c.path, c.name),
});

/**
 * 「SVN ▸」二级菜单的公共外壳（目录和文件共用）。
 *
 * 用独立浮层而不是内核的 menuRender 平铺：内核那条路只支持一级，为这一个
 * 插件去改四个调用方不划算。
 *
 * 位置**贴主菜单右边**展开（真级联，而不是盖在主菜单上）：主菜单收起后
 * 是 display:none，量不到尺寸，所以用内核 hideCtx 记下的 `CTX_LAST_RECT`。
 *
 * build({item, sep}) 里只管点菜名，浮层的位置 / 关闭 / 级联都由这里处理 ——
 * 目录和文件的菜单项不同，但外壳逻辑一模一样，抽出来省一份容易走偏的复制。
 */
function svnSubmenu(titleText, build) {
  const box = document.createElement("div");
  box.className = "ctx";
  box.style.position = "fixed";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = titleText;
  box.appendChild(title);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeSub();
      // 双栏级联：主菜单还开着，而 stopPropagation 把 document 上的 hideCtx
      // 挡住了 —— 选中一项之后得自己把主菜单收起来
      if (typeof hideCtx === "function") hideCtx();
      fn();
    });
    box.appendChild(b);
  };
  const sep = () => box.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  if (typeof build === "function") build({ item, sep });

  document.body.appendChild(box);

  // 贴主菜单右侧；右边放不下就翻到左侧，上下越界再挪回来。
  // base 为空（比如 old 内核没记 rect）时退回原来的"略微右移"。
  const base = (typeof CTX_LAST_RECT !== "undefined" && CTX_LAST_RECT) || null;
  const l = parseFloat(ctx.style.left) || 120;
  const t = parseFloat(ctx.style.top) || 120;
  const r = box.getBoundingClientRect();
  let left = base ? (base.right + 2) : (l + 6);
  if (left + r.width > window.innerWidth - 8) {
    left = Math.max(8, (base ? base.left : l) - r.width - 2);
  }
  const top = base ? base.top : t;
  box.style.left = left + "px";
  box.style.top = Math.max(8, Math.min(top, window.innerHeight - r.height - 8)) + "px";

  function closeSub() {
    box.remove();
    if (typeof ctxSubClosed === "function") ctxSubClosed(box);
  }
  // 关闭时机交给内核统一管：换项 / 主菜单收起 / 点外面 / 滚动 / Esc / 移开。
  // 老内核没这套 API 时退回自己听，至少别让浮层永远挂着。
  if (typeof ctxSubOpen === "function") {
    ctxSubOpen(box, closeSub);
  } else {
    const onDoc = (ev) => { if (!box.contains(ev.target)) closeSub(); };
    setTimeout(() => document.addEventListener("click", onDoc, true), 0);
    document.addEventListener("scroll", closeSub, true);
    window.addEventListener("blur", closeSub);
  }
}

/**
 * 目录右键的 SVN ▸。
 *
 * 两种范围（见 svnPathScope）：
 *  · 这个目录自己是副本 → 更新 / 提交 / 清理 / switch 都只动这一层（老行为）
 *  · **它自己不是副本，但下面挂着副本**（工作区根 D:\workspace\demo 这种）→
 *    只有「更新 / 清理」有意义：这两个后端有多副本分支，不传 path 会把下面每个
 *    副本各跑一遍。「提交」不在这里出现 —— commit 必须落到**一个具体副本**
 *    （后端 do_commit 只对 items 跑一次 svn commit），拿工作区根去提交只会失败。
 *    文案里写明「整棵工作区，N 个副本」，否则会被当成"只动这一层"。
 */
function svnDirSubmenu(path) {
  const scope = svnPathScope(path);
  svnSubmenu("SVN", ({ item, sep }) => {
    if (scope === "ws") {
      const under = svnWcsUnder(path).length;
      const tag = under ? "（整棵工作区，" + under + " 个副本）" : "（整棵工作区）";
      item("更新" + tag, () => svnFolderUpdate(""));
      sep();
      item("清理" + tag, () => svnFolderCleanup(""));
      return;
    }
    item("更新（update）", () => svnFolderUpdate(path));
    item("提交…", () => svnCommitPanel(path));
    item("清理（cleanup）", () => svnFolderCleanup(path));
    sep();
    item("switch 到别的 URL…", () => svnSwitchDialog(path));
  });
}

/**
 * 文件右键的 SVN ▸。
 *
 * 三处都按**单个文件**调用（svnCommitPanel / svnShowDiff / svnRevert 都只吃
 * 一个路径）：面板会以这个文件为 scopeDir 取 status，实测能正确拿到该文件的
 * 改动用它自己的状态 —— 不用为此改面板签名。
 */
function svnFileSubmenu(path, name) {
  svnSubmenu("SVN", ({ item, sep }) => {
    item("提交…", () => svnCommitPanel(path));
    item("对比仓库版本…", () => svnShowDiff(path, name || baseName(path)));
    sep();
    item("撤销本地改动…", () => svnRevert(path));
  });
}

/** svn switch：把某个目录切到另一个 URL（同仓库的分支 / 标签）。 */
async function svnSwitchDialog(path) {
  const to = prompt(
    "svn switch\n\n把：" + path + "\n切换到哪个 URL？\n\n"
    + "支持 ^/branches/xxx 这种仓库内相对写法。", "");
  const target = String(to || "").trim();
  if (!target) return;
  const ok = await confirmBox("svn switch",
    "把「" + baseName(path) + "」切换到：\n\n" + target
    + "\n\n本地修改会尽量合并保留；合不上的会留下冲突（C）标记，需要手动解决。",
    "切换");
  if (!ok) return;
  $("svnSummary").textContent = "切换中…（大目录会慢一点）";
  const res = await svnCall("switch", { path, to: target }, null, true);
  if (!res) return;
  if (res.ok) {
    toast("已切换");
    $("svnSummary").textContent = res.summary || "已切换";
    $("svnSummary").title = res.content || "";
    refreshSvn();
  } else {
    $("svnSummary").textContent = res.summary || "switch 失败";
    $("svnSummary").title = res.content || "";
    toast(res.summary || "switch 失败", true);
  }
}
