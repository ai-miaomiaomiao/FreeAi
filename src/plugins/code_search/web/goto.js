// ---------------------------------------------------------------------------
// 转到定义（F12 / Ctrl+左键）与查引用（Shift+F12）—— 按 VS Code 的语义
// ---------------------------------------------------------------------------
// 键位和 VS Code 对齐：
//   F12 / Ctrl+左键    Go to Definition —— 只找定义，**不**反查引用
//   Shift+F12          Find All References
//   ↑↓ / Enter / Esc   在多条结果之间选、跳、关
//
// 以前是"一个键双向"：在引用处查定义，在定义处反过来查引用。看着聪明，
// 但不符合肌肉记忆 —— 在定义上按 F12 突然刷出一屏引用，人只会以为按错了。
//
// 和 VS Code 对齐的另外几个细节：
//   · 一条结果也走 peek 列表，Enter / 点一下才跳（光标落在符号上，不是整行）；
//   · 多条结果用原地浮层（peek），不切到左边的搜索视图、不打断思路；
//   · 找不到定义不弹 toast（那玩意儿挡视线），只在状态栏写一句；
//   · 按住 Ctrl 时，光标所在的词下面画一条下划线，提示"这里能点"。
const PEEK = {
  items: [], idx: 0, open: false, scope: "", word: "", kind: "",
  // 标题尾部那句进度说明（查引用渐进时用：「本模块 N 处，正在补全仓…」）
  suffix: "",
  // 按**树节点**记录的折叠状态：节点 key（`d:<目录>` / `f:<完整路径>`）→ 是否折叠。
  // 换一份列表时由 peekFoldInit 清空，再由 peekFoldSync 按 peekFoldDefault
  // 逐节点写入默认值 —— 这里**不再**假定"不在表里 = 展开"，
  // 否则渐进补全新长出来的节点会一律摊开，跟默认折叠的策略相反。
  // 为什么不存下标：full 阶段会整批换掉 items，按下标记一换就全错位了。
  fold: {},
  // 当前编辑器文件所在目录（相对搜索根，`/` 分隔；根目录记 `(根目录)`）。
  // 折叠默认值要区分"本模块"和"别的模块"，靠它比。
  home: "",
  // 当前**可见**命中行的原始 item 下标序列（↑↓ 只在这条序列上走，
  // 折叠起来的条目自然被跳过）。目录行 / 文件行不是条目，所以不能算进来。
  rows: [],
  // 原始下标 → 那一行 DOM（滚动定位用；以前靠 children[idx+1]，
  // 加了目录/文件行之后这个偏移就不成立了）
  els: {},
  // 标题栏 ⟳ 的回调与「正在重查」状态。只有查引用时才有 ——
  // 定义只有一次查询，没有第二阶段可补。
  retry: null,
  retryBusy: false,
};
let gotoBusy = false;
let gotoMarkOn = false;

/** 光标所在的那个标识符（含字符区间）。不在词上返回 null。 */
function wordRangeAtCursor() {
  const ta = $("edArea");
  const pos = ta.selectionStart;
  if (pos !== ta.selectionEnd) return null;
  return wordRangeAt(pos);
}

/** 文本第 pos 个字符处的标识符区间。不在词上返回 null。
 *
 * 从 wordRangeAtCursor 里拆出来，是为了让**鼠标位置**也能复用：
 * 按住 Ctrl 时下划线要跟鼠标走，而鼠标反算出来的是一个字符偏移，
 * 不是 textarea 的选区（selectionStart 只随点击/键盘变）。
 */
function wordRangeAt(pos) {
  const ta = $("edArea");
  const v = ta.value;
  if (!(pos >= 0)) return null;
  if (pos > v.length) pos = v.length;

  let s = pos;
  let e = pos;
  while (s > 0 && /[A-Za-z0-9_$]/.test(v[s - 1])) s--;
  while (e < v.length && /[A-Za-z0-9_$]/.test(v[e])) e++;
  if (s === e) return null;

  const word = v.slice(s, e);
  if (!/^[A-Za-z_$]/.test(word)) return null;      // 纯数字不算符号
  return { from: s, to: e, word };
}

/** 隐形尺子量一段文本的像素宽度。尺子由 auto_code 插件创建（COMP_MIRROR），
 *  插件被禁用时它根本不存在 —— 量不到就返回 -1，让调用方放弃画线，
 *  而不是按住 Ctrl 的每一帧都抛 ReferenceError。 */
function mirrorWidth(t) {
  const m = (typeof COMP_MIRROR !== "undefined" && COMP_MIRROR) ? COMP_MIRROR : null;
  if (!m) return -1;
  m.textContent = t;
  return m.getBoundingClientRect().width;
}

/** 一段字符区间在编辑区里的像素位置（画下划线用，和补全共用那把隐形尺子）。
 *  量不到（尺子不在）时返回 null。 */
function charSpanPos(from, to) {
  const ta = $("edArea");
  const before = ta.value.slice(0, from);
  const nl = before.lastIndexOf("\n");
  const row = before.split("\n").length;          // 1-based
  const cs = getComputedStyle(ta);
  const lh = parseFloat(cs.lineHeight) || 22;
  const padL = parseFloat(cs.paddingLeft) || 12;
  const padT = parseFloat(cs.paddingTop) || 10;

  const x1 = mirrorWidth(before.slice(nl + 1));
  const x2 = mirrorWidth(ta.value.slice(nl + 1, to));
  if (x1 < 0 || x2 < 0) return null;

  return {
    left: padL + x1 - ta.scrollLeft,
    // row 是 1-based：首行的顶边就是 paddingTop 自己，所以要减 1。
    // 参照 80-editor.js efMarkPaint 的 `k.padT + (k.row - 1) * k.lh`；
    // 这里以前没减，整条下划线正好掉到下一行去。
    top: padT + (row - 1) * lh - ta.scrollTop,
    width: Math.max(2, x2 - x1),
    height: lh,
  };
}

// ---- 鼠标位置 → 字符偏移 ----
// textarea 不暴露"哪个字符落在哪个像素"，只能反算：
//   ① 竖直方向是等行高的，y 直接除以行高得到行号；
//   ② 行内拿隐形尺子量前缀宽度，二分找列。
// 前提是尺子（.ed-measure）和正文的字体 / tab 宽度一致 —— style.css 里那两条
// 就是照抄 .ed-area 的，动那边记得同步。
const GOTO_HOVER = { x: 0, y: 0, on: false, cache: null };

/** 第 row 行（0-based）在全文里的起始偏移；行不存在返回 -1。
 *  同一行内左右滑动是最高频的场景，按 (文本长度, 行号) 缓存一下，
 *  免得鼠标每挪一个像素就从文件头重扫一遍换行符。 */
