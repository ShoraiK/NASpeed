// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const web = name => fs.readFileSync(path.join(__dirname, "..", "webui", name), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

// Run the real handlers without a browser or network. Only the unrelated board
// painting and the modal's user response are replaced in the isolated VM.
function fixture(options = false) {
  const opts = typeof options === "boolean" ? { desktop: options } : (options || {});
  let desktop = !!opts.desktop;
  const nodes = new Map(), events = {}, requests = [], dialogs = [], downloads = [], paints = [], copied = [];
  let testRenders = 0;
  let answer = () => Promise.resolve({}), confirmation = () => Promise.resolve(true);
  function element(tag) {
    const attrs = {}, listeners = {};
    return {
      tag, value: "", children: [], dataset: {}, listeners, isConnected: true,
      classList: { add() {}, remove() {} },
      closest() { return this; },
      contains(node) { return this.children.includes(node); },
      remove() {},
      replaceWith() {},
      set textContent(value) { this.text = value; this.children = []; },
      get textContent() { return this.text || ""; },
      setAttribute(key, value) { attrs[key] = String(value); },
      getAttribute(key) { return attrs[key]; },
      hasAttribute(key) { return key === "href" ? !!this.href : key in attrs; },
      removeAttribute(key) { delete attrs[key]; delete this[key]; },
      append(...items) { this.children.push(...items); },
      addEventListener(type, fn) { listeners[type] = fn; },
      querySelectorAll() { return this.children; },
      querySelector(selector) {
        if (selector && selector.includes("radio") && this.children.length) {
          return this.children[0];
        }
        return element("span");
      },
      cloneNode() { return element(tag); },
      close() { this.open = false; },
      showModal() { this.open = true; },
      click() { if (listeners.click) listeners.click({target:this}); },
      focus() { document.activeElement = this; },
      getBoundingClientRect() { return {left:0, width:1000}; }
    };
  }
  const document = {
    cookie: "", body: {},
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element("div"));
      return nodes.get(id);
    },
    createElement: element, createElementNS: (_, tag) => element(tag),
    querySelector: () => element("div"), querySelectorAll: () => [], addEventListener() {}
  };
  function request(method, endpoint, body) {
    requests.push({method, endpoint, body});
    return answer(method, endpoint, body);
  }
  const window = {
    matchMedia: query => ({get matches() { return query.includes("min-width") ? desktop : query.includes("coarse") && !desktop; }}),
    NASpeedPrefs:{get: () => null, speedUnit: () => speedPref, set: (_k, v) => { speedPref = v || ""; }},
    IntersectionObserver: opts.IntersectionObserver,
    performance: { now: () => 0 },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {}, scrollTo() {},
    setTimeout() {}, clearTimeout() {}, addEventListener(type, fn) { events[type] = fn; }
  };
  let speedPref = "";
  const context = {
    window, document, Intl, URL:{createObjectURL(blob){downloads.push(blob);return "blob:csv";}},
    Blob, request, paint(data) { paints.push(data); }, countRender() { testRenders += 1; },
    navigator: { clipboard: { writeText(text) { copied.push(text); return Promise.resolve(); } } },
    confirm(title, body) { dialogs.push(body); return confirmation(); },
    fetch(url, options) { return request(options.method, url, JSON.parse(options.body)); }
  };
  for (const name of ["i18n.js", "format.js", "api.js", "charts.js", "history.js", "settings.js"]) {
    vm.runInNewContext(web(name), context);
  }
  vm.runInNewContext(web("app.js").replace("  init();", `
    const realConfirmDialog = confirmDialog;
    const realRenderControls = renderControls;
    const realRenderHistory = renderHistory;
    api = request;
    renderTest = countRender;
    renderRoute = renderControls = function () {};
    renderHistory = function () { historyLanguage = lang; paint(state.history); };
    confirmDialog = confirm;
    window.testUI = { state, bind, testEnded, loadHistory, exportCsv, clearHistory, showView, renderHistoryList,
      renderServerRows, drawSkyline, loadDetails, startTest, renderStatus, modelFromLive,
      buildProviders, realConfirmDialog, realRenderControls, realRenderHistory, openServers,
      setSpeedUnit, fmtSpeedUnit, renderDirection,
      buildSettings: () => settingsUi.buildSettings(),
      pickChart: (index, keep) => historyUi.pickChart(index, keep),
      clearChartPick: () => historyUi.clearChartPick(),
      renderProviderTable: items => historyUi.renderProviderTable(items),
      setFilter: id => historyUi.setFilter(id) };
  `), context);
  const ui = window.testUI;
  ui.bind();
  return {ui, state:ui.state, window, node:id => document.getElementById(id), document, events, requests,
    dialogs, downloads, paints, copied, get testRenders() { return testRenders; },
    api(fn) { answer = fn; }, confirmWith(fn) { confirmation = fn; },
    setDesktop(value) { desktop = value; }};
}

