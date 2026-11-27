// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - web UI coordinator.
 *
 * The test itself runs on the NAS. This page only starts it, polls its state
 * through the TOS proxy and draws what comes back. */
(() => {
  "use strict";

  const APP_ID = "naspeed";
  const API = "/v2/proxy/" + APP_ID + "/api";
  // The TOS desktop and its login page. The web root only loads this address.
  const TOS_LOGIN_URL = "/tos/";
  const POLL_MS = 110;
  const PAGE_SIZES = [10, 25, 50, 100]; // results a page of the History list can hold
  const VIEWS = ["test", "history", "settings"];

  const I18N = window.NASpeedI18n;
  const prefs = window.NASpeedPrefs;
  const startup = window.NASpeedStartup;
  const format = window.NASpeedFormat;
  const apiMod = window.NASpeedApi;
  const chartsMod = window.NASpeedCharts;
  const historyMod = window.NASpeedHistory;
  const settingsMod = window.NASpeedSettings;

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");
  const showMiniTraces = window.matchMedia("(min-width: 1001px)");
  const touchScreen = window.matchMedia("(any-pointer: coarse)");

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function externalLink(text, href) {
    const link = el("a", "", text);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function svgEl(tag, className) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (className) {
      node.setAttribute("class", className);
    }
    return node;
  }

  function savedPageSize() {
    const size = parseInt(prefs.get("historyPageSize"), 10);
    return PAGE_SIZES.indexOf(size) >= 0 ? size : PAGE_SIZES[0];
  }

  const state = {
    info: null,
    settings: {},
    providers: [],
    providerId: "",
    serverChoice: {}, // provider id -> {id, label, sub}
    confirmedLookups: {}, // permission to contact the catalogue in this tab
    serversToken: 0,
    view: "test",
    running: false,
    starting: false,
    stopping: false,
    startedTestId: null,
    startWarning: "",
    stopError: "",
    test: null, // live state of the test started or followed from this page
    pinned: null, // a saved result opened from the history
    lastResult: null,
    lastShown: null, // lastResult set aside while a test starts, back if the start fails
    model: null,
    history: null,
    historyRequest: 0,
    historyLoading: false,
    historyError: "",
    historyNotice: "",
    exporting: false,
    historyCount: 0,
    historyFilter: "",
    historyPage: 1,
    historyPageSize: savedPageSize(),
    chartItems: [],
    chartIndex: -1,
    chartPicked: null,
    gate: "",
    polling: false,
    pollTimer: 0,
    pollFailures: 0,
    announced: ""
  };

  /* ---- Texts and formats ------------------------------------------------ */

  let lang = I18N.fallback;
  let dict = I18N.langs[lang].text;
  const fallbackDict = I18N.langs[I18N.fallback].text;
  let locale = I18N.langs[lang].locale;
  let plural = null;
  let numberFormats = {};
  let dateFormat = null;
  let historyLanguage = "";

  function has(key) {
    return Object.prototype.hasOwnProperty.call(dict, key) || Object.prototype.hasOwnProperty.call(fallbackDict, key);
  }

  function t(key, vars) {
    let text = dict[key];
    if (text === undefined) {
      text = fallbackDict[key];
    }
    if (text === undefined) {
      return key;
    }
    if (!vars) {
      return text;
    }
    return text.replace(/\{(\w+)\}/g, (_, name) => {
      return vars[name] === undefined || vars[name] === null ? "" : String(vars[name]);
    });
  }

  function tn(key, count, vars) {
    let form = "other";
    try {
      if (plural) {
        form = plural.select(count);
      }
    } catch (error) {
      form = count === 1 ? "one" : "other";
    }
    const base = has(key + "." + form) ? key + "." + form : key + ".other";
    const merged = { n: fmtInt(count) };
    if (vars) {
      Object.keys(vars).forEach((name) => {
        merged[name] = vars[name];
      });
    }
    return t(base, merged);
  }

  function number(value, digits) {
    if (!numberFormats[digits]) {
      numberFormats[digits] = new Intl.NumberFormat(locale, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      });
    }
    return numberFormats[digits].format(value);
  }

  function fmtInt(value) {
    return format.isNumber(value) ? number(Math.round(value), 0) : "—";
  }

  function currentSpeedUnit() {
    return prefs.speedUnit ? prefs.speedUnit() : "mbps";
  }

  function isAutoUnit() {
    return currentSpeedUnit() === "auto";
  }

  function useGbps(value) {
    return isAutoUnit() && format.isNumber(value) && value >= 1000;
  }

  function fmtSpeed(value) {
    if (!format.isNumber(value)) {
      return "—";
    }
    if (useGbps(value)) {
      return number(value / 1000, 2);
    }
    if (value >= 1000) {
      return number(value, 0);
    }
    return number(value, value >= 100 ? 1 : 2);
  }

  function fmtSpeedUnit(value) {
    return fmtSpeed(value) + " " + (useGbps(value) ? t("unit.speed.gb") : t("unit.speed"));
  }

  function fmtSpeedTable(value) {
    return isAutoUnit() && format.isNumber(value) ? fmtSpeedUnit(value) : fmtSpeed(value);
  }

  function fmtMs(value) {
    if (!format.isNumber(value)) {
      return "—";
    }
    return number(value, value >= 100 ? 0 : 1);
  }

  function fmtPercent(value) {
    if (!format.isNumber(value)) {
      return "—";
    }
    if (!numberFormats["%"]) {
      numberFormats["%"] = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
    }
    return numberFormats["%"].format(value / 100);
  }

  function fmtBytes(value) {
    if (!format.isNumber(value)) {
      return "—";
    }
    if (value >= 1e9) {
      return number(value / 1e9, 2) + " " + t("unit.gb");
    }
    return number(value / 1e6, value >= 1e8 ? 0 : 1) + " " + t("unit.mb");
  }

  function fmtSeconds(value) {
    if (!format.isNumber(value)) {
      return "—";
    }
    const seconds = Math.round(value);
    if (seconds < 60) {
      return seconds + " s";
    }
    return Math.floor(seconds / 60) + " min " + (seconds % 60) + " s";
  }

  function fmtDate(timestamp) {
    if (!format.isNumber(timestamp)) {
      return "";
    }
    if (!dateFormat) {
      dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
    }
    return dateFormat.format(new Date(timestamp * 1000));
  }

  function matchLang(tag) {
    if (!tag) {
      return "";
    }
    const code = String(tag).toLowerCase().split(/[-_]/)[0];
    return I18N.langs[code] ? code : "";
  }

  /* The language is saved in the NAS settings. The browser keeps a copy only
     to paint the first frame in the right language; with neither, English. */
  function cachedLang() {
    return matchLang(prefs.get("language")) || matchLang(prefs.get("lang")) || I18N.fallback;
  }

  /* Follow the language saved on the NAS. A choice made with a version that
     kept it only in this browser ("lang") is moved to the NAS once. */
  function adoptSavedLanguage() {
    let saved = matchLang(state.settings.language);
    const legacy = matchLang(prefs.get("lang"));
    if (!saved && legacy) {
      saved = legacy;
      api("POST", "/settings", { language: legacy }).then(
        (settings) => {
          state.settings = settings;
          prefs.set("lang", "");
        },
        () => {
          /* Tried once; keeping it locally avoids surprising the user now. */
        }
      );
    } else if (saved && legacy) {
      prefs.set("lang", "");
    }
    saved = saved || I18N.fallback;
    prefs.set("language", saved);
    if (saved !== lang) {
      applyLanguage(saved);
      renderAll();
    }
  }

  function applyLanguage(code) {
    lang = I18N.langs[code] ? code : I18N.fallback;
    dict = I18N.langs[lang].text;
    locale = I18N.langs[lang].locale;
    numberFormats = {};
    dateFormat = null;
    try {
      plural = new Intl.PluralRules(locale);
    } catch (error) {
      plural = null;
    }
    document.documentElement.lang = I18N.langs[lang].tag;

    let nodes = document.querySelectorAll("[data-i18n]");
    for (let i = 0; i < nodes.length; i += 1) {
      nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
    }
    nodes = document.querySelectorAll("[data-i18n-attr]");
    for (let i = 0; i < nodes.length; i += 1) {
      const pairs = nodes[i].getAttribute("data-i18n-attr").split(";");
      for (let j = 0; j < pairs.length; j += 1) {
        const parts = pairs[j].split(":");
        if (parts.length === 2) {
          nodes[i].setAttribute(parts[0].trim(), t(parts[1].trim()));
        }
      }
    }
    renderLanguageControls();
  }

  function renderLanguageControls() {
    settingsUi.buildSeg(
      "set-language",
      I18N.order.map((code) => ({ value: code, label: I18N.langs[code].name })),
      lang,
      pickLanguage
    );
  }

  function pickLanguage(code) {
    const focused = $("set-language").contains(document.activeElement);
    applyLanguage(code);
    prefs.set("language", lang);
    renderAll();
    const button = $("set-language").querySelector('[aria-checked="true"]');
    if (focused && button) {
      button.focus(); // built again in the new language; saveSetting keeps it in place
    }
    settingsUi.saveSetting({ language: lang });
  }

  /* Everything that carries text built at run time. */
  function renderAll() {
    charts.resetTweens();
    buildProviders();
    settingsUi.buildSettings();
    renderThemeControls();
    renderUnitControls();
    renderTest();
    if (state.view === "history") {
      renderHistory();
    }
    if (state.gate) {
      showGate(state.gate);
    }
  }

  function announce(text) {
    if (text && text !== state.announced) {
      state.announced = text;
      $("announce").textContent = text;
    }
  }

  /* ---- API -------------------------------------------------------------- */

  const apiClient = apiMod.createClient({
    baseUrl: API,
    getGate: () => state.gate,
    onGate: (kind) => { showGate(kind); }
  });

  function api(method, path, body) {
    return apiClient.request(method, path, body);
  }

  function goToLogin() {
    try {
      window.top.location.href = TOS_LOGIN_URL;
    } catch (error) {
      window.location.href = TOS_LOGIN_URL;
    }
  }

  /* Replaces the application: no session, no permission or no service. */
  function showGate(kind) {
    const first = state.gate !== kind;
    state.gate = kind;
    stopPolling();
    $("gate-title").textContent = t("gate." + kind + ".title");
    $("gate-body").textContent = t("gate." + kind + ".body");
    const button = $("gate-action");
    if (kind === "session") {
      button.hidden = false;
      button.textContent = t("gate.session.go");
      button.onclick = goToLogin;
      if (first) {
        window.setTimeout(goToLogin, 1500);
      }
    } else if (kind === "offline") {
      button.hidden = false;
      button.textContent = t("err.retry");
      button.onclick = () => {
        window.location.reload();
      };
    } else {
      button.hidden = true;
    }
    $("main").inert = true;
    $("gate").hidden = false;
  }

  function errorText(error) {
    if (!error) {
      return t("err.generic");
    }
    const text = has("err." + error.code) ? t("err." + error.code) : t("err.generic");
    return error.detail ? text + " (" + String(error.detail).slice(0, 120) + ")" : text;
  }

  /* ---- Modules ---------------------------------------------------------- */

  function setFacts(id, facts) {
    const list = $(id);
    const clean = facts.filter((item) => !!item);
    const signature = clean.join("|");
    if (list.dataset.sig === signature) {
      return;
    }
    list.dataset.sig = signature;
    list.textContent = "";
    clean.forEach((item) => {
      list.append(el("span", "", item));
    });
  }

  function setPairs(list, pairs) {
    const signature = pairs.map((pair) => pair[0] + "=" + pair[1]).join("|");
    if (list.dataset.sig === signature) {
      return;
    }
    list.dataset.sig = signature;
    list.textContent = "";
    pairs.forEach((pair) => {
      const group = el("div");
      group.append(el("dt", "", pair[0]), el("dd", String(pair[1]).length > 22 ? "long" : "", pair[1]));
      list.append(group);
    });
  }

  function serverLine(server) {
    let label = format.serverLabel(server);
    if (label && format.isNumber(server.distance_km)) {
      label += " · " + t("server.distance", { v: fmtInt(server.distance_km) });
    }
    return label;
  }

  /* Why the NAS, rather than the line, may have set a result: the first reason
     either direction reports. */
  function cpuLimit(model) {
    const directions = [model.down, model.up];
    for (let i = 0; i < directions.length; i += 1) {
      const load = directions[i] && directions[i].cpu;
      if (load && load.limit && has("status.cpu." + load.limit)) {
        return load.limit;
      }
    }
    return null;
  }

  function cpuFacts(load) {
    if (!load) {
      return [];
    }
    return [
      format.isNumber(load.busy) ? t("fact.cpu", { v: fmtPercent(load.busy) }) : "",
      load.limit ? t("fact.limited") : ""
    ];
  }

  const charts = chartsMod.createCharts({
    state,
    $,
    svgEl,
    format,
    t,
    fmtMs,
    fmtSpeed,
    fmtSpeedUnit,
    fmtInt,
    fmtDate,
    reduceMotion,
    useGbps,
    updateChartSteps: () => { updateChartSteps(); },
    onChartPick: (index, keep) => { historyUi.pickChart(index, keep); }
  });

  const historyUi = historyMod.createHistory({
    state,
    PAGE_SIZES,
    $,
    el,
    svgEl,
    format,
    prefs,
    charts,
    reduceMotion,
    showMiniTraces,
    t,
    tn,
    announce,
    errorText,
    fmtInt,
    fmtSpeed,
    fmtSpeedUnit,
    fmtSpeedTable,
    useGbps,
    isAutoUnit,
    copyResult: (button, result) => { copyResult(button, result); },
    fmtMs,
    fmtBytes,
    fmtSeconds,
    fmtDate,
    serverLine,
    setPairs,
    cpuFacts,
    historyChangedError: () => new apiMod.ApiError(409, "history_changed"),
    markSeg: (id, value) => { settingsUi.markSeg(id, value); },
    api: (...args) => api(...args),
    confirmDialog: (...args) => confirmDialog(...args),
    renderHistory: (...args) => renderHistory(...args),
    renderTest: (...args) => renderTest(...args),
    showView: (...args) => showView(...args),
    loadLast: () => loadLast(),
    getLang: () => lang,
    getHistoryLanguage: () => historyLanguage,
    setHistoryLanguage: (value) => { historyLanguage = value; }
  });

  const settingsUi = settingsMod.createSettings({
    state,
    $,
    el,
    externalLink,
    t,
    has,
    fmtInt,
    api: (...args) => api(...args),
    renderTest: (...args) => renderTest(...args)
  });

  /* ---- Theme ------------------------------------------------------------ */

  function currentTheme() {
    return prefs.theme() || (darkScheme.matches ? "dark" : "light");
  }

  function setTheme(value) {
    prefs.set("theme", value);
    prefs.applyTheme(value);
    renderThemeControls();
  }

  function renderThemeControls() {
    $("theme-toggle").setAttribute("aria-label", t(currentTheme() === "dark" ? "theme.toLight" : "theme.toDark"));
    $("theme-toggle").title = $("theme-toggle").getAttribute("aria-label");
    settingsUi.buildSeg(
      "set-theme",
      [
        { value: "", label: t("set.theme.auto") },
        { value: "light", label: t("set.theme.light") },
        { value: "dark", label: t("set.theme.dark") }
      ],
      prefs.theme(),
      setTheme
    );
  }

  function setSpeedUnit(value) {
    const next = value === "auto" ? "auto" : "mbps";
    prefs.set("speedUnit", next === "auto" ? "auto" : "");
    renderUnitControls();
    charts.resetTweens();
    renderTest();
    if (state.view === "history" && state.history) {
      renderHistory();
    }
  }

  function renderUnitControls() {
    settingsUi.buildSeg(
      "set-unit",
      [
        { value: "mbps", label: t("unit.speed") },
        { value: "auto", label: t("set.unit.auto") }
      ],
      currentSpeedUnit(),
      setSpeedUnit
    );
  }

  /* ---- Test view -------------------------------------------------------- */

  function copyText(text) {
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.append(box);
      box.select();
      try {
        if (document.execCommand && document.execCommand("copy")) {
          resolve();
        } else {
          reject(new Error("copy_failed"));
        }
      } catch (error) {
        reject(error);
      } finally {
        box.remove();
      }
    });
  }

  function formatSummary(model) {
    const down = model.down ? fmtSpeedUnit(model.down.value) : "—";
    const up = model.up ? fmtSpeedUnit(model.up.value) : "—";
    const ping = model.ping && format.isNumber(model.ping.latency) ? fmtMs(model.ping.latency) + " ms" : "—";
    const jitter = model.ping && format.isNumber(model.ping.jitter)
      ? t("fact.jitter", { v: fmtMs(model.ping.jitter) })
      : "";
    return [
      format.joined(["NASpeed", fmtDate(model.ts), model.providerName, serverLine(model.server)]),
      t("hist.col.down") + ": " + down + " · " + t("hist.col.up") + ": " + up + " · " +
        t("hist.col.ping") + ": " + format.joined([ping, jitter])
    ].join("\n");
  }

  const copyTimers = new WeakMap();

  function copyWithFeedback(button, text) {
    if (!button || !text) {
      return;
    }
    copyText(text).then(() => {
      window.clearTimeout(copyTimers.get(button) || 0);
      button.textContent = t("action.copied");
      announce(t("action.copied"));
      copyTimers.set(button, window.setTimeout(() => {
        button.textContent = t("action.copy");
      }, 2000));
    }, () => {});
  }

  function copyResult(button, result) {
    copyWithFeedback(button, formatSummary(format.modelFromResult(result, "saved")));
  }

  function liveProgressNote(model) {
    const pct = Math.round(Math.max(0, Math.min(1, model.progress || 0)) * 100);
    const dir = model.phase === "download" ? model.down : model.phase === "upload" ? model.up : null;
    if (dir && dir.measuring && format.isNumber(dir.domain) && dir.points.length) {
      const left = Math.max(0, Math.ceil(dir.domain - dir.points[dir.points.length - 1][0]));
      return t("status.progress.left", { pct: fmtInt(pct), s: fmtInt(left) });
    }
    return pct > 0 ? fmtPercent(pct) : "";
  }

  function modelFromLive(test) {
    return format.modelFromLive(test);
  }

  function currentModel() {
    if (state.test && (state.running || !state.pinned)) {
      return modelFromLive(state.test);
    }
    if (state.pinned) {
      return format.modelFromResult(state.pinned, "saved");
    }
    if (state.lastResult) {
      return format.modelFromResult(state.lastResult, "last");
    }
    return null;
  }

  function renderStatus(model) {
    let title;
    let sub;
    let tone = "";
    let progress = 0;
    if (!model) {
      title = state.starting ? t("status.init") : t("status.ready");
      sub = t("status.ready.sub");
    } else if (model.status === "running") {
      title = has("status." + model.phase) ? t("status." + model.phase) : t("status.init");
      sub = format.joined([model.providerName, serverLine(model.server), liveProgressNote(model), state.startWarning, state.stopError]);
      progress = model.progress;
      announce(title);
    } else if (model.status === "error") {
      title = t("status.error");
      sub = errorText(model.error);
      tone = "alert";
    } else if (model.status === "cancelled") {
      title = t("status.cancelled");
      sub = t("status.cancelled.sub");
    } else if (model.kind === "live") {
      title = t("status.done");
      sub = format.joined([model.providerName, serverLine(model.server),
        t(model.saveError ? "status.saveFailed" : model.saved ? "status.saved" : "status.unsaved")]);
    } else if (model.kind === "last") {
      title = t("status.last");
      sub = format.joined([fmtDate(model.ts), model.providerName, serverLine(model.server)]);
    } else {
      title = fmtDate(model.ts);
      sub = format.joined([model.providerName, serverLine(model.server)]);
    }
    const limit = model && model.status === "done" ? cpuLimit(model) : null;
    if (limit) {
      sub = format.joined([sub, t("status.cpu." + limit)]);
    }
    $("status-title").textContent = title;
    $("status-sub").textContent = sub;
    $("tile-status").dataset.tone = tone;
    $("progress").style.transform = "scaleX(" + Math.max(0, Math.min(1, progress)).toFixed(4) + ")";
    const activePhase = model && model.status === "running" ? model.phase : "";
    [
      ["ping", "ping"],
      ["down", "download"],
      ["up", "upload"]
    ].forEach(([tile, phase]) => {
      $("tile-" + tile).dataset.active = String(activePhase === phase);
    });
    const canUnpin = !!model && model.kind === "saved";
    const canCopy = !!model && model.status === "done";
    $("status-unpin").hidden = !canUnpin;
    $("status-copy").hidden = !canCopy;
    $("status-actions").dataset.multi = String(canUnpin && canCopy);
    $("status-actions").hidden = !canUnpin && !canCopy;
  }

  function renderPing(model, instant) {
    const ping = model && model.ping;
    const lit = !!ping && format.isNumber(ping.latency);
    $("tile-ping").dataset.lit = String(lit);
    if (!lit) {
      charts.setTween("ping", null, true);
      return;
    }
    charts.setTween("ping", ping.latency, instant);
    const facts = [
      t("fact.jitter", { v: fmtMs(ping.jitter) }),
      t("fact.range", { low: fmtMs(ping.low), high: fmtMs(ping.high) })
    ];
    if (ping.loss > 0 && ping.loss < 1 && ping.count) {
      facts.push(tn("fact.lost", Math.round((ping.count * ping.loss) / (1 - ping.loss))));
    }
    setFacts("ping-facts", facts);
  }

  function renderDirection(kind, model, instant) {
    const direction = model && model[kind];
    $("tile-" + kind).dataset.lit = String(!!direction);
    if (!direction) {
      charts.setTween(kind, null, true);
      return;
    }
    const caption = $("tile-" + kind).querySelector(".caption");
    if (caption) {
      caption.dataset.i18n = useGbps(direction.value) ? kind + ".caption.gb" : kind + ".caption";
      caption.textContent = t(caption.dataset.i18n);
    }
    charts.setTween(kind, direction.value || 0, instant);
    let facts = [];
    const running = model.status === "running";
    if (direction.done) {
      facts.push(t("fact.peak", { v: fmtSpeedUnit(direction.peak) }));
    } else if (!running) {
      // Stopped or failed half way: only what was really moved.
      facts.push(fmtBytes(direction.bytes));
    } else if (direction.measuring) {
      facts.push(t("fact.now", { v: fmtSpeedUnit(direction.live) }));
    } else {
      facts.push(t("fact.warming"));
    }
    if (direction.streams && (direction.done || running)) {
      facts.push(tn("fact.streams", direction.streams));
    }
    if (direction.done) {
      facts.push(fmtBytes(direction.bytes));
      if (format.isNumber(direction.latency)) {
        facts.push(t("fact.loaded", { v: fmtMs(direction.latency) }));
      }
      facts = facts.concat(cpuFacts(direction.cpu).filter(Boolean));
    }
    setFacts(kind + "-facts", facts);
  }

  /* With a test on the board: who and how much. Without one: what a test
     would use. */
  function renderRoute(model) {
    const pairs = [];
    if (model && model.status !== "error") {
      const client = model.client || {};
      const bytes = (model.down ? model.down.bytes || 0 : 0) + (model.up ? model.up.bytes || 0 : 0);
      pairs.push([t("route.isp"), client.isp || "—"]);
      pairs.push([
        model.family ? t("route.ip") + " (IPv" + model.family + ")" : t("route.ip"),
        client.ip || "—"
      ]);
      pairs.push([t("route.data"), bytes ? fmtBytes(bytes) : "—"]);
      pairs.push([t("route.time"), fmtSeconds(model.elapsed)]);
    } else {
      const settings = state.settings || {};
      const durations = (state.info && state.info.options && state.info.options.durations) || {};
      const duration = settings.duration || "standard";
      pairs.push([
        t("route.measure"),
        t("duration." + duration) + (durations[duration] ? " · " + durations[duration] + " s" : "")
      ]);
      pairs.push([
        t("route.connections"),
        !settings.connections || settings.connections === "auto" ? t("value.auto") : String(settings.connections)
      ]);
      pairs.push([
        t("route.protocol"),
        !settings.ip_version || settings.ip_version === "auto" ? t("value.anyip") : "IPv" + settings.ip_version
      ]);
      pairs.push([
        t("route.history"),
        settings.save_history === false
          ? t("value.off")
          : t("history.of", { n: fmtInt(state.historyCount), max: fmtInt(settings.history_limit || 0) })
      ]);
    }
    setPairs($("route-list"), pairs);
  }

  function currentProvider() {
    for (let i = 0; i < state.providers.length; i += 1) {
      if (state.providers[i].id === state.providerId) {
        return state.providers[i];
      }
    }
    return null;
  }

  function buildProviders() {
    const box = $("providers");
    box.textContent = "";
    state.providers.forEach((provider) => {
      const button = el("button", "tile prov");
      button.type = "button";
      button.setAttribute("role", "radio");
      button.dataset.id = provider.id;
      button.append(
        el("span", "prov-name", provider.name),
        el("span", "note", has("prov." + provider.id) ? t("prov." + provider.id) : "")
      );
      button.addEventListener("click", () => {
        selectProvider(provider.id);
      });
      box.append(button);
    });
    renderControls();
  }

  function selectProvider(id) {
    if (state.running || state.starting) {
      return;
    }
    state.providerId = id;
    renderControls();
  }

  function renderControls() {
    const busy = state.running || state.starting;
    const buttons = $("providers").children;
    for (let i = 0; i < buttons.length; i += 1) {
      const checked = buttons[i].dataset.id === state.providerId;
      buttons[i].setAttribute("aria-checked", String(checked));
      buttons[i].tabIndex = checked ? 0 : -1;
      buttons[i].disabled = busy;
    }

    const provider = currentProvider();
    const change = $("server-change");
    const choice = provider ? state.serverChoice[provider.id] : null;
    if (!provider || !provider.selectable_servers) {
      $("server-name").textContent = t("server.anycast");
      $("server-sub").textContent = t("server.anycast.sub");
      change.hidden = true;
    } else {
      $("server-name").textContent = choice ? choice.label : t("server.auto");
      $("server-sub").textContent = choice ? choice.sub : t("server.auto.sub");
      change.hidden = false;
      change.disabled = busy;
    }

    const action = $("action");
    action.textContent = t(state.running ? "action.stop" : state.starting ? "action.starting" : "action.start");
    action.setAttribute("aria-busy", String(state.starting || state.stopping));
    action.disabled = state.running ? state.stopping || !state.test || !state.test.id : state.starting || !state.info;
  }

  function renderTest() {
    const previous = state.model;
    const model = currentModel();
    state.model = model;
    $("view-test").dataset.setup = String(!model && !state.starting);
    // A saved result appears at once; a live one eases from its last value.
    const instant = !model || model.kind !== "live" || !previous || previous.kind !== "live";
    renderStatus(model);
    renderPing(model, instant);
    renderDirection("down", model, instant);
    renderDirection("up", model, instant);
    renderRoute(model);
    renderControls();
    charts.markTracesDirty();
  }

  /* ---- Running a test --------------------------------------------------- */

  function stopPolling() {
    state.polling = false;
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = 0;
    }
  }

  function startPolling() {
    if (state.polling) {
      return;
    }
    state.polling = true;
    state.pollFailures = 0;
    pollOnce();
  }

  function pollOnce() {
    if (!state.polling) {
      return;
    }
    api("GET", "/state").then(
      (data) => {
        if (!state.polling) {
          return;
        }
        state.pollFailures = 0;
        if (data.test) {
          state.test = data.test;
        }
        state.running = !!data.running;
        if (state.running) {
          renderTest();
          state.pollTimer = window.setTimeout(pollOnce, POLL_MS);
          return;
        }
        stopPolling();
        testEnded();
      },
      () => {
        if (!state.polling || state.gate) {
          return;
        }
        state.pollFailures += 1;
        if (state.pollFailures >= 8) {
          stopPolling();
          state.running = false;
          state.stopping = false;
          state.startedTestId = null;
          state.test = {
            status: "error",
            phase: "done",
            provider: currentProvider(),
            error: { code: "offline", detail: "" }
          };
          renderTest();
          return;
        }
        state.pollTimer = window.setTimeout(pollOnce, 600);
      }
    );
  }

  function testEnded() {
    state.stopping = false;
    state.startedTestId = null;
    state.stopError = "";
    const test = state.test;
    renderTest();
    if (test && test.status === "done" && test.result) {
      const result = test.result;
      announce(
        t("announce.done", {
          down: fmtSpeedUnit(result.download && result.download.mbps),
          up: fmtSpeedUnit(result.upload && result.upload.mbps),
          ping: fmtMs(result.ping && result.ping.latency)
        }) + (test.save_error ? " " + t("status.saveFailed") : "")
      );
    } else if (test && test.status === "error") {
      announce(t("status.error") + ". " + errorText(test.error));
    } else if (test && test.status === "cancelled") {
      announce(t("status.cancelled"));
    }
    if (state.view === "history") {
      loadHistory();
    }
    loadLast().then(() => {
      renderRoute(state.model);
    });
  }

  function startTest(accepted, hide) {
    const provider = currentProvider();
    if (state.running || state.starting || !state.info || !provider) {
      return;
    }
    // The notice comes before every test until "Don't show this again" is ticked. If it was
    // never accepted, the server answers notice_required and the notice opens anyway.
    const hidden = state.settings.hidden_notices || {};
    if (provider.notice && !accepted && !hidden[provider.notice]) {
      openNotice(provider);
      return;
    }
    showView("test");
    state.starting = true;
    state.startedTestId = null;
    state.startWarning = "";
    state.stopError = "";
    state.pinned = null;
    state.test = null;
    state.lastShown = state.lastResult;
    state.lastResult = null;
    state.announced = "";
    renderTest();
    const choice = state.serverChoice[provider.id];
    api("POST", "/test/start", {
      provider: provider.id,
      server: choice ? choice.id : "",
      accept_notice: !!accepted,
      hide_notice: !!(accepted && hide)
    }).then(
      (data) => {
        if (accepted && provider.notice) {
          // Mirror what the server has just stored.
          const notices = state.settings.accepted_notices || {};
          notices[provider.notice] = notices[provider.notice] || Math.floor(Date.now() / 1000);
          state.settings.accepted_notices = notices;
          if (hide) {
            state.settings.hidden_notices = state.settings.hidden_notices || {};
            state.settings.hidden_notices[provider.notice] = true;
            settingsUi.syncSettings();
          }
        }
        state.starting = false;
        state.test = data.test;
        state.startedTestId = data.test.id;
        state.startWarning = data.settings_error ? t("status.providerUnsaved") : "";
        state.running = true;
        renderTest();
        startPolling();
      },
      (error) => {
        state.starting = false;
        state.lastResult = state.lastShown || null;
        if (error.code === "notice_required") {
          renderTest();
          openNotice(provider);
        } else if (error.code === "busy") {
          // Someone else started a test: follow it.
          state.running = true;
          renderTest();
          startPolling();
        } else if (error.code !== "gated" && !state.gate) {
          state.test = {
            status: "error",
            phase: "done",
            provider: { id: provider.id, name: provider.name },
            error: { code: error.code, detail: error.detail }
          };
          renderTest();
        }
      }
    );
  }

  function stopTest() {
    if (!state.running || state.stopping || !state.test || !state.test.id) {
      return;
    }
    state.stopping = true;
    state.stopError = "";
    renderControls();
    api("POST", "/test/stop", { id: state.test.id }).catch((error) => {
      state.stopping = false;
      if (!state.gate) {
        state.stopError = errorText(error);
        announce(state.stopError);
        renderTest();
      }
    });
  }

  function loadLast() {
    return api("GET", "/history?limit=1").then(
      (data) => {
        state.lastResult = (data.items && data.items[0]) || null;
        state.historyCount = data.count || 0;
      },
      () => {
        /* Not essential: the board simply opens empty. */
      }
    );
  }

  /* The window stays loaded while it is hidden or behind another. On coming
     back, follow a test that someone else started and show the latest result. */
  let resyncedAt = 0;

  function resync() {
    const now = Date.now();
    if (state.gate || state.running || state.starting || !state.info || now - resyncedAt < 3000) {
      return;
    }
    resyncedAt = now;
    api("GET", "/state")
      .then((live) => {
        if (live.running && live.test) {
          state.test = live.test;
          state.running = true;
          startPolling();
          return null;
        }
        return loadLast().then(() => {
          renderTest();
          if (state.view === "history") {
            loadHistory();
          }
        });
      })
      .catch(() => {
        /* api() already shows the gate when the session or the service is gone. */
      });
  }

  /* ---- Dialogs ---------------------------------------------------------- */

  function openNotice(provider) {
    $("dlg-notice-body").textContent = has("notice." + provider.notice) ? t("notice." + provider.notice) : "";
    const link = $("dlg-notice-link");
    const safe = /^https:\/\//.test(provider.notice_url || "");
    link.parentElement.hidden = !safe;
    if (safe) {
      link.href = provider.notice_url;
    }
    $("dlg-notice-hide").checked = false;
    $("dlg-notice").showModal();
  }

  let confirmDone = null;

  function confirmDialog(title, body, okLabel, destructive) {
    if (confirmDone) {
      const previous = confirmDone;
      confirmDone = null;
      previous(false);
    }
    $("dlg-confirm-title").textContent = title;
    $("dlg-confirm-body").textContent = body;
    $("dlg-confirm-ok").textContent = okLabel;
    $("dlg-confirm-ok").className = destructive ? "btn btn-alert" : "btn btn-ink";
    return new Promise((resolve) => {
      confirmDone = resolve;
      const dialog = $("dlg-confirm");
      if (!dialog.open) {
        dialog.showModal();
      }
    });
  }

  function serverRow(provider, server, checked) {
    const row = el("button", "row");
    row.type = "button";
    row.setAttribute("role", "radio");
    row.setAttribute("aria-checked", String(checked));
    row.tabIndex = checked ? 0 : -1;
    const text = el("span");
    if (!server) {
      text.append(el("span", "row-main", t("servers.auto")));
      row.append(text);
    } else {
      text.append(
        el("span", "row-main", format.serverLabel(server)),
        el("span", "row-sub", format.joined([server.country, server.host]))
      );
      row.append(
        text,
        el("span", "row-num", format.isNumber(server.distance_km) ? fmtInt(server.distance_km) + " km" : ""),
        el("span", "row-num", format.isNumber(server.latency_ms) ? fmtMs(server.latency_ms) + " ms" : t("servers.noreply"))
      );
    }
    row.addEventListener("click", () => {
      if (server) {
        state.serverChoice[provider.id] = {
          id: String(server.id),
          label: format.serverLabel(server),
          sub: format.joined([
            format.isNumber(server.distance_km) ? t("server.distance", { v: fmtInt(server.distance_km) }) : "",
            server.host
          ])
        };
      } else {
        delete state.serverChoice[provider.id];
      }
      $("dlg-servers").close();
      renderControls();
    });
    return row;
  }

  function renderServerRows(provider, servers, status) {
    const list = $("servers-list");
    const choice = state.serverChoice[provider.id];
    list.setAttribute("aria-busy", String(status === "loading"));
    $("servers-refresh").disabled = status === "loading";
    $("servers-refresh").setAttribute("aria-busy", String(status === "loading"));
    list.textContent = "";
    const automatic = serverRow(provider, null, !choice);
    if (!choice || !servers.some((server) => choice.id === String(server.id))) {
      automatic.tabIndex = 0;
    }
    list.append(automatic);
    if (status === "failed") {
      const box = el("div", "rows-status");
      const retry = el("button", "btn", t("err.retry"));
      retry.type = "button";
      retry.addEventListener("click", () => {
        loadServers(provider, ($("servers-q").value || "").trim());
      });
      box.append(el("p", "note rows-note", t("servers.failed")), retry);
      list.append(box);
      return;
    }
    if (status) {
      const note = el("p", "note rows-note", t("servers." + status));
      if (status === "loading") {
        note.dataset.loading = "true";
      }
      list.append(note);
      return;
    }
    if (!servers.length) {
      list.append(el("p", "note rows-note", t("servers.empty")));
      return;
    }
    servers.forEach((server) => {
      list.append(serverRow(provider, server, !!choice && choice.id === String(server.id)));
    });
  }

  function loadServers(provider, query) {
    state.serversToken += 1;
    const token = state.serversToken;
    renderServerRows(provider, [], "loading");
    let path = "/servers?provider=" + encodeURIComponent(provider.id);
    if (query) {
      path += "&q=" + encodeURIComponent(query);
    }
    const lookup = provider.notice && state.confirmedLookups[provider.id];
    api(lookup ? "POST" : "GET", path, lookup ? { accept_lookup: true } : undefined).then(
      (data) => {
        if (token === state.serversToken) {
          renderServerRows(provider, data.servers || [], "");
        }
      },
      () => {
        if (token === state.serversToken) {
          renderServerRows(provider, [], "failed");
        }
      }
    );
  }

  function openServers() {
    const provider = currentProvider();
    if (!provider || !provider.selectable_servers || state.running || state.starting) {
      return;
    }
    if (provider.notice && !state.confirmedLookups[provider.id]) {
      confirmDialog(t("lookup.title"), t("lookup.mlab"), t("lookup.allow")).then((accepted) => {
        if (accepted && state.providerId === provider.id) {
          state.confirmedLookups[provider.id] = true;
          openServers();
        }
      });
      return;
    }
    const input = $("servers-q");
    input.value = "";
    input.hidden = !provider.searchable;
    $("dlg-servers").showModal();
    loadServers(provider, "");
    if (provider.searchable) {
      input.focus();
    } else {
      const first = $("servers-list").querySelector("button[role='radio']");
      if (first) {
        first.focus();
      }
    }
  }

  /* ---- Delegates kept top-level for views, events and tests ------------- */

  function renderHistory(motion) {
    historyUi.renderHistory(motion);
  }

  function renderHistoryList(items, motion) {
    historyUi.renderHistoryList(items, motion);
  }

  function drawSkyline(items) {
    charts.drawSkyline(items);
  }

  function loadHistory(notice) {
    return historyUi.loadHistory(notice);
  }

  function loadDetails(ids) {
    return historyUi.loadDetails(ids);
  }

  function exportCsv() {
    historyUi.exportCsv();
  }

  function clearHistory() {
    historyUi.clearHistory();
  }

  /* ---- Views and events ------------------------------------------------- */

  function updateChartSteps() {
    $("h-chart-step").hidden = state.chartItems.length < 2 || !touchScreen.matches;
  }

  function showView(name) {
    if (VIEWS.indexOf(name) < 0) {
      return;
    }
    const changed = state.view !== name;
    state.view = name;
    if (name !== "history") {
      historyUi.disconnectObserver();
      historyUi.resetDownload();
    }
    VIEWS.forEach((view) => {
      $("view-" + view).hidden = view !== name;
    });
    const buttons = $("nav").querySelectorAll("button");
    for (let i = 0; i < buttons.length; i += 1) {
      if (buttons[i].dataset.view === name) {
        buttons[i].setAttribute("aria-current", "page");
      } else {
        buttons[i].removeAttribute("aria-current");
      }
    }
    if (name === "history") {
      const backToFirst = changed && state.historyPage !== 1;
      if (backToFirst) {
        state.historyPage = 1;
      }
      if (!state.history || historyLanguage !== lang) {
        renderHistory();
      } else if (backToFirst || (showMiniTraces.matches && !historyUi.showsTraces())) {
        renderHistoryList(historyUi.filteredHistory());
      }
      updateChartSteps();
      loadHistory();
    }
    if (name === "test") {
      renderTest();
    }
    if (changed) {
      // Inside TOS the body is what scrolls (see the stylesheet).
      window.scrollTo(0, 0);
      document.body.scrollTop = 0;
    }
  }

  function bind() {
    $("nav").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-view]");
      if (button) {
        showView(button.dataset.view);
      }
    });

    $("action").addEventListener("click", () => {
      if (state.running) {
        stopTest();
      } else {
        startTest(false);
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        resync();
      }
    });
    window.addEventListener("focus", resync);
    window.addEventListener("resize", () => {
      charts.invalidateChartBox();
    });
    // Only the window that started this test should stop it on close.
    window.addEventListener("pagehide", () => {
      if (state.running && !state.gate && state.startedTestId && state.test && state.test.id === state.startedTestId) {
        apiClient.stopOnExit(state.startedTestId);
      }
    });

    $("theme-toggle").addEventListener("click", () => {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    });
    const schemeChanged = () => {
      renderThemeControls();
    };
    if (darkScheme.addEventListener) {
      darkScheme.addEventListener("change", schemeChanged);
    }
    if (showMiniTraces.addEventListener) {
      showMiniTraces.addEventListener("change", () => {
        if (showMiniTraces.matches && state.view === "history" && state.history) {
          renderHistoryList(historyUi.filteredHistory());
        }
      });
    }
    if (touchScreen.addEventListener) {
      touchScreen.addEventListener("change", updateChartSteps);
    }

    // Arrow keys move between providers and servers, as in any radio group.
    settingsUi.bindRadioGroupKeys($("providers"), {
      canMove: () => !state.running && !state.starting && state.providers.length > 0,
      currentIndex: (radios) => radios.findIndex((radio) => radio.dataset.id === state.providerId),
      onMove: (next) => { selectProvider(next.dataset.id); }
    });

    settingsUi.bindRadioGroupKeys($("servers-list"), {
      onMove: (next, radios) => {
        radios.forEach((radio) => {
          radio.tabIndex = radio === next ? 0 : -1;
        });
      }
    });

    $("server-change").addEventListener("click", openServers);
    $("servers-refresh").addEventListener("click", () => {
      const provider = currentProvider();
      if (provider) {
        loadServers(provider, ($("servers-q").value || "").trim());
      }
    });
    let searchTimer = 0;
    $("servers-q").addEventListener("input", (event) => {
      window.clearTimeout(searchTimer);
      const value = event.target.value.trim();
      searchTimer = window.setTimeout(() => {
        const provider = currentProvider();
        if (provider && $("dlg-servers").open) {
          loadServers(provider, value);
        }
      }, 350);
    });

    // Dialogs close with their own buttons and with a click outside them.
    const dialogs = document.querySelectorAll("dialog");
    for (let i = 0; i < dialogs.length; i += 1) {
      dialogs[i].addEventListener("click", (event) => {
        const dialog = event.currentTarget;
        const box = dialog.getBoundingClientRect();
        const outside =
          event.target === dialog &&
          (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);
        if (outside || event.target.closest("[data-close]")) {
          dialog.close();
        }
      });
    }
    $("dlg-notice-ok").addEventListener("click", () => {
      const hide = $("dlg-notice-hide").checked;
      $("dlg-notice").close();
      startTest(true, hide);
    });
    $("dlg-confirm-ok").addEventListener("click", () => {
      const done = confirmDone;
      confirmDone = null;
      $("dlg-confirm").close();
      if (done) {
        done(true);
      }
    });
    $("dlg-confirm").addEventListener("close", () => {
      const done = confirmDone;
      confirmDone = null;
      if (done) {
        done(false);
      }
    });

    $("status-unpin").addEventListener("click", () => {
      state.pinned = null;
      renderTest();
      $("status-title").focus();
    });
    $("status-copy").addEventListener("click", () => {
      if (state.model) {
        copyWithFeedback($("status-copy"), formatSummary(state.model));
      }
    });

    $("h-filter").addEventListener("change", (event) => {
      historyUi.setFilter(event.target.value);
    });
    $("h-filter-clear").addEventListener("click", () => {
      historyUi.setFilter("");
    });
    $("h-skyline").addEventListener("keydown", (event) => {
      if (!state.chartItems.length) {
        return;
      }
      const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (step) {
        event.preventDefault();
        const next = Math.max(0, Math.min(state.chartItems.length - 1, state.chartIndex + step));
        if (state.chartPicked) {
          historyUi.pickChart(next, true);
        } else {
          state.chartIndex = next;
          charts.announceChartSelection();
        }
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        historyUi.pickChart(state.chartIndex);
      }
    });
    $("h-skyline").addEventListener("focus", () => { charts.announceChartSelection(); });
    $("h-skyline").addEventListener("blur", () => { charts.announceChartSelection(); });
    $("h-chart-reset").addEventListener("click", () => { historyUi.clearChartPick(); });
    $("h-download").addEventListener("click", () => { historyUi.scheduleDownloadReset(); });
    $("h-export").addEventListener("click", exportCsv);
    $("h-chart-prev").addEventListener("click", () => { historyUi.pickChart(state.chartIndex - 1, true); });
    $("h-chart-next").addEventListener("click", () => { historyUi.pickChart(state.chartIndex + 1, true); });
    $("h-retry").addEventListener("click", () => { loadHistory(); });
    $("h-clear").addEventListener("click", clearHistory);
    $("h-page-prev").addEventListener("click", () => { historyUi.turnPage(state.historyPage - 1); });
    $("h-page-next").addEventListener("click", () => { historyUi.turnPage(state.historyPage + 1); });
    settingsUi.buildSeg(
      "h-page-size",
      PAGE_SIZES.map((size) => ({ value: size, label: String(size) })),
      state.historyPageSize,
      (size) => { historyUi.pickPageSize(size); }
    );

    $("set-history").addEventListener("click", () => {
      settingsUi.saveSetting({ save_history: $("set-history").getAttribute("aria-checked") !== "true" });
    });
    $("set-notice").addEventListener("click", () => {
      const button = $("set-notice");
      const hidden = {};
      // The switch reads "warn before every test", so turning it off hides the notice.
      hidden[button.getAttribute("data-notice")] = button.getAttribute("aria-checked") === "true";
      settingsUi.saveSetting({ hidden_notices: hidden });
    });
    $("set-limit").addEventListener("change", (event) => {
      const max = (state.info && state.info.options && state.info.options.history_max) || 1000;
      const value = parseInt(event.target.value, 10);
      if (!format.isNumber(value)) {
        settingsUi.syncSettings();
        return;
      }
      const next = Math.max(10, Math.min(max, value));
      const excess = state.historyCount - next;
      const applyLimit = () => {
        settingsUi.saveSetting({ history_limit: next }).then((ok) => {
          if (ok && state.historyCount > next) {
            state.historyCount = next;
            state.history = null;
          }
        });
      };
      if (excess > 0) {
        confirmDialog(
          t("set.limit.confirm.title"),
          tn("set.limit.confirm.body", excess, { limit: fmtInt(next) }),
          t("set.limit.confirm.ok"),
          true
        ).then((yes) => {
          if (yes) {
            applyLimit();
          } else {
            settingsUi.syncSettings();
          }
        });
        return;
      }
      applyLimit();
    });
  }

  function init() {
    applyLanguage(cachedLang());
    bind();
    renderThemeControls();
    renderUnitControls();
    renderTest();
    api("GET", "/info")
      .then((info) => {
        if (!startup.isInfoResponse(info, APP_ID)) {
          showGate("unexpected");
          return null;
        }
        const refresh = startup.refreshForVersion(
          info.version,
          $("app").getAttribute("data-version")
        );
        if (refresh !== "current") {
          if (refresh === "stale") {
            showGate("outdated");
          }
          return null;
        }
        state.info = info;
        state.settings = info.settings || {};
        state.providers = info.providers || [];
        state.historyCount = info.history_count || 0;
        // The provider of the last test, else LibreSpeed, else the first one.
        const ids = state.providers.map((provider) => provider.id);
        const wanted = [state.settings.default_provider, "librespeed"].filter((id) => ids.indexOf(id) >= 0);
        state.providerId = wanted.length ? wanted[0] : ids[0] || "";
        adoptSavedLanguage();
        buildProviders();
        settingsUi.buildSettings();
        return api("GET", "/state");
      })
      .then((live) => {
        if (!state.info) {
          return null;
        }
        if (live.running && live.test) {
          state.test = live.test;
          state.running = true;
          startPolling();
          return null;
        }
        return state.historyCount > 0 ? loadLast() : null;
      })
      .then(
        () => {
          if (state.info) {
            renderTest();
          }
        },
        () => {
          if (!state.gate) {
            showGate("offline");
          }
        }
      );
  }

  init();
})();
