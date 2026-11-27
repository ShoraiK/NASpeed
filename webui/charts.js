// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - live test traces, number easing, thumbnails and skyline chart. */
(() => {
  "use strict";

  const PING_SLOTS = 12;

  function createCharts(deps) {
    const { state, $, svgEl, format, t, fmtMs, fmtSpeed, fmtSpeedUnit, fmtInt, fmtDate, reduceMotion } = deps;
    const figureStates = new WeakMap();
    const traces = { dirty: true, moving: false, yShown: 0 };
    let pingRects = null;
    let frameId = 0;
    let frameTime = 0;
    let chartBox = null;
    let chartRangeNote = "";

    function makeTween(id, formatter) {
      return { id, format: formatter, current: null, target: null, shown: null };
    }

    const tweens = {
      ping: makeTween("ping-value", fmtMs),
      down: makeTween("down-value", fmtSpeed),
      up: makeTween("up-value", fmtSpeed)
    };

    function paintTween(tween) {
      const text = tween.current === null ? "" : tween.format(tween.current);
      if (text !== tween.shown) {
        tween.shown = text;
        $(tween.id).textContent = text;
      }
    }

    function resetTweens() {
      Object.keys(tweens).forEach((key) => {
        tweens[key].shown = null;
        paintTween(tweens[key]);
      });
    }

    function setTween(kind, value, instant) {
      const tween = tweens[kind];
      if (!format.isNumber(value)) {
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
      const gap = tween.target - tween.current;
      if (Math.abs(gap) < Math.max(0.005, Math.abs(tween.target) * 0.0005)) {
        tween.current = tween.target;
      } else {
        tween.current += gap * (1 - Math.exp(-dt / 0.14));
      }
      paintTween(tween);
      return tween.current !== tween.target;
    }

    /* The lighter band is the ramp-up, which the result does not count. */
    function drawDirection(kind, direction, yMax) {
      const svg = $(kind + "-trace");
      const warm = svg.querySelector(".tr-warm");
      const main = svg.querySelector(".tr-main");
      if (!direction || direction.points.length < 2) {
        warm.setAttribute("d", "");
        main.setAttribute("d", "");
        return;
      }
      const split = direction.start !== null ? direction.start : direction.done ? 0 : Infinity;
      warm.setAttribute("d", format.areaPath(direction.points, 0, split, direction.domain, yMax, 1000, 0));
      main.setAttribute(
        "d",
        split === Infinity ? "" : format.areaPath(direction.points, split, Infinity, direction.domain, yMax, 1000, 0)
      );
    }

    function drawPing(ping) {
      const svg = $("ping-bars");
      if (!pingRects) {
        pingRects = [];
        for (let i = 0; i < PING_SLOTS; i += 1) {
          const rect = svgEl("rect");
          rect.setAttribute("x", String(i * 10 + 1.5));
          rect.setAttribute("width", "7");
          svg.append(rect);
          pingRects.push(rect);
        }
      }
      const values = ping && Array.isArray(ping.values) ? ping.values : [];
      let top = 1;
      values.forEach((value) => {
        top = Math.max(top, value * 1.15);
      });
      pingRects.forEach((rect, index) => {
        const value = values[index];
        const height = format.isNumber(value) ? Math.max(3, (value / top) * 100) : 0;
        rect.setAttribute("y", (100 - height).toFixed(2));
        rect.setAttribute("height", height.toFixed(2));
      });
    }

    /* Download and upload share one vertical scale, so the picture shows how
       the two really compare. */
    function drawTraces(dt) {
      const model = state.model;
      const peak = format.peakSpeed(model ? [model.down, model.up] : []);
      const target = format.niceMax(peak * 1.08);
      const live = !!model && model.kind === "live" && model.status === "running";
      let moving = false;
      if (!traces.yShown || !live || reduceMotion.matches) {
        traces.yShown = target;
      } else if (traces.yShown !== target) {
        const gap = target - traces.yShown;
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
      const dt = Math.min(0.1, Math.max(0.001, (now - frameTime) / 1000));
      frameTime = now;
      frameId = 0;
      let busy = false;
      Object.keys(tweens).forEach((key) => {
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

    function markTracesDirty() {
      traces.dirty = true;
      kick();
    }

    // Counts from the figure on show to the new one and replays the tile's swap effect.
    function tweenFigure(node, value, formatter) {
      const to = format.isNumber(value) ? value : null;
      const last = figureStates.get(node) || {};
      const from = last.value;
      window.cancelAnimationFrame(last.frame || 0);
      const current = { value: to, frame: 0 };
      figureStates.set(node, current);
      if (from === undefined || from === to || reduceMotion.matches) {
        node.textContent = formatter(to);
        return;
      }
      const tile = node.closest(".tile");
      tile.classList.remove("h-swap");
      void tile.offsetWidth; // restarts the CSS animation
      tile.classList.add("h-swap");
      if (to === null || from === null) {
        node.textContent = formatter(to);
        return;
      }
      const start = window.performance.now();
      const step = (now) => {
        const k = Math.min(1, (now - start) / 450);
        node.textContent = formatter(from + (to - from) * (1 - Math.pow(1 - k, 3)));
        if (k < 1) {
          current.frame = window.requestAnimationFrame(step);
        }
      };
      current.frame = window.requestAnimationFrame(step);
    }

    function miniTrace(result) {
      const svg = svgEl("svg", "mini");
      svg.setAttribute("viewBox", "0 0 200 100");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      const down = format.directionFromResult(result.download);
      const up = format.directionFromResult(result.upload);
      const peak = format.peakSpeed([down, up]);
      [
        [down, "sk-down", 0],
        [up, "sk-up", 104]
      ].forEach((item) => {
        if (item[0] && item[0].points.length > 1) {
          const path = svgEl("path", item[1]);
          path.setAttribute("d", format.areaPath(item[0].points, 0, Infinity, item[0].domain, peak * 1.05, 96, item[2]));
          svg.append(path);
        }
      });
      return svg;
    }

    function chartLabel(result) {
      return (
        fmtDate(result.ts) +
        " · ↓ " +
        fmtSpeedUnit(format.speedOf(result, "download")) +
        " · ↑ " +
        fmtSpeedUnit(format.speedOf(result, "upload"))
      );
    }

    function announceChartSelection() {
      const selected = $("h-chart-cursor");
      if (selected) {
        const focused = document.activeElement === $("h-skyline");
        const showCursor = Boolean(state.chartPicked) || focused;
        selected.setAttribute("x", (state.chartIndex * 1000 / Math.max(state.chartItems.length, 24)).toFixed(2));
        selected.setAttribute("visibility", showCursor ? "visible" : "hidden");
        selected.setAttribute("class", state.chartPicked ? "sk-hit sk-selected" : "sk-hit sk-focus");
      }
      const result = state.chartItems[state.chartIndex];
      $("h-chart-selection").textContent = result ? chartLabel(result) : "";
      $("h-chart-prev").disabled = state.chartIndex <= 0;
      $("h-chart-next").disabled = state.chartIndex >= state.chartItems.length - 1;
    }

    function setChartContext(picked) {
      const note = $("h-chart-note");
      const reset = $("h-chart-reset");
      if (picked) {
        note.textContent = format.joined([
          fmtDate(picked.ts),
          picked.provider_name || picked.provider,
          format.serverLabel(picked.server)
        ]);
        note.dataset.picked = "true";
        reset.hidden = false;
      } else {
        note.textContent = chartRangeNote;
        delete note.dataset.picked;
        reset.hidden = true;
      }
    }

    function invalidateChartBox() {
      chartBox = null;
    }

    function drawSkyline(items) {
      const svg = $("h-skyline");
      svg.textContent = "";
      chartBox = null;
      const ordered = items.slice().reverse();
      state.chartItems = ordered;
      // A result picked in the chart stays picked while it is still on it.
      state.chartIndex = ordered.length - 1;
      let kept = false;
      for (let k = 0; k < ordered.length; k += 1) {
        if (ordered[k].id === state.chartPicked) {
          state.chartIndex = k;
          kept = true;
        }
      }
      if (!kept) {
        state.chartPicked = null;
      }
      let peak = 1;
      ordered.forEach((result) => {
        peak = Math.max(peak, format.speedOf(result, "download") || 0, format.speedOf(result, "upload") || 0);
      });
      const yMax = format.niceMax(peak * 1.05);
      const slot = 1000 / Math.max(ordered.length, 24);
      deps.updateChartSteps();
      const selected = svgEl("rect", "sk-hit sk-selected");
      selected.id = "h-chart-cursor";
      selected.setAttribute("width", slot.toFixed(2));
      selected.setAttribute("height", "100");
      const hover = selected.cloneNode(false);
      hover.removeAttribute("id");
      hover.setAttribute("visibility", "hidden");
      svg.append(selected, hover);

      // Two paths draw every bar; coordinate hit-testing keeps each original result selectable.
      [["download", "sk-down", 0.08], ["upload", "sk-up", 0.52]].forEach((item) => {
        const commands = [];
        ordered.forEach((result, index) => {
          const value = format.speedOf(result, item[0]);
          if (value === null) {
            return;
          }
          const height = Math.max(1, (value / yMax) * 100).toFixed(2);
          commands.push("M" + ((index + item[2]) * slot).toFixed(2) + " 100v-" + height +
            "h" + (slot * 0.4).toFixed(2) + "v" + height + "z");
        });
        const path = svgEl("path", item[1]);
        path.setAttribute("d", commands.join(""));
        svg.append(path);
      });
      const surface = svgEl("rect");
      surface.setAttribute("fill", "transparent");
      surface.setAttribute("cursor", "pointer");
      surface.setAttribute("width", (slot * ordered.length).toFixed(2));
      surface.setAttribute("height", "100");
      const title = svgEl("title");
      surface.append(title);
      svg.append(surface);
      function indexAt(event) {
        if (!chartBox || !chartBox.width) {
          chartBox = svg.getBoundingClientRect();
        }
        if (!chartBox.width) {
          return 0;
        }
        return Math.min(
          ordered.length - 1,
          Math.max(0, Math.floor((event.clientX - chartBox.left) / chartBox.width * 1000 / slot))
        );
      }
      let hovered = -1;
      surface.addEventListener("pointerenter", () => {
        chartBox = svg.getBoundingClientRect();
      });
      surface.addEventListener("pointermove", (event) => {
        const index = indexAt(event);
        if (index !== hovered) {
          hovered = index;
          hover.setAttribute("x", (index * slot).toFixed(2));
          hover.setAttribute("visibility", "visible");
          title.textContent = chartLabel(ordered[index]);
        }
      });
      surface.addEventListener("pointerleave", () => {
        hovered = -1;
        chartBox = null;
        hover.setAttribute("visibility", "hidden");
      });
      surface.addEventListener("click", (event) => {
        deps.onChartPick(indexAt(event));
      });
      const yMaxText = deps.useGbps && deps.useGbps(yMax)
        ? fmtSpeedUnit(yMax)
        : fmtInt(yMax) + " " + t("unit.speed");
      chartRangeNote = t("hist.chart") + " 0–" + yMaxText + ".";
      announceChartSelection();
      setChartContext(state.chartPicked ? state.chartItems[state.chartIndex] : null);
    }

    return {
      resetTweens,
      setTween,
      markTracesDirty,
      kick,
      tweenFigure,
      miniTrace,
      chartLabel,
      announceChartSelection,
      setChartContext,
      drawSkyline,
      invalidateChartBox
    };
  }

  window.NASpeedCharts = { createCharts };
})();
