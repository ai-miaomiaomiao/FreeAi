// ---------------------------------------------------------------------------
// 视图：设备文件
// ---------------------------------------------------------------------------
function initDeviceView() {
  $("devRefresh").addEventListener("click", refreshDevices);
  $("devScrcpy").addEventListener("click", () => devScrcpy($("devSel").value));
  $("devRoot").addEventListener("click", () => devRootRemount($("devSel").value));
  $("devGo").addEventListener("click", () => listDevice($("devPath").value.trim() || "/"));
  $("devSel").addEventListener("change", () => listDevice($("devPath").value.trim() || "/"));

  // 设备下拉框右键：这台设备能做的动作。
  // 以前这一排只有三个按钮，像「固定 5555」这种没有按钮的动作就没入口 ——
  // 右键是这些"偶尔用一次、但要用的那一刻得找得到"的动作最省地方的地方。
  $("devSel").addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    const serial = $("devSel").value || "";
    ctx.innerHTML = "";

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = serial || "没有选中设备";
    ctx.appendChild(title);

    const item = (label, fn, dis) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (dis) { b.disabled = true; b.style.opacity = ".45"; }
      else b.addEventListener("click", () => { hideCtx(); fn(); });
      ctx.appendChild(b);
    };
    const sep = () => ctx.appendChild(
      Object.assign(document.createElement("div"), { className: "sep" }));

    // 最前面一条：这是"拿到设备后第一件想做的事"，别埋在一堆维护动作里
    item("adb 终端（自动 -s）", () => devAdbTerm(serial), !serial);
    sep();
    item("固定 TCP/IP 5555（无线 adb）", () => devFixTcpip(serial), !serial);
    sep();
    item("投屏（scrcpy）…", () => devScrcpy(serial), !serial);
    item("提权（adb root + remount）", () => devRootRemount(serial), !serial);
    sep();
    item("刷新设备列表", () => refreshDevices());

    ctx.classList.remove("hidden");
    const r = ctx.getBoundingClientRect();
    ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
    ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
  });
  $("devPath").addEventListener("keydown", (e) => {
    if (e.key === "Enter") listDevice($("devPath").value.trim() || "/");
  });

  $("devMkdir").addEventListener("click", () => {
    const serial = $("devSel").value;
    if (!serial) { toast("先选一个设备", true); return; }
    devNewFolder(serial, $("devPath").value.trim() || "/");
  });

  // 空白处右键：建目录 / 刷新 / 回根
  $("devList").addEventListener("contextmenu", (ev) => {
    if (ev.target.closest(".vrow")) return;      // 条目自己有菜单
    ev.preventDefault();
    const serial = $("devSel").value;
    if (!serial) return;
    const dir = $("devPath").value.trim() || "/";

    ctx.innerHTML = "";
    const info = document.createElement("div");
    info.className = "title";
    info.textContent = dir;
    ctx.appendChild(info);

    const item = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", () => { hideCtx(); fn(); });
      ctx.appendChild(b);
    };
    item("新建文件夹…", () => devNewFolder(serial, dir));
    ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
    item("刷新", () => listDevice(dir));
    item("回到设备根 /", () => listDevice("/"));

    ctx.classList.remove("hidden");
    const r = ctx.getBoundingClientRect();
    ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
    ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
  });

  refreshDevices();
}

async function refreshDevices() {
  const sel = $("devSel");
  const list = $("devList");
  sel.innerHTML = "";
  try {
    const r = await api("/api/plugin/adb_tools/devices", {});
    const devs = (r.result || {}).devices || [];
    if (!devs.length) {
      sel.innerHTML = '<option value="">没有设备</option>';
      list.innerHTML = "";
      list.appendChild(emptyBox("没检测到设备\n检查 USB 连接，或先 adb connect <ip>"));
      return;
    }
    for (const d of devs) {
      const o = document.createElement("option");
      o.value = d.serial;
      o.textContent = d.serial + (d.model ? "  " + d.model : "") + "  [" + d.state + "]";
      sel.appendChild(o);
    }
    await listDevice($("devPath").value.trim() || "/");
  } catch (e) {
    sel.innerHTML = '<option value="">读取失败</option>';
    list.innerHTML = "";
    list.appendChild(emptyBox("读取设备失败：" + e.message));
  }
}