function snapshot(revision, items) {
  return {revision, items, count:items.length, limit:1000, enabled:true};
}
const old = {id:"first", ts:123, provider:"librespeed", download:{mbps:10}, upload:{mbps:8}};
const fresh = {id:"second", ts:124, provider:"ookla", download:{mbps:20, peak_mbps:22}, upload:{mbps:16}};

async function historyActions() {
  const f = fixture();
  f.state.view = "history";
  f.state.history = snapshot("old", [old]);
  let finish, failFirst = true;
  f.api(async (method, endpoint) => {
    if (endpoint === "/history?summary=1") return new Promise(resolve => { finish = resolve; });
    if (endpoint === "/history?limit=1") return snapshot("new", [fresh]);
    if (endpoint === "/history") return snapshot("new", [fresh, old]);
    if (endpoint === "/history/clear" && failFirst) {
      failFirst = false;
      throw {code:"history_changed"};
    }
    return {};
  });
  f.ui.testEnded();
  assert.equal(f.state.history.items.length, 1);
  f.ui.clearHistory();
  f.ui.exportCsv();
  assert.equal(f.dialogs.length, 0);
  assert.equal(f.downloads.length, 0);
  finish(snapshot("new", [fresh, old]));
  await flush();
  assert.equal(f.paints.at(-1).items.length, 2);
  f.state.historyFilter = "ookla";
  f.ui.exportCsv();
  await flush();
  const csv = await f.downloads[0].text();
  assert.equal(csv.trim().split("\r\n").length, 2);
  assert.match(csv, /download_measured_bytes,upload_measured_bytes/);
  assert.match(csv, /ookla/);
  assert.match(csv, /20,16,22/);
  assert.doesNotMatch(csv, /librespeed/);
  assert.equal(f.node("h-download").hidden, false);
  assert.equal(f.node("h-export").hidden, true);
  f.ui.clearHistory();
  await flush();
  assert.match(f.dialogs[0], /2 results/);
  assert.equal(f.requests.find(r => r.endpoint === "/history/clear").body.revision, "new");
  finish(snapshot("newer", [fresh, old, {id:"third"}]));
  await flush();
  assert.equal(f.dialogs.length, 1);
  assert.match(f.node("h-feedback-text").textContent, /changed/);
  f.ui.clearHistory();
  assert.match(f.dialogs[1], /3 results/);
  await flush();
}

async function loadingAndDetails() {
  const f = fixture();
  f.api(async () => { throw {code:"offline"}; });
  await f.ui.loadHistory();
  assert.equal(f.state.history, null);
  assert.match(f.state.historyError, /could not be loaded/);
  assert.equal(f.node("h-retry").hidden, false);
  const newData = snapshot("revision", [fresh, old]);
  f.api(async () => newData);
  f.node("h-retry").click();
  await flush();
  assert.equal(f.state.history, newData);
  assert.equal(f.node("h-retry").hidden, true);
  const details = await Promise.all(f.ui.loadDetails(["first", "second"]));
  assert.deepEqual(Array.from(details, entry => entry.id), ["first", "second"]);
  await Promise.all(f.ui.loadDetails(["second"]));
  assert.equal(f.requests.filter(r => r.endpoint.includes("ids=")).length, 1);
  // A late request cannot overwrite a newer history or resurrect a deleted result.
  let earlier;
  f.api(() => new Promise(resolve => { earlier = resolve; }));
  const pending = f.ui.loadHistory();
  f.api(async () => snapshot("reduced", [old]));
  await f.ui.loadHistory();
  earlier(newData);
  await pending;
  assert.equal(f.state.history.revision, "reduced");
  await assert.rejects(f.ui.loadDetails(["second"])[0], e => e.code === "history_changed");
}

