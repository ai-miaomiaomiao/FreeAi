// 文件树 / 拖拽 / 右键菜单
// 从 index.html 拆出（原第 2256-2941 行）。
// ---------------------------------------------------------------------------
// 文件树
// ---------------------------------------------------------------------------
const treeBox = $("tree");
/**
 * 当前**活动**那棵树的展开集合（按绝对路径记）。
 *
 * 它只描述"现在左栏里这一棵"。每个工作区各有一份展开状态，存在
 * `TREE_DOM_CACHE` 的槽位里；换根时先 stash 旧的、再 restore 新的（见 loadTree）。
 * 以前这里是"全局唯一一份"，于是 A 里展开的目录切到 B 会被原样套上去 ——
 * B 里同路径的目录莫名其妙自己展开。
 */
const expanded = new Set();

// ---------------------------------------------------------------------------
// 每个工作区各缓存一棵树：切回去直接挂回来，不再列盘
// ---------------------------------------------------------------------------
//
// 为什么要有这个：切工作区原来是**每次都从零列盘** —— 先 `treeBox.innerHTML=""`
// 再逐层 renderDir 打 /api/fs/list，远程工作区每一层都是一趟 SSH。于是
// "点一下标签"到"左栏出现目录"之间整块是空白，人感觉就是整个界面卡住。
//
// 缓存的是**真的 DOM 节点**，不是数据：挂回来时事件监听器、展开状态、悬浮
// 提示（title 里的大小 / 修改时间）全都原样，代价只有一次 appendChild。
//
// 只有"确实换了个根"才用命中（见 loadTree 的 same 判断）：同一个工作区里
// 新建 / 删除 / 重命名 / 上传 / 拖动之后都会调 loadTree()，那些必须重列，
// 命中缓存会让左栏停在上一次的样子。
// 键 = treeSlotKey(ws, showHidden)：**同一个工作区按隐藏开关分两份**。
//
// 以前键只有 ws，`showHidden` 只当命中条件用 —— 结果就是各工作区开关不一致时
// （web 开着隐藏、gu/jv 没这个字段=false）来回切，每一次都判定"开关对不上"，
// 每次走完整重列。用户看到的就是"明明说好了加载到内存，怎么还是重新加载"。
// 分键之后两份 DOM 各存各的，切过去按目标工作区的开关取那一份，必然命中。
const TREE_DOM_CACHE = new Map();   // key -> { nodes: Node[], expanded: Set, showHidden: bool }
/** 缓存槽位的键：工作区 + 隐藏开关（n=不显示隐藏，h=显示隐藏）。 */
function treeSlotKey(ws, hidden) {
  return String(ws || "") + "\u0000" + (hidden ? "h" : "n");
}
/** 把一个工作区在缓存里的**所有**变体都删掉（折叠全部目录 / 工作区被移除时用）。 */
function dropTreeSlots(ws) {
  const pre = String(ws || "") + "\u0000";
  for (const k of Array.from(TREE_DOM_CACHE.keys())) {
    if (k === ws || k.startsWith(pre)) TREE_DOM_CACHE.delete(k);
  }
}
/** 取某个工作区在缓存里的槽位：优先要 hidden 指定的那份，没有就任取一份。
 *  返回的对象里 `_key` 是真实键，`showHidden` 是这份 DOM 自己按哪个开关画的。 */
function findTreeSlot(ws, hidden) {
  const want = TREE_DOM_CACHE.get(treeSlotKey(ws, hidden));
  if (want) return want;
  const pre = String(ws || "") + "\u0000";
  for (const [k, v] of TREE_DOM_CACHE) {
    if (k.startsWith(pre)) return v;
  }
  return null;
}
let treeStashKey = "";              // treeBox 里这棵树属于哪个工作区（画到一半被切走时为 ""）
// treeBox 里这棵树**是按哪个隐藏开关画的**。null = 不知道（刚初始化 / 还没画过）。
//
// 为什么不能直接用 S.showHidden 当记录值：切工作区时 20-workspace.js 会先把
// S.showHidden 改成**目标工作区**的值，之后才调 loadTree；那一轮 stash 里的
// `!!S.showHidden` 已经是新工作区的开关，而 DOM 还是旧开关画的 —— 存进槽位的
// 标记和内容对不上，下次再切回来又判 miss。必须记「画的时候的开关」。
let treeStashHidden = null;
let treeLoadSeq = 0;                // 并发 loadTree 只认最后一次（快速连点两个工作区）
let treeShownKey = "";              // 最近一次「用缓存把树挂回去」的根
let treeShownAt = 0;                // 以及那一刻的时间戳（见 loadTree 开头那个早退）
// 「刚用缓存挂好，还没被 loadTree 认领」。mountTreeFromCache 置位、loadTree 开头消费，
// 用来挡住**这一次**切换里的重复重列 —— 不能靠上面那个 800ms 时间窗：
// 后端慢的时候一轮切换就可能超过 800ms，那会把刚挂好的树整个摘下来重列一遍，
// 正是要消掉的那段卡顿。
let treeMountPending = "";          // 刚用缓存挂好、还没走完这一轮切换的工作区

/** 把 treeBox 里现在这棵树存进它所属工作区的槽位（按**画它时**的开关分键）。 */
function stashTreeDom() {
  if (!treeStashKey) return;
  // 用 treeStashHidden（画这棵树时的开关）而不是 S.showHidden：切工作区时
  // S.showHidden 已经被改成目标工作区的值了，用它会把旧开关画的 DOM 存进
  // 新开关的槽位，两边都污染（见 treeStashHidden 的注释）。
  const hidden = (treeStashHidden !== null) ? !!treeStashHidden : !!S.showHidden;
  const key = treeSlotKey(treeStashKey, hidden);
  // 展开集合和槽位里原有的取并集：重列时"恢复展开"是 setTimeout 里逐个 click 的，
  // 结尾这次 stash 跑在它们之前 —— 直接覆盖会把刚恢复的那几层漏掉，
  // 下次「刷新文件树」就整棵折回去了。
  // （「折叠全部目录」是先 delete 槽位再 loadTree，prev 为空，不会被这里救回来。）
  const prev = TREE_DOM_CACHE.get(key);
  const exp = new Set(expanded);
  if (prev && prev.expanded) for (const p of prev.expanded) exp.add(p);
  TREE_DOM_CACHE.set(key, {
    nodes: Array.from(treeBox.childNodes),
    expanded: exp,
    showHidden: hidden,
  });
  treeStashHidden = hidden;
}

/**
 * **同步**把某个工作区的树挂回左栏（不 await、不发请求）。
 *
 * 为什么单独一个函数、而不是复用 loadTree()：loadTree 里那一串 await
 * （远程工作区的 /api/ssh/workspace、逐层 renderDir）意味着"点了切工作区"
 * 到"左栏真的变过来"之间隔着至少一次后端往返。反复切的时候这个空档被
 * 放大成"整个界面卡住不动"，人就接着点，请求一路堆积。
 *
 * 这里只做纯 DOM 搬运：拿缓存的节点 appendChild 回去，监听器、展开状态、
 * 悬浮提示全都原样。缓存里没有就返回 false —— 那种情况交给随后的
 * loadTree 正常重列，不做半吊子挂载。
 *
 * 隐藏开关对不上**不再算 miss**：同一个工作区按开关存了两份变体
 * （见 treeSlotKey），这里自动挑与 S.showHidden 一致的那份；只有一份时
 * 就用它、并把 S.showHidden 同步成那份的开关 —— 挂上去的 DOM 和开关必须一致，
 * 否则"显示隐藏文件"按钮的状态和画面会互相矛盾。
 */
function mountTreeFromCache(ws, opts) {
  if (!ws) return false;
  // force = 「换开关重挂」：调用方（切工作区读到目标存档之后）已经知道
  // 左栏挂的是同一个工作区、但开关是旧的，要求换成当前开关那份。
  // 这时**只认精确变体**，不能回退（回退会挂出一棵开关不对的树）。
  const force = !!(opts && opts.force);
  const wantHidden = !!S.showHidden;
  // 返回 true 的语义统一成「左栏现在就是该显示的样子，别再重列」——
  // 「已经是它了」也算 true：调用方（如 toggleTreeHidden）拿到 false 会去
  // loadTree 重列，把"本来就已经对"的情况判成 miss 就又要白列一遍整棵树。
  if (!force) {
    if (treeStashKey === ws && treeBox.childNodes.length) return true;  // 已经是它了
  } else if (treeStashKey === ws && treeStashHidden === wantHidden
             && treeBox.childNodes.length) {
    return true;   // 已经是对的那份了
  }
  const slot = force ? TREE_DOM_CACHE.get(treeSlotKey(ws, wantHidden))
                     : findTreeSlot(ws, wantHidden);
  if (!slot || !slot.nodes || !slot.nodes.length) return false;

  // 现在左栏这棵先存回它自己的槽位（切走时不能丢掉最新的展开状态）
  if (treeStashKey) stashTreeDom();
  treeStashKey = "";
  treeBox.innerHTML = "";
  // force 模式下**不换展开状态**：同一棵树、只是换开挂，expanded 记的还是
  // 这个根的那一套路径，换掉反而会把用户展开的层丢掉。
  if (!force) {
    // 换根：expanded 里装的是上一个根的路径，套到新根一个都匹配不上，先清。
    expanded.clear();
    // 槽里的 expanded 正常一定是个 Set（只有用户真打开过的树才进槽）。
    // 仍然判一下：万一槽是旧版本代码留下的（曾经有过只列根一层的预热件），
    // 挂上去就是"根这一层、全折叠"，别拿它去覆盖用户的展开集合。
    const slotExp = slot.expanded;
    if (slotExp instanceof Set) {
      for (const p of slotExp) expanded.add(p);
    }
  }
  for (const n of slot.nodes) treeBox.appendChild(n);

  treeStashKey = ws;
  treeStashHidden = !!slot.showHidden;
  // force 只认精确变体，理论上 switch 不会进来；非 force 时回退到的那份可能开关
  // 对不上，那就同步 S.showHidden 并重绘按钮 —— 挂上去的 DOM 和开关必须一致，
  // 不能让「隐藏文件」按钮亮着、画面里却没有那些文件。
  if (!!S.showHidden !== !!slot.showHidden) {
    S.showHidden = !!slot.showHidden;
    if (typeof paintTreeHidden === "function") { try { paintTreeHidden(); } catch (e) { } }
  }
  S.treeRootPath = ws;
  // 挂上来的是一棵（可能属于别的开关变体的）新 DOM：跳转条如果正开着，
  // 命中集得按这棵重算，不然高亮留在已经被摘掉的那批节点上，看着像"不亮了"。
  treeFindRefresh();
  // 这两个标记让紧接着的 loadTree 直接早退（见它开头那个 early return），
  // 否则又会把刚挂好的 DOM 摘下来重列一遍。
  treeShownKey = ws;
  treeShownAt = Date.now();
  // **不消费、只标记**：同一次切换里 loadTree 会被调好几次（applyWorkspace 一次、
  // switchView 的 files setter 一次），全都要挡住；等这一轮切换走完由
  // applyWorkspace 清掉（同工作区内新建/删除后的重列不能被它挡住）。
  treeMountPending = ws;
  // 作废在途的那次 loadTree：它回来时挂的是上一个工作区的树
  treeLoadSeq++;
  // 挂的是缓存，磁盘上可能已经变了：等**真正落地**之后静默把根这一层对一遍。
  //
  // 不能直接用 `S.workspace !== ws` 判断：挂树的这一刻 /api/workspace 还没回来，
  // S.workspace 仍是旧值，那样这次纠偏会被立刻放弃，缓存就永远不跟磁盘对账了
  // （缓存到点不纠偏 = 别人改过的文件在左栏一直显示旧大小 / 旧名字）。
  // 所以：等这一轮切换有了结论（treeMountPending 被清掉或被下一个目标顶掉）
  // 再动手，最多等 3 秒。
  let waited = 0;
  const tryFix = () => {
    if (treeStashKey !== ws) return;                    // 期间又切走了
    const settled = (treeMountPending !== ws) || (S.workspace === ws);
    if (!settled && waited < 3000) {
      waited += 100;
      setTimeout(tryFix, 100);
      return;
    }
    treeReloadRootQuietly(ws, false).catch(() => { });
  };
  // 独立定时器，不复用别的队列的 timer —— 那些队列会 clearTimeout 自己的，
  // 顺手把这里的纠偏一并掐掉。
  setTimeout(tryFix, 0);
  return true;
}

