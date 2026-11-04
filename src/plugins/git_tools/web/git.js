/* Git 本地改动视图（仿 svn_tools/web/svn.js）。
 *
 * ## 为什么 DOM 是**自己建**的
 *
 * 内核的左栏视图容器都写在 `index.html` 里（`#viewSvn` 那批）。
 * 本插件不去改 index.html，理由有两个：
 *   · 插件被禁用时那个空壳还留在页面里（这也是 svn 现在的已知瑕疵）；
 *   · 让 `#viewGit` 的归属跟着插件走，插件删了 DOM 也不残留。
 * 所以这里在加载时自己 createElement 一块，插到 `#viewSvn` 后面 ——
 * 位置对了，宽度/隐藏那套样式（`.view`）直接复用。
 *
 * ## 为什么全用 el() 而不是内核的 $()
 *
 * `tools/check_frontend.js` 会静态检查每个 `$("xxx")` 的 id 在 index.html
 * 里存不存在。这里的 id 全是运行时建出来的，用 `$()` 会被判成"指向空气"。
 * 用 `el()`（包一层 getElementById）绕开这条静态检查，语义没变。
 *
 * ## 和 svn 视图的差别
 *
 * 一个工作区里常常有**多个仓库**（AOSP 那种布局），所以列表顶部多一层
 * 「仓库头」：仓库名 + 分支 + ↑领先 ↓落后 + 改动条数。不改的话几十个仓库的
 * 改动混在一棵树里，分不清谁是谁的。
 */

//: 视图名，必须和 __init__.py 的 VIEWS[0].view 一致
const GIT_VIEW = "git";
//: 后端 HTTP 前缀
const GIT_API = "/api/plugin/git_tools/";

let GIT_TREE = [];          // 后端给的树（按仓库分层）
let GIT_REPOS = [];         // 每个仓库的 {root,name,branch,total,error}
let GIT_COUNTS = {};
let GIT_ROOTS = [];         // 扫到的仓库根
let GIT_REMOTE = false;     // 当前工作区是 ssh://
let GIT_OPEN = new Set();   // 展开着的目录（绝对路径）
let GIT_SEL = new Set();    // 勾选的条目（绝对路径）
let GIT_BUSY = false;
let GIT_AUTO = true;
let GIT_EPOCH = 0;          // 切工作区时作废在飞的请求
let GIT_LAST = 0;           // 上次刷新的时间戳（节流）
let GIT_ERR = "";           // 上次刷新失败的错误原文（空 = 上次成功）
let GIT_AGAIN = false;      // busy 期间又被要求刷一次，结束后来补
let GIT_SNAP = {};          // 后端这次给的是哪份快照（{at,age_ms,stale}）
//: 后端给了旧快照之后补拉真结果的那次定时。照 `svn.js` 的
//: `scheduleSvnStalePickup` 的做法，但**不做多轮重试** —— git 的 status
//: 比 svn 贵一个数量级（远程 AOSP 一次 20-30s），补拉两三次等于白占
//: SSH 通道；补不上的那份交给「自动」开关的后台刷新和用户点 ↻。
const GIT_STALE = { timer: null, pickupAt: 0 };

//: 「只显示未暂存的改动」：**纯前端**剪枝（后端照常返回全量）。
//: 这是看清单的偏好，不该为了换个视角重跑一次几十秒的 status ——
//: 以前这一项调过 `refreshGitWith({})` —— 空参数，跟普通刷新一模一样，
//: 点了没有任何变化。
const GIT_HIDE_KEY = "git.hideStaged";
let GIT_HIDE_STAGED = false;
try { GIT_HIDE_STAGED = localStorage.getItem(GIT_HIDE_KEY) === "1"; }
catch (e) { GIT_HIDE_STAGED = false; }

//: 两次自动刷新之间最短间隔（保存文件、切回前台都会触发）
const GIT_MIN_GAP = 1500;

//: 后端各类动作的时间上限写在 `__init__.py`，前端必须**比它宽** ——
//: api() 的默认超时是 15 秒（见 00-core.js:149-160），一旦先在前端 abort，
//: 后端那条 git 命令其实还在跑，用户看到的是「请求超时（15s），服务可能卡住了」，
//: 接着再点一次 ↻ 就是第二批活儿压上去。svn 那边同一个坑已经踩过
//: （svn.js:1485-1494），git 插件是漏掉的那个：`gitCall()` 以前第三参整块没传。
//:
//: status：远程多仓库走一条批量 SSH 脚本，后端上限 240s（`__init__.py:410-411`）；
//: 本地是逐仓库 90s/个（`:429`）。AOSP 那种几十个仓库的工作区必然超 15s。
const GIT_STATUS_TIMEOUT = 300000;
//: 网络动作（fetch / pull / push）：后端 NET_TIMEOUT = 600s（`__init__.py:139`）。
//: 大仓库 fetch 动辄几分钟，前端断掉只会让人误判成失败。
const GIT_NET_TIMEOUT = 660000;
//: 其余动作（diff / log / plan / add / 清理…）：后端 TIMEOUT = 120s（`:137`）。
const GIT_MISC_TIMEOUT = 120000;

function el(id) {
  return document.getElementById(id);
}

function gitEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* 建一次视图骨架。重复调用安全（已存在就直接返回）。 */
function gitEnsureView() {
  let box = el("viewGit");
  if (box) return box;
  box = document.createElement("div");
  box.className = "view hidden";
  box.id = "viewGit";
  box.innerHTML = ""
    + '<div class="view-bar">'
    + '  <span id="gitSummary" style="font-size:11px;color:var(--muted)">未加载</span>'
    + '  <span class="spacer"></span>'
    + '  <button class="tbtn mini" id="gitCommit" title="提交勾选的改动">⇧ 提交</button>'
    + '  <button class="tbtn mini" id="gitFetch" title="fetch（只更新远端引用，不动工作区）">⇣</button>'
    + '  <button class="tbtn mini" id="gitMaintain" title="维护：扫描 / 清理未跟踪 / 撤销全部">维护…</button>'
    + '  <button class="tbtn mini" id="gitAuto" title="自动刷新：保存文件 / 窗口回到前台时重扫">自动</button>'
    + '  <button class="tbtn mini" id="gitOpts" title="显示选项">⚙</button>'
    + '  <button class="tbtn mini" id="gitRefresh" title="刷新">↻</button>'
    + "</div>"
    + '<div class="view-scroll" id="gitList">'
    + '  <div class="view-empty">点 ↻ 读取本地改动<br><br>'
    + "  右键条目：暂存 / 撤销改动 / 忽略 / 对比<br>"
    + "  远程工作区（ssh://）的命令在服务器上执行</div>"
    + "</div>";
  const svn = el("viewSvn");
  if (svn && svn.parentNode) svn.parentNode.insertBefore(box, svn.nextSibling);
  else document.body.appendChild(box);
  return box;
}

/* 当前工作区要带上的参数（后端靠它决定本地还是 SSH）。 */
function gitWsOpts() {
  // 界面这条路吃内存快照（"打开就出内容"）。只给 status 带 `snap` ——
  // 别的动作（diff / log / commit…）都不该看到这个参数，多传只会让人误以为
  // 它们也走快照。
  const o = {};
  if (typeof S !== "undefined" && S && S.workspace) o.workspace = S.workspace;
  return o;
}

/** status 专用的参数：工作区 + `snap=1`（吃内存快照）。 */
function gitStatusOpts() {
  return Object.assign(gitWsOpts(), { snap: true });
}

/**
 * 让后端**现在**就去扫一遍，把结果放进它的内存快照里。
 *
 * 为什么要有这个：打开面板的十几秒里，大头是后端扫盘 + `git status`
 * （远程 31 个仓库走一条批量 SSH 脚本要 20-30s）。后端现在会用内存快照
 * （见 `__init__.py` 的「内存快照」一节），但快照得**先有人算过**才有 ——
 * 否则第一次打开照样要等。所以在真正需要它之前就先丢一次请求：启动、
 * 切工作区、鼠标移到 Git 图标上。
 *
 * 后端立即返回（扫描在它自己的后台线程里），所以这里不 await 也无所谓；
 * 仍然 await + 吞异常，纯粹是为了不产生 unhandledrejection。
 */
async function gitWarm() {
  try {
    await api(GIT_API + "warm", gitWsOpts(), { timeout: GIT_MISC_TIMEOUT });
  } catch (e) { /* 未登录 / 没工作区：下次打开面板时再算，不影响功能 */ }
}

/**
 * 状态栏尾巴：把「快照有多旧 / 后台还在补」说清楚。
 *
 * 为什么必须显示：后端现在会**先把旧快照给界面**（stale-while-revalidate），
 * 于是面板上可能出现"上一次算的结果"。不标注的话，用户刚在命令行改完文件、
 * 切回来看到干净的列表，会以为插件坏了 —— 而其实一秒后就会自己更新。
 */
