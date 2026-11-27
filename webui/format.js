// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - pure formatting, data-model and SVG geometry helpers. */
(() => {
  "use strict";

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function joined(parts) {
    return parts.filter((part) => !!part).join(" · ");
  }

  function median(values) {
    const list = values.filter(isNumber).sort((a, b) => a - b);
    if (!list.length) {
      return null;
    }
    const middle = Math.floor(list.length / 2);
    return list.length % 2 ? list[middle] : (list[middle - 1] + list[middle]) / 2;
  }

  function speedOf(result, kind) {
    return result[kind] && isNumber(result[kind].mbps) ? result[kind].mbps : null;
  }

  function medianSpeed(results, kind) {
    return median(results.map((result) => speedOf(result, kind)));
  }

  function latencyOf(result) {
    return result.ping && isNumber(result.ping.latency) ? result.ping.latency : null;
  }

  function niceMax(value) {
    const steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    const base = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    for (let i = 0; i < steps.length; i += 1) {
      if (steps[i] * base >= value) {
        return steps[i] * base;
      }
    }
    return 10 * base;
  }

  function between(a, b, at) {
    const span = b[0] - a[0];
    return span > 0 ? a[1] + ((b[1] - a[1]) * (at - a[0])) / span : b[1];
  }

  /* Filled area under the points whose time lies in [from, to]. */
  function areaPath(points, from, to, domain, yMax, width, offset) {
    const sx = width / domain;
    const sy = 100 / yMax;
    const cut = [];
    let previous = null;
    for (let i = 0; i < points.length; i += 1) {
      const point = points[i];
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
    let path = "M" + (offset + cut[0][0] * sx).toFixed(1) + ",100";
    for (let i = 0; i < cut.length; i += 1) {
      path += "L" + (offset + cut[i][0] * sx).toFixed(1) + "," + (100 - Math.min(cut[i][1] * sy, 100)).toFixed(2);
    }
    return path + "L" + (offset + cut[cut.length - 1][0] * sx).toFixed(1) + ",100Z";
  }

  function peakSpeed(directions) {
    let peak = 1;
    directions.forEach((direction) => {
      if (direction) {
        direction.points.forEach((point) => {
          peak = Math.max(peak, point[1]);
        });
      }
    });
    return peak;
  }

  // At most seven page buttons, so the arrows stay put while paging: 1 2 3 4 5 … 9,
  // 1 … 4 5 6 … 9 or 1 … 5 6 7 8 9. A zero is a gap.
  function pageSlots(page, pages) {
    if (pages <= 7) {
      const slots = [];
      for (let p = 1; p <= pages; p += 1) {
        slots.push(p);
      }
      return slots;
    }
    if (page <= 4) {
      return [1, 2, 3, 4, 5, 0, pages];
    }
    if (page >= pages - 3) {
      return [1, 0, pages - 4, pages - 3, pages - 2, pages - 1, pages];
    }
    return [1, 0, page - 1, page, page + 1, 0, pages];
  }

  function directionFromLive(live) {
    if (!live) {
      return null;
    }
    const points = [[0, 0]].concat(live.series || []);
    const last = points[points.length - 1][0];
    const start = isNumber(live.window_start) ? live.window_start : null;
    let domain;
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
      points,
      start,
      domain
    };
  }

  function directionFromResult(saved) {
    if (!saved) {
      return null;
    }
    const series = Array.isArray(saved.series) ? saved.series : [];
    const total = saved.elapsed || (saved.warmup || 0) + (saved.seconds || 0) || 1;
    const points = [[0, 0]];
    for (let i = 0; i < series.length; i += 1) {
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
      points,
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
      saveError: test.save_error,
      error: test.error
    };
  }

  function modelFromResult(result, kind) {
    return {
      kind,
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

  function serverLabel(server) {
    if (!server) {
      return "";
    }
    const parts = [];
    if (server.sponsor) {
      parts.push(server.sponsor);
    }
    if (server.name && server.name !== server.sponsor) {
      parts.push(server.name);
    }
    return parts.join(", ") || server.host || "";
  }

  function csvCell(value) {
    if (value === null || value === undefined) {
      return "";
    }
    let text = String(value);
    if (/^[=+\-@\t\r]/.test(text)) {
      text = "'" + text;
    }
    if (/[",\r\n]/.test(text)) {
      text = '"' + text.replace(/"/g, '""') + '"';
    }
    return text;
  }

  function buildCsv(items) {
    const rows = [
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
        "download_measured_bytes",
        "upload_measured_bytes",
        "download_loaded_latency_ms",
        "upload_loaded_latency_ms",
        "duration_s",
        "download_nas_cpu_percent",
        "upload_nas_cpu_percent",
        "download_nas_limit",
        "upload_nas_limit"
      ]
    ];
    items.forEach((result) => {
      const server = result.server || {};
      const client = result.client || {};
      const ping = result.ping || {};
      const down = result.download || {};
      const up = result.upload || {};
      const downCpu = down.cpu || {};
      const upCpu = up.cpu || {};
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
        down.measured_bytes,
        up.measured_bytes,
        down.latency,
        up.latency,
        result.duration,
        downCpu.busy,
        upCpu.busy,
        downCpu.limit,
        upCpu.limit
      ]);
    });
    return rows.map((row) => row.map(csvCell).join(",")).join("\r\n");
  }

  window.NASpeedFormat = {
    isNumber,
    joined,
    median,
    speedOf,
    medianSpeed,
    latencyOf,
    niceMax,
    between,
    areaPath,
    peakSpeed,
    pageSlots,
    directionFromLive,
    directionFromResult,
    modelFromLive,
    modelFromResult,
    serverLabel,
    csvCell,
    buildCsv
  };
})();
