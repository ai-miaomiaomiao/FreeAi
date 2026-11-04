// 工作区 / 会话
// 从 index.html 拆出（原第 1240-1722 行）。
// ---------------------------------------------------------------------------
// 工作区
// ---------------------------------------------------------------------------
//
// 两个概念别混（后端也是这么分的）：
//
//   · trees —— 打开过的工作区，可以多格，全排在顶部那排标签上。
//   · 活动根（S.workspace）—— 只有一格：左侧文件树画的就是它，
//     会话集合、工作区记忆、.ai/save.json 也全跟着它。
//
// 左侧文件树**只画活动根这一棵**。别的工作区不在这里并列铺开 ——
// 两棵目录树叠在一栏里，同名目录长得一模一样，分不清点的是哪个仓库；
// 要换过去点顶部那一格，切完这一栏整套换掉。
//
// 顶部标签的**显示顺序** = S.trees 的列表顺序，当前那个不挪到最前 ——
// 点一下整排位置全变很晕，下一块想点的位置也不在原来那儿了。
//
// 曾经试过「最近使用排前面」（localStorage 键 wsRecent）：标签多到要横向滚
// （滚动条已隐藏）时，固定顺序下想找刚用过的那格得先滚半天，看着很划算。
// 实测撤掉了，两个原因：① loadWorkspace 每次刷新都会记一次「刚用过」，
// 于是每次刷新都整排重排，位置天天变；② 重排会跟下面「把 .on 滚进可视区」
// 打架 —— 刷新后的落点是当前格，而不是你上次看的位置。
// 现在改成记住**横向滚动位置**（localStorage 键 wsTabsScroll）：滚到哪儿
// 刷新后还回哪儿，标签自己的位置永远不动。
//
// 记在 localStorage 是因为这是「本机使用痕迹」，不是「这个目录的属性」——
// 跟别名的取舍正好相反（别名在 workspace.json，见 md/工作区别名.md）。
// 换浏览器 / 换机器各看各的，不影响后端任何逻辑。

const WS_SCROLL_KEY = "wsTabsScroll";
// 这条标签条画过没有。首屏画（页面刚加载）才回填 localStorage 里的位置，
// 之后每次重画沿用当前的 scrollLeft —— 见 renderWsTabs 开头。
let wsTabsRendered = false;
// 「点了但还没落地」的目标工作区。**只用来点亮标签**，不代表活动根 ——
// 活动根只在 /api/workspace 回来之后才改（S.workspace 一变，所有请求的
// 路由就跟着走，抢跑会让写盘落到错的工作区）。
//
// 有它的原因：点了标签要等两三次后端往返才会有反应，人以为没点上就接着点，
// 于是请求越堆越多、界面越点越卡。现在点了立刻亮，剩下的在后台追。
let wsPending = "";