/** 左栏现在挂的是哪棵树：`{ ws, hidden }`（hidden 为 null = 还没画过/不知道）。
 *
 *  给 20-workspace.js 用：切工作区读到目标存档后，要判断"挂着的这棵是不是
 *  同一个工作区、但按旧开关画的"。那边**不能直接** typeof 查 treeStashHidden ——
 *  它和这个文件是分开的 <script>，前者若先加载，`let` 还在 TDZ 里，
 *  typeof 会抛 ReferenceError。走函数就只在运行时取值，顺序无所谓。
 */
function currentTreeMount() {
  return { ws: treeStashKey, hidden: treeStashHidden };
}

/** 这一轮切换走完了：解除「刚挂好别再重列」的锁定。
 *
 *  不清的话，之后同工作区里新建 / 删除 / 上传后的 loadTree 会一直被它挡住，
 *  左栏停在上一次的样子（比不缓存还糟）。
 */
function clearTreeMount() {
  treeMountPending = "";
}

/** 把文件树面板重画一遍 —— 当前工作区那一棵，外加下面那个「远程」分组。
 *
 *  `force` 透传给 renderDir = 绕过远程目录的 8 秒短缓存。只有
 *  「刷新文件树」这种"我就是要看最新"的入口传 true；切换工作区、
 *  建完文件重画之类不传（走缓存，省掉一批重复的远程往返）。
 */
async function loadTree(force) {
  // 这一轮切换刚开始、树是用缓存挂上去的：本次调用直接跳过重列。
  // 注意判的是 `treeMountPending` 而不是 `S.workspace` —— 点击瞬间挂树时
  // `/api/workspace` 还没回来，`S.workspace` 仍是旧值；那时候放过去就会拿
  // **旧工作区**从头列一遍盘（正是"点了没反应"的来源）。
  // 标记由 applyWorkspace 收尾时清掉（见 clearTreeMount），所以同工作区
  // 内新建 / 删除后的重列不受影响。
  if (!force && treeMountPending && treeStashKey === treeMountPending
      && treeStashHidden === !!S.showHidden) {
    treeFindRefresh();     // 树没重列，但筛选高亮得跟着当前 DOM 走（见文件末尾）
    return;
  }
  // 同一次切换里 loadTree 会被调两遍（applyWorkspace 自己一遍 + switchView 的
  // 文件视图 setter 一遍）。刚用缓存把树挂回去的话，第二遍纯属把刚挂好的 DOM
  // 摘下来再挂一遍 —— 直接早退。
  if (!force && treeStashKey === S.workspace && treeStashKey
      && treeStashKey === treeShownKey && (Date.now() - treeShownAt) < 800
      && treeBox.childNodes.length
      && treeStashHidden === !!S.showHidden) {
    treeFindRefresh();
    return;
  }
  const seq = ++treeLoadSeq;
  const root = S.workspace;
  // 当前左栏里这棵树先存回它自己的槽位 —— 不管接下来是重列它、还是换成
  // 另一个工作区（切走时要把 A 最新的展开状态留下，不能还留着上次的快照）。
  // 画到一半被切走时 treeStashKey 是 ""，这半棵不能当成"这个工作区的树"存下来。
  const oldKey = treeStashKey;
  if (oldKey) stashTreeDom();
  treeStashKey = "";
  treeBox.innerHTML = "";
  // 哪些目录是展开的，重列后恢复，免得刷新一下全折回去
  const keep = new Set(expanded);
  expanded.clear();

  renderWorkspace();          // 顺手把面板上那个小标签刷了
  if (!root) {
    treeBox.appendChild(emptyBox("还没有工作区\n点顶部的 ＋ 选一个目录"));
    treeFindRefresh();
    return;
  }

  // 槽位按**当前开关**取；没有精确那份就取该工作区任一变体（用来拿展开状态）。
  // 注意命中缓存那一段（下面 ③）仍然要求开关精确一致，所以取到别的变体
  // 只会影响 keep 的恢复，不会挂出一棵开关不对的树。
  const slot = findTreeSlot(root, !!S.showHidden);
  // 从别的根切过来时，展开状态要用**目标工作区自己**那份：keep 这时候装的是
  // 上一个工作区的路径，套到新根上一个都匹配不上（等于白白丢掉用户的展开）。
  if (oldKey && oldKey !== root && slot && slot.expanded instanceof Set) {
    keep.clear();
    for (const p of slot.expanded) keep.add(p);
  }
  // 命中条件（缺一条都不行，写清楚免得以后又踩）：
  //   ① **真的换了个根**（oldKey !== root）。同一个工作区里新建 / 删除 / 重命名 /
  //      上传 / 拖动之后都会调 loadTree()，那些要的是重列；命中缓存会让左栏停在
  //      改之前的样子，比不缓存还糟。
  //   ② 不是「刷新文件树」那种强制刷新。
  //   ③ 隐藏开关精确一致（变了就得按新开关重列）。
  //   ④ 当前正停在文件视图上（停在别的视图时树是 hidden 的，直接复用看不出问题；
  //      但从那边切回来时 onshow 会重拉，别让缓存把它糊住）。
  if (!force && oldKey !== root && slot && slot.expanded instanceof Set
      && slot.showHidden === !!S.showHidden && curView === "files") {
    // 先把展开集合换成**这个工作区自己**那份：keep 用来恢复「本地数据」行的展开，
    // 后面挂回来的 DOM 节点本身就是展开着的，不用再逐个 click。
    for (const p of slot.expanded) expanded.add(p);
    // 「本地数据」那行（远程工作区才有）每次重建：它要问一次 /api/ssh/workspace
    // 拿目录名，本地接口几毫秒，重建比复用更省心（缓存里那份带 localData 标记，跳过）
    if (String(root).startsWith("ssh://")) await renderLocalData(new Set(expanded), force);
    if (seq !== treeLoadSeq) return;
    for (const n of slot.nodes) {
      if (n.dataset && n.dataset.localData) continue;
      treeBox.appendChild(n);
    }
    treeStashKey = root;
    treeStashHidden = !!slot.showHidden;
    S.treeRootPath = root;
    treeShownKey = root;
    treeShownAt = Date.now();
    // 静默纠偏：缓存可能是几分钟前列的，磁盘上已经变了。不 await、不阻塞任何东西 ——
    // 拉回来只换掉根这一层的差异，没变就什么都不做。
    treeReloadRootQuietly(root, force).catch(() => { /* 纠偏失败不影响已显示的树 */ });
    treeFindRefresh();
    return;
  }

  // 远程工作区：顶上先挂一行「本地数据」目录，再画远程那棵树
  await renderLocalData(keep, force);
  if (seq !== treeLoadSeq) return;          // 已经被下一次 loadTree 顶掉了

  const section = document.createElement("div");
  // 记下这棵是哪个根 —— 空白处右键时靠它决定"在哪个目录里新建"
  section.dataset.root = root;
  treeBox.appendChild(section);
  await renderDir(section, root, 0, keep, force);
  if (seq !== treeLoadSeq) return;

  treeStashKey = root;
  // 这一棵是刚按**当前开关**列的，标记跟着一起定，stash 时分键才不会错。
  treeStashHidden = !!S.showHidden;
  S.treeRootPath = root;
  try { await api("/api/state", { treeRoot: S.treeRootPath }); } catch (e) { /* 忽略 */ }
  // 重列完了把这份新树存进槽位：不然缓存里留的还是"改之前"的样子
  stashTreeDom();
  // 清掉「刚用缓存挂回去」的标记：后续 800ms 内的 loadTree（新建 / 删除 / 上传后
  // 重画）必须真的重列，不能被上面那个早退挡住。
  treeShownKey = "";
  // 重列 / 新建 / 删除之后，跳转条里那串字还在：命中集得按新的 DOM 重算
  //（否则高亮停在已经被删掉的行上，按 Enter 跳到空气里）。
  treeFindRefresh();
}

/** 「折叠全部目录」：连内存缓存一起丢，否则切回来挂的是缓存里那棵**展开着的** DOM。
 *  只清 expanded 不够 —— 缓存里的 DOM 节点本身就带着展开的子树。
 *  这个工作区的两个开关变体都要删（只删当前开关那份，切回来换个开关又挂出旧树）。 */
async function foldAllTree() {
  expanded.clear();
  if (treeStashKey) dropTreeSlots(treeStashKey);
  treeStashKey = "";     // 置空后 loadTree 不会把旧的 dome 再 stash 回去
  treeStashHidden = null;
  await loadTree();
}

/** 缓存命中后，悄悄把根这一层和磁盘对一遍（不拦界面）。 */
async function treeReloadRootQuietly(root, force) {
  // 跳过「本地数据」那一块（远程工作区顶上挂的，dataset.root 是本地目录）
  const section = Array.from(treeBox.children)
    .find((x) => x.dataset && x.dataset.root === root && !x.dataset.localData);
  if (!section || !section.firstChild) return;
  const holder = section.firstChild;
  const oldNodes = Array.from(holder.childNodes);
  // 上一次是"读取失败"的提示（没有 _entry）：不纠偏，点重试才重建
  if (oldNodes.some((n) => !n._entry)) return;

  let r;
  try {
    const q = new URLSearchParams({ scope: treeScope(root) });
    if (root) q.set("path", root);
    q.set("compact", "1");
    if (S.showHidden) q.set("hidden", "1");
    if (force) q.set("nocache", "1");
    r = await api("/api/fs/list?" + q.toString(), undefined, { timeout: TREE_TIMEOUT });
  } catch (e) {
    return;                    // 纠偏失败无所谓，用户下次刷新照样能拿到新内容
  }
  if (treeStashKey !== root || S.workspace !== root) return;   // 期间切走了

  const cur = r.path || root;
  // 展开着的目录，它的子容器是**紧跟在这个节点后面的兄弟节点**（见 expandDir）。
  // 重排时必须成对搬：只搬节点不搬容器，展开的内容会跑到别的目录下面去。
  const boxOf = new Map();     // pathKey -> 子容器
  const loose = [];            // 没有 _entry 的节点（不该有，兜底不让它们乱跑）
  for (const n of oldNodes) {
    if (n._owner && n._owner._entry) boxOf.set(treePathKey(n._owner._entry.path), n);
    else if (!n._entry) loose.push(n);
  }
  const byKey = new Map();
  for (const n of oldNodes) {
    if (n._entry) byKey.set(treePathKey(n._entry.path), n);
  }
  const same = r.entries.length === byKey.size
    && r.entries.every((en, i) => {
      const n = byKey.get(treePathKey(en.path));
      return n && oldNodes.indexOf(n) >= 0;
    });
  if (same) return;            // 绝大多数情况：什么都没变，直接走人

  // 只重排根这一层：仍在的条目直接复用原节点（监听器、子容器全都不动），
  // 新出现的补上，没了的摘掉。
  const frag = document.createDocumentFragment();
  for (const en of r.entries) {
    const k = treePathKey(en.path);
    let node = byKey.get(k);
    if (node) byKey.delete(k);          // 用掉就删，剩下的就是"磁盘上已经没了"
    else node = makeNode(en, 0, cur, new Set(expanded));
    // 顺手刷新"大小 / 修改时间"那句提示，不然改过的文件还显示旧大小
    const bits = [];
    if (!en.is_dir) bits.push(fmtSize(en.size));
    const mt = fmtTime(en.mtime);
    if (mt) bits.push("修改于 " + mt);
    node.title = en.path + (bits.length ? "  （" + bits.join(" · ") + "）" : "");
    frag.appendChild(node);             // appendChild 会把它从原位置摘出来
    const box = boxOf.get(k);
    if (box) frag.appendChild(box);     // 子容器紧跟在它的节点后面
  }
  // 剩下还在 holder 里的就是"没了"的节点和它们的子容器（已经被 appendChild
  // 摘走的自然不在），连同残留的杂项一起清掉。
  holder.textContent = "";
  for (const n of loose) frag.appendChild(n);   // 理论上不会有，别默默丢掉
  holder.appendChild(frag);
  // 根这一层换过节点了：跳转条里的命中集得按新的 DOM 重算
  treeFindRefresh();
}

