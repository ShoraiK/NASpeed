// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - browser preferences (theme and language).
 *
 * Loaded before the page is painted so the saved theme is applied without a
 * flash. Inside TOS the page shares its origin with the desktop, so every key
 * is prefixed. */
(function () {
  "use strict";

  var PREFIX = "naspeed.";

  function get(name) {
    try {
      return window.localStorage.getItem(PREFIX + name);
    } catch (error) {
      return null;
    }
  }

  function set(name, value) {
    try {
      if (value === null || value === undefined || value === "") {
        window.localStorage.removeItem(PREFIX + name);
      } else {
        window.localStorage.setItem(PREFIX + name, String(value));
      }
    } catch (error) {
      /* Private mode or blocked storage: the choice lasts for this visit only. */
    }
  }

  /* "light", "dark" or "" (follow the system). */
  function theme() {
    var value = get("theme");
    return value === "light" || value === "dark" ? value : "";
  }

  function applyTheme(value) {
    var root = document.documentElement;
    if (value === "light" || value === "dark") {
      root.setAttribute("data-theme", value);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  applyTheme(theme());

  window.NASpeedPrefs = { get: get, set: set, theme: theme, applyTheme: applyTheme };
})();
