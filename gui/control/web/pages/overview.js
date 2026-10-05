/* Обзор: карточки показателей (с мини-графиками по logs/history.json), все наборы с миниатюрами и прогрессом,
   модели и API за сегодня (тот же сборщик, что у главного окна конвейеров). */
"use strict";

(() => {
  const FILTERS = [["all", "Все"], ["main", "Обычные"], ["other", "Второй круг"], ["pending", "Не влиты"]];
  const MODEL_COLORS = ["var(--accent)", "var(--accent2)", "var(--blue)", "var(--yellow)", "var(--green)", "var(--red)"];
  const state = { filter: "all", thumbs: {}, observer: null };

  function percent(a, b) { return b ? Math.round(100 * a / b) : 0; }

  function kpi(title, value, hint, series, color) {
    return h("div", { class: "card kpi span3" }, h("div", { class: "glow", style: { background: color } }),
      h("h3", {}, title), h("div", { class: "value" }, value), h("div", { class: "hint" }, hint), sparkline(series, color));
  }

  function datasetRow(item) {
    const share = item.total ? percent(item.resolved, item.total) : 0;
    const thumbs = h("div", { class: "thumbs", "data-name": item.name }, [0, 1, 2].map(() => h("span")));
    const row = h("tr", { class: "clickable", ondblclick: () => App.call("open_dataset", item.name, "window") },
      h("td", {}, item.name),
      h("td", { class: "mode" }, item.mode === "other" ? "второй круг" : "обычный"),
      h("td", {}, h("span", { class: "bar" }, h("i", { style: { width: `${share}%` } })),
        item.total ? `${fmt(item.resolved)} / ${fmt(item.total)}` : "—"),
      h("td", {}, h("span", { class: `pill ${item.status}` }, item.statusText)),
      h("td", { class: "num" }, item.broken || ""),
      h("td", {}, thumbs),
      h("td", { class: "num" },
        h("button", { class: "btn small ghost", title: "Папка набора", onclick: e => { e.stopPropagation(); App.call("open_dataset", item.name, "folder"); } }, "Папка"),
        h("button", { class: "btn small ghost", title: "Окно лога набора", onclick: e => { e.stopPropagation(); App.call("open_dataset", item.name, "window"); } }, "Лог")));
    if (state.thumbs[item.name]) fillThumbs(thumbs, state.thumbs[item.name]);
    else if (state.observer) state.observer.observe(thumbs);
    return row;
  }

  function fillThumbs(node, urls) {
    node.innerHTML = "";
    if (!urls.length) { node.append(h("span"), h("span"), h("span")); return; }
    for (const url of urls) node.append(h("img", { src: url, loading: "lazy" }));
  }

  function datasetsCard(data) {
    const chips = h("div", { class: "chips" }, FILTERS.map(([key, label]) =>
      h("span", { class: `chip ${state.filter === key ? "on" : ""}`, onclick: () => { state.filter = key; render(document.getElementById("content"), data); } }, label)));
    const rows = data.datasets.filter(item =>
      state.filter === "all" || (state.filter === "pending" ? item.status !== "done" : item.mode === state.filter)).reverse();
    const table = h("table", {},
      h("tr", {}, h("th", {}, "Набор"), h("th", {}, "Режим"), h("th", {}, "Определено"), h("th", {}, "Состояние"),
        h("th", { class: "num" }, "Битых"), h("th", {}, "Примеры"), h("th")),
      rows.map(datasetRow));
    return h("div", { class: "card span8" },
      h("h3", {}, "Наборы", h("span", { class: "right" }, chips)),
      h("div", { class: "scroll", style: { maxHeight: "460px" } }, rows.length ? table : h("div", { class: "empty" }, "Таких наборов нет")));
  }

  function modelsCard(live, probes) {
    const card = h("div", { class: "card span4" }, h("h3", {}, "Модели сегодня"));
    if (!live.models.length) card.append(h("div", { class: "empty" }, "Сегодня моделям ещё ничего не отправляли"));
    live.models.slice(0, 8).forEach((model, index) => {
      const share = percent(model.determined, model.checked);
      card.append(h("div", { class: "model" },
        h("span", {}, model.model),
        h("span", { class: "track" }, h("i", { style: { width: `${share}%`, background: MODEL_COLORS[index % MODEL_COLORS.length] } })),
        h("span", { class: "pct" }, `${share}%`),
        h("small", {}, `${fmt(model.checked)} файлов${model.seconds ? ` · ${model.seconds} с/файл` : ""}${model.errors ? ` · сбоев ${model.errors}` : ""}`)));
    });
    card.append(h("h3", { style: { marginTop: "18px" } }, "API",
      h("span", { class: "right" }, h("button", { class: "btn small ghost", onclick: () => App.go("apis") }, "Проверить все"))));
    if (live.apis.length) {
      for (const api of live.apis.slice(0, 8)) {
        card.append(h("div", { class: "api-row" }, h("span", {}, h("span", { class: `dot ${api.ok ? "on" : "bad"}` }), api.api),
          h("small", {}, api.ok ? `${fmt(api.files)} файлов${api.last ? ` · ответ ${api.last}` : ""}` : api.state)));
      }
    } else if (probes && probes.probes) {
      const byRoute = {};
      for (const row of probes.probes) (byRoute[row.route] = byRoute[row.route] || []).push(row.status);
      for (const [route, statuses] of Object.entries(byRoute)) {
        const ok = statuses.filter(s => s === "ok").length;
        card.append(h("div", { class: "api-row" }, h("span", {}, h("span", { class: `dot ${ok ? "on" : "bad"}` }), route),
          h("small", {}, ok ? `работает: ${ok} из ${statuses.length} моделей` : "не работает")));
      }
      card.append(h("div", { class: "hint", style: { color: "var(--faint)", fontSize: "12px", marginTop: "8px" } }, `по проверке ${probes.time}`));
    } else {
      card.append(h("div", { class: "empty" }, "Нет данных — «Проверить все»"));
    }
    return card;
  }

  function render(root, data) {
    const waifu = data.waifu || {};
    const history = data.history || [];
    const scroll = root.querySelector(".scroll");
    const top = scroll ? scroll.scrollTop : 0;
    if (state.observer) state.observer.disconnect();
    state.observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const node = entry.target;
        state.observer.unobserve(node);
        const name = node.dataset.name;
        if (state.thumbs[name]) { fillThumbs(node, state.thumbs[name]); continue; }
        window.pywebview.api.thumbs(name).then(urls => { state.thumbs[name] = urls; fillThumbs(node, urls); }).catch(() => {});
      }
    });
    const main = data.datasets.filter(d => d.mode === "main").length;
    const grid = h("div", { class: "grid" },
      kpi("Наборов", fmt(data.datasets.length), `${main} обычных · ${data.datasets.length - main} второго круга`,
        history.map(p => p.datasets), "#8b7cff"),
      kpi("Картинок в Waifu", waifu.error ? "—" : fmt(waifu.images), waifu.error || `${fmt(waifu.titles)} тайтлов`,
        history.map(p => p.images), "#5ad1c4"),
      kpi("В Other", waifu.error ? "—" : fmt(waifu.other), "готовы ко второму кругу", history.map(p => p.other), "#f2c94c"),
      kpi("Определено", `${percent(data.resolved, data.total)}%`, `${fmt(data.resolved)} из ${fmt(data.total)} · битых ${fmt(data.broken)}`,
        history.map(p => p.total ? p.resolved / p.total : null), "#3ecf8e"),
      datasetsCard(data),
      modelsCard(data.live, data.probes));
    root.innerHTML = "";
    root.append(grid);
    const newScroll = root.querySelector(".scroll");
    if (newScroll) newScroll.scrollTop = top;
  }

  App.page("overview", {
    title: "Обзор",
    sub: "Коллекция, наборы и модели — обновляется сама",
    every: 6000,
    actions(bar) {
      bar.append(
        h("button", { class: "btn ghost", onclick: () => App.go("apis") }, "Проверить API"),
        h("button", { class: "btn", onclick: () => App.go("pipelines") }, "Подготовить"),
        h("button", { class: "btn primary", onclick: async () => toast(await App.call("start_pipelines")) }, "▶ Запустить конвейеры"));
    },
    async render(root) { render(root, await App.call("overview")); },
    async refresh(root) { render(root, await window.pywebview.api.overview()); },
    leave() { if (state.observer) state.observer.disconnect(); },
  });
})();