function lineStartOf(row) {
  const v = $("edArea").value;
  const c = GOTO_HOVER.cache;
  if (c && c.len === v.length && c.row === row) return c.start;
  let start = 0;
  let line = 0;
  for (let i = 0; i < v.length && line < row; i++) {
    if (v.charCodeAt(i) === 10) { line++; start = i + 1; }
  }
  if (line < row) return -1;                  // 鼠标落在文末下方的空白里
  GOTO_HOVER.cache = { len: v.length, row: row, start: start };
  return start;
}

/** 视口坐标 → 字符偏移。落在行号槽 / 内边距 / 行尾右侧空白上返回 -1。 */
function offsetAtPoint(clientX, clientY) {
  const ta = $("edArea");
  if (!ta) return -1;
  const cs = getComputedStyle(ta);
  const lh = parseFloat(cs.lineHeight) || 22;
  const padL = parseFloat(cs.paddingLeft) || 12;
  const padT = parseFloat(cs.paddingTop) || 10;
  const r = ta.getBoundingClientRect();
  // 换成"内容坐标"：减掉元素左上角，再加回已经滚过去的距离
  const cx = clientX - r.left + ta.scrollLeft - padL;
  const cy = clientY - r.top + ta.scrollTop - padT;
  if (cx < 0 || cy < 0) return -1;

  const start = lineStartOf(Math.floor(cy / lh));
  if (start < 0) return -1;

  const v = ta.value;
  let end = v.indexOf("\n", start);
  if (end < 0) end = v.length;
  const text = v.slice(start, end);

  // 二分：找最大的 k，使 text[0..k) 的宽度不超过 cx
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    const w = mirrorWidth(text.slice(0, mid));
    if (w < 0) return -1;                     // 尺子不在，放弃
    if (w <= cx) lo = mid; else hi = mid - 1;
  }
  // 鼠标停在这一行文字右边的空白里（离最后一个字符超过一个字符宽）：不算压在词上
  if (lo >= text.length) {
    const wl = mirrorWidth(text);
    if (wl < 0 || cx - wl > 8) return -1;
  }
  return start + lo;
}

// ---- Ctrl 按住时的下划线提示 ----
function gotoMarkHide() {
  const mark = $("edGotoMark");
  if (mark) mark.classList.add("hidden");
  // 只清自己写的那句，别把"待保存…"之类误删
  if (($("edStatus").textContent || "").indexOf("Ctrl+点击") === 0) setEdStatus("");
}

function gotoMarkRefresh() {
  const mark = $("edGotoMark");
  if (!mark) return;

  const e = ed();
  if (!gotoMarkOn || !e || e.preview || e.remote) { gotoMarkHide(); return; }

  // 取哪个词：鼠标在编辑区里就以**鼠标**为准（Ctrl 按着挪鼠标是这里的主场景），
  // 鼠标不在里面（刚按下 Ctrl、键盘移动光标）才退回光标所在的词。
  let r = null;
  if (GOTO_HOVER.on) {
    const off = offsetAtPoint(GOTO_HOVER.x, GOTO_HOVER.y);
    if (off >= 0) r = wordRangeAt(off);
  } else {
    r = wordRangeAtCursor();
  }
  if (!r) { gotoMarkHide(); return; }

  const p = charSpanPos(r.from, r.to);
  if (!p) { gotoMarkHide(); return; }
  mark.style.left = p.left + "px";
  mark.style.top = (p.top + p.height - 2) + "px";
  mark.style.width = p.width + "px";
  mark.classList.remove("hidden");
  // 正在查的时候别抢状态栏：gotoAtCursor 已经写了"查找定义…"，
  // 再盖成"Ctrl+点击跳转到定义：xxx"会让人以为点了没反应。
  // 文案不带方向了 —— Ctrl+左键是双向的，点下去才知道往哪边跳
  if (!gotoBusy) setEdStatus("Ctrl+点击跳转：" + r.word);
}

// ---- peek 浮层（多条定义 / 查引用）----
function peekOpen(items, res, kind, word, suffix) {
  compClose();                     // 别和补全列表叠在一起
  PEEK.items = items;
  PEEK.idx = 0;
  PEEK.open = true;
  PEEK.scope = (res && res.scope) || S.workspace;
  PEEK.word = word || "";
  PEEK.kind = kind;
  PEEK.suffix = suffix || "";
  // 本模块 = 当前编辑器文件所在目录。必须在 PEEK.scope 定下来之后算 ——
  // peekDirOf 的退路就是"相对 PEEK.scope 截目录"。取不到（没打开文件 /
  // 预览态 / 设备文件）就留空串，那样任何一组都不算"自己模块"，一律折叠。
  const eh = ed();
  PEEK.home = (eh && eh.path && !eh.preview && !eh.remote)
    ? peekDirOf({ path: eh.path })
    : "";
  // 换了词 / 换了查询 = 换了一份列表，折叠状态和 ⟳ 都是上一份的，一起清掉。
  // 先清空，具体默认值由 peekPaint 里的 peekFoldSync 按组写入
  peekFoldInit();
  PEEK.retry = null;
  PEEK.retryBusy = false;
  peekPaint();
  // 两层一起开：mask 铺满视口做半黑遮罩，box 是被 flex 居中的那个框
  $("edPeek").classList.remove("hidden");
  $("edPeekMask").classList.remove("hidden");
}

/** 一条结果所属的目录（相对工作区根，`/` 分隔；根目录记成 `(根目录)`）。
 *
 *  优先用后端算好的 `folder`：远程工作区里 `path` 是 `ssh://…` 标识，
 *  自己截目录会截出 `ssh://user@host:port/work/…` 这种没法当目录名看的串。
 *  没有这个字段（老结果 / 别的接口）才从相对路径里退一步截。
 */
function peekDirOf(it) {
  if (it && it.folder) return String(it.folder);
  const p = String(relPath(it && it.path, PEEK.scope) || (it && it.path) || "?")
    .replace(/\\/g, "/");
  const n = p.lastIndexOf("/");
  // 根目录必须和后端 `_folder_of` / `_ws_folder` 的写法一字不差 —— 它们都
  // 把"文件就在搜索根下"记成 `(根目录)`。以前这里写 `.`，于是同一个根同时
  // 出现两种写法（后端给了 folder 的用 `(根目录)`、退路算出来的用 `.`），
  // 而且「本模块」拿 `.` 去比永远比不上 `(根目录)`，自己模块反而被折叠。
  return n > 0 ? p.slice(0, n) : "(根目录)";
}

/** 一条结果拆成「目录串 + 文件名」，给 peekTree 建树用。 */
function peekSplitOf(it) {
  const dir = peekDirOf(it);
  const full = String((it && it.path) || "?").replace(/\\/g, "/");
  const n = full.lastIndexOf("/");
  return { dir: dir, full: full, name: n >= 0 ? full.slice(n + 1) : full };
}

