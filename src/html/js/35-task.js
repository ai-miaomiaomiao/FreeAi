// ---------------------------------------------------------------------------
// 任务中心：左下角的进度面板
//
// 为什么需要它：上传、下载、拉取远程内容这些动作都在后端跑，前端只能等着，
// 中间没有任何反馈 —— 拖一个 200MB 的 apk 进来，界面上什么都没有，跟卡死一样。
// 这里做一个统一的"在手任务"列表，谁在跑都能往里登记。
//
// 设计取舍：
// 1. 用 XHR 而不是 fetch。fetch 的请求体上传进度需要 ReadableStream 分块，
//    浏览器支持参差；XHR 的 upload.onprogress 是十几年的老接口，稳。
// 2. 面板默认收起，只在有任务时自动弹出；没有任务时整个隐藏，不占地方。
// 3. 任务完成后停留 2.5 秒再消失 —— 一闪而过的"完成"等于没提示。
// 4. 失败不自动消失，必须手动关，否则出错信息一闪就没了，用户根本来不及看。
// ---------------------------------------------------------------------------

const TASKS = [];          // { id, name, kind, loaded, total, state, msg }
let TASK_SEQ = 0;
let TASK_OPEN = false;

const TASK_HOLD_MS = 2500; // 成功后停留多久

function taskBox() { return $("taskBox"); }

function ensureTaskPanel() {
  const box = taskBox();
  if (!box) return null;
  if (!box.dataset.ready) {
    box.dataset.ready = "1";
    box.innerHTML = "";
    const head = document.createElement("div");
    head.className = "task-head";
    const t = document.createElement("span");
    t.textContent = "任务";
    t.className = "task-title";
    const cnt = document.createElement("span");
    cnt.className = "task-count";
    cnt.id = "taskCount";
    const fold = document.createElement("button");
    fold.className = "task-fold";
    fold.id = "taskFold";
    fold.title = "收起 / 展开";
    fold.textContent = "▾";
    fold.addEventListener("click", () => {
      TASK_OPEN = !TASK_OPEN;
      renderTasks();
    });
    head.appendChild(t);
    head.appendChild(cnt);
    head.appendChild(fold);
    const list = document.createElement("div");
    list.className = "task-list";
    list.id = "taskList";
    box.appendChild(head);
    box.appendChild(list);
  }
  return box;
}

/** 登记一个任务，返回 id。 */
function taskStart(name, kind) {
  const id = "t" + (++TASK_SEQ);
  TASKS.push({
    id, name: name || "未命名", kind: kind || "task",
    loaded: 0, total: 0, state: "run", msg: "",
    t0: Date.now(),          // 开始时间：taskElapsed() 靠它算耗时，不设就永远是空串
    // 历史要落到**发起时**的工作区：任务跑着的时候用户可能切走，
    // 结束时再读 S.workspace 就记到别的项目里去了
    ws: S.workspace || "",
  });
  TASK_OPEN = true;          // 有新任务就展开
  renderTasks();
  return id;
}

/**
 * 更新进度。total=0 表示未知大小（显示不确定进度条）。
 *
 * 注意 total 的语义：传 0 / 不传 都表示"现在还不知道总大小"，所以要**显式清成 0**。
 * 以前是 `if (total) t.total = total;`，一旦先收到过确切大小、后面又来一个 total=0
 * 的事件，旧值会赖着不走，进度条永远退不回不确定动画。
 */
function taskProgress(id, loaded, total) {
  const t = TASKS.find((x) => x.id === id);
  if (!t) return;
  t.loaded = loaded || 0;
  t.total = total || 0;
  renderTasks();
}

function taskDone(id, msg) {
  const t = TASKS.find((x) => x.id === id);
  if (!t) return;
  t.state = "ok";
  t.t1 = Date.now();          // 任务总览要显示"跑了多久"，结束时间得留一个
  t.msg = msg || "";
  if (t.total) t.loaded = t.total;
  reportFileTask(t);
  renderTasks();
  setTimeout(() => taskRemove(id), TASK_HOLD_MS);
}

function taskFail(id, msg) {
  const t = TASKS.find((x) => x.id === id);
  if (!t) return;
  t.state = "err";
  t.t1 = Date.now();
  t.msg = msg || "失败";
  reportFileTask(t);       // 失败也要留一条历史，不然"上次传失败了"查不到
  renderTasks();           // 失败不自动消失，等用户看见
}

