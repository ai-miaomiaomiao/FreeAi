// ---------------------------------------------------------------------------
// 视图：搜索（定义 / 引用）
// ---------------------------------------------------------------------------
// 这批搜索结果属于哪个工作区
let SEARCH_WS = "";

/**
 * 换了工作区就把结果清掉 —— 结果里那一列路径是**按工作区算的相对路径**
 * （`relPath(it.path, scope)`），换一个工作区再看着它就没意义了。
 *
 * 不清也不算错（点开还是能跳到绝对路径去），但列表会指着一堆 `../..` 让人困惑。
 * 这里不清就不动 —— 切走再切回来还是要留着上次的结果。
 */
function ensureSearchWorkspace() {
  if (!VIEW_INIT.search) return;
  if (SEARCH_WS === (S.workspace || "")) return;
  SEARCH_WS = S.workspace || "";
  // 范围是绝对路径：上一个工作区里的目录在新工作区里没有意义，留着会
  // 让人对着"范围：别的项目/xxx"发呆 —— 清回整个工作区。
  const ws = S.workspace || "";
  if (FIND.folder && ws && !FIND.folder.startsWith(ws)) FIND.folder = "";
  paintFindScope();
  const list = $("findResults");
  if (!list) return;
  list.innerHTML = "";
  list.appendChild(emptyBox("换了工作区 —— 上次的结果属于上一个工作区的\n重新搜一次"));
}

function initSearchView() {
  const run = () => doSearch();

  $("findGo").addEventListener("click", run);
  $("findInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); run(); }
  });
  // 切模式顺带重搜一次：模式改了结果就该跟着变，省得再点一下「查」。
  // 关键词为空时 doSearch 自己会退化成空态提示，不会真发请求。
  $("findMode").addEventListener("change", () => { paintFindOpts(); doSearch(); });
  // 三个开关：.* 正则 / cC 忽略大小写 / word 整词。
  // 点了就重搜一次（和 VS Code 的开关一样，不用再按「查」）。
  for (const id of ["findRe", "findCi", "findWord"]) {
    const b = $(id);
    if (!b) continue;
    b.addEventListener("click", () => {
      b.classList.toggle("on");
      doSearch();
    });
  }
  $("findInclude").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });
  // 排除框同样回车即搜（用户改完过滤条件就想看结果，别再点一次「查」）
  const excludeBox = $("findExclude");
  if (excludeBox) {
    excludeBox.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); doSearch(); }
    });
  }
  $("findScopeClear").addEventListener("click", () => {
    FIND.folder = "";
    paintFindScope();
    doSearch();
  });
  // 停止：告诉后端把这个 job 标记取消（扫描循环每个目录看一眼，毫秒级收手），
  // 然后等它把已有结果返回 —— 比 abort 掉连接好，前端还能看到"扫到哪儿了"。
  $("findStop").addEventListener("click", () => stopSearch());

  WS_HANDLERS.search = ensureSearchWorkspace;
  SEARCH_WS = S.workspace || "";
  paintFindOpts();
  paintFindScope();
}

/**
 * 选项行按模式显示：
 *   · `.*` / `cC` / `word` 三个开关常驻（文本搜索和符号搜索都认）
 *   · 包含 / 排除 —— 文本搜索和**文件夹模式**都认（VS Code 那两个框）
 * 符号的「定义/引用」模式不显示文件过滤，免得用户以为过滤生效了其实没生效。
 */
function paintFindOpts() {
  const mode = $("findMode").value;
  const ff = $("findFileOpts");
  if (ff) ff.classList.toggle("hidden", !(mode === "text" || mode === "folders"));
  const inp = $("findInput");
  if (inp) {
    inp.placeholder = mode === "text"
      ? "要搜索的文本，如 APK_INSTALL"
      : (mode === "folders" ? "符号名（结果按文件夹分组）" : "符号名，如 parse_status");
  }
}

/** 三个开关的当前状态。 */
function findFlags() {
  const on = (id) => {
    const b = $(id);
    return !!(b && b.classList.contains("on"));
  };
  return { regex: on("findRe"), ignoreCase: on("findCi"), wholeWord: on("findWord") };
}

/**
 * 当前搜索范围怎么显示。
 *
 * 只显示 basename 不够用：`vendor/autochips` 和 `vendor/mediatek` 在左栏都只剩
 * 一个 `vendor`，右键进来的是哪一层根本看不出来（用户视角："看不出在哪个
 * 文件夹搜索"）。所以优先显示**相对工作区的路径**（层级清楚、又不至于长到
 * 放不下），完整绝对路径放 title 里悬停看。
 */
function findScope() {
  const ws = S.workspace || "";
  const dir = FIND.folder || "";
  if (!dir) return { text: "整个工作区", full: ws };
  // 右键的正好是工作区根：按"整个工作区"说更准确（后端也等价）
  const norm = (s) => String(s || "").replace(/[\\/]+$/, "").toLowerCase();
  if (ws && norm(dir) === norm(ws)) return { text: "整个工作区", full: dir };
  const rel = relPath(dir, ws);
  return { text: (rel && rel !== ".") ? rel : (baseName(dir) || dir), full: dir };
}

/** 搜索范围提示：从文件树右键进来时是某个目录，否则是整个工作区。 */
function paintFindScope() {
  const lab = $("findInLabel");
  const clr = $("findScopeClear");
  if (!lab) return;
  const info = findScope();
  // 右键的正好是工作区根时 findScope 会把它归一成"整个工作区"，不算收窄
  const narrowed = !!FIND.folder && info.text !== "整个工作区";
  if (narrowed) {
    lab.textContent = "范围：" + info.text + "/";
    lab.title = info.full;          // 悬停看绝对路径
    lab.style.color = "var(--accent)";
    if (clr) clr.classList.remove("hidden");
  } else {
    lab.textContent = "范围内：整个工作区";
    lab.title = info.full || S.workspace || "";
    lab.style.color = "";
    if (clr) clr.classList.add("hidden");
  }
}

