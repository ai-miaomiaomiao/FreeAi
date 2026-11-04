// Gradle / 脚本
// 从 index.html 拆出（原第 6387-7004 行）。
// 视图：Gradle
// ---------------------------------------------------------------------------
let GRADLE_TREE = null;
let GRADLE_PROJECT = "";
let GRADLE_FLAT = [];     // 摊平的任务表（带小写 name/path），过滤时用（见 flatGradleTasks）
// 空 = 用默认那份（buildSrc/base/gradle/jbr17）。也可能是**自选**的绝对路径
// （候选列表只扫 buildSrc/base/gradle，工程外的 JDK 得靠「📁」自己挑）。
let GRADLE_JDK = "";
// JDK 是**工程级**的选择（`buildSrc/base/gradle/jbr17` 这种路径只在原来那个
// 工程里存在）。按工作区记一份：切走再切回来能恢复，切到别的工程则不带过去。
const GRADLE_JDK_BY_WS = {};
let GRADLE_JDK_START = "";        // 「📁」弹窗的起始目录（后端给的候选目录）
let GRADLE_DEV = "";      // 空 = 交给后端自动判定
// 列表区两段，各自可折叠（以前是一个「◷ 最近」按钮二选一 —— 想看最近就得把
// 任务树整个藏起来，切回来还得再点一次）。
// ALL TASKS 默认**折叠**：Android 工程动辄几万个任务，平时只用「最近」或
// 过滤框跑固定几个，一进来全铺开既慢又挡视线。要浏览时点标题展开。
const GRADLE_SEC = { all: false, recent: false };
let GRADLE_RECENT = [];    // 最近任务缓存：头部要显示 n/30，展开前就得有数
let GRADLE_LOADING = "";   // 非空 = 正在问 Gradle，正文显示这句话
let GRADLE_ERR = null;     // 非空 = 上一次读取失败的内容（errorBox 用）
// **这份树属于哪个工作区**。任务列表是跟着工作区走的（后端缓存也是按工程根存
// 在 <工作区>/.ai/temp/gradle_tools/），前端这几个变量必须一起换 —— 不然切了
// 工作区，面板上挂的还是上一个工程的任务，点 ▶ 算出来的命令也带着旧 project。
let GRADLE_WS = "";
const GRADLE_OPEN = {};   // 目录展开状态（按路径记，切走再回来不丢）
const GRADLE_MORE = {};   // 目录 → 这个目录已经放出多少个任务（点「还有 N 个」往上加）
let GRADLE_BUSY = false;  // 正在问 Gradle 要列表（那是个几分钟的请求，别重复发）
// 多选：Ctrl / Shift + 点击勾上的任务（key → {path, name}）。
// 存 key 而不是任务对象：任务树每次重画都新建一排 DOM，换工作区还会整批换对象，
// 只有 path / name 是稳定的。用 Map 而不是 Set 是为了跑的时候能还原出
// `{path, name}` —— RECENT 里的条目本来就只有这两个字段，统一形状后
// runGradleTask 两条路都能直接用。
const GRADLE_SEL = new Map();
// 上一次画列表时的过滤词：只为了判断"过滤结果换了一批没有"，
// 变了才把滚动条归零（见 paintGradle）。
let GRADLE_LAST_FILTER = null;

// 为什么有这两个数：车机这份工程（D:\workspace\demo）`gradle tasks --all` 有
// **51506** 条任务（100+ 个模块 × 每个 300 多个）。把这些一次性铺成 DOM，
// 就是十几万个节点 + 五万个 click 监听 —— 主线程直接锁死，看着就是"界面卡死"
// （缓存 tasks.json 也有 12MB）。所以：
//   ① 目录**默认折叠**（点开才画，切进视图只画几十个顶层目录）
//   ② 单个目录下的任务**分批**画（首批 GRADLE_PAGE 条，点一次再放一批）
//   ③ 过滤时**不画树**，直接平铺列出匹配项，且最多 GRADLE_FILTER_MAX 条
//   ④ 再加一道**总量闸门**：不管怎么点，一次最多画 GRADLE_MAX_ROWS 行
//      （只靠①，用户把 2032 个目录全点开照样是 3.4 万行 —— 目录多、每个目录
//       又不超 100 条，②那条分批拦不住）
const GRADLE_PAGE = 100;
const GRADLE_FILTER_MAX = 300;
const GRADLE_MAX_ROWS = 2000;

function initGradleView() {
  $("gradleRefresh").addEventListener("click", refreshGradle);
  $("gradleFilter").addEventListener("input", () => paintGradle());
  $("gradleJdk").addEventListener("change", () => {
    GRADLE_JDK = $("gradleJdk").value;
    GRADLE_JDK_BY_WS[S.workspace || ""] = GRADLE_JDK;
    // 落盘（刷新 / 重开面板后还是这份，见 saveGradleJdk），
    // 之后重刷一次：下面那行说明和 title 也跟着换，不然选完还显示上一个 JDK
    saveGradleJdk(GRADLE_JDK).then(() => refreshGradleJdk());
  });
  $("gradleJdkPick").addEventListener("click", pickGradleJdk);
  $("gradleDev").addEventListener("change", () => {
    GRADLE_DEV = $("gradleDev").value;
    // 落盘（刷新 / 重开面板后还是这台，见 saveGradleDevice），
    // 之后重刷一次：上面那行红字（「有 2 台在线但没选设备」）和「当前选择」
    // 当场跟着变，不用等下次点 ↻
    saveGradleDevice(GRADLE_DEV).then(() => refreshGradleDevices());
  });
  $("gradleDevRefresh").addEventListener("click", refreshGradleDevices);
  $("gradleRunSel").addEventListener("click", runGradleSelected);


  // 切走再切回来时，先核一下"面板上这份树还是当前工作区的吗"
  viewRegister({ view: "gradle", onshow: () => ensureGradleWorkspace() });
  WS_HANDLERS.gradle = ensureGradleWorkspace;

  // 设备与 JDK 都跟着工程走，统一由 resetGradleFor 去刷（它两个都会调）。
  // 这里不再各刷一遍 —— 否则面板第一次打开要枚举两次 adb。
  resetGradleFor(S.workspace || "");
}