function taskRemove(id) {
  const i = TASKS.findIndex((x) => x.id === id);
  if (i >= 0) TASKS.splice(i, 1);
  renderTasks();
}

// ---------------------------------------------------------------------------
// 任务历史（落盘到 <工作区>/.ai/tasks/history.json）
// ---------------------------------------------------------------------------
// 这里的"任务"和上面的进度面板不是一回事：上面那些跑完就消失，而用户经常
// 「在 gu 里跑了个构建，切到别的项目之后想看结果」。所以完成/失败时记一条，
// 打开任务总览时把各工作区的历史合起来显示（见 loadTaskHistory）。

/** 记录一条任务历史。失败静默 —— 记历史不该影响任务本身。 */
function reportTask(task) {
  try {
    api("/api/tasks", task, { ws: task.ws || S.workspace || "" }).catch(() => {});
  } catch (e) { /* 忽略 */ }
}

/** 只留输出尾部：整份可能有几百 KB，历史里看结果够用。 */
function taskTail(text, n) {
  const s = String(text || "").replace(/\s+$/, "");
  return s.length > n ? "…\n" + s.slice(-n) : s;
}

/** 上传 / 下载完成或失败：也记进历史（终端在 90-tools.js 里自己上报）。 */
function reportFileTask(t) {
  if (!t || (t.kind !== "upload" && t.kind !== "download")) return;
  const t0 = t.t0 || 0;
  const t1 = t.t1 || Date.now();
  reportTask({
    id: "file-" + t.id + "-" + t0,
    kind: t.kind,
    name: t.name,
    cwd: "",
    ws: t.ws || S.workspace || "",
    state: t.state === "ok" ? "ok" : "err",
    startedAt: t0,
    endedAt: t1,
    ms: t1 - t0,
    tail: t.msg || "",
  });
}

function humanBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
}

function taskElapsed(t) {
  if (!t.t0) return "";
  const s = (Date.now() - t.t0) / 1000;
  return s < 1 ? "" : s.toFixed(0) + "s";
}

function renderTasks() {
  const box = ensureTaskPanel();
  if (!box) return;
  const list = $("taskList");
  const cnt = $("taskCount");
  const fold = $("taskFold");
  if (!list) return;

  // 没有任何任务 → 整个面板隐藏，别在左下角留个空盒子
  if (!TASKS.length) {
    box.classList.add("hidden");
    list.innerHTML = "";
    if (cnt) cnt.textContent = "";
    if (typeof updateTaskBadge === "function") updateTaskBadge();
    return;
  }
  box.classList.remove("hidden");

  const running = TASKS.filter((t) => t.state === "run").length;
  const failed = TASKS.filter((t) => t.state === "err").length;
  if (cnt) {
    cnt.textContent = failed
      ? `${failed} 失败 · ${running} 进行中`
      : (running ? `${running} 进行中` : "全部完成");
    cnt.className = "task-count" + (failed ? " bad" : (running ? "" : " good"));
  }
  if (fold) fold.textContent = TASK_OPEN ? "▾" : "▸";
  list.classList.toggle("hidden", !TASK_OPEN);

  list.innerHTML = "";
  TASKS.forEach((t) => {
    const row = document.createElement("div");
    row.className = "task-item " + t.state;

    const top = document.createElement("div");
    top.className = "task-line";

    const nm = document.createElement("span");
    nm.className = "task-name";
    nm.textContent = t.name;
    nm.title = t.name;

    const st = document.createElement("span");
    st.className = "task-state";
    if (t.state === "run") {
      // 有总大小就显示百分比，没有就转圈
      st.textContent = t.total ? Math.floor(t.loaded / t.total * 100) + "%" : "…";
    } else if (t.state === "ok") {
      st.textContent = "✓";
    } else {
      st.textContent = "✕";
    }

    const x = document.createElement("button");
    x.className = "task-x";
    x.textContent = "✕";
    x.title = t.state === "run" ? "取消" : "移除";
    x.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (t.state === "run" && t.abort) { try { t.abort(); } catch (e) {} }
      taskRemove(t.id);
    });

    top.appendChild(nm);
    top.appendChild(st);
    top.appendChild(x);
    row.appendChild(top);

    const bar = document.createElement("div");
    bar.className = "task-bar";
    const fill = document.createElement("div");
    fill.className = "task-fill";
    if (t.total) {
      fill.style.width = Math.min(100, t.loaded / t.total * 100) + "%";
    } else {
      fill.classList.add("indet");     // 不确定进度：走一条循环动画
    }
    bar.appendChild(fill);
    row.appendChild(bar);

    const sub = document.createElement("div");
    sub.className = "task-sub";
    const parts = [];
    if (t.total) parts.push(humanBytes(t.loaded) + " / " + humanBytes(t.total));
    else if (t.loaded) parts.push(humanBytes(t.loaded));
    if (t.state === "err" && t.msg) parts.push(t.msg);
    else if (t.state === "ok" && t.msg) parts.push(t.msg);
    sub.textContent = parts.join(" · ");
    if (sub.textContent) row.appendChild(sub);

    list.appendChild(row);
  });
  if (typeof updateTaskBadge === "function") updateTaskBadge();
}

