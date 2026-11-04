// ---------------------------------------------------------------------------
// 编辑器状态：打开过哪些文件、光标在第几行 —— 按工作区存进 .ai/save.json
// ---------------------------------------------------------------------------
// 以前切工作区只换会话和文件树，编辑器标签留在原地：切到 B 看到的还是 A 的
// 文件，再切回 A 又要一个个点开。这里把「打开了哪些文件 + 每个文件的光标/
// 滚动 + 哪个是活动标签」记进**该工作区的** `.ai/save.json`
// （和 showHidden、终端开关同一份），切过去或刷新页面时铺回来。
//
// 只存"位置"，**不存文件内容**：内容在磁盘上，存一份反而会出现
// "编辑器里的文本和磁盘不一致"这种更难解释的问题。未保存的改动在切走前
// 先落盘（见 prepareEditorSwitch）。
//
// 上面说的是**刷新页面**时的恢复（save.json，只记位置）。同一个页面生命周期里
// 反复切工作区走的是**内存缓存**（文件末尾的 ED_WS_CACHE）：每个工作区在内存里
// 存一份真标签对象（含文本），切回来同步挂上、一次盘都不读。两套东西的分工：
//   · ED_WS_CACHE —— 页面还活着，切走再切回 → 0 请求；
//   · .ai/save.json —— 页面刷新 / 重开 → 只记位置，按位置重新读盘。
const ED_STATE_DELAY = 600;     // 标签变化后多久落盘
const ED_STATE_MAX = 5;         // 只恢复**最近打开**的 5 个标签（见下面恢复循环）

// 内存缓存的容量上限（见文件末尾 ED_WS_CACHE 那一段的说明）
const ED_CACHE_MAX_FILES = 8;             // 每个工作区最多缓存几个标签
const ED_CACHE_MAX_BYTES = 1500 * 1024;   // 单个文件文本超过这个就不进缓存（1.5MB）

let edStateTimer = null;
let edStateMuted = false;       // 恢复过程中不要存"半恢复"的状态

/** 当前编辑器的状态快照。
 *
 *  `buf` 传了就按**那份标签数组**算（内存缓存用，见文件末尾）：没有 textarea
 *  可读，活动标签也按它自己的 `sel` / `scroll`，不碰界面。 */
function editorSnapshot(buf) {
  const src = buf || EDITORS;
  const files = [];
  for (let i = 0; i < src.length; i++) {
    const e = src[i];
    // 活动标签读 textarea 里的**实时**值，其余用内存里的（见 switchEditor）
    const live = !buf && (i === activeEd) && !e.preview;
    const text = live ? $("edArea").value : (e.text || "");
    const sel = live ? $("edArea").selectionStart : (e.sel || 0);
    const before = text.slice(0, Math.max(0, sel));
    files.push({
      path: e.path,
      line: before.split("\n").length,
      col: Math.max(0, sel - before.lastIndexOf("\n") - 1),
      scroll: live ? $("edArea").scrollTop : (e.scroll || 0),
      preview: !!e.preview,
    });
  }
  return { files, active: activeEd };
}

/** 标签变了之后防抖落盘（打开 / 关闭 / 切换标签时调用）。 */
function saveEditorStateSoon() {
  if (edStateMuted) return;
  if (edStateTimer) clearTimeout(edStateTimer);
  edStateTimer = setTimeout(() => {
    edStateTimer = null;
    saveEditorState();
  }, ED_STATE_DELAY);
}

/** 立即保存。切工作区、刷新页面前必须用这个 —— 防抖那个可能还没跑。
 *
 *  `ws` 显式指定存到哪个工作区：切工作区时它和 `/api/workspace` 并发跑，
 *  那时 `S.workspace` 还是旧值 —— 但下一次刷新、或者对方先回来，就没准了。
 *  写死目标最稳。 */
async function saveEditorState(ws) {
  if (edStateTimer) { clearTimeout(edStateTimer); edStateTimer = null; }
  const who = ws || S.workspace;
  if (edStateMuted || !who) return;
  try {
    await api("/api/state", { editors: editorSnapshot(), ws: who });
  } catch (e) {
    // 存不下只是下次恢复不到，不能挡住切工作区
  }
}