/**
 * 工作区变了就换一份（见 GRADLE_WS 的说明）。**没进过这个视图就不管** ——
 * 等它第一次进来时由 initGradleView 自己初始化，省掉一次没人看的请求。
 */
function ensureGradleWorkspace() {
  if (!VIEW_INIT.gradle) return;
  if (GRADLE_WS !== (S.workspace || "")) resetGradleFor(S.workspace || "");
}

/**
 * 把 Gradle 面板整个换成 `ws` 这个工作区的。
 *
 * 后端的缓存/最近任务本来就按工作区分开存（`<工作区>/.ai/temp/gradle_tools/`），
 * 所以这里只要扔掉前端这份、重新读一次缓存就行 —— **不主动跑 Gradle**
 * （那要几分钟），没缓存就显示"点 ↻ 让 Gradle 列一次"。
 *
 * 展开状态（GRADLE_OPEN）也跟着清：目录路径是上一个工程的，留着没意义，
 * 而且会把新工程的同名目录莫名其妙地展开。
 */
function resetGradleFor(ws) {
  GRADLE_WS = ws;
  GRADLE_TREE = null;
  GRADLE_FLAT = [];
  GRADLE_PROJECT = ws;
  // JDK 也跟工程走：上一个工程自选的路径（多半在新工程里根本不存在）
  // 留着的话，refreshGradleJdk 会带着它去问，界面直接顶一行「（不存在）」
  GRADLE_JDK = GRADLE_JDK_BY_WS[ws] || "";
  for (const k in GRADLE_OPEN) delete GRADLE_OPEN[k];
  for (const k in GRADLE_MORE) delete GRADLE_MORE[k];
  GRADLE_RECENT = [];
  GRADLE_LOADING = "";
  GRADLE_ERR = null;
  // 勾选集也得清：换工程后那些 path 属于上一个工程，留着会让「运行选中的 N 个」
  // 把上一个工程的任务名发给新工程（后端会报任务不存在，或者更糟：撞上同名任务）
  GRADLE_SEL.clear();
  GRADLE_SEC.all = false;      // 换工程也回到折叠：用户没点开就不铺
  GRADLE_SEC.recent = false;

  paintGradle();               // 先画空壳（两段标题 + "未加载"）
  loadGradleCache();
  loadGradleRecent();
  // 候选 JDK 也跟着工程走（默认那份在 <工程根>/buildSrc/base/gradle 下）
  refreshGradleJdk();
  // 设备同理：选择是按工程存在各自的 prefs.json 里的，换工程必须重读，
  // 否则下拉还停在上一个工程选的那台（而那台多半跟这个工程无关）。
  // 清掉内存那份，让 refreshGradleDevices 走「存盘 / 自动」这条分支。
  GRADLE_DEV = "";
  refreshGradleDevices();
}

/** 拉一次「最近运行过的任务」。头部要显示 n/30，所以数据得先有。 */
async function loadGradleRecent() {
  try {
    const r = await api("/api/plugin/gradle_tools/recent", {});
    GRADLE_RECENT = (r.result || {}).recent || [];
  } catch (e) {
    GRADLE_RECENT = [];
  }
  paintGradle();
}

/** 把「最近任务」画进 `box`。点一下直接重跑（和点任务树里的 ▶ 一样）。 */
function paintGradleRecentInto(box) {
  if (!GRADLE_RECENT.length) {
    box.appendChild(emptyBox("还没有运行记录\n点任意任务跑一次就会记在这儿"));
    return;
  }
  for (const it of GRADLE_RECENT) {
    const row = document.createElement("div");
    row.className = "vrow";
    row.title = (it.path || it.name || "") + "\n点击直接运行，Ctrl+点击多选";
    row.appendChild(document.createTextNode("▶ " + (it.name || it.path)));
    // 光有 name 分不清是哪个模块：`:app:installDebugApk` 和
    // `:launcher:installDebugApk` 在列表里长得一模一样（截图里那两条）。
    // 把完整任务路径单独放一行，再带上时间。
    const bits = [];
    if (it.path && it.path !== it.name) bits.push(it.path);
    if (it.ago) bits.push(it.ago);
    if (bits.length) {
      const sub = document.createElement("div");
      sub.className = "sub";
      sub.style.paddingLeft = "12px";
      sub.textContent = bits.join("　·　");
      row.appendChild(sub);
    }
    // 最近任务行同样支持 Ctrl+点击多选：勾中的行按 key 记，
    // paintGradle 重画时由这里恢复高亮（DOM 是新建的，状态只能挂在 GRADLE_SEL 上）
    const task = { path: it.path, name: it.name };
    if (GRADLE_SEL.has(gradleKey(task))) row.classList.add("gpick");
    row.addEventListener("click", (ev) => gradleRowClick(ev, task));
    box.appendChild(row);
  }

  const foot = document.createElement("div");
  foot.className = "vrow";
  foot.style.textAlign = "center";
  const clr = document.createElement("button");
  clr.className = "tbtn mini";
  clr.textContent = "清空记录";
  clr.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const ok = await confirmBox("清空最近任务", "清掉这份运行记录？（不影响任务列表本身）", "清空");
    if (!ok) return;
    await api("/api/plugin/gradle_tools/clear_recent", {});
    loadGradleRecent();
  });
  foot.appendChild(clr);
  box.appendChild(foot);
}

