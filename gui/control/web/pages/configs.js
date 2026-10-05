/* Конфиги: шаблоны пачек формой (этапы с перетаскиванием, панель этапа, API с порядком, запасная модель),
   config.json и простые конфиги формами, у каждого — вкладка JSON. «Проверить» и «Сохранить» — тем же кодом,
   что движок (gui/control/checks.py); при сохранении прежний файл остаётся рядом как .bak. Ключи "//" не теряются. */
"use strict";

(() => {
  const API_TYPES = ["ai_image", "json_meta"];
  const s = { files: [], meta: null, file: null, data: null, view: "form", dirty: false, stage: 0, messages: [] };

  /* ---------- общее ---------- */
  function changed() { s.dirty = true; drawHeaderState(); }

  function drawHeaderState() {
    const mark = document.getElementById("cfg-dirty");
    if (mark) mark.textContent = s.dirty ? "есть несохранённые изменения" : "";
  }

  function datalist(id, values) {
    return h("datalist", { id }, values.map(v => h("option", { value: v })));
  }

  function field(label, input, hint, error) {
    return h("div", { class: "field" }, h("span", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null,
      error ? h("div", { class: "error" }, error) : null);
  }

  function textInput(value, onInput, attrs = {}) {
    return h("input", { type: "text", value: value ?? "", spellcheck: "false", oninput: e => { onInput(e.target.value); changed(); }, ...attrs });
  }

  function numberOrEmpty(value) {
    const text = String(value).trim().replace(",", ".");
    if (!text) return undefined;
    const number = Number(text);
    return Number.isFinite(number) ? number : value;
  }

  /* ---------- маршруты API (перетаскивание, удаление, добавление) ---------- */
  function routesEditor(list, onChange) {
    const keys = Object.fromEntries(s.meta.routes.map(r => [r.route, r.filled]));
    const box = h("div", { class: "routes" });
    let dragFrom = null;
    const redraw = () => { box.replaceWith(routesEditor(list, onChange)); };
    list.forEach((item, index) => {
      const route = typeof item === "string" ? item : item.api;
      const model = typeof item === "string" ? "" : item.model;
      const row = h("div", { class: "route", draggable: "true",
          ondragstart: () => { dragFrom = index; },
          ondragover: e => { e.preventDefault(); row.classList.add("drag-over"); },
          ondragleave: () => row.classList.remove("drag-over"),
          ondrop: e => { e.preventDefault(); if (dragFrom === null || dragFrom === index) return;
            const [moved] = list.splice(dragFrom, 1); list.splice(index, 0, moved); onChange(); redraw(); } },
        h("span", { class: "grip" }, "⋮⋮"), route, model ? h("span", { style: { color: "var(--accent2)" } }, `(${model})`) : null,
        keys[route] ? null : h("span", { class: "warn" }, "нет ключа"),
        h("span", { class: "x", title: "Убрать", onclick: () => { list.splice(index, 1); onChange(); redraw(); } }, "✕"));
      box.append(row);
    });
    const route = h("select", {}, h("option", { value: "" }, "+ ключ…"), s.meta.routes.map(r => h("option", { value: r.route }, r.route)));
    const model = h("input", { type: "text", placeholder: "модель (пусто — модель этапа)", list: "cfg-models" });
    box.append(h("div", { class: "add-route" }, route, model, h("button", { class: "btn small", onclick: () => {
      if (!route.value) return;
      list.push(model.value.trim() ? { api: route.value, model: model.value.trim() } : route.value);
      onChange(); redraw();
    } }, "Добавить")));
    return box;
  }

  /* ---------- шаблон пачки ---------- */
  function stages() { return (s.data.stages || []).filter(stage => "id" in stage); }

  function templateForm() {
    const data = s.data;
    const countKey = "batches" in data ? "batches" : "max_batches";
    const top = h("div", { style: { display: "flex", gap: "16px", alignItems: "center", flexWrap: "wrap", marginBottom: "14px" } },
      h("select", { style: { width: "150px" }, onchange: e => { const value = data[countKey] ?? 4; delete data.batches; delete data.max_batches; data[e.target.value] = value; changed(); } },
        ["max_batches", "batches"].map(k => h("option", { value: k, selected: k === countKey }, k === "batches" ? "пачек ровно" : "пачек не больше"))),
      h("input", { type: "number", min: 1, max: 16, value: data[countKey] ?? 4, style: { width: "80px" },
        oninput: e => { data["batches" in data ? "batches" : "max_batches"] = Number(e.target.value); changed(); } }),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!data.rotate_ai,
        onchange: e => { if (e.target.checked) data.rotate_ai = true; else delete data.rotate_ai; changed(); } }), "свой порядок AI-этапов у каждой пачки"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: data.previous === "keep",
        onchange: e => { if (e.target.checked) data.previous = "keep"; else delete data.previous; changed(); } }), "хранить ответы убранных этапов"));
    return h("div", {}, top, h("div", { class: "stages" }, stageTable(), stagePanel()));
  }

  function stageTable() {
    let dragFrom = null;
    const list = stages();
    const rows = list.map((stage, index) => {
      const input = [stage.from !== undefined ? `${stage.from}+` : "", stage.where || ""].filter(Boolean).join(" ");
      const row = h("tr", { class: `clickable stage-row ${index === s.stage ? "selected" : ""} ${stage.enabled === false ? "off" : ""}`, draggable: "true",
          onclick: () => { s.stage = index; drawForm(); },
          ondragstart: () => { dragFrom = index; },
          ondragover: e => { e.preventDefault(); row.classList.add("drag-over"); },
          ondragleave: () => row.classList.remove("drag-over"),
          ondrop: e => { e.preventDefault(); move(dragFrom, index); } },
        h("td", {}, "⋮⋮"), h("td", {}, h("b", {}, stage.id)), h("td", { class: "mode" }, stage.type), h("td", {}, stage.model || ""),
        h("td", { class: "num" }, stage.accept ?? ""), h("td", { class: "mode" }, input), h("td", { class: "num" }, (stage.api || []).length || ""),
        h("td", { class: "mode" }, stage.fallback ? stage.fallback.model : ""));
      return row;
    });
    const tools = h("div", { style: { display: "flex", gap: "6px", marginTop: "10px", flexWrap: "wrap" } },
      h("button", { class: "btn small", onclick: addStage }, "+ Этап"),
      h("button", { class: "btn small", onclick: duplicateStage }, "Дублировать"),
      h("button", { class: "btn small", onclick: toggleStage }, "Вкл / выкл"),
      h("button", { class: "btn small danger", onclick: deleteStage }, "Удалить"),
      h("span", { class: "hint", style: { color: "var(--faint)", fontSize: "12px", alignSelf: "center" } }, "порядок — перетаскиванием строк"));
    return h("div", { class: "card", style: { padding: "10px 12px" } },
      h("table", {}, h("tr", {}, h("th"), h("th", {}, "Этап"), h("th", {}, "Тип"), h("th", {}, "Модель"), h("th", { class: "num" }, "accept"),
        h("th", {}, "Вход"), h("th", { class: "num" }, "API"), h("th", {}, "Запасная")), rows), tools);
  }

  function move(from, to) {
    if (from === null || from === to) return;
    const all = s.data.stages, list = stages();
    const a = all.indexOf(list[from]);
    const [moved] = all.splice(a, 1);
    all.splice(all.indexOf(list[to]) + (from < to ? 1 : 0), 0, moved);
    s.stage = stages().indexOf(moved);
    changed(); drawForm();
  }

  function addStage() {
    const all = s.data.stages = s.data.stages || [];
    const stage = { id: "NEW", type: "ai_image", model: "", accept: 0.5, api: [] };
    const current = stages()[s.stage];
    all.splice(current ? all.indexOf(current) + 1 : all.length, 0, stage);
    s.stage = stages().indexOf(stage); changed(); drawForm();
  }

  function duplicateStage() {
    const current = stages()[s.stage];
    if (!current) return;
    const copy = JSON.parse(JSON.stringify(current)); copy.id = `${current.id}-2`;
    s.data.stages.splice(s.data.stages.indexOf(current) + 1, 0, copy);
    s.stage += 1; changed(); drawForm();
  }

  function toggleStage() {
    const current = stages()[s.stage];
    if (!current) return;
    if (current.enabled === false) delete current.enabled; else current.enabled = false;
    changed(); drawForm();
  }

  async function deleteStage() {
    const current = stages()[s.stage];
    if (!current || !await confirmBox("Удалить этап?", `Этап ${current.id} уйдёт из шаблона (до сохранения можно «Отменить»).`, "Удалить", "danger")) return;
    s.data.stages.splice(s.data.stages.indexOf(current), 1);
    s.stage = Math.max(0, s.stage - 1); changed(); drawForm();
  }

  function stagePanel() {
    const stage = stages()[s.stage];
    if (!stage) return h("div", { class: "card empty" }, "Этапов нет — «+ Этап»");
    const api = API_TYPES.includes(stage.type);
    const set = (key, value) => { if (value === "" || value === undefined) delete stage[key]; else stage[key] = value; };
    const whereError = h("div", { class: "error" });
    const checkWhere = async text => { whereError.textContent = text.trim() ? await window.pywebview.api.check_where(text) : ""; };
    const panel = h("div", { class: "card" }, h("h3", {}, `Этап ${stage.id}`),
      field("id", textInput(stage.id, v => set("id", v), { list: "cfg-names" }), "метка в логе и папках; подписи — config.json → names"),
      field("Тип", h("select", { onchange: e => { stage.type = e.target.value; if (!API_TYPES.includes(stage.type)) { delete stage.api; delete stage.model; delete stage.fallback; } changed(); drawForm(); } },
        s.meta.types.map(t => h("option", { value: t, selected: t === stage.type }, t)))),
      api ? field("Модель", textInput(stage.model, v => set("model", v), { list: "cfg-models" }), "имя модели из providers\\*\\provider.json") : null,
      stage.type && !stage.type.startsWith("neighbors") ? field("accept", textInput(stage.accept, v => set("accept", numberOrEmpty(v))), "порог уверенности 0…1: ниже — файл идёт дальше") : null,
      field("from", textInput(stage.from, v => set("from", numberOrEmpty(v))), "берёт файлы, у которых уверенность этапа выше не ниже этого"),
      h("div", { class: "field" }, h("span", {}, "where"),
        textInput(stage.where, v => { set("where", v); checkWhere(v); }, { placeholder: "[AI-GF] and [AI-K3]" }),
        h("div", { class: "hint" }, "эти этапы проверили файл и не определили"), whereError),
      field("Пояснение", textInput(stage["//"], v => set("//", v))));
    if (stage.where) checkWhere(stage.where);
    if (api) {
      stage.api = stage.api || [];
      panel.append(h("div", { class: "subsection" }, h("h3", {}, "API — по порядку попыток"), routesEditor(stage.api, changed)));
      const fallback = stage.fallback;
      const sub = h("div", { class: "subsection" }, h("h3", {}, h("label", { class: "check" },
        h("input", { type: "checkbox", checked: !!fallback, onchange: e => {
          if (e.target.checked) stage.fallback = { model: "", after: 20, api: [] }; else delete stage.fallback;
          changed(); drawForm(); } }), "Запасная модель"),
        h("span", { class: "right", style: { color: "var(--faint)" } }, "все API спят дольше after минут — этап переходит на неё")));
      if (fallback) {
        const fset = (key, value) => { if (value === "" || value === undefined) delete fallback[key]; else fallback[key] = value; };
        fallback.api = fallback.api || [];
        sub.append(
          field("Модель", textInput(fallback.model, v => fset("model", v), { list: "cfg-models" })),
          field("after, мин", textInput(fallback.after, v => fset("after", numberOrEmpty(v)))),
          field("В таблице", textInput(fallback.table, v => fset("table", v)), "как этап будет называться в окне после перехода"),
          field("accept", textInput(fallback.accept, v => fset("accept", numberOrEmpty(v))), "пусто — порог этапа"),
          h("div", { style: { marginTop: "8px" } }, routesEditor(fallback.api, changed)));
      }
      panel.append(sub);
    }
    return panel;
  }

  /* ---------- config.json ---------- */
  function configForm() {
    const data = s.data;
    const names = data.names = data.names || {};
    const table = h("table", {}, h("tr", {}, h("th", {}, "id этапа"), h("th", {}, "Метка (лог, папки)"), h("th", {}, "В таблице окна"), h("th")),
      Object.entries(names).map(([id, value]) => {
        const label = Array.isArray(value) ? value[0] : value, shown = Array.isArray(value) ? value[value.length - 1] : value;
        return h("tr", {}, h("td", {}, h("b", {}, id)),
          h("td", {}, textInput(label, v => { names[id] = [v, (names[id] || [])[1] || v]; })),
          h("td", {}, textInput(shown, v => { names[id] = [(names[id] || [])[0] || id, v]; })),
          h("td", { class: "num" }, h("button", { class: "btn small ghost", onclick: () => { delete names[id]; changed(); drawForm(); } }, "✕")));
      }));
    const monitors = s.meta.monitors.length ? s.meta.monitors : [data.monitor || 1];
    return h("div", { class: "grid" },
      h("div", { class: "card span6" }, h("h3", {}, "Коллекция и окна"),
        field("Коллекция", textInput(data.collection, v => { data.collection = v; }), "папка с anime-paths.json (Pictures\\Anime)"),
        field("Пустые поля", h("select", { onchange: e => { data.empty_cells = e.target.value; changed(); } },
          [["blank", "ничего не писать"], ["dash", "прочерки «—»"]].map(([v, t]) => h("option", { value: v, selected: (data.empty_cells || "dash") === v }, t)))),
        field("Монитор при старте", h("select", { onchange: e => { data.monitor = Number(e.target.value); changed(); } },
          monitors.map(m => h("option", { value: m, selected: Number(data.monitor) === Number(m) }, `№ ${m}`))), "номер Windows: Параметры → Дисплей → Определить"),
        field("Пачки (run)", h("span", { class: "mode" }, (data.run || []).join(", ") || "—"), "пишет prepare"),
        h("div", { class: "hint", style: { color: "var(--faint)", fontSize: "12px", marginTop: "10px" } }, "Ширины столбцов лога и таблицы — во вкладке JSON (log_columns, table_columns).")),
      h("div", { class: "card span6" }, h("h3", {}, "Подписи этапов",
          h("span", { class: "right" }, h("button", { class: "btn small", onclick: async () => {
            const answer = await promptBox("Новая подпись этапа", "Метка — не длиннее 20 символов, без \\ / : * ? \" < > |",
              [{ name: "id", label: "id этапа" }, { name: "label", label: "Метка" }, { name: "table", label: "В таблице" }]);
            if (answer && answer.id) { names[answer.id] = [answer.label || answer.id, answer.table || answer.label || answer.id]; changed(); drawForm(); }
          } }, "+ Подпись"))),
        h("div", { class: "scroll", style: { maxHeight: "420px" } }, table)));
  }

  /* ---------- простые конфиги ---------- */
  function flatForm() {
    const data = s.data;
    const card = h("div", { class: "card", style: { maxWidth: "980px" } });
    const nested = [];
    for (const [key, value] of Object.entries(data)) {
      if (key.startsWith("//")) continue;
      let hint = data[`//${key}`] || "";
      const limit = s.file.path === "live.json" ? s.meta.limits[key] : null;
      if (limit) hint = `${limit[2]} (${limit[0]}…${limit[1]})`;
      if (typeof value === "boolean") {
        card.append(field(key, h("label", { class: "check" }, h("input", { type: "checkbox", checked: value,
          onchange: e => { data[key] = e.target.checked; changed(); } }), value ? "да" : "нет"), hint));
      } else if (typeof value === "number" || typeof value === "string") {
        card.append(field(key, textInput(value, v => { data[key] = typeof value === "number" ? numberOrEmpty(v) : v; }), String(hint)));
      } else {
        nested.push(key);
      }
    }
    if (nested.length) card.append(h("div", { class: "hint", style: { color: "var(--faint)", fontSize: "12px", marginTop: "10px" } },
      `Вложенные параметры (${nested.join(", ")}) — во вкладке JSON.`));
    return card;
  }

  /* ---------- редактор целиком ---------- */
  function drawForm() {
    const host = document.getElementById("cfg-body");
    if (!host) return;
    host.innerHTML = "";
    if (s.view === "json" || s.data === null || s.file.kind === "readonly") {
      const area = h("textarea", { id: "cfg-json", rows: 30, spellcheck: "false", readonly: s.file.kind === "readonly" ? true : null,
        oninput: () => changed() });
      area.value = s.data === null ? s.text : JSON.stringify(s.data, null, 2);
      host.append(area);
      if (s.file.kind === "readonly") host.prepend(h("div", { class: "msg tip", style: { marginBottom: "10px" } },
        "Этот файл пишет prepare из шаблона режима — правьте шаблон. Здесь — только просмотр."));
    } else if (s.file.kind === "template") host.append(templateForm());
    else if (s.file.kind === "config") host.append(configForm());
    else host.append(flatForm());
    drawMessages();
  }

  function drawMessages() {
    const box = document.getElementById("cfg-messages");
    if (!box) return;
    box.innerHTML = "";
    for (const [kind, text] of s.messages) box.append(h("div", { class: `msg ${kind}` }, text));
  }

  function jsonToData() {
    const area = document.getElementById("cfg-json");
    if (!area || s.file.kind === "readonly") return true;
    try { s.data = JSON.parse(area.value); return true; }
    catch (error) { s.messages = [["err", `JSON: ${error.message}`]]; drawMessages(); return false; }
  }

  function show(problems, savedText) {
    const errors = problems.filter(p => !p.startsWith("совет:"));
    s.messages = [];
    if (savedText) s.messages.push(["ok", savedText]);
    for (const p of problems) s.messages.push([p.startsWith("совет:") ? "tip" : "err", p]);
    if (!problems.length && !savedText) s.messages.push(["ok", "Ошибок нет — так движок и примет."]);
    drawMessages();
    return errors;
  }

  async function check() {
    if (s.view === "json" && !jsonToData()) return;
    show(await App.call("check_config", s.file.path, s.data));
  }

  async function save() {
    if (s.view === "json" && !jsonToData()) return;
    let result = await App.call("save_config", s.file.path, s.data, false);
    if (!result.saved) {
      const errors = result.problems.filter(p => !p.startsWith("совет:"));
      if (!await confirmBox("Есть ошибки", `${errors.slice(0, 4).join("\n")}\n\nВсё равно сохранить?`, "Сохранить", "danger")) { show(result.problems); return; }
      result = await App.call("save_config", s.file.path, s.data, true);
    }
    s.dirty = false; drawHeaderState();
    show(result.problems, `Сохранено: configs\\${s.file.path.replace(/\//g, "\\")} (прежний — .bak)`);
    toast("Сохранено", "ok");
  }

  async function open(file) {
    if (s.dirty && !await confirmBox("Отбросить изменения?", "В текущем файле есть несохранённые изменения.", "Отбросить", "danger")) return;
    const result = await App.call("read_config", file.path);
    s.file = file; s.data = result.data; s.text = result.text; s.dirty = false; s.stage = 0; s.messages = [];
    if (result.error) s.messages.push(["err", result.error]);
    if (s.file.kind === "template" && s.data) {
      const first = stages().findIndex(st => API_TYPES.includes(st.type));
      s.stage = first < 0 ? 0 : first;
    }
    s.view = s.data === null || file.kind === "readonly" ? "json" : "form";
    draw();
  }

  function draw() {
    const root = document.getElementById("content");
    const list = h("div", { class: "filelist" });
    let group = null;
    for (const file of s.files) {
      if (file.group !== group) { group = file.group; list.append(h("div", { class: "group" }, group)); }
      list.append(h("div", { class: `file ${s.file && s.file.path === file.path ? "active" : ""}`, onclick: () => open(file) }, file.title));
    }
    const tabs = h("div", { class: "tabs" },
      [["form", "Форма"], ["json", "JSON"]].map(([key, label]) => h("div", { class: `tab ${s.view === key ? "active" : ""}`, onclick: () => {
        if (key === s.view) return;
        if (s.view === "json" && !jsonToData()) return;
        s.view = key; draw();
      } }, label)));
    const editor = h("div", { style: { minWidth: 0 } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" } },
        h("b", {}, s.file ? `configs\\${s.file.path.replace(/\//g, "\\")}` : ""), h("small", { id: "cfg-dirty", style: { color: "var(--yellow)" } }),
        h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => open(s.file) }, "Отменить изменения"),
        h("button", { class: "btn", onclick: check }, "Проверить"),
        h("button", { class: "btn primary", onclick: save }, "Сохранить")),
      tabs, h("div", { id: "cfg-messages", class: "messages", style: { marginBottom: "12px" } }), h("div", { id: "cfg-body" }),
      datalist("cfg-models", s.meta.models), datalist("cfg-names", s.meta.names));
    root.innerHTML = "";
    root.append(h("div", { class: "split" }, list, editor));
    drawHeaderState();
    drawForm();
  }

  App.page("configs", {
    title: "Конфиги",
    sub: "Шаблоны пачек, общие настройки и live.json — формой или как JSON",
    async render() {
      [s.files, s.meta] = await Promise.all([App.call("config_files"), App.call("config_meta")]);
      const first = s.file ? s.files.find(f => f.path === s.file.path) : s.files[0];
      s.dirty = false;
      await open(first || s.files[0]);
    },
  });
})();
