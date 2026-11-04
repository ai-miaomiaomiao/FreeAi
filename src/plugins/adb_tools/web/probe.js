// 操作时间线采集 —— 界面侧的「结束采集」按钮。
//
// 流程：AI 调 probe_start → 这里轮询发现 running=true → 输入框上方冒出
// 一条横幅（带计时和条数）→ 用户操作完（设备 / 模拟器 / 浏览器都算）→
// 点「结束采集」→ 停采集，顺手把「请分析时间线」递给 AI。
//
// 为什么按钮要做在界面上：采集是**等用户操作**的，只有用户知道什么时候
// 操作完了。让用户再打一句「好了」也行，但那句话本身没有信息量。
//
// 主窗口和任务台（detached=board）都挂一份：两边的输入区是两套 DOM，
// 用户可能在任意一边看着采集跑，按钮就得在任意一边都能点。
(function () {
  "use strict";

  if (typeof api !== "function") return;

  const IN_BOARD = (typeof DETACHED_BOARD !== "undefined") && DETACHED_BOARD;
  const POLL_MS = 2000;
  let bar = null;        // 横幅 DOM
  let timerText = null;  // 计时文字
  let busy = false;

  /** 横幅插在哪个容器里：主窗口是输入区，任务台是 dt-composer。 */
  function hostEl() {
    return document.querySelector(IN_BOARD ? ".dt-composer" : ".composer-inner");
  }

  function inputEl() {
    return document.getElementById(IN_BOARD ? "dtInput" : "input");
  }

  function sendNow() {
    if (IN_BOARD) {
      if (typeof dtSendChat === "function") dtSendChat();
    } else if (typeof send === "function") {
      send();
    }
  }

  /** 建（或复用）横幅。 */
  function ensureBar() {
    if (bar && bar.parentNode) return bar;
    const host = hostEl();
    if (!host) return null;

    bar = document.createElement("div");
    bar.id = "probeBar";
    bar.style.cssText =
      "display:flex;align-items:center;gap:8px;margin:0 0 6px;" +
      "padding:6px 10px;border:1px solid var(--border);border-radius:8px;" +
      "background:rgba(61,158,255,.08);font-size:12px;line-height:1.5";

    const dot = document.createElement("span");
    dot.textContent = "●";
    dot.style.cssText = "color:#3d9eff";

    timerText = document.createElement("span");
    timerText.style.cssText = "color:var(--fg);font-family:var(--mono)";

    const hint = document.createElement("span");
    hint.textContent = "采集中，操作完点右边";
    hint.style.cssText = "color:var(--muted)";

    const spacer = document.createElement("span");
    spacer.style.flex = "1";

    const stop = document.createElement("button");
    stop.className = "tbtn mini";
    stop.textContent = "结束采集";
    stop.addEventListener("click", stopAndAnalyze);

    bar.appendChild(dot);
    bar.appendChild(timerText);
    bar.appendChild(hint);
    bar.appendChild(spacer);
    bar.appendChild(stop);

    // 主窗口插在 attn / sendQueue 之前；任务台插在 .dt-attn 之后（原来就在最上）
    const attn = host.querySelector(".dt-attn");
    if (IN_BOARD && attn && attn.parentNode === host) {
      attn.insertAdjacentElement("afterend", bar);
    } else {
      host.insertBefore(bar, host.firstChild);
    }
    return bar;
  }

  function hideBar() {
    if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
    bar = null;
    timerText = null;
  }

  function fmtDur(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":"
      + String(s % 60).padStart(2, "0");
  }

  /** 用户点「结束采集」：停掉，然后让 AI 接着分析。 */
  async function stopAndAnalyze() {
    if (busy) return;
    busy = true;
    let summary = "已停止采集";
    let secs = 30;
    try {
      const r = await api("/api/plugin/adb_tools/probe_stop", {});
      const res = (r && r.result) || {};
      summary = res.summary || summary;
      const d = res.data || {};
      if (d.elapsed) secs = Math.max(5, Math.min(300, Math.round(d.elapsed) + 3));
    } catch (e) {
      toast("结束采集失败：" + e.message, true);
      busy = false;
      return;
    }
    hideBar();
    busy = false;
    toast(summary);

    // 交给 AI 去分析。输入框有草稿时**不覆盖** —— 那是用户正在写的东西，
    // 追加一句提示让他自己按回车，比擅自发出去礼貌。
    const ip = inputEl();
    if (!ip) return;
    const ask = "【采集已结束】请用 adb({action:\"timeline\", seconds:"
      + secs + "}) 拉取最近的时间线（包含设备界面和电脑前台窗口），"
      + "结合 logcat 一起分析刚才发生了什么。";
    if (ip.value.trim()) {
      ip.value = ip.value.replace(/\s*$/, "") + "\n" + ask;
      toast("已把「分析时间线」写进输入框，按 Enter 发送");
      return;
    }
    ip.value = ask;
    sendNow();
  }

  async function tick() {
    // 页面在后台时不发请求：用户切走了，横幅也没人看
    if (document.hidden) return;
    let data;
    try {
      const r = await api("/api/plugin/adb_tools/probe_status", {});
      data = ((r && r.result) || {}).data || {};
    } catch (e) {
      return;                   // 插件被禁用 / 接口 404：静默，不刷屏
    }

    if (data.running) {
      ensureBar();
      if (timerText) {
        timerText.textContent = fmtDur(data.elapsed) + " · "
          + (data.count || 0) + " 条";
      }
    } else if (bar) {
      hideBar();                // 到点自动停 / 别的窗口点了结束
    }
  }

  setInterval(tick, POLL_MS);
  // 页面从后台回来时立刻对一次，别等两秒
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });
  // 任务台是异步建 DOM 的（dtBuild 要等会话列表到位），先对一次
  setTimeout(tick, 1200);
})();
