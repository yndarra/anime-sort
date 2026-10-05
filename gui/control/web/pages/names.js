/* Имена: tools/fix_name/names.json — правила «как папка называется сейчас → как должна называться».
   Блоки — целевые тайтлы (как в файле), «new» — новые правила; «Упорядочить» разносит их по блокам (regroup.py).
   Строки рисуются только у раскрытых блоков (правил — больше тысячи). Персонаж — «Тайтл\Персонаж». */
"use strict";

(() => {
  const s = { data: null, query: "", open: new Set(["new"]), dirty: false };

  function blocks() { return [["new", s.data.new], ...Object.entries(s.data.titles)]; }

  function matches(source, target) {
    if (!s.query) return true;
    const q = s.query.toLowerCase();
    return source.toLowerCase().includes(q) || String(target).toLowerCase().includes(q);
  }

  function mark() { s.dirty = true; const node = document.getElementById("names-state"); if (node) node.textContent = "есть несохранённые изменения"; }

  function ruleRow(rules, source) {
    let current = source;
    const sourceInput = h("input", { type: "text", value: source, spellcheck: "false", onchange: e => {
      const value = e.target.value.trim();
      if (!value || value === current) return;
      const target = rules[current]; delete rules[current]; rules[value] = target; current = value; mark(); } });
    const targetInput = h("input", { type: "text", value: rules[source], spellcheck: "false", placeholder: "пусто — правило выключено",
      oninput: e => { rules[current] = e.target.value; mark(); } });
    const row = h("div", { class: `names-rule ${String(rules[source]).trim() ? "" : "off"}` }, sourceInput, h("span", { class: "arrow" }, "→"), targetInput,
      h("button", { class: "btn small ghost", title: "Удалить правило", onclick: () => { delete rules[current]; mark(); row.remove(); } }, "✕"));
    return row;
  }

  function draw() {
    const root = document.getElementById("content");
    let total = 0, shown = 0;
    const list = h("div", { class: "card", style: { padding: "6px 14px" } });
    for (const [title, rules] of blocks()) {
      const entries = Object.entries(rules);
      total += entries.length;
      const found = entries.filter(([source, target]) => matches(source, target));
      if (s.query && !found.length) continue;
      if (!s.query && title !== "new" && !entries.length) continue;
      const open = s.query ? true : s.open.has(title);
      list.append(h("div", { class: "names-title", onclick: () => { if (s.open.has(title)) s.open.delete(title); else s.open.add(title); draw(); } },
        h("span", { style: { color: "var(--faint)" } }, open ? "▾" : "▸"),
        h("b", {}, title === "new" ? "new — ещё не разнесены по тайтлам" : title),
        h("span", { class: "count" }, s.query ? `${found.length} из ${entries.length}` : `${entries.length} правил`)));
      if (open) for (const [source] of found) { list.append(ruleRow(rules, source)); shown += 1; }
    }
    const search = h("input", { type: "text", value: s.query, placeholder: "Поиск по источнику и цели…", style: { maxWidth: "420px" },
      oninput: e => { s.query = e.target.value; clearTimeout(draw.timer); draw.timer = setTimeout(() => { draw(); document.getElementById("names-search").focus(); }, 250); } });
    search.id = "names-search";
    root.innerHTML = "";
    root.append(h("div", { style: { display: "flex", gap: "12px", alignItems: "center", marginBottom: "14px" } }, search,
      h("small", { style: { color: "var(--muted)" } }, `правил ${total}${s.query ? `, найдено ${shown}` : ""}`),
      h("small", { id: "names-state", style: { color: "var(--yellow)" } }, s.dirty ? "есть несохранённые изменения" : ""),
      h("span", { class: "spacer" }),
      h("button", { class: "btn", onclick: addRule }, "+ Правило")), list);
    const input = document.getElementById("names-search");
    if (s.query) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  }

  async function addRule() {
    const answer = await promptBox("Новое правило", "Цели — официальные английские названия. Персонаж — «Тайтл\\Персонаж».",
      [{ name: "source", label: "Источник" }, { name: "target", label: "Цель" }]);
    if (!answer || !answer.source.trim()) return;
    s.data.new[answer.source.trim()] = answer.target.trim();
    s.open.add("new"); mark(); draw();
  }

  async function save() { toast(await App.call("save_names", s.data), "ok"); s.dirty = false; draw(); }

  App.page("names", {
    title: "Имена",
    sub: "names.json — правила переименования тайтлов и персонажей в Waifu",
    actions(bar) {
      bar.append(
        h("button", { class: "btn ghost", onclick: async () => {
          if (await confirmBox("Агент имён", "Агент тратит баланс OpenRouter (ключ агента). Запустить?", "Запустить"))
            App.startTask(App.call("run_tool", "names_agent", false));
        } }, "Агент имён"),
        h("button", { class: "btn", onclick: async () => { if (s.dirty) await save(); App.startTask(App.call("apply_names", true)); } }, "Показать, что изменится"),
        h("button", { class: "btn", onclick: async () => {
          if (!await confirmBox("Применить к Waifu?", "Папки Waifu переименуются и сольются по правилам.", "Применить")) return;
          if (s.dirty) await save();
          App.startTask(App.call("apply_names", false));
        } }, "Применить к Waifu"),
        h("button", { class: "btn", onclick: async () => { s.data = await App.call("regroup_names", s.data); s.dirty = false; draw(); toast("Упорядочено", "ok"); } }, "Упорядочить"),
        h("button", { class: "btn primary", onclick: save }, "Сохранить"));
    },
    async render() { s.data = await App.call("names"); s.dirty = false; draw(); },
  });
})();