/**
 * 确保设备列表枚举过了，返回当前选中的序列号（可能是空串 = 让后端自己挑）。
 *
 * 为什么要这一层：日志视图原来直接读 `$("devSel").value`，而那个 `<select>`
 * 是**设备视图**初始化时填的。没进过设备视图时它只有一个占位项 ——
 * 那个占位项的值被当成了序列号发给后端，于是后端回「没有可用设备」，
 * 而设备明明插着。谁要用设备号谁先 ensure 一次就不会有这个坑。
 *
 * 同一时刻只枚举一次（多个视图同时进来不会打三次 adb）；
 * 没枚举出东西就把缓存置空，下次还会重试。
 */
let DEVICES_READY = null;

async function ensureDevices(force) {
  if (force || !DEVICES_READY) {
    DEVICES_READY = refreshDevices().catch(() => { DEVICES_READY = null; });
  }
  await DEVICES_READY;
  const v = $("devSel").value || "";
  if (!v && !force) DEVICES_READY = null;      // 没结果 → 下次重试
  return v;
}

function fmtTime(ms) {
  const t = Number(ms) || 0;
  if (!t) return "";
  const d = new Date(t);
  const p = (x) => (x < 10 ? "0" : "") + x;
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

async function listDevice(path) {
  const serial = $("devSel").value;
  const list = $("devList");
  if (!serial) return;

  $("devPath").value = path;
  list.innerHTML = "";
  list.appendChild(emptyBox("读取中…"));

  try {
    const r = await api("/api/plugin/adb_tools/ls", { serial, remote: path });
    const res = r.result || {};
    list.innerHTML = "";
    if (!res.ok) { list.appendChild(emptyBox(res.error || "读取失败")); return; }

    const base = path.replace(/\/+$/, "");

    if (path !== "/" && path !== "") {
      const up = path.replace(/\/[^/]*\/?$/, "") || "/";
      const row = rowItem("📁 ..", up);
      row.addEventListener("click", () => listDevice(up));
      list.appendChild(row);
    }

    const entries = res.entries || [];
    if (!entries.length) { list.appendChild(emptyBox("空目录")); return; }

    for (const en of entries) {
      const full = base + "/" + en.name;
      const text = (en.isDir ? "📁 " : "📄 ") + en.name +
        (en.isDir ? "" : "　" + fmtSize(en.size));
      const row = rowItem(text, full);
      // 悬浮显示权限 / 属主 / 时间 —— 这些是搬运 parser.js 之后才有的
      row.title = [full, en.perms ? en.perms + "  " + (en.mode || "") : "",
                   en.owner ? en.owner + ":" + (en.group || "") : "",
                   fmtTime(en.mtimeMs),
                   en.linkTarget ? "→ " + en.linkTarget : ""]
        .filter(Boolean).join("\n");
      if (en.isDir) {
        row.addEventListener("click", () => listDevice(full));
      } else {
        row.addEventListener("click", () => openRemoteFile(serial, full));
      }
      row.addEventListener("contextmenu", (ev) =>
        devItemMenu(ev, serial, Object.assign({ full: full }, en),
                    () => listDevice(path)));
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = "";
    list.appendChild(emptyBox("读取失败：" + e.message));
  }
}

// ---------------------------------------------------------------------------
// 设备文件操作（对应 adb_tools 的 mkdir / rename / delete / stat）
// ---------------------------------------------------------------------------
// 这些操作原来完全不存在 —— 设备面板只能看。后端现在补齐了，
// 这里给它接上入口。
const DEV_WRITABLE = ["/sdcard", "/storage", "/mnt/sdcard", "/mnt/media_rw",
                      "/data/local/tmp"];

function devIsWritable(p) {
  return DEV_WRITABLE.some((pre) => p === pre || p.startsWith(pre + "/"));
}

function devAsk(title, label, initial) {
  return new Promise((resolve) => {
    const body = document.createElement("div");
    const hint = document.createElement("div");
    hint.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:8px;" +
      "word-break:break-all;white-space:pre-wrap";
    hint.textContent = label;

    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = initial || "";
    inp.style.cssText = "width:100%;box-sizing:border-box";

    body.appendChild(hint);
    body.appendChild(inp);

    const foot = document.createElement("div");
    const sp = document.createElement("span");
    sp.className = "spacer";
    const cancel = document.createElement("button");
    cancel.className = "tbtn";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.className = "tbtn";
    ok.textContent = "确定";
    foot.appendChild(sp);
    foot.appendChild(cancel);
    foot.appendChild(ok);

    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      closeModal();
      resolve(v);
    };
    ok.addEventListener("click", () => finish(inp.value.trim() || null));
    cancel.addEventListener("click", () => finish(null));
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); finish(inp.value.trim() || null); }
      if (ev.key === "Escape") finish(null);
    });

    openModal(title, body, foot);
    setTimeout(() => { inp.focus(); inp.select(); }, 30);
  });
}

