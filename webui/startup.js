// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
((root) => {
  "use strict";

  const RELOAD_PARAM = "naspeed_version";

  function isInfoResponse(info, appId) {
    return !!info &&
      typeof info === "object" &&
      info.app === appId &&
      typeof info.version === "string" &&
      /^\d+(?:\.\d+){0,2}$/.test(info.version);
  }

  function refreshForVersion(infoVersion, pageVersion) {
    if (infoVersion === pageVersion) {
      return "current";
    }

    const search = root.location.search;
    const params = search.slice(1).split("&");
    const marker = `${RELOAD_PARAM}=${encodeURIComponent(infoVersion)}`;
    if (params.includes(marker)) {
      return "stale";
    }

    const separator = search ? "&" : "?";
    const url = root.location.pathname + search + separator + marker + root.location.hash;
    root.location.replace(url);
    return "reloading";
  }

  root.NASpeedStartup = { isInfoResponse, refreshForVersion };
})(window);