/**
 * 切工作区**之前**：脏文件先存盘，再把状态写进**旧**工作区的 save.json。
 *
 * 顺序不能反：`/api/state` 写的是"当前工作区"，等 `/api/workspace` 切完再存，
 * 状态就跑到新工作区去了。
 *
 * `ws` = 要写入的工作区。以前没这个参数、靠 `S.workspace` —— 于是它必须
 * 抢在 `/api/workspace` 之前 await 完，切一次工作区就得串行等一趟 HTTP +
 * 每个脏文件一次写盘。现在由调用方把旧工作区显式传进来，这一步就能和
 * `/api/workspace` **并发**跑（见 applyWorkspace）。
 */
async function prepareEditorSwitch(ws) {
  const who = ws || S.workspace;
  for (let i = 0; i < EDITORS.length; i++) {
    if (!EDITORS[i].dirty) continue;
    // 必须串行：doSave 存的是**当前活动那个**（读 #edArea 的实时文本），
    // 得先 switchEditor 再存。脏文件是少数（通常 0~1 个），串行代价可忽略。
    switchEditor(i);
    await doSave();
  }
  // 先压进内存缓存，再异步写 save.json：切走这一下要快，而缓存是同步的。
  // 顺序不能反 —— 脏文件刚落盘，此刻 buffer 里是最新内容。
  // 目标工作区用 edSlotsWs（这批标签自己的归属），不用 S.workspace：
  // 移除当前工作区那条路是先调这里、再请求，中间 S.workspace 已经变了。
  stashEditor((typeof edSlotsWs === "string" && edSlotsWs) ? edSlotsWs : who);
  await saveEditorState(who);
}

/** 把光标/滚动放回原处（openInEditor 走的是 jumpToLine，会选整行）。 */
function restoreCaret(f) {
  const e = ed();
  if (!e || !f) return;
  const ta = $("edArea");
  const rows = ta.value.split("\n");
  // 首屏分片还没等到全文（e.partial）、而目标行在首屏之外：现在摆会把光标夹到
  // 第 200 行，scrollTop 也会被浏览器按"只有 200 行"夹住 —— 位置直接错。
  // 记下来，等 edLoadFull 拿到全文后再补摆一次（见 80-editor.js 的 edLoadFull）。
  if (e.partial && (f.line || 1) > rows.length) {
    e._caretWant = f;
    return;
  }
  e._caretWant = null;
  const line = Math.max(1, Math.min(f.line || 1, rows.length));

  let pos = 0;
  for (let i = 0; i < line - 1; i++) pos += rows[i].length + 1;
  pos += Math.max(0, Math.min(f.col || 0, (rows[line - 1] || "").length));

  try { ta.setSelectionRange(pos, pos); } catch (err) { /* 忽略 */ }
  ta.scrollTop = Math.max(0, f.scroll || 0);
  $("edHl").scrollTop = ta.scrollTop;
  e.sel = pos;
  e.scroll = ta.scrollTop;
}

/**
 * 铺回某个工作区上次打开的编辑器。`snap` 是 `/api/state` 里的 `editors`。
 *
 * 先把当前标签整个收掉：不这么做的话，切到 B 时 A 的文件还挂着，
 * 文件名看着像"B 里也有这个文件"。
 */