/**
 * 设备右键的「adb 终端」：开一个新终端标签，**绑定这台设备**，不弹框。
 *
 * 绑定之后，这个标签里每条 `adb …` 都会自动补 `-s "<序列号>"`
 * （见 90-tools.js 的 termAdbRewrite）—— 车机上 USB 和 TCP 常同时挂着，
 * 不补就是 `more than one device/emulator`。参考实现
 * `D:\workspace\demo\vscode\logcat\logcat\adbterm.js` 是用 adb shim 做的
 * 同一件事（整个终端每条 adb 自动带 -s）；网页终端没有 shell 拦截，
 * 所以在"发送命令之前"改写。
 *
 * 两点如实写在终端里，别让人猜：
 *   · 网页终端没有 TTY，`adb shell` 是一次性的（跑完就退）；交互式 shell
 *     要用 scrcpy 或外部终端；
 *   · 显式写了 `-s 别的设备` 的以用户为准，不覆盖。
 */
function devAdbTerm(serial) {
  const s = String(serial || "").trim();
  if (!s) { toast("先选一个设备", true); return; }
  if (typeof termNewTab !== "function") {
    toast("终端模块没加载，刷新页面再试", true);
    return;
  }
  const tab = termNewTab((typeof S !== "undefined" && S.workspace) || "");
  if (!tab) return;
  tab.adbSerial = s;
  tab.name = "adb · " + s;
  if (typeof renderTermTabs === "function") renderTermTabs();
  if (typeof termWrite === "function") {
    termWrite(tab,
      "[adb 终端] 设备 " + s + "\n"
      + "本标签里每条 adb 都会自动带 -s（自己写了 -s 的以你为准）。\n"
      + "注意：网页终端没有 TTY，adb shell 是一次性命令；"
      + "要交互式 shell 请用 scrcpy 或外部终端。\n");
  }
  const inp = $("termIn");
  if (inp) inp.focus();
}

/** 通用确认框，返回 Promise<boolean>。危险操作（移出版本控制等）用得上。 */

/** 投屏前的确认：把命令摆出来，给「复制命令」和「在这台机器上投屏」两个选择。 */
function scrcpyBox(cmd) {
  return new Promise((resolve) => {
    const body = document.createElement("div");
    body.style.cssText = "font-size:13px;max-width:540px";

    const hint = document.createElement("div");
    hint.style.cssText = "font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:6px";
    hint.textContent = "镜像窗口会开在**跑后端的那台机器**上。工程在远端的话，" +
      "窗口不会出现在你自己的屏幕上 —— 那种情况点「复制命令」，在本机终端里跑。";
    body.appendChild(hint);

    const pre = document.createElement("pre");
    pre.style.cssText = "white-space:pre-wrap;word-break:break-all;background:var(--hover);" +
      "padding:8px;border-radius:4px;margin:0;font-size:12px;user-select:text";
    pre.textContent = cmd || "（算不出命令）";
    body.appendChild(pre);

    const foot = document.createElement("div");
    const sp = document.createElement("span");
    sp.className = "spacer";
    const cancel = document.createElement("button");
    cancel.className = "tbtn";
    cancel.textContent = "取消";
    const copy = document.createElement("button");
    copy.className = "tbtn";
    copy.textContent = "复制命令";
    const go = document.createElement("button");
    go.className = "tbtn";
    go.textContent = "在这台机器上投屏";
    foot.appendChild(sp);
    foot.appendChild(cancel);
    foot.appendChild(copy);
    foot.appendChild(go);

    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      closeModal();
      resolve(v);
    };
    const onKey = (ev) => { if (ev.key === "Escape") finish(false); };
    cancel.addEventListener("click", () => finish(false));
    // 复制走完就关掉 —— 用户要的是命令，不是顺手投一次
    copy.addEventListener("click", () => { copyText(cmd); finish(false); });
    go.addEventListener("click", () => finish(true));
    document.addEventListener("keydown", onKey);

    openModal("投屏（scrcpy）", body, foot);
    setTimeout(() => go.focus(), 30);
  });
}

