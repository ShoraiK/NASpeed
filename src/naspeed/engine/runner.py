# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Test orchestration.

One test runs at a time, in a background thread. The phases are:

    init      choose the server and learn who the client is
    ping      idle latency and jitter
    download  parallel connections; warm-up is discarded
    upload    same, in the other direction

The live state is kept in a plain dict that the API hands to the web UI.

How a speed is computed: the transfer runs until the connections have ramped
up (``WARMUP_MIN`` seconds, longer if more connections had to be added), then
bytes are counted over a fixed measurement window. The result is simply
``bytes in the window * 8 / window length``.
"""

from __future__ import annotations

import copy
import os
import socket
import threading
import time
from collections import deque

from .. import __version__, log
from ..providers.base import Context, ProviderError
from ..store import CONNECTION_CHOICES, DURATIONS, IP_VERSIONS, new_id
from . import latency, netutil
from .pool import TransferPool

logger = log.get("runner")

TICK = 0.05
WARMUP_MIN = 1.5
WARMUP_MAX = 6.0
SETTLE_AFTER_SCALE = 1.5
SCALE_INTERVAL = 1.0
LIVE_WINDOW = 1.0
SERIES_INTERVAL = 0.1
NO_DATA_TIMEOUT = 8.0
PING_SAMPLES = 12
RESULT_SERIES_POINTS = 48

# Share of the overall progress bar taken by each phase.
PHASE_SPAN = (("init", 0.05), ("ping", 0.07), ("download", 0.44), ("upload", 0.44))


class TestError(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = str(detail)


class _Cancelled(Exception):
    pass


def streams_for_speed(mbps):
    """How many parallel connections a line of this speed deserves."""
    if mbps < 150:
        return 4
    if mbps < 600:
        return 8
    if mbps < 2500:
        return 16
    if mbps < 6000:
        return 24
    return 32


def stream_cap(tls):
    """Upper bound on connections, scaled to the machine.

    A TLS connection costs one worker process, a plain one a thread.
    """
    cpus = os.cpu_count() or 2
    if tls:
        return max(4, min(16, cpus * 2))
    return max(4, min(32, cpus * 4))


def _recent_rate(samples, now, lookback):
    """Speed in Mbit/s over the last ``lookback`` seconds of ``(t, bytes)`` samples."""
    if len(samples) < 2:
        return 0.0
    start = samples[0]
    for sample in samples:
        if sample[0] >= now - lookback:
            start = sample
            break
    last = samples[-1]
    if last[0] - start[0] < 0.1:
        start = samples[0]
    elapsed = last[0] - start[0]
    return (last[1] - start[1]) * 8 / elapsed / 1e6 if elapsed > 0 else 0.0


def _resample(series, points):
    """Reduce ``[[t, value], ...]`` to at most ``points`` evenly spaced values."""
    values = [v for _t, v in series]
    if len(values) <= points:
        return [round(v, 1) for v in values]
    step = len(values) / float(points)
    out = []
    for index in range(points):
        chunk = values[int(index * step): max(int((index + 1) * step), int(index * step) + 1)]
        out.append(round(sum(chunk) / len(chunk), 1))
    return out


class TestRunner:
    def __init__(self, cfg, store, registry):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self._lock = threading.Lock()
        self._state = None
        self._thread = None
        self._stop = threading.Event()
        self._pool = None

    # ---- public API -------------------------------------------------------
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, provider_id, server_id="", options=None):
        with self._lock:
            if self.running():
                raise TestError("busy")
            provider = self.registry.get(str(provider_id))
            if provider is None:
                raise TestError("unknown_provider", provider_id)
            opts = self._options(options)
            self._stop = threading.Event()
            self._state = {
                "id": new_id(),
                "status": "running",
                "phase": "init",
                "progress": 0.0,
                "provider": provider.describe(),
                "server": None,
                "client": None,
                "options": opts,
                "started_at": int(time.time()),
                "elapsed": 0.0,
                "ping": None,
                "download": None,
                "upload": None,
                "error": None,
                "result": None,
                "saved": False,
            }
            self._t0 = time.perf_counter()
            self._thread = threading.Thread(
                target=self._run, args=(provider, str(server_id or ""), opts), name="test", daemon=True
            )
            self._thread.start()
        return self.snapshot()

    def stop(self):
        self._stop.set()

    def shutdown(self, timeout=4.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        pool = self._pool
        if pool is not None:
            pool.stop(0.5)

    def snapshot(self):
        with self._lock:
            if self._state is None:
                return None
            state = copy.deepcopy(self._state)
        if state["status"] == "running":
            state["elapsed"] = round(time.perf_counter() - self._t0, 1)
        return state

    # ---- internals --------------------------------------------------------
    def _options(self, overrides):
        settings = self.store.get_settings()
        opts = {
            "duration": settings.get("duration", "standard"),
            "connections": str(settings.get("connections", "auto")),
            "ip_version": str(settings.get("ip_version", "auto")),
        }
        overrides = overrides or {}
        if overrides.get("duration") in DURATIONS:
            opts["duration"] = overrides["duration"]
        if str(overrides.get("connections", "")) in CONNECTION_CHOICES:
            opts["connections"] = str(overrides["connections"])
        if str(overrides.get("ip_version", "")) in IP_VERSIONS:
            opts["ip_version"] = str(overrides["ip_version"])
        return opts

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def _progress(self, phase, fraction):
        base = 0.0
        for name, span in PHASE_SPAN:
            if name == phase:
                return round(base + span * max(0.0, min(1.0, fraction)), 4)
            base += span
        return 1.0

    def _check(self):
        if self._stop.is_set():
            raise _Cancelled()

    def _run(self, provider, server_id, opts):
        started = time.time()
        try:
            logger.info("test started: provider=%s server=%s options=%s",
                        provider.id, server_id or "auto", opts)
            ctx = Context(self.cfg, opts["ip_version"], self._stop)
            session = provider.open(ctx, server_id)
            self._check()
            family = 6 if session.endpoint.family == socket.AF_INET6 else 4
            client = dict(session.client) if session.client else {}
            self._set(server=dict(session.server), client=client or None, phase="ping",
                      progress=self._progress("ping", 0.0), ip_family=family)

            ping = self._ping(session)
            download = self._transfer("download", session, opts, client)
            upload = self._transfer("upload", session, opts, client)

            result = {
                "id": self._state["id"],
                "ts": int(started),
                "app_version": __version__,
                "provider": provider.id,
                "provider_name": provider.name,
                "server": dict(session.server),
                "client": client,
                "ping": ping,
                "download": download,
                "upload": upload,
                "duration": round(time.perf_counter() - self._t0, 1),
                "options": dict(opts),
                "ip_family": family,
            }
            saved = False
            if self.store.get_settings().get("save_history", True):
                self.store.add_result(result)
                saved = True
            self._set(status="done", phase="done", progress=1.0, result=result, saved=saved,
                      client=client or None, elapsed=result["duration"])
            logger.info("test finished: provider=%s down=%.1f Mbps up=%.1f Mbps ping=%s ms",
                        provider.id, download["mbps"] if download else 0.0,
                        upload["mbps"] if upload else 0.0, ping.get("latency"))
        except _Cancelled:
            logger.info("test cancelled")
            self._set(status="cancelled", phase="done", elapsed=round(time.perf_counter() - self._t0, 1))
        except (TestError, ProviderError, netutil.NetError) as exc:
            logger.warning("test failed: %s", exc)
            self._set(status="error", phase="done", elapsed=round(time.perf_counter() - self._t0, 1),
                      error={"code": exc.code, "detail": exc.detail[:300]})
        except Exception as exc:  # never let the thread die silently
            logger.exception("test crashed")
            self._set(status="error", phase="done", elapsed=round(time.perf_counter() - self._t0, 1),
                      error={"code": "internal_error", "detail": str(exc)[:300]})
        finally:
            pool = self._pool
            if pool is not None:
                pool.stop(0.5)
                self._pool = None

    def _ping(self, session):
        summary = {}

        def on_sample(values, attempts):
            data = latency.summarize(values, attempts)
            data["values"] = [round(v, 2) for v in values]
            summary.clear()
            summary.update(data)
            self._set(ping=dict(data), progress=self._progress("ping", attempts / float(PING_SAMPLES)))

        values, _attempts = latency.measure(session.endpoint, PING_SAMPLES, 0.05, 2.0, self._stop, on_sample)
        self._check()
        if not values:
            raise TestError("server_unreachable", session.endpoint.host)
        result = {key: summary.get(key) for key in ("latency", "low", "high", "jitter", "count", "loss")}
        # Kept so the web UI can redraw the samples of a saved result.
        result["values"] = [round(v, 2) for v in values]
        return result

    def _transfer(self, direction, session, opts, client):
        spec = session.spec(direction)
        window = DURATIONS[opts["duration"]]
        if session.max_window:
            window = min(window, session.max_window)
        if session.fixed_streams:
            fixed = int(session.fixed_streams)
        elif opts["connections"] != "auto":
            fixed = int(opts["connections"])
        else:
            fixed = None
        cap = min(session.max_streams, stream_cap(session.endpoint.tls))
        initial = min(fixed or session.initial_streams, cap)

        # "window" is the length of the measurement; "window_start" is when the
        # warm-up ended (seconds since the transfer began, None until then).
        live_state = {"live_mbps": 0.0, "mbps": 0.0, "bytes": 0, "streams": 0,
                      "measuring": False, "peak_mbps": 0.0, "series": [],
                      "window": window, "window_start": None}
        self._set(phase=direction, progress=self._progress(direction, 0.0), **{direction: live_state})

        pool = TransferPool(self.cfg, spec)
        self._pool = pool
        probe = latency.LoadedProbe(session.endpoint)
        samples = deque()
        series = []
        window_start = None
        mark = None
        end = (0.0, 0)
        snap = None
        peak = 0.0
        try:
            pool.add_streams(initial)
            probe.start()
            t0 = time.perf_counter()
            settle_until = WARMUP_MIN
            last_scale = 0.0
            last_series = -1.0
            while True:
                if self._stop.wait(TICK):
                    raise _Cancelled()
                t = time.perf_counter() - t0
                snap = pool.snapshot()
                end = (t, snap.bytes)
                samples.append(end)
                while len(samples) > 2 and samples[1][0] <= t - LIVE_WINDOW:
                    samples.popleft()
                t_old, b_old = samples[0]
                live = (snap.bytes - b_old) * 8 / (t - t_old) / 1e6 if t - t_old >= 0.2 else 0.0
                if t >= 1.0 and mark is None:
                    mark = end
                if window_start is not None:
                    peak = max(peak, live)

                # Add connections while the line is still ramping up. The decision
                # uses the most recent half second, which is past the slow start.
                if (fixed is None and window_start is None and t >= 1.0
                        and t - last_scale >= SCALE_INTERVAL
                        and t <= WARMUP_MAX - SETTLE_AFTER_SCALE):
                    wanted = min(cap, streams_for_speed(_recent_rate(samples, t, 0.5)))
                    if wanted > pool.streams:
                        pool.add_streams(wanted - pool.streams)
                        last_scale = t
                        settle_until = t + SETTLE_AFTER_SCALE

                if window_start is None and snap.bytes > 0 and (t >= settle_until or t >= WARMUP_MAX):
                    window_start = end
                    probe.begin_window()

                if window_start is not None and t - window_start[0] >= 0.3:
                    average = (snap.bytes - window_start[1]) * 8 / (t - window_start[0]) / 1e6
                    fraction = 0.15 + 0.85 * (t - window_start[0]) / window
                else:
                    average = live
                    fraction = 0.15 * min(1.0, t / max(settle_until, 0.1))

                if t - last_series >= SERIES_INTERVAL:
                    last_series = t
                    series.append([round(t, 2), round(live, 2)])
                with self._lock:
                    state = self._state[direction]
                    state["live_mbps"] = round(live, 2)
                    state["mbps"] = round(average, 2)
                    state["bytes"] = snap.bytes
                    state["streams"] = snap.active
                    state["measuring"] = window_start is not None
                    if window_start is not None:
                        state["window_start"] = round(window_start[0], 2)
                    state["peak_mbps"] = round(peak, 2)
                    state["series"] = list(series)
                    self._state["progress"] = self._progress(direction, fraction)
                    if snap.info.get("client_ip") and not client.get("ip"):
                        client["ip"] = snap.info["client_ip"]
                        self._state["client"] = dict(client)

                if window_start is not None and t - window_start[0] >= window:
                    break
                if snap.finished:
                    break
                if snap.bytes == 0 and t >= NO_DATA_TIMEOUT:
                    raise TestError("no_data", snap.last_error)
                if t >= WARMUP_MAX + window + 3.0:
                    break
        finally:
            pool.stop()
            self._pool = None
            loaded = probe.finish()

        if snap is None or end[1] <= 0:
            raise TestError("transfer_failed", snap.last_error if snap else "")

        measured = window_start is not None and end[0] - window_start[0] >= 0.5
        complete = measured and end[0] - window_start[0] >= window * 0.5
        if snap.finished and not complete and not session.self_terminating:
            # Every connection died before we had a usable measurement.
            raise TestError("transfer_failed", snap.last_error)
        if measured:
            seconds = end[0] - window_start[0]
            transferred = end[1] - window_start[1]
        elif mark is not None and end[0] - mark[0] >= 0.3:
            seconds = end[0] - mark[0]
            transferred = end[1] - mark[1]
        else:
            seconds = max(end[0], 0.001)
            transferred = end[1]
        mbps = transferred * 8 / seconds / 1e6

        result = {
            "mbps": round(mbps, 2),
            "bytes": int(end[1]),
            "seconds": round(seconds, 2),
            "peak_mbps": round(max(peak, mbps), 2),
            "streams": pool.streams,
            "latency": loaded.get("latency"),
            "jitter": loaded.get("jitter"),
            "errors": snap.errors,
            # The series covers the whole transfer ("elapsed" seconds); its first
            # "warmup" seconds are the ramp-up that the result does not count.
            "series": _resample(series, RESULT_SERIES_POINTS),
            "elapsed": round(end[0], 2),
            "warmup": round(window_start[0], 2) if measured else None,
        }
        if snap.info.get("min_rtt_ms") is not None:
            result["server_min_rtt_ms"] = snap.info["min_rtt_ms"]
        with self._lock:
            state = self._state[direction]
            state.update({"live_mbps": 0.0, "mbps": result["mbps"], "bytes": result["bytes"],
                          "streams": 0, "streams_used": result["streams"],
                          "measuring": False, "done": True,
                          "latency": result["latency"], "peak_mbps": result["peak_mbps"],
                          "elapsed": result["elapsed"], "window_start": result["warmup"]})
            self._state["progress"] = self._progress(direction, 1.0)
        return result