/** 把结果列表建成 **目录 → 文件 → 命中行** 三级树（和左侧文件树同一套观感）。
 *
 *  以前是按目录一级平铺，每个组头写一整串路径、每条命中再写一遍完整
 *  `路径:行号` —— 同一个目录出现十次，屏幕大部分面积在重复同一段路径。
 *  现在路径只出现一次（在树上），每条命中行只剩 `:行号 + 所属类·函数`。
 *
 *  为什么保留「目录」这一层、不做成扁平的按文件分组：一个目录里往往躺着
 *  同一个类的正本和一堆同模块调用点，扁平分组会把它们拆成十几个只含一两行的
 *  分组，分组头比内容还长。
 *
 *  为什么不按路径字典序排：节点顺序就是键盘顺序，字典序会让人按 ↓ 时看着
 *  选中项在列表里来回跳。保持各项**首次出现**的顺序（后端已按相关度/行号排过）。
 */
function peekTree() {
  const root = {
    // depth 取 -1：让顶层节点的 depth 是 0（缩进 = depth × 12px），
    // 不用为"根这一层不渲染"再特判
    kind: "root", depth: -1, children: [],
    dirs: Object.create(null), files: Object.create(null),
  };
  PEEK.items.forEach((it, i) => {
    const sp = peekSplitOf(it);
    // `(根目录)` 表示文件就在搜索根下，没有目录段
    const segs = (sp.dir === "(根目录)" ? "" : sp.dir).split("/").filter(Boolean);
    let node = root;
    let acc = "";
    for (const s of segs) {
      acc = acc ? acc + "/" + s : s;
      let d = node.dirs[s];
      if (!d) {
        d = node.dirs[s] = {
          kind: "dir", name: s, key: "d:" + acc, path: acc,
          depth: node.depth + 1, children: [], idxs: [],
          dirs: Object.create(null), files: Object.create(null),
        };
        node.children.push(d);
      }
      d.idxs.push(i);
      node = d;
    }
    // 同一目录下同名文件只有一份，用文件名当键就够
    let f = node.files[sp.name];
    if (!f) {
      f = node.files[sp.name] = {
        kind: "file", name: sp.name, key: "f:" + sp.full, path: sp.full,
        // 折叠默认值要拿它和 PEEK.home 比（后端 `folder` 口径，和 peekDirOf 一致）
        dirKey: sp.dir, depth: node.depth + 1, idcs: [], idxs: [], children: [],
      };
      node.children.push(f);
    }
    f.idxs.push(i);
  });
  peekTreeCount(root);
  // 只有一个文件时省掉目录层：整个列表只有一条路径，目录行纯属占地方。
  // 文件名换成相对工作区的完整路径，路径信息不丢。
  if (root.nfiles === 1) {
    const only = peekTreeFirstFile(root);
    if (only) {
      only.depth = 0;
      only.name = String(relPath(only.path, PEEK.scope) || only.path).replace(/\\/g, "/");
      return [only];
    }
  }
  // 单链压缩：`aacplib/src/com/server/ui/aacp/manager` 八行并成一行。
  // 必须在 peekTreeCount 之后（要读 nfiles）、在默认折叠之前（peekFoldSync 在
  // peekPaint 里才跑），否则算出的是压缩前的节点。
  peekCompactDirs(root.children, 0);
  return root.children;
}

/** 递归数每个目录下有几个文件（目录行要显示「N 处 / M 个文件」）。 */
function peekTreeCount(n) {
  if (n.kind === "file") return 1;
  let c = 0;
  for (const ch of n.children) c += peekTreeCount(ch);
  n.nfiles = c;
  return c;
}

/** 树上第一个文件节点（只在「整份列表只有 1 个文件」时用一次）。 */
function peekTreeFirstFile(n) {
  for (const ch of n.children) {
    if (ch.kind === "file") return ch;
    const r = peekTreeFirstFile(ch);
    if (r) return r;
  }
  return null;
}

/** 把「一路只有一个子目录」的链并成一行（`aacplib/src/com/server`）。
 *
 *  对应 VS Code 的 `explorer.compactFolders` / 左侧文件树的 `compact=1`
 *  （`agent_tools/core.py:204 merge_dir_chain`、`40-tree.js:245`）。Android 工程
 *  `src/main/java/com/xxx` 一路点下去要点五次，而每一层都不提供任何"往哪走"的
 *  信息 —— 查引用时这种链会把列表撑成八行只为显示一个文件，看着完全不像文件树。
 *
 *  **只并目录**（和左侧树一致）：中间只要出现一个文件就停，那一刻这一层开始有
 *  信息量了。合并后：
 *   · `name` 拼成 `a/b/c`，`path` / `idxs` / `nfiles` / `children` 换成最深那段；
 *   · `paths` 留链上每一层的完整路径 —— 默认折叠要拿它和 PEEK.home 比，
 *     否则 home 落在链中间时 `n.path === PEEK.home` 不再成立（见 peekFoldDefault）；
 *   · `key` 跟**链首**走：渐进补全把链又接长一段，折叠状态仍是同一个节点。
 */
function peekCompactDirs(nodes, depth) {
  for (const n of nodes) {
    n.depth = depth;
    if (n.kind !== "dir") continue;
    const paths = [n.path];
    while (n.children.length === 1 && n.children[0].kind === "dir") {
      const c = n.children[0];
      n.name = n.name + "/" + c.name;
      paths.push(c.path);
      n.path = c.path;
      n.idxs = c.idxs;
      n.nfiles = c.nfiles;
      n.children = c.children;
    }
    n.paths = paths;
    n.chain = paths.length;
    n.key = "d:" + paths[0];
    peekCompactDirs(n.children, depth + 1);
  }
}

/** 「本模块」那个文件最多摊开几条。别的一律折叠，见 peekFoldDefault。
 *
 *  为什么是 10：查引用在真实工程里动不动 30~200 处，一条条摊开就是一屏滚到
 *  底也看不到有几个文件、哪一片最密集。先收起目录、只把文件名和处数亮出来，
 *  用户看一眼（`ConnectManager.kt 37 处`）就知道该展开哪一片，比从头滚一遍快。
 *  本模块 10 条以内一屏能看完，硬折起来反而多一步点击，所以阈值卡在这里。
 */
const PEEK_AUTO_FOLD_OVER = 10;

/** 一个树节点默认是展开还是折叠。
 *
 *  规则（用户定的，和 VS Code 那种"全看总数"不同）：
 *   · **目录**：本模块（`PEEK.home`）的祖先链逐级展开，好让人顺着树走到自己
 *     那个文件；别的目录一律折叠 —— 哪怕下面只有 2 条。
 *   · **文件**：所在目录 = 本模块、且该文件命中**不超过 10 条**才展开；
 *     别的文件一律折叠；本模块那个文件超过 10 条也折叠。
 *
 *  为什么本模块这么特殊：查引用时本模块那几处几乎总是要找的，摊开能直接按
 *  ↓ 选；而别的模块动不动几十上百条，摊开只会把人淹掉。阈值按**单个文件**
 *  的条数算而不是总数：一个 200 处的引用里，本模块往往只有 5~8 处。
 */