/**
 * 远程工作区顶上那一行「本地数据」。
 *
 * 远程工作区的文件都在服务器上，但笔记 / md 这类东西得存在本地（也方便
 * 版本管理、方便别的工具打开）。所以给每个 ssh:// 工作区配一个本地目录
 * `<应用根>/remote/<远程目录名>`（后端 ssh_workspace.local_dir_for 建的），
 * 在这儿挂成第一行。它走的是本地路径，新建 / 重命名 / 删除全都照常可用。
 */
async function renderLocalData(keep, force) {
  if (!String(S.workspace || "").startsWith("ssh://")) return;
  let local = "";
  try {
    const r = await api("/api/ssh/workspace");
    const hit = ((r && r.workspaces) || []).find((w) => w.ws === S.workspace);
    local = (hit && hit.local_dir) || "";
  } catch (e) {
    return;                      // 老后端没这个字段：不显示，不影响远程树
  }
  if (!local) return;

  const sec = document.createElement("div");
  sec.dataset.root = local;
  // 缓存复用时要靠这个标记认出"这块是临时重建的，别把缓存里那份也挂上来"
  // （见 loadTree 里的命中分支）
  sec.dataset.localData = "1";

  const row = document.createElement("div");
  row.className = "node";
  row.style.paddingLeft = "8px";
  row.innerHTML = '<span class="arrow">▸</span><span class="ico">📝</span>'
                + '<span class="nm"></span>';
  const arrow = row.querySelector(".arrow");
  row.querySelector(".nm").textContent = "本地数据（remote/" + baseName(local) + "）";
  row.title = local + "\n\n这个远程工作区对应的本地目录：笔记 / md 存这儿，不走网络";

  let box = null;
  let open = false;
  row.addEventListener("click", async () => {
    if (open) {
      open = false;
      arrow.textContent = "▸";
      if (box) box.classList.add("hidden");
      return;
    }
    open = true;
    arrow.textContent = "▾";
    if (box) { box.classList.remove("hidden"); return; }
    box = document.createElement("div");
    sec.appendChild(box);
    await renderDir(box, local, 1, keep, force);
  });

  // 右键走本地目录那一套：新建文件 / 新建文件夹 / 复制路径 /
  // 「添加目录到对话（只传路径）」/ 在资源管理器中打开 —— 全是本地路径，都能用。
  // 少了这个监听，右键会落到浏器默认菜单上，用户就只能自己翻路径了。
  row.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openContextMenu(e.clientX, e.clientY,
                    { path: local, name: "本地数据", is_dir: true },
                    local,
                    async () => { await loadTree(); });
  });

  sec.appendChild(row);
  treeBox.appendChild(sec);

  // 定位到这边打开的文件时（treeRevealFile 会把祖先目录塞进 expanded），
  // 这个根也得跟着展开，否则子目录全在折叠的容器里，等于没定位。
  if (keep && keep.has(local)) {
    setTimeout(() => { if (!open) row.click(); }, 0);
  }

  // 和下面的远程树之间留一条分界，否则看起来像一个目录的两段
  const sep = document.createElement("div");
  sep.className = "tree-sep";
  sep.dataset.localData = "1";   // 同上：缓存命中时这一条也要重建，别挂两份
  treeBox.appendChild(sep);
}

/**
 * 这个路径该按哪个 scope 列目录。
 *
 * 主树是"当前工作区之内"（scope=workspace，后端会拿工作区根做约束）；
 * 远程工作区顶上额外挂的「本地数据」目录不在工作区下面，必须走
 * scope=roots（fs_roots 白名单），否则后端直接回"路径不合法"。
 */
function treeScope(path) {
  if (!path) return "workspace";
  const ws = String(S.workspace || "");
  if (!ws) return "roots";
  const norm = (s) => String(s || "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  const p = norm(path), r = norm(ws);
  return (p === r || p.startsWith(r + "\\")) ? "workspace" : "roots";
}

/**
 * 列目录的超时。
 *
 * 为什么不是 00-core.js 的默认 15s：远程工作区一次列目录要走 SSH
 * （握手 → listdir），服务器忙 / 网络抖动时握手单项就能吃掉十几秒。15s 一到
 * 前端先放弃，后端其实还在跑，于是「偶发一次之后一直报」（同源 6 条连接被
 * 卡住的请求占满，后面的全排队到超时）—— 和 git 面板 / gradle 任务列表是
 * 同一个坑。40s 的由来：后端 LIST_TIMEOUT 30s + 握手余量 10s，比后端宽，
 * 保证先看到后端那句明确的「远程列目录超时」，而不是前端这句含糊的。
 * 注意不要改成 0（不限时）：列目录是点一下就出结果的操，卡住就要能放弃。
 *
 * 连接已经有池了（ssh_workspace._POOL），正常第二次起只剩 listdir，
 * 这个超时现在只在真出问题时才会碰到。
 */
const TREE_TIMEOUT = 40000;

/**
 * 列一个目录并画出来。
 *
 * `force` = 不要后端那 8 秒目录短缓存（ssh_workspace.LIST_TTL）。
 * 只有"用户明确要求看最新"的入口才传：刷新文件树、读取失败的重试。
 * 重画恢复展开状态时不传 —— 那批目录刚列过，再列一遍纯属白跑往返。
 */
async function renderDir(container, path, depth, autoExpand, force) {
  const holder = document.createElement("div");
  container.appendChild(holder);
  let r;
  try {
    const q = new URLSearchParams({ scope: treeScope(path) });
    if (path) q.set("path", path);
    // compact=1：把"一路只有一个子目录"的链并成一行（`src\com\android`）。
    // 跟 VS Code 的 explorer.compactFolders 一样 ——
    // Android 那种 src/main/java/com/xxx 一层层点下去要点五次。
    q.set("compact", "1");
    // hidden=1：把 . 开头的也列出来（后端默认是挡掉的）
    if (S.showHidden) q.set("hidden", "1");
    // nocache=1：不走后端那 8 秒目录短缓存（见 renderDir 的 force 说明）
    if (force) q.set("nocache", "1");
    // 第三参 opts 必须给 —— 不传就吃 00-core.js 的默认 15s（见 TREE_TIMEOUT）。
    r = await api("/api/fs/list?" + q.toString(), undefined, { timeout: TREE_TIMEOUT });
  } catch (e) {
    const d = document.createElement("div");
    d.className = "node";
    d.style.color = "var(--danger)";
    // 超时和"路径不存在/没登记"是两回事，不能都写成同一句：
    // 前者是"还没读回来"（稍后重试可能就好），后者是"这里本来就没有"。
    d.textContent = e.timeout
      ? "读取失败：目录读超时（" + Math.round(TREE_TIMEOUT / 1000) + "s），远程可能仍在握手"
      : "读取失败：" + e.message;
    // 失败可恢复：点一下重列这个目录。
    // 不靠"折叠再展开"来重试 —— 那会连带清掉其它已展开的分支。
    const redo = document.createElement("span");
    redo.textContent = "重试";
    redo.style.cssText = "margin-left:8px;text-decoration:underline;cursor:pointer;";
    redo.onclick = (ev) => {
      ev.stopPropagation();
      holder.remove();
      // 重试一律绕过缓存：上一次失败可能就是缓存里那批"旧结果"误导的
      renderDir(container, path, depth, autoExpand, true);
    };
    d.appendChild(redo);
    holder.appendChild(d);
    return;
  }
  const cur = r.path || path;
  for (const en of r.entries) {
    const node = makeNode(en, depth, cur, autoExpand);
    holder.appendChild(node);
    // 之前是展开状态的话，重载后自动展开回去。
    // 用 treePathKey 比：expanded 里存的路径可能来自另一处写法（大小写 /
    // 正反斜杠），严格 has() 在远程工作区上永远不中。
    if (en.is_dir && autoExpand) {
      const k = treePathKey(en.path);
      let hit = autoExpand.has(en.path);
      if (!hit) {
        for (const x of autoExpand) {
          if (treePathKey(x) === k) { hit = true; break; }
        }
      }
      if (hit) setTimeout(() => node.click(), 0);
    }
  }
}

function makeNode(en, depth, parentPath, autoExpand) {
  const d = document.createElement("div");
  d.className = "node";
  d.style.paddingLeft = (8 + depth * 14) + "px";
  d.innerHTML = '<span class="arrow"></span><span class="ico"></span><span class="nm"></span>';
  const arrow = d.querySelector(".arrow");
  const ico = d.querySelector(".ico");
  const nm = d.querySelector(".nm");

  // 源码文件给一个字母标识（K / C / J / X / Py …），让人一眼看出这是源码，
  // 而不是一堆长得一样的 📄。标识来自「谁染色谁给标识」（插件 EDITOR.icons）
  // + 内核兜底表（py / js / md 这类没有染色插件的），见 80-editor.js 的
  // fileIconOf()。80-editor.js 在 40-tree.js 之后加载，但这里只在建节点时
  // 调用（那时早加载完了）；仍套一层 typeof —— 树在登录后第一次 loadTree
  // 就可能跑，不该因为加载顺序差一拍就抛错。
  const mark = en.is_dir ? ""
    : (typeof fileIconOf === "function" ? fileIconOf(en.path) : "");
  ico.textContent = en.is_dir ? "📁" : (mark || "📄");
  // 只给「有标识」的加 class / data-ic：CSS 按 data-ic 分配颜色，
  // 插件和内核都不用知道主题色（和染色「token 类型给前端」同一套分工）。
  if (mark) {
    ico.classList.add("src");
    ico.dataset.ic = mark;
  }
  // 目录先摆一个「折叠」箭头。以前只在 expandDir() 里才写 ▾/▸，于是
  // **没展开过的目录整行看不到任何展开标记** —— 用户根本不知道它能点开。
  // 文件留空（.arrow 是固定 14px 宽，有没有箭头都不会让名字挪位）。
  arrow.textContent = en.is_dir ? "▸" : "";
  // compact 折叠：后端把"一路只有一个子目录"的链并成一行，给了 label
  //（`src\com\android`）。没有 label 就是普通条目，用 name。
  nm.textContent = en.label || en.name;
  // 把条目信息挂在节点上：Del 键删除时要靠它认出「删的是哪一个」。
  // 名字取 baseName —— 折叠目录显示的是整串 label（`src\com\android`），
  // 而删除实际落在**末级**那个目录上。
  d._entry = { path: en.path, name: baseName(en.path), isDir: !!en.is_dir };
  if (en.chain && en.chain.length > 1) {
    nm.title = "已经折叠了 " + en.chain.length + " 层目录";
    d.title = en.path + "\n\n" + en.chain.length + " 层目录并成了一行"
              + "（中间每层都只有一个子文件夹）";
  } else {
    // 悬浮提示：路径 +（大小 · 修改时间）。目录只有时间（大小对目录没意义）。
    // 后端拿不到 mtime 时给 0，fmtTime 返回空串，这时退回原来只显示路径，
    // 不会留下一个空括号。
    const bits = [];
    if (!en.is_dir) bits.push(fmtSize(en.size));
    const mt = fmtTime(en.mtime);
    if (mt) bits.push("修改于 " + mt);
    d.title = en.path + (bits.length ? "  （" + bits.join(" · ") + "）" : "");
  }

  let childBox = null;
  let open = false;

  async function expandDir() {
    if (childBox) { childBox.remove(); childBox = null; }
    open = true;
    expanded.add(en.path);
    arrow.textContent = "▾";
    ico.textContent = "📂";
    childBox = document.createElement("div");
    // 记下这个子容器归哪个条目：它是 d 的**兄弟节点**（不是子节点），
    // 增量重排根这一层时要靠它认出"这个容器得跟着 d 一起走 / 一起删"
    // （见 treeReloadRootQuietly）。
    childBox._owner = d;
    d.after(childBox);
    await renderDir(childBox, en.path, depth + 1, autoExpand);
  }

  d.addEventListener("click", async () => {
    if (en.is_dir) {
      if (open) {
        open = false;
        expanded.delete(en.path);
        arrow.textContent = "▸";
        ico.textContent = "📁";
        if (childBox) childBox.classList.add("hidden");
        return;
      }
      if (childBox) childBox.classList.remove("hidden");
      await expandDir();
    } else {
      selectNode(d);
      openInEditor(en.path, en.name);
    }
  });

  d.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    selectNode(d);
    // 远程节点只支持浏览 / 编辑 / 保存，本地那套新建/重命名/删除用不了。
    // 但右键不能"什么都没发生" —— 至少得能拿走路径。
    if (String(en.path || "").startsWith("ssh://")) {
      remoteNodeMenu(e.clientX, e.clientY, en);
      return;
    }
    openContextMenu(e.clientX, e.clientY, en, parentPath, async () => {
      if (en.is_dir) await expandDir();
      else await loadTree();
    });
  });

  // 远程节点不参与拖拽：拖到本地目录会走本地 rename，必然失败
  if (!String(en.path || "").startsWith("ssh://")) wireTreeDrag(d);

  return d;
}