/** 当前生效的文件过滤（包含/排除），没填就是空串。 */
function findFilterText() {
  const inc = (($("findInclude") || {}).value || "").trim();
  const exc = (($("findExclude") || {}).value || "").trim();
  const parts = [];
  if (inc) parts.push("包含 " + inc);
  if (exc) parts.push("排除 " + exc);
  return parts.join("；");
}

/** 结果不完整时的警示行（超时 / 被上限截断都用得上）。 */
function findWarnLine(text) {
  const d = document.createElement("div");
  d.className = "vgroup wrap";      // wrap：说明文字换行显示，别被省略号吃掉
  d.style.color = "var(--danger)";
  d.textContent = "⚠ " + text;
  return d;
}

/**
 * 结果区顶部那行「在 xxx 下搜索」。
 *
 * 工具条上的范围标签在滚动/窗口窄的时候很容易被忽略，而搜索结果**必须**能
 * 一眼看出边界：同一批路径在"整个工作区"和"某个子目录"下含义完全不同。
 * 这行比工具条宽得多（还带过滤条件），所以用 .wrap 允许换行 —— 省略号会把
 * 「包含 device,frameworks；排除 out」整段切掉，等于白写。
 */
function findScopeHead(query) {
  const info = findScope();
  const narrowed = FIND.folder && info.text !== "整个工作区";
  const filt = findFilterText();
  // 只列和默认值不一样的开关，免得这行太长
  const f = findFlags();
  const bits = [];
  if (f.regex) bits.push("正则");
  if (!f.ignoreCase) bits.push("区分大小写");
  if (!f.wholeWord) bits.push("非整词");
  const flags = bits.join(" · ");
  const d = document.createElement("div");
  d.className = "vgroup wrap";
  d.textContent = (narrowed
    ? "在 " + info.text + "/ 下搜索「" + query + "」"
    : "在整个工作区搜索「" + query + "」")
    + (flags ? "　·　" + flags : "")
    + (filt ? "　·　" + filt : "");
  d.title = info.full || "";
  return d;
}

/**
 * 入口：跑一次搜索。
 *
 * 三个模式走两个后端接口 —— 符号（定义/引用/文件夹模式）走 `code_search/search`，
 * 文本走 `code_search/search_text`。文件夹模式不是另一种搜索，
 * 只是**结果的画法**不同（按目录分组折叠），所以后端仍然拿 definitions+references，
 * 前端按每条结果的 `folder` 字段分桶。
 */
// 搜索任务状态：seq 用来丢弃过期响应（连按回车 / 停止后旧请求才回来），
// job 是给后端的取消凭据（见插件 EXPOSE 的 cancel）。
let findSeq = 0;
let findJob = "";
let findRunning = false;

/** 「停止」按钮只在搜索过程中出现，「查」同时置灰避免并发点。 */
function paintFindBusy(on) {
  findRunning = !!on;
  const stop = $("findStop"), go = $("findGo");
  if (stop) stop.classList.toggle("hidden", !on);
  if (go) go.disabled = !!on;
}

/** 让后端停下正在跑的那个 job（不发 abort：让它把已有结果返回，前端还能看）。 */
function cancelJob(job) {
  if (!job) return;
  try {
    api("/api/plugin/code_search/cancel", { job }).catch(() => { /* 忽略 */ });
  } catch (e) { /* 忽略 */ }
}

function stopSearch() {
  if (!findRunning) return;
  const job = findJob;
  findJob = "";
  cancelJob(job);
  toast("正在停止…");
}