function peekFoldDefault(n) {
  if (!PEEK.home) return !(n.kind === "file" && !n.depth);
  if (n.kind === "dir") {
    // 自己是 home，或是 home 的祖先 → 展开，把路径一路铺到本模块。
    // 必须看 paths（压缩后链上每一层）：home 落在链中间时，节点自己的 path
    // 已经是链尾的深层目录，只比 path 会让本模块那一行被折起来看不见。
    return !(n.paths || [n.path]).some(
      (p) => p === PEEK.home || PEEK.home.indexOf(p + "/") === 0);
  }
  if (n.dirKey !== PEEK.home) return true;
  return n.idxs.length > PEEK_AUTO_FOLD_OVER;
}

/** 换了一份列表：清掉上一份的折叠状态。
 *
 *  默认值不在这里写 —— 交给 peekFoldSync 在 peekPaint 里按节点补。原因是渐进
 *  补全（`defDeepen` / `refDeepen`）会往同一份列表里追加条目、长出新的节点，
 *  这里插不进去。
 */
function peekFoldInit() {
  PEEK.fold = {};
}

/** 给**还没快照过**的节点写入默认折叠值（递归走整棵树）。
 *
 *  每份列表只写一次：写过之后用户怎么点就怎么留着，本模块从 5 条补到 50 条
 *  也不会突然被收起 —— 正在看的一片凭空消失，比多摊开几条更难受。
 *  渐进补全新长出来的节点不在这张表里，于是按 peekFoldDefault 得到折叠态，
 *  正是想要的（新出现的通常是别的模块）。
 */
function peekFoldSync(nodes) {
  for (const n of nodes) {
    if (!(n.key in PEEK.fold)) PEEK.fold[n.key] = peekFoldDefault(n);
    peekFoldSync(n.children);
  }
}

function peekPaint() {
  const box = $("edPeek");
  box.innerHTML = "";
  PEEK.rows = [];
  PEEK.els = {};

  // 先建树：标题栏的「全部折叠」按钮要拿节点数，必须在建标题之前算好
  const nodes = peekTree();
  // 给新出现的节点补默认折叠值（本模块祖先链展开，其余折叠）
  peekFoldSync(nodes);
  // 所有可折叠节点（目录 + 文件）摊成一份，给「全部折叠」按钮算状态用
  const foldables = [];
  (function walk(arr) {
    for (const n of arr) { foldables.push(n); walk(n.children); }
  })(nodes);
  const foldedCount = foldables.filter((n) => !!PEEK.fold[n.key]).length;
  const allFolded = foldables.length > 0 && foldedCount === foldables.length;

  // 标题：文字 + （只在查引用时）⟳ 重查按钮 + 全部折叠按钮
  const head = document.createElement("div");
  head.className = "ph";
  const ht = document.createElement("span");
  ht.className = "ph-txt";
  ht.textContent = (PEEK.kind === "references" ? "引用" : "定义") + "　"
    + PEEK.word + "　" + PEEK.items.length + " 处"
    + (PEEK.suffix || "")
    + "　（↑↓ 选，Enter 跳，Esc 关）";
  head.appendChild(ht);
  if (typeof PEEK.retry === "function") {
    const rt = document.createElement("button");
    rt.type = "button";
    rt.className = "ph-retry" + (PEEK.retryBusy ? " busy" : "");
    rt.textContent = PEEK.retryBusy ? "…" : "⟳";
    rt.title = "重查一遍全仓，结果原地替换这条列表（滚动位置和选中项保留）";
    if (PEEK.retryBusy) rt.disabled = true;
    // mousedown + preventDefault：和行一样，别让 textarea 先失焦把选区弄丢
    rt.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!PEEK.retryBusy) PEEK.retry();
    });
    head.appendChild(rt);
  }
  // 全部折叠 / 展开：树里有可折叠节点时才出现 —— 没有节点就折叠等于整屏空白
  if (foldables.length) {
    const fb = document.createElement("button");
    fb.type = "button";
    fb.className = "ph-retry ph-fold";     // 复用 ⟳ 的按钮外观，只多一个 nowrap
    fb.textContent = allFolded ? "全部展开" : "全部折叠";
    fb.title = allFolded
      ? ("展开全部 " + foldables.length + " 个节点")
      : ("折叠全部 " + foldables.length + " 个节点（只剩目录和文件名，好找密集的那一片）");
    // mousedown + preventDefault：和行、⟳ 一致，别让 textarea 先失焦丢选区
    fb.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      // 逐节点显式写值，不能靠清空表：peekFoldSync 会把空表立刻刷回默认值，
      // 点「全部展开」就等于没反应
      for (const n of foldables) PEEK.fold[n.key] = !allFolded;
      peekPaint();
    });
    head.appendChild(fb);
  }
  box.appendChild(head);

  // 递归画树：目录行 → 文件行 → 命中行（缩进按 depth 逐级加）
  const drawNodes = (arr) => {
    for (const n of arr) {
      // 折叠态一律查表（不跟按钮状态与在一起）：默认值已由 peekFoldSync 写全
      const folded = !!PEEK.fold[n.key];
      const row = document.createElement("div");
      row.className = (n.kind === "dir" ? "pk-dir" : "pk-file")
        + (folded ? " fold" : "");
      row.style.paddingLeft = (2 + n.depth * 12) + "px";
      // 选中项在这个节点底下 → 目录/文件行也描一下：折叠起来后还能看出选在哪
      if (n.idxs.indexOf(PEEK.idx) >= 0) row.classList.add("on");

      const ar = document.createElement("span");
      ar.className = "ar";
      ar.textContent = folded ? "▸" : "▾";
      row.appendChild(ar);

      if (n.kind === "dir") {
        const ic = document.createElement("span");
        ic.className = "ic";
        // 和左侧文件树一致：展开 📂 / 折叠 📁（`40-tree.js:275/287`）
        ic.textContent = folded ? "📁" : "📂";
        row.appendChild(ic);
        const nm = document.createElement("span");
        nm.className = "nm";
        nm.textContent = n.name;
        nm.title = n.path;
        row.appendChild(nm);
        const cnt = document.createElement("span");
        cnt.className = "cnt";
        cnt.textContent = n.idxs.length + " 处"
          + (n.nfiles > 1 ? " / " + n.nfiles + " 个文件" : "");
        row.appendChild(cnt);
      } else {
        // 字母标识（K / J / Py …）和左侧文件树同一套：fileIconOf 由编辑器模块
        // 提供，插件被禁用时它不存在 —— 兜底成 📄，别让整个浮层报错
        const mark = (typeof fileIconOf === "function") ? fileIconOf(n.path) : "";
        const ic = document.createElement("span");
        ic.className = mark ? "ic src" : "ic";
        ic.textContent = mark || "📄";
        if (mark) ic.dataset.ic = mark;   // CSS 按 data-ic 分色，这里只给字母
        row.appendChild(ic);
        const nm = document.createElement("span");
        nm.className = "nm";
        nm.textContent = n.name;
        nm.title = n.path;
        row.appendChild(nm);
        const cnt = document.createElement("span");
        cnt.className = "cnt";
        cnt.textContent = n.idxs.length + " 处";
        row.appendChild(cnt);
      }

      row.title = (folded ? "展开" : "折叠")
        + (n.kind === "dir" ? "目录 " + n.path : "文件 " + n.path)
        // 压缩过就说明屏幕上这行顶掉了好几层，讲清楚免得以为路径少了一段
        + (n.kind === "dir" && n.chain > 1
          ? "（合并了 " + n.chain + " 层：中间每层只有一个子目录）"
          : "");
      // mousedown + preventDefault：否则 textarea 会先失焦、选区被挪走
      row.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        PEEK.fold[n.key] = !folded;
        peekPaint();
      });
      box.appendChild(row);

      if (folded) continue;
      if (n.kind === "dir") { drawNodes(n.children); continue; }

      // 命中行：只剩 `:行号 + 所属类·函数` + 源码 —— 路径已经在文件行上写过了
      for (const i of n.idxs) {
        const it = PEEK.items[i];
        const hit = document.createElement("div");
        hit.className = "pi" + (i === PEEK.idx ? " on" : "");
        hit.style.paddingLeft = (2 + (n.depth + 1) * 12 + 14) + "px";

        const loc = document.createElement("div");
        loc.className = "loc";
        const sc = it.scope || {};
        loc.textContent = ":" + it.line
          + (sc.cls || sc.fn ? "　" + (sc.cls || "") + (sc.fn ? "·" + sc.fn : "") : "");
        loc.title = String(it.path || "?") + ":" + it.line;

        const code = document.createElement("div");
        code.className = "code";
        code.textContent = it.text || "";

        hit.appendChild(loc);
        hit.appendChild(code);
        hit.addEventListener("mousedown", (ev) => { ev.preventDefault(); peekAccept(i); });
        box.appendChild(hit);
        PEEK.els[i] = hit;
        PEEK.rows.push(i);
      }
    }
  };
  drawNodes(nodes);

  // 选中项被折叠进去了（刚点了目录/文件行）→ 挪到它前面最近的可见行。
  // 不挪的话 ↑↓ 会从一个看不见的位置开始走，看着像第一次按方向键没反应。
  if (PEEK.rows.length && PEEK.rows.indexOf(PEEK.idx) < 0) {
    let pick = PEEK.rows[0];
    for (const i of PEEK.rows) {
      if (i < PEEK.idx) pick = i; else break;
    }
    PEEK.idx = pick;
    const el = PEEK.els[PEEK.idx];
    if (el) el.classList.add("on");
  }

  peekScroll();
}