// ---------------------------------------------------------------------------
// 文件树拖拽移动
// ---------------------------------------------------------------------------
//
// 跟文件管理器一样：把条目拖到某个目录上 = 挪进去，拖到下面的空白处 = 挪回工作区根。
//
// 用 HTML5 原生 drag & drop，不是拿鼠标事件重写 —— 后者得自己处理自动滚动、
// 拖出窗口、光标样式，很容易做出"拖到窗口外就卡住不放"的怪味。
//
// 三个漏了就会出事的地方：
//   ① **目录不能拖进自己或自己的子孙**。手一抖把 a 拖到 a/b 上，后端会报错，
//      但报错之前界面得先告诉你不许放（红虚线），不然像"点了没反应"。
//   ② 挪完**打开着的文件路径会失效**。编辑器还指着老路径，下次 Ctrl+S 就在
//      老位置新建一个同名文件，看起来像"复制了一份"。见 afterMove。
//   ③ **展开状态是按绝对路径记的**，不跟着改就整棵折回去，像子目录丢了。

let TREE_DRAG = null;     // {path, name, isDir}
let TREE_MARK = null;     // 当前高亮着的那个元素（每帧全量查一遍太浪费）

/** 路径比较用的规范化：统一成反斜杠、去掉末尾分隔符、忽略大小写。 */
function normKey(p) {
  return String(p || "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

/** 取所在目录（不含末尾分隔符）。右侧菜单「在当前文件夹搜索」要用。 */
function dirOfPath(p) {
  const s = String(p || "").replace(/[\\/]+$/, "");
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  return i > 0 ? s.slice(0, i) : s;
}

/** 能不能把这个条目放进 dstDir。 */
function canDropInto(src, dstDir) {
  if (!src || !dstDir) return false;
  const a = normKey(src.path);
  const b = normKey(dstDir);
  if (!a || !b) return false;
  if (b === normKey(dirOf(src.path))) return false;   // 已经就在这个目录里
  if (src.isDir && (b === a || b.startsWith(a + "\\"))) return false;  // 拖进自己里面
  return true;
}

function markDrop(el, cls) {
  if (TREE_MARK && TREE_MARK !== el) TREE_MARK.classList.remove("drop-ok", "drop-bad");
  TREE_MARK = el;
  if (el) {
    el.classList.remove("drop-ok", "drop-bad");
    el.classList.add(cls);
  }
}

function clearDropMarks() {
  if (TREE_MARK) TREE_MARK.classList.remove("drop-ok", "drop-bad");
  TREE_MARK = null;
}

/** 把拖拽的四件事挂到一行上。 */
function wireTreeDrag(d) {
  const ent = d._entry;

  // 能被拖的只有条目本身；工作区根不是"条目"，没有节点，所以不用特判
  d.draggable = true;

  d.addEventListener("dragstart", (ev) => {
    TREE_DRAG = { path: ent.path, name: ent.name, isDir: ent.isDir };
    d.classList.add("dragging");
    // Chrome 里不 setData 的话拖拽根本不会真正开始
    ev.dataTransfer.setData("text/plain", ent.path);
    // 坑：原来是 "move"，但聊天区的 dragover 里设的是 dropEffect="copy"。
    // 浏览器要两者兼容才允许 drop —— move 和 copy 不兼容，所以往聊天框拖时
    // drop 事件根本不触发，表现为"拖过去毫无反应、不出附件 chip"。
    // 用 "copyMove" 同时放行两种情况：树内拖动仍是 move，拖到聊天区是 copy。
    ev.dataTransfer.effectAllowed = "copyMove";
  });

  d.addEventListener("dragend", () => {
    d.classList.remove("dragging");
    TREE_DRAG = null;
    clearDropMarks();
  });

  d.addEventListener("dragover", (ev) => {
    // 从资源管理器拖文件进来 → 复制到这个目录（不是移动，见 uploadInto 上面的说明）
    if (hasForeignFiles(ev)) {
      ev.preventDefault();
      ev.stopPropagation();
      ev.dataTransfer.dropEffect = "copy";
      markDrop(d, "drop-ok");
      return;
    }
    if (!TREE_DRAG) return;
    ev.preventDefault();
    ev.stopPropagation();        // 别冒到 #tree 上去把"整个根"也点亮了
    // 拖到文件上 = 放进它所在的目录（文件管理器也是这么做的）
    const dstDir = ent.isDir ? ent.path : dirOf(ent.path);
    const ok = canDropInto(TREE_DRAG, dstDir);
    ev.dataTransfer.dropEffect = ok ? "move" : "none";
    markDrop(d, ok ? "drop-ok" : "drop-bad");
  });

  d.addEventListener("dragleave", () => {
    d.classList.remove("drop-ok", "drop-bad");
  });

  d.addEventListener("drop", async (ev) => {
    if (hasForeignFiles(ev)) {
      ev.preventDefault();
      ev.stopPropagation();
      clearDropMarks();
      await uploadInto(ent.isDir ? ent.path : dirOf(ent.path), ev.dataTransfer);
      return;
    }
    if (!TREE_DRAG) return;
    ev.preventDefault();
    ev.stopPropagation();
    const src = TREE_DRAG;
    const dstDir = ent.isDir ? ent.path : dirOf(ent.path);
    clearDropMarks();
    if (!canDropInto(src, dstDir)) return;
    await moveEntry(src, dstDir);
  });
}

// ---------------------------------------------------------------------------
// 从资源管理器拖文件进来（复制）
// ---------------------------------------------------------------------------
//
// **只能复制，不能移动** —— 浏览器出于安全不给源文件的真实路径
// （`File.path` 在网页里永远是 undefined），所以拿不到"从哪儿来"，
// 也就没法把原文件挪走。表现上跟资源管理器的"复制到此处"一样。
//
// 文件夹是靠 `webkitGetAsEntry()` 递归读的：浏览器只给一棵虚拟的文件树，
// 每个文件还得自己 file() 取出来再传。

/** 这次拖的是不是"从系统拖进来的文件"（而不是我们自己在树里拖）。 */
function hasForeignFiles(ev) {
  const dt = ev && ev.dataTransfer;
  if (!dt) return false;
  // 内部拖拽只 set 了 text/plain；系统拖进来的一定带 "Files"
  return Array.from(dt.types || []).indexOf("Files") >= 0;
}

function entryFile(entry) {
  return new Promise((res, rej) => entry.file(res, rej));
}

/** 传一个 File 到 dir 下。返回 {ok, bad, over}。 */
async function putOne(file, dir) {
  const dst = joinPath(dir, file.name);
  try {
    const res = await fetch("/api/fs/upload?path=" + encodeURIComponent(dst)
                            + "&overwrite=1&ws=" + encodeURIComponent(S.workspace || ""), {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        Authorization: "Bearer " + S.token,
      },
      body: file,
    });
    if (res.status === 401) { logout("登录已失效，请重新输入密码"); return { ok: 0, bad: 1, over: 0 }; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      toast("复制 " + file.name + " 失败：" + (d.error || ("HTTP " + res.status)), true);
      return { ok: 0, bad: 1, over: 0 };
    }
    const d = await res.json();
    return { ok: 1, bad: 0, over: d.overwritten ? 1 : 0 };
  } catch (e) {
    toast("复制 " + file.name + " 失败：" + e.message, true);
    return { ok: 0, bad: 1, over: 0 };
  }
}

/** 递归处理拖进来的一个条目（文件或整个目录）。 */
async function walkEntry(entry, dir) {
  if (entry.isFile) {
    try {
      return await putOne(await entryFile(entry), dir);
    } catch (e) {
      return { ok: 0, bad: 1, over: 0 };
    }
  }
  if (!entry.isDirectory) return { ok: 0, bad: 0, over: 0 };

  const sub = joinPath(dir, entry.name);
  // 建目录。已存在会报错 —— 那是正常情况（拖一份新的进去覆盖旧的），忽略。
  // 空目录靠这一句建出来；有文件的目录后面写文件时也会自动建父目录。
  try { await api("/api/fs/mkdir", { path: sub }); } catch (e) { /* 已存在 */ }

  let ok = 0, bad = 0, over = 0;
  const reader = entry.createReader();
  for (;;) {
    // readEntries 一次最多给 100 条，必须一直读到返回空数组为止
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    for (const e of batch) {
      const r = await walkEntry(e, sub);
      ok += r.ok; bad += r.bad; over += r.over;
    }
  }
  return { ok, bad, over };
}

/** 把这次拖进来的东西复制到 dir。 */
async function uploadInto(dir, dt) {
  if (!dir) { toast("还没有工作区", true); return; }

  const entries = [];
  for (const it of Array.from((dt && dt.items) || [])) {
    if (it.kind !== "file") continue;
    const en = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
    if (en) entries.push(en);
  }
  // 拿不到 entry API 就退回 files（老浏览器，或者某些来源就只有 FileList）
  const plain = entries.length ? [] : Array.from((dt && dt.files) || []);
  if (!entries.length && !plain.length) return;

  toast("正在复制到 " + (baseName(dir) || "工作区根") + " …");

  let ok = 0, bad = 0, over = 0;
  for (const en of entries) {
    const r = await walkEntry(en, dir);
    ok += r.ok; bad += r.bad; over += r.over;
  }
  for (const f of plain) {
    const r = await putOne(f, dir);
    ok += r.ok; bad += r.bad; over += r.over;
  }

  // 把落点目录展开再重画 —— 不展开的话，传完一脸茫然：
  // 树上看不出任何变化（新来的都在那个收起来的文件夹里）
  if (ok) expanded.add(dir);
  if (ok) notifySvnFsChanged(dir);   // 拖进来一批文件：都是 `?`
  await loadTree();
  if (!ok && !bad) return;
  toast("已复制 " + ok + " 个文件"
        + (over ? "（覆盖 " + over + " 个同名文件）" : "")
        + (bad ? "，失败 " + bad + " 个" : ""), !!bad);
}

/** 真正挪：调 rename，再收拾界面状态。 */
async function moveEntry(src, dstDir) {
  const to = joinPath(dstDir, src.name);
  try {
    await api("/api/fs/rename", { path: src.path, to });
  } catch (e) {
    toast("移动失败：" + e.message, true);
    return;
  }
  await afterMove(src.path, to);
  toast("已移到 " + (baseName(dstDir) || "工作区根") + " / " + src.name);
}

/** 挪完之后把界面上所有"指着老路径"的东西改到新路径。 */
async function afterMove(from, to) {
  const f = String(from).replace(/[\\/]+$/, "");
  const t = String(to).replace(/[\\/]+$/, "");
  const remap = (p) => {
    if (!p) return p;
    if (p === f) return t;
    if (p.startsWith(f + "\\") || p.startsWith(f + "/")) return t + p.slice(f.length);
    return p;
  };

  // 展开状态是按绝对路径记的：不改就整棵折回去，看着像子目录被弄丢了
  for (const p of Array.from(expanded)) {
    const q = remap(p);
    if (q !== p) { expanded.delete(p); expanded.add(q); }
  }

  // 打开着的文件必须跟着改路径。不改的话编辑器还指着老路径，
  // 下次 Ctrl+S 会在旧位置新建一个同名文件 —— 那是真的会丢东西（写进错的地方）
  let touched = false;
  for (const e of EDITORS) {
    const q = remap(e.path);
    if (q !== e.path) { e.path = q; e.name = baseName(q); touched = true; }
  }
  if (touched) { renderEdTabs(); paintEditor(); }

  // 拖动移动 = 老位置删掉 + 新位置新增；跨副本时两边各扫各自的
  notifySvnFsChanged(f);
  notifySvnFsChanged(t);
  await loadTree();
}

function selectNode(d) {
  treeBox.querySelectorAll(".node.sel").forEach((n) => n.classList.remove("sel"));
  d.classList.add("sel");
}

// ---------------------------------------------------------------------------
// 自动定位：点了编辑器里的文件标签，左边树展开到那个文件并选中
// ---------------------------------------------------------------------------
// 对应 VS Code 的 explorer.autoReveal。两处开销要盯住：
//   ① loadTree() 是**逐层**发 /api/fs/list（展开几层就几个请求）。Android 那种
//      深目录切一次标签扫十几下，所以只在文件树正显示着的时候才做（见下方 soon）。
//   ② 节点已经在 DOM 里就别重画，直接选中 —— 这是最常走的一条路（文件就在
//      眼前，只是没高亮）。

// ⚠️ 这几个名字必须带 tree 前缀，别叫 `revealInTree`：
// 插件 adb_tools/web/logcat.js 里就有一个同名函数（导出日志后把文件露出来）。
// 插件 js 是**后注入**的顶层脚本，同名的 function 声明会直接覆盖内核这个 ——
// 覆盖之后内核这套整个失效，表现是"树会展开，但不高亮、不滚动"（踩过一次：
// 插件那版只往 expanded 里塞目录再重画，压根没有选中这一步）。

/** 比较用的键：分隔符统一、去尾斜杠、忽略大小写（Windows 路径不区分大小写）。 */
function treePathKey(p) {
  return String(p || "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

/**
 * 按绝对路径找树上的节点。
 *
 * `wantDir` 不传 = 只认文件（老行为，定位「打开的文件」用）；传 true = 只认
 * 目录。目录和文件在树里是同一个 class，靠 `_entry.isDir` 区分 —— 以前
 * 无条件跳过目录，于是 `.ai/history` 这种"要定位的其实是个文件夹"永远找不到。
 */
function findTreeNodeByPath(path, wantDir) {
  const key = treePathKey(path);
  for (const n of treeBox.querySelectorAll(".node")) {
    const e = n._entry;
    if (!e) continue;
    if (wantDir ? !e.isDir : e.isDir) continue;
    if (treePathKey(e.path) === key) return n;
  }
  return null;
}

/**
 * 把某个绝对路径在树里展开并高亮。不在任何已挂载的根下就返回 false。
 *
 * `wantDir` = 目标是目录（`.ai/history` 这种），见 findTreeNodeByPath。
 */
async function treeRevealFile(path, wantDir) {
  if (!path) return false;
  const key = treePathKey(path);

  // 已知的根：当前工作区 + 树上挂着的其它根（远程工作区的「本地数据」目录）。
  // 只认 S.workspace 的话，在远程工作区里打开 web/remote/gp/xx.md 永远定位不到。
  const roots = [];
  if (S.workspace) roots.push(S.workspace);
  for (const el of treeBox.querySelectorAll("[data-root]")) {
    const r = el.dataset.root;
    if (r && roots.indexOf(r) < 0) roots.push(r);
  }
  const root = roots.find((r) => {
    const rk = treePathKey(r);
    return key === rk || key.startsWith(rk + "\\");
  });
  // 外部文件（别的工程 / adb:// 之类的假路径）压根不在树里，别折腾
  if (!root) return false;
  const rk = treePathKey(root);

  const seen = findTreeNodeByPath(path, wantDir);
  if (seen && seen.offsetParent !== null) {
    selectNode(seen);
    seen.scrollIntoView({ block: "nearest" });
    return true;
  }

  // 把父目录一路标成「展开」，loadTree 恢复展开状态时会顺着带下来。
  //
  // 前缀必须换成**根的真实大小写**：expanded 是个区分大小写的 Set，
  // 而路径可能是模型从对话里抄来的（`d:\workspace\...` 全小写），差一个字母
  // 就 has() 不中、树展开了又折回去。
  //
  // 分隔符要保留 path 自己的写法：远程（ssh://）的节点 path 全是 `/`，
  // 以前这里无条件换成 `\`，于是 expanded 里存的是 `ssh:\\…\rootdir`，
  // 而 en.path 是 `ssh://…/rootdir` —— has() 永远不中，定位就报"不在树里"。
  const rootNorm = String(root).replace(/[\\/]+$/, "");
  const tail = String(path).replace(/[\\/]+$/, "").slice(rk.length);
  let cur = rootNorm + tail;
  for (let i = 0; i < 64; i++) {
    const up = parentOf(cur);
    if (!up || up === cur) break;
    expanded.add(up);
    if (treePathKey(up) === rk) break;
    cur = up;
  }

  const seq = ++TREE_REVEAL_SEQ;
  await loadTree();
  // renderDir 里的自动展开是 setTimeout 一帧一层排下去的（见 autoExpand 那段），
  // 深目录一次画不完，所以这里轮询等节点出现，而不是 loadTree 一返回就找。
  for (let i = 0; i < 40; i++) {
    if (seq !== TREE_REVEAL_SEQ) return false;   // 期间又切了别的文件，这次作废
    const n = findTreeNodeByPath(path, wantDir);
    if (n) {
      selectNode(n);
      n.scrollIntoView({ block: "nearest" });
      return true;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

let TREE_REVEAL_TIMER = 0;
let TREE_REVEAL_SEQ = 0;

/** 延迟一点再定位。连续点几个标签时只认最后一个，免得排队把整棵树画好几遍。 */
function treeRevealFileSoon(path) {
  if (!path) return;
  clearTimeout(TREE_REVEAL_TIMER);
  TREE_REVEAL_TIMER = setTimeout(() => {
    // 只在文件树露在眼前时做：左栏停在搜索 / SVN 上时用户看不到树，
    // 为此整棵重扫一遍纯属白花（大工程一次十几秒的磁盘访问）。
    // 判断方式和 Del 键那处一致 —— 祖先 display:none 时 offsetParent 是 null。
    if (!treeBox.offsetParent) return;
    treeRevealFile(path);
  }, 120);
}

// ---- 新建 / 重命名 / 删除 ----
function joinPath(dir, name) {
  const d = String(dir || "").replace(/[\\/]+$/, "");
  if (!d) return name;
  const sep = d.includes("\\") || !d.includes("/") ? "\\" : "/";
  return d + sep + name;
}

function parentOf(p) {
  return String(p || "").replace(/[\\/][^\\/]*$/, "");
}

/**
 * 文件系统变了（新建 / 删除 / 重命名 / 拖进来）→ 通知 SVN 视图跟上。
 *
 * 只传**变动的那个路径**：SVN 插件会把它映射到所属的那棵子树，只重扫那一棵
 * （55 个副本全量扫一次要两秒，而新建一个文件只影响一个副本）。
 * 插件没启用 / 还没加载时 `maybeRefreshSvn` 不存在，静默跳过 —— 文件操作本身
 * 绝不能因为 SVN 这边出问题而失败。
 */
function notifySvnFsChanged(path) {
  if (typeof maybeRefreshSvn !== "function") return;
  try { maybeRefreshSvn(false, path); } catch (e) { /* 刷新失败不影响文件操作 */ }
}

function copyPath(p) {
  navigator.clipboard.writeText(p)
    .then(() => toast("路径已复制"))
    .catch(() => toast(p));
}

// 在系统文件管理器里打开：目录就打开它，文件是打开所在目录并选中它
//（见后端 reveal_in_file_manager 里的 explorer /select,）。
// 浏览器自己开不了资源管理器（file:// 也不行，会被拦），所以只能绕后端。
// 成功不弹 toast —— 资源管理器窗口本身就是反馈，再来一句纯属噪音。
async function revealInExplorer(path) {
  try {
    await api("/api/fs/reveal", { path });
  } catch (e) { toast("打开资源管理器失败：" + e.message, true); }
}

// 复制**文件/目录本体**到系统剪贴板（不是路径）。浏览器写不了「文件项」这类
// 剪贴板内容，只能让后端用 PowerShell 的 Set-Clipboard 代劳 —— 之后在资源
// 管理器里 Ctrl+V 就能粘出来，和在那里自己 Ctrl+C 一样（目录会整棵拷走）。
async function copyEntryToClipboard(path) {
  try {
    const r = await api("/api/fs/clipboard", { path });
    const kind = (r && r.is_dir) ? "文件夹 " : "文件 ";
    toast("已复制" + kind + ((r && r.name) || "") + "，可以到资源管理器里粘贴");
  } catch (e) { toast("复制失败：" + e.message, true); }
}

async function newFile(dir) {
  const name = prompt("新建文件（可带子路径，例如 src/a.py）", "");
  if (name === null) return;
  const n = name.trim();
  if (!n) return;
  const path = joinPath(dir, n);
  try {
    await api("/api/fs/write", { path, text: "" });
    notifySvnFsChanged(path);          // 新文件在 SVN 里是 `?`，得让它出现在面板上
    toast("已新建 " + n);
    await loadTree();
    openInEditor(path, baseName(path));
  } catch (e) { toast("新建失败：" + e.message, true); }
}

async function newDir(dir) {
  const name = prompt("新建文件夹", "");
  if (name === null) return;
  const n = name.trim();
  if (!n) return;
  try {
    await api("/api/fs/mkdir", { path: joinPath(dir, n) });
    notifySvnFsChanged(joinPath(dir, n));
    toast("已新建文件夹 " + n);
    await loadTree();
  } catch (e) { toast("新建失败：" + e.message, true); }
}

async function renameEntry(path, oldName) {
  const name = prompt("重命名 / 移动（填相对名字，或直接填新路径）", oldName);
  if (name === null) return;
  const n = name.trim();
  if (!n || n === oldName) return;
  const target = /[\\/]/.test(n) ? n : joinPath(parentOf(path), n);
  try {
    const r = await api("/api/fs/rename", { path, to: target });
    // 重命名 = 删一个 + 加一个，两头可能不在同一个副本里，所以两个都报
    notifySvnFsChanged(path);
    notifySvnFsChanged(r.path || target);
    // 编辑器里开着的话，路径跟着换
    const e = EDITORS.find((x) => x.path === path);
    if (e) { e.path = r.path; e.name = baseName(r.path); renderEdTabs(); }
    toast("已重命名为 " + baseName(r.path));
    await loadTree();
  } catch (e) { toast("重命名失败：" + e.message, true); }
}

// 删文件 / 删文件夹（右键菜单和 Del 键都走这里）。
// **特意不弹确认框**：后端是把东西丢进系统回收站，点错了去回收站还原就行；
// 每删一个都拦一道「不可恢复」反而碍事（新建 / 重命名也都从来不问）。
async function deleteEntry(path, name, isDir) {
  const what = isDir ? "文件夹" : "文件";
  try {
    await api("/api/fs/delete", { path });
    // 关掉这个文件（或这个目录下任何文件）的编辑器标签
    const prefix = path.replace(/[\\/]+$/, "");
    const hit = EDITORS.findIndex((x) =>
      x.path === path ||
      x.path.startsWith(prefix + "\\") ||
      x.path.startsWith(prefix + "/"));
    if (hit >= 0) closeEditor(hit);
    notifySvnFsChanged(path);          // 已纳入版本的会变成 `!` missing
    toast("已把" + what + "「" + name + "」移到回收站");
    await loadTree();
  } catch (e) { toast("删除失败：" + e.message, true); }
}

// Del 键直接删**选中**的那一项，同样不弹框、同样进回收站。
// 选中态是 `.node.sel`（点文件 / 右键都会置上），条目信息在 makeNode 时
// 挂到了节点上，所以这里不用再往回查一遍路径。
//
// 拦的条件写得很紧 —— 这个键太容易误触，宁可偶尔不灵：
//   · 键盘得在"没人管"的状态（activeElement 是 body）：编辑器 / 终端 / 输入框
//     里按 Del 是删字符；日志面板那种 tabindex 容器拿到焦点时也不算数
//     （它自己也用方向键选行，别一边选日志一边把文件删了）。
//   · 文件视图得正在显示：切到搜索 / 设备面板后树还留在 DOM 里（带着上次的
//     选中态），不加这条就会在别的面板里把文件删掉。
//   · 没开弹窗。
document.addEventListener("keydown", (e) => {
  if (e.key !== "Delete" || e.ctrlKey || e.altKey || e.metaKey) return;
  const act = document.activeElement;
  if (act && act !== document.body && act !== document.documentElement) return;
  if (!treeBox.offsetParent) return;             // 文件视图没在显示
  if (!$("modal").classList.contains("hidden")) return;
  const sel = treeBox.querySelector(".node.sel");
  const en = sel && sel._entry;
  if (!en) return;
  e.preventDefault();
  deleteEntry(en.path, en.name, en.isDir);
});

// ---------------------------------------------------------------------------
// 右键菜单
// ---------------------------------------------------------------------------
const ctxEl = $("ctx");   // 名字别叫 ctx：25-tabs.js / 30-chat.js 里 ctx 是菜单容器的另一个引用

//: 主菜单最后一次显示时的位置 / 尺寸。收起后是 display:none，量不到 ——
//: 二级菜单（SVN ▸ / SSH ▸）靠它贴到主菜单右边展开。
var CTX_LAST_RECT = null;

//: 当前打开的二级菜单（双栏级联）：{ box, close }
var CTX_SUB = null;

/** 关掉当前二级菜单（没开就什么也不做）。 */
function closeCtxSub() {
  if (!CTX_SUB) return;
  const s = CTX_SUB;
  CTX_SUB = null;
  try { s.close(); } catch (e) { /* 关二级菜单失败不能连累主菜单 */ }
}

/**
 * 二级菜单建好后交给内核统一管（插件自己只管创建和内容）。
 *
 * 内核负责这些关闭时机：换到别的菜单项、主菜单收起、滚动、点外面、Esc、
 * 窗口失焦、鼠标移出子菜单一会儿。同一时刻只留一个。
 */
function ctxSubOpen(box, close) {
  closeCtxSub();
  CTX_SUB = { box: box, close: close };

  const onKey = (ev) => {
    if (ev.key !== "Escape") return;
    ev.stopPropagation();          // 别让 Esc 继续去关编辑器 / 清选择
    closeCtxSub();
    hideCtx();                     // 双栏级联时主菜单也开着，一起收
  };
  box._subKey = onKey;
  document.addEventListener("keydown", onKey, true);

  // 移出子菜单一会儿就收：从主菜单滑到别处时，不该一直挂在那儿
  let t = 0;
  box.addEventListener("mouseenter", () => clearTimeout(t));
  box.addEventListener("mouseleave", () => {
    clearTimeout(t);
    t = setTimeout(() => closeCtxSub(), 300);
  });
}

/** 子菜单自己收起来时调一下，把内核这边的登记和监听摘干净。 */
function ctxSubClosed(box) {
  if (CTX_SUB && CTX_SUB.box === box) CTX_SUB = null;
  if (box && box._subKey) {
    document.removeEventListener("keydown", box._subKey, true);
    box._subKey = null;
  }
}

/**
 * 点一项（或悬停触发）。
 *
 * 末尾带 `▸` 的项开的是二级菜单：**主菜单留着**，做成双栏级联；普通项
 * 直接执行并把菜单收起来。
 *
 * 为什么不给 menuRegister 加 `submenu: true`：`SVN ▸` 是插件注册的，内核
 * 拿不到"这一项有没有子菜单"；label 末尾的 `▸` 本来就是"我有下级"的约定。
 */
function ctxRunItem(label, fn) {
  if (/▸\s*$/.test(label || "")) {
    // 主菜单还开着：现量一次最准（hideCtx 记的那份可能是上一次菜单的）
    if (!ctx.classList.contains("hidden")) {
      CTX_LAST_RECT = ctx.getBoundingClientRect();
    }
    closeCtxSub();                 // 换项开新的之前，先把旧的收掉
    fn();
    return;
  }
  hideCtx();
  fn();
}

/** 末尾带 `▸` 的项：鼠标停 140ms 自动展开，不用点。 */
function ctxHoverSub(btn, label, fn) {
  if (!/▸\s*$/.test(label || "")) return;
  let timer = 0;
  btn.addEventListener("mouseenter", () => {
    clearTimeout(timer);
    // 停一下再开：斜着划过一排菜单项时，不该每一项都闪一下
    timer = setTimeout(() => ctxRunItem(label, fn), 140);
  });
  btn.addEventListener("mouseleave", () => clearTimeout(timer));
}

/**
 * 远程节点（ssh://）的右键菜单。
 *
 * 本地那套菜单（新建 / 重命名 / 删除 / 粘贴）全是对 os.path 的操作，远程节点
 * 上用不了；但"右键点了什么都没发生"更糟 —— 先把能用的给出来：添加到对话、
 * 复制路径、在这个文件夹里搜。
 */
function remoteNodeMenu(x, y, en) {
  ctx.innerHTML = "";

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = en.path;
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => ctxRunItem(label, fn));
    ctxHoverSub(b, label, fn);     // 带 ▸ 的停一下就展开（主菜单留着）
    ctx.appendChild(b);
  };

  // 文件：下载到本机（走 /api/download 的 ssh 分支，SFTP 取字节）
  if (!en.is_dir && typeof downloadFile === "function") {
    item("下载到本机", () => downloadFile(en.path));
  }
  item("复制路径", () => copyText(en.path));

  // 添加到对话：和本地菜单（`addAttachment` / `addDirAttachment`）同一套入口，
  // 所以任务台开着时由 boardRouteAttach 自动转过去，不用在这里再判一次。
  //
  // 名字取法和 `openContextMenu`（`:1202`）保持一致：折叠目录（`src\main\java\com\server`
  // 显示成一串链）的 `en.name` 是整串 label，要退回 `baseName(path)` 取末级，
  // 否则 chip 上会挂一长串。baseName 先把 \ 统一成 / 再取末段，ssh:// 路径也对。
  //
  // 后端不会本地读 ssh:// 的内容 —— `_load_attachments` / `_load_dirs` 对 ssh://
  // 只记路径（read_file 的 os.path.abspath 会把 ssh:// 拼成 C:\...\ssh:\... 后
  // open 失败）。模型手上有 _t_read_file 的远程分支，能自己按需读，见
  // md/SSH文件树添加到对话.md。
  const foldedDir = !!(en.chain && en.chain.length > 1);
  const attachName = foldedDir ? baseName(en.path) : en.name;
  if (typeof addAttachment === "function") {
    if (en.is_dir) {
      if (typeof addDirAttachment === "function") {
        item("添加目录到对话（只传路径）",
             () => addDirAttachment(en.path, attachName));
      }
    } else {
      item("添加到对话", () => addAttachment(en.path, attachName));
    }
  }

  // 在远程文件夹里搜：把搜索视图的范围设成它（插件没加载就不给这一项）
  if (en.is_dir && typeof FIND !== "undefined"
      && typeof paintFindScope === "function") {
    item("在此文件夹中搜索", () => {
      FIND.folder = en.path;
      paintFindScope();
      if (typeof switchView === "function") switchView("search");
    });
  }

  // 在此打开终端：cwd 带 ssh:// 前缀，后端据此把命令丢到服务器上跑
  // （见 web_agent 的 _remote_term_ctx）。不能复用本地那个 `cd`，所以后端
  // 专门有一条远程分支。
  if (en.is_dir && typeof openTerminalAt === "function") {
    item("在此打开终端", () => openTerminalAt(en.path));
  }

  // SSH ▸：像 svn_tools 的「SVN ▸」那样开二级菜单 —— 根目录、ripgrep 路径、
  // 连接设置收在一起，别把「新建 / 打开终端」这些常用的挤下去。
  const curWs = (typeof S !== "undefined" && S.workspace) || "";
  if (en.is_dir && String(curWs).startsWith("ssh://")) {
    item("SSH ▸", () => remoteSshSubmenu(en.path, curWs));
  }

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

/**
 * 把应用自带的 Linux 版 ripgrep 传到远端
 * （`POST /api/ssh/workspace/rg` 带 `auto:true`）。
 *
 * 远端一处 rg 都没有时，搜索会静默退回慢一档的 grep / find，用户只感觉
 * 「远程怎么这么慢」。后端 `deploy_rg` 会先探测：已经有能跑的就复用、
 * 不覆盖，架构不对则一个字节都不传 —— 所以这一项可以随便点，不会破坏
 * 现有环境。
 *
 * 两个入口共用：文件树 `SSH ▸` 里的「上传内置 ripgrep…」，以及工作区
 * 标签栏右键（`20-workspace.js` 的 `treeTabMenu`）。`ws` 是 ssh:// 标识。
 */
async function uploadBundledRg(ws) {
  toast("正在检查远端 ripgrep…");
  try {
    // 5.4MB 走 SFTP，慢网可能十几秒；超时给足，别让用户以为卡死了
    const r = await api("/api/ssh/workspace/rg", { ws, auto: true },
                        { timeout: 120000 });
    if (!r || r.ok === false) throw new Error((r && r.error) || "失败");
    const msg = {
      found: "远端已有 ripgrep：" + r.rg,
      uploaded: "已上传内置 ripgrep → " + r.rg,
      reused: "复用已上传的那份：" + r.rg,
    }[r.action] || ("ripgrep：" + (r.rg || "未知"));
    toast(msg);
  } catch (e) {
    toast("配置 ripgrep 失败：" + (e.message || e), true);
  }
}

/**
 * 远程目录右键的二级菜单（`SSH ▸`），和 svn_tools 的 `SVN ▸` 一个做法。
 *
 * 内核的菜单只支持一级（remoteNodeMenu / openContextMenu 里都是平铺的
 * item()），所以这里自己起一个 `.ctx` 浮层，贴主菜单右边展开。位置沿用主
 * 菜单收起前的 left/top —— hideCtx 只加 hidden，那两个值还在。
 */
function remoteSshSubmenu(path, ws) {
  const box = document.createElement("div");
  box.className = "ctx";
  box.style.position = "fixed";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "SSH";
  box.appendChild(title);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      // 先量按钮位置：closeSub() 之后它已经脱离 DOM，再量就是全 0
      const rect = b.getBoundingClientRect();
      closeSub();
      // 双栏级联时主菜单还开着，选中后要连它一起收（stopPropagation 已经
      // 把 document 上那个 hideCtx 挡住了，得自己叫）
      hideCtx();
      fn(ev, rect);
    });
    box.appendChild(b);
    return b;
  };

  item("把此目录设为工作区根目录", async () => {
    try {
      const r = await api("/api/ssh/workspace/root", { ws, path });
      if (!r || r.ok === false) throw new Error((r && r.error) || "设置失败");
      if (r.ws && r.ws !== ws) {
        // 换根 = 换工作区标识（会话、.ai 数据都跟着它）：旧格子换成新的
        await removeTree(ws, true);
        await addTree(r.ws, true);
        await activateTree(r.ws);
      }
      toast("工作区根已设为 " + (r.name || r.ws));
    } catch (e) {
      toast("设置失败：" + (e.message || e), true);
    }
  });

  // rg 的位置跟目录无关：选完直接写回配置，不用重开设置窗，也不用重启
  // （工具每次现读 ssh_targets.json）
  item("选择 ripgrep 路径…", (ev, rect) => {
    if (typeof pickRemoteTreeForWs !== "function") {
      toast("前端还是旧版本 —— 刷新一下页面", true);
      return;
    }
    // rect 是 item() 在关菜单之前量好的：喂一个"假 anchor"，接口和真元素一样
    pickRemoteTreeForWs(ws, {
      mode: "file",
      anchor: { getBoundingClientRect: () => rect, contains: () => false },
      title: "选择远程 ripgrep",
      onPick: async (p) => {
        try {
          const r = await api("/api/ssh/workspace/rg", { ws, rg: p });
          if (!r || r.ok === false) throw new Error((r && r.error) || "保存失败");
          toast(r.warn || ("ripgrep 已设为 " + p), !!r.warn);
        } catch (e) {
          toast("保存失败：" + (e.message || e), true);
        }
      },
    });
  });

  // 具体逻辑在 uploadBundledRg：标签栏右键也走它（见 20-workspace.js），
  // 两个入口共用一份，文案和超时不会各写一遍。
  item("上传内置 ripgrep…", () => uploadBundledRg(ws));

  item("SSH 设置（目录 / ripgrep 路径）…", () => {
    if (typeof dtAddRemoteDialog !== "function") return;
    dtAddRemoteDialog(async (r) => {
      if (!r || !r.ws || r.ws === ws) return;
      await removeTree(ws, true);
      await addTree(r.ws, true);
      await activateTree(r.ws);
    }, { ws });
  });

  document.body.appendChild(box);

  // 贴主菜单右侧展开（和 SVN ▸ 一样是真级联）；右边放不下就翻到左侧
  const base = CTX_LAST_RECT;
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
    ctxSubClosed(box);             // 摘掉内核挂在它身上的 Esc 监听和登记
  }
  // 关闭时机交给内核统一管：换项 / 主菜单收起 / 点外面 / 滚动 / Esc / 移开
  if (typeof ctxSubOpen === "function") {
    ctxSubOpen(box, closeSub);
  } else {
    // 老内核没有这套：至少别让浮层永远挂着
    const onDoc = (ev) => { if (!box.contains(ev.target)) closeSub(); };
    setTimeout(() => document.addEventListener("click", onDoc, true), 0);
    document.addEventListener("scroll", closeSub, true);
  }
}

function hideCtx() {
  if (!ctx.classList.contains("hidden")) {
    // 隐藏前量一次：此刻还有尺寸（收起后就量不到了）
    CTX_LAST_RECT = ctx.getBoundingClientRect();
  }
  closeCtxSub();                   // 主菜单收了，挂在外面的二级菜单也一起收
  ctx.classList.add("hidden");
}
document.addEventListener("click", hideCtx);

//: 插件重画滚动容器（`innerHTML=""` 再补回内容、恢复 scrollTop）会抖出一次
//: scroll 事件。那不是用户滚的，却会被下面这个 capture 监听当成"页面滚了"
//: 把菜单收掉 —— 表现就是「panel 每 5 秒刷一次，右键菜单自己消失」。
//: 重画期间把它加到 0 以上，重画结束后由插件减回去（见 logcat 的 renderLogSide）。
var CTX_SCROLL_MUTE = 0;

//: 这些容器会**自己**不停滚／重画（日志跟随最新、侧栏每 5 秒重画），
//: 它们滚出来的事件不代表"用户把页面滚走了"。菜单是 position:fixed，
//: 不跟着滚，所以这里直接忽略，否则日志一边滚菜单一边消失。
//:
//: 判据**同时认 id 和 class**，只认 id 会漏：`.sess-panel` 是 25-tabs.js
//: 动态建的，天生只有 class 没有 id。
//:
//: 「自己滚」不等于「用户在读」—— 这里只挡掉"程序滚出来的事件把菜单收掉"，
//: 用户真要关菜单点一下别处即可；漏挡的代价比误挡大得多（菜单根本点不开）。
const CTX_SCROLL_IGNORE_ID = { logList: 1, logSide: 1, termOut: 1, termIn: 1, dtLog: 1 };
const CTX_SCROLL_IGNORE_CLS = ["sess-panel", "think-body", "dt-log"];

function ctxScrollIgnored(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.id && CTX_SCROLL_IGNORE_ID[el.id]) return true;
  const cl = el.classList;
  if (!cl) return false;
  for (const c of CTX_SCROLL_IGNORE_CLS) if (cl.contains(c)) return true;
  return false;
}

