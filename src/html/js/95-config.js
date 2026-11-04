// IDE 配置：模型 / 自定义网关 / 命令放行
//
// 入口在设置菜单（见 99-boot.js 的 openSettingsMenu）。为什么做成弹窗、
// 不给活动栏加图标：这三块都是"改完就走"的低频设置，加起来十几个字段，
// 长期占一格图标不值当。
//
// 本文件**不碰任何 $("id")**：弹窗容器是页面里已有的 #modalBody，内容
// 全是现建的。tools/check_frontend.js 会校验每个 $("x") 里的 id 在
// index.html 里存在 —— 这里需要的输入框页面上一个都没有，用了就会报错。
"use strict";

// 输入框 / 文本域的统一样式。用 CSS 变量而不是写死颜色：
// 深色主题下写死浅底会变成"白框里看不清字"（插件页那两个白方块就是这么来的）。
const IDECFG_IN_CSS = "width:100%;box-sizing:border-box;padding:5px 7px;" +
  "border-radius:6px;border:1px solid var(--border);background:var(--bg);" +
  "color:var(--text);font-size:12px;font-family:Consolas,monospace;outline:none";

function ideCfgErr(e) {
  return (e && e.message) ? e.message : String(e || "未知错误");
}

/** 一个输入控件（input / password / textarea）。 */
function ideCfgInput(value, kind, placeholder) {
  const area = (kind === "textarea");
  const el = document.createElement(area ? "textarea" : "input");
  if (area) {
    el.rows = 3;
    el.style.cssText = IDECFG_IN_CSS + ";resize:vertical;line-height:1.5";
  } else {
    el.type = kind || "text";
    el.style.cssText = IDECFG_IN_CSS;
  }
  el.value = (value == null) ? "" : String(value);
  if (placeholder) el.placeholder = placeholder;
  return el;
}

/** 一行：标签（+ 灰色说明）+ 控件。 */
function ideCfgField(label, hint, el) {
  const row = document.createElement("div");
  row.style.cssText = "margin-bottom:10px";
  const lab = document.createElement("div");
  lab.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:3px";
  lab.textContent = label;
  if (hint) {
    const h = document.createElement("span");
    h.style.cssText = "opacity:.82";
    h.textContent = "　" + hint;
    lab.appendChild(h);
  }
  row.appendChild(lab);
  row.appendChild(el);
  return row;
}

/** 小节：标题 + 一句说明 + 容器（返回容器，往里塞东西）。 */
function ideCfgSection(title, sub) {
  const box = document.createElement("div");
  box.style.cssText = "margin-bottom:16px;padding-bottom:14px;" +
    "border-bottom:1px solid var(--border)";
  const h = document.createElement("div");
  h.style.cssText = "font-weight:600;font-size:13px;margin-bottom:4px";
  h.textContent = title;
  box.appendChild(h);
  if (sub) {
    const s = document.createElement("div");
    s.style.cssText = "font-size:11px;color:var(--muted);margin-bottom:10px;" +
      "line-height:1.7;white-space:pre-line";
    s.textContent = sub;
    box.appendChild(s);
  }
  return box;
}

function ideCfgBtn(text, onClick, danger) {
  const b = document.createElement("button");
  b.className = "tbtn mini";
  if (danger) b.style.cssText = "color:var(--danger);border-color:var(--danger)";
  b.textContent = text;
  b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    if (b.disabled) return;
    b.disabled = true;
    try { await onClick(); } catch (e) { toast(ideCfgErr(e), true); }
    b.disabled = false;
  });
  return b;
}

function ideCfgSmall(text, css) {
  const d = document.createElement("span");
  d.style.cssText = "font-size:11px;color:var(--muted);" + (css || "");
  d.textContent = text;
  return d;
}

