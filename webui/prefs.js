// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - browser preferences (theme and language).
 *
 * Loaded before the page is painted so the saved theme is applied without a
 * flash. Inside TOS the page shares its origin with the desktop, so every key
 * is prefixed. */
(() => {
  "use strict";

  const PREFIX = "naspeed.";
  const darkScheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

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

  /* "mbps" or "auto" (switches to Gbps from 1000 Mbps upward). */
  function speedUnit() {
    return get("speedUnit") === "auto" ? "auto" : "mbps";
  }

  /* "light", "dark" or "" (follow the system). */
  function theme() {
    const value = get("theme");
    return value === "light" || value === "dark" ? value : "";
  }

  function applyTheme(value) {
    const resolved =
      value === "light" || value === "dark"
        ? value
        : (darkScheme && darkScheme.matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", resolved);
  }

  applyTheme(theme());
  if (darkScheme && darkScheme.addEventListener) {
    darkScheme.addEventListener("change", () => {
      if (!theme()) {
        applyTheme("");
      }
    });
  }

  /* Inside the TOS desktop the page lives in an iframe and TOS lays its window
     controls over the top of it; the stylesheet keeps that strip clear. */
  if (window.self !== window.top) {
    document.documentElement.classList.add("embedded");
  }

  window.NASpeedPrefs = { get, set, theme, applyTheme, speedUnit };
})();