document.addEventListener("scroll", (ev) => {
  if (CTX_SCROLL_MUTE > 0) return;
  if (ctxScrollIgnored(ev.target)) return;
  hideCtx();
}, true);
window.addEventListener("blur", hideCtx);

function openContextMenu(x, y, en, parentPath, refresh) {
  ctx.innerHTML = "";

  // 折叠过的目录（`src\main\java\com\server`）：显示的是一整串，
  // 但重命名 / 删除 / 新建实际作用在**末级那个目录**（server）上。
  // 提示里必须写末级的名字，否则「重命名 src」会让人以为动的是最外层。
  const folded = !!(en.chain && en.chain.length > 1);
  const realName = folded ? baseName(en.path) : en.name;

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = folded
    ? en.label + "　（折叠了 " + en.chain.length + " 层，实际是 " + realName + "）"
    : en.name;
  ctx.appendChild(title);

  function item(label, fn, danger) {
    const b = document.createElement("button");
    b.textContent = label;
    if (danger) b.style.color = "var(--danger)";
    b.addEventListener("click", () => ctxRunItem(label, fn));
    ctxHoverSub(b, label, fn);     // 带 ▸ 的停一下就展开（SVN ▸ 就是走这里）
    ctx.appendChild(b);
  }
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  // 新建的目标：在目录上就建在它里面，在文件上就建在它旁边
  const targetDir = en.is_dir ? en.path : parentPath;

  if (!en.is_dir) {
    item("在编辑器中打开", () => openInEditor(en.path, en.name));
    item("添加到对话框", () => addAttachment(en.path, en.name));
    // 图片 / 网页这类"文本读不出来"的文件，「只读预览」只会看到一堆乱码
    // （/api/fs/read 对二进制会直接报错），所以换成插件提供的「在浏览器中预览」。
    // 用 typeof 懒查：插件被禁用时那个函数不存在，这里就退回原来的纯文本预览。
    const mediaOk = typeof mediaPreviewable === "function"
      && mediaPreviewable(en.path);
    item(mediaOk ? "在浏览器中预览" : "只读预览", () => {
      if (mediaOk && typeof openMediaPreview === "function") {
        openMediaPreview(en.path);
      } else {
        previewFile(en.path, en.name);
      }
    });
    item("重命名 / 移动", () => renameEntry(en.path, realName));
    item("下载到本机", () => downloadFile(en.path));
    // 复制的是**文件本体**（apk / so / 截图都能用），粘贴到资源管理器即可。
    // 「复制路径」只给文本，二进制文件拿路径没用 —— 两件事分开列。
    // 只有 Windows 支持（Set-Clipboard），别的系统上干脆不显示，
    // 免得点了才被告知"不支持"。
    if (S.caps && S.caps.clipboardPath) {
      item("复制文件（可直接粘贴）", () => copyEntryToClipboard(en.path));
    }
    item("复制路径", () => copyPath(en.path));
    item("在资源管理器中打开", () => revealInExplorer(en.path));
    // 插件项（比如 code_search 的「在当前文件夹搜索」）由插件自己登记，
    // 禁用插件时 menuItems 会自动把它们滤掉，这里不用再判断是否存在。
    menuRender("tree.file", { path: en.path, name: realName, isDir: false }, {
      item: (label, fn) => item(label, fn), sep,
    });
    sep();
    // 不弹确认框 + 进回收站，所以标签里直接写清楚，免得点的时候心里没底
    item("删除文件（进回收站）", () => deleteEntry(en.path, realName, false), true);
  } else {
    item("新建文件", () => newFile(targetDir));
    item("新建文件夹", () => newDir(targetDir));
    sep();
    // 只把目录路径告诉模型，不在这里展开 —— 展开成几十个文件会把上下文塞满，
    // 而模型本来就有 list_dir / read_file，让它自己按需读更省。
    item("添加目录到对话（只传路径）", () => addDirAttachment(en.path, realName));
    // 搜索、SVN 三条都是插件能力，走注册表
    menuRender("tree.dir", { path: en.path, name: realName, isDir: true }, {
      item: (label, fn, danger) => item(label, fn, danger), sep,
    });
    item("在此打开终端", () => openTerminalAt(en.path));
    item("刷新此目录", () => refresh && refresh());
    sep();
    item("重命名 / 移动", () => renameEntry(en.path, realName));
    // 目录也能复制：CF_HDROP 里放的就是路径，资源管理器粘贴时整棵拷走
    if (S.caps && S.caps.clipboardPath) {
      item("复制文件夹（可直接粘贴）", () => copyEntryToClipboard(en.path));
    }
    item("复制路径", () => copyPath(en.path));
    item("在资源管理器中打开", () => revealInExplorer(en.path));
    sep();
    item("删除文件夹（进回收站）", () => deleteEntry(en.path, realName, true), true);
  }

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

$("treeRefresh").addEventListener("click", () => loadTree());

/**
 * 定位到当前打开的文件：在树里逐层展开并选中它。
 *
 * 打开文件时本来就会自动定位一次（treeRevealFileSoon），但那次可能被跳过：
 * 左栏当时停在搜索 / SVN 上，或者用户手动折回去了。这个按钮是"现在就要找到"。
 */
$("treeLocate").addEventListener("click", async () => {
  const e = (typeof ed === "function") ? ed() : null;
  if (!e || !e.path) { toast("还没有打开的文件", true); return; }
  // 左栏可能停在别的视图上：先切回文件树，否则展开了也看不见
  if (typeof switchView === "function") {
    try { switchView("files"); } catch (err) { /* 忽略 */ }
  }
  TREE_REVEAL_SEQ++;              // 作废掉还在排队的那次自动定位
  const ok = await treeRevealFile(e.path);
  if (!ok) toast("这个文件不在当前文件树里", true);
});
$("treeTop").addEventListener("click", () => {
  // "回到根目录"没意义（树本来就画的是根），折叠更有用
  expanded.clear();
  loadTree();
});
/**
 * 文件树那个「＋」：新建文件 / 新建文件夹。
 *
 * 原来这里挂的是"再加一个工作区"—— 但左侧文件树现在只画活动根这一棵
 * （见 renderWorkspace 上面的说明），再添一格只是往顶部标签栏多塞一个，
 * 跟这一栏看到的没关系。那个动作在「工作区」标签栏上已经有了，
 * 这个位置留给"我要在这儿建个东西"更顺手。
 *
 * 建在**活动根**下：树里没有"当前选中的目录"这个概念，猜你点了哪儿再决定
 * 只会让人拿不准。要建在别的目录，在树上对着那个目录右键（同一对函数）。
 */
function treeAddMenu(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  const dir = S.workspace || "";
  if (!dir) { toast("还没有工作区，先在顶部 ＋ 选一个目录", true); return; }

  ctx.innerHTML = "";
  const info = document.createElement("div");
  info.className = "title";
  info.textContent = dir;          // 建在哪儿，一眼看到，别靠猜
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  // openContextMenu 里的 sep 是它的局部函数，这里得自己来一份
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));
  item("新建文件…", () => newFile(dir));
  item("新建文件夹…", () => newDir(dir));
  sep();
  // 和目录右键里的「在此打开终端」是同一件事，只是入口放在这里 ——
  // 顶部那排的终端按钮已经拿掉了（见 90-tools.js 的说明），
  // 想直接开终端而不去树上找目录时，这个「＋」是最顺手的落点。
  // 工作目录用活动根：树里没有"当前选中目录"这个概念，猜不如给个确定的。
  item("在此打开终端", () => openTerminalAt(dir));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  const btn = $("treeAdd").getBoundingClientRect();
  // 键盘触发时 clientX/Y 是 0，退回按钮的位置（不然菜单会跑到左上角）
  const x = ev.clientX || btn.left;
  const y = ev.clientY || btn.bottom;
  ctx.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

