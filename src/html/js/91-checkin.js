// ---------------------------------------------------------------------------
// 打卡提醒
// ---------------------------------------------------------------------------
// 一个助理一份清单，存在服务端 ~/.web_ai/checkin-<id>.json。
// 时间是"本地时区的 HH:MM"，days 是 Mon..Sun —— 到点由服务进程后台线程直发微信。
//
// 为什么界面是"整份保存"而不是逐条即时写入：
// 这张表通常就几行，用户习惯一口气改完再点保存；逐条保存反而会出现
// 改到一半被刷新打断、留下半截配置的情况。
async function openCheckin(botId) {
  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:10px;min-height:320px";

  const head = document.createElement("div");
  head.style.cssText = "font-size:12px;color:var(--muted);line-height:1.6";
  head.innerHTML =
    "到点会直接发一条微信到这个助理绑定的微信号（不经过模型）。<br>" +
    "助理没连上、或者还没人给它发过消息，提醒就发不出去 —— 先点「发测试消息」验证一次。";
  body.appendChild(head);

  const list = document.createElement("div");
  list.style.cssText = "display:flex;flex-direction:column;gap:6px;overflow:auto;flex:1";
  body.appendChild(list);

  const statusLine = document.createElement("div");
  statusLine.style.cssText = "font-size:12px;color:var(--muted)";

  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";

  const addBtn = document.createElement("button");
  addBtn.className = "tbtn";
  addBtn.textContent = "＋ 添加一条";

  const testBtn = document.createElement("button");
  testBtn.className = "tbtn";
  testBtn.textContent = "发测试消息";
  testBtn.title = "立刻发一条到微信，确认通道是否正常";

  const saveBtn = document.createElement("button");
  saveBtn.className = "tbtn primary";
  saveBtn.textContent = "保存";

  foot.appendChild(addBtn);
  foot.appendChild(testBtn);
  foot.appendChild(saveBtn);
  foot.appendChild(statusLine);

  openModal("打卡提醒", body, foot);

  let meta = { days: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], entries: [] };
  let rows = [];

  const DAY_CN = { Mon: "一", Tue: "二", Wed: "三", Thu: "四", Fri: "五", Sat: "六", Sun: "日" };

  function makeRow(e) {
    const row = document.createElement("div");
    row.style.cssText =
      "display:flex;gap:6px;align-items:center;padding:6px;border:1px solid var(--line);border-radius:6px;flex-wrap:wrap";

    const en = document.createElement("input");
    en.type = "checkbox";
    en.checked = e.enabled !== false;
    en.title = "启用/停用这一条";

    const tm = document.createElement("input");
    tm.type = "time";
    tm.value = e.time || "08:28";
    // 时间框默认字号极小、灰色偏淡，肉眼很难看清；这里显式放大并提高对比度。
    // 不同浏览器对 time 控件内部伪元素的字号处理不一致，必须连 ::-webkit-datetime-edit
    // 一起覆盖，否则 Chrome 里数字还是小的。
    tm.style.cssText =
      "width:104px;font-size:15px;font-weight:600;letter-spacing:.5px;" +
      "color:var(--fg,#e6e6e6);background:var(--bg2,#2a2a2a);" +
      "border:1px solid var(--line,#444);border-radius:6px;padding:4px 6px;" +
      "font-variant-numeric:tabular-nums";

    const days = document.createElement("span");
    days.style.cssText = "display:flex;gap:2px";
    const dayBoxes = {};
    for (const d of meta.days) {
      const lb = document.createElement("label");
      lb.style.cssText =
        "display:flex;align-items:center;gap:2px;font-size:14px;cursor:pointer;padding:2px 1px";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = (e.days || []).includes(d);
      dayBoxes[d] = cb;
      lb.appendChild(cb);
      lb.appendChild(document.createTextNode(DAY_CN[d] || d));
      days.appendChild(lb);
    }

    const tx = document.createElement("input");
    tx.type = "text";
    tx.value = e.text || "";
    tx.placeholder = "提醒内容";
    tx.style.cssText =
      "flex:1;min-width:160px;font-size:14px;color:var(--fg,#e6e6e6);" +
      "background:var(--bg2,#2a2a2a);border:1px solid var(--line,#444);" +
      "border-radius:6px;padding:4px 8px";

    const del = document.createElement("button");
    del.className = "tbtn mini";
    del.textContent = "删";
    del.title = "删掉这一条（保存后生效）";

    row.appendChild(en);
    row.appendChild(tm);
    row.appendChild(days);
    row.appendChild(tx);
    row.appendChild(del);
    list.appendChild(row);

    const rec = { row, en, tm, dayBoxes, tx };
    del.addEventListener("click", () => {
      row.remove();
      rows = rows.filter((r) => r !== rec);
    });
    return rec;
  }

  function collect() {
    return rows.map((r) => ({
      time: r.tm.value,
      days: meta.days.filter((d) => r.dayBoxes[d].checked),
      text: r.tx.value.trim(),
      enabled: r.en.checked,
    }));
  }

  function render(entries) {
    list.innerHTML = "";
    rows = [];
    for (const e of entries) rows.push(makeRow(e));
    ensureEmptyHint();
  }

  // 空清单时放一句提示；它本身不是一行数据，添加第一条时必须先清掉，
  // 否则提示文字会一直粘在列表最上面（之前"加不了多条"就是这行没清干净）。
  let emptyHint = null;
  function ensureEmptyHint() {
    if (emptyHint) {
      emptyHint.remove();
      emptyHint = null;
    }
    if (!rows.length) {
      emptyHint = document.createElement("div");
      emptyHint.style.cssText = "font-size:12px;color:var(--muted);padding:8px";
      emptyHint.textContent = "还没有提醒，点下面的「＋ 添加一条」。";
      list.appendChild(emptyHint);
    }
  }

  addBtn.addEventListener("click", () => {
    rows.push(makeRow({ time: "08:28", days: ["Mon", "Tue", "Wed", "Thu", "Fri"], text: "打卡提醒：记得打卡", enabled: true }));
    ensureEmptyHint();
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const r = await api("/api/checkin/save", { id: botId, entries: collect() });
      meta.entries = r.entries;
      render(r.entries);
      statusLine.textContent = "已保存 " + r.entries.length + " 条" +
        (r.dropped ? "（" + r.dropped + " 条信息不全已忽略）" : "");
    } catch (e) {
      toast("保存失败：" + e.message, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    try {
      await api("/api/checkin/test", { id: botId });
      toast("测试消息已发出，看看手机微信");
    } catch (e) {
      toast("发送失败：" + e.message, true);
    } finally {
      testBtn.disabled = false;
    }
  });

  try {
    const st = await api("/api/checkin?id=" + encodeURIComponent(botId));
    meta = st;
    render(st.entries || []);
    statusLine.textContent = st.last_fire_at
      ? "上次提醒：" + st.last_fire_at + (st.last_error ? "　⚠ " + st.last_error : "")
      : (st.last_error ? "⚠ " + st.last_error : "还没发过提醒");
  } catch (e) {
    toast("读取打卡设置失败：" + e.message, true);
  }
}