async function pickingAndOwnership() {
  const f = fixture();
  const provider = {id:"local"};
  const list = f.node("servers-list"), dlg = f.node("dlg-servers");
  f.ui.renderServerRows(provider, [{id:"a", name:"A"}, {id:"b", name:"B"}], "");
  dlg.open = true;
  list.listeners.keydown({key:"ArrowDown", target:list.children[0], preventDefault(){}});
  assert.equal(dlg.open, true);
  assert.equal(f.document.activeElement, list.children[1]);
  assert.equal(f.state.serverChoice.local, undefined);
  list.children[1].click();
  assert.equal(dlg.open, false);
  assert.equal(f.state.serverChoice.local.id, "a");
  f.ui.renderServerRows(provider, [{id:"b", name:"B"}], "");
  assert.equal(list.children.filter(row => row.tabIndex === 0).length, 1);
  assert.equal(list.children[0].getAttribute("aria-checked"), "false");

  f.state.running = true;
  f.state.test = {id:"observed"};
  f.events.pagehide();
  assert.equal(f.requests.length, 0);
  f.state.startedTestId = "previous";
  f.events.pagehide();
  assert.equal(f.requests.length, 0);
  f.state.startedTestId = "observed";
  f.events.pagehide();
  assert.equal(f.requests[0].body.id, "observed");
  assert.ok(f.requests[0].endpoint.endsWith("/test/stop"));
}

async function serverLookupConsent() {
  const f = fixture();
  f.state.providers = [{id:"mlab", notice:"mlab_privacy", selectable_servers:true}];
  f.state.providerId = "mlab";
  f.confirmWith(() => Promise.resolve(false));
  f.node("server-change").click();
  await flush();
  assert.equal(f.requests.length, 0);
  assert.equal(f.dialogs.length, 1);
  f.confirmWith(() => Promise.resolve(true));
  f.node("server-change").click();
  await flush();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].method, "POST");
  assert.equal(f.requests[0].endpoint, "/servers?provider=mlab");
  assert.equal(f.requests[0].body.accept_lookup, true);
  assert.equal(f.state.settings.accepted_notices, undefined);
  f.node("dlg-servers").close();
  f.node("server-change").click();
  await flush();
  assert.equal(f.dialogs.length, 2);
  assert.equal(f.requests.length, 2);
}

async function chartAndWarning() {
  const f = fixture();
  const many = Array.from({length:1000}, (_, index) => ({...old, id:String(index)}));
  f.ui.drawSkyline(many);
  const chart = f.node("h-skyline");
  assert.equal(chart.children.filter(item => item.tag === "path").length, 2);
  assert.ok(chart.children.length < 10);
  assert.equal(f.node("h-chart-step").hidden, false);
  chart.listeners.keydown({key:"ArrowLeft", preventDefault(){}});
  assert.equal(f.state.chartIndex, 998);
  chart.children.at(-1).listeners.click({clientX:267.5});
  assert.equal(f.state.chartIndex, 267);
  f.node("h-chart-next").click();
  assert.equal(f.state.chartIndex, 268);
  assert.equal(f.state.chartPicked, many[999 - 268].id);
  // A finished test keeps its measurements even when persistence failed.
  const test = {status:"done", save_error:"storage_error", saved:false, result:fresh};
  const model = f.ui.modelFromLive(test);
  f.node("progress").style = {};
  f.ui.renderStatus(model);
  assert.match(f.node("status-sub").textContent, /could not be saved/);
  f.state.test = test;
  f.api(async () => snapshot("old", [old]));
  f.ui.testEnded();
  await flush();
  assert.match(f.node("announce").textContent, /could not be saved/);
}