async function loadGradleCache() {
  const ws = S.workspace || "";      // 这次读的是哪个工作区的缓存
  try {
    // 读缓存本身不启动 Gradle，但这份 tasks.json 实测有 12MB（见文件头 41-44 行），
    // 慢磁盘 / 大工程下 15s 余量偏紧，给 60s。仍然要有限时：读缓存卡住时
    // 不该让 await 永远挂着（正是 api() 149-151 行说的那种"看着像整页卡死"）。
    const r = await api("/api/plugin/gradle_tools/tasks_cached", {}, { timeout: 60000 });
    if (ws !== (S.workspace || "")) return;   // 期间切了工作区，这份属于上一个
    const res = r.result || {};
    GRADLE_ERR = null;
    if (!res.tasks || !res.tasks.length) {
      GRADLE_TREE = null;
      GRADLE_FLAT = [];
      paintGradle();
      return;
    }
    applyGradle(res);
  } catch (e) {
    if (ws !== (S.workspace || "")) return;
    GRADLE_ERR = "读取失败：" + e.message;
    paintGradle();
  }
}

async function refreshGradle() {
  // 后端**不限时**（跑完为止），这个工程要几分钟 —— 期间再点一次 ↻ 会同时起
  // 两个 gradle，互相抢 lock（报 "another Gradle instance"）。所以拦住重入。
  if (GRADLE_BUSY) { toast("上一个工程还在列任务，等它跑完再点"); return; }
  GRADLE_BUSY = true;

  const btn = $("gradleRefresh");
  const ws = S.workspace || "";      // 这次是给哪个工作区列的
  btn.disabled = true;
  // 不强行展开：折叠着也有反馈 —— 段标题右边会显示「加载中…」
  // （paintGradle 的 allNote）。用户折起来的段不该被一次刷新改掉。
  GRADLE_ERR = null;
  GRADLE_LOADING = "正在问 Gradle 要任务列表…\n" +
    "不限时、跑完为止。大工程（上百个模块）要几分钟，这期间界面还能用";
  paintGradle();
  try {
    // **必须 timeout: 0（不限时），不能吃 api() 的默认 15s。**
    // 后端 `do_tasks` 走 `_explicit_timeout(args)`：调用方没显式给 timeout
    // 就是 None = 让 gradle 跑完（`plugins/gradle_tools/__init__.py:97-109`
    // 记着由来 —— 以前默认 180s，于是等三分钟换来一句「列任务超时」，列表照样空）。
    // 前端这一行漏了参数，就变成「后端还在跑、浏览器 15s 先 abort」：界面报
    // 「请求超时（15s）」，而 gradle 仍在跑；用户再点 ↻ 就会起第二个 gradle
    // 抢 lock（正是上面 208-209 行警告的那种）。所以这里传 0。
    const r = await api("/api/plugin/gradle_tools/tasks", {}, { timeout: 0 });
    // 期间切走了就别往当前面板上画 —— 这批结果是上一个工程的
    if (ws !== (S.workspace || "")) return;
    const res = r.result || {};
    GRADLE_LOADING = "";
    if (!res.ok) {
      // 失败时后端给的是「结论 + 怎么办 + daemon 日志」这种多段文本，
      // 必须用左对齐、保留换行的块显示；塞进居中的 .view-empty 会拍成一行。
      GRADLE_ERR = res.content || res.summary || "读取失败";
      paintGradle();
      toast(res.summary || "读任务列表失败", true);
      return;
    }
    applyGradle(res);
  } catch (e) {
    if (ws !== (S.workspace || "")) return;
    GRADLE_LOADING = "";
    GRADLE_ERR = "读取失败：" + e.message;
    paintGradle();
  } finally {
    // 已知边界（本次不处理）：用户手动刷新页面 / 关掉标签后，后端 subprocess
    // 仍在跑完这批任务，HTTP 断开它不感知。改成 timeout: 0 之后这条不再由
    // 「前端 15s abort」触发，就少了一条"点了没反应又重入"的路径。
    GRADLE_BUSY = false;
    btn.disabled = false;
  }
}

/**
 * 把任务树摊平成一维（顺带把小写的 name / path 算好）。
 *
 * 过滤时每敲一个字符都要拿 name / path 比一遍；5 万条的树当场 toLowerCase
 * 出 10 万个临时字符串是纯浪费。摊平**一次**，之后只做 indexOf。
 */
function flatGradleTasks(tree) {
  const out = [];
  const walk = (node) => {
    for (const t of node.tasks || []) {
      out.push({ t, name: String(t.name || "").toLowerCase(),
                 path: String(t.path || "").toLowerCase() });
    }
    for (const d of node.dirs || []) walk(d);
  };
  if (tree) walk(tree);
  return out;
}