async function devScrcpy(serial) {
  // 先把命令算出来给用户看一眼
  let cmd = "";
  try {
    const r = await api("/api/plugin/adb_tools/scrcpy",
                        { serial: serial || "", dry_run: true });
    const res = r.result || {};
    if (!res.ok) { toast(res.summary || "投屏不可用", true); }
    cmd = (res.data && res.data.command) || res.summary || "";
  } catch (e) {
    toast("算命令失败：" + e.message, true);
    return;
  }

  if (!(await scrcpyBox(cmd))) return;

  try {
    const r = await api("/api/plugin/adb_tools/scrcpy", { serial: serial || "" });
    const res = r.result || {};
    if (res.ok) toast(res.summary || "投屏已启动");
    else await infoBox("投屏没起来", res.content || res.summary || "失败");
  } catch (e) {
    toast("投屏失败：" + e.message, true);
  }
}

async function devRootRemount(serial) {
  const ok = await confirmBox("adb root + adb remount",
    "给设备提权？改 /system、/vendor 这类分区之前需要。\n\n"
    + "· adbd 会以 root 重启，中间会短暂断连（已自动 wait-for-device）\n"
    + "· user 版固件根本开不了 root，会明确告诉你，不会在那儿干等\n"
    + "· /sdcard 这类路径本来就不用提权，推送时会自动跳过",
    "提权");
  if (!ok) return;

  toast("提权中…");
  try {
    const r = await api("/api/plugin/adb_tools/root_remount", { serial: serial || "" });
    const res = r.result || {};
    if (res.ok) toast(res.summary || "已提权");
    else await infoBox("提权没成功", res.content || res.summary || "失败");
    refreshDevices();
  } catch (e) {
    toast("提权失败：" + e.message, true);
  }
}

/**
 * 固定 TCP/IP 5555（无线 adb）。
 *
 * 一件事分三步：写 persist.adb.tcp.port=5555（重启也固定）→ adb tcpip 5555
 * （adbd 重启成网络模式）→ 按设备自己的 IP 自动 adb connect。
 *
 * 为什么值得单独一个入口：Android 11+ 的「无线调试」每次都是**随机端口**，
 * 换一次就要重新配对一次；这儿固定 5555，之后随时 `adb connect <ip>:5555`
 * 就回来，不用再摸 USB 线。
 */
async function devFixTcpip(serial) {
  serial = serial || $("devSel").value || "";
  if (!serial) { toast("先选一台设备", true); return; }

  const ok = await confirmBox("固定 TCP/IP 5555",
    "把 " + serial + " 的 adbd 切到网络模式，端口固定 5555？\n\n"
    + "· 需要 USB 连着（第一次得靠它切过去）\n"
    + "· adbd 会重启，USB 传输闪断一下，日志流会自动重连\n"
    + "· 顺手写 persist.adb.tcp.port：重启后仍然固定在 5555"
    + "（要 root，写不上不影响本次）\n"
    + "· 切完自动 adb connect <设备IP>:5555",
    "固定 5555");
  if (!ok) return;

  toast("正在切到 TCP 5555…");
  try {
    const r = await api("/api/plugin/adb_tools/tcpip",
                        { serial, port: 5555, persist: true, connect: true });
    const res = r.result || {};
    const d = res.data || {};
    // 有注意项（没连上 / persist 没写进去）就摊开说 —— 只弹一句 toast 的话，
    // 用户会以为"切好了"，拔了线才发现连不上
    if (!res.ok || (d.warnings && d.warnings.length)) {
      await infoBox(res.ok ? "已切到 5555（有注意项）" : "固定 5555 没成功",
                    res.content || res.summary || "失败");
    } else {
      toast(res.summary || "已固定 5555");
    }
    await refreshDevices();
    if (typeof loadLogDevices === "function") await loadLogDevices();
  } catch (e) {
    toast("固定 5555 失败：" + e.message, true);
  }
}

async function devCall(fn, payload, okMsg) {
  try {
    const r = await api("/api/plugin/adb_tools/" + fn, payload);
    const res = r.result || {};
    if (!res.ok) {
      toast(res.summary || res.error || "操作失败", true);
      return null;
    }
    if (okMsg) toast(okMsg);
    return res;
  } catch (e) {
    toast("失败：" + e.message, true);
    return null;
  }
}

async function devNewFolder(serial, dir) {
  const name = await devAsk("新建文件夹", "在 " + dir + " 下面建一个目录", "");
  if (!name) return;
  if (name.indexOf("/") >= 0) { toast("名字里不能有斜杠", true); return; }
  const full = (dir.replace(/\/+$/, "") || "") + "/" + name;
  const res = await devCall("mkdir", { serial, remote: full }, "已建目录 " + name);
  if (res) listDevice(dir);
}