async function doSearch() {
  const symbol = $("findInput").value.trim();
  const list = $("findResults");
  const mode = $("findMode").value;

  list.innerHTML = "";
  // 空关键词的文案按当前模式给：默认已是文本搜索，还提示"输入符号名"会让人
  // 以为选错了模式（这块以前是写死符号语义的）。
  if (!symbol) {
    paintFindBusy(false);
    list.appendChild(emptyBox(mode === "text" ? "输入要搜索的文本后回车"
      : (mode === "folders" ? "输入符号名后回车（结果按文件夹分组）" : "输入符号名后回车")));
    return;
  }

  // 上一个还在跑：先让后端停掉再开新的，免得两个扫描抢 CPU
  if (findRunning) { cancelJob(findJob); findJob = ""; }

  const seq = ++findSeq;
  const job = "j" + seq + "-" + Date.now().toString(36);
  findJob = job;
  paintFindBusy(true);
  list.appendChild(emptyBox("搜索中…（大工程较慢，可点「停止」）"));

  const scopeArg = FIND.folder ? { folder: FIND.folder } : {};
  // 不设超时：搜索跑多久由用户点「停止」决定。以前前后端各掐一个表
  // （后端 45s / 前端 60s），两边不一致时还会出现"界面报超时、后端还在扫"，
  // 现在两边都去掉了，只剩 /cancel 这一条中断路径。
  const SLOW = { timeout: 0 };
  const flags = findFlags();

  try {
    if (mode === "text") {
      const r = await api("/api/plugin/code_search/search_text", Object.assign({
        query: symbol,
        job,
        regex: flags.regex,
        whole_word: flags.wholeWord,
        case_sensitive: !flags.ignoreCase,
        include: $("findInclude").value.trim(),
        // 排除：文件名通配符（*.c,*.h）。目录级的排除由后端默认黑名单兜着
        // （out / build / target …，见 code_search.SKIP_DIRS）。
        exclude: $("findExclude") ? $("findExclude").value.trim() : "",
        // 0 = 不限条数：后端扫到哪儿给到哪儿，不在中途自己截断。
        // 渲染上限由 paintText 控制（几万条 DOM 会把界面拖死）。
        max_results: 0,
      }, scopeArg), SLOW);
      if (seq !== findSeq) return;            // 已经开了新搜索，丢弃这次结果
      const res = r.result || {};
      if (res.ok === false) { list.innerHTML = ""; list.appendChild(emptyBox(res.error || "搜索失败")); return; }
      paintText(symbol, res);
      paintSshHint(res);
      return;
    }

    // 文件夹模式也带上文件过滤（VS Code 的「包含的文件 / 排除的文件」）；
    // 定义/引用模式这两个框是隐藏的，不带 —— 免得把上一次填的值偷偷应用上去
    const fileOpts = mode === "folders" ? {
      include: $("findInclude").value.trim(),
      exclude: $("findExclude").value.trim(),
    } : {};
    const r = await api("/api/plugin/code_search/search", Object.assign({
      symbol,
      job,
      mode: (mode === "folders" ? "both" : mode),
      // 三个开关对符号搜索同样生效：
      //   .* → 把输入当正则（此时不再展开 getter/setter 变体）
      //   cC → 忽略大小写（关掉 = 区分）
      //   word → 整词匹配（关掉 = 子串也算）
      regex: flags.regex,
      whole_word: flags.wholeWord,
      case_sensitive: !flags.ignoreCase,
      max_results: 0,          // 0 = 不限条数
    }, fileOpts, scopeArg), SLOW);
    if (seq !== findSeq) return;
    const res = r.result || {};
    if (res.ok === false) { list.innerHTML = ""; list.appendChild(emptyBox(res.error || "搜索失败")); return; }
    paintFind(symbol, res, mode === "folders");
    paintSshHint(res);
  } catch (e) {
    if (seq !== findSeq) return;
    // 前端等不及了（60s 超时 / 断网）也要让后端停手 —— 否则它会把整棵树扫完，
    // 白烧 CPU，而且这次结果没人要了。
    cancelJob(job);
    list.innerHTML = "";
    list.appendChild(emptyBox("搜索失败：" + e.message));
  } finally {
    if (seq === findSeq) { findJob = ""; paintFindBusy(false); }
  }
}

/**
 * 远程工作区（UNC 共享）的提示条。
 *
 * 搜索跑在 `\\192.168.x.x\share\...` 上时，每一次目录遍历都是一次 SMB 往返，
 * 几万个文件能把一次搜索拖到几分钟。配好 SSH 之后遍历留在服务器本机，只把
 * 命中行传回来 —— 结果一样，快一两个数量级。入口就摆在结果区顶上，
 * 不用去翻设置。
 */
function paintSshHint(res) {
  const ssh = res && res.ssh;
  if (!ssh) return;
  const list = $("findResults");
  if (!list) return;
  const bar = document.createElement("div");
  bar.className = "ssh-hint" + (ssh.configured ? " ok" : "");
  const txt = document.createElement("span");
  txt.className = "txt";
  txt.textContent = ssh.configured
    ? "远程工作区（" + ssh.host + "）：搜索在服务器上执行"
    : (ssh.hint || "这是远程共享，配置 SSH 后搜索会快很多");
  bar.appendChild(txt);
  if (!ssh.configured) {
    const b = document.createElement("button");
    b.className = "tbtn mini";
    b.textContent = ssh.paramiko === false ? "安装 paramiko" : "配置 SSH";
    b.addEventListener("click", () => {
      if (ssh.paramiko === false) {
        const d = document.createElement("div");
        d.style.cssText = "font-size:13px;line-height:1.9;white-space:pre-wrap";
        d.textContent = "SSH 搜索需要 Python 的 paramiko 库。\n\n"
          + "在应用根目录执行：\n\n    pip install paramiko\n\n"
          + "装完重启服务，再回到这里搜索。";
        openModal("需要 paramiko", d, null);
        return;
      }
      openSshDialog(ssh.suggest || {}, ssh);
    });
    bar.appendChild(b);
  }
  list.prepend(bar);
}