async function hiddenHistoryAndThumbnails() {
  for (const desktop of [false, true]) {
    const f = fixture(desktop);
    const data = snapshot("latest", [old, fresh]);
    let respond;
    f.api((method, endpoint) => {
      if (endpoint === "/history?summary=1") return new Promise(resolve => { respond = resolve; });
      return Promise.resolve(data);
    });
    f.ui.showView("history");
    f.ui.showView("settings");
    respond(data);
    await flush();
    assert.equal(f.paints.filter(result => result === data).length, 0);
    f.ui.showView("history");
    assert.equal(f.paints.at(-1), data);
    respond(data);
    await flush();
    f.state.historyPage = 2;
    f.ui.showView("settings");
    f.ui.showView("history");
    assert.equal(f.state.historyPage, 1);
    assert.equal(f.requests.filter(r => r.endpoint.includes("ids=")).length, desktop ? 1 : 0);
    respond(data);
    await flush();
    if (!desktop) {
      f.ui.showView("settings");
      f.setDesktop(true);
      f.ui.showView("history");
      assert.equal(f.requests.filter(r => r.endpoint.includes("ids=")).length, 1);
      respond(data);
      await flush();
    }
  }
}

async function reentrantConfirmAndProviderKeys() {
  const f = fixture();
  const first = f.ui.realConfirmDialog("One", "First", "OK");
  assert.equal(f.node("dlg-confirm").open, true);
  const second = f.ui.realConfirmDialog("Two", "Second", "OK", true);
  assert.equal(await first, false);
  f.node("dlg-confirm-ok").click();
  assert.equal(await second, true);

  f.state.providers = [{id:"a", name:"A"}, {id:"b", name:"B"}];
  f.state.providerId = "a";
  f.ui.buildProviders();
  const providers = f.node("providers");
  providers.listeners.keydown({key:"ArrowRight", target:providers.children[0], preventDefault(){}});
  assert.equal(f.state.providerId, "b");
  assert.equal(f.document.activeElement, providers.children[1]);
}

async function optimizationsAndModules() {
  // 2.6: drawSkyline caches chart bounding rect during hover moves until resize/leave.
  const f1 = fixture();
  const chart = f1.node("h-skyline");
  let rectCalls = 0;
  chart.getBoundingClientRect = () => { rectCalls += 1; return { left: 0, width: 1000 }; };
  f1.ui.drawSkyline([old, fresh]);
  const surface = chart.children.at(-1);
  surface.listeners.pointerenter();
  surface.listeners.pointermove({ clientX: 100 });
  surface.listeners.pointermove({ clientX: 600 });
  surface.listeners.pointermove({ clientX: 900 });
  assert.equal(rectCalls, 1);
  f1.events.resize();
  surface.listeners.pointermove({ clientX: 250 });
  assert.equal(rectCalls, 2);

  // 2.7: IntersectionObserver defers detail fetches to visible history rows.
  const observers = [];
  class FakeObserver {
    constructor(cb) {
      this.cb = cb;
      this.observed = [];
      this.unobserved = [];
      this.disconnected = false;
      observers.push(this);
    }
    observe(target) { this.observed.push(target); }
    unobserve(target) { this.unobserved.push(target); }
    disconnect() { this.disconnected = true; }
    trigger(entries) { this.cb(entries, this); }
  }
  const third = { id: "third", ts: 125, provider: "cloudflare", download: { mbps: 30 }, upload: { mbps: 25 } };
  const f2 = fixture({ desktop: true, IntersectionObserver: FakeObserver });
  f2.state.view = "history";
  f2.api(async () => ({ items: [third, fresh] }));
  f2.ui.renderHistoryList([third, fresh, old]);
  const obs = observers.at(-1);
  assert.equal(obs.observed.length, 3);
  assert.equal(f2.requests.length, 0);
  obs.trigger([
    { target: obs.observed[0], isIntersecting: true },
    { target: obs.observed[1], isIntersecting: true },
    { target: obs.observed[2], isIntersecting: false }
  ]);
  await flush();
  assert.equal(obs.unobserved.length, 2);
  assert.equal(f2.requests.length, 1);
  assert.equal(f2.requests[0].endpoint, "/history?ids=third,second");
  f2.ui.showView("settings");
  assert.equal(obs.disconnected, true);

  // 2.8: saveSetting updates controls in place without rebuilding DOM or rendering hidden Test view.
  const f3 = fixture();
  f3.state.info = {
    version: "1.0.1",
    options: { durations: { quick: 5, standard: 8 }, connections: ["auto", "4"], ip_versions: ["auto", "4"], history_max: 1000 }
  };
  f3.state.settings = { duration: "standard", connections: "auto", ip_version: "auto", save_history: true, history_limit: 300 };
  f3.state.view = "settings";
  f3.ui.buildSettings();
  const quickBtn = f3.node("set-duration").children[0];
  f3.api(async () => ({ ...f3.state.settings, duration: "quick" }));
  quickBtn.click();
  await flush();
  assert.equal(f3.node("set-duration").children[0], quickBtn);
  assert.equal(quickBtn.getAttribute("aria-checked"), "true");
  assert.equal(f3.testRenders, 0);

  // Toggling picked chart column restores medians cleanly.
  f3.ui.drawSkyline([old, fresh]);
  f3.ui.pickChart(0);
  assert.equal(f3.state.chartPicked, fresh.id);
  f3.ui.pickChart(0);
  assert.equal(f3.state.chartPicked, null);
}