/**
 * 带进度的文件上传。走 XHR 是因为 fetch 拿不到上传进度。
 * 返回 { ok, status, data }，失败不抛异常（调用方据此继续处理下一个文件）。
 */
function uploadWithProgress(file, dst, taskId) {
  return new Promise((res) => {
    const t = TASKS.find((x) => x.id === taskId);
    const xhr = new XMLHttpRequest();
    if (t) t.abort = () => xhr.abort();

    xhr.open("POST", "/api/fs/upload?path=" + encodeURIComponent(dst) + "&overwrite=1"
             + "&ws=" + encodeURIComponent(S.workspace || ""));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Authorization", "Bearer " + S.token);

    if (xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) taskProgress(taskId, e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) {}
      res({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, data });
    };
    xhr.onerror = () => res({ ok: false, status: 0, data: { error: "网络错误" } });
    xhr.onabort = () => res({ ok: false, status: -1, data: { error: "已取消" } });
    xhr.send(file);
  });
}


// ---------------------------------------------------------------------------
// 任务总览（左下角 ⏱）
//
// 和上面那个"上传/下载进度条"不是一回事：那个只在有文件任务时冒出来，
// 这个是把**所有在手的事**汇总成一张表 —— 会话在生成、终端在跑命令、
// 文件在上传/下载，各自跑了多久。入口固定在活动栏底部，任何时候都能点开。
// ---------------------------------------------------------------------------

/** 毫秒 → 人话耗时：`12 秒` / `3 分 5 秒` / `1 时 12 分`。 */
function fmtDur(ms) {
  const s = Math.max(0, Math.round((ms || 0) / 1000));
  if (s < 60) return s + " 秒";
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return m + " 分" + (sec ? " " + sec + " 秒" : "");
  return Math.floor(m / 60) + " 时 " + (m % 60) + " 分";
}

/**
 * 收集当前在手的任务。耗时是"到现在"或"到结束"。
 *
 * 每一行都带 `ws`：任务列表要按工作区分组（切走之后在后台跑的那些也得算在
 * 它们自己那个项目名下，而不是堆在"当前工作区"里）。
 */
