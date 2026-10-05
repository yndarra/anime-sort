/* Конвейеры (обычный режим) и Второй круг (Waifu\Other → временные dataN-other): шаги работы карточками,
   живые пачки и наборы режима. Шаблон пачки режима — configs/main или configs/other (страница «Конфиги»). */
"use strict";

(() => {
  const STEPS = {
    main: [
      ["agent", "Агент конфигов", "Опросить все ключи и модели и пересобрать шаблоны под то, что сейчас работает (платно: OpenRouter, ключ агента)"],
      ["prepare", "Подготовить", "Проверить API, найти новые dataN, собрать пачки config1…N по configs\\main\\template.json"],
      ["start", "Запустить конвейеры", "Главное окно anime-sort, пачки и окна наборов (как start.vbs)"],
      ["stop", "Остановить всё", "Закрыть главное окно, пачки и окна наборов — сделанное сохраняется"],
      ["finish", "Finish", "Влить готовые наборы в Waifu → мелкие тайтлы → нумерация папок"],
    ],
    other: [
      ["small", "Мелкие тайтлы → Other", "Персонажи мелких тайтлов — в большие тайтлы, остальное — в Other (сначала предпросмотр и вопрос)"],
      ["other", "Other → dataN-other", "Всё из Waifu\\Other — во временные папки второго круга (по размеру из настроек)"],
      ["prepare", "Подготовить", "Проверить API и собрать пачки по configs\\other\\template.json — только для dataN-other"],
      ["start", "Запустить конвейеры", "Главное окно anime-sort, пачки и окна наборов (как start.vbs)"],
      ["stop", "Остановить всё", "Закрыть главное окно, пачки и окна наборов — сделанное сохраняется"],
      ["finish", "Finish", "Влить готовые наборы в Waifu → мелкие тайтлы → нумерация папок"],
    ],
  };
  const BUTTON = { start: "▶ Запустить", stop: "■ Остановить", agent: "Запустить агента" };

  function makePage(mode) {
    const settings = {};

    async function doStep(action) {
      if (action === "start") { toast(await App.call("start_pipelines")); return; }
      if (action === "stop") {
        if (await confirmBox("Остановить всё?", "Закроются главное окно, все пачки и окна наборов. Всё сделанное сохранится — следующий запуск продолжит с того же места.", "Остановить", "danger"))
          toast(await App.call("stop_pipelines"));
        return;
      }
      if (action === "agent" && !await confirmBox("Агент конфигов", "Агент тратит баланс OpenRouter (ключ агента) — обычно центы. Запустить?", "Запустить")) return;
      await App.startTask(App.call("step", action, mode, action === "small" ? settings.small_title_max_files : null));
    }

    function stepsCard() {
      return h("div", { class: "card span5" }, h("h3", {}, "Шаги"),
        h("div", { class: "steps" }, STEPS[mode].map(([action, title, about], index) =>
          h("div", { class: "step" }, h("div", { class: "n" }, index + 1),
            h("div", {}, h("b", {}, title), h("small", {}, about)),
            h("button", { class: `btn ${action === "start" ? "primary" : action === "stop" ? "danger" : ""}`, onclick: () => doStep(action) },
              BUTTON[action] || "Выполнить")))));
    }

    function settingsCard() {
      const field = (key, label, hint) => h("div", { class: "field" }, h("span", {}, label),
        h("input", { type: "number", min: 1, value: settings[key], oninput: e => { settings[key] = e.target.value; } }),
        h("div", { class: "hint" }, hint));
      return h("div", { class: "card span5" }, h("h3", {}, "Настройки второго круга",
          h("span", { class: "right" }, h("button", { class: "btn small primary", onclick: async () => toast(await App.call("save_other_settings", settings), "ok") }, "Сохранить"))),
        field("other_batch_size", "Файлов в dataN-other", "по столько файлов other.py кладёт в одну временную папку"),
        field("small_title_max_files", "Мелкий тайтл — до", "тайтл с таким числом картинок и меньше разбирается"),
        h("div", { class: "hint", style: { color: "var(--faint)", fontSize: "12px" } }, "configs\\other\\settings.json"));
    }

    function batchesCard(state) {
      const card = h("div", { class: "card span7" }, h("h3", {}, "Пачки сейчас",
        h("span", { class: "right" }, h("span", { class: `pill ${state.running ? "active" : "new"}` }, state.running ? "конвейеры работают" : "конвейеры остановлены"))));
      if (!state.running || !state.batches.length) {
        card.append(h("div", { class: "empty" }, state.running ? "Пачки запускаются…" : "Конвейеры не запущены — шаги слева: подготовить → запустить."));
        return card;
      }
      card.append(h("div", { class: "batch", style: { color: "var(--muted)", fontSize: "12px" } },
        h("span", {}, "Пачка"), h("span", {}, "Набор"), h("span", {}, "Этап"), h("span", {}, "Определено"), h("span", {}, "Папок"), h("span", {}, "Идёт")));
      for (const batch of state.batches) {
        const share = batch.total ? Math.round(100 * batch.resolved / batch.total) : 0;
        card.append(h("div", { class: "batch" }, h("b", {}, batch.config),
          h("span", {}, batch.folder || h("span", { class: "muted" }, batch.left ? "ждёт" : "готово")),
          h("span", { class: "muted" }, batch.stage),
          batch.folder ? h("span", {}, h("span", { class: "bar" }, h("i", { style: { width: `${share}%` } })), `${share}%`) : h("span"),
          h("span", {}, batch.left), h("span", { class: "muted" }, batch.running)));
      }
      return card;
    }

    function setsCard(state) {
      const card = h("div", { class: "card span7" }, h("h3", {}, "Наборы этого режима"));
      if (!state.datasets.length) { card.append(h("div", { class: "empty" }, "Наборов этого режима нет")); return card; }
      for (const status of ["active", "started", "new", "completed", "done"]) {
        const items = state.datasets.filter(d => d.status === status);
        if (!items.length) continue;
        card.append(h("div", { style: { margin: "10px 0" } },
          h("span", { class: `pill ${status}` }, `${items[0].statusText} · ${items.length}`),
          h("div", { class: "chips", style: { marginTop: "8px" } }, items.map(d =>
            h("span", { class: "chip", title: d.total ? `${d.resolved} / ${d.total}` : "", ondblclick: () => App.call("open_dataset", d.name, "window") }, d.name)))));
      }
      return card;
    }

    async function draw(root) {
      const state = await window.pywebview.api.pipeline_state(mode);
      const grid = h("div", { class: "grid" }, stepsCard(), batchesCard(state));
      if (mode === "other") grid.append(settingsCard());
      grid.append(setsCard(state));
      root.innerHTML = "";
      root.append(grid);
    }

    return {
      title: mode === "main" ? "Конвейеры" : "Второй круг",
      sub: mode === "main" ? "Обычный режим: новые dataN → Waifu" : "Waifu\\Other → временные dataN-other → Waifu",
      every: 4000,
      actions(bar) {
        bar.append(h("button", { class: "btn", onclick: () => App.go("configs") }, "Шаблон пачки"),
          h("button", { class: "btn primary", onclick: () => doStep("start") }, "▶ Запустить конвейеры"));
      },
      async render(root) {
        if (mode === "other") Object.assign(settings, await App.call("other_settings"));
        await draw(root);
      },
      async refresh(root) {
        if (root.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;   // не мешать вводу
        await draw(root);
      },
    };
  }

  App.page("pipelines", makePage("main"));
  App.page("other", makePage("other"));
})();