$("treeAdd").addEventListener("click", treeAddMenu);

/**
 * 「.*」按钮：显不显示以点开头的文件和目录。
 *
 * 后端 `/api/fs/list` 一直有 `hidden` 开关，只是界面没接 —— 于是 `.ai`、`.git`、
 * `.gradle` 这些永远看不见。可有些事非看不可：清理 `.ai/` 里的缓存、
 * 看一眼 `.gradle` 有没有写进去、确认 `.gitignore` 是谁改的。
 *
 * 默认**不显示**：这些目录在 99% 的时候只是噪音，而且 `.git` 一动就是几千个对象。
 */
function paintTreeHidden() {
  const b = $("treeHidden");
  if (!b) return;
  b.classList.toggle("on", !!S.showHidden);
  b.title = S.showHidden
    ? "正在显示以 . 开头的文件和目录 —— 点一下隐藏"
    : "只显示普通文件。点一下把 .ai、.git 这些也列出来";
}

async function toggleTreeHidden() {
  S.showHidden = !S.showHidden;
  paintTreeHidden();
  // 记住这个选择：它跟"这个工作区怎么摆"是一类，跟 .ai/save.json 走
  try { await api("/api/state", { showHidden: S.showHidden }); } catch (e) { /* 记不住不算事 */ }
  // 先试缓存里**另一种开关**那份：同一棵树按开关存了两份变体（见 treeSlotKey），
  // 大部分情况点一下就换过去、一次盘都不列。没有那份才 loadTree 重列。
  //
  // 为什么值得这么写：`.*` 按钮是"看一眼再点回去"的操作，原来每次都整棵重列，
  // 大工程（gu/jv 那种）点一下要等好几秒。
  const mounted = treeStashKey;
  if (!(mounted && typeof mountTreeFromCache === "function"
        && mountTreeFromCache(mounted, { force: true }))) {
    await loadTree();
  } else if (typeof clearTreeMount === "function") {
    // 挂树会把 treeMountPending 置上（那是给"切工作区"那一轮用的）。
    // 这里不是切换，必须立刻解除 —— 否则之后在这个工作区里新建 / 删除 / 上传
    // 触发的 loadTree 会被它一直挡住，左栏停在改之前的样子。
    clearTreeMount();
    treeShownKey = "";
  }
  toast(S.showHidden ? "已显示隐藏文件" : "已隐藏 . 开头的项");
}