async function restoreEditorState(snap) {
  const files = (snap && Array.isArray(snap.files)) ? snap.files : [];

  // 从这一刻起，EDITORS 里挂的就是**这个**工作区的标签了。
  // 不更新的话：切到 B 后什么都没点、又切到 C，prepareEditorSwitch 会拿
  // 上一个值把 B 的标签压进 A 的槽里（见 80-editor.js 的 edSlotsWs）。
  if (typeof S !== "undefined") edSlotsWs = S.workspace;

  // 内存里已经有**这个工作区**的标签：直接同步挂上，一次盘都不读。
  // 这是"反复切工作区不卡"的关键 —— save.json 只记位置，按它恢复就得对每个
  // 文件发一趟 /api/fs/read（远程工作区还是一趟 SSH）。
  const hit = mountEditorFromCache(S.workspace);
  if (hit) {
    // Logcat / 对比面板是全局的，上一个工作区开着的话要收掉，
    // 否则切过去编辑区显示的是别人的日志（标签栏已经换了）。
    // ED_DIFF 也要清：它存的是上一个工作区的两个路径。
    ED_SHOW_LOGCAT = false;
    ED_SHOW_DIFF = false;
    ED_DIFF = null;
    // 补全词表按工作区建，规则和下面读盘那条路一致
    compReset();
    compWarm();
    renderEdTabs();
    paintEditor();
    NAV.back.length = 0;
    NAV.fwd.length = 0;
    paintNav();
    // 内容来自缓存，磁盘上可能已经被改过（AI 直接写盘 / svn 更新）：
    // 后台对一遍 mtime，变了才重读 —— 不阻塞切工作区。
    if (typeof revalidateCachedEditors === "function") revalidateCachedEditors();
    return;
  }

  EDITORS.length = 0;
  activeEd = -1;
  // 对比面板里的路径属于上一个工作区，跟着一起收掉 —— 留着的话切过去
  // 还能点箭头去改 A 项目的文件。
  ED_DIFF = null;
  ED_SHOW_DIFF = false;
  // 补全的词表是**按工作区**建的：换工程要把旧池子倒掉再重新预热，
  // 否则新工程里敲字先命中的是上一个工程的词
  compReset();
  compWarm();
  renderEdTabs();
  paintEditor();

  if (!files.length) {
    NAV.back.length = 0;
    NAV.fwd.length = 0;
    paintNav();
    return;
  }

  edStateMuted = true;          // 恢复途中别存"只开了一半"的状态
  try {
    // **并发读、按快照顺序落位**（不是纯串行）。
    //
    // 以前是串行 await openInEditor：开过 5 个文件就是 5 次 /api/fs/read
    // 一趟接一趟，切一次工作区等于把最慢的那个乘 5。现在并发发出去、标签按
    // 快照顺序 push —— 顺序仍然可控，总耗时只取最慢的一次。
    //
    // 只取**末尾**的 n 条：files 是按打开顺序 push 的（见 editorSnapshot），
    // 末尾才是最近打开的。以前从 0 开始取，恢复出来的是"最早开的 5 个"，
    // 跟用户预期正好相反。
    const n = Math.min(files.length, ED_STATE_MAX);
    const wantFiles = [];
    for (let i = files.length - n; i < files.length; i++) {
      const f = files[i];
      if (f && f.path) wantFiles.push(f);
    }
    // 并发读盘，但标签顺序仍按快照：openInEditor 是"谁先读完谁先 push"，
    // 所以要回来之后再排一次序（此时 EDITORS 里只有这次恢复的标签，重排安全）。
    const opened = await Promise.all(wantFiles.map(
      (f) => openInEditor(f.path, baseName(f.path), f.line || 1).catch(() => false)));
    void opened;
    const rank = new Map();
    wantFiles.forEach((f, idx) => { if (!rank.has(f.path)) rank.set(f.path, idx); });
    const sorted = EDITORS.slice().sort(
      (a, b) => (rank.has(a.path) ? rank.get(a.path) : 1e9)
        - (rank.has(b.path) ? rank.get(b.path) : 1e9));
    EDITORS.length = 0;
    for (const e of sorted) EDITORS.push(e);
    // 光标只对**最后会成为活动标签**的那个摆：别的标签此刻并没有显示在
    // #edArea 里，摆也没用，还会跟末尾那次 switchEditor 打架。
    // 所以这里不摆 —— 统一留到函数末尾（原逻辑就在那儿）。
  } finally {
    edStateMuted = false;
  }

  if (!EDITORS.length) {
    NAV.back.length = 0;
    NAV.fwd.length = 0;
    paintNav();
    return;
  }

  // 活动标签按 **path** 找：中间有文件打不开时，下标会错位
  const want = files[(snap.active >= 0 && snap.active < files.length) ? snap.active : 0];
  let idx = EDITORS.length - 1;
  if (want && want.path) {
    const hit = EDITORS.findIndex((e) => e.path === want.path);
    if (hit >= 0) idx = hit;
  }
  switchEditor(idx);

  // 光标信息要取**实际恢复出来的那个标签**的记录，不能用 `want`：
  // 它可能压根没被恢复（不在最近 n 条里 / 文件已不存在），那样就会拿一个
  // 没打开的文件的行号去摆当前编辑器的光标，位置直接跳错。
  // 同一个 path 在快照里可能出现两次（preview 与编辑各一条），
  // 倒序查是为了命中最近那次打开。
  const curEd = EDITORS[idx];
  let meta = null;
  if (curEd && curEd.path) {
    for (let i = files.length - 1; i >= 0; i--) {
      if (files[i] && files[i].path === curEd.path) { meta = files[i]; break; }
    }
  }
  if (meta && !isPreviewOnly(curEd.path)) restoreCaret(meta);

  // 恢复过程本身会往跳转历史里塞一堆"打开文件"，清掉 ——
  // 而且切工作区之后，上一个工程的历史本来也不该留着
  NAV.back.length = 0;
  NAV.fwd.length = 0;
  paintNav();

  // 失效条目是靠 openInEditor 的 stat 校验剔掉的（它发现文件不存在会
  // 「toast + return false」，压根没建标签，EDITORS 里就没有它）；
  // 这里回写一次快照，让那个死链从 .ai/save.json 里消失，
  // 免得每次切换工作区都为一个不存在的文件弹一次 toast。
  saveEditorState();
  // 这次是"真的读了盘"，顺手把结果压进内存缓存：下次切回来就不用再读了
  stashEditor(S.workspace);
}

