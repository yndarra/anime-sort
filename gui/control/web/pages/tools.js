/* Инструменты: всё из tools/ — карточками по группам. «Показать» — пробный прогон (ничего не меняет),
   «Выполнить» — по-настоящему. Список и аргументы — в gui/control/api.py (TOOLS). */
"use strict";

(() => {
  async function run(tool, preview) {
    if (tool.warning && !await confirmBox(tool.title, tool.warning, "Запустить")) return;
    await App.startTask(App.call("run_tool", tool.key, preview));
  }

  App.page("tools", {
    title: "Инструменты",
    sub: "Коллекция Waifu, имена, наборы, наблюдатели — вывод и вопросы в «Задачах» внизу",
    async render(root) {
      const tools = await App.call("tools");
      const groups = [...new Set(tools.map(t => t.group))];
      const grid = h("div", { class: "grid" });
      for (const group of groups) {
        const card = h("div", { class: "card span6" }, h("h3", {}, group));
        for (const tool of tools.filter(t => t.group === group)) {
          card.append(h("div", { class: "api-row", style: { padding: "11px 0" } },
            h("div", {}, h("div", {}, tool.title), h("small", { style: { textAlign: "left", display: "block" } }, tool.about)),
            h("div", { style: { display: "flex", gap: "6px" } },
              tool.preview ? h("button", { class: "btn small", onclick: () => run(tool, true) }, "Показать") : null,
              h("button", { class: `btn small ${group === "Наблюдатели" ? "" : "primary"}`, onclick: () => run(tool, false) },
                group === "Наблюдатели" ? "Запустить" : "Выполнить"))));
        }
        grid.append(card);
      }
      root.innerHTML = "";
      root.append(grid);
    },
  });
})();