/** SSH 配置弹窗：主机 / 端口 / 用户名 / 密码（或私钥）/ 路径映射。 */
function openSshDialog(suggest, ssh) {
  const ws = FIND.folder || S.workspace || "";
  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:9px;font-size:13px";

  const tip = document.createElement("div");
  tip.style.cssText = "color:var(--muted);font-size:12px;line-height:1.7;white-space:pre-wrap";
  tip.textContent = "工作区在远程共享上（" + (suggest.host || ssh.host || "") + "）。\n"
    + "配好 SSH 后，目录遍历在服务器本机执行，只把命中行传回来。";
  body.appendChild(tip);

  const field = (label, key, value, type, placeholder) => {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:center;gap:8px";
    const sp = document.createElement("span");
    sp.textContent = label;
    sp.style.cssText = "flex:0 0 96px;color:var(--muted)";
    const inp = document.createElement("input");
    inp.dataset.key = key;
    inp.type = type || "text";
    inp.value = value == null ? "" : String(value);
    if (placeholder) inp.placeholder = placeholder;
    inp.style.cssText = "flex:1;min-width:0;background:var(--bg);"
      + "border:1px solid var(--border);border-radius:6px;padding:5px 8px;"
      + "color:var(--text);font-size:12px;outline:none";
    row.appendChild(sp);
    row.appendChild(inp);
    body.appendChild(row);
    return inp;
  };

  field("主机", "host", suggest.host || "", "text");
  field("端口", "port", suggest.port || 22, "number");
  field("用户名", "user", suggest.user || "", "text");
  field("密码", "password", "", "password", "留空则用私钥 / 免密");
  field("私钥路径", "key", suggest.key || "", "text", "可选，如 C:/Users/me/.ssh/id_rsa");
  field("本地根", "local_root", suggest.local_root || "", "text");
  field("远程根", "remote_root", suggest.remote_root || "", "text", "如 /home/dev");

  const status = document.createElement("div");
  status.style.cssText = "color:var(--muted);font-size:12px;min-height:18px";
  body.appendChild(status);

  const collect = () => {
    const out = { ws };
    for (const inp of body.querySelectorAll("input")) out[inp.dataset.key] = inp.value;
    out.port = Number(out.port) || 22;
    out.enabled = true;
    return out;
  };

  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;gap:8px;align-items:center;width:100%";

  const testBtn = document.createElement("button");
  testBtn.className = "tbtn";
  testBtn.textContent = "测试连接";
  testBtn.addEventListener("click", async () => {
    status.style.color = "var(--muted)";
    status.textContent = "连接中…";
    try {
      const r = await api("/api/ssh/test", collect(), { timeout: 20000 });
      const ok = !!(r && r.ok);
      status.textContent = ok
        ? "连接成功（" + (r.system || "?") + "，" + r.ms + " ms）"
        : ("失败：" + ((r && r.error) || "未知原因"));
      status.style.color = ok ? "var(--ok)" : "var(--danger)";
    } catch (e) {
      status.textContent = "失败：" + e.message;
      status.style.color = "var(--danger)";
    }
  });

  const saveBtn = document.createElement("button");
  saveBtn.className = "tbtn";
  saveBtn.textContent = "保存并重搜";
  saveBtn.addEventListener("click", async () => {
    status.style.color = "var(--muted)";
    status.textContent = "保存中…";
    try {
      const r = await api("/api/ssh", collect());
      if (r && r.error) throw new Error(r.error);
      closeModal();
      toast("SSH 配置已保存");
      doSearch();
    } catch (e) {
      status.textContent = "保存失败：" + e.message;
      status.style.color = "var(--danger)";
    }
  });

  const spacer = document.createElement("span");
  spacer.style.cssText = "flex:1";
  foot.appendChild(testBtn);
  foot.appendChild(spacer);
  foot.appendChild(saveBtn);

  openModal("SSH 搜索配置", body, foot);
}

/**
 * 树形缩进步长。
 *
 * 取 14 是跟左边文件树对齐（`40-tree.js:221` 的 `8 + depth*14`）：两个面板
 * 并排显示，步长不同的话同级目录永远错开一截。以前这里写的是 12。
 */
const FIND_INDENT = 14;

/** 折叠记忆：同一个搜索里点开的文件夹，重画时不该又折回去。 */
const FIND = { folder: "", open: new Set() };

/**
 * 一行「文件夹」，可折叠。
 *
 * `label` 是**这一层的目录名**（不是完整路径），`depth` 决定缩进 —— 结果区要
 * 长得和左边文件树一样：vendor → mediatek → proprietary → … 一层层点开，
 * 而不是把 `vendor/mediatek/proprietary/external/xxx` 整条路径塞一行再省略号。
 * 完整路径放 title 里悬停看。
 */
function findFolderRow(label, count, depth, onToggle, isOpen, fullPath) {
  const d = document.createElement("div");
  d.className = "fd";
  d.style.paddingLeft = (8 + (depth || 0) * FIND_INDENT) + "px";
  const caret = document.createElement("i");
  caret.textContent = isOpen ? "▾" : "▸";
  const name = document.createElement("b");
  name.textContent = "📁 " + label;
  name.title = fullPath || label;
  const cnt = document.createElement("span");
  cnt.className = "cnt";
  cnt.textContent = count;
  d.appendChild(caret); d.appendChild(name); d.appendChild(cnt);
  if (onToggle) d.addEventListener("click", onToggle);
  return d;
}

/**
 * 把「多级单链目录」压成一行（VSCode `explorer.compactFolders` 的行为）。
 *
 * 实际结果里大量出现 `aacplib → md`、`src → com → server → ui → aacp → config`
 * 这种链：每层只挂一个子目录、自己一个文件都没有。逐层画出来占掉大半屏，而
 * 左栏只有两百来像素，文件名被挤到看不见 —— 这些中间层一个字的信息都没多给。
 *
 * 判据两条同时满足才合并：
 *   ① **本层自己没有文件**（`hasOwn` 为假）；
 *   ② 只有一个子目录。
 * 一旦本层挂了文件就不能并下一层：那层的文件会挂到一个名字里没有它自己的
 * 目录下面，看起来像放错了位置。
 *
 * `hasOwn` 由调用方给：文本模式看 `files`，文件夹模式看 `defs/refs`。
 * 返回链末端节点 + 合并后的显示名 + 末端路径（折叠 key 和 title 都用它，
 * 否则同一棵树重画时 key 变化会把用户点开的折叠态弄丢）。
 */
function collapseChain(node, name, path, hasOwn) {
  let cur = node, label = name, p = path;
  while (!hasOwn(cur) && cur.kids.size === 1) {
    const only = [...cur.kids.values()][0];
    label = label ? label + "/" + only.name : only.name;
    p = only.path;
    cur = only;
  }
  return { node: cur, label: label, path: p };
}