async function devRenameItem(serial, entry, refresh) {
  const dir = entry.full.replace(/\/[^/]*$/, "") || "/";
  const name = await devAsk("重命名 / 移动",
    "原名：" + entry.full + "\n新路径（可以带目录，用来移动）", entry.full);
  if (!name || name === entry.full) return;

  const to = name.charAt(0) === "/" ? name
    : (dir.replace(/\/+$/, "") + "/" + name);

  const res = await devCall("rename", { serial, remote: entry.full, to });
  if (res) {
    toast("已改为 " + baseName(to));
  } else {
    // 目标已存在时再问一次要不要覆盖
    const sure = await devAsk("目标已存在", "要覆盖 " + to + " 吗？\n输入 yes 确认", "");
    if (sure && sure.toLowerCase() === "yes") {
      await devCall("rename", { serial, remote: entry.full, to, overwrite: true },
                    "已覆盖");
    }
  }
  refresh();
}

async function devDeleteItem(serial, entry, refresh) {
  const what = entry.isDir ? "目录（连同里面所有内容）" : "文件";
  const sure = await devAsk("删除确认",
    "要删除这个" + what + "吗？\n" + entry.full + "\n\n输入 yes 确认", "");
  if (!sure || sure.toLowerCase() !== "yes") return;
  const res = await devCall("delete", { serial, remote: entry.full }, "已删除");
  if (res) refresh();
}

function devShowStat(serial, entry) {
  const rows = [
    ["路径", entry.full],
    ["类型", entry.isLink ? ("符号链接 → " + (entry.linkTarget || "?"))
      : entry.isDir ? "目录" : "文件"],
    ["权限", (entry.perms || "?") + (entry.mode ? "   " + entry.mode : "")],
    ["属主", (entry.owner || "?") + ":" + (entry.group || "?")],
    ["大小", fmtSize(entry.size) + "（" + fmtSizeNum(entry.size) + " 字节）"],
    ["修改时间", fmtTime(entry.mtimeMs) || "（解析不出）"],
    ["硬链接", String(entry.links == null ? "?" : entry.links)],
  ];

  const body = document.createElement("div");
  body.style.cssText = "font-size:13px;line-height:2";
  for (const pair of rows) {
    const d = document.createElement("div");
    const k = document.createElement("span");
    k.style.cssText = "color:var(--muted);display:inline-block;width:76px";
    k.textContent = pair[0];
    const v = document.createElement("span");
    v.style.cssText = "word-break:break-all;font-family:Consolas,monospace";
    v.textContent = pair[1];
    d.appendChild(k);
    d.appendChild(v);
    body.appendChild(d);
  }

  const foot = document.createElement("div");
  const sp = document.createElement("span");
  sp.className = "spacer";
  const copy = document.createElement("button");
  copy.className = "tbtn";
  copy.textContent = "复制路径";
  copy.addEventListener("click", () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(entry.full)
        .then(() => toast("已复制"))
        .catch(() => toast("复制失败", true));
    } else {
      toast("这个环境不支持剪贴板", true);
    }
  });
  const close = document.createElement("button");
  close.className = "tbtn";
  close.textContent = "关闭";
  close.addEventListener("click", closeModal);
  foot.appendChild(sp);
  foot.appendChild(copy);
  foot.appendChild(close);

  openModal("属性", body, foot);
}

/** 本地 → 设备。默认拿编辑器里正开着的文件，省得手打路径。 */
async function devPushTo(serial, entry) {
  // 目标：目录就用它本身，文件则覆盖它
  const remote = entry.full;
  const cur = ed();
  const guess = (cur && !cur.remote && cur.path) ? cur.path : "";
  const local = await devAsk("推送到设备",
    "把本地文件推到：\n" + remote + "\n\n本地路径（默认取编辑器里当前打开的文件）：",
    guess);
  if (!local) return;

  const res = await devCall("push", { serial, local, remote },
                            "已推送（权限位会自动还原）");
  if (res) refreshDeviceList();
}

/** 设备 → 本地磁盘。默认落到工作区根目录。 */
async function devPullTo(serial, entry) {
  if (entry.isDir) {
    toast("目录暂不支持拉取，先进去挑文件", true);
    return;
  }
  const name = baseName(entry.full);
  const guess = S.workspace ? (S.workspace.replace(/[\\/]+$/, "") + "/" + name) : name;
  const local = await devAsk("下载到本地",
    "从设备拉取：\n" + entry.full + "\n\n本地保存到（目录会自动创建）：", guess);
  if (!local) return;

  const res = await devCall("pull", { serial, remote: entry.full, local }, "已下载");
  if (res) toast("已保存到 " + local);
}