$("treeHidden").addEventListener("click", toggleTreeHidden);

// ---------------------------------------------------------------------------
// 下载到本机
// ---------------------------------------------------------------------------

/**
 * 把工作区里的一个文件下载到本机。
 *
 * 为什么不直接用 `/api/fs/raw?path=…` 这种「带 token 的直链」：
 * 浏览器发起的下载是裸 GET，`<a download>` 带不了 Authorization 头；
 * 而把登录 token 拼进 URL 会留在历史记录、代理日志里。所以复用预览那套
 * **10 分钟短票据**（`POST /api/preview/ticket`）：先拿票，再拼地址交给浏览器。
 *
 * 用 `<a download>` 而不是 `window.open`：后者会把 zip 变成"在线打开"，
 * 有些浏览器对 octet-stream 还是会先弹预览窗；`download` 属性一步到位。
 * 也不走 fetch+blob —— 那得把整个文件读进内存，几百兆的 apk 直接把页面干崩。
 */
async function downloadFile(path) {
  if (!path) { toast("没有可下载的文件"); return; }
  let ticket = "";
  try {
    const r = await api("/api/preview/ticket", { method: "POST" });
    ticket = r && (r.ticket || (r.result && r.result.ticket)) || "";
  } catch (e) {
    toast("取下载票据失败：" + (e && e.message ? e.message : e));
    return;
  }
  if (!ticket) { toast("取下载票据失败"); return; }

  const url = "/api/download?" + new URLSearchParams
    ({ path: path, ticket: ticket }).toString();
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split(/[\\/]/).pop() || "download.bin";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 0);
  // 任务面板里留一条：真正传输由浏览器接管（前端拿不到进度），
  // 所以这里只记"已交给浏览器"，耗时就是取票 + 触发的那一下。
  try {
    const tid = taskStart("下载 " + a.download, "download");
    taskDone(tid, "已交给浏览器");
  } catch (e) { /* 任务面板坏了不该影响下载 */ }
  toast("已开始下载：" + a.download);
}