// ---------------------------------------------------------------------------
// 弹窗
// ---------------------------------------------------------------------------
async function openConfig() {
  let cfg = null;
  try {
    cfg = await api("/api/config");
  } catch (e) {
    toast("读取配置失败：" + ideCfgErr(e), true);
    return;
  }

  // 放行清单读不到（工作区没登记 / 老后端）不该连模型配置一起挡掉
  let allowData = { allow: [], rules: { ask: [], block: [] }, workspace: "", path: "" };
  let allowErr = "";
  try {
    allowData = await api("/api/confirm/allow");
  } catch (e) {
    allowErr = ideCfgErr(e);
  }

  // 表单草稿：保存时只读这里，不去 DOM 里逐个捞（省掉一堆 querySelector）
  const draft = {
    base_url: cfg.default_base_url || cfg.base_url || "",
    api_key: "",
    clear_api_key: false,
    model: cfg.model || "",
    models: (cfg.models || []).join("\n"),
    reasoning_effort: cfg.reasoning_effort || "",
    context_tokens: (cfg.context_tokens == null) ? "" : cfg.context_tokens,
    // 输出上限（max_tokens）的**全局兜底**。按模型配的那份（model_output）
    // 在聊天区右键压缩按钮的菜单里改，这里不铺表格 —— 大多数人只调一个数。
    max_tokens: (cfg.max_tokens == null) ? "" : cfg.max_tokens,
    endpoints: (cfg.endpoints_edit || []).map((ep) => ({
      id: ep.id || "",
      name: ep.name || "",
      base_url: ep.base_url || "",
      models: (ep.models || []).join("\n"),
      key_mode: ep.key_mode || "inherit",
      api_key: "",
    })),
  };

  const body = document.createElement("div");
  body.style.cssText = "max-height:62vh;overflow:auto;padding-right:4px";

  // ---- ① 模型 / 网关 ----
  const s1 = ideCfgSection("模型与网关",
    "全局设置，跟工作区无关。改完保存即生效（下一次请求就走新配置），不用重启服务。");
  const inBase = ideCfgInput(draft.base_url, "text",
    "https://host/v1 或完整的 /chat/completions");
  inBase.addEventListener("input", () => { draft.base_url = inBase.value; });
  s1.appendChild(ideCfgField("网关地址（默认网关）", "自定义网关在下面单独配", inBase));

  const inKey = ideCfgInput("", "password",
    cfg.has_api_key ? "已配置，留空 = 不修改" : "还没配置");
  inKey.addEventListener("input", () => { draft.api_key = inKey.value; });
  s1.appendChild(ideCfgField("API Key", "留空 = 不改；要清空得勾下面那个", inKey));

  const clearRow = document.createElement("div");
  clearRow.style.cssText = "margin:-4px 0 10px;font-size:11px;color:var(--muted)";
  const chkClear = document.createElement("input");
  chkClear.type = "checkbox";
  chkClear.style.cssText = "vertical-align:-1px;margin-right:5px";
  chkClear.addEventListener("change", () => { draft.clear_api_key = chkClear.checked; });
  clearRow.appendChild(chkClear);
  clearRow.appendChild(ideCfgSmall("清除已保存的 API Key（勾上并保存才会真删）"));
  s1.appendChild(clearRow);

  const inModel = ideCfgInput(draft.model, "text", "deepseek-v4-flash");
  inModel.addEventListener("input", () => { draft.model = inModel.value; });
  s1.appendChild(ideCfgField("默认模型", "", inModel));

  const inModels = ideCfgInput(draft.models, "textarea", "一行一个模型名");
  inModels.addEventListener("input", () => { draft.models = inModels.value; });
  s1.appendChild(ideCfgField("可选模型清单", "一行一个，显示在顶栏下拉里", inModels));

  const inEffort = ideCfgInput(draft.reasoning_effort, "text", "留空 = 不传这个参数");
  inEffort.addEventListener("input", () => { draft.reasoning_effort = inEffort.value; });
  s1.appendChild(ideCfgField("reasoning_effort", "部分网关认这个字段", inEffort));

  const inCtx = ideCfgInput(draft.context_tokens, "text", "留空 = 用内置默认（支持 1M / 200K）");
  inCtx.addEventListener("input", () => { draft.context_tokens = inCtx.value; });
  s1.appendChild(ideCfgField("上下文窗口上限", "", inCtx));

  const inOut = ideCfgInput(draft.max_tokens, "text", "留空 = 内置 8192（支持 8K / 16384）");
  inOut.addEventListener("input", () => { draft.max_tokens = inOut.value; });
  s1.appendChild(ideCfgField("单次输出上限",
    "留空 = 用内置 8192。超了长回答会被切成半句；按模型微调在聊天区右键压缩按钮里", inOut));
  body.appendChild(s1);

  // ---- ② 自定义网关 ----
  const s2 = ideCfgSection("自定义网关",
    "一个网关 = 一套地址 + 它自己的模型名。模型名属于某个网关，切模型会连网关一起切。\n" +
    "密钥三态：「沿用全局 key」/「自带 key」/「不带 key」（本地自建网关常常不校验）。");
  const epBox = document.createElement("div");
  s2.appendChild(epBox);

  const renderEndpoints = () => {
    epBox.innerHTML = "";
    if (!draft.endpoints.length) {
      epBox.appendChild(ideCfgSmall("还没有自定义网关。"));
    }
    draft.endpoints.forEach((ep, idx) => {
      const card = document.createElement("div");
      card.style.cssText = "border:1px solid var(--border);border-radius:8px;" +
        "padding:9px 10px;margin-bottom:8px";

      const head = document.createElement("div");
      head.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:7px";
      const tag = document.createElement("span");
      tag.style.cssText = "font-size:12px;font-weight:500";
      tag.textContent = ep.name || ep.id || ("网关 " + (idx + 1));
      head.appendChild(tag);
      head.appendChild(ideCfgSmall("id: " + (ep.id || "（保存时按名字生成）")));
      const sp = document.createElement("span");
      sp.style.flex = "1";
      head.appendChild(sp);
      head.appendChild(ideCfgBtn("删除", () => {
        draft.endpoints.splice(idx, 1);
        renderEndpoints();
      }, true));
      card.appendChild(head);

      const inName = ideCfgInput(ep.name, "text", "下拉里的分组名");
      inName.addEventListener("input", () => { ep.name = inName.value; });
      card.appendChild(ideCfgField("名称", "", inName));

      const inUrl = ideCfgInput(ep.base_url, "text", "http://host:8086/v1");
      inUrl.addEventListener("input", () => { ep.base_url = inUrl.value; });
      card.appendChild(ideCfgField("地址", "", inUrl));

      const inEpsModels = ideCfgInput(ep.models, "textarea", "一行一个");
      inEpsModels.addEventListener("input", () => { ep.models = inEpsModels.value; });
      card.appendChild(ideCfgField("模型清单", "", inEpsModels));

      const sel = document.createElement("select");
      sel.style.cssText = IDECFG_IN_CSS + ";font-family:inherit";
      for (const [val, text] of [["inherit", "沿用全局 key"],
                                 ["own", "自带 key"],
                                 ["none", "不带 key"]]) {
        const o = document.createElement("option");
        o.value = val;
        o.textContent = text;
        if (val === ep.key_mode) o.selected = true;
        sel.appendChild(o);
      }
      card.appendChild(ideCfgField("密钥", "", sel));

      const inEpKey = ideCfgInput("", "password",
        ep.key_mode === "own" ? "已配置，留空 = 保持原 key" : "选了「自带 key」才能填");
      inEpKey.disabled = (ep.key_mode !== "own");
      inEpKey.addEventListener("input", () => { ep.api_key = inEpKey.value; });
      sel.addEventListener("change", () => {
        ep.key_mode = sel.value;
        ep.api_key = "";                       // 换模式就别把旧输入带过去
        inEpKey.value = "";
        inEpKey.disabled = (ep.key_mode !== "own");
        inEpKey.placeholder = (ep.key_mode === "own")
          ? "留空 = 保持原 key" : "选了「自带 key」才能填";
      });
      card.appendChild(ideCfgField("该网关的 API Key", "留空 = 保持原来的", inEpKey));
      epBox.appendChild(card);
    });
    const addBtn = ideCfgBtn("＋ 新增网关", () => {
      draft.endpoints.push({ id: "", name: "", base_url: "", models: "",
                             key_mode: "inherit", api_key: "" });
      renderEndpoints();
    });
    epBox.appendChild(addBtn);
  };
  renderEndpoints();
  body.appendChild(s2);

  // ---- ③ 命令放行 ----
  const s3 = ideCfgSection("命令放行（永久）",
    "按**工作区**存：在这里放行的命令只对这个工作区生效。\n" +
    "改动立即写盘、立即生效，不用点下面的保存。");
  if (allowErr) {
    const err = document.createElement("div");
    err.style.cssText = "font-size:12px;color:var(--danger)";
    err.textContent = "读不到放行清单：" + allowErr;
    s3.appendChild(err);
  } else {
    if (allowData.workspace) {
      s3.appendChild(ideCfgSmall("当前工作区：" + allowData.workspace, "display:block"));
    }
    if (allowData.path) {
      s3.appendChild(ideCfgSmall("清单文件：" + allowData.path, "display:block;margin-bottom:8px"));
    }
    const allowBox = document.createElement("div");
    s3.appendChild(allowBox);

    const applyAllow = async (payload) => {
      const r = await api("/api/confirm/allow", payload);
      allowData = r;
      renderAllow();
      toast("放行清单已更新");
    };

    const renderAllow = () => {
      allowBox.innerHTML = "";
      const now = allowData.allow || [];
      const askRules = (allowData.rules && allowData.rules.ask) || [];

      // 已放行的
      const h1 = document.createElement("div");
      h1.style.cssText = "font-size:12px;margin:2px 0 5px";
      h1.textContent = "已放行（" + now.length + " 项）";
      allowBox.appendChild(h1);
      if (!now.length) {
        allowBox.appendChild(ideCfgSmall("暂无。下面的列表里点「加入」即可。"));
      }
      for (const why of now) {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:8px;" +
          "font-size:12px;padding:3px 0";
        const t = document.createElement("span");
        t.style.flex = "1";
        t.textContent = "✔ " + why;
        row.appendChild(t);
        row.appendChild(ideCfgBtn("移除", () => applyAllow({ remove: [why] })));
        allowBox.appendChild(row);
      }
      if (now.length) {
        const clr = ideCfgBtn("清空全部放行", () => applyAllow({ clear: true }), true);
        allowBox.appendChild(clr);
      }

      // 已知定级理由：还没放行的可以一键加入
      const rest = askRules.filter((r) => now.indexOf(r.why) < 0);
      const h2 = document.createElement("div");
      h2.style.cssText = "font-size:12px;margin:12px 0 5px";
      h2.textContent = "已知的可放行类型（" + askRules.length + " 条，未放行 " + rest.length + "）";
      allowBox.appendChild(h2);
      for (const r of rest) {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:8px;" +
          "font-size:12px;padding:3px 0";
        const t = document.createElement("span");
        t.style.flex = "1";
        t.textContent = r.why;
        t.title = r.pattern || "";
        row.appendChild(t);
        row.appendChild(ideCfgBtn("加入", () => applyAllow({ add: [r.why] })));
        allowBox.appendChild(row);
      }

      // 绝对禁止：只读，不给按钮
      const blocks = (allowData.rules && allowData.rules.block) || [];
      const h3 = document.createElement("div");
      h3.style.cssText = "font-size:12px;margin:14px 0 5px";
      h3.textContent = "绝对禁止（" + blocks.length + " 条，不可关闭）";
      allowBox.appendChild(h3);
      allowBox.appendChild(ideCfgSmall(
        "这些命令直接拒绝，放行清单对它们无效 —— 不可逆操作没有「确认」的意义。"));
      const pre = document.createElement("div");
      pre.style.cssText = "font-family:Consolas,monospace;font-size:11px;" +
        "color:var(--muted);line-height:1.6;margin-top:5px;white-space:pre-wrap";
      pre.textContent = blocks.map((b) => "✕ " + b.pattern).join("\n");
      allowBox.appendChild(pre);
    };
    renderAllow();
  }
  body.appendChild(s3);

  // ---- 底部：保存 ----
  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;align-items:center;gap:10px";
  const saveBtn = document.createElement("button");
  saveBtn.className = "tbtn";
  saveBtn.textContent = "保存";
  const status = document.createElement("span");
  status.style.cssText = "font-size:11px;color:var(--muted);flex:1";
  status.textContent = "保存模型 / 网关设置；命令放行是即时生效的。";

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    status.style.color = "var(--muted)";
    status.textContent = "保存中…";
    const payload = {
      base_url: draft.base_url,
      model: draft.model,
      models: draft.models,
      reasoning_effort: draft.reasoning_effort,
      context_tokens: draft.context_tokens,
      max_tokens: draft.max_tokens,
      clear_api_key: draft.clear_api_key,
      endpoints: draft.endpoints.map((ep) => ({
        id: ep.id, name: ep.name, base_url: ep.base_url,
        models: ep.models, key_mode: ep.key_mode, api_key: ep.api_key || "",
      })),
    };
    // 只有真填了才发 api_key：发空串会被后端当成"清除"（留空 = 不改）
    if (draft.api_key) payload.api_key = draft.api_key;
    try {
      const r = await api("/api/config", payload);
      // 顶栏下拉跟着重画：模型清单 / 自定义网关都可能变了。
      // 重新拉一次 /api/config 而不是拿响应拼 —— S.endpoints 要的是脱敏版
      // （for_public），而保存响应里给的是可编辑版（for_edit）。
      try {
        const fresh = await api("/api/config");
        S.model = fresh.model || S.model;
        S.models = fresh.models || S.models;
        S.endpoints = fresh.endpoints || S.endpoints;
        S.endpoint = fresh.endpoint || "";
        if (typeof renderModels === "function") renderModels();
        if (typeof dtRenderModel === "function") dtRenderModel();
      } catch (e) { /* 下拉没刷新不影响已生效的配置 */ }

      // 用后端洗过的端点表回写草稿：新增的网关在保存前 id 是空的，
      // 不回写的话"再点一次保存"会按空 id 找不到旧记录、把 key 弄丢。
      // 顺便把 base_url 归一化后的样子（补 /chat/completions）显示出来。
      if (Array.isArray(r.endpoints)) {
        draft.endpoints = r.endpoints.map((ep) => ({
          id: ep.id || "",
          name: ep.name || "",
          base_url: ep.base_url || "",
          models: (ep.models || []).join("\n"),
          key_mode: ep.key_mode || "inherit",
          api_key: "",
        }));
        renderEndpoints();
      }

      const warns = r.warnings || [];
      if (warns.length) {
        status.style.color = "var(--danger)";
        status.textContent = "已写入文件，但 " + warns.join("；");
      } else {
        status.style.color = "var(--ok)";
        status.textContent = "已保存（" + (r.changed || []).join(", ") +
          "），下一次请求生效";
      }
      toast(warns.length ? warns.join("；") : "配置已保存", warns.length > 0);
      draft.api_key = "";          // 保存过的 key 不再留在内存里
    } catch (e) {
      status.style.color = "var(--danger)";
      status.textContent = "保存失败：" + ideCfgErr(e);
    }
    saveBtn.disabled = false;
  });

  foot.appendChild(saveBtn);
  foot.appendChild(status);
  foot.appendChild(ideCfgBtn("关闭", () => closeModal()));

  openModal("⚙ IDE 配置", body, foot);
}