/** 改权限位。 */
async function devChmod(serial, entry) {
  const cur = (entry.mode || entry.perms || "").replace(/[^0-7]/g, "");
  const mode = await devAsk("改权限位",
    "路径：" + entry.full + "\n当前：" + (entry.perms || "?")
    + (entry.mode ? "（" + entry.mode + "）" : "") + "\n\n新的八进制权限（如 755 / 0644）：",
    cur.length >= 3 ? cur.slice(-3) : "755");
  if (!mode) return;
  if (!/^[0-7]{3,4}$/.test(mode)) { toast("权限只能是三位或四位八进制", true); return; }

  const res = await devCall("chmod", { serial, remote: entry.full, mode },
                            "权限已改为 " + mode);
  if (res) refreshDeviceList();
}

/** 重新列当前设备目录（改完权限 / 推完文件后刷新显示）。 */
function refreshDeviceList() {
  const p = $("devPath").value.trim() || "/";
  listDevice(p);
}

function fmtSizeNum(n) {
  return (Number(n) || 0).toLocaleString("en-US");
}

function devItemMenu(e, serial, entry, refresh) {
  e.preventDefault();
  e.stopPropagation();
  ctx.innerHTML = "";

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = baseName(entry.full);
  ctx.appendChild(info);

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  if (entry.isDir) {
    item("进入", () => listDevice(entry.full));
  } else {
    item("在编辑器打开", () => openRemoteFile(serial, entry.full));
  }
  item("查看属性", () => devShowStat(serial, entry));

  if (!devIsWritable(entry.full)) {
    const warn = document.createElement("div");
    warn.style.cssText = "color:var(--muted);font-size:11px;padding:6px 10px;" +
      "border-top:1px solid var(--border)";
    warn.textContent = "系统分区 · 改动需要 root + remount";
    ctx.appendChild(warn);
  }

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("推送到这里…", () => devPushTo(serial, entry));
  if (!entry.isDir) item("下载到本地…", () => devPullTo(serial, entry));
  item("改权限…", () => devChmod(serial, entry));

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("重命名 / 移动…", () => devRenameItem(serial, entry, refresh));
  item("删除…", () => devDeleteItem(serial, entry, refresh));

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("投屏（这台设备）…", () => devScrcpy(serial));
  item("提权（adb root + remount）", () => devRootRemount(serial));

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  item("复制路径", () => copyText(entry.full));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(e.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(e.clientY, window.innerHeight - r.height - 8) + "px";
}

/** 打开设备上的文件。
 *
 *  两个角色分开：**拉内容**是插件的事（它才知道 adb 怎么用），
 *  **开文件**是编辑器的事（EDITORS / 标签页 / 只读锁都是它的内部状态）。
 *  以前整段写在这里，等于插件反向依赖编辑器实现，编辑器又反过来依赖
 *  `/api/plugin/adb_tools/pull_text` —— 两边互相咬住。
 */
async function openRemoteFile(serial, remote) {
  try {
    const r = await api("/api/plugin/adb_tools/pull_text", { serial, remote });
    const res = r.result || {};
    if (!res.ok) { toast(res.error || "读取失败", true); return; }
    edOpenRemote({
      key: "adb://" + serial + remote,
      name: remote.split("/").pop() || remote,
      text: res.text || "",
      truncated: !!res.truncated,
      side: { serial: serial, path: remote },
      // 写回也归插件：编辑器保存时调它，不关心背后是 adb。
      write: async (text, eol) => {
        const w = await api("/api/plugin/adb_tools/push_text", {
          serial: serial,
          remote: remote,
          text: text,
          eol: eol || "lf",
        });
        return w.result || {};
      },
      note: res.truncated
        ? "设备文件 · 内容被截断，已锁为只读（截断了的内容写回去会把原文件截掉）"
        : "设备文件 · 保存即写回设备",
    });
  } catch (e) {
    toast("读取失败：" + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 以前这张表在内核 50-views.js 里，内核得认识每个插件的 initXxxView。
// 前端资源搬进插件目录后，这件事也该由插件自己做。
//
// 时机：syncPluginViews() 先 await 加载本文件、再建活动栏按钮，
// 所以这里注册的 setter 一定赶得上第一次切视图。
viewRegister({
  view: "device",
  setters: () => initDeviceView(),
});
