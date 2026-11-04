/* 助理记忆面板
 *
 * 为什么单独一个文件而不是塞进 90-tools.js：
 * 那个文件已经 1000 多行，助理面板本身逻辑也密。记忆面板是独立的小功能，
 * 拆出来以后要改也只用看这一块。加载顺序上 92 排在 90/91 之后、
 * 99-boot 之前，正好能用到前面定义的全局函数（openModal / api / toast）。
 *
 * 这里展示三样东西：
 *   1. 记忆正文 —— 可以手改，改完立刻对该助理的下一句话生效
 *   2. 能力手册 —— 只读。写的是「你有什么能力、工具怎么调」，
 *      就是模型看到的那份，放在这里方便用户知道助理到底能干什么
 *   3. 工具清单 —— 同样只读，让用户有据可查
 */
function openWechatMemory(botId) {
  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:10px;min-width:min(680px,86vw)";

  const tip = document.createElement("div");
  tip.style.cssText = "font-size:13px;line-height:1.6;color:var(--muted,#8a8f98)";
  tip.textContent =
    "这份记忆只属于当前这个助理，跟工作区记忆是两回事。助理每轮对话都会读到它，" +
    "它也能自己往里记（工具 remember）。改完点保存，下一句就生效。";
  body.appendChild(tip);

  const meta = document.createElement("div");
  meta.style.cssText = "font-size:13px;color:var(--muted,#8a8f98)";
  meta.textContent = "加载中…";
  body.appendChild(meta);

  const ta = document.createElement("textarea");
  ta.spellcheck = false;
  ta.style.cssText =
    "width:100%;height:260px;box-sizing:border-box;padding:10px;resize:vertical;" +
    "font-family:ui-monospace,Consolas,monospace;font-size:14px;line-height:1.6;" +
    "background:#17191d;color:#e6e8eb;border:1px solid #333840;border-radius:6px";
  body.appendChild(ta);

  // 能力手册：折叠起来默认不占地方，需要时展开看
  const det = document.createElement("details");
  const sum = document.createElement("summary");
  sum.textContent = "📖 能力手册（只读，助理看到的就是这份）";
  sum.style.cssText = "cursor:pointer;font-size:13px;color:#9aa4b2;padding:2px 0";
  det.appendChild(sum);
  const pre = document.createElement("pre");
  pre.style.cssText =
    "white-space:pre-wrap;word-break:break-word;margin:6px 0 0;padding:10px;" +
    "background:#14161a;border:1px solid #2b3038;border-radius:6px;" +
    "font-size:12.5px;line-height:1.6;color:#b8c0cc;max-height:320px;overflow:auto";
  pre.textContent = "";
  det.appendChild(pre);
  body.appendChild(det);

  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;gap:8px;justify-content:flex-end";

  const tplBtn = document.createElement("button");
  tplBtn.className = "tbtn";
  tplBtn.textContent = "填入模板";
  tplBtn.title = "把默认骨架填进输入框（会覆盖当前内容，但不会自动保存）";

  const resetBtn = document.createElement("button");
  resetBtn.className = "tbtn";
  resetBtn.textContent = "清空";
  resetBtn.title = "清空输入框内容（同样不会自动保存）";

  const saveBtn = document.createElement("button");
  saveBtn.className = "tbtn primary";
  saveBtn.textContent = "保存";

  foot.appendChild(tplBtn);
  foot.appendChild(resetBtn);
  foot.appendChild(saveBtn);

  openModal("助理记忆", body, foot);

  let template = "";

  function paint(data) {
    if (template === "") template = data.template || "";
    const when = data.updated ? `，最后修改 ${data.updated}` : "";
    if (data.exists) {
      meta.textContent = `已保存 ${data.chars} 字符（上限 ${data.limit}）${when}`;
    } else {
      // 「没写过」和「写了空模板」要分清楚：后者会让看板显示假的已有状态
      meta.textContent = `还没有记忆（上限 ${data.limit} 字符）`;
    }
    ta.value = data.text || "";
    pre.textContent = data.guide || "（没有手册）";
    if (data.tools && data.tools.length) {
      sum.textContent = `📖 能力手册（只读，助理看到的就是这份）· 工具：${data.tools.join(" / ")}`;
    }
  }

  async function load() {
    try {
      const data = await api("/api/wechat/memory?id=" + encodeURIComponent(botId || ""));
      paint(data);
    } catch (e) {
      meta.textContent = "读取失败：" + (e && e.message ? e.message : e);
    }
  }

  tplBtn.addEventListener("click", () => {
    ta.value = template || "";
    ta.focus();
  });

  resetBtn.addEventListener("click", () => {
    ta.value = "";
    ta.focus();
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    const old = saveBtn.textContent;
    saveBtn.textContent = "保存中…";
    try {
      const data = await api("/api/wechat/memory", { id: botId || "", text: ta.value }, {});
      paint(data);
      if (typeof toast === "function") toast("已保存");
    } catch (e) {
      meta.textContent = "保存失败：" + (e && e.message ? e.message : e);
      if (typeof toast === "function") toast("保存失败");
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = old;
    }
  });

  load();
}
