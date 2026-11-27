// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - HTTP API client and TOS proxy session headers. */
(() => {
  "use strict";

  function ApiError(status, code, detail) {
    this.status = status;
    this.code = code;
    this.detail = detail || "";
  }

  function cookie(name) {
    const prefix = encodeURIComponent(name) + "=";
    const items = document.cookie.split(";");
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i].trim();
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
  function requestOptions(method, body) {
    const headers = { Accept: "application/json", "X-Requested-With": "NASpeed" };
    const csrf = cookie("X-Csrf-Token");
    if (csrf) {
      headers["X-Csrf-Token"] = csrf;
      const session = cookie("TMSESSNAME");
      if (session) {
        headers.Cookie = "TMSESSNAME=" + session + "; X-Csrf-Token=" + csrf + ";";
      }
    }
    const options = { method, headers, credentials: "include", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return options;
  }

  function createClient({ baseUrl, getGate, onGate }) {
    function request(method, path, body) {
      const gate = getGate();
      if (gate === "denied" || gate === "session") {
        return Promise.reject(new ApiError(0, "gated"));
      }
      return fetch(baseUrl + path, requestOptions(method, body)).then(
        (response) => {
          return response
            .json()
            .catch(() => null)
            .then((data) => {
              // Without a session the TOS proxy answers HTTP 200 with
              // {"is_login": false, "code_num": 117} before NASpeed is reached.
              if (response.status === 401 || (data && data.is_login === false)) {
                onGate("session");
                throw new ApiError(401, "unauthorized");
              }
              if (response.status === 403) {
                onGate("denied");
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
        () => {
          throw new ApiError(0, "offline");
        }
      );
    }

    function stopOnExit(testId) {
      const options = requestOptions("POST", { id: testId });
      options.keepalive = true;
      return fetch(baseUrl + "/test/stop", options).catch(() => {});
    }

    return { request, stopOnExit };
  }

  window.NASpeedApi = { ApiError, cookie, requestOptions, createClient };
})();