function gitSnapTail(snap) {
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

/**
 * 后端给了一份**旧快照**（`snapshot.stale=true`）—— 等它后台算完再取一次。
 *
 * 2000ms 而不是立刻：git status 在几十个仓库的工程上要好几秒到几十秒，
 * 立刻再来一发只会拿到同一份旧快照（而且又起一个后台线程，白烧磁盘 /
 * 白占 SSH 通道）。同一个快照时间戳只补一次，等太久就交给下一次
 * 自动刷新或用户点 ↻。
 */
function gitScheduleStalePickup(snap) {
  const at = Number(snap && snap.at) || 0;
  if (!at || at === GIT_STALE.pickupAt) return;
  GIT_STALE.pickupAt = at;
  if (GIT_STALE.timer) clearTimeout(GIT_STALE.timer);
  GIT_STALE.timer = setTimeout(() => {
    GIT_STALE.timer = null;
    if (curView === GIT_VIEW) refreshGit();
  }, 2000);
}

/**
 * 调一个 git 动作。
 * `quiet=true` 时不自动弹错 —— 给提交这种要自己处理失败的调用方用。
 * `timeout` 见上面那几个常量的注释：**必须按后端那条命令的上限来给**，
 * 不传就按普通动作的 120s（api() 自带的 15s 对 git 基本都不够）。
 */
async function gitCall(action, body, okMsg, quiet, timeout) {
  const payload = Object.assign(gitWsOpts(), body || {});
  const ms = (timeout === undefined || timeout === null) ? GIT_MISC_TIMEOUT
                                                         : timeout;
  try {
    const r = await api(GIT_API + action, payload, { timeout: ms });
    const res = (r && r.result) || {};
    if (!res.ok) {
      if (!quiet) toast(res.summary || res.content || "操作失败", true);
      return res;
    }
    if (okMsg) toast(okMsg);
    return res;
  } catch (e) {
    // 超时单独交代（照 svn.js:728-743 的做法）：后端那条 git 命令不感知
    // HTTP 断开，很可能还在跑。把它和「压根没跑起来」混成一句 e.message，
    // 用户就会直接再点一次提交 —— 这才是真正会造成重复提交的地方。
    if (e && e.timeout) {
      if (!quiet) toast("git " + action + " 请求超时", true);
      return { ok: false, summary: "请求超时",
               content: String(e.message || ""),
               data: { kind: "timeout",
                       hint: "前端等不到响应就放弃了，后端那条 git 命令可能还在跑。"
                           + "先点 ↻ 看一次结果，别急着重复提交。" } };
    }
    if (!quiet) toast("git " + action + " 失败：" + e.message, true);
    return { ok: false, summary: e.message, data: {} };
  }
}

/**
 * 两段式扫描：先 `--untracked-files=no` 出首屏，再补一次全量。
 *
 * 为什么分两段：status 的耗时几乎全在「扫未跟踪文件」那一半 —— `normal`
 * 要 readdir 整棵工作树再逐条对 .gitignore，`no` 只拿工作区比 index。
 * gp221 那种 31 个仓库的工作区，第一段通常一两秒，全量要 20-30s，
 * 正好卡在旧版 15s 的请求超时上（那个坑见 md/git面板请求超时15s.md）。
 *
 * 返回**第一段**的结果：只有第一段失败才算整体失败（调用方据此清空并报错）；
 * 第二段失败只在状态栏追加一句，绝不推翻已经画出来的首屏。
 * `ep` 是调用方当时的 GIT_EPOCH，两段各自都要校验 —— 中间可能切了工作区。
 */
async function gitScan(ep, extra) {
  const sum = el("gitSummary");
  // snap=1：status 这条吃后端内存快照（打开即用的来源）；别的动作不吃。
  const body = Object.assign(gitStatusOpts(), extra || {});
  if (body.include_ignored) {
    // 「包含已忽略」本来就要扫全树（后端那条 ls-files --others --ignored），
    // 第一段省不下时间，别白跑一遍
    if (sum) sum.textContent = "扫描中…（含已忽略，请稍候）";
    const only = await gitCall("status", body, "", true, GIT_STATUS_TIMEOUT);
    if (ep !== GIT_EPOCH || !only.ok) return only;
    gitApply(only);
    return only;
  }
  const quick = Object.assign({}, body, { untracked: false });
  if (sum) sum.textContent = "扫描中…（不含未跟踪文件）";
  const first = await gitCall("status", quick, "", true, GIT_STATUS_TIMEOUT);
  if (ep !== GIT_EPOCH || !first.ok) return first;
  gitApply(first);
  if (sum) sum.textContent = (first.summary || "扫描完成") + "　正在补全未跟踪…";
  const full = Object.assign({}, body, { untracked: true });
  const res = await gitCall("status", full, "", true, GIT_STATUS_TIMEOUT);
  if (ep !== GIT_EPOCH) return first;
  if (res.ok) {
    // 后端的全量那份可能是一份**过期快照**（warm 太早、或刚被写操作作废）。
    // 这种情况下不能拿它覆盖首屏：首屏是刚算出来的新的，而这份是旧的，
    // 覆盖上去会"更完整但更旧" —— 已跟踪的新改动凭空消失，比不补还坏。
    // 交给下一次自动刷新 / 补拉去拿真结果。
    const fd = res.data || res || {};
    const fullStale = !!(fd.snapshot && fd.snapshot.stale);
    if (fullStale) {
      if (sum) {
        sum.textContent = (first.summary || "")
          + "　未跟踪稍后补全：后端在后台重算全量";
      }
      gitScheduleStalePickup(fd.snapshot);
      return first;
    }
    gitApply(res);
  } else if (sum) {
    // 首屏那半（已跟踪的改动）是准的，只是未跟踪还没补上
    sum.textContent = (first.summary || "")
      + "　未跟踪未补全：" + (res.summary || "读取失败") + "，点 ↻ 重试";
  }
  return first;
}

/**
 * 刷新改动清单。`force=true` 跳过节流（用户点了 ↻ / 切进来）。
 *
 * `fresh=true` 绕过后端内存快照同步重算 —— 写操作之后必须用它：
 * 拿旧快照下结论会漏文件，或者把刚撤销的改动又列出来。
 *
 * `extra` 是附加的 status 选项（`include_ignored` / `untracked` …）。
 */
async function refreshGit(force, fresh, extra) {
  if (GIT_BUSY) {
    // 正在扫的时候又来一次（连点 ↻ / 切工作区撞上在跑的扫描）：
    // **记下来，等这次结束再补一遍**，而不是并发再压一批 git 命令上去。
    // 以前这里是 `if (GIT_BUSY && !force) return`，force 时直接并发跑
    // 第二遍 —— 连点两下 ↻ 就是两批 status 抢同一块磁盘。
    GIT_AGAIN = true;
    return;
  }
  const now = Date.now();
  if (!force && now - GIT_LAST < GIT_MIN_GAP) return;
  GIT_LAST = now;
  GIT_BUSY = true;
  GIT_EPOCH += 1;
  const ep = GIT_EPOCH;
  const sum = el("gitSummary");
  if (sum) sum.textContent = "扫描中…（多仓库 / 远程请稍候）";
  try {
    const opts = Object.assign({}, extra || {}, fresh ? { fresh: true } : {});
    const res = await gitScan(ep, opts);
    // 期间切了工作区 / 又刷了一次：这次结果作废
    if (ep !== GIT_EPOCH) return;
    if (!res.ok) {
      GIT_TREE = []; GIT_REPOS = []; GIT_COUNTS = {}; GIT_ROOTS = [];
      // 错误原文必须留住：以前这里只清空就 renderGitTree()，而那个函数看到
      // GIT_TREE/GIT_REPOS 全空只会画「没扫到 git 仓库」—— 超时、SSH 断开、
      // 后端报错统统被洗成「没有仓库」，用户于是去怀疑仓库路径不对。
      GIT_ERR = res.summary || res.content || "读取失败";
      renderGitTree();
      if (sum) sum.textContent = res.summary || "读取失败";
      toast(res.summary || res.content || "读取失败", true);
      return;
    }
    gitApply(res);
  } finally {
    GIT_BUSY = false;
    if (ep === GIT_EPOCH) {
      GIT_LAST = Date.now();
      if (GIT_AGAIN) { GIT_AGAIN = false; refreshGit(true); }
    }
  }
}

/** 把后端结果落进状态并重画（refresh 和带选项的刷新共用）。 */
function gitApply(res) {
  // 兜底读 res 本身：旧版后端 `status` 入口把 data 的字段平铺在顶层
  // （不带 data 键），当时这里只认 res.data，于是永远拿到空对象、
  // 列表区画「没扫到 git 仓库」。后端已改成和其它入口一致的
  // {ok, summary, content, data}，这里留一层兼容，防止缓存里的老页面 / 老服务混搭。
  const d = res.data || res || {};
  GIT_ERR = "";
  GIT_TREE = d.tree || [];
  GIT_REPOS = d.repos || [];
  GIT_COUNTS = d.counts || {};
  GIT_ROOTS = d.roots || [];
  GIT_REMOTE = !!d.remote;
  GIT_SNAP = d.snapshot || {};
  // 勾选集里可能有已经提交掉的文件，剔一遍
  const alive = new Set();
  for (const t of GIT_TREE) gitCollectPaths(t, alive);
  for (const p of Array.from(GIT_SEL)) if (!alive.has(p)) GIT_SEL.delete(p);
  renderGitTree();
  const sum = el("gitSummary");
  if (sum) {
    sum.textContent = (res.summary || "") + gitSnapTail(GIT_SNAP)
      + (GIT_AUTO ? "" : "　（后台自动刷新已关）");
  }
  // 旧快照：先渲染出来让人马上有东西看，等后端后台算完再补一次真结果
  if (GIT_SNAP.stale) gitScheduleStalePickup(GIT_SNAP);
}

function gitCollectPaths(node, out) {
  out.add(node.fsPath);
  for (const c of node.children || []) gitCollectPaths(c, out);
}

/** 按缓存的树重画（折叠 / 勾选都不该再跑一次 status）。 */
function renderGitTree() {
  const list = el("gitList");
  if (!list) return;
  list.innerHTML = "";
  // 「只显示未暂存的改动」是显示层的事：剪一份出来，原树不动
  const tree = GIT_HIDE_STAGED ? gitPruneStaged(GIT_TREE) : GIT_TREE;
  if (!tree.length) {
    // 失败优先于"空"：仓库可能就在那儿，只是这次没读出来。用 errorBox
    // （左对齐、保留换行、可选中复制），别用居中的 view-empty 把长错误拍成一行。
    if (GIT_ERR) {
      const t = (GIT_ERR.indexOf("超时") >= 0)
        ? GIT_ERR + "\n扫描超时，后端可能仍在跑（多仓库 / 远程请多等一会儿），"
            + "稍后点 ↻ 再看一次。"
        : GIT_ERR;
      list.appendChild(errorBox(t));
      return;
    }
    // 有树却被剪空：是开关剪的，别说成"干净"（那是两回事）
    if (GIT_HIDE_STAGED && GIT_TREE.length) {
      list.appendChild(emptyBox("没有未暂存的改动"
        + "　（⚙ 里「只显示未暂存的改动」正开着）"));
      return;
    }
    list.appendChild(emptyBox(GIT_REPOS.length ? "工作区是干净的"
                                               : "没扫到 git 仓库"));
    return;
  }
  const byRoot = {};
  for (const r of GIT_REPOS) byRoot[r.root] = r;
  for (const t of tree) {
    list.appendChild(gitRepoHead(t, byRoot[t.fsPath]));
    gitNodes(t.children || [], list, 0);
  }
  gitMarkSel();
}

/**
 * 剪掉「只暂存、工作区已经干净」的文件（`M ` / `A ` 这类）。
 *
 * 为什么要重算 total：目录行和仓库头显示的是 `node.total`，剪完还留着
 * 被剪掉的那几条的数字，就会出现「目录徽标 3，展开只有一个文件」。
 * 剪空的目录 / 仓库整行不画 —— 和「没改动的仓库不出现」是同一条规矩。
 */
function gitPruneStaged(nodes) {
  const out = [];
  for (const n of nodes || []) {
    if (n.kind === "file") {
      if (n.staged && !n.unstaged) continue;
      out.push(n);
      continue;
    }
    const kids = gitPruneStaged(n.children);
    if (!kids.length) {
      // 空仓库头保留：那可能是 status 失败的仓库，界面靠它画 ⚠
      if (n.kind === "repo" && !(n.children || []).length) out.push(n);
      continue;
    }
    out.push(Object.assign({}, n, {
      children: kids,
      total: kids.reduce((a, k) => a + (k.total || 0), 0),
    }));
  }
  return out;
}

/** 仓库头：名字 + 分支 + 领先/落后 + 错误。 */
function gitRepoHead(node, repo) {
  const d = document.createElement("div");
  d.style.cssText = "display:flex;align-items:center;gap:6px;padding:3px 8px;"
    + "font-size:11px;color:var(--muted);border-top:1px solid var(--line);"
    + "margin-top:4px;cursor:default;overflow:hidden";
  const br = (repo && repo.branch) || {};
  let text = "▣ " + node.name;
  if (repo && repo.error) {
    text += "  ⚠ " + repo.error;
  } else if (br.branch) {
    text += "  " + br.branch;
    if (br.ahead) text += " ↑" + br.ahead;
    if (br.behind) text += " ↓" + br.behind;
    if (br.detached) text += "（游离头）";
    if (br.unborn) text += "（还没有提交）";
  } else if (br.detached) {
    text += "  （游离头）";
  }
  if (repo && repo.total) text += " · " + repo.total + " 处改动";
  d.textContent = text;
  return d;
}

function gitColor(color) {
  if (color === "danger") return "var(--danger)";
  if (color === "ok") return "var(--ok)";
  if (color === "warn") return "var(--warn)";
  if (color === "info") return "var(--accent)";
  return "var(--fg)";
}

function gitDirRow(node, depth) {
  const open = GIT_OPEN.has(node.fsPath);
  const row = document.createElement("div");
  row.style.cssText = "display:flex;align-items:center;gap:6px;padding:2px 8px;"
    + "padding-left:" + (8 + depth * 14) + "px;cursor:pointer;font-size:12px";
  row.innerHTML = '<span style="width:12px;color:var(--muted);flex:none">'
    + (open ? "▾" : "▸") + "</span>"
    + '<span style="color:' + gitColor(node.color) + ';overflow:hidden;'
    + 'text-overflow:ellipsis;white-space:nowrap">'
    + gitEsc(node.name) + "/</span>"
    + '<span style="color:var(--muted);font-size:11px;flex:none">'
    + (node.total || 0) + "</span>";
  row.addEventListener("click", () => {
    if (open) GIT_OPEN.delete(node.fsPath);
    else GIT_OPEN.add(node.fsPath);
    renderGitTree();
  });
  row.addEventListener("contextmenu", (ev) => gitMenu(ev, node, true));
  return row;
}

function gitFileRow(node, depth) {
  const row = document.createElement("div");
  row.dataset.path = node.fsPath;
  row.style.cssText = "display:flex;align-items:center;gap:6px;padding:2px 8px;"
    + "padding-left:" + (8 + depth * 14 + 12) + "px;cursor:pointer;font-size:12px";
  const on = GIT_SEL.has(node.fsPath);
  row.innerHTML = '<input type="checkbox"' + (on ? " checked" : "")
    + ' style="margin:0;flex:none">'
    + '<span style="color:' + gitColor(node.color)
    + ';font-family:monospace;width:14px;flex:none">'
    + gitEsc(node.status === " " ? "" : node.status) + "</span>"
    + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
    + gitEsc(node.name) + "</span>"
    + (node.conflict ? '<span style="color:var(--danger);font-size:11px">冲突</span>'
                     : "");
  const cb = row.querySelector("input");
  cb.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (cb.checked) GIT_SEL.add(node.fsPath);
    else GIT_SEL.delete(node.fsPath);
  });
  row.addEventListener("click", (ev) => {
    if (ev.target === cb) return;
    if (typeof openInEditor === "function") openInEditor(node.fsPath, node.name);
  });
  row.addEventListener("contextmenu", (ev) => gitMenu(ev, node, false));
  return row;
}