function applyGradle(res) {
  GRADLE_TREE = res.tree || null;
  GRADLE_PROJECT = res.project || S.workspace || "";
  GRADLE_WS = S.workspace || "";     // 记下这份树是谁的（见 GRADLE_WS 的说明）
  GRADLE_FLAT = flatGradleTasks(GRADLE_TREE);
  paintGradle();
}

async function refreshGradleDevices() {
  const sel = $("gradleDev");
  const keep = GRADLE_DEV;
  try {
    // 把当前选择**一起问**：不传的话后端只按存盘那份算，本页刚选还没落盘时
    // 「←」和下面那行红字说的是另一台。传 null 表示「按存盘 / 自动」。
    const r = await api("/api/plugin/gradle_tools/devices", { serial: keep || null });
    const res = r.result || {};
    sel.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "自动";
    sel.appendChild(auto);

    const list = res.devices || [];
    for (const d of list) {
      const o = document.createElement("option");
      o.value = d.serial;
      o.textContent = d.serial + (d.model ? "  " + d.model : "") +
        (d.state === "device" ? "" : "  [" + d.state + "]");
      sel.appendChild(o);
    }
    // 本页选过用本页的，否则用**存盘那份**（刷新页面后靠它把选择找回来）。
    const saved = res.saved || "";
    const want = keep || saved;
    // 选的那台不在列表里（拔了 / 换了口）：补一个 option 把它显示出来。
    // 不补的话 sel.value = want 设不上，下拉静默退回「自动」，而 GRADLE_DEV
    // 还是旧值 —— 跑命令时又把这台不在线的传下去。
    if (want && !list.some((d) => d.serial === want)) {
      const o = document.createElement("option");
      o.value = want;
      o.textContent = want + "  [不在线]";
      sel.appendChild(o);
    }
    // 选「自动」就让它**停在**「自动」：以前这里会拿后端自动选中的那台填回来，
    // 于是用户选了「自动」、刷新一下又变成具体设备，永远退不回去。
    sel.value = want;
    GRADLE_DEV = sel.value || "";
    const cur = res.device || {};
    sel.title = res.warning || (cur.label ? cur.serial + " · " + cur.label : "");
    // 枚举失败这类信息不能只塞 title（要悬停才看得见），直接摆一行出来
    const note = $("gradleDevNote");
    if (note) {
      const msg = res.warning || "";
      note.textContent = msg;
      note.classList.toggle("hidden", !msg);
      note.classList.toggle("bad", !!msg);
    }
    if (res.warning) toast(res.warning, true);
  } catch (e) {
    sel.innerHTML = '<option value="">读取失败</option>';
    const note = $("gradleDevNote");
    if (note) {
      note.textContent = "枚举失败：" + e.message;
      note.classList.remove("hidden");
      note.classList.add("bad");
    }
  }
}

/**
 * 把界面上选的设备落盘（`<工程>/.ai/temp/gradle_tools/prefs.json` 的 `serial`）。
 *
 * 以前 change 只改内存里的 `GRADLE_DEV`：后端每轮都当「没选设备」，于是下拉里
 * 显示着那台、红字却报「有 2 台设备在线但没选设备」，而且刷新页面后选择直接
 * 消失（`keep` 也一并没了）。空串是「自动」，同样要存 —— 不然下次又被自动填回。
 * 存盘失败只提示，不打断这次选择。
 */
async function saveGradleDevice(serial) {
  try {
    await api("/api/plugin/gradle_tools/devices",
              { serial: serial || "", save: true });
  } catch (e) {
    toast("设备选择没能存盘：" + e.message, true);
  }
}

/**
 * 把界面上选的 JDK 落盘（`<工程>/.ai/temp/gradle_tools/prefs.json`）。
 *
 * 以前只有内存里的 `GRADLE_JDK_BY_WS`：刷新页面就清空，用户以为自己一直用的是
 * jbr21，其实命令里已经少了 `-Dorg.gradle.java.home`，悄悄退回默认的 jbr17 ——
 * 构建行为跟着变，还很难发现。存盘失败只提示，不打断这次选择。
 */
async function saveGradleJdk(path) {
  try {
    await api("/api/plugin/gradle_tools/jdks",
              { java_home: path || "", save: true });
  } catch (e) {
    toast("JDK 选择没能存盘：" + e.message, true);
  }
}