/**
 * 本地折叠：切 class + 箭头 + 记收起态，**不发请求、不重画**。
 *
 * 以前折叠直接 `doSearch()` —— 文本模式下等于「点一下文件夹 = 整个仓库重扫
 * 一遍」：大工程要等几秒，`list.innerHTML = ""` 清空再重建，滚动位置也丢了。
 * 文件夹模式则是 `paintFind(...)` 把整棵树重画。折叠纯属显示层的事，
 * 结果就在手边，没有理由回去问后端。
 *
 * `FIND.open` 存的是**收起**的 key（沿用原语义）；返回 true = 这次是展开。
 */
function toggleFold(key, row, body) {
  if (!body) return false;
  const open = body.classList.contains("hidden");
  body.classList.toggle("hidden", !open);
  if (open) FIND.open.delete(key); else FIND.open.add(key);
  const caret = row && row.querySelector("i");
  if (caret) caret.textContent = open ? "▾" : "▸";
  return open;
}

/**
 * 一条命中行：被命中那段高亮出来（只对已有明文 text 的场景用）。
 *
 * `pad` 是左内边距（px），由调用方按层级给 —— CSS 里原来写死 `padding-left:
 * 26px`，于是不管属主文件在第几层，命中行都从同一个 x 起，看不出它属于谁。
 */
function findHitLine(text, col, len, lineNo, path, pad) {
  const d = document.createElement("div");
  d.className = "frl";
  if (pad) d.style.paddingLeft = pad + "px";
  const no = document.createElement("span");
  no.className = "no";
  no.textContent = lineNo;
  d.appendChild(no);

  const s = String(text || "");
  // col 是相对**原始行**的偏移，而 text 已经 strip 过左空白，
  // 直接用会错位。这里退化成"在裁剪后的文本里找它第一次出现的位置"，
  // 找不到就整行不高亮 —— 高亮错位比不高亮更让人困惑。
  let st = -1, en = -1;
  if (len > 0) {
    const probe = s.slice(Math.max(0, col), col + len);
    st = probe ? s.indexOf(probe) : -1;
    if (st >= 0) en = st + len;
  }
  if (st < 0) {
    d.appendChild(document.createTextNode(s));
  } else {
    d.appendChild(document.createTextNode(s.slice(0, st)));
    const mk = document.createElement("mark");
    mk.textContent = s.slice(st, en);
    d.appendChild(mk);
    d.appendChild(document.createTextNode(s.slice(en)));
  }
  d.title = path + ":" + lineNo;
  d.addEventListener("click", () => openInEditor(path, baseName(path), lineNo));
  return d;
}

/**
 * 结果"没搜完"的原因（超时 / 文件数到顶），完整搜索返回空串。
 *
 * 这条提示比什么都重要：AOSP 里 `device,packages,vendor` 有 26 万个源文件，
 * 所以搜索过程中要么等它扫完，要么用户自己点「停止」；停止后要给一句
 */
function incompleteReason(res) {
  if (res.cancelled) {
    return "已停止搜索 —— 下面是停止前扫到的部分";
  }
  if (res.timed_out || res.timedOut) {
    return "搜索太久被自动中止（结果不完整）：用「包含/排除」收窄范围，或指定更深的目录";
  }
  if (res.capped) {
    return "到达文件数上限，只扫了一部分文件：收窄范围后重搜";
  }
  return "";
}