function peekScroll() {
  const box = $("edPeek");
  const cur = PEEK.els[PEEK.idx];
  if (!box || !cur) return;
  const top = cur.offsetTop;
  const bottom = top + cur.offsetHeight;
  if (top < box.scrollTop) box.scrollTop = top;
  else if (bottom > box.scrollTop + box.clientHeight) {
    box.scrollTop = bottom - box.clientHeight;
  }
}

function peekClose() {
  if (!PEEK.open) return;
  PEEK.open = false;
  PEEK.items = [];
  PEEK.idx = 0;
  PEEK.suffix = "";
  PEEK.fold = {};
  PEEK.home = "";
  PEEK.rows = [];
  PEEK.els = {};
  PEEK.retry = null;
  PEEK.retryBusy = false;
  const box = $("edPeek");
  if (box) {
    box.classList.add("hidden");
    box.innerHTML = "";
  }
  // 遮罩必须跟着关：只关框的话，透明层还铺在视口上，整页都点不动了
  const mask = $("edPeekMask");
  if (mask) mask.classList.add("hidden");
}

function peekAccept(i) {
  const it = PEEK.items[i];
  if (!it) return;
  const scope = PEEK.scope;
  peekClose();
  gotoJump(it, { scope: scope });
}

/** 列表开着时，按键先给 peek。返回 true 表示这个键已经被吃掉了。 */
function peekKey(ev) {
  if (!PEEK.open) return false;

  if (ev.key === "Escape") { ev.preventDefault(); peekClose(); return true; }

  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    // 只在**可见行**上走：折叠起来的文件整段跳过（原先是 items 上取模，
    // 有了目录/文件树之后那样会走到看不见的条目上去）
    const rows = PEEK.rows;
    if (!rows.length) return true;
    const d = ev.key === "ArrowDown" ? 1 : -1;
    const at = rows.indexOf(PEEK.idx);
    const pos = (at < 0)
      ? (d > 0 ? 0 : rows.length - 1)
      : (at + d + rows.length) % rows.length;
    PEEK.idx = rows[pos];
    peekPaint();
    return true;
  }

  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    peekAccept(PEEK.idx);
    return true;
  }
  return false;
}

// ---- 主入口 ----
async function gotoJump(it, res) {
  setEdStatus("");
  // 带上 col / 词长：跳过去要选中那个符号，而不是整行
  await openInEditor(it.path, baseName(it.path), it.line, it.col, (it.word || "").length);
  // 放在 await 之后：openInEditor 打开新文件时会自己写一次状态（截断警告 / 空），
  // 先写我们的会被它盖掉
  setEdStatus("→ " + relPath(it.path, (res && res.scope) || S.workspace) + ":" + it.line);
}

/** 结果 → 浮层 / 状态栏。`suffix` 是浮层标题后面那句进度说明。
 *
 *  **一律走浮层**，命中几条都一样 —— 定义和引用的行为完全一致：先摆出来，
 *  Enter / 点一下才跳。
 *
 *  以前这里有三个短路，全删了：
 *   · `items.length === 1` → 直接跳。跳过去浮层就关了，后面 2、3 档补出来的
 *     结果**没地方摆**，于是"小工程看见一条就跳走、跳完才知道还有第二处定义"；
 *   · 定义只有一条且就在当前行 → 只写一句状态栏。Ctrl+左键在定义行上点，
 *     弹出的是"定义就在这一行"而不是引用列表，看着像没反应；
 *   · 副作用同上：单条定义不探后续档位。
 *  现在的观感和 VS Code 的 peek 一致：一条也是一行列表。
 */
function gotoShow(res, kind, suffix) {
  const items = (kind === "definitions" ? res.definitions : res.references) || [];
  if (!items.length) {
    setEdStatus(kind === "definitions"
      ? ("没找到 " + res.word + " 的定义")
      : (res.word + " 没有被引用"));
    return;
  }
  setEdStatus("");
  peekOpen(items, res, kind, res.word, suffix);
}