// ---------------------------------------------------------------------------
// 文件树快速跳转（type-ahead）
// ---------------------------------------------------------------------------
//
// 场景：点开一个文件之后想切到隔壁那个 `tb_xxx.kt` —— 现在只能在树里拿滚轮翻。
// 这个功能让「点完文件直接敲 tb」就跳到 tb 开头的项（和资源管理器 / VS Code 的
// 快速输入一个意思）。
//
// 几条硬约束，决定了它只能这么写：
//   · **只筛当前已展开、且当前看得见**的节点。折叠的目录其实还在 DOM 里
//     （折叠只是给子容器加 `hidden`，见 makeNode），所以判据必须是
//     `n.offsetParent !== null`，"节点存在"不等于"你看得见"。
//   · **不问后端**。DOM 里已经有这些条目了，敲一个字母发一趟 `/api/fs/list`
//     在远程工作区就是一次 SSH 往返，手感直接废掉。
//   · **不改选中态**。`.node.sel` 是 Del 键删除的依据（见上面那段 keydown），
//     跳转只是"跳过去"，顺手改选中态就等于敲两个字母后一下 Del 删错了文件。
//   · **只敲门后敲的字**：焦点必须在 body（编辑器 / 输入框 / 终端里敲字一律不抢），
//     文件视图正在显示，没有弹窗、没有右键菜单。这条和 Del 键那套条件同源。
var TF_Q = "";        // 当前筛选词（小写，统一按小写比 —— Windows 路径本来就不分大小写）
var TF_HITS = [];     // 命中节点，按 DOM 顺序 = 从上到下的显示顺序
var TF_IDX = -1;      // 当前项在 TF_HITS 里的下标

/** 拿树里**现在看得见**的节点（折叠的子树会被 offsetParent 挡掉）。 */
function treeFindVisibleNodes() {
  const out = [];
  for (const n of treeBox.querySelectorAll(".node")) {
    if (!n._entry) continue;              // 空目录提示之类不是条目
    if (n.offsetParent === null) continue;
    out.push(n);
  }
  return out;
}

/** 摘掉所有跳转高亮（不动 `.sel`）。 */
function treeFindClearMarks() {
  for (const n of treeBox.querySelectorAll(".node.tf-hit,.node.tf-cur")) {
    n.classList.remove("tf-hit", "tf-cur");
  }
}

/** 只重画「当前项」那一格，不重算命中集（按 Enter / ↑↓ 用）。 */
function treeFindPaintCur() {
  for (const n of TF_HITS) n.classList.remove("tf-cur");
  const cnt = $("treeFindCount");
  if (TF_IDX < 0 || !TF_HITS[TF_IDX]) {
    if (cnt) cnt.textContent = "";
    return;
  }
  TF_HITS[TF_IDX].classList.add("tf-cur");
  TF_HITS[TF_IDX].scrollIntoView({ block: "nearest" });
  if (cnt) cnt.textContent = (TF_IDX + 1) + "/" + TF_HITS.length;
}

/**
 * 按 TF_Q 重算命中集并高亮。
 *
 * 匹配分两层：**先当前缀**（敲 "tb" 优先落在 tb 开头的项上，这正是需求里要的），
 * 前缀一个都没有时才退回"名字里包含"（想用 mid 里的关键字找 `build_mid.sh` 也行）。
 * 两层都基于 `_entry.name`（末级名字）—— 折叠目录显示的是整串 label，
 * 拿 label 匹配会让上级目录名也命中，反而看不清是哪一项。
 */
function treeFindRefresh() {
  const cnt = $("treeFindCount");
  // `TF_Q` 是 var（在文件末尾初始化）：loadTree 在页面刚起来时可能比它更早跑到，
  // 那时读出来是 undefined —— 兜一下，不然 `!q` 判真会把已有的高亮当"没词"清掉。
  const q = TF_Q || "";
  if (!q) {
    treeFindClearMarks();
    TF_HITS = [];
    TF_IDX = -1;
    if (cnt) cnt.textContent = "";
    return;
  }
  const all = treeFindVisibleNodes();
  const names = all.map((n) => String((n._entry && n._entry.name) || "").toLowerCase());
  let hits = [];
  all.forEach((n, i) => { if (names[i].startsWith(q)) hits.push(n); });
  if (!hits.length) {
    all.forEach((n, i) => { if (names[i].indexOf(q) >= 0) hits.push(n); });
  }
  // 上一个当前项：敲字前后命中的往往是同一批，留着它就不用每敲一个字母都跳回第一个
  const prev = (TF_IDX >= 0) ? TF_HITS[TF_IDX] : null;
  treeFindClearMarks();
  for (const n of hits) n.classList.add("tf-hit");
  TF_HITS = hits;
  if (!hits.length) {
    TF_IDX = -1;
    if (cnt) cnt.textContent = "无匹配";
    return;
  }
  let idx = prev ? hits.indexOf(prev) : -1;
  if (idx < 0) idx = 0;
  TF_IDX = idx;
  treeFindPaintCur();
}

/** 在当前命中集里往前 / 往后挪一项（循环）。dir = 1 下一项，-1 上一项。 */
function treeFindStep(dir) {
  if (!TF_HITS.length) {
    treeFindRefresh();
    if (!TF_HITS.length) return;
  }
  const n = TF_HITS.length;
  let i = (TF_IDX < 0) ? (dir > 0 ? 0 : -1) : TF_IDX + dir;
  i = ((i % n) + n) % n;
  TF_IDX = i;
  treeFindPaintCur();
}

/** 打开跳转条。`seed` = 刚敲下的那个字符（鼠标点按钮时不传）。 */
function treeFindOpen(seed) {
  const bar = $("treeFindBar");
  const inp = $("treeFindInput");
  if (!bar || !inp) return;
  if (bar.classList.contains("hidden")) {
    bar.classList.remove("hidden");
    inp.value = seed || "";
  } else if (seed && document.activeElement !== inp) {
    // 条还开着，但焦点早被点走了（人刚点过树上的文件 / 编辑器）：这一下当成**新的开始**，
    // 接着拼上一轮的词只会让人越敲越长、还跳不到想去的地方。
    inp.value = seed;
  } else if (seed) {
    inp.value = (inp.value || "") + seed;
  }
  TF_Q = String(inp.value || "").toLowerCase();
  treeFindRefresh();
  inp.focus();
  try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (e) { /* 忽略 */ }
}

/** 关掉跳转条，把焦点还给 body。 */
function treeFindClose() {
  const bar = $("treeFindBar");
  const inp = $("treeFindInput");
  if (bar) bar.classList.add("hidden");
  if (inp) inp.value = "";
  TF_Q = "";
  TF_HITS = [];
  TF_IDX = -1;
  treeFindClearMarks();
  const cnt = $("treeFindCount");
  if (cnt) cnt.textContent = "";
  // 焦点必须还回去：留在输入框上的话，条一藏起来，下次点完文件敲字母就全进了
  // 一个看不见的输入框里 —— 表现是"跳转失效了"。
  const act = document.activeElement;
  if (act && act !== document.body && typeof act.blur === "function") act.blur();
}

(function wireTreeFind() {
  const inp = $("treeFindInput");
  if (!inp) return;
  inp.addEventListener("input", () => {
    TF_Q = String(inp.value || "").toLowerCase();
    treeFindRefresh();
  });
  inp.addEventListener("keydown", (e) => {
    if (e.isComposing) return;                    // 中文输入法正在选词
    if (e.key === "Enter") {
      e.preventDefault();
      // 别冒泡出去：别处也有 Enter 的监听，一次回车不该连带触发别人的动作
      e.stopPropagation();
      treeFindStep(e.shiftKey ? -1 : 1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      // 同理：90-tools.js 那份 Esc 是用来关终端 / 清选择的，这里只收跳转条
      e.stopPropagation();
      treeFindClose();
    }
  });
  $("treeFindUp").addEventListener("click", () => treeFindStep(-1));
  $("treeFindDown").addEventListener("click", () => treeFindStep(1));
  $("treeFindX").addEventListener("click", () => treeFindClose());
  // 鼠标入口：⌕ 按钮（再点一下收起）
  $("treeFindBtn").addEventListener("click", () => {
    const bar = $("treeFindBar");
    if (!bar || bar.classList.contains("hidden")) treeFindOpen("");
    else treeFindClose();
  });
  // 键盘入口：Ctrl+F。
  // **只在焦点是 body 时**接管 —— 焦点在编辑区里时，Ctrl+F 是编辑器自己的查找，
  // 抢过来会让人找不到代码里的一段字，那是更常用的功能。
  // 走 capture 并停掉传播：编辑器那份监听挂在 document 上，不让它顺手也开一个框。
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey)) return;
    if (e.key !== "f" && e.key !== "F") return;
    if (e.isComposing) return;
    const act = document.activeElement;
    if (act && act !== document.body && act !== document.documentElement) return;
    if (!treeBox.offsetParent) return;             // 文件视图没在显示
    if (!$("modal").classList.contains("hidden")) return;
    e.preventDefault();
    e.stopPropagation();
    treeFindOpen("");
  }, true);
})();

// 裸字符拦截：焦点在 body 上、文件视图正显示、没有弹窗 / 右键菜单时才接管。
// 条件写得很紧，理由和上面 Del 键那段一样 —— 抢键盘的代价是别处输入失灵。
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  if (e.isComposing) return;
  if (e.key.length !== 1) return;              // 只认可打印单字符；Enter/Esc/方向键有自己的路
  const act = document.activeElement;
  if (act && act !== document.body && act !== document.documentElement) return;
  if (!treeBox.offsetParent) return;           // 文件视图没在显示
  if (!$("modal").classList.contains("hidden")) return;
  if (!$("ctx").classList.contains("hidden")) return;
  if (!String(e.key).trim()) return;           // 空格留给滚动，不抢
  e.preventDefault();
  treeFindOpen(e.key);
});