function collectTasks() {
  const rows = [];

  // 1) 会话：正在生成的那几个标签（别的项目的也在列表里，点一下能切过去）
  for (const key of (S.tabOrder || [])) {
    const t = (S.tabs || {})[key];
    if (!t || !t.running) continue;
    rows.push({
      group: "会话",
      name: t.title || t.sid,
      note: t.ws || "",
      ws: t.ws || "",
      t0: t.runT0 || Date.now(),
      running: true,
      go: () => { closeModal(); if (S.tabs[t.key]) switchTabByUser(t.key); },
      goTip: "点一下切到这个会话",
    });
  }

  // 2) 终端：每个标签页各跑各的，在跑的全列出来（同一标签页里后一条会掐掉
  //    前一条，见 90-tools.js）。`typeof` 在 TDZ 里会抛，所以套一层 try ——
  //    35-task 比 90-tools 先加载。
  //    切走的工作区里那些命令行也在这张表里（termRunningJobs 不按 ws 过滤），
  //    点一下能切回去、并停在对那个标签上（termFocusTab）。
  let jobs = [];
  try {
    if (typeof termRunningJobs === "function") jobs = termRunningJobs() || [];
    else if (typeof TERM_JOB !== "undefined" && TERM_JOB) jobs = [TERM_JOB];
  } catch (e) { jobs = []; }
  for (const job of jobs) {
    if (!job || !job.running) continue;
    const tab = job.tab || null;
    rows.push({
      group: "终端", name: job.cmd, note: job.cwd || "",
      ws: job.ws || (tab && tab.ws) || "",
      t0: job.t0 || Date.now(), running: true,
      go: (tab && typeof termFocusTab === "function")
        ? () => { closeModal(); termFocusTab(tab.id); }
        : null,
      goTip: "点一下切到这个终端",
    });
  }

  // 3) 上传 / 下载：走 35-task.js 自己的 TASKS
  for (const t of TASKS) {
    if (t.kind !== "upload" && t.kind !== "download") continue;
    rows.push({
      group: t.kind === "upload" ? "上传" : "下载",
      name: t.name,
      note: t.msg || "",
      ws: t.ws || "",
      t0: t.t0,
      t1: t.state === "run" ? 0 : (t.t1 || Date.now()),
      running: t.state === "run",
      state: t.state,
      pct: t.total ? Math.floor(t.loaded / t.total * 100) : -1,
    });
  }
  return rows;
}

/** 一行任务（会话 / 终端 / 上传 / 下载共用）。 */
function taskRowEl(r) {
  const label = { "会话": "chat", "终端": "term", "上传": "up", "下载": "down" };
  const row = document.createElement("div");
  row.className = "tk-row" + (r.running ? "" : " done") + (r.state === "err" ? " bad" : "");

  const g = document.createElement("span");
  g.className = "tk-g tk-" + (label[r.group] || "chat");
  g.textContent = r.group;

  const nm = document.createElement("span");
  nm.className = "tk-name";
  // 上传/下载的任务名里本来带"上传 xxx"（浮动面板那边靠它区分），
  // 这里的行首已经有类型小标签了，去掉重复的前缀。
  nm.textContent = (r.group === "上传" || r.group === "下载")
    ? String(r.name).replace(/^(上传|下载)\s*/, "") : r.name;
  nm.title = r.name;      // 名字被省略号截断时，悬停还能看全

  row.appendChild(g);
  row.appendChild(nm);
  if (r.pct >= 0) {
    const p = document.createElement("span");
    p.className = "tk-pct";
    p.textContent = r.pct + "%";
    row.appendChild(p);
  }
  const tm = document.createElement("span");
  tm.className = "tk-time";
  tm.textContent = fmtDur((r.t1 || Date.now()) - (r.t0 || Date.now()));
  row.appendChild(tm);

  const tip = (r.note ? r.note + "\n" : "") + (r.go ? (r.goTip || "点一下切过去") : "");
  if (tip) row.title = tip;
  if (r.go) {
    row.style.cursor = "pointer";
    row.addEventListener("click", r.go);
  }
  return row;
}

function paintTaskBoard(box) {
  const rows = collectTasks();
  box.innerHTML = "";
  if (!rows.length) {
    const head = document.createElement("div");
    head.className = "tk-group";
    head.textContent = "现在在跑";
    box.appendChild(head);
    box.appendChild(emptyBox("现在没有在跑的任务\n会话生成、终端命令、上传下载都会出现在这里"));
    return;
  }

  // 按工作区分组：切走之后在后台跑着的会话/终端都在这里，各自归到自己项目名下。
  // 当前工作区排最前，其余按路径排 —— 组的位置必须固定，否则每秒重画时
  // 耗时数字一变、组的顺序跟着跳，根本没法点。
  const cur = S.workspace || "";
  const groups = new Map();
  for (const r of rows) {
    const ws = r.ws || "";
    if (!groups.has(ws)) groups.set(ws, []);
    groups.get(ws).push(r);
  }
  const keys = Array.from(groups.keys()).sort((a, b) => {
    if (a === cur) return -1;
    if (b === cur) return 1;
    return a.localeCompare(b);
  });

  for (const ws of keys) {
    const head = document.createElement("div");
    head.className = "tk-group tk-group-ws" + (ws === cur ? " cur" : "");

    const nm = document.createElement("span");
    nm.className = "tk-ws-name";
    nm.textContent = ws ? wsLabel(ws) : "未归属";
    nm.title = ws || "";
    head.appendChild(nm);

    if (ws === cur) {
      const tag = document.createElement("span");
      tag.className = "tk-ws-tag";
      tag.textContent = "当前";
      head.appendChild(tag);
    } else if (ws) {
      // 组头也能点：不想找具体是哪个会话、只想切过去时用
      head.style.cursor = "pointer";
      head.title = "切到 " + ws;
      head.addEventListener("click", () => { closeModal(); syncWorkspace(ws); });
    }
    box.appendChild(head);

    for (const r of groups.get(ws)) box.appendChild(taskRowEl(r));
  }
}