function gitNodes(nodes, box, depth) {
  for (const n of nodes || []) {
    if (n.kind === "file") {
      box.appendChild(gitFileRow(n, depth));
      continue;
    }
    box.appendChild(gitDirRow(n, depth));
    if (!GIT_OPEN.has(n.fsPath)) continue;
    gitNodes(n.children || [], box, depth + 1);
  }
}

/** 行是刚重建的，勾选态要按路径重新打一遍。 */
function gitMarkSel() {
  const list = el("gitList");
  if (!list) return;
  for (const row of list.querySelectorAll("[data-path]")) {
    if (!row.dataset || !row.dataset.path) continue;
    const cb = row.querySelector("input");
    if (cb) cb.checked = GIT_SEL.has(row.dataset.path);
  }
}

/** 勾选项 → 给后端的 targets（没勾就用这一条路径）。 */
function gitTargets(node) {
  if (GIT_SEL.size) return Array.from(GIT_SEL);
  return node && node.fsPath ? [node.fsPath] : [];
}

/**
 * 带输入框的确认框。
 *
 * 内核只有 `confirmBox`（没有输入框），`openModal` 是它下面那层、接受任意
 * body/foot —— 直接用它拼一个，比在插件里再写一套遮罩省事，也不会和内核的
 * 弹窗层级打架。返回 `Promise<string|null>`（取消 → null）。
 */
