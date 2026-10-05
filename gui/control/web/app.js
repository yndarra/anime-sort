/* Пульт anime-sort — ядро страницы: переходы между страницами, опрос Python раз в секунду, панель «Задачи»,
   всплывающие сообщения и диалоги. Python-сторона — gui/control/api.py (window.pywebview.api).

   Страница регистрируется так:  App.page("overview", { title, sub, actions(bar), render(root), refresh(root), every })
     actions — кнопки в шапке; render — нарисовать; refresh — обновить данные (раз в every мс, пока страница открыта). */
"use strict";

const App = {
  pages: {},
  current: null,
  timer: null,
  selectedTask: null,
  cursors: {},          // id задачи → сколько строк вывода уже показано
  shownTask: null,
  lastPoll: null,

  page(name, spec) { this.pages[name] = spec; },

  /* ---------- вызов Python ---------- */
  async call(method, ...args) {
    try {
      return await window.pywebview.api[method](...args);
    } catch (error) {
      toast(`${method}: ${error.message || error}`, "err");
      throw error;
    }
  },

  /* ---------- переходы ---------- */
  go(name) {
    if (!this.pages[name]) name = "overview";
    if (location.hash !== "#" + name) { location.hash = name; return; }
    this.show(name);
  },

  async show(name) {
    const spec = this.pages[name];
    if (this.current && this.current.leave) this.current.leave();
    clearInterval(this.timer);
    this.current = spec;
    document.querySelectorAll(".nav").forEach(nav => nav.classList.toggle("active", nav.dataset.page === name));
    $("#page-title").textContent = spec.title;
    $("#page-sub").textContent = spec.sub || "";
    const bar = $("#page-actions");
    bar.innerHTML = "";
    if (spec.actions) spec.actions(bar);
    const root = $("#content");
    root.innerHTML = '<div class="loading">Загрузка…</div>';
    root.scrollTop = 0;
    try {
      await spec.render(root);
    } catch (error) {
      root.innerHTML = "";
      root.append(h("div", { class: "msg err" }, `Страница не открылась: ${error.message || error}`));
      console.error(error);
    }
    if (spec.refresh) this.timer = setInterval(() => { if (this.current === spec) spec.refresh(root).catch(console.error); }, spec.every || 5000);
  },

  /* ---------- задачи ---------- */
  async startTask(promise) {
    const id = await promise;
    this.selectedTask = id;
    $("#dock").classList.remove("collapsed");
    $("#dock-toggle").textContent = "свернуть ▾";
    this.poll();
    return id;
  },

  async poll() {
    const cursors = {};
    if (this.selectedTask != null) cursors[this.selectedTask] = this.shownTask === this.selectedTask ? (this.cursors[this.selectedTask] || 0) : 0;
    let data;
    try { data = await window.pywebview.api.poll(cursors); } catch (error) { return; }
    this.lastPoll = data;
    renderFoot(data);
    renderTasks(data);
  },
};

/* ---------- помощники ---------- */
function $(selector, root = document) { return root.querySelector(selector); }

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "html") node.innerHTML = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmt(number) { return number === null || number === undefined ? "—" : Number(number).toLocaleString("ru-RU"); }

function toast(text, kind = "") {
  const node = h("div", { class: `toast ${kind}` }, text);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "err" ? 8000 : 4000);
}

function modal(title, text, build, buttons) {
  /* Диалог: build(body) рисует содержимое; buttons — [{label, kind, value}] → Promise со значением нажатой. */
  return new Promise(resolve => {
    const body = h("div");
    const close = value => { back.remove(); resolve(value); };
    const actions = h("div", { class: "actions" }, buttons.map(b =>
      h("button", { class: `btn ${b.kind || ""}`, onclick: () => close(typeof b.value === "function" ? b.value(body) : b.value) }, b.label)));
    const back = h("div", { class: "modal-back", onclick: event => { if (event.target === back) close(null); } },
      h("div", { class: "modal" }, h("h2", {}, title), text ? h("p", {}, text) : null, body, actions));
    if (build) build(body);
    document.body.append(back);
    const first = body.querySelector("input, select, textarea");
    if (first) first.focus();
  });
}

function confirmBox(title, text, okLabel = "Да", kind = "primary") {
  return modal(title, text, null, [{ label: "Отмена", value: false }, { label: okLabel, kind, value: true }]);
}

function promptBox(title, text, fields) {
  /* fields — [{name, label, type, value, placeholder}] → Promise({name: value}) или null. */
  return modal(title, text, body => {
    for (const field of fields) {
      body.append(h("div", { class: "field" }, h("span", {}, field.label),
        h("input", { type: field.type || "text", name: field.name, value: field.value || "", placeholder: field.placeholder || "",
                     autocomplete: "off", spellcheck: "false" })));
    }
  }, [{ label: "Отмена", value: null },
      { label: "Сохранить", kind: "primary", value: body => Object.fromEntries([...body.querySelectorAll("input")].map(i => [i.name, i.value])) }]);
}