/**
 * `kind`：
 *   · `"definitions"` —— F12
 *   · `"references"`  —— Shift+F12
 *   · `"auto"`        —— Ctrl+左键：**方向交给后端按「光标这一侧是定义还是
 *     引用」定**。在引用处点 → 找定义；在定义处点 → 查引用（原版 ctrl-go-to 的
 *     `declPositionReturns`，默认 `references`）。
 *
 * 以前这里写死 `"definitions"`，于是「在定义行上按住 Ctrl 点」什么都不发生
 * （后端返回的定义就是当前这一行，`gotoShow` 只写一句"定义就在这一行"）。
 * 用户要的就是原版那个双向行为。
 *
 * 注意 `e` / `at` 现在**只**用于发请求（`ask`），不再传给 `gotoShow` —— 它
 * 一律打开 peek 列表，不需要知道光标在哪。
 */
async function gotoAtCursor(kind) {
  kind = (kind === "references" || kind === "auto") ? kind : "definitions";
  const isAuto = kind === "auto";

  const e = ed();
  if (!e || e.preview) return;
  // 设备上的文件（adb://）只在内存里，磁盘上没有这个路径，搜不了
  if (e.remote) { setEdStatus("设备文件不支持跳转"); return; }
  if (gotoBusy) return;                 // 连按 F12 别叠请求

  const at = cursorAt();
  gotoBusy = true;
  peekClose();
  setEdStatus(isAuto ? "查找跳转目标…"
    : (kind === "definitions" ? "查找定义…" : "查找引用…"));

  // auto 不传 mode：后端 `at_cursor` 在 mode 缺省时按 `is_decl_at` 自己判方向。
  // `ref_stage` 是**老参数**：后端认 `level`（字段名 `def_level`）优先，只在
  // 没带档位时才看它（`near` = 1 档、`full` = 3 档）。
  // `level` 是**分层档位**（1/2/3），定义侧和引用侧共用：1 = 文件所在目录的
  // 父目录（贴着声明的兄弟目录），2 = 工作区根下第二层（`<根>/a/b`），
  // 3 = 工作区根。每档只有一个目录、且前两档**不兜底**，所以每档都能秒回，
  // 见 `defDeepen` / `refDeepen`。
  //
  // `first` = 这是这一次点击的**第一次请求**（方向未定）：档位交给后端按最终
  // 方向自己挑最浅那一档（引用 → near，定义 → 1 档）。不能在前端提前发
  // `def_level=1`：auto 点在定义行上时后端判成"查引用"，带着 def_level 会让它
  // 一路短路成空，白等一趟往返（"点了十几秒没反应"就是这么来的）。
  const ask = (stage, level, first) => {
    const body = isAuto
      ? { path: e.path, line: at.line, col: at.col, ref_stage: stage }
      : { path: e.path, line: at.line, col: at.col, mode: kind, ref_stage: stage };
    if (level) body.def_level = level;
    if (first) body.first = 1;
    return api("/api/plugin/code_search/at_cursor", body);
  };
  /** 统一校验返回；不合格就写好状态栏并返回 null。 */
  const pick = (r) => {
    const res = (r && r.result) || {};
    if (!res.ok) { setEdStatus(res.error || "跳转失败", true); return null; }
    if (!res.word) { setEdStatus("光标处没有符号"); return null; }
    if (!res.mode) { setEdStatus((res.word || "") + " 是关键字，不跳转"); return null; }
    return res;
  };

  /** 定义侧的分层渐进：第 1 档先摆出来，**有结果就停**，2、3 档交给 ⟳。
   *
   *  为什么这么干：后端 `def_level` 一档只搜**那一个目录**（1 = 文件目录的
   *  父目录、2 = 根下第二层、3 = 工作区根），且前两档**不兜底**（不跑猜根、
   *  SDK、全树）。第 1 档一两百毫秒就回来，绝大多数符号在这一档就够。
   *
   *  以前是「第 1 档有结果也继续自动跑 2、3 档」。第 3 档等价于扫全树
   *  （`gp` 冷页缓存实测 50 秒），期间列表被 `peekPaint()` 原地重建两次，
   *  用户滚到的位置和选中项跟着跳 —— 主观就是「跳转卡、列表一直在变」。
   *  现在只有第 1 档**空**时才自动往下探（否则什么都不出现，用户干等）；
   *  有结果就给 ⟳ 让用户自己决定要不要扩到全仓。
   *
   *  以前单条命中直接跳（"既然只有一条，再等后面几档没意义"）。现在不这么干：
   *  跳走之后浮层会关，用户看不到"其实别处还有一处定义"。单条也摆出来。
   */
  const defDeepen = async (res1, dir) => {
    const acc = [];
    const seen = new Set();
    const merge = (arr) => {
      for (const it of (arr || [])) {
        const k = it.path + ":" + it.line;
        if (seen.has(k)) continue;
        seen.add(k);
        acc.push(it);
      }
    };
    let shown = false;
    const items1 = res1.definitions || [];
    merge(items1);

    if (items1.length) {
      // 有结果就摆出来并停在这儿。进度后缀不再承诺「正在探第 2 层」——
      // 那会让用户以为马上还有更多，实际要自己点 ⟳。
      gotoShow(res1, dir,
        "　（第 1 层 " + items1.length + " 处，⟳ 可搜更大范围）");
      // 列表已经画出来了，后面换内容走「原地改 PEEK.items」，不再走 gotoShow
      shown = true;
      armRetry();
      return;
    }
    setEdStatus("第 1 层没有 " + res1.word + "，正在探第 2 层…");

    // 三层：1 已在手里，顺着 2 → 3 往下探。2 = 工作区根下第二层（覆盖面比 1 大
    // 一个量级），3 = 工作区根（等价于扫全树）。第 3 档回来就不再往下 —— 后端
    // 在那一档自己会跑猜根 / SDK 兜底。
    const DEEP = [2, 3];
    for (const lv of DEEP) {
      const r = pick(await ask("", lv));
      if (!r) return;
      merge(r.definitions);
      const last = lv === DEEP[DEEP.length - 1];
      if (!acc.length) {
        setEdStatus(last ? ("没找到 " + r.word + " 的定义")
          : ("第 " + lv + " 层也没有 " + r.word + "，继续往下找…"));
        continue;
      }
      const tail = last ? "　（三层都搜过了）"
        : "　（已到第 " + lv + " 层，正在探第 " + (lv + 1) + " 层…）";
      if (shown && PEEK.open && PEEK.kind === "definitions") {
        // 原地换列表：保住用户已经滚到的位置和选中项
        PEEK.items = acc;
        PEEK.idx = Math.min(PEEK.idx, acc.length - 1);
        PEEK.suffix = tail;
        peekPaint();
        peekScroll();
      } else {
        // 第 1 档没命中、或用户中途按 Esc 关了浮层 → 在这儿重新摆一次。
        // gotoShow 现在一律打开浮层，不会再「直接跳走」，所以后面不需要再判
        // PEEK.open 来决定是否继续探下一档。
        gotoShow({ ...r, definitions: acc }, dir, tail);
        shown = true;
      }
    }
    if (PEEK.open && PEEK.kind === "definitions") {
      PEEK.suffix = "　（三层都搜过了）";
      peekPaint();
    }
  };

  /** 引用侧的分层渐进：和 `defDeepen` 同一套三层，只是结果字段换成 references。
   *
   *  为什么改：以前引用只有「本模块 → 全仓」两档 —— 第 1 档没命中就直接跳全树
   *  grep（AOSP 量级 13~21 秒），中间那层（工作区根下第二段）压根没人问。
   *  在 BootReceiver.java 上看到的「本模块没有引用，正在搜全仓…」再等十几秒，
   *  就是这么来的。现在 1/2/3 三档都是单目录，几百毫秒一档。
   */
  const refDeepen = async (res1, dir) => {
    const acc = [];
    const seen = new Set();
    const merge = (arr) => {
      for (const it of (arr || [])) {
        const k = it.path + ":" + it.line;
        if (seen.has(k)) continue;
        seen.add(k);
        acc.push(it);
      }
    };
    const items1 = res1.references || [];
    merge(items1);
    // 以前这里记 `jumpedOne`：第 1 档只有 1 条时 gotoShow 会直接跳走，于是跳过
    // 第 3 档的全树 grep。现在单条也摆列表；而第 1 档**有结果就停**，全树那次
    // 只有用户点 ⟳ 才跑 —— 不再有「点一下引用先傻等 50 秒」这回事。
    let shown = false;

    if (items1.length) {
      gotoShow(res1, dir,
        "　（第 1 层 " + items1.length + " 处，⟳ 可搜更大范围）");
      shown = true;
      armRetry();
      return;
    }
    setEdStatus("第 1 层没有 " + res1.word + " 的引用，正在探第 2 层…");

    const DEEP = [2, 3];
    for (const lv of DEEP) {
      const last = lv === DEEP[DEEP.length - 1];
      // 第 3 档带 `ref_stage="full"`：万一碰上还没有档位逻辑的老后端，
      // 这一路会退化成"扫全仓"，至少不会什么都没搜
      const r = pick(await ask(last ? "full" : "", lv));
      if (!r) return;
      // 方向万一被后端改成定义，别拿空的引用列表覆盖已经摆出来的结果
      if (r.mode && r.mode !== "references") return;
      merge(r.references);
      if (!acc.length) {
        setEdStatus(last ? ("没找到 " + r.word + " 的引用")
          : ("第 " + lv + " 层也没有 " + r.word + " 的引用，继续往下找…"));
        continue;
      }
      const tail = last ? "　（三层都搜过了）"
        : ("　（已到第 " + lv + " 层，正在探第 " + (lv + 1) + " 层…）");
      if (shown && PEEK.open && PEEK.kind === "references") {
        // 原地换列表：保住用户已经滚到的位置和选中项
        PEEK.items = acc;
        PEEK.idx = Math.min(PEEK.idx, Math.max(0, acc.length - 1));
        PEEK.suffix = tail;
        PEEK.retry = retryFull;     // 原位替换不走 peekOpen，⟳ 得自己留着
        peekPaint();
        peekScroll();
      } else {
        gotoShow({ ...r, references: acc }, dir, tail);
        shown = true;
        armRetry();
      }
    }
    if (PEEK.open && PEEK.kind === "references") {
      PEEK.suffix = "　（三层都搜过了）";
      peekPaint();
    }
  };

  /** 浮层标题栏的 ⟳：重跑一次 full，结果**原地**替换这条列表。
   *
   *  为什么需要手动入口：full 在这个函数里只自动跑一次。查引用时
   *  「本模块先摆出来、全仓慢慢补」这个渐进是对的，但补完就再也没有第二次 ——
   *  用户刚 svn update 过、或者刚才误按 Esc 关掉又重开，就只能重新按一遍
   *  Shift+F12，而这一来滚动位置和选中项全丢了。
   *
   *  gotoBusy 自己管：这属于**另一次**点击，不跟外层请求共用生命周期；
   *  但共用同一把锁是故意的 —— 重查期间再按 F12 没道理叠两个请求。
   */
  const retryFull = async () => {
    if (gotoBusy || PEEK.retryBusy) return;
    gotoBusy = true;
    PEEK.retryBusy = true;
    PEEK.suffix = "　（正在重查全仓…）";
    peekPaint();
    let kept = false;
    try {
      const r2 = pick(await ask("full"));
      if (!r2) return;
      // 方向变了就别拿空的引用列表去覆盖已经摆出来的那条
      if (r2.mode && r2.mode !== "references") {
        setEdStatus("这个位置现在查到的是定义，不覆盖引用列表", true);
        return;
      }
      const full2 = r2.references || [];
      PEEK.items = full2;
      PEEK.idx = Math.min(PEEK.idx, Math.max(0, full2.length - 1));
      PEEK.suffix = "";
      kept = true;
    } catch (err) {
      PEEK.suffix = "　（重查失败：" + ((err && err.message) || err) + "）";
    } finally {
      if (!kept && PEEK.suffix === "　（正在重查全仓…）") PEEK.suffix = "";
      PEEK.retryBusy = false;
      gotoBusy = false;
      if (PEEK.open) { peekPaint(); peekScroll(); }
    }
  };

  /** 引用列表摆出来之后登记 ⟳。定义那条路一次查完，没有第二阶段，不登记。 */
  const armRetry = () => {
    if (!PEEK.open || PEEK.kind !== "references") return;
    PEEK.retry = retryFull;
    peekPaint();
  };

  try {
    // 第一次请求只要**最浅那一档**：定义/引用都是第 1 档（贴文件的那个父目录），
    // 一两百毫秒就回来。auto（Ctrl+左键）方向还没定 → 不给档位，让后端按判出来
    // 的方向自己挑。
    const res = pick(await ask(kind === "references" ? "near" : "",
      kind === "definitions" ? 1 : 0, isAuto));
    if (!res) return;
    // 方向以**后端回报的 mode** 为准：auto 时它已经按「光标在定义上还是
    // 引用上」判过了，前端再自己猜一遍就是两套逻辑，迟早对不上
    const dir = (isAuto && res.mode)
      ? (res.mode === "references" ? "references" : "definitions")
      : kind;

    // 定义：分层渐进 —— 第 1 档先摆出来，2、3 档随后原地补
    if (dir !== "references") { await defDeepen(res, dir); return; }

    // 引用：和定义侧同一套三层渐进 —— 第 1 档先摆出来（秒回），2、3 档回来原地加长
    await refDeepen(res, dir);
  } catch (err) {
    setEdStatus("跳转失败：" + err.message, true);
  } finally {
    gotoBusy = false;
  }
}

