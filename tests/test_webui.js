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
function fixture(desktop = false) {
  const nodes = new Map(), events = {}, requests = [], dialogs = [], downloads = [], paints = [];
  let answer = () => Promise.resolve({}), confirmation = () => Promise.resolve(true);
  function element(tag) {
    const attrs = {}, listeners = {};
    return {
      tag, children: [], dataset: {}, listeners,
      set textContent(value) { this.text = value; this.children = []; },
      get textContent() { return this.text || ""; },
      setAttribute(key, value) { attrs[key] = String(value); },
      getAttribute(key) { return attrs[key]; },
      hasAttribute(key) { return key === "href" ? !!this.href : key in attrs; },
      removeAttribute(key) { delete attrs[key]; delete this[key]; },
      append(...items) { this.children.push(...items); },
      addEventListener(type, fn) { listeners[type] = fn; },
      querySelectorAll() { return this.children; },
      querySelector() { return element("span"); },
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
    querySelectorAll: () => [], addEventListener() {}
  };
  function request(method, endpoint, body) {
    requests.push({method, endpoint, body});
    return answer(method, endpoint, body);
  }
  const window = {
    matchMedia: query => ({get matches() { return query.includes("min-width") ? desktop : query.includes("coarse") && !desktop; }}),
    NASpeedPrefs:{get: () => null}, cancelAnimationFrame() {}, scrollTo() {},
    setTimeout() {}, clearTimeout() {}, addEventListener(type, fn) { events[type] = fn; }
  };
  const context = {
    window, document, Intl, URL:{createObjectURL(blob){downloads.push(blob);return "blob:csv";}},
    Blob, request, paint(data) { paints.push(data); },
    confirm(title, body) { dialogs.push(body); return confirmation(); },
    fetch(url, options) { return request(options.method, url, JSON.parse(options.body)); }
  };
  vm.runInNewContext(web("i18n.js"), context);
  vm.runInNewContext(web("app.js").replace("  init();", `
    api = request;
    renderTest = renderRoute = renderControls = function () {};
    renderHistory = function () { historyLanguage = lang; paint(state.history); };
    confirmDialog = confirm;
    window.testUI = { state, bind, testEnded, loadHistory, exportCsv, clearHistory, showView, renderHistoryList,
      renderServerRows, drawSkyline, loadDetails, startTest, renderStatus, modelFromLive };
  `), context);
  const ui = window.testUI;
  ui.bind();
  return {ui, state:ui.state, node:id => document.getElementById(id), document, events, requests,
    dialogs, downloads, paints, api(fn) { answer = fn; }, confirmWith(fn) { confirmation = fn; },
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
  assert.match(csv, /ookla/);
  assert.match(csv, /20,16,22/);
  assert.doesNotMatch(csv, /librespeed/);
  assert.equal(f.node("h-download").hidden, false);
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

(async () => {
  await historyActions();
  await loadingAndDetails();
  await pickingAndOwnership();
  await serverLookupConsent();
  await chartAndWarning();
  await hiddenHistoryAndThumbnails();
  console.log("Web UI regression checks passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