async function uiFixesPoint3() {
  // 3.1 & 3.2: chart cursor stays hidden without selection, shows picked bar context & reset button.
  const f = fixture();
  f.state.view = "history";
  f.ui.drawSkyline([old, fresh]);
  assert.equal(f.node("h-chart-cursor").getAttribute("visibility"), "hidden");
  assert.equal(f.node("h-chart-reset").hidden, true);
  assert.equal(f.node("h-chart-note").dataset.picked, undefined);
  f.ui.pickChart(0);
  assert.equal(f.node("h-chart-cursor").getAttribute("visibility"), "visible");
  assert.equal(f.node("h-chart-reset").hidden, false);
  assert.equal(f.node("h-chart-note").dataset.picked, "true");
  assert.match(f.node("h-chart-note").textContent, /ookla/);
  f.node("h-chart-reset").click();
  assert.equal(f.state.chartPicked, null);
  assert.equal(f.node("h-chart-cursor").getAttribute("visibility"), "hidden");
  assert.equal(f.node("h-chart-reset").hidden, true);
  assert.equal(f.node("h-chart-note").dataset.picked, undefined);

  // 3.3 & 3.4: leaving History resets CSV button; historyFailed writes to #h-feedback, not #h-limit.
  f.node("h-download").hidden = false;
  f.node("h-export").hidden = true;
  f.ui.showView("test");
  assert.equal(f.node("h-download").hidden, true);
  assert.equal(f.node("h-export").hidden, false);

  f.state.view = "history";
  f.state.history = snapshot("r1", [fresh, old]);
  f.node("h-limit").textContent = "Up to 300 are kept.";
  f.confirmWith(() => Promise.resolve(true));
  f.api(async (_m, ep) => {
    if (ep === "/history/clear") throw { code: "storage_error" };
    return {};
  });
  f.ui.clearHistory();
  await flush();
  assert.equal(f.node("h-limit").textContent, "Up to 300 are kept.");
  assert.equal(f.node("h-feedback").hidden, false);
  assert.ok(f.node("h-feedback-text").textContent.length > 0);

  // 3.5: lowering history_limit below historyCount prompts before trimming.
  f.state.info = { options: { history_max: 1000 } };
  f.state.settings = { duration: "standard", connections: "auto", ip_version: "auto", save_history: true, history_limit: 300 };
  f.state.historyCount = 120;
  f.requests.length = 0;
  f.dialogs.length = 0;
  f.confirmWith(() => Promise.resolve(false));
  f.node("set-limit").listeners.change({ target: { value: "50" } });
  await flush();
  assert.equal(f.dialogs.length, 1);
  assert.match(f.dialogs[0], /70/);
  assert.equal(f.requests.length, 0);
  assert.equal(f.node("set-limit").value, "300");

  f.confirmWith(() => Promise.resolve(true));
  f.api(async (_m, _ep, body) => ({ ...f.state.settings, ...body }));
  f.node("set-limit").listeners.change({ target: { value: "50" } });
  await flush();
  assert.equal(f.dialogs.length, 2);
  assert.equal(f.requests[0].endpoint, "/settings");
  assert.equal(f.requests[0].body.history_limit, 50);
  assert.equal(f.state.historyCount, 50);
  assert.equal(f.state.history, null);

  f.node("set-limit").listeners.change({ target: { value: "400" } });
  await flush();
  assert.equal(f.dialogs.length, 2);
  assert.equal(f.requests[1].body.history_limit, 400);

  // 3.7: openServers focuses #servers-q when searchable, else first server row.
  f.state.providers = [
    { id: "ookla", selectable_servers: true, searchable: true },
    { id: "librespeed", selectable_servers: true, searchable: false }
  ];
  f.state.providerId = "ookla";
  f.api(async () => ({ servers: [] }));
  f.ui.openServers();
  assert.equal(f.document.activeElement, f.node("servers-q"));
  f.node("dlg-servers").close();
  f.state.providerId = "librespeed";
  f.ui.openServers();
  assert.equal(f.document.activeElement, f.node("servers-list").children[0]);
  await flush();

  // 3.8: starting label + aria-busy, disabled pager buttons, speed units on list rows.
  f.state.starting = true;
  f.state.running = false;
  f.ui.realRenderControls();
  assert.equal(f.node("action").textContent, "Starting…");
  assert.equal(f.node("action").getAttribute("aria-busy"), "true");
  assert.equal(f.node("action").disabled, true);

  const pageItems = Array.from({ length: 12 }, (_, i) => ({ ...old, id: "p" + i }));
  f.ui.renderHistoryList(pageItems);
  assert.equal(f.node("h-page-prev").disabled, true);
  assert.equal(f.node("h-page-prev").getAttribute("aria-disabled"), undefined);
  assert.equal(f.node("h-page-next").disabled, false);
  const firstRowSummary = f.node("h-list").children[1].children[0];
  assert.equal(firstRowSummary.children[3].dataset.unit, "Mbps");
  assert.equal(firstRowSummary.children[4].dataset.unit, "Mbps");
}