// ---------------------------------------------------------------------------
// 编辑器内存缓存：每个工作区一份标签对象（按工作区分槽，切回来 0 请求）
// ---------------------------------------------------------------------------
// 和文件树那套 TREE_DOM_CACHE 完全对称：
//   · 切走（prepareEditorSwitch）→ 把当前 EDITORS 存进 `ws` 自己的槽；
//   · 切回（restoreEditorState）→ 命中就同步 push 回 EDITORS，一次盘都不读；
//   · 没命中 → 走原来那条读盘恢复，恢复完顺手压一份。
//
// 为什么可以缓存内容（而 save.json 不能）：这份内存只在**同一个页面**里有效，
// 一刷新就没了，不存在"存档里是旧内容、下次打开还以为是最新"的问题。切走时
// 脏文件已经落盘（prepareEditorSwitch），所以槽里放的是干净版本；磁盘后来被
// 改了（AI 写盘 / svn 更新）由 revalidateCachedEditors 对 mtime 发现。
const ED_WS_CACHE = new Map();   // ws -> { eds, active, at }

/** 这个标签能不能整份进缓存（内容是完整文本、且不是设备/预览那种特殊标签）。 */
function edCacheable(e) {
  if (!e || !e.path) return false;
  if (e.preview) return false;                 // 媒体 / md 预览：内容在别处，缓存没意义
  if (e.remote) return false;                  // 设备文件：文本是虚拟 FS 出来的，重开很便宜
  if (e.partial) return false;                 // 首屏分片：手上只有前 N 行，不是全文
  if (typeof isPreviewOnly === "function" && isPreviewOnly(e.path)) return false;
  return typeof e.text === "string";
}

/** 槽里的标签能不能原样挂回界面。 */
function edMountable(e) {
  if (!edCacheable(e)) return false;
  if (String(e.path).startsWith("ssh://")) return false;   // 远程：对账要一趟 SSH，不值
  if (typeof isPreviewOnly === "function" && isPreviewOnly(e.path)) return false;
  return true;
}

/** 浅拷贝一个标签：EDITORS 里放的是槽自己的对象，切走再存会互相污染。 */
function cloneEdEntry(e) {
  const o = {};
  for (const k in e) {
    if (!Object.prototype.hasOwnProperty.call(e, k)) continue;
    if (k === "_caretWant") continue;
    o[k] = e[k];
  }
  // **染色结果跟着内容一起常驻**：tokens 是纯数据（{start,end,type}），和 e.text
  // 一一对应，只要 hlText 还是这份文本就能直接复用。以前这里无条件清空，
  // 表现为「切过去标签里是纯文本，要等好几秒才变颜色」—— 文件内容本来就在内存，
  // 被迫重新 POST 一遍全文再算一遍的只是颜色。
  // 严格按 hlText === text 判：不等就是「手上这份文本还没染过」，颜色照旧重算。
  if (o.hlText !== o.text) {
    o.tokens = null;
    o.hlText = "";
  }
  o.dirty = false;        // 切走前一定落过盘，见 prepareEditorSwitch
  o.partial = false;      // 能进缓存的一定是全文
  return o;
}

/**
 * 把当前编辑器压进 `ws` 的槽。
 *
 * 只留**末尾** ED_CACHE_MAX_FILES 个：和 save.json 取"最近打开的 n 个"同理，
 * 标签顺序就是打开顺序。单文件文本超过 ED_CACHE_MAX_BYTES 的整个跳过（缓存
 * 它换不来什么，反而把内存撑大）。
 */