async function refreshGradleJdk() {
  const sel = $("gradleJdk");
  const keep = GRADLE_JDK;
  try {
    // 把当前选择**一起问**：不传的话后端只会拿默认那份 jbr17 来描述 current，
    // 于是选了别的 JDK 时右边那行说明（和「←」标记）说的其实是另一份。
    const r = await api("/api/plugin/gradle_tools/jdks", { java_home: keep || null });
    const res = r.result || {};
    const cur = res.current || {};
    // 本页还没选过（刚打开面板 / 刚刷新页面）：用**存盘那份**，别退回默认。
    // 后端在没传 java_home 时也是这么兜底的（见 do_jdks），这里把下拉选中项对上。
    const saved = res.saved || "";
    const want = keep || saved;
    GRADLE_JDK_START = (res.searchDirs || [])[0] || "";
    sel.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "";
    // 文案别写死 buildSrc 路径：切到普通工程时那条默认是"不存在"的（后端已经
    // 在工程里没有 jbr17 时按"不指定"处理，这里跟着说清楚即可）
    auto.textContent = "默认（工程内 jbr17 / 系统 JDK）";
    sel.appendChild(auto);

    const list = res.jdks || [];
    for (const j of list) {
      const o = document.createElement("option");
      o.value = j.abs;
      o.textContent = (j.version ? "Java " + j.version + " · " : "") + j.rel;
      sel.appendChild(o);
    }
    // 手工挑的那份（或存盘的那份）不在候选列表里，也得留一格 ——
    // 否则重刷一次就悄悄回到「默认」。
    // 比较忽略大小写：Windows 上存盘的 `d:\...` 和候选里的 `D:\...` 是同一个目录，
    // 不忽略的话每次刷新都会多出一格重复的「（自选）」。
    const lower = (x) => String(x || "").toLowerCase();
    const hit = list.find((j) => lower(j.abs) === lower(want));
    if (want && !hit) {
      const o = document.createElement("option");
      o.value = want;
      o.textContent = "（自选）" + shortPath(want, 2);
      sel.appendChild(o);
    }
    // 用候选里那份的原始写法，不然 value 对不上 option，选中会落空
    if (want) sel.value = hit ? hit.abs : want;
    GRADLE_JDK = sel.value || "";
    // 不可用时说清楚为什么（截图里最常见的是「目录在，但不是本平台的 JDK」）
    sel.title = cur.set && !cur.exists
      ? "当前指定的 JDK 不可用：" + (cur.rel || cur.path)
      : "当前：" + (cur.rel || cur.path || "不指定");
    // 一行说明：`JDK 17 · buildSrc/base/gradle/jbr17（不存在）`，
    // 不可用时标红 —— 原来只有 title，不悬停根本不知道配的 JDK 是坏的
    const note = $("gradleJdkNote");
    if (note) {
      const parts = [];
      if (cur.version) parts.push("JDK " + cur.version);
      if (cur.rel || cur.path) parts.push(cur.rel || cur.path);
      const bad = !!(cur.set && !cur.exists);
      let txt = parts.join(" · ");
      if (bad) txt += "（不存在）";
      note.textContent = txt;
      note.classList.toggle("hidden", !txt);
      note.classList.toggle("bad", bad);
    }
    if (cur.set && !cur.exists) toast("JDK 不可用：" + (cur.rel || cur.path), true);
  } catch (e) {
    sel.innerHTML = '<option value="">读取失败</option>';
  }
}

/**
 * 「📁」= 自己到文件夹里挑一份 JDK。
 *
 * 下拉里的候选是后端**扫**出来的（工程里的 buildSrc/base/gradle，外加 Linux 上的
 * /home/android/sdk*），工程外的 JDK（Studio 自带的 jbr、单独装的 JDK）不在里面，
 * 所以得有个自己指的入口。选完**当场验一下**：只看本平台那把 bin/java，不合格就不
 * 收 —— 否则这个路径会一路带到 Gradle，变成
 * 「Java home supplied seems to be invalid」那种找不到北的报错。
 */
function pickGradleJdk() {
  pickDirectory({
    title: "选择 JDK 目录",
    hint: "选 JDK 的根目录（里面有 bin、lib 那一层），不是 bin 里面。\n"
        + "候选列表只扫过工程里的 buildSrc/base/gradle，工程外的 JDK 这样自己挑。",
    label: "用这个 JDK",
    start: GRADLE_JDK || GRADLE_JDK_START || GRADLE_PROJECT || "",
    onPick: (path) => setGradleJdk(path),
  });
}

async function setGradleJdk(path) {
  const p = String(path || "").trim();
  if (!p) return;
  try {
    const r = await api("/api/plugin/gradle_tools/jdks", { java_home: p });
    const res = r.result || {};
    const cur = res.current || {};
    if (!cur.exists) {
      toast("这个目录不能当 JDK：" + (res.currentText || ""), true);
      return;
    }
    GRADLE_JDK = p;
    GRADLE_JDK_BY_WS[S.workspace || ""] = p;
    // 验过了才落盘：坏路径存进去，下次打开面板会顶一行红字，还每次构建都失败
    await saveGradleJdk(p);
    await refreshGradleJdk();
    toast("JDK 已换成 " + (cur.rel || p));
  } catch (e) {
    toast("选择失败：" + e.message, true);
  }
}

/** 一行任务（▶ 名字 + 分组 + 描述）。任务树和过滤后的平铺列表共用。 */
function gradleTaskRow(t, indent) {
  const row = document.createElement("div");
  row.className = "vrow";
  row.style.paddingLeft = indent + "px";
  row.title = (t.path || t.name) + "\n点击直接运行，Ctrl+点击多选";

  const name = document.createElement("span");
  name.textContent = "▶ " + t.name;
  row.appendChild(name);

  if (t.group) {
    const g = document.createElement("span");
    g.className = "sub";
    g.textContent = "  " + String(t.group).replace(/\s*tasks?$/i, "");
    row.appendChild(g);
  }
  if (t.description) {
    const d = document.createElement("div");
    d.className = "sub";
    d.style.paddingLeft = "12px";
    d.textContent = t.description;
    row.appendChild(d);
  }
  // 高亮按 GRADLE_SEL 恢复：树 / 过滤结果 / 最近任务三处共用这个行构造器，
  // 所以只在这里判一次就够了
  if (GRADLE_SEL.has(gradleKey(t))) row.classList.add("gpick");
  row.addEventListener("click", (ev) => gradleRowClick(ev, t));
  return row;
}