async function uiImprovementsPoint4() {
  // 4.1 & 4.6: pinned status offers back-to-latest + copy summary, and copies via clipboard.
  const f = fixture();
  const saved = {
    ...fresh,
    download: { mbps: 2350, bytes: 120000000, measured_bytes: 100000000, series: [2000, 2350] },
    upload: { mbps: 450, bytes: 30000000, measured_bytes: 25000000, series: [400, 450] },
    ping: { latency: 11.5, jitter: 1.2, low: 10, high: 13, values: [11, 12] }
  };
  f.state.pinned = saved;
  f.node("progress").style = {};
  f.ui.renderStatus({
    kind: "saved", status: "done", phase: "done", progress: 1,
    providerName: "Ookla", server: { sponsor: "ISP", name: "Madrid" },
    down: { value: 2350 }, up: { value: 450 }, ping: saved.ping, ts: 124, elapsed: 18
  });
  assert.equal(f.node("status-actions").hidden, false);
  assert.equal(f.node("status-unpin").hidden, false);
  assert.equal(f.node("status-copy").hidden, false);
  assert.equal(f.node("status-actions").dataset.multi, "true");
  f.state.model = {
    kind: "saved", status: "done", phase: "done", providerName: "Ookla",
    server: { sponsor: "ISP", name: "Madrid" }, down: { value: 2350 }, up: { value: 450 },
    ping: saved.ping, ts: 124
  };
  f.node("status-copy").click();
  await flush();
  assert.equal(f.copied.length, 1);
  assert.match(f.copied[0], /Ookla/);
  assert.match(f.copied[0], /2,350 Mbps/);
  assert.match(f.copied[0], /450\.0 Mbps/);
  assert.equal(f.node("status-copy").textContent, "Copied");

  f.node("status-unpin").click();
  assert.equal(f.state.pinned, null);

  // 4.4: live running status highlights active tile and shows progress/remaining time.
  f.ui.renderStatus({
    kind: "live", status: "running", phase: "download", progress: 0.52,
    providerName: "Ookla",
    down: { measuring: true, domain: 10, points: [[0, 0], [3.2, 500]] },
    up: null
  });
  assert.equal(f.node("status-actions").dataset.multi, "false");
  assert.equal(f.node("tile-down").dataset.active, "true");
  assert.equal(f.node("tile-up").dataset.active, "false");
  assert.equal(f.node("tile-ping").dataset.active, "false");
  assert.match(f.node("status-sub").textContent, /52%.*~7 s/);

  // 4.3: server picker shows busy state, retry on failure, and refresh reloads list.
  const provider = { id: "ookla", selectable_servers: true, searchable: true };
  f.state.providers = [provider];
  f.state.providerId = "ookla";
  f.ui.renderServerRows(provider, [], "loading");
  assert.equal(f.node("servers-list").getAttribute("aria-busy"), "true");
  assert.equal(f.node("servers-refresh").disabled, true);
  f.ui.renderServerRows(provider, [], "failed");
  assert.equal(f.node("servers-refresh").disabled, false);
  const failedBox = f.node("servers-list").children[1];
  assert.equal(failedBox.className, "rows-status");
  f.requests.length = 0;
  f.api(async () => ({ servers: [] }));
  f.node("servers-refresh").click();
  await flush();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].endpoint, "/servers?provider=ookla");

  // 4.5: clicking a provider in median table filters history and shows clear filter chip.
  f.state.view = "history";
  f.state.history = snapshot("r4", [saved, old]);
  f.ui.realRenderHistory();
  assert.equal(f.node("h-filter-clear").hidden, true);
  const tableRows = f.node("h-table-body").children;
  const ooklaBtn = tableRows[0].children[0].children[0];
  ooklaBtn.click();
  assert.equal(f.state.historyFilter, "ookla");
  f.ui.realRenderHistory();
  assert.equal(f.node("h-filter-clear").hidden, false);
  assert.equal(f.node("h-table-body").children[0].dataset.active, "true");
  f.node("h-filter-clear").click();
  assert.equal(f.state.historyFilter, "");

  // 4.7: automatic Gbps speed unit formatting and measured_bytes CSV export.
  f.ui.setSpeedUnit("auto");
  assert.equal(f.ui.fmtSpeedUnit(2350), "2.35 Gbps");
  assert.equal(f.ui.fmtSpeedUnit(450), "450.0 Mbps");
  f.ui.renderDirection("down", { status: "done", down: { value: 2350, done: true, peak: 2500, bytes: 120000000, points: [[0, 0], [1, 2350]], domain: 1, start: 0 } }, true);
  f.requests.length = 0;
  f.api(async () => ({ items: [saved] }));
  f.ui.exportCsv();
  await flush();
  const csv = await f.downloads.at(-1).text();
  assert.match(csv, /download_measured_bytes,upload_measured_bytes/);
  assert.match(csv, /120000000,30000000,100000000,25000000/);
}

(async () => {
  await historyActions();
  await loadingAndDetails();
  await pickingAndOwnership();
  await serverLookupConsent();
  await chartAndWarning();
  await hiddenHistoryAndThumbnails();
  await reentrantConfirmAndProviderKeys();
  await optimizationsAndModules();
  await uiFixesPoint3();
  await uiImprovementsPoint4();
  console.log("Web UI regression checks passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