let _taskBoardTimer = 0;

//: 各工作区合并后的任务历史（新的在前）
let TASK_HISTORY = [];
let _taskHistReq = 0;

/**
 * 拉历史：把**已打开的每个工作区**的 `.ai/tasks/history.json` 都读一遍再合并。
 *
 * 为什么不只读当前工作区：用户的原话是"在 gu 跑完切走就看不到了"。
 * 只读当前那个项目，切到 B 之后 A 的记录就永远看不到；而 S.trees 里恰好
 * 记着所有打开过的根。
 */
async function loadTaskHistory() {
  const trees = (S.trees && S.trees.length)
    ? S.trees.slice() : [S.workspace].filter(Boolean);
  const my = ++_taskHistReq;
  const lists = await Promise.all(trees.map((ws) =>
    api("/api/tasks", undefined, { ws })
      .then((r) => (r && r.tasks) || [])
      .catch(() => [])));
  if (my !== _taskHistReq) return false;      // 期间又刷新了一次，以最新的为准
  const out = [];
  for (const list of lists) for (const t of list) out.push(t);
  out.sort((a, b) => (b.endedAt || b.savedAt || 0) - (a.endedAt || a.savedAt || 0));
  TASK_HISTORY = out.slice(0, 100);
  return true;
}

function fmtTaskTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, "0");
  return (d.getMonth() + 1) + "-" + p(d.getDate()) + " "
    + p(d.getHours()) + ":" + p(d.getMinutes());
}

/** 历史区：**不跟着每秒重画** —— 点开一条看输出时不能被刷回去。 */
function paintTaskHistory(box) {
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "tk-group";
  const title = document.createElement("span");
  title.textContent = "最近运行过"
    + (TASK_HISTORY.length ? "（" + TASK_HISTORY.length + "）" : "");
  const refresh = document.createElement("button");
  refresh.className = "tbtn mini";
  refresh.textContent = "刷新";
  refresh.title = "重新读各工作区的 .ai/tasks";
  refresh.addEventListener("click", () => {
    title.textContent = "读取中…";
    loadTaskHistory().then((ok) => { if (ok) paintTaskHistory(box); });
  });
  head.appendChild(title);
  head.appendChild(refresh);
  box.appendChild(head);

  if (!TASK_HISTORY.length) {
    box.appendChild(emptyBox(
      "还没有历史任务\n跑过的会话 / 终端命令 / 上传下载会记在 .ai/tasks 里"));
    return;
  }

  for (const t of TASK_HISTORY) {
    const row = document.createElement("div");
    row.className = "tk-row hist" + (t.state === "err" ? " bad" : "");
    row.title = "点一下看输出";

    const g = document.createElement("span");
    g.className = "tk-g " + (t.kind === "term" ? "tk-term" : "tk-chat");
    g.textContent = t.kind === "term" ? "终端"
      : (t.kind === "upload" ? "上传"
        : (t.kind === "download" ? "下载"
          : (t.kind === "chat" ? "会话" : (t.kind || "任务"))));

    const nm = document.createElement("span");
    nm.className = "tk-name";
    nm.textContent = t.name || "未命名";
    nm.title = (t.cwd ? t.cwd + "\n" : "") + (t.name || "");

    const ws = document.createElement("span");
    ws.className = "tk-ws";
    ws.textContent = wsLabel(t.ws || "");
    ws.title = t.ws || "";

    const tm = document.createElement("span");
    tm.className = "tk-time";
    tm.textContent = fmtTaskTime(t.endedAt)
      + (t.ms ? " · " + fmtDur(t.ms) : "");

    const st = document.createElement("span");
    st.className = "tk-pct";
    st.textContent = t.state === "ok" ? "✓" : (t.state === "err" ? "✕" : "—");

    row.appendChild(g);
    row.appendChild(nm);
    row.appendChild(ws);
    row.appendChild(tm);
    row.appendChild(st);
    row.addEventListener("click", () => toggleTaskDetail(row, t));
    box.appendChild(row);
  }
}