/** 文本搜索结果：文件夹 → 文件 → 命中行，逐层折叠。 */
function paintText(query, res) {
  const list = $("findResults");
  list.innerHTML = "";
  // 先交代搜索边界，再看结果 —— 结果里的路径都是相对这个范围的
  list.appendChild(findScopeHead(query));
  // 结果不完整（超时 / 文件数到顶）必须说明：否则"没找到"会被当成"确实没有"
  const partial = incompleteReason(res);
  if (partial) list.appendChild(findWarnLine(partial));
  // 包含范围里的名字写错（framework vs frameworks）时后端会给一句提示
  if (res.note) list.appendChild(findWarnLine(res.note));
  const groups = res.groups || [];
  if (!groups.length) {
    list.appendChild(emptyBox("没找到「" + query + "」\n扫了 "
      + (res.scanned || res.files || 0) + " 个文件"
      + (partial ? "\n（只扫了一部分，别当成\"确实没有\"）" : "")));
    return;
  }

  const head = document.createElement("div");
  head.className = "vgroup";
  head.textContent = (res.truncated ? "结果太多，已截断到 " + res.hits + " 处（" : "")
    + res.files + " 个文件 / " + res.hits + " 处命中"
    + (res.truncated ? "）" : "");
  list.appendChild(head);

  // 条数已经不限制了（后端扫完为止），但 DOM 不能无限堆 —— 几万个文件行
  // 会把浏览器拖死。这里只限制**画出来多少行**，并明确告诉用户还有多少没显示。
  const RENDER_MAX = 2000;
  let rendered = 0;
  let cut = 0;

  // ---- 把「一条完整目录路径一行」改成按 `/` 拆开的**目录树** ----
  //
  // 以前每个 group 只画一行 `aacplib/src/com/server/ui/aacp/config`：左栏两百来
  // 像素，这一串必被省略号切成 `aacplib/src/com/hzbh...`，而且永远从 x=8 起 ——
  // 同级目录互不嵌套，文件行和命中行也不跟着缩进，看不出谁在谁下面。
  // 现在和「文件夹模式」（`paintFind`）共用同一套树形观感。
  // 后端 `groups[].folder` 本来就是完整相对路径（`__init__.py:2256`），够拆。
  const root = { name: "", path: "", kids: new Map(), hits: 0, files: [] };
  for (const g of groups) {
    const folder = g.folder && g.folder !== "(根目录)" ? g.folder : "";
    let node = root;
    if (folder) {
      let acc = "";
      for (const seg of folder.split("/").filter(Boolean)) {
        acc = acc ? acc + "/" + seg : seg;
        let kid = node.kids.get(seg);
        if (!kid) {
          kid = { name: seg, path: acc, kids: new Map(), hits: 0, files: [] };
          node.kids.set(seg, kid);
        }
        node = kid;
      }
    }
    node.hits += g.hits || 0;
    for (const f of (g.files || [])) node.files.push(f);
  }

  // 子树命中数：目录行右侧那个数字要算上所有后代，否则折起来就看不出里面有多少
  const subHits = (n) => n.hits
    + [...n.kids.values()].reduce((a, k) => a + subHits(k), 0);
  // 子树文件数：跳过的分支要计入 `cut`（以前 `continue` 只跳一个 group，漏算后代）
  const subFiles = (n) => n.files.length
    + [...n.kids.values()].reduce((a, k) => a + subFiles(k), 0);
  // 这个目录自己有没有文件 —— 合并单链目录的判据之一（见 `collapseChain`）
  const hasOwnFiles = (n) => n.files.length > 0;

  // 文件行：`名 :行` 一行，下面挂匹配行。缩进按层级给 —— 这正是「对齐」的全部来源。
  // `depth` 是所属目录的层级，文件比它再进一级（`+1`）。
  const renderFiles = (node, depth, host) => {
    const pad = 8 + (depth + 1) * FIND_INDENT;
    for (const f of node.files) {
      if (rendered >= RENDER_MAX) { cut += 1; continue; }
      rendered += 1;
      // 文件 key 直接用完整相对路径：它本来就在整个结果里唯一，
      // 不必再拼上层目录（目录 key 是 `"t:" + 目录路径`，也不会和它撞）
      const fkey = "t:" + f.path;
      const fOpen = !FIND.open.has(fkey);
      const fbody = document.createElement("div");
      fbody.className = fOpen ? "" : "hidden";

      const row = document.createElement("div");
      row.className = "fr";
      row.title = f.path;
      row.style.paddingLeft = pad + "px";
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = "📄 " + f.name;
      const lc = document.createElement("span");
      lc.className = "lc";
      lc.textContent = f.hits;
      row.appendChild(nm); row.appendChild(lc);
      // 命中行**首次展开时才建**：折叠/展开只切 class，DOM 只建一次。
      // 以前每次点都 doSearch() 整片重扫重画，这是「展开了却要等几秒」的主因。
      // 命中行比属主文件再进一级，一眼看出它是谁下面的。
      const hitPad = pad + FIND_INDENT;
      let filled = false;
      const fill = () => {
        if (filled) return;
        filled = true;
        // 一个文件命中太多就不全铺开：先给前 20 条，剩下的靠点文件名看全部
        const shown = f.matches || [];
        for (const m of shown.slice(0, 20)) {
          fbody.appendChild(findHitLine(m.text, m.col, m.len, m.line, f.path, hitPad));
        }
        if (shown.length > 20) {
          const more = document.createElement("div");
          more.className = "frl";
          more.style.paddingLeft = hitPad + "px";
          more.textContent = "…还有 " + (shown.length - 20) + " 处（点文件名可收起）";
          fbody.appendChild(more);
        }
      };
      if (fOpen) fill();
      row.addEventListener("click", () => {
        if (toggleFold(fkey, row, fbody)) fill();
      });
      host.appendChild(row);
      host.appendChild(fbody);
    }
  };

  // 画 node 这个目录**里面**的东西：先子目录（按名字排），再本目录下的文件。
  // 子目录行和它的 body 一起挂进父 body —— 父目录收起时整棵子树跟着隐藏，
  // 折叠仍然只是切 class，不重画、不重扫。
  // `depth` 是 node **自己**的层级：root 传 -1，顶层目录因此是 0。
  const renderInside = (node, depth, host) => {
    const kids = [...node.kids.values()]
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    for (const kid of kids) {
      // 到画图上限就整棵子树跳过，并把里面所有文件计入 cut
      // （以前 `continue` 只跳一个 group，后代的文件数就漏算了）
      if (rendered >= RENDER_MAX) { cut += subFiles(kid); continue; }
      // 单链目录压成一行（`aacplib/md`、`src/com/server/ui/aacp/config`）：
      // 中间层没有自己的文件，逐层画出来只是白占宽度。折叠 key 和 title 都
      // 用**链末端**路径，缩进按合并后那一层算 —— 压几层就少缩几级，
      // 否则深层文件会被推到右边界外面去。
      const cc = collapseChain(kid, kid.name, kid.path, hasOwnFiles);
      const fkey = "t:" + cc.path;
      const isOpen = !FIND.open.has(fkey);
      const grow = findFolderRow(cc.label, subHits(cc.node), depth + 1, null, isOpen, cc.path);
      const body = document.createElement("div");
      body.className = isOpen ? "" : "hidden";
      host.appendChild(grow);
      host.appendChild(body);
      grow.addEventListener("click", () => toggleFold(fkey, grow, body));
      renderInside(cc.node, depth + 1, body);
    }
    renderFiles(node, depth, host);
  };
  renderInside(root, -1, list);

  if (cut) {
    list.appendChild(findWarnLine(
      "结果太多：只显示了前 " + rendered + " 个文件，另有 " + cut
      + " 个没画出来。用「包含 / 排除」收窄，或在文件树上右键缩小范围。"));
  }
}

/** 候选文件数（后端 `scan` 回填的 `cands`）。后端用 Python 遍历那条路时给 null。 */
function findCands(res) {
  const n = res && res.cands;
  return (typeof n === "number" && n >= 0) ? n : null;
}