/**
 * 过滤：**不画树**，把匹配的任务平铺列出来。
 *
 * 以前是"带着 filter 走一遍树" —— 树里 5 万个任务一个不少地过一遍，只是把不匹配的
 * 跳过。DOM 是少了，但找任务的路径依旧又深又长，而且每敲一个字符都全量重扫。
 * 平铺更符合"我在找某个任务"的用法：列出来、一眼看到、直接点。
 *
 * 限量是必须的：搜 `a` 能命中几万条，全画出来照样卡。
 */
function paintGradleMatches(list, filter) {
  const out = [];
  for (const it of GRADLE_FLAT) {
    if (it.name.indexOf(filter) >= 0 || it.path.indexOf(filter) >= 0) {
      out.push(it.t);
      if (out.length > GRADLE_FILTER_MAX) break;
    }
  }

  if (!out.length) {
    list.appendChild(emptyBox("没有匹配「" + filter + "」的任务"));
    return;
  }

  const more = out.length > GRADLE_FILTER_MAX;
  const head = document.createElement("div");
  head.className = "vgroup";
  head.textContent = "匹配　" + (more ? ">" + GRADLE_FILTER_MAX : out.length);
  list.appendChild(head);

  for (const t of out.slice(0, GRADLE_FILTER_MAX)) {
    const row = gradleTaskRow(t, 10);
    // 平铺时任务名会重名（每个模块都有 assembleDebug），把所属项目带上才分得清
    const p = document.createElement("div");
    p.className = "sub";
    p.style.paddingLeft = "12px";
    p.textContent = t.path || "";
    row.appendChild(p);
    list.appendChild(row);
  }
  if (more) {
    list.appendChild(emptyBox("匹配太多了，只列出前 " + GRADLE_FILTER_MAX +
      " 条\n继续输入几个字符缩小范围"));
  }
}

/**
 * 一段的标题行（ALL TASKS / RECENT TASKS）：可折叠，右边挂计数或状态。
 *
 * 为什么做成两段而不是一个「◷ 最近」按钮：那个按钮是**二选一**，想看最近就得
 * 先把任务树藏起来，切回来还要再点一次；两段各管各的，都能同时开着。
 *
 * `extra` 传 "sticky" 表示这一行吸顶（见 .g-sec.sticky）：列表滚到几千行时
 * 标题要一直挂在顶上，不然滑下去既不知道自己看的是哪一段，也点不到折叠。
 * 为什么不是全加：两段叠在一起时会白占两行高度，收起的/没内容的段不吸。
 */
function gradleSecHead(key, icon, label, note, extra) {
  const d = document.createElement("div");
  d.className = "g-sec" + (extra ? " " + extra : "");
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = GRADLE_SEC[key] ? "▾" : "▸";
  const ic = document.createElement("span");
  ic.textContent = icon;
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = label;
  const nt = document.createElement("span");
  nt.className = "cnt";
  nt.textContent = note || "";
  d.appendChild(caret);
  d.appendChild(ic);
  d.appendChild(nm);
  d.appendChild(nt);
  d.addEventListener("click", () => {
    GRADLE_SEC[key] = !GRADLE_SEC[key];
    if (key === "recent" && GRADLE_SEC.recent) { loadGradleRecent(); return; }
    paintGradle();
  });
  return d;
}

/**
 * 重画列表的入口。
 *
 * 展开/折叠是**全量重绘**，所以这里记一下滚动位置再还原 —— 不然每点开一层
 * 都跳回顶部，"一层层往下点"根本没法用（点第 30 个目录还得重新滚下去）。
 */
function paintGradle() {
  const list = $("gradleList");
  if (!list) return;
  const filter = (($("gradleFilter") || {}).value || "").trim().toLowerCase();
  // 过滤词变了才跳回顶部；**折叠目录 / 勾选任务**这类重绘要保持原位 ——
  // 多选是连续点好几行，每点一次都跳回顶上根本没法用。
  const keepTop = (filter === GRADLE_LAST_FILTER) ? list.scrollTop : 0;
  GRADLE_LAST_FILTER = filter;

  list.innerHTML = "";

  // ---- ALL TASKS ----
  const allNote = GRADLE_LOADING ? "加载中…"
    : (GRADLE_ERR ? "读取失败"
      : (GRADLE_TREE ? (filter ? "过滤中" : GRADLE_FLAT.length + " 个")
        : "未加载（点 ↻）"));
  list.appendChild(gradleSecHead("all", "☰", "ALL TASKS", allNote, "sticky"));
  const allBody = document.createElement("div");
  allBody.style.paddingBottom = "6px";   // 和下一个悬浮头之间的上边距（见 .g-sec.sticky）
  list.appendChild(allBody);
  if (!GRADLE_SEC.all) {
    allBody.classList.add("hidden");
  } else if (GRADLE_LOADING) {
    allBody.appendChild(emptyBox(GRADLE_LOADING));
  } else if (GRADLE_ERR) {
    allBody.appendChild(errorBox(GRADLE_ERR));
  } else if (!GRADLE_TREE) {
    allBody.appendChild(emptyBox("还没有任务列表\n点上面的 ↻ 让 Gradle 列一次" +
      "（Android 工程要等一会儿）"));
  } else if (filter) {
    paintGradleMatches(allBody, filter);
  } else {
    paintGradleTree(allBody);
  }

  // ---- RECENT TASKS ----
  const recentNote = GRADLE_RECENT.length + "/30";
  // 一个任务都没加载出来时，"最近" 也是空的（还在加载 / 没列过 / 列失败了）。
  // 这一段的标题上吸顶会把 "点 ↻ 列一次" 那句提示压掉半截，缩成一行也一样，
  // 所以这几种情况下不让它吸顶。
  const recentSticky = !(GRADLE_LOADING || GRADLE_ERR || !GRADLE_TREE);
  list.appendChild(gradleSecHead("recent", "◷", "RECENT TASKS", recentNote,
                                recentSticky ? "sticky" : ""));
  const recentBody = document.createElement("div");
  list.appendChild(recentBody);
  if (GRADLE_SEC.recent) paintGradleRecentInto(recentBody);
  else recentBody.classList.add("hidden");

  // 勾选数变了，头部按钮的显隐与文案跟着变。
  // 放在这里而不是每个改 GRADLE_SEL 的地方各写一次：paintGradle 是所有
  // "列表内容变了"的唯一出口，漏不掉。
  gradlePaintSel();
  list.scrollTop = keepTop;
}