function gitAskText(title, text, defVal) {
  return new Promise((resolve) => {
    const body = document.createElement("div");
    const hint = document.createElement("div");
    hint.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:6px;"
      + "white-space:pre-wrap;line-height:1.5;max-height:240px;overflow:auto";
    hint.textContent = text;
    body.appendChild(hint);
    const ta = document.createElement("textarea");
    ta.value = defVal || "";
    ta.style.cssText = "width:100%;min-height:70px;box-sizing:border-box;"
      + "font-size:12px;padding:6px;resize:vertical";
    body.appendChild(ta);

    const foot = document.createElement("div");
    foot.appendChild(Object.assign(document.createElement("span"),
      { className: "spacer" }));
    const cancel = document.createElement("button");
    cancel.className = "tbtn";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.className = "tbtn";
    ok.textContent = "提交";
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
      if (ev.key === "Escape") finish(null);
      // Ctrl+Enter 提交：单行文本里回车要留给输入法，不能直接当确定
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) finish(ta.value);
    };
    ok.addEventListener("click", () => finish(ta.value));
    cancel.addEventListener("click", () => finish(null));
    document.addEventListener("keydown", onKey);
    openModal(title, body, foot);
    setTimeout(() => ta.focus(), 30);
  });
}

// ---------------------------------------------------------------------------
// 右键菜单
// ---------------------------------------------------------------------------

