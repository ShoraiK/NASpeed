// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - history view, chart picking, lazy thumbnails and CSV export. */
(() => {
  "use strict";

  function createHistory(deps) {
    const {
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
      fmtMs,
      fmtBytes,
      fmtSeconds,
      fmtDate,
      serverLine,
      setPairs,
      cpuFacts,
      markSeg
    } = deps;

    // Results are immutable. Share detail requests between thumbnails and open rows.
    const detailCache = new Map();
    let listShowsTraces = false;
    let listSwap = 0;
    let thumbnailObserver = null;
    let medianCache = { items: null, values: null };
    let downloadResetTimer = 0;

    function disconnectObserver() {
      if (thumbnailObserver) {
        thumbnailObserver.disconnect();
        thumbnailObserver = null;
      }
    }

    function showsTraces() {
      return listShowsTraces;
    }

    function filteredHistory() {
      const items = (state.history && state.history.items) || [];
      if (!state.historyFilter) {
        return items;
      }
      return items.filter((result) => result.provider === state.historyFilter);
    }

    function historyMedians(items) {
      if (medianCache.items === items) {
        return medianCache.values;
      }
      const values = {
        down: format.medianSpeed(items, "download"),
        up: format.medianSpeed(items, "upload"),
        ping: format.median(items.map(format.latencyOf))
      };
      medianCache = { items, values };
      return values;
    }

    function loadDetails(ids) {
      const missing = ids.filter((id) => !detailCache.has(id));
      if (missing.length) {
        const batch = deps.api("GET", "/history?ids=" + missing.map(encodeURIComponent).join(",")).then((data) => {
          return new Map(data.items.map((item) => [item.id, item]));
        });
        missing.forEach((id) => {
          const entry = batch.then((items) => {
            if (!items.has(id)) {
              throw deps.historyChangedError();
            }
            return items.get(id);
          }).catch((error) => {
            if (detailCache.get(id) === entry) {
              detailCache.delete(id);
            }
            throw error;
          });
          detailCache.set(id, entry);
        });
      }
      return ids.map((id) => detailCache.get(id));
    }

    function historyFeedback() {
      const unavailable = !state.history || state.historyLoading || !!state.historyError;
      $("h-clear").disabled = unavailable;
      $("h-export").disabled = unavailable || state.exporting;
      $("h-filter").disabled = state.exporting;
      let message = state.historyError || state.historyNotice;
      if (!message && state.historyLoading && !state.history) {
        message = t("hist.loading");
      }
      $("h-feedback").hidden = !message;
      $("h-feedback-text").textContent = message;
      $("h-retry").hidden = !state.historyError;
    }

    function loadHistory(notice) {
      const request = ++state.historyRequest;
      state.historyLoading = true;
      state.historyError = "";
      state.historyNotice = notice || "";
      historyFeedback();
      return deps.api("GET", "/history?summary=1").then(
        (data) => {
          if (request !== state.historyRequest) {
            return;
          }
          const previous = state.history;
          state.historyLoading = false;
          state.history = data;
          state.historyCount = data.count || 0;
          const ids = new Set(data.items.map((item) => item.id));
          detailCache.forEach((_, id) => {
            if (!ids.has(id)) {
              detailCache.delete(id);
            }
          });
          if (state.pinned && !ids.has(state.pinned.id)) {
            state.pinned = null;
          }
          if (state.lastResult && !ids.has(state.lastResult.id)) {
            state.lastResult = null;
            if (data.items.length && !state.running) {
              deps.loadLast();
            }
          }
          historyFeedback();
          if (!previous || !data.revision || previous.revision !== data.revision || previous.limit !== data.limit ||
              previous.enabled !== data.enabled || deps.getHistoryLanguage() !== deps.getLang()) {
            resetDownload();
            if (state.view === "history") {
              deps.renderHistory();
            } else {
              deps.setHistoryLanguage(""); // The new snapshot has not been drawn yet.
            }
          }
        },
        (error) => {
          if (request !== state.historyRequest || state.gate) {
            return;
          }
          state.historyLoading = false;
          state.historyError = t("hist.loadFailed") + " " + errorText(error);
          historyFeedback();
        }
      );
    }

    function pinResult(result) {
      if (state.running) {
        return;
      }
      state.pinned = result;
      deps.showView("test");
      deps.renderTest();
      $("status-title").focus();
    }

    function renderHistoryFigures(items) {
      const picked = state.chartPicked ? state.chartItems[state.chartIndex] : null;
      const medians = picked ? null : historyMedians(items);
      charts.setChartContext(picked);
      [
        ["h-down", picked ? format.speedOf(picked, "download") : medians.down, fmtSpeed, "down", true],
        ["h-up", picked ? format.speedOf(picked, "upload") : medians.up, fmtSpeed, "up", true],
        ["h-ping", picked ? format.latencyOf(picked) : medians.ping, fmtMs, "ping", false]
      ].forEach((figure) => {
        const caption = $(figure[0]).querySelector(".caption");
        const suffix = figure[4] && deps.useGbps(figure[1]) ? ".gb" : "";
        caption.dataset.i18n = (picked ? figure[3] + ".caption" : "hist.median." + figure[3]) + suffix;
        caption.textContent = t(caption.dataset.i18n);
        charts.tweenFigure($(figure[0] + "-value"), figure[1], figure[2]);
      });
    }

    // A column picked in the chart shows its result in the three tiles above it; picking
    // it again brings back the medians. With keep, the arrow keys move the pick along.
    function pickChart(index, keep) {
      const result = state.chartItems[index];
      if (!result) {
        return;
      }
      state.chartPicked = !keep && state.chartPicked === result.id ? null : result.id;
      state.chartIndex = index;
      charts.announceChartSelection();
      renderHistoryFigures(state.chartItems);
    }

    function clearChartPick() {
      if (!state.chartPicked) {
        return;
      }
      state.chartPicked = null;
      charts.announceChartSelection();
      renderHistoryFigures(state.chartItems);
    }

    function setFilter(providerId) {
      const next = providerId || "";
      if (state.historyFilter === next) {
        return;
      }
      resetDownload();
      state.historyFilter = next;
      state.historyPage = 1;
      deps.renderHistory("fresh");
    }

    function renderProviderTable(items) {
      const groups = {};
      const order = [];
      items.forEach((result) => {
        if (!groups[result.provider]) {
          groups[result.provider] = { name: result.provider_name || result.provider, items: [] };
          order.push(result.provider);
        }
        groups[result.provider].items.push(result);
      });
      order.sort((a, b) => groups[b].items.length - groups[a].items.length);
      const body = $("h-table-body");
      body.textContent = "";
      order.forEach((id) => {
        const group = groups[id];
        const active = state.historyFilter === id;
        const row = el("tr");
        row.dataset.active = String(active);
        const nameCell = el("td");
        const filterButton = el("button", "prov-filter", group.name);
        filterButton.type = "button";
        filterButton.setAttribute("aria-pressed", String(active));
        filterButton.addEventListener("click", () => {
          setFilter(state.historyFilter === id ? "" : id);
        });
        nameCell.append(filterButton);
        row.append(
          nameCell,
          el("td", "num", fmtInt(group.items.length)),
          el("td", "num", deps.fmtSpeedTable(format.medianSpeed(group.items, "download"))),
          el("td", "num", deps.fmtSpeedTable(format.medianSpeed(group.items, "upload"))),
          el("td", "num", fmtMs(format.median(group.items.map(format.latencyOf))))
        );
        body.append(row);
      });
    }

    function directionFacts(direction) {
      if (!direction) {
        return "—";
      }
      return format.joined([
        fmtSpeedUnit(direction.mbps),
        t("fact.peak", { v: fmtSpeed(direction.peak_mbps) }),
        direction.streams ? tn("fact.streams", direction.streams) : "",
        format.isNumber(direction.latency) ? t("fact.loaded", { v: fmtMs(direction.latency) }) : ""
      ].concat(cpuFacts(direction.cpu)));
    }

    /* A delete that did not go through: say so in the history feedback banner. */
    function historyFailed(error) {
      if (!state.gate) {
        const text = errorText(error);
        state.historyNotice = text;
        historyFeedback();
        announce(text);
      }
    }

    function fillPanel(bar, result) {
      const client = result.client || {};
      const ping = result.ping || {};
      const bytes = ((result.download && result.download.bytes) || 0) + ((result.upload && result.upload.bytes) || 0);
      const panel = el("dl", "pairs bar-panel");
      setPairs(panel, [
        [t("hist.col.server"), format.joined([serverLine(result.server), result.server && result.server.host]) || "—"],
        [t("route.isp"), client.isp || "—"],
        [result.ip_family ? t("route.ip") + " (IPv" + result.ip_family + ")" : t("route.ip"), client.ip || "—"],
        [t("hist.col.ping"), format.joined([fmtMs(ping.latency) + " ms", t("fact.jitter", { v: fmtMs(ping.jitter) })])],
        [t("hist.col.down"), directionFacts(result.download)],
        [t("hist.col.up"), directionFacts(result.upload)],
        [t("route.data"), bytes ? fmtBytes(bytes) : "—"],
        [t("route.time"), fmtSeconds(result.duration)]
      ]);
      const actions = el("div", "bar-actions");
      const open = el("button", "btn", t("hist.open"));
      open.type = "button";
      open.addEventListener("click", () => {
        pinResult(result);
      });
      const copy = el("button", "btn", t("action.copy"));
      copy.type = "button";
      copy.addEventListener("click", () => {
        deps.copyResult(copy, result);
      });
      const remove = el("button", "btn btn-danger", t("hist.delete"));
      remove.type = "button";
      remove.addEventListener("click", () => {
        deps.confirmDialog(t("hist.delete.title"), t("hist.delete.body"), t("hist.delete"), true).then((yes) => {
          if (!yes) {
            return;
          }
          deps.api("POST", "/history/delete", { id: result.id }).then(() => {
            if (state.pinned && state.pinned.id === result.id) {
              state.pinned = null;
            }
            loadHistory();
          }, historyFailed);
        });
      });
      actions.append(open, copy, remove);
      panel.append(actions);
      bar.append(panel);
    }

    function attachThumbnails(visible) {
      const promises = loadDetails(visible.map((item) => item.id));
      visible.forEach((item, index) => {
        promises[index].then((detail) => {
          if (item.thumbnail.isConnected) {
            item.thumbnail.replaceWith(charts.miniTrace(detail));
          }
        }).catch(() => {
          // Thumbnails are decorative. Opening the row retries and reports a detail error.
        });
      });
    }

    function renderHistoryList(items, motion) {
      disconnectObserver();
      const animate = Boolean(motion) && !reduceMotion.matches;
      const size = state.historyPageSize;
      clampPage(items.length);
      const first = (state.historyPage - 1) * size;
      const list = $("h-list");
      list.textContent = "";
      list.dataset.motion = animate ? motion : "";
      const autoUnit = deps.isAutoUnit();
      const head = el("div", "h-list-head");
      head.append(
        el("span", "", ""),
        el("span", "", format.joined([t("hist.col.provider"), t("hist.col.server")])),
        el("span", "", ""),
        el("span", "num", autoUnit ? t("hist.col.down") : t("hist.col.down") + " (" + t("unit.speed") + ")"),
        el("span", "num", autoUnit ? t("hist.col.up") : t("hist.col.up") + " (" + t("unit.speed") + ")"),
        el("span", "num", t("hist.col.ping") + " (ms)")
      );
      list.append(head);
      const page = items.slice(first, first + size);
      listShowsTraces = showMiniTraces.matches && state.view === "history";
      const useObserver = listShowsTraces && typeof window.IntersectionObserver === "function";
      const pendingThumbs = new Map();
      if (useObserver) {
        thumbnailObserver = new window.IntersectionObserver((entries, observer) => {
          const visible = [];
          entries.forEach((entry) => {
            if (!entry.isIntersecting) {
              return;
            }
            observer.unobserve(entry.target);
            const target = pendingThumbs.get(entry.target);
            if (target) {
              pendingThumbs.delete(entry.target);
              visible.push(target);
            }
          });
          if (visible.length) {
            attachThumbnails(visible);
          }
        }, { rootMargin: "200px 0px" });
      }
      const eagerItems = [];
      const unit = t("unit.speed");
      page.forEach((result, index) => {
        const bar = el("details", animate ? "bar row-in" : "bar");
        if (animate) {
          bar.style.setProperty("--i", String(Math.min(index, 12))); // one after another
        }
        const summary = el("summary");
        const thumbnail = svgEl("svg", "mini");
        thumbnail.setAttribute("aria-hidden", "true");
        const downValue = format.speedOf(result, "download");
        const upValue = format.speedOf(result, "upload");
        const downCell = el("span", "bar-num bar-down", autoUnit ? deps.fmtSpeedTable(downValue) : fmtSpeed(downValue));
        const upCell = el("span", "bar-num bar-up", autoUnit ? deps.fmtSpeedTable(upValue) : fmtSpeed(upValue));
        if (autoUnit) {
          downCell.dataset.hasUnit = "true";
          upCell.dataset.hasUnit = "true";
        } else {
          downCell.dataset.unit = unit;
          upCell.dataset.unit = unit;
        }
        summary.append(
          el("span", "bar-date", fmtDate(result.ts)),
          el("span", "bar-prov", format.joined([result.provider_name || result.provider, format.serverLabel(result.server)])),
          thumbnail,
          downCell,
          upCell,
          el("span", "bar-num bar-ping", fmtMs(format.latencyOf(result)))
        );
        if (thumbnailObserver) {
          pendingThumbs.set(bar, { id: result.id, thumbnail });
          thumbnailObserver.observe(bar);
        } else if (listShowsTraces) {
          eagerItems.push({ id: result.id, thumbnail });
        }
        bar.append(summary);
        bar.addEventListener("toggle", function loadPanel() {
          if (!bar.open || bar.dataset.filled) {
            return;
          }
          bar.dataset.filled = "1";
          const status = el("div", "bar-actions");
          const note = el("span", "note", t("hist.loading"));
          note.setAttribute("role", "status");
          status.append(note);
          bar.append(status);
          loadDetails([result.id])[0].then((detail) => {
            if (bar.isConnected) {
              status.remove();
              fillPanel(bar, detail);
            }
          }, (error) => {
            if (!bar.isConnected || state.gate) {
              return;
            }
            note.textContent = errorText(error);
            const retry = el("button", "btn", t("err.retry"));
            retry.type = "button";
            retry.addEventListener("click", () => {
              status.remove();
              delete bar.dataset.filled;
              if (error.code === "history_changed") {
                loadHistory(t("err.history_changed"));
              } else {
                loadPanel();
              }
            });
            status.append(retry);
          });
        });
        list.append(bar);
      });
      if (eagerItems.length) {
        attachThumbnails(eagerItems);
      }
      renderPager(items.length);
    }

    // Keeps the page on show within the results there are; returns how many pages.
    function clampPage(count) {
      const pages = Math.max(1, Math.ceil(count / state.historyPageSize));
      state.historyPage = Math.min(Math.max(1, state.historyPage), pages);
      return pages;
    }

    // The bar under the History list: the pages, the results on show and how many a page
    // holds. It hides while everything fits on the smallest page.
    function renderPager(total) {
      const pager = $("h-pager");
      pager.hidden = total <= PAGE_SIZES[0];
      if (pager.hidden) {
        return;
      }
      const pages = clampPage(total);
      const page = state.historyPage;
      const first = (page - 1) * state.historyPageSize;
      const box = $("h-page-nums");
      const focused = box.contains(document.activeElement) ? document.activeElement.dataset.page : "";
      box.textContent = "";
      format.pageSlots(page, pages).forEach((number) => {
        if (!number) {
          const gap = el("span", "pg-gap", "…");
          gap.setAttribute("aria-hidden", "true");
          box.append(gap);
          return;
        }
        const button = el("button", "pg", fmtInt(number));
        button.type = "button";
        button.dataset.page = String(number);
        button.setAttribute("aria-label", t("hist.page.number", { page: fmtInt(number) }));
        if (number === page) {
          button.setAttribute("aria-current", "page");
        }
        button.addEventListener("click", () => {
          turnPage(number);
        });
        box.append(button);
      });
      if (focused) {
        (box.querySelector('[data-page="' + focused + '"]') || box.querySelector("[aria-current]")).focus();
      }
      $("h-page-turn").hidden = pages <= 1;
      $("h-page-prev").disabled = page <= 1;
      $("h-page-next").disabled = page >= pages;
      $("h-page-status").textContent = t("hist.page.status", { page: fmtInt(page), pages: fmtInt(pages) });
      $("h-range").textContent = t("hist.page.range", {
        from: fmtInt(first + 1),
        to: fmtInt(Math.min(total, first + state.historyPageSize)),
        total: fmtInt(total)
      });
    }

    // Another page of the History list: the pager answers at once, the rows on show slip
    // away and the new ones come in one after another.
    function swapHistoryList(motion) {
      const list = $("h-list");
      const top = document.querySelector(".top");
      if (list.getBoundingClientRect().top < Math.max(0, top ? top.getBoundingClientRect().bottom : 0)) {
        list.scrollIntoView({ block: "start", behavior: reduceMotion.matches ? "auto" : "smooth" });
      }
      const items = filteredHistory();
      const pages = clampPage(items.length);
      announce(t("hist.page.status", { page: fmtInt(state.historyPage), pages: fmtInt(pages) }));
      renderPager(items.length);
      window.clearTimeout(listSwap);
      if (reduceMotion.matches) {
        renderHistoryList(items);
        return;
      }
      list.dataset.leaving = motion;
      listSwap = window.setTimeout(() => {
        delete list.dataset.leaving;
        renderHistoryList(items, motion);
      }, 140);
    }

    function turnPage(page) {
      const pages = clampPage(filteredHistory().length);
      const next = Math.min(Math.max(1, page), pages);
      if (next !== state.historyPage) {
        const motion = next > state.historyPage ? "next" : "prev";
        state.historyPage = next;
        swapHistoryList(motion);
      }
    }

    function pickPageSize(size) {
      if (size === state.historyPageSize) {
        return;
      }
      const first = (state.historyPage - 1) * state.historyPageSize;
      state.historyPageSize = size;
      state.historyPage = Math.floor(first / size) + 1; // the first result on show stays on show
      prefs.set("historyPageSize", size);
      markSeg("h-page-size", size);
      swapHistoryList("fresh");
    }

    function renderHistory(motion) {
      const data = state.history;
      deps.setHistoryLanguage(deps.getLang());
      const all = data ? data.items : [];
      const empty = !all.length;
      ["h-down", "h-up", "h-ping", "h-list"].forEach((id) => {
        $(id).hidden = empty;
      });
      ["h-head", "h-chart", "h-table"].forEach((name) => {
        document.querySelector("." + name).hidden = empty;
      });
      $("h-empty").hidden = !empty || !data;
      if (empty) {
        disconnectObserver();
        $("h-pager").hidden = true;
        if (!data) {
          return;
        }
        $("h-empty-title").textContent = t(data.enabled === false ? "hist.disabled" : "hist.empty");
        $("h-empty-sub").textContent = data.enabled === false ? "" : t("hist.empty.sub");
        return;
      }

      // Provider filter, rebuilt from what the history really holds.
      const filter = $("h-filter");
      const names = {};
      all.forEach((result) => {
        names[result.provider] = result.provider_name || result.provider;
      });
      if (state.historyFilter && !names[state.historyFilter]) {
        state.historyFilter = "";
      }
      filter.textContent = "";
      const first = el("option", "", t("hist.all"));
      first.value = "";
      filter.append(first);
      Object.keys(names).forEach((id) => {
        const option = el("option", "", names[id]);
        option.value = id;
        filter.append(option);
      });
      filter.value = state.historyFilter;
      $("h-filter-clear").hidden = !state.historyFilter;

      const items = filteredHistory();
      $("h-count").textContent = tn("hist.count", all.length);
      $("h-limit").textContent =
        data.enabled === false ? t("hist.disabled") : t("hist.limit", { max: fmtInt(data.limit) });
      charts.drawSkyline(items);
      renderHistoryFigures(state.chartItems);
      renderProviderTable(all);
      renderHistoryList(items, motion);
    }

    function resetDownload() {
      window.clearTimeout(downloadResetTimer);
      downloadResetTimer = 0;
      const link = $("h-download");
      if (link.hasAttribute("href")) {
        const url = link.href;
        link.removeAttribute("href");
        if (URL.revokeObjectURL) {
          window.setTimeout(() => { URL.revokeObjectURL(url); }, 2000);
        }
      }
      link.hidden = true;
      $("h-export").hidden = false;
    }

    function scheduleDownloadReset() {
      window.clearTimeout(downloadResetTimer);
      downloadResetTimer = window.setTimeout(resetDownload, 2000);
    }

    function downloadCsv(items) {
      resetDownload();
      const csv = format.buildCsv(items);
      const url = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" }));
      const link = $("h-download");
      link.href = url;
      // The link also works with a fresh user gesture when a browser blocks the synthetic click.
      $("h-export").hidden = true;
      link.hidden = false;
      link.click();
    }

    function exportCsv() {
      if (!state.history || state.historyLoading || state.historyError || state.exporting) {
        return;
      }
      const filter = state.historyFilter;
      state.exporting = true;
      state.historyNotice = t("hist.exporting");
      resetDownload();
      historyFeedback();
      deps.api("GET", "/history").then((data) => {
        const items = data.items.filter((item) => !filter || item.provider === filter);
        if (!items.length) {
          return loadHistory(t("err.history_changed"));
        }
        downloadCsv(items);
        state.historyNotice = "";
      }).catch((error) => {
        state.historyNotice = errorText(error);
      }).finally(() => {
        state.exporting = false;
        historyFeedback();
      });
    }

    function clearHistory() {
      const data = state.history;
      if (!data || !data.items.length || state.historyLoading || state.historyError) {
        return;
      }
      deps.confirmDialog(t("hist.clear.title"), tn("hist.clear.body", data.items.length), t("hist.clear"), true).then((yes) => {
        if (!yes) {
          return;
        }
        deps.api("POST", "/history/clear", { revision: data.revision }).then(() => {
          state.pinned = null;
          state.lastResult = null;
          loadHistory();
        }, (error) => {
          if (error.code === "history_changed") {
            loadHistory(t("err.history_changed"));
          } else {
            historyFailed(error);
          }
        });
      });
    }

    return {
      filteredHistory,
      setFilter,
      loadDetails,
      loadHistory,
      renderHistory,
      renderHistoryList,
      renderProviderTable,
      pickChart,
      clearChartPick,
      turnPage,
      pickPageSize,
      resetDownload,
      scheduleDownloadReset,
      exportCsv,
      clearHistory,
      showsTraces,
      disconnectObserver
    };
  }

  window.NASpeedHistory = { createHistory };
})();