/** 展开/收起一条历史任务的输出。 */
function toggleTaskDetail(row, t) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("tk-detail")) { next.remove(); return; }

  const d = document.createElement("div");
  d.className = "tk-detail";

  const meta = document.createElement("div");
  meta.className = "tk-meta";
  const bits = [];
  if (t.ws) bits.push("工作区 " + t.ws);
  if (t.cwd) bits.push("目录 " + t.cwd);
  if (t.startedAt) bits.push("开始 " + fmtTaskTime(t.startedAt));
  if (t.ms) bits.push("耗时 " + fmtDur(t.ms));
  // 会话没有退出码，硬写一行「退出码 —」只会让人以为出错了
  if (t.kind !== "chat") bits.push(t.code == null ? "退出码 —" : "退出码 " + t.code);
  meta.textContent = bits.join(" · ");
  d.appendChild(meta);

  const pre = document.createElement("pre");
  pre.className = "tk-out";
  pre.textContent = t.tail || "（没有记录输出）";
  d.appendChild(pre);

  if (t.ws && t.ws !== S.workspace) {
    const go = document.createElement("button");
    go.className = "tbtn mini";
    go.textContent = "切到这个工作区";
    go.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      closeModal();
      await activateTree(t.ws);
    });
    d.appendChild(go);
  }

  row.after(d);
}

function openTaskBoard() {
  const body = document.createElement("div");
  body.className = "tk-board";

  // 两个区分开：上面每秒重画（耗时在走），下面只在打开/点刷新时画一次，
  // 否则展开的输出每秒被重建一遍，根本没法看。
  const live = document.createElement("div");
  live.className = "tk-live";
  const hist = document.createElement("div");
  hist.className = "tk-hist";
  body.appendChild(live);
  body.appendChild(hist);

  paintTaskBoard(live);
  hist.textContent = "读取历史…";
  loadTaskHistory().then((ok) => { if (ok) paintTaskHistory(hist); });

  openModal("任务", body, null, "tk-modal");
  clearInterval(_taskBoardTimer);
  _taskBoardTimer = setInterval(() => {
    if (!document.body.contains(body)) { clearInterval(_taskBoardTimer); _taskBoardTimer = 0; return; }
    paintTaskBoard(live);
  }, 1000);
  modalOnClose = () => { clearInterval(_taskBoardTimer); _taskBoardTimer = 0; };
}

/** 活动栏那个 ⏱ 上的角标：有几个"还在跑"的。 */
function updateTaskBadge() {
  const b = $("taskBadge");
  if (!b) return;
  let n = 0;
  try {
    n = collectTasks().filter((r) => r.running).length;
  } catch (e) { n = TASKS.filter((t) => t.state === "run").length; }
  b.textContent = n ? String(n) : "";
  b.classList.toggle("hidden", !n);
}

// 入口已经并进活动栏底部的 ⚙ 菜单（见 99-boot.js 的 openSettingsMenu），
// 这里只把角标刷一次；openTaskBoard 仍然由菜单调用。
//
// ⚠ 不能立即调用：updateTaskBadge → collectTasks 会读 TASKS / 各种顶层 const，
// 而它们在 90-tools.js 等**后面的文件**里才声明。35-task.js 在 <script> 链里排得
// 早，加载到这里就调用 = 读 TDZ 里的 TASKS，整页立刻「Cannot access 'TASKS'
// before initialization」。
//
// 本文件是同步 <script>（index.html 第 497 行，在 body 中段），执行到这里时文档
// 必然还在解析中，readyState 只可能是 "loading"，所以没有"else 直接调用"这条
// 退路 —— 那一条是死代码，而且真按它跑就是上面那个 TDZ 崩溃。
document.addEventListener("DOMContentLoaded", updateTaskBadge, { once: true });
