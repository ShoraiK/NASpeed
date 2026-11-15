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