// ---- 事件 ----
$("edArea").addEventListener("keydown", peekKey);

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Control" && !gotoMarkOn) {
    gotoMarkOn = true;
    gotoMarkRefresh();
  }
});

document.addEventListener("keyup", (ev) => {
  if (ev.key === "Control" && gotoMarkOn) {
    gotoMarkOn = false;
    gotoMarkHide();
  }
});

window.addEventListener("blur", () => {
  gotoMarkOn = false;
  gotoMarkHide();
});

$("edArea").addEventListener("click", () => {
  peekClose();
  gotoMarkRefresh();
});

// 点遮罩空白处关闭。用 mousedown 而不是 click：`preventDefault` 能把焦点留在
// textarea 上，否则焦点掉到 body，Esc / ↑↓ 就没人接了（peek 的键盘契约全靠
// edArea 的 keydown，见 `peekKey`）。
// `target === currentTarget` 才算点在遮罩本身 —— 点框内部的事件会冒泡上来。
const peekMaskEl = $("edPeekMask");
if (peekMaskEl) {
  peekMaskEl.addEventListener("mousedown", (ev) => {
    if (ev.target !== ev.currentTarget) return;
    ev.preventDefault();
    peekClose();
  });
}

// 鼠标在编辑区里移动：记坐标，一帧最多重算一次下划线。
// 这层不能省 —— selectionStart 只随点击 / 键盘变，鼠标滑过时它一动不动，
// 不记坐标的话下划线会一直停在上次点过的地方（正是用户报的"跟的是选中位置"）。
let gotoHoverRaf = 0;
$("edArea").addEventListener("mousemove", (ev) => {
  if (!gotoMarkOn) return;
  GOTO_HOVER.on = true;
  GOTO_HOVER.x = ev.clientX;
  GOTO_HOVER.y = ev.clientY;
  if (gotoHoverRaf) return;
  gotoHoverRaf = requestAnimationFrame(() => {
    gotoHoverRaf = 0;
    if (gotoMarkOn) gotoMarkRefresh();
  });
});