function stashEditor(ws) {
  const who = ws || S.workspace;
  if (!who) return;
  const cur = ed();
  // 只有"文本域里真的是这个文件"时才回读 —— 预览（preview）和 Logcat / 对比
  // 面板占着编辑区时，`#edArea` 里是上一个文件的残留文本，回读会把别的文件
  // 的内容写进这个标签。
  if (cur && !cur.preview && !ED_SHOW_LOGCAT && !ED_SHOW_DIFF) {
    try {
      cur.text = $("edArea").value;
      cur.sel = $("edArea").selectionStart;
      cur.scroll = $("edArea").scrollTop;
    } catch (e) { /* 界面还没建好：用内存里的旧值 */ }
  }
  const keep = [];
  for (const e of EDITORS) {
    if (!edCacheable(e)) continue;
    if ((e.text || "").length > ED_CACHE_MAX_BYTES) continue;
    keep.push(cloneEdEntry(e));
  }
  if (!keep.length) { ED_WS_CACHE.delete(who); return; }
  const tail = keep.slice(Math.max(0, keep.length - ED_CACHE_MAX_FILES));
  // 活动标签记的是路径：过滤之后下标会错位
  const act = cur ? cur.path : "";
  let idx = -1;
  for (let i = 0; i < tail.length; i++) if (tail[i].path === act) { idx = i; break; }
  ED_WS_CACHE.set(who, { eds: tail, active: idx, at: Date.now() });
}

/**
 * 从 `ws` 的槽同步挂回 EDITORS。命中返回 true（一次盘都没读）。
 *
 * **只做同步的事**：此刻还在 applyWorkspace 里等 /api/workspace + /api/state，
 * 任何 await 都会让"点了切工作区"再卡一段。
 */
function mountEditorFromCache(ws) {
  const who = ws || S.workspace;
  const slot = who ? ED_WS_CACHE.get(who) : null;
  if (!slot || !slot.eds || !slot.eds.length) return false;
  EDITORS.length = 0;
  for (const e of slot.eds) {
    if (!edMountable(e)) continue;
    EDITORS.push(cloneEdEntry(e));
  }
  if (!EDITORS.length) { ED_WS_CACHE.delete(who); return false; }
  // 活动标签按 path 找（中间有标签被剔除时下标会错位）
  const wantPath = (slot.eds[slot.active] || {}).path || "";
  let idx = 0;
  for (let i = 0; i < EDITORS.length; i++) if (EDITORS[i].path === wantPath) { idx = i; break; }
  activeEd = idx;
  const e = EDITORS[idx];
  if (e && e.sel != null) e._caretWant = null;
  return true;
}

/**
 * 挂上来的是缓存：磁盘上可能已经变了。逐标签比一次 mtime，变了才重读。
 *
 * 判据照抄 editorWatchTick（那套已经处理过"远程不做 / 预览不做 / 分片不做"）。
 * 这里**不弹提示**：用户只是切了个工作区，没人改东西就不该看见任何字。
 */
let _edRevalidateBusy = false;
async function revalidateCachedEditors() {
  if (_edRevalidateBusy) return;
  _edRevalidateBusy = true;
  const who = S.workspace;
  try {
    for (const e of EDITORS.slice()) {
      // 期间又切走了 / 标签被关了：这一轮作废
      if (S.workspace !== who) return;
      if (EDITORS.indexOf(e) < 0) continue;
      if (!e.mtime) continue;
      let st = null;
      try { st = await api("/api/fs/stat", { path: e.path }); } catch (_) { continue; }
      if (!st || st.remote || st.exists === false) continue;
      if (String(st.mtime || "") === String(e.mtime || "")) continue;
      const i = EDITORS.indexOf(e);
      if (!(await refreshEditorFromDisk(e))) continue;
      if (EDITORS.indexOf(e) < 0) continue;
      if (activeEd === i) paintEditor();
    }
  } catch (_) {
    /* 对账失败只是显示旧内容，不能把界面搞崩 */
  } finally {
    _edRevalidateBusy = false;
  }
}

/**
 * 丢掉某个工作区的编辑器槽位（移除工作区时调）。
 *
 * 缓存只留**用户真正打开过**的工作区，不做后台预热：读整份文件文本代价高，
 * 空闲时替用户把别的工区读一遍只会白占磁盘 / SSH（远程还是一趟连接），
 * 而真切过去时那份内容也可能已经过期、照样要重读。所以槽位只在两处消失：
 * 工作区被从标签栏移除（这里）、以及页面刷新（内存整个没了）。
 */
function dropEditorSlot(ws) {
  if (!ws) return;
  ED_WS_CACHE.delete(ws);
}