/**
 * 画任务树。**只画展开的那一层**。
 *
 * 目录默认折叠（`GRADLE_OPEN[here] === true` 才算展开），点一次只往下走一层，
 * 子目录仍旧是折的 —— 所以"一层层展开"。这个工程（D:\workspace\demo）全展开是
 * 5 万个任务 / 17 万个 DOM 节点，那样点一下就锁死主线程。
 */
function paintGradleTree(list) {
  let shown = 0;
  let budget = GRADLE_MAX_ROWS;   // 还有多少行额度
  let cut = false;                // 额度用完了（要告诉用户去哪儿找剩下的）

  // 树是「项目 → 分组 → 任务」三层，按目录递归铺出来（和原版侧边栏同一形状）。
  const walk = (node, path, depth) => {
    for (const dir of node.dirs || []) {
      if (budget <= 0) { cut = true; return; }
      const here = path + "/" + dir.name;
      const open = GRADLE_OPEN[here] === true;

      budget--;
      const row = document.createElement("div");
      row.className = "vrow";
      row.style.paddingLeft = (10 + depth * 10) + "px";
      const caret = document.createElement("span");
      caret.className = "caret";
      caret.textContent = open ? "▾" : "▸";
      row.appendChild(caret);
      row.appendChild(document.createTextNode("📁 " + dir.name));
      const cnt = document.createElement("span");
      cnt.className = "sub";
      cnt.textContent = "  " + dir.count;
      row.appendChild(cnt);
      row.addEventListener("click", () => {
        GRADLE_OPEN[here] = !open;
        paintGradle();
      });
      list.appendChild(row);

      if (open) walk(dir, here, depth + 1);
    }

    // 一个目录下几百个任务全画出来是"卡死"的另一半 —— 分批（点一次多放一批）
    const pool = node.tasks || [];
    const limit = Math.max(GRADLE_PAGE, GRADLE_MORE[path] || 0);
    const head = pool.slice(0, limit);
    for (const t of head) {
      if (budget <= 0) { cut = true; return; }
      budget--;
      shown++;
      list.appendChild(gradleTaskRow(t, 10 + depth * 10 + 12));
    }
    if (pool.length > head.length && budget > 0) {
      const more = document.createElement("div");
      more.className = "vrow";
      more.style.paddingLeft = (10 + depth * 10 + 12) + "px";
      more.style.color = "var(--accent)";
      more.textContent = "…还有 " + (pool.length - head.length) + " 个（点这里再放 " +
        GRADLE_PAGE + " 个）";
      more.addEventListener("click", () => {
        GRADLE_MORE[path] = limit + GRADLE_PAGE;
        paintGradle();
      });
      budget--;
      list.appendChild(more);
    }
  };

  walk(GRADLE_TREE, "", 0);

  if (cut) {
    list.appendChild(emptyBox("这个工程有 " + GRADLE_FLAT.length + " 个任务，" +
      "一次最多画 " + GRADLE_MAX_ROWS + " 行，剩下的没画。\n" +
      "用上面的过滤框找 —— 折起来几个目录也能腾出额度"));
  } else if (!shown && !(GRADLE_TREE.dirs || []).length) {
    list.appendChild(emptyBox("没有任务"));
  }
}

// ---------------------------------------------------------------------------
// 多选：Ctrl / Shift + 点击勾任务，然后一起并行跑
// ---------------------------------------------------------------------------
// 普通点击**不变**，还是"点了就跑"（那是用得最多的路径，多一步确认都是倒退）。
// 只有带修饰键才进勾选模式，这样两种用法互不干扰。

function gradleKey(t) {
  return String((t && (t.path || t.name)) || "");
}

/** 一行被点了：带修饰键 = 勾选 / 取消（不执行），不带 = 原来的「点了就跑」。 */
function gradleRowClick(ev, t) {
  const multi = !!(ev && (ev.ctrlKey || ev.metaKey || ev.shiftKey));
  if (!multi) { runGradleTask(t); return; }
  if (ev.preventDefault) ev.preventDefault();   // 别再让行内 caret / 折叠跟着响应
  const k = gradleKey(t);
  if (!k) return;
  if (GRADLE_SEL.has(k)) {
    GRADLE_SEL.delete(k);
  } else {
    GRADLE_SEL.set(k, { path: t.path || k, name: t.name || k });
  }
  paintGradle();      // 全量重绘，高亮由 gradleTaskRow / recent 行按 GRADLE_SEL 恢复
}

