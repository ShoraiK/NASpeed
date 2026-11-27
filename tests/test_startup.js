// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const replacements = [];
const window = {
  location: {
    pathname: "/naspeed/",
    search: "?mode=desktop",
    hash: "#settings",
    replace(url) {
      replacements.push(url);
    }
  }
};

vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "webui", "startup.js"), "utf8"),
  { window }
);

const startup = window.NASpeedStartup;

assert.equal(startup.isInfoResponse({ app: "naspeed", version: "0.2.3" }, "naspeed"), true);
assert.equal(startup.isInfoResponse({ app: "other", version: "0.2.3" }, "naspeed"), false);
assert.equal(startup.isInfoResponse({ app: "naspeed", version: "v0.2.3" }, "naspeed"), false);
assert.equal(startup.isInfoResponse(null, "naspeed"), false);

assert.equal(startup.refreshForVersion("0.2.2", "0.2.2"), "current");
assert.equal(startup.refreshForVersion("0.2.3", "0.2.2"), "reloading");
assert.equal(replacements[0], "/naspeed/?mode=desktop&naspeed_version=0.2.3#settings");
window.location.search = "?mode=desktop&naspeed_version=0.2.3";
assert.equal(startup.refreshForVersion("0.2.3", "0.2.2"), "stale");
assert.equal(replacements.length, 1);
assert.equal(startup.refreshForVersion("0.2.4", "0.2.2"), "reloading");
assert.equal(replacements.length, 2);

// Check theme preference resolution and automatic system theme switching.
function prefsFixture(systemDark) {
  const attrs = {};
  const classes = new Set();
  const storage = new Map();
  let onSchemeChange = null;
  const prefsWindow = {
    self: {},
    top: {},
    localStorage: {
      getItem: key => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key)
    },
    matchMedia: () => ({
      get matches() { return systemDark; },
      addEventListener: (_type, fn) => { onSchemeChange = fn; }
    })
  };
  prefsWindow.self = prefsWindow;
  const prefsDocument = {
    documentElement: {
      setAttribute: (name, value) => { attrs[name] = value; },
      classList: { add: name => classes.add(name) }
    }
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "webui", "prefs.js"), "utf8"),
    { window: prefsWindow, document: prefsDocument }
  );
  return {
    prefs: prefsWindow.NASpeedPrefs,
    attrs,
    classes,
    setSystemDark(value) {
      systemDark = value;
      if (onSchemeChange) onSchemeChange();
    }
  };
}

const pf = prefsFixture(true);
assert.equal(pf.attrs["data-theme"], "dark");
assert.equal(pf.classes.has("embedded"), true);
pf.setSystemDark(false);
assert.equal(pf.attrs["data-theme"], "light");
pf.prefs.set("theme", "dark");
pf.prefs.applyTheme("dark");
pf.setSystemDark(false);
assert.equal(pf.attrs["data-theme"], "dark");
pf.prefs.set("theme", "");
pf.prefs.applyTheme("");
assert.equal(pf.attrs["data-theme"], "light");

assert.equal(pf.prefs.speedUnit(), "mbps");
pf.prefs.set("speedUnit", "auto");
assert.equal(pf.prefs.speedUnit(), "auto");
pf.prefs.set("speedUnit", "other");
assert.equal(pf.prefs.speedUnit(), "mbps");
pf.prefs.set("speedUnit", "");
assert.equal(pf.prefs.speedUnit(), "mbps");

// Verify i18n parity across all languages and placeholders.
(() => {
  const i18nWindow = {};
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "webui", "i18n.js"), "utf8"),
    { window: i18nWindow }
  );
  const i18n = i18nWindow.NASpeedI18n;
  assert.ok(i18n && Array.isArray(i18n.order) && i18n.langs && i18n.fallback, "missing NASpeedI18n metadata");
  assert.ok(i18n.langs[i18n.fallback], "missing fallback language dictionary");
  assert.deepEqual([...i18n.order].sort(), Object.keys(i18n.langs).sort(), "i18n.order does not match langs");

  const placeholders = text =>
    [...new Set([...String(text).matchAll(/\{([a-zA-Z0-9_]+)\}/g)].map(m => m[1]))].sort();
  const baseText = i18n.langs[i18n.fallback].text;
  const baseKeys = Object.keys(baseText).sort();
  assert.ok(baseKeys.length > 0, "empty base i18n dictionary");

  for (const code of i18n.order) {
    const entry = i18n.langs[code];
    assert.ok(entry && entry.name && entry.locale && entry.tag && entry.text, `incomplete entry for ${code}`);
    assert.deepEqual(Object.keys(entry.text).sort(), baseKeys, `key mismatch for language ${code}`);
    for (const key of baseKeys) {
      assert.equal(typeof entry.text[key], "string", `${code}.${key} must be a string`);
      assert.ok(entry.text[key].length > 0, `${code}.${key} must not be empty`);
      assert.deepEqual(
        placeholders(entry.text[key]),
        placeholders(baseText[key]),
        `placeholder mismatch in ${code}.${key}`
      );
    }
  }
})();

// Verify every static DOM element id referenced in webui/*.js exists in index.html.
(() => {
  const webuiDir = path.join(__dirname, "..", "webui");
  const html = fs.readFileSync(path.join(webuiDir, "index.html"), "utf8");
  const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
  const dynamicIds = new Set(["h-chart-cursor"]);
  const missing = [];
  for (const file of fs.readdirSync(webuiDir).filter(f => f.endsWith(".js")).sort()) {
    const code = fs.readFileSync(path.join(webuiDir, file), "utf8");
    for (const m of code.matchAll(/(?<![\w$])(?:\$|getElementById)\(\s*["']([^"']+)["']\s*\)/g)) {
      const id = m[1];
      if (!htmlIds.has(id) && !dynamicIds.has(id)) {
        missing.push(`${file}: #${id}`);
      }
    }
  }
  assert.deepEqual(missing, [], `missing DOM ids in index.html: ${missing.join(", ")}`);
})();

// This is the web UI test entry point used by CI and release builds.
require("./test_webui.js");