/**
 * 「有文件提到这个词，但一条符号都没命中，而且扫描没被打断」
 * —— 也就是全落在字符串 / 注释里（`_search.code_mask` 故意屏蔽的那类）。
 *
 * 这时候「没找到」是**正确**的：定义/引用模式只认真正的代码符号。
 * 但用户想看的往往正是那句字面量，所以要给他一个换模式的出口。
 */
function onlyInLiterals(res) {
  const n = findCands(res);
  return n !== null && n > 0 && !incompleteReason(res);
}

/**
 * 符号模式的空结果文案。
 *
 * 以前只会说「没找到 X，扫了 3 个文件」——那个 3 是**内容含该词的文件数**
 * （rg 粗筛的结果），不是全仓文件数，用户看到只会以为搜索只扫了 3 个文件就放弃。
 * 三种原因分开说：压根没这个词 / 全在字符串注释里 / 扫描被打断。
 */
function emptyFindText(symbol, res, partial) {
  const n = findCands(res);
  if (n === 0) {
    return "没找到「" + symbol + "」\n整个工作区没有一个文件出现这个名字\n"
      + "（拼错了？或者它来自没纳入工作区的 jar / aar / SDK）";
  }
  if (n !== null && n > 0 && !partial) {
    return "没找到「" + symbol + "」的符号定义或引用\n共 " + n
      + " 个文件提到这个词，但都只出现在字符串或注释里\n"
      + "（定义/引用模式只看真正的代码符号 —— 想找那些文字就换文本模式）";
  }
  return "没找到「" + symbol + "」\n扫了 " + (res.scanned || 0) + " 个文件"
    + (partial ? "\n（只扫了一部分，别当成\"确实没有\"）" : "");
}

/** 「改用文本模式重搜」：把模式下拉切到 text，再跑一次同一份输入。 */
function findToTextBtn() {
  const box = document.createElement("div");
  box.style.cssText = "display:flex;justify-content:center;padding:6px 0 2px";
  const b = document.createElement("button");
  b.className = "tbtn mini";
  b.textContent = "改用文本模式重搜";
  b.addEventListener("click", () => {
    const sel = $("findMode");
    if (!sel) return;
    sel.value = "text";
    paintFindOpts();          // 文本模式才显示「包含 / 排除」两个框
    doSearch();
  });
  box.appendChild(b);
  return box;
}

/** 符号搜索结果。`byFolder` = 文件夹模式（按目录分组折叠）。 */
function paintFind(symbol, res, byFolder) {
  const list = $("findResults");
  const defs = res.definitions || [];
  const refs = res.references || [];
  list.innerHTML = "";
  // 同 paintText：结果列表必须先说清"在哪个范围里搜的"
  list.appendChild(findScopeHead(symbol));
  const partial = incompleteReason(res);
  if (partial) list.appendChild(findWarnLine(partial));
  if (res.note) list.appendChild(findWarnLine(res.note));

  if (!defs.length && !refs.length) {
    list.appendChild(emptyBox(emptyFindText(symbol, res, partial)));
    // 只有「有文件提到、但全在字符串/注释里」才给这个出口：其余两种
    // （压根没这个词 / 扫描被打断）换模式也没用。
    if (onlyInLiterals(res)) list.appendChild(findToTextBtn());
    return;
  }

  const scope = res.scope || S.workspace || "";

  // 一条结果行（和原来一样：子行显示 类·函数 + 相对路径:行，下面一行代码）
  const makeRow = (it, indent) => {
    const row = document.createElement("div");
    row.className = "vrow";
    row.title = it.path + ":" + it.line;
    if (indent) row.style.paddingLeft = indent + "px";
    const head = document.createElement("div");
    head.className = "sub";
    const where = sc => (sc && sc.cls) ? (sc.cls + (sc.fn ? "·" + sc.fn : "") + "  ") : "";
    head.textContent = where(it.scope) + relPath(it.path, scope) + ":" + it.line;
    const code = document.createElement("div");
    code.style.cssText = "font-family:Consolas,monospace;font-size:11px;" +
      "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    code.textContent = it.text || "";
    row.appendChild(head);
    row.appendChild(code);
    row.addEventListener("click", () => openInEditor(it.path, baseName(it.path), it.line));
    return row;
  };

  if (!byFolder) {
    const group = (label, items) => {
      if (!items.length) return;
      const g = document.createElement("div");
      g.className = "vgroup";
      g.textContent = label + "　" + items.length;
      list.appendChild(g);
      for (const it of items) list.appendChild(makeRow(it, 0));
    };
    group("定义", defs);
    group("引用", refs);
    return;
  }

  // ---- 文件夹模式：先按目录分桶，再在每个目录里分定义/引用 ----
  // ---- 文件夹模式：按目录**树**展开，和左边文件树同一套观感 ----
  //
  // 以前是"每个完整目录路径一行"（`vendor/mediatek/proprietary/external/xxx`），
  // 左栏两百来像素，标题被省略号切成 `vendor/mediatek/proprietary/ex...`，
  // 一眼分不清是哪个仓；而且同级目录互不嵌套，看不出层次。现在按 `/` 拆成树。
  const root = { name: "", path: "", kids: new Map(), defs: [], refs: [] };
  let hitDirs = 0;                      // 有命中的目录数（中间层不算）
  const put = (it, kind) => {
    const folder = it.folder && it.folder !== "(根目录)" ? it.folder : "";
    let node = root;
    if (folder) {
      let acc = "";
      for (const seg of folder.split("/").filter(Boolean)) {
        acc = acc ? acc + "/" + seg : seg;
        let kid = node.kids.get(seg);
        if (!kid) {
          kid = { name: seg, path: acc, kids: new Map(), defs: [], refs: [] };
          node.kids.set(seg, kid);
        }
        node = kid;
      }
    }
    node[kind].push(it);
  };
  for (const it of defs) put(it, "defs");
  for (const it of refs) put(it, "refs");

  const subtreeCount = (n) => n.defs.length + n.refs.length
    + [...n.kids.values()].reduce((a, k) => a + subtreeCount(k), 0);
  // 这个目录自己有没有命中 —— 合并单链目录的判据之一（见 `collapseChain`）
  const hasOwnHit = (n) => n.defs.length + n.refs.length > 0;
  const countHitDirs = (n) => {
    if (n.defs.length || n.refs.length) hitDirs += 1;
    for (const k of n.kids.values()) countHitDirs(k);
  };
  countHitDirs(root);

  const head = document.createElement("div");
  head.className = "vgroup";
  head.textContent = "定义 " + defs.length + " · 引用 " + refs.length
    + " · " + hitDirs + " 个文件夹";
  list.appendChild(head);

  // `host` = 这一层往哪个容器里画：折叠时只切那个容器的 class，不再整棵树重画。
  // 子树**首次展开才建**（`data-built` 记一次），收起的分支连 DOM 都不产生。
  const renderNode = (node, depth, host) => {
    const kids = [...node.kids.values()]
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    for (const kid of kids) {
      // 单链目录压成一行，与文本模式同一套规则（`collapseChain`）。
      // 判据看 defs/refs：中间层两样都空才算「没有自己的命中」。
      // 注意这里 findFolderRow 传的是 `depth` 而不是 `depth + 1` —— 这是
      // 文件夹模式从来的写法，本轮不顺手改（改了整棵树的缩进都会变）。
      const cc = collapseChain(kid, kid.name, kid.path, hasOwnHit);
      const fkey = "s:" + cc.path;
      // 默认全展开：文件夹模式的用处是"看清分布"，一进来就全折上等于什么都没给
      const isOpen = !FIND.open.has(fkey);
      const grow = findFolderRow(cc.label, subtreeCount(cc.node), depth, null, isOpen, cc.path);
      const body = document.createElement("div");
      body.className = isOpen ? "" : "hidden";
      host.appendChild(grow);
      host.appendChild(body);
      const build = () => {
        if (body.dataset.built) return;
        body.dataset.built = "1";
        renderNode(cc.node, depth + 1, body);
      };
      if (isOpen) build();
      grow.addEventListener("click", () => { if (toggleFold(fkey, grow, body)) build(); });
    }

    // 命中行挂在这个目录下（比目录再缩进一层）
    const pad = 8 + (depth + 1) * FIND_INDENT;
    const label = (text, n) => {
      const t = document.createElement("div");
      t.className = "vgroup";
      t.style.paddingLeft = pad + "px";
      t.textContent = text + "　" + n;
      host.appendChild(t);
    };
    if (node.defs.length) {
      label("定义", node.defs.length);
      for (const it of node.defs) host.appendChild(makeRow(it, pad));
    }
    if (node.refs.length) {
      label("引用", node.refs.length);
      for (const it of node.refs) host.appendChild(makeRow(it, pad));
    }
  };
  renderNode(root, 0, list);
}