/** 上次顶部标签条滚到哪儿了（像素）。没存过 / 读坏了都当 0。 */
function wsTabsSavedScroll() {
  try {
    const n = Number(localStorage.getItem(WS_SCROLL_KEY));
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch (e) { return 0; }
}

/** 记住顶部标签条当前的横向滚动位置。 */
function wsTabsSaveScroll() {
  const box = $("wsTabs");
  if (!box) return;
  // 还没布局（宽度 0）时不写：那会儿 scrollLeft 恒为 0，写进去等于把存档点
  // 抹成"最左"，下次真画出来就再也回不到原来的位置了。
  if (box.clientWidth <= 0) return;
  try {
    localStorage.setItem(WS_SCROLL_KEY, String(Math.round(box.scrollLeft)));
  } catch (e) { /* 隐私模式 / 配额满：记不住就每次回到最左，不影响主流程 */ }
}

/** 把想恢复的位置夹进合法范围。窗口变窄、标签变少之后旧值会偏大，
 *  不夹的话浏览器会自己贴到最右，看着像「记忆错了」。 */
function wsTabsClampScroll(box, want) {
  const max = Math.max(0, box.scrollWidth - box.clientWidth);
  return Math.min(Math.max(0, want), max);
}

/** 顶部那排工作区标签。 */
function renderWsTabs() {
  const box = $("wsTabs");
  if (!box) return;
  // innerHTML = "" 会把 scrollLeft 一起归零，所以**先**把想恢复的位置取出来：
  // 首屏画时读 localStorage，之后每次重画沿用当前的（切工作区、加删工作区都会
  // 触发重画，沿用当前值才不会每次都被拽回上次存档点）。
  const keep = wsTabsRendered ? box.scrollLeft : wsTabsSavedScroll();
  box.innerHTML = "";
  const trees = S.trees || [];
  if (!trees.length) {
    const d = document.createElement("span");
    d.style.cssText = "font-size:13px;color:var(--muted)";
    d.textContent = "没有工作区";
    box.appendChild(d);
    return;
  }
  // 顺序就是 S.trees 本身，不排序、不挪前（理由见文件顶部）。
  // 高亮认「刚点的那一格」：切工作区要等后端往返，等 S.workspace 变过来才亮
  // 就成了"点了没反应"（见 wsPending 的说明）。
  const self = wsPending || S.workspace;
  for (const t of trees) {
    const on = (t === self);
    // 远程工作区（ssh://…）也占一格：名字取末级目录（/home/dev/project → gp）。
    // 它和本地格子是同一套语义 —— 切过去文件树/编辑器就是那边的，区别只是
    // 会话和 .ai 数据存在本地 .ai/remote/<hash>。
    const remote = String(t).startsWith("ssh://");
    const b = document.createElement("button");
    b.className = "ws-tab" + (on ? " on" : "") + (remote ? " remote" : "");
    // 有别名就显示别名（两个 gp 目录靠它区分）。title 里始终给真实全路径，
    // 免得起了名之后再也想不起这一格到底指向哪儿。
    const alias = (S.labels || {})[t] || "";
    b.textContent = (remote ? "🖥 " : "") + wsLabel(t);
    if (alias) b.classList.add("aliased");
    b.title = remote
      ? t + "\n\n远程工作区（SSH）\n文件树 / 编辑器走远端，会话和 .ai 数据存在本地"
      : t + (on
        ? "\n\n当前工作区：会话和工作区记忆跟它走"
        : "\n点一下切过去\n右键：重命名 / 换目录 / 移除");
    b.addEventListener("click", () => activateTree(t));
    b.addEventListener("contextmenu", (ev) => treeTabMenu(ev, t));
    box.appendChild(b);
  }
  // 先把记忆里的位置回填（innerHTML="" 已经把它归零了），再判断当前那格在不在
  // 可视区。顺序反过来的话，回填会把刚滚好的位置冲掉。
  box.scrollLeft = wsTabsClampScroll(box, keep);
  // 当前那格如果不在可视区（首屏恢复的位置看不到它，或刚切到一个远处的标签），
  // 只在这时才把它滚进来 —— 处于可视区就一点都不动，保留你上次滚到的位置。
  const on = box.querySelector(".ws-tab.on");
  if (on) {
    const br = box.getBoundingClientRect(), ar = on.getBoundingClientRect();
    if (ar.left < br.left) box.scrollLeft -= (br.left - ar.left);
    else if (ar.right > br.right) box.scrollLeft += (ar.right - br.right);
  }
  // 回填 + 可能的那一滚都算「用户看到的位置」，存一次：这样首屏恢复之后的位置
  // 立刻成为新的存档点，不会出现「刷新两次回到不同地方」。
  // 容器还没布局（宽度 0）时不算数：那会儿 scrollLeft 恒被浏览器夹成 0，
  // 一旦把 wsTabsRendered 置位，下一次重画就会把这 0 当成「当前位置」，
  // 存档点也就丢了 —— 留着 false，下一次重画仍然从 localStorage 恢复。
  if (box.clientWidth > 0) {
    wsTabsRendered = true;
    wsTabsSaveScroll();
  }
}

// 滚动条已隐藏（见 style.css 的 .ws-tabs），但鼠标滚轮在横向容器里默认不走 ——
// 有横向溢出时就把竖向 deltaY 折算成 scrollLeft，和 .term-tabs 的手感对齐。
// passive:false 必须显式给：默认 passive 的监听里调不了 preventDefault，
// 那样页面会跟着纵向滚一下，等于白转。
(function initWsTabsWheel() {
  const box = $("wsTabs");
  if (!box) return;
  // 滚到哪儿记到哪儿（passive: 只读，不拦默认行为）。脱手、刷新、切工作区
  // 之后都从这个存档点恢复，见 renderWsTabs 开头的 keep。
  box.addEventListener("scroll", wsTabsSaveScroll, { passive: true });
  box.addEventListener("wheel", (ev) => {
    if (ev.ctrlKey) return;               // Ctrl+滚轮是整页缩放（00-core.js），别抢
    if (box.scrollWidth <= box.clientWidth) return;  // 没溢出就交给页面
    // 一格一格挪：滚轮一格大概半个标签宽，触控板（|deltaY| 很小）就挪少点。
    // 方向只看正负 —— 不同设备一次 deltaY 是 1、3 还是 100 都不一样。
    const mag = Math.abs(ev.deltaY) >= 100 ? box.clientWidth * 0.8 : 60;
    box.scrollLeft += (ev.deltaY > 0 ? mag : -mag);
    ev.preventDefault();
  }, { passive: false });
})();

// ---------------------------------------------------------------------------
// 插件面板「跟着工作区走」
// ---------------------------------------------------------------------------
//
// 这些插件的数据都是**按工作区**存的，而且统一落在
// `<工作区>/.ai/temp/<插件名>/`（见 plugins/_shared/wsdata.py）——
// gradle_tools / logcat / launcher 各一个子目录。所以切了
// 工作区，面板上挂着的东西就全都"不是这个工作区的"了：任务列表是上一个工程的、
// 点 ▶ 算出来的命令带着旧 project、日志面板显示的行写在别的目录里。
//
// 这里登记每个视图"换成当前工作区"的处理，切工作区时统一跑一遍
// （activateTree / removeTree，见 onWorkspaceChanged 的调用点）。
//
// 每个 handler **自己**负责两件事，别在调用点上写：
//   ① 这个视图还没初始化过就直接返回 —— 等它第一次进来时自然是最新的，
//      别为一个没人看的视图白跑请求；
//   ② 已经跟当前工作区一致也直接返回 —— 不然会把用户的选择/展开状态白清一遍。
//
// 设备视图（adb_tools 的 devices）**不登记**：连着哪台车机是物理事实，
// 跟打开哪个目录没关系，换工作区不该把设备选择清掉。
const WS_HANDLERS = {};

function onWorkspaceChanged() {
  for (const name in WS_HANDLERS) {
    try {
      WS_HANDLERS[name]();
    } catch (e) {
      // 一个视图换不过来不能挡住切工作区本身
      console.warn("跟着工作区换 " + name + " 失败", e);
    }
  }
  // handler 只负责"把上一个工作区的状态扔掉"，不少面板清完缓存就结束了，
  // DOM 上还挂着旧那棵树（SVN 就是这样：清了 SVN_TREE，却没人重画）。
  // 当前正显示的视图统一再跑一次 onshow —— 那是插件自己定义的重拉入口。
  // 已经初始化过才跑：没进过的视图等它第一次进来自然是最新的。
  if (VIEW_INIT[curView] && VIEW_ONSHOW[curView]) {
    try {
      VIEW_ONSHOW[curView]();
    } catch (e) {
      console.warn("切工作区后刷新 " + curView + " 失败", e);
    }
  }
}

/** 工作区标签的右键菜单。 */
function treeTabMenu(ev, path) {
  ev.preventDefault();
  ev.stopPropagation();
  ctx.innerHTML = "";
  const on = (path === S.workspace);

  const item = (label, fn, dis) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (dis) { b.disabled = true; b.style.opacity = ".45"; }
    else b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = path;
  ctx.appendChild(info);

  // 起别名：两个末级同名的目录（两个 gp）在标签上分不清，靠它区分。
  // 用原生 prompt 而不是自建弹窗 —— 文件重命名也是这么做的（见 40-tree.js）。
  // 输入原名或留空 = 清掉别名，退回末级目录名。
  const cur = (S.labels || {})[path] || "";
  item("重命名…", () => renameTree(path, cur));

  // 远程格子没有"换目录"这回事：它的路径由 SSH 目标决定，换就是删了重加
  const remote = String(path || "").startsWith("ssh://");
  if (!on) item("切换到此工作区", () => activateTree(path));
  if (!remote) item("换目录…", () => changeTreeDir(path));
  else if (typeof dtAddRemoteDialog === "function") {
    // 编辑已有 SSH：改目录 / 改这台机器上 ripgrep 的路径。密码不回显，
    // 留空就是"不改"（见后端的 _handle_ssh_workspace_add）。
    item("SSH 设置（目录 / ripgrep 路径）…", () => {
      dtAddRemoteDialog(async (r) => {
        // 标识没变（只改了 rg）：工具每次现读 ssh_targets.json，立刻生效，
        // 标签不用动
        if (!r || !r.ws || r.ws === path) return;
        // 换了远程目录 / 端口 → 标识变了：把旧格子换成新的，别留两格
        await removeTree(path, true);
        await addTree(r.ws, true);
        await activateTree(r.ws);
      }, { ws: path });
    });
  }

  // 远端没有 rg 时搜索会静默退回慢一档的 grep：这里给个不用翻二级菜单的
  // 入口（文件树 `SSH ▸` 里那一项是同一个函数，见 40-tree.js）。
  // 用 typeof 防护：新前端配旧内核时至少什么都不显示，不会报错。
  if (remote && typeof uploadBundledRg === "function") {
    item("上传内置 ripgrep…", () => uploadBundledRg(path));
  }
  item("复制路径", () => copyText(path));

  // 插件项：工作区标签的上下文
  menuRender("workspace", { path, current: on }, {
    item: (label, fn, danger) => {
      const b2 = document.createElement("button");
      b2.textContent = label;
      if (danger) b2.style.color = "var(--danger)";
      b2.addEventListener("click", () => { hideCtx(); fn(); });
      ctx.appendChild(b2);
    },
    sep: () => ctx.appendChild(
      Object.assign(document.createElement("div"), { className: "sep" })),
  });

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  // 破坏性的排最后，而且最后一棵不许移除（后端也会拒）
  item("移除（不删文件）", () => removeTree(path), (S.trees || []).length <= 1);

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/**
 * 给一格工作区起别名（只改显示名，不动磁盘目录）。
 *
 * 用原生 prompt：项目里没有自建输入框组件，文件重命名也是原生 prompt
 * （见 40-tree.js 的 rename 分支），保持一致。
 *
 * 传空串 = 清除别名。后端把别名存进 .ai/workspace.json 的 labels，
 * 所以换个浏览器打开还是同一个名字。
 */
async function renameTree(path, cur) {
  const now = String(cur || "");
  const tip = baseName(path) + (now ? "（当前显示为「" + now + "」）" : "");
  const input = prompt(
    "给这个工作区起个显示名（只改标签上的字，不动磁盘目录）\n" + tip
    + "\n留空 = 恢复成目录名", now);
  if (input === null) return;                     // 点了取消
  const label = String(input).trim();
  try {
    const r = await api("/api/workspace/label", { path, label });
    S.labels = r.labels || S.labels || {};
    renderWsTabs();
    renderWorkspace();
    toast(label ? "已重命名为「" + r.label + "」" : "已恢复成 " + baseName(path));
  } catch (e) {
    toast("重命名失败：" + (e && e.message ? e.message : e), true);
  }
}

/**
 * 把一格工作区**换成另一个目录**（仓库挪了位置，不想再多占一格）。
 *
 * 实现上是"先加新的、再撤旧的" —— 万一新目录不可用，旧的还在，
 * 不会出现"换失败了、格子也没了"。
 */
function changeTreeDir(oldPath) {
  pickDirectory({
    title: "换目录",
    hint: "把「" + wsLabel(oldPath) + "」这一格换成别的目录。\n"
        + "旧的只是从列表里拿掉，**不会删任何文件**。",
    label: "换成这个",
    onPick: async (newPath) => {
      if (newPath === oldPath) { toast("还是同一个目录"); return; }
      const wasActive = (oldPath === S.workspace);
      const hadAlias = !!(S.labels || {})[oldPath];
      await addTree(newPath, true);
      // 先切过去，再撤旧的 —— 这样撤的时候旧的已经不是当前工作区，
      // 不会触发"移除当前工作区 → 自动切到第一棵"那套
      if (wasActive) await activateTree(newPath);
      // 别名跟"这一格"走：起过名字的格子换完目录还叫那个名字。
      // 必须赶在 removeTree 之前搬 —— 移除时后端会按 trees 过滤别名，
      // 晚一步旧路径那条记录就没了。
      if (hadAlias) {
        try {
          const r = await api("/api/workspace/label/move",
            { from: oldPath, to: newPath });
          S.labels = r.labels || S.labels || {};
        } catch (e) {
          console.warn("搬别名校验失败", e);
        }
      }
      await removeTree(oldPath, true);
      toast("已换成 " + wsLabel(newPath));
    },
  });
}

/**
 * 切工作区：会话列表、工作区记忆、终端工作目录整套换过去。
 *
 * 这一步会把当前会话列表整个换掉并清空消息区，所以**不能**做成
 * "点文件就顺便切" —— 那会让人莫名其妙地丢上下文。
 *
 * 模型**不在**这里换：它是全局偏好（跟助理一样），换项目不该跟着变。
 */
async function activateTree(path) {
  if (!path) return;
  if (path === S.workspace) {
    // 已经在这一格：不要整轮切换，但要把"待切"的高亮收回来
    wsPending = "";
    renderWsTabs();
    return;
  }
  // 点了就亮：标签高亮 + 左栏换成**缓存里那棵**。这两步都是纯 DOM（不发请求），
  // 放在这里是为了"点下去立刻有反应"——反复切时的卡顿感主要来自这段空档。
  wsPending = path;
  quickSwitchVisual(path);
  // 真正换根走同一条队列（见 syncWorkspace），连点时只保留最后一个目标
  await syncWorkspace(path);
}

/**
 * 切工作区时**立刻**能做的那些视觉动作：亮标签、左栏挂缓存里的树、切到文件视图。
 *
 * 纯同步、不发请求、不碰 S.workspace（活动根只能由 /api/workspace 回来之后再改，
 * 否则这中间发出的请求会被路由到还没生效的工作区上）。
 */
function quickSwitchVisual(ws) {
  if (!ws) return;
  // 独立窗口（?detached=log / board）不切视图：左栏整个是藏起来的，
  // 而且 37-detached.js 定位文件时也走 applyWorkspace，切一次就把日志面板顶掉。
  const detached = (typeof DETACHED_LOG !== "undefined" && DETACHED_LOG)
    || (typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD);
  // 顺序：**先挂树再切视图**。反过来（先 switchView）的话，files 视图第一次
  // 初始化会走 setter → loadTree()，而那时 S.workspace 还是旧值 —— 它会拿旧
  // 工作区从头列一遍盘。先挂好，setter 里那次 loadTree 就命中早退。
  if (typeof mountTreeFromCache === "function") {
    try { mountTreeFromCache(ws); } catch (e) { /* 挂不上就让随后的 loadTree 重列 */ }
  }
  if (!detached && typeof switchView === "function" && curView !== "files") {
    switchView("files");
  }
  renderWsTabs();
  renderWorkspace();
}

/**
 * 真正换活动根。`fromTab` = 从标签栏切过来的：此时**不要**动标签，
 * 只是把文件树/终端/编辑器/插件面板换到目标工作区，然后显示那个标签。
 *
 * 两件事分开是关键：以前换根=resetAllTabs，A 项目正在跑的会话被 abort，
 * 切回来还得重拉历史 —— 所谓"两个工作区同时干"根本做不到。
 */
async function applyWorkspace(path, opts) {
  opts = opts || {};
  const first = !S.workspace;
  // 这次切换的序号。**反复切时只认最后一次**：前一轮的 /api/workspace 回来得晚
  // （后端要读 .ai、写 workspace.json、load 会话），回来的那一瞬间会把 S.workspace
  // 改回旧目标，于是"点了 B 停在 A"。这里的判断就是唯一的出口。
  // 必须 `(x||0)+1` 起步：`undefined++` 是 NaN，而 NaN !== NaN 恒真 ——
  // 那样每一轮都会被自己判成"过期"，工作区永远切不过去。
  const seq = (applyWorkspace._seq = (applyWorkspace._seq || 0) + 1);
  try {
    // 切走前要先落盘的编辑器状态，和 /api/workspace **并发**发出去。
    // 以前是 await prepareEditorSwitch() 完才开始请求 —— 切一次串行等一趟
    // 写盘 + 一趟 HTTP，连点几下就明显卡。saveEditorState 现在显式带 ws，
    // 所以并发也写得到**旧**工作区（见 85-editor-state.js）。
    const prevWs = S.workspace;
    const prep = (first || !prevWs) ? Promise.resolve()
      : (typeof prepareEditorSwitch === "function"
        ? prepareEditorSwitch(prevWs).catch(() => { }) : Promise.resolve());
    const r = await api("/api/workspace", { path });
    // 期间用户又点了别的：这一轮的结果整个作废（不落地、不刷界面）
    if (seq !== applyWorkspace._seq) return;
    await prep.catch(() => { });
    const prev = S.workspace;
    // 只清目标工作区自己的旧面板，而且**只在首次进入时清**。
    //
    // 坑：原来这里是 `if (!opts.keepTabs) resetTabsOfWs(path)` —— 每次切工作区
    // 都把目标工作区的标签全销毁，包括**正在生成回复**的那些。标签对象一销毁，
    // t.running / t.controller 跟着没了，于是切回去看到的按钮是「发送」而不是
    // 「停止」，后台的流也没人接（事件还在发，但没有对应的面板）。
    //
    // 现在只在 first（第一次确定工作区）时清一次：那时候还没有任何标签，
    // 清了等于没清，纯粹兜底。之后来回切都保留，running 状态和 DOM 都还在。
    // 换工作区前先把**当前**这批标签存回旧工作区的 save.json，
    // 否则刚开的会话会随切走一起从存档里消失（面板还在内存里，但存档没更新）。
    //
    // 不再 await：它和上面的 /api/workspace 已经**互相独立**（各写各的 .ai），
    // 串行等只是白等一趟往返。发出去就不管，失败也无所谓（下次切换还会写）。
    if (!first && S.workspace && S.workspace !== (r.workspace || "")) {
      api("/api/state", { openTabs: snapshotOpenTabs(S.workspace), ws: S.workspace })
        .catch(() => { /* 存不下不影响切换 */ });
    }
    if (first || opts.rebuildTabs) resetTabsOfWs(path);
    S.workspace = r.workspace || "";
    // 落地了：不再"待切"，标签高亮回到活动根（见 wsPending 的说明）
    if (wsPending && wsPending === S.workspace) wsPending = "";
    // 终端立刻换成**这个工作区自己**那一批标签（和会话标签一个规则：切走的那批
    // 留在后台继续跑，切回来原样显示；见 90-tools.js 的 termFollowWorkspace）。
    // 放在这里而不是后面：中间还有好几个 await 和视图刷新，任何一步抛异常都会
    // 让终端留在旧工作区；而且这段空档里如果用户敲了命令，也会跑进旧项目。
    if (typeof termFollowWorkspace === "function") termFollowWorkspace(S.workspace);
    S.memoryPath = r.memory_path || "";
    S.trees = r.trees || S.trees;
    if (r.labels) S.labels = r.labels;
    S.sessions = r.sessions || [];
    // 这里**不写 view**：切走时磁盘上那格应该原样留着，切回来才好恢复。
    // （曾经为了「切过去就是文件树」在这里 fire-and-forget 写 view:"files"，
    //  那会把目标工作区自己的记录冲掉，于是"切回来还是文件树"。已撤。）
    // 用户点哪个标签就该停在哪个标签：从标签栏切过来时 `switchTab` 已经把
    // activeSid 设成那个标签了，这里**不能**无脑后端 active 覆盖 ——
    // 否则"点 B1 切到 B 项目，结果停在 B2"（后端 active 是上次聊的那个）。
    // 只有当当前活动标签不属于目标工作区时，才用后端 active 兜底。
    const keep = (S.activeSid && S.tabs[S.activeSid]
                  && (S.tabs[S.activeSid].ws || "") === (r.workspace || "")) ? S.activeSid : "";
    S.activeSid = keep || (r.active ? tabKey(r.active, S.workspace) : "");

    // 这个工作区自己的界面开关跟着它的 .ai/save.json 走 ——
    // 不重新读的话，在 A 里打开了"显示隐藏"，切到 B 按钮还亮着，
    // 可 B 的 save.json 里其实没有这个设置，下次启动就变回去了
    let st = {};
    // 开始读存档之前左栏挂的是哪份（quickSwitchVisual 可能已经把目标工作区的树
    // 按**当前开关**挂上了）：读完发现目标存档的开关和它不一样，就要换成另一份。
    // 走 currentTreeMount() 而不是直接碰 treeStashKey / treeStashHidden：
    // 那两个 let 定义在 40-tree.js，和这里是分开的 <script>，直接引用在
    // 加载顺序不对时是 TDZ ReferenceError（typeof 也拦不住）。
    const mountNow = (typeof currentTreeMount === "function") ? currentTreeMount() : null;
    const mountedWs = mountNow ? mountNow.ws : "";
    const mountedHidden = mountNow ? mountNow.hidden : null;
    if (S.rememberUi !== false) {
      try {
        st = await api("/api/state") || {};
        // 这一趟期间又点了别的工作区：这是**上上个**工作区的存档，
        // 拿它去改 showHidden / openTabs 会把界面设成别人的。
        if (seq !== applyWorkspace._seq) return;
        S.showHidden = !!st.showHidden;
        // 每个工作区各自记着自己开过哪些标签（见 25-tabs.js persistOpenTabs）
        S.openTabs = Array.isArray(st.openTabs) ? st.openTabs.filter((x) => typeof x === "string") : [];
        // 左栏已经挂着**这个工作区**的树，但按的是旧开关画的：先存一份进去（按它
        // 自己画的时候那个开关），再把当前开关那份挂出来。缓存里有就直接挂、
        // 一次盘都不列；没有就让下面的 loadTree 正常重列。
        //
        // 不加这一步的话，quickSwitchVisual 那次挂载（那时 S.showHidden 还是
        // 旧工作区的值）会留下一个开关不对的树，紧接着的 loadTree 按 treeStashHidden
        // 判定"开关不一致"而整棵重列 —— 正好抵消掉缓存的意义。
        if (mountedWs === S.workspace && mountedHidden !== null
            && !!mountedHidden !== !!S.showHidden
            && typeof mountTreeFromCache === "function") {
          try { mountTreeFromCache(S.workspace, { force: true }); } catch (e) { /* 挂不上就走重列 */ }
        }
      } catch (e) { /* 读不到就沿用当前值 */ }
    } else {
      // 关掉「记忆界面布局」：不读目标工作区的存档，回到"全新打开"的默认布局。
      // 这里必须**主动清**而不是沿用内存里上一个工作区的值 —— 否则在 A 里
      // 开了「显示隐藏」，切到 B 还亮着，而 B 的存档根本没被读过。
      S.showHidden = false;
      S.openTabs = [];
    }
    // 切工作区**固定落到文件树**，而且当场就切 —— 不等下面 syncPluginViews()
    // 那趟 /api/plugins 往返。
    //
    // 以前这里是 `setWantedView(st.view || "files")`，把"目标工作区自己上次停的
    // 那一格"登记下来，实际的切换拖到 applyPluginViews() 末尾的 applyWantedView()
    // 才发生（VIEW_META 要到那一刻才齐）。于是点完切工作区，左栏还挂着**上一个
    // 工作区的视图**（在 gradle 里点切工作区，看到的还是 gradle 的任务列表），
    // 等到插件清单拉回来才闪一下变成目标视图 —— 这段时间叠加起来就是"整个界面
    // 很卡"。而多工作区场景下，切过去第一件事几乎总是要看文件，恢复上一次的
    // 插件视图反而像没切成功。
    //
    // 这里**不写盘**：switchView / setWantedView 都不碰 save.json，目标工作区
    // 存档里的 view 原值保留，刷新页面时仍按它自己的记录恢复（见 10-login.js）。
    // 独立窗口（?detached=log / board）不切：那里的左栏整个是藏起来的，
    // 而 37-detached.js 定位文件时也会走 applyWorkspace，切一次就把日志面板顶掉了。
    const _detached = (typeof DETACHED_LOG !== "undefined" && DETACHED_LOG)
      || (typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD);
    if (!_detached && typeof setWantedView === "function") setWantedView("files");
    if (!_detached && typeof switchView === "function") switchView("files");
    paintTreeHidden();

    renderWorkspace();
    renderWsTabs();
    // 工作区变了 → 输入区可能要从"置灰"恢复成可用（或者反过来）
    if (typeof syncComposerEnabled === "function") syncComposerEnabled();
    // 只看当前工作区的标签：别的项目的会话**不中断、不销毁**（流继续跑，
    // 切回去内容还在），只是从标签栏里隐藏。以前这里是「别的项目标签原样留着」，
    // 结果在 B 项目里还挂着 A 项目一排标签，容易误点、看着也乱。
    hideTabsOfOtherWs(S.workspace);
    // 各插件面板跟着工作区换一遍（任务列表、脚本记录、日志抓取…，见 WS_HANDLERS）。
    // **放到下一帧**：这里面有的要读磁盘（gradle 缓存、svn 状态），同步跑会让
    // "点了切工作区"到"面板真的变过来"之间明显顿一下；先让界面画完再扫。
    setTimeout(() => {
      try { onWorkspaceChanged(); } catch (e) { /* 一个面板换不过来不该挡住切换 */ }
    }, 0);
    // 插件图标也跟着换：禁用清单是**每个工作区各一份**
    // （`<工作区>/.ai/plugins_disabled.json`），不重拉的话切过去看到的还是
    // 上一个工作区的插件集合 —— 在 A 里禁用了 adb，切到 B 图标依然不见。
    //
    // 但**先看版本号**：插件目录和禁用清单位于应用根，跟工作区无关；一样就
    // 说明集合没变，这趟请求纯属白扫一遍 plugins 目录（见 agent_plugins.stamp）。
    //
    // 「按工作区类型藏哪个图标」必须先设：下面 syncPluginViews() 不 await，
    // 它建按钮时会读 VIEW_GATE 的当前值 —— 反过来（先拉再设）第一次进界面
    // 会看到 🐘 闪一下才消失。
    if (typeof setViewGate === "function") setViewGate(r.views_hidden);
    if (typeof syncPluginViewsIfChanged === "function") {
      syncPluginViewsIfChanged(r.plugins_fp);
    } else if (typeof syncPluginViews === "function") {
      syncPluginViews();
    }

    // 这三件事**互不依赖**：文件树 / 会话历史 / 编辑器恢复。
    // 以前是串行 await，切一次工作区的耗时是三者**相加**；而编辑器恢复本身还是
    // 逐个 `await openInEditor(...)`（开过 5 个文件就是 5 次读盘，远程工作区
    // 每次都是一趟 SSH），所以"切个工作区卡好几秒"主要卡在这里，跟会话存储
    // 没关系（那个实测 3~4ms）。并行之后只取最慢的那一项。
    await Promise.all([
      loadTree(),
      // 铺回**这个**工作区的当前会话（不是清空）——
      // 每个工作区一套 sessions.json，切过去就该看到那边的进度
      loadCurrentSession(),
      // 这个工作区上次开着的编辑器（文件 + 每个文件的光标位置）
      restoreEditorState(st.editors),
    ]);
    // 这一轮切换走完：解除"树刚挂好别再重列"的锁定，之后同工作区里的
    // 新建 / 删除 / 上传重列才会真的重列（见 40-tree.js 的 clearTreeMount）。
    if (typeof clearTreeMount === "function") clearTreeMount();
    // 这里**不做**后台预热：文件树 / 编辑器只缓存**用户真正打开过**的工作区。
    // 之前那套空闲预热反而成了"切的时候还卡"的来源 —— 切视图、切工作区都会排
    // 一次队，紧接着就是列盘 / 打 SSH，和当前这一棵抢 IO。详见
    // md/文件树内存缓存与预加载.md。
    // 这个工作区里可能有会话正在后端生成（另一个窗口 / 刷新前那一轮）：
    // 问一次 /api/chat/active，把标签状态对回来（不 await，别拖慢切换）
    if (typeof syncActiveSessions === "function") syncActiveSessions();
    if (!first && !opts.quiet) toast("已切到 " + wsLabel(S.workspace));
  } catch (e) {
    toast("切换失败：" + e.message, true);
  } finally {
    // 半路退出（网络错 / 被更晚的一次顶掉）也要解锁：留着标记的话，
    // 之后同工作区里新建、删除触发的重列会被一直挡住。
    // 但**只在我是最后一轮时清**：被顶掉的那一轮要是清了，排队中的新一轮
    // 就会拿缓存挂好的树再从头列一遍盘（白抖一次，正是要消掉的东西）。
    if (seq === applyWorkspace._seq && typeof clearTreeMount === "function") {
      clearTreeMount();
    }
  }
}

/**
 * 从标签栏切过来的静默换根：不弹提示、不做编辑器存盘（调用方 switchTab
 * 已经在点击瞬间就切了面板，这里只是把周边视图追上去）。
 * 失败就悄悄回退，不能让一次网络抖动把人卡死在半截状态。
 */
async function syncWorkspace(ws) {
  if (!ws || ws === S.workspace) {
    // 已经在这个工作区上（或没给）：没有要切的东西，但连点期间可能压过
    // 别的目标，把它清掉，标签高亮回到活动根。
    if (!ws) return;
    wsPending = "";
    if (ws === S.workspace) renderWsTabs();
    return;
  }
  if (syncWorkspace._busy) {
    // 正在切的时候又点了一个别的工作区的标签：记下最后一次，切完接着切。
    // 以前这里直接 return —— 快速点两个不同项目时第二次被悄悄丢弃，
    // 结果停在第一次的目标上，标签高亮的却是最后点的那个。
    syncWorkspace._pending = ws;
    // **但仍然要给反馈**：以前 busy 分支什么都不做，连点期间界面一动不动，
    // 人就以为没点上继续点 —— 这才是"反复切工作区越来越卡"的来源。
    // 视觉切换是纯 DOM，重入无害（缓存里那棵直接挂上）。
    wsPending = ws;
    try { quickSwitchVisual(ws); } catch (e) { /* 视觉跟不上不影响后台切换 */ }
    return;
  }
  syncWorkspace._busy = true;
  S.wsSwitching = true;
  try {
    // 存盘不在这里做：applyWorkspace 内部把它和 /api/workspace 并发发出去
    await applyWorkspace(ws, { quiet: true, keepTabs: true });
  } catch (e) {
    toast("切换工作区失败：" + e.message, true);
  } finally {
    S.wsSwitching = false;
    syncWorkspace._busy = false;
    const next = syncWorkspace._pending;
    syncWorkspace._pending = "";
    if (next && next !== S.workspace) syncWorkspace(next);
  }
}

/** 再加一个工作区进来（**不**切过去）。`quiet` 时不弹提示。 */
async function addTree(path, quiet) {
  if (!path) return;
  try {
    const r = await api("/api/workspace/add", { path });
    S.trees = r.trees || S.trees;
    if (r.labels) S.labels = r.labels;
    renderWorkspace();
    renderWsTabs();
    // 工作区变了 → 输入区可能要从"置灰"恢复成可用（或者反过来）
    if (typeof syncComposerEnabled === "function") syncComposerEnabled();
    await loadTree();
    if (!quiet) toast(r.already ? "它已经在标签栏里了"
                                : "已加入工作区：" + baseName(path) + "（点顶部标签切过去）");
  } catch (e) {
    toast("添加失败：" + e.message, true);
  }
}

/** 把一个工作区从标签栏拿掉（只动列表，不删任何文件）。 */
async function removeTree(path, quiet) {
  if (!quiet) {
    const ok = await confirmBox("移除工作区",
      "把「" + baseName(path) + "」从列表里拿掉？\n\n"
      + "只动列表，**不会删任何文件**。\n"
      + (path === S.workspace
         ? "它现在是当前工作区 —— 移除后会自动切到剩下的第一个，当前会话也会换过去。"
         : ""),
      "移除");
    if (!ok) return;
  }

  // 移除的正好是当前工作区时后端会顺手切走，所以状态要先存进旧工作区
  const wasActive = (path === S.workspace);
  if (wasActive) await prepareEditorSwitch();

  try {
    const r = await api("/api/workspace/remove", { path });
    S.trees = r.trees || [];
    if (r.labels) S.labels = r.labels;
    // 这个工作区已经被移出标签栏了：它的文件树 DOM 快照和编辑器文本槽一起丢掉，
    // 否则那棵展开着的树 / 那几份文件全文会一直占着内存（以前是靠在预热里
    // 遍历一遍标签栏来清理的，预热删掉之后必须在这儿补上）。
    if (typeof dropTreeSlots === "function") dropTreeSlots(path);
    if (typeof dropEditorSlot === "function") dropEditorSlot(path);
    if (r.workspace && r.workspace !== S.workspace) {
      // 移除的正好是当前工作区：后端已经替我们切好了，这里同步过来
      S.workspace = r.workspace;
      S.memoryPath = r.memory_path || "";
      S.sessions = r.sessions || [];
      S.activeSid = r.active ? tabKey(r.active, S.workspace) : "";
      if (typeof termFollowWorkspace === "function") termFollowWorkspace(S.workspace);
      else { termCwd = S.workspace; if (typeof showTermCwd === "function") showTermCwd(); }

      let st = {};
      if (S.rememberUi !== false) {
        try {
          st = await api("/api/state") || {};
          S.showHidden = !!st.showHidden;
          S.openTabs = Array.isArray(st.openTabs) ? st.openTabs.filter((x) => typeof x === "string") : [];
        } catch (e) { /* 读不到就沿用当前值 */ }
      } else {
        // 关掉「记忆界面布局」：不读新工作区的存档，也不沿用旧工作区的值
        // （理由同 applyWorkspace 里那段）。
        S.showHidden = false;
        S.openTabs = [];
      }
      // 移除当前工作区 = 被动换到另一个项目，规则和手动切**完全一致**：
      // 固定落文件树，当场切（理由同 applyWorkspace 里那段）。
      const _det = (typeof DETACHED_LOG !== "undefined" && DETACHED_LOG)
        || (typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD);
      if (!_det && typeof setWantedView === "function") setWantedView("files");
      if (!_det && typeof switchView === "function") switchView("files");
      paintTreeHidden();

      onWorkspaceChanged();           // 同上：各插件面板也要跟着换
      // 插件图标 / 视图按钮跟着换（不同工作区的插件集合可能不同），
      // 顺便在它收尾时把上面登记的视图切回来
      if (typeof syncPluginViews === "function") syncPluginViews();
      // 标签栏也要跟着换：被移除那个项目的标签收起来（不销毁，重新加回来还在），
      // 新工作区按它自己的 openTabs 恢复。少了这两行，移除当前工作区后
      // 标签栏还挂着一排旧项目的会话，而新工作区一个标签都没有。
      hideTabsOfOtherWs(S.workspace);
      renderTabBar();
      await loadCurrentSession();     // 跟着切到了别的会话，铺回来
      await restoreEditorState(st.editors);   // 以及那边上次开的编辑器
    }
    renderWorkspace();
    renderWsTabs();
    // 工作区变了 → 输入区可能要从"置灰"恢复成可用（或者反过来）
    if (typeof syncComposerEnabled === "function") syncComposerEnabled();
    await loadTree();
    if (!quiet) toast("已移除：" + baseName(path));
  } catch (e) {
    toast("移除失败：" + e.message, true);
  }
}

// 统一切模型的入口：主界面顶栏和任务台（37-detached.js）共用这一份，
// 免得两处各写一遍后行为分叉（一处 toast、一处不 toast；一处跨窗口同步、一处忘）。
// key 是下拉的 value：`端点id::模型名`，默认网关是 `::模型名`（见 10-login.js）。
async function applyModelKey(key, opts = {}) {
  const raw = String(key || "");
  const at = raw.indexOf("::");
  const endpoint = at >= 0 ? raw.slice(0, at) : "";
  const model = at >= 0 ? raw.slice(at + 2) : raw;
  if (!model) return null;
  try {
    const r = await api("/api/model", { model, endpoint });
    S.model = r.model;
    S.endpoint = r.endpoint || "";
    if (typeof renderModels === "function") renderModels();
    if (typeof dtRenderModel === "function") dtRenderModel();
    // 另一个窗口的下拉还停在旧值上，用户会以为没生效 —— 广播一声，让对面
    // 直接改内存里的状态重画，不重新请求（两个窗口都请求一遍没必要）。
    if (typeof boardSendModelChanged === "function") boardSendModelChanged(r);
    if (!opts.quiet) {
      if (r.warning) toast(r.warning, true);
      else toast("已切换模型：" + r.model);
    }
    return r;
  } catch (err) {
    if (typeof renderModels === "function") renderModels();
    if (typeof dtRenderModel === "function") dtRenderModel();
    if (!opts.quiet) toast("切换失败：" + err.message, true);
    return null;
  }
}

$("modelSel").addEventListener("change", (e) => applyModelKey(e.target.value));

// ---------------------------------------------------------------------------
// 工作区
// ---------------------------------------------------------------------------
async function loadWorkspace() {
  try {
    const r = await api("/api/workspace");
    S.workspace = r.workspace || "";
    S.memoryPath = r.memory_path || "";
    S.trees = r.trees || (S.workspace ? [S.workspace] : []);
    S.labels = r.labels || {};
    if (r.sessions) S.sessions = r.sessions;
    if (r.active !== undefined) {
      S.activeSid = r.active ? tabKey(r.active, S.workspace) : "";
    }
    renderWorkspace();
    renderWsTabs();
    // 工作区变了 → 输入区可能要从"置灰"恢复成可用（或者反过来）
    if (typeof syncComposerEnabled === "function") syncComposerEnabled();
    // 终端同样换成这个工作区自己的那一批。这里是登录 / 刷新这条路径（后端可能
    // 已经把活动根换成别的了），切工作区那条走 applyWorkspace 里的同一个函数。
    if (typeof termFollowWorkspace === "function") termFollowWorkspace(S.workspace);
  } catch (e) { /* 忽略 */ }
}

// ＋ = 两条路：本地目录（选完直接切过去），或登记一个远程 SSH 工作区
$("wsAdd").addEventListener("click", (ev) => {
  // 必须拦住冒泡：document 上挂着 `click -> hideCtx`，菜单刚显示、
  // 这次点击继续冒到 document 就又被收起来了 —— 表现就是"点 ＋ 没反应"。
  ev.stopPropagation();
  ctx.innerHTML = "";
  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  item("打开本地目录…", () => openWorkspacePicker());
  item("添加远程 SSH 工作区…", () => {
    if (typeof dtAddRemoteDialog !== "function") {
      toast("当前版本没有远程工作区功能", true);
      return;
    }
    dtAddRemoteDialog(async (r) => {
      // 刚登记的远程：塞进顶部标签栏并切过去。它的 .ai 落在本地
      // .ai/remote/<hash>，会话 / TODO / 插件数据都跟着那一份走。
      await addTree(r.ws, true);
      await activateTree(r.ws);
    });
  });
  // 当前就在远程工作区：也给一个显眼的"设置"入口，省得去顶部标签上右键找
  if (String(S.workspace || "").startsWith("ssh://")
      && typeof dtAddRemoteDialog === "function") {
    item("当前 SSH 工作区设置（目录 / ripgrep 路径）…", () => {
      dtAddRemoteDialog(async (r) => {
        if (!r || !r.ws || r.ws === S.workspace) return;
        await removeTree(S.workspace, true);
        await addTree(r.ws, true);
        await activateTree(r.ws);
      }, { ws: S.workspace });
    });
  }
  ctx.classList.remove("hidden");
  const anchor = ev.currentTarget.getBoundingClientRect();
  const box = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(anchor.left,
    window.innerWidth - box.width - 8) + "px";
  ctx.style.top = Math.min(anchor.bottom + 4,
    window.innerHeight - box.height - 8) + "px";
});

/**
 * 目录选择器：在 fs_roots 里随便导航，选中一个目录。
 *
 * 抽出来是因为有三处要用它：切活动根、加一个工作区、在标签上右键换目录。
 * 以前只有"切换工作区"一种用途，整个弹窗写死在调用点里。
 */
/**
 * 清一下粘进来的路径。
 *
 * 只管两件事：**首尾空格**和**成对引号**。第二件是真会遇到的 ——
 * 资源管理器「复制路径」给的是 `"D:\workspace\demo"`，带着引号粘进来，
 * 不剥掉的话会变成路径里合法但不存在的字符，报错还看不出为什么。
 *
 * 不动末尾的分隔符：`C:\` 剥成 `C:` 在 Windows 上会变成"当前盘的工作目录"，
 * 是个能把人送走的坑。
 */
function cleanPath(v) {
  let s = String(v == null ? "" : v).trim();
  if (s.length >= 2) {
    const q = s[0];
    if ((q === '"' || q === "'") && s[s.length - 1] === q) s = s.slice(1, -1).trim();
  }
  return s;
}

/**
 * 把用户输入的「文件名」洗干净（只保留文件名，不带目录）。
 *
 * 去掉路径分隔符和 Windows 不允许的字符 —— 粘一整条路径进来时，取最后一段
 * 才是用户真正想要的名字，而不是在目录里再套一层目录。
 * 结尾的点和空格也要去掉：Windows 上 `a.` 和 `a ` 会被系统悄悄改名。
 */
function cleanFileName(val) {
  let s = String(val == null ? "" : val).trim();
  s = s.split(/[\\/]/).pop() || "";
  s = s.replace(/[:*?"<>|]/g, "");
  return s.replace(/[. ]+$/g, "");
}

function pickDirectory(opts) {
  const body = document.createElement("div");
  const hint = document.createElement("div");
  hint.style.cssText = "font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:8px";
  hint.textContent = opts.hint || "";
  // 路径**可以直接粘**。一层层点进去找深层目录太慢，而"在资源管理器里
  // 复制路径、粘过来"才是最自然的动作 —— 注意复制出来的会自带一对引号。
  const pathRow = document.createElement("div");
  pathRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px";
  const crumb = document.createElement("input");
  crumb.type = "text";
  crumb.className = "crumb";
  crumb.id = "wpCrumb";
  crumb.spellcheck = false;
  crumb.autocomplete = "off";
  crumb.placeholder = "直接粘路径，回车跳过去";
  crumb.style.cssText = "flex:1;min-width:0;box-sizing:border-box;font-size:12px;"
    + "font-family:var(--mono);padding:4px 8px";
  // 已经打开过的工作区做成下拉候选，省得记路径
  const dl = document.createElement("datalist");
  dl.id = "wpRecent";
  for (const t of (S.trees || [])) {
    const o = document.createElement("option");
    o.value = t;
    dl.appendChild(o);
  }
  crumb.setAttribute("list", "wpRecent");
  const go = document.createElement("button");
  go.className = "tbtn mini";
  go.textContent = "去";
  go.title = "跳到这个路径";
  pathRow.appendChild(crumb);
  pathRow.appendChild(go);
  pathRow.appendChild(dl);

  const list = document.createElement("div");
  list.id = "wpList";
  body.appendChild(hint);
  body.appendChild(pathRow);

  // 可选：顺便给文件起个名（导出日志用）。不传 `fileName` 就不出现这一行 ——
  // 切工作区那种「只选目录」的场景不受影响。
  let nameInput = null;
  if (opts.fileName !== undefined) {
    const nameRow = document.createElement("div");
    nameRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;align-items:center";
    const lb = document.createElement("span");
    lb.textContent = "文件名";
    lb.style.cssText = "font-size:12px;color:var(--muted);flex:0 0 auto";
    nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "crumb";
    nameInput.spellcheck = false;
    nameInput.autocomplete = "off";
    nameInput.value = String(opts.fileName || "");
    nameInput.style.cssText = "flex:1;min-width:0;box-sizing:border-box;font-size:12px;"
      + "font-family:var(--mono);padding:4px 8px";
    // 回车 = 确认：和上面路径框一样，改完名字直接敲回车就能存
    nameInput.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      pick.click();
    });
    nameRow.appendChild(lb);
    nameRow.appendChild(nameInput);
    // 直接 append：`list` 这时还没挂到 body 上，用 insertBefore(…, list)
    // 会抛 NotFoundError（参照节点不是子节点），点导出就是"毫无反应"。
    body.appendChild(nameRow);
  }

  body.appendChild(list);

  const foot = document.createElement("div");
  const cur = document.createElement("span");
  cur.style.cssText = "font-size:12px;color:var(--muted)";
  const sp = document.createElement("span");
  sp.className = "spacer";
  const pick = document.createElement("button");
  pick.className = "tbtn";
  pick.textContent = opts.label || "使用当前目录";
  foot.appendChild(cur);
  foot.appendChild(sp);
  foot.appendChild(pick);

  let here = "";

  async function load(p) {
    try {
      const q = new URLSearchParams({ scope: "roots", dirs_only: "1" });
      if (p) q.set("path", p);
      const r = await api("/api/fs/list?" + q.toString());
      here = r.path || p || "";
      cur.textContent = shortPath(here, 3);
      // 回填**后端整理过的**路径：`D:/a//b/` 会变成 `D:\a\b`
      crumb.value = here;
      list.innerHTML = "";
      if (r.parent) list.appendChild(mkRow("⬆", "..", () => load(r.parent)));
      for (const en of r.entries) {
        if (!en.is_dir) continue;
        list.appendChild(mkRow("📁", en.name, () => load(en.path)));
      }
    } catch (e) {
      // 别把输入框清掉 —— 路径打错了要能就地改，全选着等用户重打
      toast("这个路径打不开：" + e.message, true);
      crumb.focus();
      crumb.select();
    }
  }

  /** 跳到输入框里那个路径。输入框和「去」都走它。 */
  function jump() {
    const p = cleanPath(crumb.value);
    if (!p) return;
    load(p);
  }

  go.addEventListener("click", jump);
  crumb.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    jump();
  });

  function mkRow(ico, name, onclick) {
    const d = document.createElement("div");
    d.className = "node";
    d.innerHTML = '<span class="arrow"></span><span class="ico"></span><span class="nm"></span>';
    d.querySelector(".ico").textContent = ico;
    d.querySelector(".nm").textContent = name;
    d.addEventListener("click", onclick);
    return d;
  }

  pick.addEventListener("click", async () => {
    // 以输入框为准：可能刚粘进来、还没按回车 —— 那样也应当能直接确认
    const target = cleanPath(crumb.value) || here;
    if (!target) { toast("还没有选择目录", true); return; }

    let fname = "";
    if (nameInput) {
      fname = cleanFileName(nameInput.value);
      if (!fname) { toast("文件名不能为空", true); nameInput.focus(); return; }
      // 没写扩展名就补 .log：导出的日志多半是拿来双击看 / 贴给别人
      if (!/\.[A-Za-z0-9]{1,8}$/.test(fname)) fname += ".log";
    }
    closeModal();
    await opts.onPick(target, fname);
  });

  openModal(opts.title || "选择目录", body, foot);
  load(opts.start || "");
}