/** 头部那个「▶ 运行选中的 N 个」：有没有、写什么字。 */
function gradlePaintSel() {
  const btn = $("gradleRunSel");
  if (!btn) return;
  const n = GRADLE_SEL.size;
  if (!n) { btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  btn.textContent = "▶ 运行选中的 " + n + " 个";
}

/**
 * 把勾上的任务逐个送进终端。
 *
 * **为什么串行 await，而不是 Promise.all 一把发**：
 * runInTerminal 里 `tab.busy = true` 是在 `await fetch` **之前**的同步段就执行的
 * （`src/html/js/90-tools.js:1417-1421`：先 `if (tab.controller) abort()`，再置
 * busy，然后才 fetch）。所以第二个任务进来时必然看到"当前标签忙"，从而走
 * `termNewTab` 新开一个标签。反过来若并发发出，两条可能都拿到同一个空闲标签，
 * 后者会把前者的 fetch abort 掉 —— 后端探到客户端断开就走 killpg 杀整棵进程树，
 * 任务1 直接被掐死（不是"看不见"那么轻）。串行还顺带保证标签顺序 = 勾选顺序。
 *
 * 不清空 `GRADLE_SEL` 就重画的话，用户会看到"点了运行，高亮还在"，
 * 以为没生效又点一次 —— 那就会把同一批任务再跑一遍。
 */
async function runGradleSelected() {
  const picked = Array.from(GRADLE_SEL.values());
  if (!picked.length) return;
  GRADLE_SEL.clear();
  paintGradle();
  for (const t of picked) await runGradleTask(t);
}

// 命令行交给后端算 —— 它要按默认终端决定 ./gradlew 还是 .\gradlew.bat，
// 还要拼上 JDK 与设备参数（引号规则在 POSIX shell 下不一样）。
async function runGradleTask(t) {
  try {
    const r = await api("/api/plugin/gradle_tools/command", {
      task: t.path || t.name,
      project: GRADLE_PROJECT,
      java_home: GRADLE_JDK || null,
      serial: GRADLE_DEV || null,
      args: null,
    });
    const res = r.result || {};
    if (!res.ok) { toast(res.error || "生成命令失败", true); return; }

    // 点了就跑：以前是"填进终端，让人再按一次回车"，等于多一步没意义的操作
    // （VS Code 的 Gradle 插件也是点了直接执行）。命令仍然由后端拼，
    // 终端那边照常流式输出，随时可以点停止。
    termCwd = res.cwd || GRADLE_PROJECT || S.workspace || "";
    if (typeof runInTerminal === "function") {
      // 当前标签还在跑命令 → 另开一个标签，别就地复用。
      // 就地复用的后果不是"看不见任务1"，而是任务1 真被杀掉：runInTerminal 对
      // 同一标签会 abort 上一条 fetch（90-tools.js `if (tab.controller) abort()`），
      // 后端探到客户端断开就走 _client_gone → killpg 掉整个进程树。所以上一个
      // gradle 任务（尤其是长编译）在点第二个任务时会被掐断。
      // 空闲标签维持就地复用，免得每点一次任务就堆一个终端；这条判断和
      // openTerminalAt 里 `if (!tab || tab.busy) termCreateTab()` 是同一个规则。
      // typeof 逐层判断：gradle 视图可能被单独加载（没有主界面），那时这些函数不存在。
      let target = (typeof termActiveTab === "function") ? termActiveTab() : null;
      if (target && target.busy && typeof termNewTab === "function") {
        const tab = termNewTab(termCwd);
        // 标签条上不能全是「终端 3 / 终端 4」—— 两个 gradle 并行时得一眼看出
        // 哪条是哪个任务（名字太长 CSS 会省略号，任务面板里能看到完整命令）
        const label = t.name || t.path;
        if (tab && label) {
          tab.name = label;
          if (typeof renderTermTabs === "function") renderTermTabs();
        }
        target = tab || target;
      }
      // 不 await（它会一直流到命令结束）；吞掉 rejection，别让失败变成
      // "Unhandled promise rejection" 糊在控制台上
      Promise.resolve(runInTerminal(res.command, termCwd)).catch(() => {});
      const where = target ? "（" + target.name + "）" : "";
      if (res.warning) toast(res.warning, true);
      else toast("已执行：" + (t.name || t.path) + where);
    } else {
      // 插件被单独加载（没有主界面）时的兜底：还是填进终端
      toggleTerminal(true);
      $("termIn").value = res.command;
      $("termIn").focus();
      toast("已填入终端，回车执行");
    }
    // 命令填进终端不代表已经执行，但后端通常在这时就记了 recent；
    // 拉一次让头部那个 n/30 跟上（拉不到就保持原样，不影响什么）
    loadGradleRecent();
  } catch (e) {
    toast("失败：" + e.message, true);
  }
}



// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 文件里已有的 viewRegister(...) 是 init 内部补 onshow 用的，那是"进了视图之后"；
// 这一条管的是"第一次进视图该跑什么" —— 没有它，按钮点开是空白。
viewRegister({
  view: "gradle",
  setters: () => initGradleView(),
  onshow: () => ensureGradleWorkspace(),
});