/* sparkline: массив чисел → SVG-линия (мини-график карточки). */
function sparkline(values, color) {
  const points = values.filter(v => v !== null && v !== undefined);
  // SVG — в своём пространстве имён (document.createElement("svg") браузер не рисует).
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 200 34");
  svg.setAttribute("preserveAspectRatio", "none");
  if (points.length < 2) {
    svg.innerHTML = `<line x1="0" y1="30" x2="200" y2="30" stroke="${color}" stroke-opacity=".35" stroke-width="2" stroke-dasharray="4 5"/>`;
    return svg;
  }
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const coords = points.map((v, i) => `${(i / (points.length - 1)) * 200},${30 - ((v - min) / span) * 26}`).join(" ");
  svg.innerHTML = `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" points="${coords}"/>`;
  return svg;
}

/* ---------- низ меню и панель задач ---------- */
function renderFoot(data) {
  const running = data.pipelines;
  $("#foot-state").innerHTML = "";
  $("#foot-state").append(h("span", { class: `dot ${running ? "on" : "off"}` }), running ? "Конвейеры работают" : "Конвейеры остановлены");
  const active = data.tasks.filter(t => t.status === "running" || t.status === "waiting").length;
  $("#foot-tasks").textContent = active;
  $("#foot-time").textContent = data.time;
}

const STATUS_ICON = { queued: "…", running: "▶", waiting: "?", ok: "✓", failed: "✕", stopped: "■" };

function renderTasks(data) {
  const list = $("#task-list");
  const waiting = data.tasks.filter(t => t.status === "waiting").length;
  const active = data.tasks.filter(t => t.status === "running").length;
  $("#dock-summary").textContent = data.tasks.length
    ? `${data.tasks.length} задач${active ? ` · идёт ${active}` : ""}${waiting ? ` · ждёт ответа ${waiting}` : ""}`
    : "пока ничего не запускали";
  if (App.selectedTask == null && data.tasks.length) App.selectedTask = data.tasks[0].id;
  list.innerHTML = "";
  for (const task of data.tasks) {
    list.append(h("div", { class: `task ${task.status} ${task.id === App.selectedTask ? "sel" : ""}`,
                           onclick: () => { App.selectedTask = task.id; App.poll(); } },
      h("span", { class: "st" }, STATUS_ICON[task.status] || ""), task.title, h("time", {}, task.started)));
  }
  const task = data.tasks.find(t => t.id === App.selectedTask);
  const output = $("#task-output");
  const question = $("#task-question");
  if (!task) { output.textContent = ""; question.hidden = true; return; }
  const chunk = data.lines[String(task.id)];
  if (App.shownTask !== task.id) { output.innerHTML = ""; App.shownTask = task.id; }
  if (chunk) {
    const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    const fragment = document.createDocumentFragment();
    for (const parts of chunk.lines) {
      for (const [text, code] of parts) fragment.append(code ? h("span", { class: `c${code}` }, text) : document.createTextNode(text));
      fragment.append("\n");
    }
    output.append(fragment);
    App.cursors[task.id] = chunk.cursor;
    if (atBottom) output.scrollTop = output.scrollHeight;
  }
  question.innerHTML = "";
  question.hidden = !(task.question || task.status === "running" || task.status === "waiting");
  if (task.question) {
    question.append(h("b", {}, task.question.prompt));
    if (task.question.options.length) {
      task.question.options.forEach((option, index) => question.append(
        h("button", { class: `btn small ${index === 0 ? "primary" : ""}`, onclick: () => answer(task.id, option.key) }, option.label)));
    } else {
      question.append(h("button", { class: "btn small primary", onclick: () => answer(task.id, "") }, "Продолжить"));
    }
  }
  if (task.status === "running" || task.status === "waiting") {
    question.append(h("span", { class: "spacer" }),
      h("button", { class: "btn small danger", onclick: () => App.call("task_stop", task.id) }, "Остановить задачу"));
  }
}

async function answer(id, key) {
  await App.call("task_answer", id, key);
  App.poll();
}

/* ---------- запуск ---------- */
function boot() {
  document.querySelectorAll(".nav").forEach(nav => nav.addEventListener("click", () => App.go(nav.dataset.page)));
  window.addEventListener("hashchange", () => App.show(location.hash.slice(1) || "overview"));
  $("#dock-head").addEventListener("click", () => {
    const dock = $("#dock");
    dock.classList.toggle("collapsed");
    $("#dock-toggle").textContent = dock.classList.contains("collapsed") ? "развернуть ▴" : "свернуть ▾";
  });
  App.call("start_page").then(page => App.show(location.hash.slice(1) || page || "overview"));
  App.poll();
  setInterval(() => App.poll(), 1000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