/**
 * 从文件树右键进来的入口：把搜索范围钉在这个目录上，切到搜索视图。
 *
 * 不在这里直接发请求 —— 用户通常是想在里面再改关键词/换模式，
 * 预填 + 聚焦比"替他搜一次"顺手。右键是文件时用它所在的目录。
 */
function searchInFolder(dir, name) {
  // code_search 被禁用时 🔍 这个按钮压根不存在（它的 VIEWS 读不到），
  // 面板也就永远刷不出来 —— 直接退回文件树，别让左栏停在一块空白上。
  if (!$("activity").querySelector('.act[data-view="search"]')) {
    switchView("files");
    return;
  }
  FIND.folder = dir || "";
  FIND.open.clear();
  switchView("search");
  paintFindScope();
  const inp = $("findInput");
  if (inp) {
    if (name) inp.value = name;
    inp.focus();
    inp.select();
  }
  if (inp && inp.value.trim()) doSearch();
}


// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 以前这张表在内核 50-views.js 里，内核得认识每个插件的 initXxxView。
// 前端资源搬进插件目录后，这件事也该由插件自己做。
//
// 时机：syncPluginViews() 先 await 加载本文件、再建活动栏按钮，
// 所以这里注册的 setter 一定赶得上第一次切视图。
viewRegister({
  view: "search",
  setters: () => initSearchView(),
});

// ---------------------------------------------------------------------------
// 右键菜单：把「在当前文件夹搜索」从内核搬到插件自己家
// ---------------------------------------------------------------------------
// 以前这两个菜单项写死在 40-tree.js 里，还配着 `typeof searchInFolder ===
// "function"` 的守卫 —— 内核明明不需要知道搜索这回事，却得替它判断"插件在不在"。
// 现在内核只负责建菜单骨架，插件往 tree.file / tree.dir 两个槽位登记自己的项。
//
// id 传插件文件夹名：插件被禁用时 menuItems() 会整批滤掉，不用自己撤销。
menuRegister("tree.file", "code_search", {
  label: "在当前文件夹搜索",
  order: 10,
  // 右键文件时搜的是**它所在目录**，但关键词预填成文件名 —— 最常见的意图是
  // "这目录里还有谁用这个文件/这个类"。
  run: (c) => searchInFolder(dirOfPath(c.path), c.name),
});

menuRegister("tree.dir", "code_search", {
  label: "在当前文件夹搜索",
  order: 10,
  run: (c) => searchInFolder(c.path, ""),
});
