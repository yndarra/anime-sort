/* API и ключи: провайдеры (providers/<имя>/provider.json), ключи (secrets/… — значения сюда не приходят никогда,
   только «есть / пусто»; вставка уходит прямо в файл), «Проверить все API» — каждый ключ × каждая модель
   (tools/agent/config_agent.py: survey), итог — в logs/probes.json и балансы OpenRouter. */
"use strict";

(() => {
  const STATE = { ok: "работает", no_balance: "нет баланса", no_image: "теряет картинку", no_key: "нет ключа", error: "ошибка" };
  const s = { providers: [], provider: null, keys: [], probes: null, surveying: false };

  function providersCard() {
    const current = s.providers.find(p => p.name === s.provider) || s.providers[0];
    if (current) s.provider = current.name;
    const area = h("textarea", { rows: 16, spellcheck: "false" });
    area.value = current ? current.text : "";
    const note = h("small", { style: { color: "var(--muted)" } }, current ? `${current.models} моделей · ${current.endpoint}` : "");
    return h("div", { class: "card span7" },
      h("h3", {}, "Провайдеры", h("span", { class: "right" },
        h("button", { class: "btn small", onclick: async () => {
          const answer = await promptBox("Новый провайдер", "Создастся providers\\<имя>\\provider.json по образцу и папка для ключей.",
            [{ name: "name", label: "Имя (латиница)" }]);
          if (!answer) return;
          const result = await App.call("new_provider", answer.name);
          if (!result.ok) { toast(result.error, "err"); return; }
          s.provider = answer.name.trim(); await load(); draw();
        } }, "+ Провайдер"))),
      h("div", { class: "chips", style: { marginBottom: "12px" } }, s.providers.map(p =>
        h("span", { class: `chip ${p.name === s.provider ? "on" : ""}`, onclick: () => { s.provider = p.name; draw(); } },
          p.name, p.images === false ? " · только текст" : ""))),
      area,
      h("div", { style: { display: "flex", gap: "10px", alignItems: "center", marginTop: "10px" } }, note, h("span", { class: "spacer" }),
        h("button", { class: "btn primary", onclick: async () => {
          const result = await App.call("save_provider", s.provider, area.value);
          if (result.saved) { toast("provider.json сохранён", "ok"); await load(); draw(); } else toast(result.error, "err");
        } }, "Сохранить provider.json")));
  }

  function keysCard() {
    const rows = s.keys.map(key => h("tr", {},
      h("td", {}, key.agent ? h("span", {}, "agent/openrouter ", h("small", { style: { color: "var(--muted)" } }, "(агенты)")) : key.route),
      h("td", {}, h("span", { class: `pill ${key.filled ? "done" : "new"}` }, key.filled ? "есть" : "пусто")),
      h("td", { class: "num" },
        h("button", { class: "btn small", onclick: () => putKey(key) }, key.filled ? "Заменить" : "Вставить"),
        key.filled ? h("button", { class: "btn small ghost", onclick: async () => {
          if (!await confirmBox("Убрать ключ?", `${key.route} уйдёт в Корзину.`, "Убрать", "danger")) return;
          const result = await App.call("remove_key", key.route);
          if (result.ok) { toast("Ключ в Корзине", "ok"); await load(); draw(); } else toast(result.error, "err");
        } }, "Убрать") : null)));
    return h("div", { class: "card span5" },
      h("h3", {}, "Ключи", h("span", { class: "right" }, h("button", { class: "btn small", onclick: () => putKey(null) }, "+ Ключ"))),
      h("div", { class: "hint", style: { color: "var(--faint)", fontSize: "12px", marginBottom: "8px" } },
        "secrets\\providers — не в git; значения ключей сюда не приходят и нигде не показываются"),
      h("div", { class: "scroll", style: { maxHeight: "380px" } }, h("table", {}, rows)));
  }

  async function putKey(key) {
    const [provider, name] = key ? (key.agent ? ["agent", "openrouter"] : key.route.split("/")) : [s.provider || "", "key0"];
    const answer = await modal("Ключ API", "Ключ записывается только в файл secrets\\…", body => {
      body.append(
        h("div", { class: "field" }, h("span", {}, "Провайдер"), h("select", { name: "provider", disabled: key ? true : null },
          [...s.providers.map(p => p.name), "agent"].map(p => h("option", { value: p, selected: p === provider }, p === "agent" ? "agent (ключ агентов)" : p)))),
        h("div", { class: "field" }, h("span", {}, "Имя ключа"), h("input", { type: "text", name: "key", value: name, disabled: key ? true : null })),
        h("div", { class: "field" }, h("span", {}, "Ключ"), h("input", { type: "password", name: "value", autocomplete: "off" })));
    }, [{ label: "Отмена", value: null },
        { label: "Сохранить", kind: "primary", value: body => ({ provider: body.querySelector("[name=provider]").value,
          key: body.querySelector("[name=key]").value, value: body.querySelector("[name=value]").value }) }]);
    if (!answer) return;
    const result = await App.call("put_key", answer.provider, answer.provider === "agent" ? "openrouter" : answer.key, answer.value);
    if (result.ok) { toast("Ключ сохранён", "ok"); await load(); draw(); } else toast(result.error, "err");
  }

  function surveyCard() {
    const card = h("div", { class: "card span12" }, h("h3", {}, "Проверка всех API",
      h("span", { class: "right" },
        s.probes && s.probes.time ? h("small", { style: { color: "var(--muted)" } }, `последняя: ${s.probes.time}`) : null,
        h("button", { class: "btn small primary", disabled: s.surveying ? true : null, onclick: survey },
          s.surveying ? "Проверяю… (до пары минут)" : "Проверить все API"))));
    if (s.probes && s.probes.balances && s.probes.balances.length) {
      card.append(h("div", { class: "chips", style: { marginBottom: "10px" } }, s.probes.balances.map(b => h("span", { class: "chip on" }, `${b.route}: $${b.usd}`))));
    }
    const rows = (s.probes && s.probes.probes) || [];
    if (!rows.length) { card.append(h("div", { class: "empty" }, "Проверки ещё не было — по маленькому запросу на ключ × модель, ключ агента не тратится")); return card; }
    const byRoute = {};
    for (const row of rows) (byRoute[row.route] = byRoute[row.route] || []).push(row);
    card.append(h("div", { class: "scroll", style: { maxHeight: "420px" } }, h("table", {},
      h("tr", {}, h("th", {}, "Ключ"), h("th", {}, "Модель"), h("th", {}, "Вид"), h("th", {}, "Состояние"), h("th", {}, "Подробности")),
      Object.entries(byRoute).flatMap(([route, list]) => list.map((row, index) => h("tr", {},
        h("td", {}, index === 0 ? h("b", {}, route) : ""), h("td", {}, row.model), h("td", { class: "mode" }, row.images ? "картинка" : "текст"),
        h("td", {}, h("span", { class: `pill ${row.status === "ok" ? "done" : row.status === "no_key" ? "new" : "bad"}` }, STATE[row.status] || row.status)),
        h("td", { class: "mode", style: { whiteSpace: "normal" } }, row.status === "ok" ? `${row.seconds} с` : row.detail)))))));
    return card;
  }

  async function survey() {
    s.surveying = true; draw();
    try { s.probes = await App.call("survey"); toast("Проверка закончена", "ok"); }
    finally { s.surveying = false; draw(); }
  }

  async function load() {
    [s.providers, s.keys] = await Promise.all([App.call("providers"), App.call("keys")]);
    if (!s.probes) s.probes = await App.call("probes");
  }

  function draw() {
    if (App.current !== App.pages.apis) return;
    const root = document.getElementById("content");
    root.innerHTML = "";
    root.append(h("div", { class: "grid" }, providersCard(), keysCard(), surveyCard()));
  }

  App.page("apis", {
    title: "API и ключи",
    sub: "Провайдеры, ключи и проверка всех API",
    async render() { await load(); draw(); },
  });
})();