/** path 是不是在 root 下面（Windows 大小写不敏感，统一按 / 比）。 */
function gitUnder(path, root) {
  const p = String(path || "").replace(/\\/g, "/").toLowerCase();
  const r = String(root || "").replace(/\\/g, "/").toLowerCase().replace(/\/+$/, "");
  if (!p || !r) return false;
  return p === r || p.startsWith(r + "/");
}

/** 右键菜单要不要出现 Git ▸：清单还没扫出来时先显示（svn 也是这个取舍）。 */
function gitPathInRepo(path) {
  if (!GIT_ROOTS || !GIT_ROOTS.length) return true;
  for (const r of GIT_ROOTS) if (gitUnder(path, r)) return true;
  return false;
}

function gitMenu(ev, node, isDir) {
  ev.preventDefault();
  ev.stopPropagation();
  if (typeof ctx === "undefined" || !ctx) return;
  ctx.innerHTML = "";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = node.name + (isDir ? "/" : "");
  ctx.appendChild(title);

  const item = (label, fn, danger) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (danger) b.style.color = "var(--danger)";
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  const targets = gitTargets(node);
  item("暂存（git add）", () => gitDo("add", { targets: targets }, "已暂存"));
  item("取消暂存", () => gitDo("unstage", { targets: targets }, "已取消暂存"));
  sep();
  item("撤销改动…", () => gitDiscard(targets), true);
  item("对比 HEAD", () => gitDiff(node.fsPath));
  sep();
  item("忽略（写 .gitignore）", () => gitDo("ignore",
    { path: node.fsPath, name: node.name, detach: false }, "已加入 .gitignore"));
  item("忽略并从版本控制摘除", () => gitDo("ignore",
    { path: node.fsPath, name: node.name, detach: true }, "已忽略并摘除"), true);
  if (isDir) {
    sep();
    item("在这个仓库 fetch", () => gitDo("fetch", { path: node.fsPath },
                                       "已 fetch"));
    item("查看提交历史", () => gitShowLog(node.fsPath));
  }

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

async function gitDo(action, body, okMsg) {
  const res = await gitCall(action, body, okMsg, false,
                            gitActionTimeout(action));
  // 写操作之后必须 fresh：后端的内存快照已被作废，这里再明确一次
  // （将来后端漏了作废，也不会静默给出旧列表）。
  if (res && res.ok) refreshGit(true, true);
  return res;
}

/** 每个动作该等多久（对应 `__init__.py` 里那条命令的上限，这里只放宽不收紧）。 */
function gitActionTimeout(action) {
  if (action === "fetch" || action === "pull" || action === "push"
      || action === "commit") return GIT_NET_TIMEOUT;
  return GIT_MISC_TIMEOUT;
}

async function gitDiscard(targets) {
  const ok = await confirmBox("撤销改动",
    "把这些文件的改动丢掉、恢复成 HEAD 的样子？\n\n"
    + "未提交的内容会永久丢失。未跟踪（新）的文件不在这一步 —— "
    + "它们要走「维护 → 清理未跟踪」。", "撤销");
  if (!ok) return;
  await gitDo("discard", { targets: targets }, "已撤销");
}

/** 对比 HEAD：先拿结构化 diff 确认确实有改动，再开编辑器看文件本身。 */
async function gitDiff(path) {
  const res = await gitCall("diff_text", { path: path }, "", true);
  if (!res.ok) { toast(res.summary || "对比失败", true); return; }
  const files = (res.data || {}).files || [];
  if (!files.length) { toast("这个文件没有未提交的改动"); return; }
  if (typeof openInEditor === "function") openInEditor(path, "");
}

async function gitShowLog(path) {
  const res = await gitCall("log", { path: path, limit: 15 }, "", true);
  if (!res.ok) { toast(res.summary || "读取失败", true); return; }
  const list = (res.data || {}).commits || [];
  const text = list.map((c) => c.sha + "  " + c.date + "  " + c.author
    + "\n    " + c.subject).join("\n");
  if (typeof infoBox === "function") infoBox("提交历史", text || "（没有提交）");
  else toast(res.summary || "");
}

// ---------------------------------------------------------------------------
// 提交
// ---------------------------------------------------------------------------

async function gitCommitDialog(node) {
  const targets = gitTargets(node);
  const plan = await gitCall("plan", { targets: targets }, "", true);
  if (!plan.ok) { toast(plan.summary || "预览失败", true); return; }
  const d = plan.data || {};
  const files = d.files || [];
  if (!files.length) { toast("没有可提交的改动"); return; }
  const conflicts = d.conflicts || [];
  if (conflicts.length) {
    toast("还有 " + conflicts.length + " 个冲突没解决，先处理掉再提交", true);
    return;
  }
  const lines = files.map((f) => "  " + (f.xy || "") + " " + f.rel).join("\n");
  const msg = await gitAskText("提交 " + files.length + " 个文件",
    "将提交：\n" + lines + "\n\n提交说明（Ctrl+Enter 提交）：", "");
  if (msg == null) return;
  if (!String(msg).trim()) { toast("提交说明不能为空", true); return; }
  const res = await gitCall("commit", { targets: targets, message: msg },
                            "", true, GIT_NET_TIMEOUT);
  if (!res.ok) { toast(res.summary || "提交失败", true); return; }
  toast(res.summary || "已提交");
  GIT_SEL.clear();
  refreshGit(true, true);
}

// ---------------------------------------------------------------------------
// 维护菜单
// ---------------------------------------------------------------------------

function gitMaintainMenu(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "维护";
  ctx.appendChild(title);
  const item = (label, fn, danger) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (danger) b.style.color = "var(--danger)";
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  item("扫描未跟踪 / 已忽略（只看）", gitDoCleanScan);
  item("清理未跟踪 / 已忽略…", gitDoCleanRemove);
  ctx.appendChild(Object.assign(document.createElement("div"),
    { className: "sep" }));
  item("重新扫描仓库清单", async () => {
    await gitCall("repo_roots", { rescan: true }, "", true);
    refreshGit(true, true);
  });
  item("撤销全部本地改动…", gitDoDiscardAll, true);
  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

async function gitDoCleanScan() {
  const res = await gitCall("clean_scan", {});
  if (!res.ok) { toast(res.summary || "扫描失败", true); return; }
  const items = (res.data || {}).items || [];
  toast(items.length ? res.summary : "没有可清理的");
  // 明细贴到视图里（toast 塞不下）
  const list = el("gitList");
  if (!list || !items.length) return;
  list.innerHTML = "";
  for (const it of items) {
    const row = document.createElement("div");
    row.style.cssText = "padding:2px 10px;font-size:12px;color:var(--muted)";
    row.textContent = (it.ignored ? "已忽略  " : "未跟踪  ") + it.rel;
    list.appendChild(row);
  }
}

async function gitDoCleanRemove() {
  const ok = await confirmBox("清理未跟踪 / 已忽略",
    GIT_REMOTE
      ? "把这些未跟踪 / 已忽略的文件从服务器上删掉？\n\n"
        + "服务器没有回收站，删了就找不回来。"
      : "这些文件会被清理（本地走回收站）。\n\n可以恢复，但状态会丢。",
    "清理");
  if (!ok) return;
  const scan = await gitCall("clean_scan", {}, "", true);
  const items = ((scan.data || {}).items) || [];
  if (!items.length) { toast("没有可清理的"); return; }
  const res = await gitCall("clean_remove",
    { paths: items.map((i) => i.path) }, "", true);
  toast(res.summary || (res.ok ? "已清理" : "清理失败"), !res.ok);
  refreshGit(true, true);
}

async function gitDoDiscardAll() {
  const ok = await confirmBox("撤销全部本地改动",
    "把所有仓库的未提交改动丢掉？\n\n不可恢复。已暂存的也会一起丢掉。",
    "全部撤销");
  if (!ok) return;
  const res = await gitCall("discard_all",
    { all_workspaces: true, roots: GIT_ROOTS }, "", true);
  toast(res.summary || (res.ok ? "已撤销" : "撤销失败"), !res.ok);
  refreshGit(true, true);
}

// ---------------------------------------------------------------------------
// 视图初始化 / 挂载
// ---------------------------------------------------------------------------

function initGitView() {
  gitEnsureView();
  const on = (id, fn) => {
    const b = el(id);
    if (b && !b.dataset.bound) {
      b.dataset.bound = "1";
      b.addEventListener("click", fn);
    }
  };
  on("gitRefresh", () => refreshGit(true, true));
  on("gitAuto", () => {
    GIT_AUTO = !GIT_AUTO;
    const b = el("gitAuto");
    if (b) b.style.opacity = GIT_AUTO ? "1" : "0.5";
    localStorage.setItem("git.auto", GIT_AUTO ? "1" : "0");
    // 顺手把状态栏的「（后台自动刷新已关）」尾巴更新掉
    const sum = el("gitSummary");
    if (sum) sum.textContent = (sum.textContent || "")
      .replace(/　（后台自动刷新已关）$/, "")
      + (GIT_AUTO ? "" : "　（后台自动刷新已关）");
  });
  on("gitOpts", gitOptsMenu);
  on("gitCommit", () => gitCommitDialog(null));
  on("gitFetch", () => gitDo("fetch", {}, "已 fetch"));
  on("gitMaintain", gitMaintainMenu);

  // 活动栏那个 ⑂ 按钮：鼠标移上去 / 键盘聚焦就预热一次。
  // 用户从"想点"到"点下去"通常有两三百毫秒，这段时间够后端把扫描跑起来；
  // 真点开时 `do_status` 命中的就是一份新鲜快照 —— 面板直接就出内容。
  // 不用 click：click 说明已经切过来了，onshow 那次刷新会等不到快照。
  const gitAct = document.querySelector('.act[data-view="' + GIT_VIEW + '"]');
  if (gitAct && !gitAct.dataset.warmBound) {
    gitAct.dataset.warmBound = "1";
    gitAct.addEventListener("mouseenter", () => {
      if (curView !== GIT_VIEW) gitWarm();
    });
    gitAct.addEventListener("focus", () => {
      if (curView !== GIT_VIEW) gitWarm();
    });
  }

  if (localStorage.getItem("git.auto") === "0") {
    GIT_AUTO = false;
    const b = el("gitAuto");
    if (b) b.style.opacity = "0.5";
  }

  // 切工作区：折叠 / 勾选都是上一个工作区的绝对路径，必须清掉
  WS_HANDLERS.git = () => {
    GIT_OPEN.clear();
    GIT_SEL.clear();
    GIT_TREE = [];
    GIT_REPOS = [];
    GIT_ROOTS = [];
    GIT_ERR = "";
    GIT_SNAP = {};
    GIT_EPOCH += 1;
    if (GIT_STALE.timer) { clearTimeout(GIT_STALE.timer); GIT_STALE.timer = null; }
    GIT_STALE.pickupAt = 0;
    const list = el("gitList");
    if (list) {
      list.innerHTML = "";
      list.appendChild(emptyBox("切换工作区，正在重新扫描…"));
    }
    const sum = el("gitSummary");
    if (sum) sum.textContent = "扫描中…";
    // 先让后端在新工作区里开跑（它立即返回），再正常刷新 ——
    // 大工作区里这一下能把"扫描中"的时间压掉一半以上。
    gitWarm();
    // fresh：换了工作区，上一份快照属于别的工作区，不能拿来用
    refreshGit(true, true);
  };
}

/** ⚙：显示选项。 */
function gitOptsMenu(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "显示选项";
  ctx.appendChild(title);
  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  // 带 ✓ 的是开关项：点一次换一边。以前「只显示未暂存的改动」调过
  // `refreshGitWith({})` —— 空参数，跟普通刷新一模一样，点了没有任何变化。
  item((GIT_HIDE_STAGED ? "✓ " : "") + "只显示未暂存的改动", () => {
    GIT_HIDE_STAGED = !GIT_HIDE_STAGED;
    try { localStorage.setItem(GIT_HIDE_KEY, GIT_HIDE_STAGED ? "1" : "0"); }
    catch (e) { /* 存不了就只影响下次打开 */ }
    // 只重画不重扫：剪枝要看的数据（staged / unstaged）已经在 GIT_TREE 里
    if (GIT_HIDE_STAGED) {
      // 被剪掉的行不能还留着勾选：看不见却被「⇧ 提交」带上是数据风险
      const alive = new Set();
      for (const t of gitPruneStaged(GIT_TREE)) gitCollectPaths(t, alive);
      for (const p of Array.from(GIT_SEL)) if (!alive.has(p)) GIT_SEL.delete(p);
    }
    renderGitTree();
  });
  item("包含已忽略的文件", () => refreshGit(true, false,
                                      { include_ignored: true }));
  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/** 文件树右键的二级入口：只对"这一条路径"动手。 */
function gitTreeSubmenu(path, isDir) {
  if (!path) return;
  const name = String(path).split(/[\\/]/).pop() || path;
  const fake = { fsPath: path, name: name, kind: isDir ? "dir" : "file",
                 children: [], total: 0, color: "" };
  // 复用同一个菜单渲染：它只要坐标，这里贴当前窗口中间
  const ev = { preventDefault() {}, stopPropagation() {},
               clientX: Math.round(window.innerWidth / 2),
               clientY: Math.round(window.innerHeight / 3) };
  gitMenu(ev, fake, isDir);
}

// 建视图 + 注册。顺序不能反：viewRegister 之后 switchView 才认得 git
// （而 switchView 会 viewBox 查 `#viewGit`，所以 DOM 得先建）。
gitEnsureView();
// 启动就丢一次预热：**这是"打开即用"的关键**。
// 服务器起来 / 页面刷新之后立刻在后台把 status 和仓库清单扫一遍，等用户点
// ⑂ 图标时 `do_status` 命中的就是这份现成的快照 —— 面板直接出内容。
// 不 await、失败也无声（未登录时必然 401，等真正打开面板时自然会算）。
gitWarm();

/**
 * 后台刷新（「自动」开关管的就是这件事）。
 *
 * 以前 `GIT_AUTO` 是个**死开关**：只在点了按钮时改一下外观和 localStorage，
 * 没有任何地方读它 —— 打开着与关着行为完全一样。这里把它真正接上：
 * 窗口回到前台 / 标签页切回来时补刷一次，走 GIT_MIN_GAP 节流。
 * 视图没显示、或开关关着时什么都不做（那才叫"后台偷偷扫"）。
 */
function maybeRefreshGit() {
  if (!GIT_AUTO) return;
  if (curView !== GIT_VIEW) return;
  refreshGit(false);
}

window.addEventListener("focus", () => maybeRefreshGit());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) maybeRefreshGit();
});

viewRegister({
  view: GIT_VIEW,
  setters: () => initGitView(),
  // 每次打开都刷 —— 点进来看到旧的比"稍微慢一点"更糟。
  // 不带 fresh：后端有快照就直接给现成的（"打开即用"），没有就现算；
  // 有旧快照时它会先返回旧的再后台补，比对着"扫描中…"干等强。
  onshow: () => {
    gitWarm();
    refreshGit(true);
  },
});

// 文件树右键：`Git ▸`
menuRegister("tree.dir", "git_tools", {
  label: "Git ▸",
  order: 21,
  sep: true,
  when: (c) => gitPathInRepo(c && c.path),
  run: (c) => gitTreeSubmenu(c && c.path, true),
});

menuRegister("tree.file", "git_tools", {
  label: "Git ▸",
  order: 21,
  sep: true,
  when: (c) => gitPathInRepo(c && c.path),
  run: (c) => gitTreeSubmenu(c && c.path, false),
});