// 鼠标离开编辑区：交回光标（键盘移动光标时下划线还得有）
$("edArea").addEventListener("mouseleave", () => {
  GOTO_HOVER.on = false;
  if (gotoMarkOn) gotoMarkRefresh();
});

$("edArea").addEventListener("keyup", () => {
  if (gotoMarkOn) gotoMarkRefresh();
});

$("edArea").addEventListener("scroll", () => {
  if (gotoMarkOn) gotoMarkRefresh();
});

// 继续打字就说明不想看这个列表了
$("edArea").addEventListener("input", () => peekClose());

// ---- 编辑器右键菜单：转到定义 / 查找引用 ----
// 对位原版的 `editor/context` 两条（`package.json:116-128`）。
// 这两条是**纯白捡**的：按钮行为就是 F12 / Shift+F12，只是给不用键盘的场景
// 补一个入口。区别只在前置动作 —— 右键不移动光标，必须先把光标挪到点击处，
// 否则调 gotoAtCursor 会跳「上一次点过的位置」，看着像跳错了。
const GOTO_MENU_EXTS = [
  ".java", ".kt", ".kts", ".xml", ".gradle", ".aidl",
  ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx",
];

/** 这条菜单项在这个文件上有没有意义。 */
function gotoMenuOk(ctx) {
  if (!ctx || !ctx.path) return false;
  if (typeof ed !== "function") return false;
  const e = ed();
  if (!e || e.preview || e.remote) return false;   // 预览 / 设备文件（adb://）搜不了
  const p = String(ctx.path).toLowerCase();
  return GOTO_MENU_EXTS.some((x) => p.endsWith(x));
}

/** 把光标挪到右键点下那一点；挪不动（点在行尾空白 / 隐形尺子不在）返回 false。 */
function gotoMenuPutCursor(ctx) {
  const ta = ctx.textarea || $("edArea");
  if (!ta) return false;
  const off = offsetAtPoint(ctx.clientX, ctx.clientY);
  if (!(off >= 0)) return false;
  // 只设选区、**不 focus**：一 focus 视口就会滚回光标处，而菜单刚在点击位置弹出
  ta.selectionStart = off;
  ta.selectionEnd = off;
  return true;
}

async function gotoMenuRun(ctx, kind) {
  try {
    // 挪不动就按当前光标走（等价于在这个位置按了 F12），不当成失败
    gotoMenuPutCursor(ctx);
    await gotoAtCursor(kind);
  } catch (err) {
    setEdStatus("跳转失败：" + ((err && err.message) || err), true);
  }
}

menuRegister("editor", "code_search", {
  label: "转到定义", order: 10, when: gotoMenuOk,
  run: (ctx) => gotoMenuRun(ctx, "definitions"),
});
menuRegister("editor", "code_search", {
  label: "查找引用", order: 11, when: gotoMenuOk,
  run: (ctx) => gotoMenuRun(ctx, "references"),
});

// ---------------------------------------------------------------------------
// 打开工作区时预热「分级依据」
// ---------------------------------------------------------------------------
/** 让后端**后台**建好「该去哪些目录里找定义」。
 *
 *  为什么要预热：分级依据本来是**懒建**的（第一次按 F12 才起后台线程），而
 *  那一次请求拿不到表，只能退化成猜根再兜底扫全树 —— 于是**第一次点击永远
 *  是最慢的那一次**（大工程十几秒）。打开工作区时先建，第一次点击就走快路。
 *
 *  后端按「这棵树有没有 gradle」自己分流：Gradle 工程建依赖表 + 类索引，
 *  其它工程建目录分层表（只给视图看规模，查询侧改用祖先链）。远程工作区
 *  **什么都不用建** —— 它的分层是文件目录往上退 3/2/1 层，纯字符串算的。
 *  这个接口不阻塞，几毫秒就回。
 *
 *  同一个工作区 60 秒内只发一次：切标签、刷新页面都会走到这里，重复发等于
 *  让后端反复扫目录（AOSP 量级几百毫秒到两秒，白烧 IO 还和文件树抢盘）。
 */
const GOTO_WARM = { ws: "", at: 0 };

async function gotoWarm() {
  const ws = S.workspace || "";
  // 本地工作区要预热：后端得扫一遍目录树（判有没有 gradle / 建分层表），
  // 不预热的话第一次点击的依赖表是空的。
  // 远程工作区其实没事可做（分层是祖先链现算的），照样发一次 —— 后端直接
  // 回 ready，成本只有一次 HTTP，不值得在这加分支判断。
  if (!ws) return;
  const now = Date.now();
  if (GOTO_WARM.ws === ws && now - GOTO_WARM.at < 60000) return;
  GOTO_WARM.ws = ws;
  GOTO_WARM.at = now;
  try {
    await api("/api/plugin/code_search/warm", { scope: ws });
  } catch (e) { /* 预热失败不该影响跳转本身 */ }
}

WS_HANDLERS.goto_warm = gotoWarm;

// 首屏（登录 / 刷新）不走 onWorkspaceChanged，所以自己排一次；
// 800ms 是让文件树和首屏渲染先跑完，别在用户最需要响应的时候抢磁盘。
setTimeout(gotoWarm, 800);
