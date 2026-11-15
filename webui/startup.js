// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
(function (root) {
  "use strict";

  var RELOAD_PARAM = "naspeed_version";

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

    var search = root.location.search;
    var params = search.slice(1).split("&");
    var marker = RELOAD_PARAM + "=" + encodeURIComponent(infoVersion);
    if (params.indexOf(marker) !== -1) {
      return "stale";
    }

    var separator = search ? "&" : "?";
    var url = root.location.pathname + search + separator + marker + root.location.hash;
    root.location.replace(url);
    return "reloading";
  }

  root.NASpeedStartup = {
    isInfoResponse: isInfoResponse,
    refreshForVersion: refreshForVersion
  };
})(window);
