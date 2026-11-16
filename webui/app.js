// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - web UI logic.
 *
 * The test itself runs on the NAS. This page only starts it, polls its state
 * through the TOS proxy and draws what comes back. */
(function () {
  "use strict";

  var APP_ID = "naspeed";
  var API = "/v2/proxy/" + APP_ID + "/api";
  // The TOS desktop and its login page. The web root only loads this address.
  var TOS_LOGIN_URL = "/tos/";
  var POLL_MS = 110;
  var HISTORY_PAGE = 40;
  var PING_SLOTS = 12;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var VIEWS = ["test", "history", "settings"];
  var ARROW_STEPS = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

  var I18N = window.NASpeedI18n;
  var prefs = window.NASpeedPrefs;
  var startup = window.NASpeedStartup;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var darkScheme = window.matchMedia("(prefers-color-scheme: dark)");

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function externalLink(text, href) {
    var link = el("a", "", text);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function svgEl(tag, className) {
    var node = document.createElementNS(SVG_NS, tag);
    if (className) {
      node.setAttribute("class", className);
    }
    return node;
  }

  var state = {
    info: null,
    settings: {},
    providers: [],
    providerId: "",
    serverChoice: {}, // provider id -> {id, label, sub}
    serversToken: 0,
    view: "test",
    running: false,
    starting: false,
    stopping: false,
    test: null, // live state of the test started or followed from this page
    pinned: null, // a saved result opened from the history
    lastResult: null,
    lastShown: null, // lastResult set aside while a test starts, back if the start fails
    model: null,
    history: null,
    historyCount: 0,
    historyFilter: "",
    historyShown: HISTORY_PAGE,
    chartItems: [],
    chartIndex: -1,
    gate: "",
    polling: false,
    pollTimer: 0,
    pollFailures: 0,
    announced: ""
  };

  /* ---- Texts and formats ------------------------------------------------ */

  var lang = I18N.fallback;
  var dict = I18N.langs[lang].text;
  var fallbackDict = dict;
  var locale = I18N.langs[lang].locale;
  var plural = null;
  var numberFormats = {};
  var dateFormat = null;

  function has(key) {
    return dict[key] !== undefined || fallbackDict[key] !== undefined;
  }

  function t(key, vars) {
    var text = dict[key];
    if (text === undefined) {
      text = fallbackDict[key];
    }
    if (text === undefined) {
      return key;
    }
    if (vars) {
      text = text.replace(/\{(\w+)\}/g, function (match, name) {
        return vars[name] !== undefined ? String(vars[name]) : match;
      });
    }
    return text;
  }

  function tn(key, count, vars) {
    var form = "other";
    try {
      form = plural.select(count);
    } catch (error) {
      form = "other";
    }
    var full = has(key + "." + form) ? key + "." + form : key + ".other";
    var values = { n: fmtInt(count) };
    if (vars) {
      Object.keys(vars).forEach(function (name) {
        values[name] = vars[name];
      });
    }
    return t(full, values);
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

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function fmtInt(value) {
    return isNumber(value) ? number(Math.round(value), 0) : "—";
  }

  function fmtSpeed(value) {
    if (!isNumber(value)) {
      return "—";
    }
    if (value >= 1000) {
      return number(value, 0);
    }
    return number(value, value >= 100 ? 1 : 2);
  }

  function fmtSpeedUnit(value) {
    return fmtSpeed(value) + " " + t("unit.speed");
  }

  function fmtMs(value) {
    if (!isNumber(value)) {
      return "—";
    }
    return number(value, value >= 100 ? 0 : 1);
  }

  function fmtPercent(value) {
    if (!isNumber(value)) {
      return "—";
    }
    if (!numberFormats["%"]) {
      numberFormats["%"] = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
    }
    return numberFormats["%"].format(value / 100);
  }

  function fmtBytes(value) {
    if (!isNumber(value)) {
      return "—";
    }
    if (value >= 1e9) {
      return number(value / 1e9, 2) + " " + t("unit.gb");
    }
    return number(value / 1e6, value >= 1e8 ? 0 : 1) + " " + t("unit.mb");
  }

  function fmtSeconds(value) {
    if (!isNumber(value)) {
      return "—";
    }
    var seconds = Math.round(value);
    if (seconds < 60) {
      return seconds + " s";
    }
    return Math.floor(seconds / 60) + " min " + (seconds % 60) + " s";
  }

  function fmtDate(timestamp) {
    if (!isNumber(timestamp)) {
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
    var code = String(tag).toLowerCase().split(/[-_]/)[0];
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
    var saved = matchLang(state.settings.language);
    var legacy = matchLang(prefs.get("lang"));
    if (saved || !legacy) {
      prefs.set("lang", "");
    } else {
      saved = legacy;
      api("POST", "/settings", { language: legacy }).then(
        function (settings) {
          state.settings = settings;
          prefs.set("lang", "");
        },
        function () {
          /* Tried again the next time the app opens. */
        }
      );
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

    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
    }
    nodes = document.querySelectorAll("[data-i18n-attr]");
    for (i = 0; i < nodes.length; i += 1) {
      var pairs = nodes[i].getAttribute("data-i18n-attr").split(";");
      for (var j = 0; j < pairs.length; j += 1) {
        var parts = pairs[j].split(":");
        if (parts.length === 2) {
          nodes[i].setAttribute(parts[0].trim(), t(parts[1].trim()));
        }
      }
    }
    $("lang-settings").value = lang;
  }

  function fillLanguageSelect() {
    var select = $("lang-settings");
    select.textContent = "";
    I18N.order.forEach(function (code) {
      var option = el("option", "", I18N.langs[code].name);
      option.value = code;
      select.append(option);
    });
  }

  /* Everything that carries text built at run time. */
  function renderAll() {
    Object.keys(tweens).forEach(function (key) {
      tweens[key].shown = null;
      paintTween(tweens[key]);
    });
    buildProviders();
    buildSettings();
    renderThemeControls();
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

  function ApiError(status, code, detail) {
    this.status = status;
    this.code = code;
    this.detail = detail || "";
  }

  function cookie(name) {
    var prefix = encodeURIComponent(name) + "=";
    var items = document.cookie.split(";");
    for (var i = 0; i < items.length; i += 1) {
      var item = items[i].trim();
      if (item.indexOf(prefix) === 0) {
        return item.slice(prefix.length);
      }
    }
    return "";
  }

  /* Calls go through the platform proxy and carry the TOS session the way the
     TOS 7 guide asks for (sections 8.9 and 8.10): X-Csrf-Token plus a "Cookie"
     header. Browsers drop a hand-written Cookie header and send the real one,
     which holds the same two values. */
  function api(method, path, body) {
    if (state.gate === "denied" || state.gate === "session") {
      return Promise.reject(new ApiError(0, "gated"));
    }
    var headers = { Accept: "application/json", "X-Requested-With": "NASpeed" };
    var csrf = cookie("X-Csrf-Token");
    if (csrf) {
      headers["X-Csrf-Token"] = csrf;
      var session = cookie("TMSESSNAME");
      if (session) {
        headers.Cookie = "TMSESSNAME=" + session + "; X-Csrf-Token=" + csrf + ";";
      }
    }
    var options = { method: method, headers: headers, credentials: "include", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(API + path, options).then(
      function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (data) {
            // Without a session the TOS proxy answers HTTP 200 with
            // {"is_login": false, "code_num": 117} before NASpeed is reached.
            if (response.status === 401 || (data && data.is_login === false)) {
              showGate("session");
              throw new ApiError(401, "unauthorized");
            }
            if (response.status === 403) {
              showGate("denied");
              throw new ApiError(403, (data && data.error) || "forbidden");
            }
            if (!response.ok) {
              throw new ApiError(response.status, (data && data.error) || "generic", data && data.detail);
            }
            if (data === null) {
              throw new ApiError(response.status, "bad_response");
            }
            return data;
          });
      },
      function () {
        throw new ApiError(0, "offline");
      }
    );
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
    var first = state.gate !== kind;
    state.gate = kind;
    stopPolling();
    $("gate-title").textContent = t("gate." + kind + ".title");
    $("gate-body").textContent = t("gate." + kind + ".body");
    var button = $("gate-action");
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
      button.onclick = function () {
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
    var text = has("err." + error.code) ? t("err." + error.code) : t("err.generic");
    return error.detail ? text + " (" + String(error.detail).slice(0, 120) + ")" : text;
  }

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
    buildSeg(
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

  /* ---- Model: one shape for a live test and for a saved result ----------- */

  function directionFromLive(live) {
    if (!live) {
      return null;
    }
    var points = [[0, 0]].concat(live.series || []);
    var last = points[points.length - 1][0];
    var start = isNumber(live.window_start) ? live.window_start : null;
    var domain;
    if (live.done) {
      domain = Math.max(last, live.elapsed || 0, 0.5);
    } else {
      domain = Math.max(last, (start === null ? Math.max(1.5, last) : start) + (live.window || 8));
    }
    return {
      value: live.mbps,
      live: live.live_mbps,
      peak: live.peak_mbps,
      streams: live.done ? live.streams_used : live.streams,
      bytes: live.bytes,
      latency: live.done ? live.latency : null,
      cpu: (live.done && live.cpu) || null,
      done: !!live.done,
      measuring: !!live.measuring,
      points: points,
      start: start,
      domain: domain
    };
  }

  function directionFromResult(saved) {
    if (!saved) {
      return null;
    }
    var series = Array.isArray(saved.series) ? saved.series : [];
    var total = saved.elapsed || (saved.warmup || 0) + (saved.seconds || 0) || 1;
    var points = [[0, 0]];
    for (var i = 0; i < series.length; i += 1) {
      points.push([((i + 0.5) * total) / series.length, series[i]]);
    }
    if (series.length) {
      points.push([total, series[series.length - 1]]);
    }
    return {
      value: saved.mbps,
      live: 0,
      peak: saved.peak_mbps,
      streams: saved.streams,
      bytes: saved.bytes,
      latency: saved.latency,
      cpu: saved.cpu || null,
      done: true,
      measuring: false,
      points: points,
      start: isNumber(saved.warmup) ? saved.warmup : null,
      domain: total
    };
  }

  function modelFromLive(test) {
    return {
      kind: "live",
      status: test.status,
      phase: test.phase,
      progress: test.progress || 0,
      providerName: test.provider ? test.provider.name : "",
      server: test.server,
      client: test.client,
      family: test.ip_family,
      ping: test.ping,
      down: directionFromLive(test.download),
      up: directionFromLive(test.upload),
      ts: test.started_at,
      elapsed: test.elapsed,
      saved: !!test.saved,
      error: test.error
    };
  }

  function modelFromResult(result, kind) {
    return {
      kind: kind,
      status: "done",
      phase: "done",
      progress: 1,
      providerName: result.provider_name || result.provider || "",
      server: result.server,
      client: result.client,
      family: result.ip_family,
      ping: result.ping,
      down: directionFromResult(result.download),
      up: directionFromResult(result.upload),
      ts: result.ts,
      elapsed: result.duration,
      saved: true,
      error: null
    };
  }

  function currentModel() {
    if (state.test && (state.running || !state.pinned)) {
      return modelFromLive(state.test);
    }
    if (state.pinned) {
      return modelFromResult(state.pinned, "saved");
    }
    if (state.lastResult) {
      return modelFromResult(state.lastResult, "last");
    }
    return null;
  }

  function serverLabel(server) {
    if (!server) {
      return "";
    }
    var parts = [];
    if (server.sponsor) {
      parts.push(server.sponsor);
    }
    if (server.name && server.name !== server.sponsor) {
      parts.push(server.name);
    }
    return parts.join(", ") || server.host || "";
  }

  function serverLine(server) {
    var label = serverLabel(server);
    if (label && isNumber(server.distance_km)) {
      label += " · " + t("server.distance", { v: fmtInt(server.distance_km) });
    }
    return label;
  }

  function joined(parts) {
    return parts
      .filter(function (part) {
        return !!part;
      })
      .join(" · ");
  }

  /* Why the NAS, rather than the line, may have set a result: the first reason
     either direction reports. */
  function cpuLimit(model) {
    var directions = [model.down, model.up];
    for (var i = 0; i < directions.length; i += 1) {
      var load = directions[i] && directions[i].cpu;
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
      isNumber(load.busy) ? t("fact.cpu", { v: fmtPercent(load.busy) }) : "",
      load.limit ? t("fact.limited") : ""
    ];
  }

  /* ---- Numbers that ease towards their value ---------------------------- */

  function makeTween(id, format) {
    return { el: $(id), format: format, current: null, target: null, shown: null };
  }

  var tweens = {
    ping: makeTween("ping-value", fmtMs),
    down: makeTween("down-value", fmtSpeed),
    up: makeTween("up-value", fmtSpeed)
  };

  function paintTween(tween) {
    var text = tween.current === null ? "" : tween.format(tween.current);
    if (text !== tween.shown) {
      tween.shown = text;
      tween.el.textContent = text;
    }
  }

  function setTween(tween, value, instant) {
    if (!isNumber(value)) {
      tween.current = null;
      tween.target = null;
    } else {
      tween.target = value;
      if (instant || tween.current === null || reduceMotion.matches) {
        tween.current = value;
      }
    }
    paintTween(tween);
  }

  function stepTween(tween, dt) {
    if (tween.current === null || tween.target === null || tween.current === tween.target) {
      return false;
    }
    var gap = tween.target - tween.current;
    if (Math.abs(gap) < Math.max(0.005, Math.abs(tween.target) * 0.0005)) {
      tween.current = tween.target;
    } else {
      tween.current += gap * (1 - Math.exp(-dt / 0.14));
    }
    paintTween(tween);
    return tween.current !== tween.target;
  }

  /* ---- Drawing the test ------------------------------------------------- */

  var traces = { dirty: true, moving: false, yShown: 0 };
  var pingRects = null;
  var frameId = 0;
  var frameTime = 0;

  function niceMax(value) {
    var steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    var base = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    for (var i = 0; i < steps.length; i += 1) {
      if (steps[i] * base >= value) {
        return steps[i] * base;
      }
    }
    return 10 * base;
  }

  function between(a, b, at) {
    var span = b[0] - a[0];
    return span > 0 ? a[1] + ((b[1] - a[1]) * (at - a[0])) / span : b[1];
  }

  /* Filled area under the points whose time lies in [from, to]. */
  function areaPath(points, from, to, domain, yMax, width, offset) {
    var sx = width / domain;
    var sy = 100 / yMax;
    var cut = [];
    var previous = null;
    for (var i = 0; i < points.length; i += 1) {
      var point = points[i];
      if (point[0] < from) {
        previous = point;
        continue;
      }
      if (!cut.length && previous && point[0] > from) {
        cut.push([from, between(previous, point, from)]);
      }
      if (point[0] > to) {
        if (previous) {
          cut.push([to, between(previous, point, to)]);
        }
        break;
      }
      cut.push(point);
      previous = point;
    }
    if (cut.length < 2) {
      return "";
    }
    var path = "M" + (offset + cut[0][0] * sx).toFixed(1) + ",100";
    for (i = 0; i < cut.length; i += 1) {
      path += "L" + (offset + cut[i][0] * sx).toFixed(1) + "," + (100 - Math.min(cut[i][1] * sy, 100)).toFixed(2);
    }
    return path + "L" + (offset + cut[cut.length - 1][0] * sx).toFixed(1) + ",100Z";
  }

  /* The lighter band is the ramp-up, which the result does not count. */
  function drawDirection(kind, direction, yMax) {
    var svg = $(kind + "-trace");
    var warm = svg.querySelector(".tr-warm");
    var main = svg.querySelector(".tr-main");
    if (!direction || direction.points.length < 2) {
      warm.setAttribute("d", "");
      main.setAttribute("d", "");
      return;
    }
    var split = direction.start !== null ? direction.start : direction.done ? 0 : Infinity;
    warm.setAttribute("d", areaPath(direction.points, 0, split, direction.domain, yMax, 1000, 0));
    main.setAttribute(
      "d",
      split === Infinity ? "" : areaPath(direction.points, split, Infinity, direction.domain, yMax, 1000, 0)
    );
  }

  function drawPing(ping) {
    var svg = $("ping-bars");
    if (!pingRects) {
      pingRects = [];
      for (var i = 0; i < PING_SLOTS; i += 1) {
        var rect = svgEl("rect");
        rect.setAttribute("x", String(i * 10 + 1.5));
        rect.setAttribute("width", "7");
        svg.append(rect);
        pingRects.push(rect);
      }
    }
    var values = ping && Array.isArray(ping.values) ? ping.values : [];
    var top = 1;
    values.forEach(function (value) {
      top = Math.max(top, value * 1.15);
    });
    pingRects.forEach(function (rect, index) {
      var value = values[index];
      var height = isNumber(value) ? Math.max(3, (value / top) * 100) : 0;
      rect.setAttribute("y", (100 - height).toFixed(2));
      rect.setAttribute("height", height.toFixed(2));
    });
  }

  /* Download and upload share one vertical scale, so the picture shows how
     the two really compare. */
  function drawTraces(dt) {
    var model = state.model;
    var peak = 1;
    ["down", "up"].forEach(function (kind) {
      var direction = model && model[kind];
      if (direction) {
        direction.points.forEach(function (point) {
          peak = Math.max(peak, point[1]);
        });
      }
    });
    var target = niceMax(peak * 1.08);
    var live = !!model && model.kind === "live" && model.status === "running";
    var moving = false;
    if (!traces.yShown || !live || reduceMotion.matches) {
      traces.yShown = target;
    } else if (traces.yShown !== target) {
      var gap = target - traces.yShown;
      if (Math.abs(gap) < target * 0.002) {
        traces.yShown = target;
      } else {
        traces.yShown += gap * (1 - Math.exp(-dt / 0.18));
        moving = true;
      }
    }
    drawDirection("down", model && model.down, traces.yShown);
    drawDirection("up", model && model.up, traces.yShown);
    drawPing(model && model.ping);
    traces.dirty = false;
    return moving;
  }

  function frame(now) {
    var dt = Math.min(0.1, Math.max(0.001, (now - frameTime) / 1000));
    frameTime = now;
    frameId = 0;
    var busy = false;
    Object.keys(tweens).forEach(function (key) {
      busy = stepTween(tweens[key], dt) || busy;
    });
    if (traces.dirty || traces.moving) {
      traces.moving = drawTraces(dt);
    }
    if (busy || traces.moving) {
      frameId = window.requestAnimationFrame(frame);
    }
  }

  function kick() {
    if (!frameId) {
      frameTime = window.performance.now();
      frameId = window.requestAnimationFrame(frame);
    }
  }

  /* ---- Test view -------------------------------------------------------- */

  function setFacts(id, facts) {
    var box = $(id);
    var signature = facts.join("|");
    if (box.dataset.sig === signature) {
      return;
    }
    box.dataset.sig = signature;
    box.textContent = "";
    facts.forEach(function (fact) {
      box.append(el("span", "", fact));
    });
  }

  function setPairs(list, pairs) {
    var signature = pairs
      .map(function (pair) {
        return pair.join("=");
      })
      .join("|");
    if (list.dataset.sig === signature) {
      return;
    }
    list.dataset.sig = signature;
    list.textContent = "";
    pairs.forEach(function (pair) {
      var group = el("div");
      group.append(el("dt", "", pair[0]), el("dd", String(pair[1]).length > 22 ? "long" : "", pair[1]));
      list.append(group);
    });
  }

  function renderStatus(model) {
    var title;
    var sub;
    var tone = "";
    var progress = 0;
    if (!model) {
      title = state.starting ? t("status.init") : t("status.ready");
      sub = t("status.ready.sub");
    } else if (model.status === "running") {
      title = has("status." + model.phase) ? t("status." + model.phase) : t("status.init");
      sub = joined([model.providerName, serverLine(model.server)]);
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
      sub = joined([model.providerName, serverLine(model.server), t(model.saved ? "status.saved" : "status.unsaved")]);
    } else if (model.kind === "last") {
      title = t("status.last");
      sub = joined([fmtDate(model.ts), model.providerName, serverLine(model.server)]);
    } else {
      title = fmtDate(model.ts);
      sub = joined([model.providerName, serverLine(model.server)]);
    }
    var limit = model && model.status === "done" ? cpuLimit(model) : null;
    if (limit) {
      sub = joined([model.kind === "last" ? fmtDate(model.ts) : "", t("status.cpu." + limit)]);
    }
    $("status-title").textContent = title;
    $("status-sub").textContent = sub;
    $("tile-status").dataset.tone = tone;
    $("progress").style.transform = "scaleX(" + Math.max(0, Math.min(1, progress)).toFixed(4) + ")";
  }

  function renderPing(model, instant) {
    var ping = model && model.ping;
    var lit = !!ping && isNumber(ping.latency);
    $("tile-ping").dataset.lit = String(lit);
    if (!lit) {
      setTween(tweens.ping, null, true);
      return;
    }
    setTween(tweens.ping, ping.latency, instant);
    var facts = [
      t("fact.jitter", { v: fmtMs(ping.jitter) }),
      t("fact.range", { low: fmtMs(ping.low), high: fmtMs(ping.high) })
    ];
    if (ping.loss > 0 && ping.loss < 1 && ping.count) {
      facts.push(tn("fact.lost", Math.round((ping.count * ping.loss) / (1 - ping.loss))));
    }
    setFacts("ping-facts", facts);
  }

  function renderDirection(kind, model, instant) {
    var direction = model && model[kind];
    $("tile-" + kind).dataset.lit = String(!!direction);
    if (!direction) {
      setTween(tweens[kind], null, true);
      return;
    }
    setTween(tweens[kind], direction.value || 0, instant);
    var facts = [];
    var running = model.status === "running";
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
      if (isNumber(direction.latency)) {
        facts.push(t("fact.loaded", { v: fmtMs(direction.latency) }));
      }
      facts = facts.concat(cpuFacts(direction.cpu).filter(Boolean));
    }
    setFacts(kind + "-facts", facts);
  }

  /* With a test on the board: who and how much. Without one: what a test
     would use. */
  function renderRoute(model) {
    var pairs = [];
    if (model && model.status !== "error") {
      var client = model.client || {};
      var bytes = (model.down ? model.down.bytes || 0 : 0) + (model.up ? model.up.bytes || 0 : 0);
      pairs.push([t("route.isp"), client.isp || "—"]);
      pairs.push([
        model.family ? t("route.ip") + " (IPv" + model.family + ")" : t("route.ip"),
        client.ip || "—"
      ]);
      pairs.push([t("route.data"), bytes ? fmtBytes(bytes) : "—"]);
      pairs.push([t("route.time"), fmtSeconds(model.elapsed)]);
    } else {
      var settings = state.settings || {};
      var durations = (state.info && state.info.options && state.info.options.durations) || {};
      var duration = settings.duration || "standard";
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
    for (var i = 0; i < state.providers.length; i += 1) {
      if (state.providers[i].id === state.providerId) {
        return state.providers[i];
      }
    }
    return state.providers[0] || null;
  }

  function buildProviders() {
    var box = $("providers");
    box.textContent = "";
    state.providers.forEach(function (provider) {
      var button = el("button", "tile prov");
      button.type = "button";
      button.setAttribute("role", "radio");
      button.dataset.id = provider.id;
      button.append(
        el("span", "prov-name", provider.name),
        el("span", "note", has("prov." + provider.id) ? t("prov." + provider.id) : "")
      );
      button.addEventListener("click", function () {
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
    var busy = state.running || state.starting;
    var buttons = $("providers").children;
    for (var i = 0; i < buttons.length; i += 1) {
      var checked = buttons[i].dataset.id === state.providerId;
      buttons[i].setAttribute("aria-checked", String(checked));
      buttons[i].tabIndex = checked ? 0 : -1;
      buttons[i].disabled = busy;
    }

    var provider = currentProvider();
    var change = $("server-change");
    var choice = provider ? state.serverChoice[provider.id] : null;
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

    var action = $("action");
    action.textContent = t(state.running ? "action.stop" : "action.start");
    action.disabled = state.running ? state.stopping : state.starting || !state.info;
  }

  function renderTest() {
    var previous = state.model;
    var model = currentModel();
    state.model = model;
    // A saved result appears at once; a live one eases from its last value.
    var instant = !model || model.kind !== "live" || !previous || previous.kind !== "live";
    renderStatus(model);
    renderPing(model, instant);
    renderDirection("down", model, instant);
    renderDirection("up", model, instant);
    renderRoute(model);
    renderControls();
    traces.dirty = true;
    kick();
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
    state.pollTimer = 0;
    if (!state.polling) {
      return;
    }
    api("GET", "/state").then(
      function (data) {
        if (!state.polling) {
          return;
        }
        state.pollFailures = 0;
        if (data.test) {
          state.test = data.test;
        } else if (!data.running && state.test && state.test.status === "running") {
          /* The service restarted during the test and no longer knows it. */
          state.test = {
            status: "error",
            phase: "done",
            provider: state.test.provider,
            error: { code: "interrupted" }
          };
        }
        state.running = !!data.running;
        if (state.running) {
          renderTest();
          state.pollTimer = window.setTimeout(pollOnce, POLL_MS);
        } else {
          state.polling = false;
          testEnded();
        }
      },
      function (error) {
        if (!state.polling || state.gate) {
          state.polling = false;
          return;
        }
        state.pollFailures += 1;
        if (state.pollFailures >= 8) {
          state.polling = false;
          state.running = false;
          state.test = {
            status: "error",
            phase: "done",
            provider: state.test ? state.test.provider : null,
            error: { code: error.code === "offline" ? "offline" : "generic" }
          };
          testEnded();
          return;
        }
        state.pollTimer = window.setTimeout(pollOnce, 600);
      }
    );
  }

  function testEnded() {
    state.stopping = false;
    var test = state.test;
    renderTest();
    if (test && test.status === "done" && test.result) {
      var result = test.result;
      announce(
        t("announce.done", {
          down: fmtSpeedUnit(result.download && result.download.mbps),
          up: fmtSpeedUnit(result.upload && result.upload.mbps),
          ping: fmtMs(result.ping && result.ping.latency)
        })
      );
    } else if (test && test.status === "error") {
      announce(t("status.error") + ". " + errorText(test.error));
    } else if (test && test.status === "cancelled") {
      announce(t("status.cancelled"));
    }
    state.history = null;
    loadLast().then(function () {
      renderRoute(state.model);
    });
  }

  function startTest(accepted, hide) {
    var provider = currentProvider();
    if (state.running || state.starting || !state.info || !provider) {
      return;
    }
    // The notice comes before every test until "Don't show this again" is ticked. If it was
    // never accepted, the server answers notice_required and the notice opens anyway.
    var hidden = state.settings.hidden_notices || {};
    if (provider.notice && !accepted && !hidden[provider.notice]) {
      openNotice(provider);
      return;
    }
    showView("test");
    state.starting = true;
    state.pinned = null;
    state.test = null;
    state.lastShown = state.lastResult;
    state.lastResult = null;
    state.announced = "";
    renderTest();
    var choice = state.serverChoice[provider.id];
    api("POST", "/test/start", {
      provider: provider.id,
      server: choice ? choice.id : "",
      accept_notice: !!accepted,
      hide_notice: !!(accepted && hide)
    }).then(
      function (data) {
        if (accepted && provider.notice) {
          // Mirror what the server has just stored.
          var notices = state.settings.accepted_notices || {};
          notices[provider.notice] = notices[provider.notice] || Math.floor(Date.now() / 1000);
          state.settings.accepted_notices = notices;
          if (hide) {
            state.settings.hidden_notices = state.settings.hidden_notices || {};
            state.settings.hidden_notices[provider.notice] = true;
            buildSettings();
          }
        }
        state.starting = false;
        state.test = data.test;
        state.running = true;
        renderTest();
        startPolling();
      },
      function (error) {
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
    if (!state.running || state.stopping) {
      return;
    }
    state.stopping = true;
    renderControls();
    api("POST", "/test/stop").catch(function () {
      state.stopping = false;
      renderControls();
    });
  }

  function loadLast() {
    return api("GET", "/history?limit=1").then(
      function (data) {
        state.lastResult = (data.items && data.items[0]) || null;
        state.historyCount = data.count || 0;
      },
      function () {
        /* Not essential: the board simply opens empty. */
      }
    );
  }

  /* The window stays loaded while it is hidden or behind another. On coming
     back, follow a test that someone else started and show the latest result. */
  var resyncedAt = 0;

  function resync() {
    var now = Date.now();
    if (state.gate || state.running || state.starting || !state.info || now - resyncedAt < 3000) {
      return;
    }
    resyncedAt = now;
    api("GET", "/state")
      .then(function (live) {
        if (live.running && live.test) {
          state.test = live.test;
          state.running = true;
          startPolling();
          return null;
        }
        return loadLast().then(function () {
          renderTest();
          if (state.view === "history") {
            loadHistory();
          }
        });
      })
      .catch(function () {
        /* api() already shows the gate when the session or the service is gone. */
      });
  }

  /* ---- Dialogs ---------------------------------------------------------- */

  function openNotice(provider) {
    $("dlg-notice-title").textContent = t("notice." + provider.notice + ".title");
    $("dlg-notice-body").textContent = t("notice." + provider.notice + ".body");
    var link = $("dlg-notice-link");
    var safe = /^https:\/\//.test(provider.notice_url || "");
    link.parentElement.hidden = !safe;
    if (safe) {
      link.href = provider.notice_url;
    }
    $("dlg-notice-hide").checked = false;
    $("dlg-notice").showModal();
  }

  var confirmDone = null;

  function confirmDialog(title, body, okLabel) {
    $("dlg-confirm-title").textContent = title;
    $("dlg-confirm-body").textContent = body;
    $("dlg-confirm-ok").textContent = okLabel;
    return new Promise(function (resolve) {
      confirmDone = resolve;
      $("dlg-confirm").showModal();
    });
  }

  function serverRow(provider, server, checked) {
    var row = el("button", "row");
    row.type = "button";
    row.setAttribute("role", "radio");
    row.setAttribute("aria-checked", String(checked));
    row.tabIndex = checked ? 0 : -1;
    var text = el("span");
    if (!server) {
      text.append(el("span", "row-main", t("servers.auto")));
      row.append(text);
    } else {
      text.append(
        el("span", "row-main", serverLabel(server)),
        el("span", "row-sub", joined([server.country, server.host]))
      );
      row.append(
        text,
        el("span", "row-num", isNumber(server.distance_km) ? fmtInt(server.distance_km) + " km" : ""),
        el("span", "row-num", isNumber(server.latency_ms) ? fmtMs(server.latency_ms) + " ms" : t("servers.noreply"))
      );
    }
    row.addEventListener("click", function () {
      if (server) {
        state.serverChoice[provider.id] = {
          id: String(server.id),
          label: serverLabel(server),
          sub: joined([
            isNumber(server.distance_km) ? t("server.distance", { v: fmtInt(server.distance_km) }) : "",
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
    var list = $("servers-list");
    var choice = state.serverChoice[provider.id];
    list.textContent = "";
    list.append(serverRow(provider, null, !choice));
    if (status) {
      list.append(el("p", "note rows-note", t("servers." + status)));
      return;
    }
    if (!servers.length) {
      list.append(el("p", "note rows-note", t("servers.empty")));
      return;
    }
    servers.forEach(function (server) {
      list.append(serverRow(provider, server, !!choice && choice.id === String(server.id)));
    });
  }

  function loadServers(provider, query) {
    state.serversToken += 1;
    var token = state.serversToken;
    renderServerRows(provider, [], "loading");
    var path = "/servers?provider=" + encodeURIComponent(provider.id);
    if (query) {
      path += "&q=" + encodeURIComponent(query);
    }
    api("GET", path).then(
      function (data) {
        if (token === state.serversToken) {
          renderServerRows(provider, data.servers || [], "");
        }
      },
      function () {
        if (token === state.serversToken) {
          renderServerRows(provider, [], "failed");
        }
      }
    );
  }

  function openServers() {
    var provider = currentProvider();
    if (!provider || !provider.selectable_servers || state.running || state.starting) {
      return;
    }
    var input = $("servers-q");
    input.value = "";
    input.hidden = !provider.searchable;
    $("dlg-servers").showModal();
    loadServers(provider, "");
  }

  /* ---- History ---------------------------------------------------------- */

  function median(values) {
    var list = values.filter(isNumber).sort(function (a, b) {
      return a - b;
    });
    if (!list.length) {
      return null;
    }
    var middle = Math.floor(list.length / 2);
    return list.length % 2 ? list[middle] : (list[middle - 1] + list[middle]) / 2;
  }

  function speedOf(result, kind) {
    return result[kind] && isNumber(result[kind].mbps) ? result[kind].mbps : null;
  }

  function medianSpeed(results, kind) {
    return median(
      results.map(function (result) {
        return speedOf(result, kind);
      })
    );
  }

  function latencyOf(result) {
    return result.ping && isNumber(result.ping.latency) ? result.ping.latency : null;
  }

  function filteredHistory() {
    var items = (state.history && state.history.items) || [];
    if (!state.historyFilter) {
      return items;
    }
    return items.filter(function (result) {
      return result.provider === state.historyFilter;
    });
  }

  function loadHistory() {
    api("GET", "/history").then(
      function (data) {
        state.history = data;
        state.historyCount = data.count || 0;
        if (!state.running) {
          state.lastResult = (data.items && data.items[0]) || null;
        }
        renderHistory();
      },
      function () {
        if (!state.gate && !state.history) {
          state.history = { items: [], count: 0, enabled: true, limit: 0 };
          renderHistory();
        }
      }
    );
  }

  function pinResult(result) {
    if (state.running) {
      return;
    }
    state.pinned = result;
    showView("test");
    renderTest();
    $("status-title").focus();
  }

  function miniTrace(result) {
    var svg = svgEl("svg", "mini");
    svg.setAttribute("viewBox", "0 0 200 100");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    var down = directionFromResult(result.download);
    var up = directionFromResult(result.upload);
    var peak = 1;
    [down, up].forEach(function (direction) {
      if (direction) {
        direction.points.forEach(function (point) {
          peak = Math.max(peak, point[1]);
        });
      }
    });
    [
      [down, "sk-down", 0],
      [up, "sk-up", 104]
    ].forEach(function (item) {
      if (item[0] && item[0].points.length > 1) {
        var path = svgEl("path", item[1]);
        path.setAttribute("d", areaPath(item[0].points, 0, Infinity, item[0].domain, peak * 1.05, 96, item[2]));
        svg.append(path);
      }
    });
    return svg;
  }

  function chartLabel(result) {
    return (
      fmtDate(result.ts) +
      " · ↓ " +
      fmtSpeedUnit(speedOf(result, "download")) +
      " · ↑ " +
      fmtSpeedUnit(speedOf(result, "upload"))
    );
  }

  function announceChartSelection() {
    var hits = $("h-skyline").querySelectorAll(".sk-hit");
    for (var i = 0; i < hits.length; i += 1) {
      hits[i].classList.toggle("sk-selected", i === state.chartIndex);
    }
    var result = state.chartItems[state.chartIndex];
    $("h-chart-selection").textContent = result ? chartLabel(result) : "";
  }

  function drawSkyline(items) {
    var svg = $("h-skyline");
    svg.textContent = "";
    var ordered = items.slice().reverse();
    state.chartItems = ordered;
    state.chartIndex = ordered.length - 1;
    var peak = 1;
    ordered.forEach(function (result) {
      peak = Math.max(peak, speedOf(result, "download") || 0, speedOf(result, "upload") || 0);
    });
    var yMax = niceMax(peak * 1.05);
    var slot = 1000 / Math.max(ordered.length, 24);
    ordered.forEach(function (result, index) {
      var x = index * slot;
      var hit = svgEl("rect", "sk-hit");
      hit.setAttribute("x", x.toFixed(2));
      hit.setAttribute("y", "0");
      hit.setAttribute("width", slot.toFixed(2));
      hit.setAttribute("height", "100");
      var title = svgEl("title");
      title.textContent = chartLabel(result);
      hit.append(title);
      hit.classList.toggle("sk-selected", index === state.chartIndex);
      hit.addEventListener("click", function () {
        pinResult(result);
      });
      svg.append(hit);
      [
        ["download", "sk-down", 0.08],
        ["upload", "sk-up", 0.52]
      ].forEach(function (item) {
        var value = speedOf(result, item[0]);
        if (value === null) {
          return;
        }
        var height = Math.max(1, (value / yMax) * 100);
        var bar = svgEl("rect", item[1]);
        bar.setAttribute("x", (x + slot * item[2]).toFixed(2));
        bar.setAttribute("y", (100 - height).toFixed(2));
        bar.setAttribute("width", (slot * 0.4).toFixed(2));
        bar.setAttribute("height", height.toFixed(2));
        svg.append(bar);
      });
    });
    announceChartSelection();
    $("h-chart-note").textContent = t("hist.chart") + " 0–" + fmtInt(yMax) + " " + t("unit.speed") + ".";
  }

  function renderProviderTable(items) {
    var groups = {};
    var order = [];
    items.forEach(function (result) {
      if (!groups[result.provider]) {
        groups[result.provider] = { name: result.provider_name || result.provider, items: [] };
        order.push(result.provider);
      }
      groups[result.provider].items.push(result);
    });
    order.sort(function (a, b) {
      return groups[b].items.length - groups[a].items.length;
    });
    var body = $("h-table-body");
    body.textContent = "";
    order.forEach(function (id) {
      var group = groups[id];
      var row = el("tr");
      row.append(
        el("td", "", group.name),
        el("td", "num", fmtInt(group.items.length)),
        el("td", "num", fmtSpeed(medianSpeed(group.items, "download"))),
        el("td", "num", fmtSpeed(medianSpeed(group.items, "upload"))),
        el("td", "num", fmtMs(median(group.items.map(latencyOf))))
      );
      body.append(row);
    });
  }

  function directionFacts(direction) {
    if (!direction) {
      return "—";
    }
    return joined([
      fmtSpeedUnit(direction.mbps),
      t("fact.peak", { v: fmtSpeed(direction.peak_mbps) }),
      direction.streams ? tn("fact.streams", direction.streams) : "",
      isNumber(direction.latency) ? t("fact.loaded", { v: fmtMs(direction.latency) }) : ""
    ].concat(cpuFacts(direction.cpu)));
  }

  /* A delete that did not go through: say so where the history gives its size
     (one line, so without the detail). */
  function historyFailed(error) {
    if (!state.gate) {
      var text = errorText({ code: error.code });
      $("h-limit").textContent = text;
      announce(text);
    }
  }

  function fillPanel(bar, result) {
    var client = result.client || {};
    var ping = result.ping || {};
    var bytes = ((result.download && result.download.bytes) || 0) + ((result.upload && result.upload.bytes) || 0);
    var panel = el("dl", "pairs bar-panel");
    setPairs(panel, [
      [t("hist.col.server"), joined([serverLine(result.server), result.server && result.server.host]) || "—"],
      [t("route.isp"), client.isp || "—"],
      [result.ip_family ? t("route.ip") + " (IPv" + result.ip_family + ")" : t("route.ip"), client.ip || "—"],
      [t("hist.col.ping"), joined([fmtMs(ping.latency) + " ms", t("fact.jitter", { v: fmtMs(ping.jitter) })])],
      [t("hist.col.down"), directionFacts(result.download)],
      [t("hist.col.up"), directionFacts(result.upload)],
      [t("route.data"), bytes ? fmtBytes(bytes) : "—"],
      [t("route.time"), fmtSeconds(result.duration)]
    ]);
    var actions = el("div", "bar-actions");
    var open = el("button", "btn", t("hist.open"));
    open.type = "button";
    open.addEventListener("click", function () {
      pinResult(result);
    });
    var remove = el("button", "btn btn-danger", t("hist.delete"));
    remove.type = "button";
    remove.addEventListener("click", function () {
      confirmDialog(t("hist.delete.title"), t("hist.delete.body"), t("hist.delete")).then(function (yes) {
        if (!yes) {
          return;
        }
        api("POST", "/history/delete", { id: result.id }).then(function () {
          if (state.pinned && state.pinned.id === result.id) {
            state.pinned = null;
          }
          loadHistory();
        }, historyFailed);
      });
    });
    actions.append(open, remove);
    panel.append(actions);
    bar.append(panel);
  }

  function renderHistoryList(items) {
    var list = $("h-list");
    list.textContent = "";
    var head = el("div", "h-list-head");
    head.append(
      el("span", "", ""),
      el("span", "", joined([t("hist.col.provider"), t("hist.col.server")])),
      el("span", "", ""),
      el("span", "num", t("hist.col.down") + " (" + t("unit.speed") + ")"),
      el("span", "num", t("hist.col.up") + " (" + t("unit.speed") + ")"),
      el("span", "num", t("hist.col.ping") + " (ms)")
    );
    list.append(head);
    items.slice(0, state.historyShown).forEach(function (result) {
      var bar = el("details", "bar");
      var summary = el("summary");
      summary.append(
        el("span", "bar-date", fmtDate(result.ts)),
        el("span", "bar-prov", joined([result.provider_name || result.provider, serverLabel(result.server)])),
        miniTrace(result),
        el("span", "bar-num bar-down", fmtSpeed(speedOf(result, "download"))),
        el("span", "bar-num bar-up", fmtSpeed(speedOf(result, "upload"))),
        el("span", "bar-num bar-ping", fmtMs(latencyOf(result)))
      );
      bar.append(summary);
      bar.addEventListener("toggle", function () {
        if (bar.open && !bar.dataset.filled) {
          bar.dataset.filled = "1";
          fillPanel(bar, result);
        }
      });
      list.append(bar);
    });
    $("h-more").hidden = items.length <= state.historyShown;
  }

  function renderHistory() {
    var data = state.history;
    if (!data) {
      return;
    }
    var all = data.items || [];
    var empty = !all.length;
    ["h-down", "h-up", "h-ping", "h-list"].forEach(function (id) {
      $(id).hidden = empty;
    });
    ["h-head", "h-chart", "h-table"].forEach(function (name) {
      document.querySelector("." + name).hidden = empty;
    });
    $("h-empty").hidden = !empty;
    if (empty) {
      $("h-more").hidden = true;
      $("h-empty-title").textContent = t(data.enabled === false ? "hist.disabled" : "hist.empty");
      $("h-empty-sub").textContent = data.enabled === false ? "" : t("hist.empty.sub");
      return;
    }

    // Provider filter, rebuilt from what the history really holds.
    var filter = $("h-filter");
    var names = {};
    all.forEach(function (result) {
      names[result.provider] = result.provider_name || result.provider;
    });
    if (state.historyFilter && !names[state.historyFilter]) {
      state.historyFilter = "";
    }
    filter.textContent = "";
    var first = el("option", "", t("hist.all"));
    first.value = "";
    filter.append(first);
    Object.keys(names).forEach(function (id) {
      var option = el("option", "", names[id]);
      option.value = id;
      filter.append(option);
    });
    filter.value = state.historyFilter;

    var items = filteredHistory();
    $("h-count").textContent = tn("hist.count", all.length);
    $("h-limit").textContent =
      data.enabled === false ? t("hist.disabled") : t("hist.limit", { max: fmtInt(data.limit) });
    $("h-down-value").textContent = fmtSpeed(medianSpeed(items, "download"));
    $("h-up-value").textContent = fmtSpeed(medianSpeed(items, "upload"));
    $("h-ping-value").textContent = fmtMs(median(items.map(latencyOf)));
    drawSkyline(items);
    renderProviderTable(all);
    renderHistoryList(items);
  }

  function csvCell(value) {
    if (value === null || value === undefined) {
      return "";
    }
    var text = String(value);
    // Names come from third-party servers: keep spreadsheets from running them.
    if (typeof value === "string" && /^[=+\-@\t\r]/.test(text)) {
      text = "'" + text;
    }
    if (/[",\r\n]/.test(text)) {
      text = '"' + text.replace(/"/g, '""') + '"';
    }
    return text;
  }

  function exportCsv() {
    var rows = [
      [
        "date",
        "provider",
        "server",
        "server_host",
        "distance_km",
        "isp",
        "ip",
        "ip_version",
        "latency_ms",
        "jitter_ms",
        "download_mbps",
        "upload_mbps",
        "download_peak_mbps",
        "upload_peak_mbps",
        "download_connections",
        "upload_connections",
        "download_bytes",
        "upload_bytes",
        "download_loaded_latency_ms",
        "upload_loaded_latency_ms",
        "duration_s",
        "download_nas_cpu_percent",
        "upload_nas_cpu_percent",
        "download_nas_limit",
        "upload_nas_limit"
      ]
    ];
    filteredHistory().forEach(function (result) {
      var server = result.server || {};
      var client = result.client || {};
      var ping = result.ping || {};
      var down = result.download || {};
      var up = result.upload || {};
      var downCpu = down.cpu || {};
      var upCpu = up.cpu || {};
      rows.push([
        isNumber(result.ts) ? new Date(result.ts * 1000).toISOString() : "",
        result.provider_name || result.provider,
        serverLabel(server),
        server.host,
        server.distance_km,
        client.isp,
        client.ip,
        result.ip_family,
        ping.latency,
        ping.jitter,
        down.mbps,
        up.mbps,
        down.peak_mbps,
        up.peak_mbps,
        down.streams,
        up.streams,
        down.bytes,
        up.bytes,
        down.latency,
        up.latency,
        result.duration,
        downCpu.busy,
        upCpu.busy,
        downCpu.limit,
        upCpu.limit
      ]);
    });
    var csv = rows
      .map(function (row) {
        return row.map(csvCell).join(",");
      })
      .join("\r\n");
    var url = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
    var link = el("a");
    link.href = url;
    link.download = "naspeed-history.csv";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 2000);
  }

  function clearHistory() {
    var count = state.history ? (state.history.items || []).length : 0;
    confirmDialog(t("hist.clear.title"), tn("hist.clear.body", count), t("hist.clear")).then(function (yes) {
      if (!yes) {
        return;
      }
      api("POST", "/history/clear").then(function () {
        state.pinned = null;
        state.lastResult = null;
        loadHistory();
      }, historyFailed);
    });
  }

  /* ---- Settings --------------------------------------------------------- */

  var flashTimer = 0;

  function flash(text) {
    $("set-status").textContent = text;
    window.clearTimeout(flashTimer);
    flashTimer = window.setTimeout(function () {
      $("set-status").textContent = "";
    }, 2500);
  }

  function buildSeg(id, options, current, onPick) {
    var box = $(id);
    box.textContent = "";
    options.forEach(function (option) {
      var button = el("button", "", option.label);
      var selected = String(option.value) === String(current);
      button.type = "button";
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(selected));
      button.tabIndex = selected ? 0 : -1;
      button.dataset.value = String(option.value);
      button.addEventListener("click", function () {
        onPick(option.value);
      });
      button.addEventListener("keydown", function (event) {
        var step = ARROW_STEPS[event.key];
        if (!step || !options.length) {
          return;
        }
        event.preventDefault();
        var index = options.findIndex(function (item) {
          return String(item.value) === button.dataset.value;
        });
        var next = options[(index + step + options.length) % options.length];
        var nextButton = box.querySelector('[data-value="' + next.value + '"]');
        if (nextButton) {
          nextButton.focus();
        }
        onPick(next.value);
      });
      box.append(button);
    });
  }

  function saveSetting(patch) {
    var focused = document.activeElement;
    var group = focused && focused.closest ? focused.closest(".seg[role='radiogroup']") : null;
    var groupId = group && group.id;
    var focusedValue = focused && focused.dataset && focused.dataset.value;
    var restoreFocus = function () {
      if (!groupId || focusedValue === undefined) {
        return;
      }
      var buttons = $(groupId).querySelectorAll("button[role='radio']");
      for (var i = 0; i < buttons.length; i += 1) {
        if (buttons[i].dataset.value === focusedValue) {
          buttons[i].focus();
          break;
        }
      }
    };
    api("POST", "/settings", patch).then(
      function (settings) {
        state.settings = settings;
        flash(t("set.saved"));
        buildSettings();
        renderTest();
        restoreFocus();
      },
      function () {
        if (!state.gate) {
          flash(t("set.failed"));
          buildSettings();
          restoreFocus();
        }
      }
    );
  }

  function buildSettings() {
    if (!state.info) {
      return;
    }
    var settings = state.settings;
    var options = state.info.options || {};
    var durations = options.durations || {};
    buildSeg(
      "set-duration",
      ["quick", "standard", "long"]
        .filter(function (key) {
          return durations[key] !== undefined;
        })
        .map(function (key) {
          return { value: key, label: t("duration." + key) + " · " + durations[key] + " s" };
        }),
      settings.duration,
      function (value) {
        saveSetting({ duration: value });
      }
    );
    buildSeg(
      "set-connections",
      (options.connections || []).map(function (value) {
        return { value: value, label: value === "auto" ? t("value.auto") : value };
      }),
      String(settings.connections),
      function (value) {
        saveSetting({ connections: value });
      }
    );
    buildSeg(
      "set-ip",
      (options.ip_versions || []).map(function (value) {
        return { value: value, label: value === "auto" ? t("value.auto") : "IPv" + value };
      }),
      String(settings.ip_version),
      function (value) {
        saveSetting({ ip_version: value });
      }
    );
    $("set-history").setAttribute("aria-checked", String(settings.save_history !== false));
    $("set-history-state").textContent = t(settings.save_history !== false ? "value.on" : "value.off");
    // Only M-Lab has a notice today; the switch follows the first provider that has one.
    var noticed = state.providers.filter(function (provider) {
      return provider.notice;
    })[0];
    $("notice-row").hidden = !noticed;
    if (noticed) {
      $("set-notice").setAttribute("data-notice", noticed.notice);
      $("set-notice").setAttribute("aria-checked", String(!(settings.hidden_notices || {})[noticed.notice]));
    }
    var limit = $("set-limit");
    limit.max = String(options.history_max || 1000);
    limit.value = String(settings.history_limit);
    $("set-limit-help").textContent = t("set.limit.help", { max: fmtInt(options.history_max || 1000) });

    var version = state.info.version;
    $("about-version").textContent = "NASpeed " + (state.info.beta ? t("about.beta", { version: version }) : version);
    $("about-mode").textContent = has("about.mode." + state.info.mode) ? t("about.mode." + state.info.mode) : "";
    var licenseParts = t("about.license").split("{license}");
    var licenseLink = externalLink("GPL-3.0-only", "https://www.gnu.org/licenses/gpl-3.0.html");
    $("about-license").textContent = "";
    $("about-license").append(licenseParts[0], licenseLink, licenseParts.slice(1).join(""));
    var list = $("about-providers");
    list.textContent = "";
    state.providers.forEach(function (provider) {
      var name = el("dt");
      if (/^https:\/\//.test(provider.homepage || "")) {
        name.append(externalLink(provider.name, provider.homepage));
      } else {
        name.textContent = provider.name;
      }
      var item = el("div", "s-prov");
      item.append(name, el("dd", "", has("about.prov." + provider.id) ? t("about.prov." + provider.id) : ""));
      list.append(item);
    });
  }

  /* ---- Views and events ------------------------------------------------- */

  function showView(name) {
    if (VIEWS.indexOf(name) < 0) {
      return;
    }
    var changed = state.view !== name;
    state.view = name;
    VIEWS.forEach(function (view) {
      $("view-" + view).hidden = view !== name;
    });
    var buttons = $("nav").querySelectorAll("button");
    for (var i = 0; i < buttons.length; i += 1) {
      if (buttons[i].dataset.view === name) {
        buttons[i].setAttribute("aria-current", "page");
      } else {
        buttons[i].removeAttribute("aria-current");
      }
    }
    if (name === "history") {
      state.historyShown = HISTORY_PAGE;
      renderHistory();
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
    $("nav").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-view]");
      if (button) {
        showView(button.dataset.view);
      }
    });

    $("action").addEventListener("click", function () {
      if (state.running) {
        stopTest();
      } else {
        startTest(false);
      }
    });

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        resync();
      }
    });
    window.addEventListener("focus", resync);

    $("theme-toggle").addEventListener("click", function () {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    });
    var schemeChanged = function () {
      renderThemeControls();
    };
    if (darkScheme.addEventListener) {
      darkScheme.addEventListener("change", schemeChanged);
    }

    $("lang-settings").addEventListener("change", function (event) {
      applyLanguage(event.target.value);
      prefs.set("language", lang);
      renderAll();
      saveSetting({ language: lang });
    });

    // Arrow keys move between providers, as in any radio group.
    $("providers").addEventListener("keydown", function (event) {
      var step = ARROW_STEPS[event.key];
      if (!step || state.running || state.starting || !state.providers.length) {
        return;
      }
      event.preventDefault();
      var index = 0;
      state.providers.forEach(function (provider, i) {
        if (provider.id === state.providerId) {
          index = i;
        }
      });
      var next = state.providers[(index + step + state.providers.length) % state.providers.length];
      selectProvider(next.id);
      var button = $("providers").querySelector('[data-id="' + next.id + '"]');
      if (button) {
        button.focus();
      }
    });

    $("servers-list").addEventListener("keydown", function (event) {
      var step = ARROW_STEPS[event.key];
      if (!step) {
        return;
      }
      var radios = $("servers-list").querySelectorAll("button[role='radio']");
      if (!radios.length) {
        return;
      }
      event.preventDefault();
      var index = Array.prototype.indexOf.call(radios, event.target);
      var next = radios[(Math.max(index, 0) + step + radios.length) % radios.length];
      next.focus();
      next.click();
    });

    $("server-change").addEventListener("click", openServers);
    var searchTimer = 0;
    $("servers-q").addEventListener("input", function (event) {
      window.clearTimeout(searchTimer);
      var value = event.target.value.trim();
      searchTimer = window.setTimeout(function () {
        var provider = currentProvider();
        if (provider && $("dlg-servers").open) {
          loadServers(provider, value);
        }
      }, 350);
    });

    // Dialogs close with their own buttons and with a click outside them.
    var dialogs = document.querySelectorAll("dialog");
    for (var i = 0; i < dialogs.length; i += 1) {
      dialogs[i].addEventListener("click", function (event) {
        var dialog = event.currentTarget;
        var box = dialog.getBoundingClientRect();
        var outside =
          event.target === dialog &&
          (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);
        if (outside || event.target.closest("[data-close]")) {
          dialog.close();
        }
      });
    }
    $("dlg-notice-ok").addEventListener("click", function () {
      var hide = $("dlg-notice-hide").checked;
      $("dlg-notice").close();
      startTest(true, hide);
    });
    $("dlg-confirm-ok").addEventListener("click", function () {
      var done = confirmDone;
      confirmDone = null;
      $("dlg-confirm").close();
      if (done) {
        done(true);
      }
    });
    $("dlg-confirm").addEventListener("close", function () {
      var done = confirmDone;
      confirmDone = null;
      if (done) {
        done(false);
      }
    });

    $("h-filter").addEventListener("change", function (event) {
      state.historyFilter = event.target.value;
      state.historyShown = HISTORY_PAGE;
      renderHistory();
    });
    $("h-skyline").addEventListener("keydown", function (event) {
      if (!state.chartItems.length) {
        return;
      }
      var step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (step) {
        event.preventDefault();
        state.chartIndex = Math.max(0, Math.min(state.chartItems.length - 1, state.chartIndex + step));
        announceChartSelection();
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        pinResult(state.chartItems[state.chartIndex]);
      }
    });
    $("h-export").addEventListener("click", exportCsv);
    $("h-clear").addEventListener("click", clearHistory);
    $("h-more").addEventListener("click", function () {
      state.historyShown += HISTORY_PAGE;
      renderHistoryList(filteredHistory());
    });

    $("set-history").addEventListener("click", function () {
      saveSetting({ save_history: $("set-history").getAttribute("aria-checked") !== "true" });
    });
    $("set-notice").addEventListener("click", function () {
      var button = $("set-notice");
      var hidden = {};
      // The switch reads "warn before every test", so turning it off hides the notice.
      hidden[button.getAttribute("data-notice")] = button.getAttribute("aria-checked") === "true";
      saveSetting({ hidden_notices: hidden });
    });
    $("set-limit").addEventListener("change", function (event) {
      var max = (state.info && state.info.options && state.info.options.history_max) || 1000;
      var value = parseInt(event.target.value, 10);
      if (!isNumber(value)) {
        buildSettings();
        return;
      }
      saveSetting({ history_limit: Math.max(10, Math.min(max, value)) });
    });
  }

  function init() {
    fillLanguageSelect();
    applyLanguage(cachedLang());
    bind();
    renderThemeControls();
    renderTest();
    api("GET", "/info")
      .then(function (info) {
        if (!startup.isInfoResponse(info, APP_ID)) {
          showGate("unexpected");
          return null;
        }
        var refresh = startup.refreshForVersion(
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
        var ids = state.providers.map(function (provider) {
          return provider.id;
        });
        var wanted = [state.settings.default_provider, "librespeed"].filter(function (id) {
          return ids.indexOf(id) >= 0;
        });
        state.providerId = wanted.length ? wanted[0] : ids[0] || "";
        adoptSavedLanguage();
        buildProviders();
        buildSettings();
        return api("GET", "/state");
      })
      .then(function (live) {
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
        function () {
          if (state.info) {
            renderTest();
          }
        },
        function () {
          if (!state.gate) {
            showGate("offline");
          }
        }
      );
  }

  init();
})();