/** 顶部的 ＋：切工作区（顺带把它加进列表）。 */
function openWorkspacePicker() {
  pickDirectory({
    title: "切换工作区",
    hint: "会话、工作区记忆都在这个目录的 .ai/ 下面 —— 换一个就是整套换过去。\n"
        + "路径可以直接粘（上面那个框），不用一层层点。\n"
        + "选中的目录会自动加进顶部那排标签（不会移除已有的）。\n"
        + "（模型是全局的，不会跟着换）",
    label: "切到这个目录",
    // 从当前工作区起步：输入框里就有东西可以改，而不是空着让人从盘符找起。
    // 要换到别处就把它改掉 / 粘一条新的进来。
    start: S.workspace || "",
    onPick: (path) => activateTree(path),
  });
}

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------
async function loadSessions() {
  try {
    const r = await api("/api/sessions");
    S.sessions = r.sessions || [];
    S.activeSid = r.active ? tabKey(r.active, S.workspace) : "";
  } catch (e) { /* 忽略 */ }
}

$("sessBtn").addEventListener("click", () => {
  // 弹窗打开这一刻的工作区：**整份清单都属于它**。
  // 弹窗开着期间用户可能切了工作区（`S.workspace` 已经变了），这时点一行如果
  // 还按"当前工作区"建标签，就会把 A 项目的会话开成 B 项目的标签 —— 两个工作区
  // 都有 s1 时，消息会直接写进 B 的同名会话（"p60 的对话串到 jv"就是这么来的）。
  const dlgWs = S.workspace || "";
  const body = document.createElement("div");
  const foot = document.createElement("div");
  const add = document.createElement("button");
  add.className = "tbtn";
  add.textContent = "＋ 新建会话";
  foot.appendChild(add);
  // 界面上**不提供删除**（用户要求）：删会话是不可逆的 os.remove，
  // 误点就没了；要删自己去 `<工作区>/.ai/sessions/` 里删，正文下面有提示。

  // -----------------------------------------------------------------------
  // 两个表（用户要求）：
  //   表 1「当前会话」—— 标签栏里正开着的，叉掉就从这个表消失；
  //   表 2「历史会话」—— 后端全量会话，点一下就打开到表 1。
  // 表 2 的存在意义：叉掉 ≠ 删除。会话内容一直在 sessions.json 里，
  // 只是不再占着标签栏，想看就在这儿点回来。
  // -----------------------------------------------------------------------
  function render() {
    body.innerHTML = "";
    const openList = openTabsOfWs();
    const openSids = openList.map((x) => x.sid);

    const mkRow = (s, opened) => {
      const d = document.createElement("div");
      d.className = "node" + (s.active ? " sel" : "");
      d.innerHTML = '<span class="arrow"></span><span class="ico">' +
        (opened ? "🗂" : "💬") + '</span><span class="nm"></span>';
      d.querySelector(".nm").textContent =
        s.title + "  （" + s.messages + " 条 · " + fmtNum(s.tokens) + " tk）";
      d.title = opened ? "已在标签栏打开，点击切换" : "点击打开到标签栏";
      d.addEventListener("click", () => openSessionInto(s, opened));
      return d;
    };

    // ---- 表 1 ----
    const h1 = document.createElement("div");
    h1.className = "sess-sec-title";
    h1.textContent = "当前会话（" + openList.length + "）";
    body.appendChild(h1);

    for (const o of openList) {
      const s = (S.sessions || []).find((x) => x.id === o.sid) || {
        id: o.sid, title: o.title || o.sid, messages: 0, tokens: 0, active: false,
      };
      body.appendChild(mkRow(s, true));
    }
    if (!openList.length) body.appendChild(dimLine("标签栏是空的，从下面「历史会话」里点一个打开"));

    // ---- 表 2 ----
    const rest = (S.sessions || []).filter((s) => !openSids.includes(s.id));
    const h2 = document.createElement("div");
    h2.className = "sess-sec-title";
    h2.textContent = "历史会话（" + rest.length + "）";
    body.appendChild(h2);
    for (const s of rest) body.appendChild(mkRow(s, false));
    if (!rest.length) body.appendChild(dimLine("没有其它会话了"));
  }

  function dimLine(text) {
    const d = document.createElement("div");
    d.className = "sess-sec-empty";
    d.textContent = text;
    return d;
  }

  // 当前工作区开着的标签（按标签栏顺序）。以**弹窗打开时**那个工作区为准。
  function openTabsOfWs() {
    const out = [];
    for (const key of S.tabOrder) {
      const t = S.tabs[key];
      if (!t || (t.ws || "") !== dlgWs) continue;
      out.push({ sid: t.sid, title: t.title });
    }
    return out;
  }

  // 点一个会话：已开着就直接切过去，没开着就建标签再切。
  async function openSessionInto(s, opened) {
    const key = tabKey(s.id, dlgWs);
    if (opened && S.tabs[key]) { closeModal(); switchTabByUser(key); return; }
    try {
      // 标签**必须挂在这个会话自己的工作区**上：`ensureTab` 不传 ws 会按当前
      // 工作区兜底，弹窗开着期间切过工作区就会挂错（见 dlgWs 的注释）。
      const t = ensureTab(s.id, tabTitleFor(s.id), dlgWs);
      // 顺序很重要：先把历史拉下来、**在还隐藏着的面板里**渲染完，最后才切显示。
      // 反过来的话用户会先看到一个空面板、再"啪"地被几百个节点糊满。
      if (t && !t.rendered) {
        const ok = await paintHistoryInto(t);
        if (!ok) {                         // 拉失败就退回老路：让后端切 active 再试一次
          await api("/api/sessions/select", { id: s.id }, { ws: dlgWs });
          await paintHistoryInto(t);
        }
      }
      const r = await api("/api/sessions/select", { id: s.id }, { ws: dlgWs }).catch(() => null);
      if (r) S.sessions = r.sessions || S.sessions;
      S.activeSid = t ? t.key : key;
      closeModal();
      switchTabByUser(S.activeSid);
      refreshUsage((r && r.usage) || {});
    } catch (e) { toast("切换失败：" + e.message, true); }
  }

  // 新建会话：**必须**走 newSessionInWorkspace（25-tabs.js）这一份实现。
  // 以前这里另写了一份 `api("/api/sessions/new", {})`：只带默认活动根，
  // 多工作区并行时会把会话建到别的工作区、标签却挂在当前工作区上 ——
  // 两个地方各写一遍迟早跑偏，所以统一收口到那一个函数。
  add.addEventListener("click", async () => {
    closeModal();
    const t = await newSessionInWorkspace();
    if (t) toast("已新建会话");
  });

  render();
  // 删除入口在右键菜单里（标签栏 / 任务台列表），这里是"去哪删、删完文件在哪"的说明。
  const tip = dimLine("删除：右键会话标签（或任务台列表里的会话）→ 删除会话。"
    + "文件只是挪到「<工作区>\\.ai\\sessions\\old」，列表里消失但还能捞回来；"
    + "想彻底删就自己去那个文件夹删。");
  tip.style.whiteSpace = "pre-wrap";
  body.appendChild(tip);
  body.appendChild(foot);
  openModal("会话管理", body, null);
});

