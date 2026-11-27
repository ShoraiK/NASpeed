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

import os
import socket
import threading
import time
from collections import deque

from .. import __version__, log
from ..providers.base import Context, ProviderError
from ..store import CONNECTION_CHOICES, DURATIONS, IP_VERSIONS, StorageError, new_id
from . import cpu, latency, netutil
from .pool import TransferPool

logger = log.get("runner")

TICK = 0.05
# A test started from the web interface stops when nobody has asked for its state for
# this long: the window was closed, the browser crashed or the connection was lost.
UNWATCHED_TIMEOUT = 20.0
WARMUP_MIN = 1.5
WARMUP_MAX = 6.0
SETTLE_AFTER_SCALE = 1.5
SCALE_INTERVAL = 1.0
LIVE_WINDOW = 1.0
SERIES_INTERVAL = 0.1
NO_DATA_TIMEOUT = 8.0
PING_SAMPLES = 12
RESULT_SERIES_POINTS = 48
# A step that adds connections counts as "still growing" when the speed rose by
# at least this share of what the new connections would add if each carried as
# much as the old ones did.
GROWTH_SHARE = 0.5
MAIN_RSS_RESERVE_BYTES = 64 * 1024 * 1024
TLS_WORKER_RSS_BYTES = 16 * 1024 * 1024

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


class StreamScaler:
    """Decides how many connections a transfer should have once it has warmed up.

    Two rules, and the larger answer wins:

    * the speed: ``streams_for_speed`` of the recent rate;
    * the growth: a trial step that doubles the connections, repeated while
      each step keeps raising the speed by at least ``GROWTH_SHARE`` of what
      the new connections would add at the old speed per connection.

    A full line stops after the trial step. A service that limits every
    connection, as Cloudflare's upload sometimes does, keeps getting more
    connections up to ``cap``.
    """

    def __init__(self, cap):
        self.cap = cap
        self.growing = True
        self._before = None  # (streams, rate) just before the last step

    def wanted(self, rate, streams):
        """Connections to run, given the recent ``rate`` (Mbit/s) with ``streams`` open."""
        if rate <= 0:
            return streams  # nothing to judge until data flows
        if self.growing and self._before is not None:
            old_streams, old_rate = self._before
            added = streams / old_streams - 1
            if added > 0 and old_rate > 0:
                self.growing = (rate / old_rate - 1) / added >= GROWTH_SHARE
        target = streams_for_speed(rate)
        if self.growing:
            target = max(target, streams * 2)
        target = min(self.cap, target)
        if target <= streams:
            return streams
        self._before = (streams, rate)
        return target


def stream_cap(tls):
    """Upper bound on connections, scaled to the machine and cgroup RAM limit.

    A TLS connection costs one worker process (~23 MiB RSS / ~11 MiB anon),
    a plain one a thread.
    """
    cpus = os.cpu_count() or 2
    if not tls:
        return max(4, min(32, cpus * 4))
    cap = max(4, min(16, cpus * 2))
    limit = cpu.memory_limit_bytes()
    if limit is not None:
        budget = max(0, limit - MAIN_RSS_RESERVE_BYTES) // TLS_WORKER_RSS_BYTES
        cap = min(cap, max(4, budget))
    return cap


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
        self._polled = 0.0

    # ---- public API -------------------------------------------------------
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, provider_id, server_id="", options=None, watched=False):
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
            self._polled = time.monotonic()
            self._thread = threading.Thread(
                target=self._run, args=(provider, str(server_id or ""), opts), name="test", daemon=True
            )
            self._thread.start()
            if watched:
                threading.Thread(target=self._watch, args=(self._stop, self._thread),
                                 name="test-watch", daemon=True).start()
        return self.snapshot()

    def stop(self, test_id=None):
        with self._lock:
            if test_id is not None and (self._state is None or self._state["id"] != test_id):
                return False
            self._stop.set()
            return True

    def _watch(self, stop, thread):
        """Stop the test when nobody watches it any more (see UNWATCHED_TIMEOUT)."""
        while thread.is_alive() and not stop.wait(1.0):
            if time.monotonic() - self._polled > UNWATCHED_TIMEOUT:
                logger.info("nobody is watching the test: stopping it")
                stop.set()

    def shutdown(self, timeout=4.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        pool = self._pool
        if pool is not None:
            pool.stop(0.5)

    def snapshot(self):
        self._polled = time.monotonic()  # someone is watching: see _watch
        with self._lock:
            if self._state is None:
                return None
            state = dict(self._state)
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
            self._set(server=dict(session.server), client=dict(client) if client else None,
                      phase="ping", progress=self._progress("ping", 0.0), ip_family=family)

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
                "client": dict(client),
                "ping": ping,
                "download": download,
                "upload": upload,
                "duration": round(time.perf_counter() - self._t0, 1),
                "options": dict(opts),
                "ip_family": family,
            }
            saved = False
            save_error = None
            try:
                saved = self.store.add_result(result, if_enabled=True) is not None
            except StorageError:
                save_error = "storage_error"
            self._set(status="done", phase="done", progress=1.0, result=result, saved=saved,
                      save_error=save_error, client=dict(client) if client else None, elapsed=result["duration"])
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

    def _transfer_plan(self, session, opts):
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
        return window, fixed, cap, initial

    def _maybe_scale_pool(self, scaler, samples, pool, t, last_scale, settle_until):
        """Add connections during warm-up when recent throughput keeps growing."""
        if t - last_scale < SCALE_INTERVAL or t > WARMUP_MAX - SETTLE_AFTER_SCALE:
            return last_scale, settle_until
        wanted = scaler.wanted(_recent_rate(samples, t, 0.5), pool.streams)
        if wanted > pool.streams:
            pool.add_streams(wanted - pool.streams)
            return t, t + SETTLE_AFTER_SCALE
        return last_scale, settle_until

    def _update_live_transfer(self, direction, *, window, window_start, live, average,
                              snap, peak, series, grown, fraction, client):
        with self._lock:
            prev = self._state[direction]
            # Assign a fresh immutable transfer dict so snapshot() stays shallow.
            self._state[direction] = {
                "live_mbps": round(live, 2),
                "mbps": round(average, 2),
                "bytes": snap.bytes,
                "streams": snap.active,
                "measuring": window_start is not None,
                "peak_mbps": round(peak, 2),
                "series": list(series) if grown else prev["series"],
                "window": window,
                "window_start": round(window_start[0], 2) if window_start is not None else None,
            }
            self._state["progress"] = self._progress(direction, fraction)
            if snap.info.get("client_ip") and not client.get("ip"):
                client["ip"] = snap.info["client_ip"]
                self._state["client"] = dict(client)

    def _run_transfer_loop(self, direction, pool, probe, window, fixed, cap, client):
        samples = deque()
        series = []
        window_start = None
        mark = None
        end = (0.0, 0)
        snap = None
        peak = 0.0
        cpu_start = None
        scaler = StreamScaler(cap)
        t0 = time.perf_counter()
        settle_until = WARMUP_MIN
        last_scale = 0.0
        last_series = -1.0
        while True:
            if self._stop.wait(TICK):
                raise _Cancelled()
            t = time.perf_counter() - t0
            snap = pool.snapshot()
            if snap.info.get("upload_rejection"):
                raise TestError("transfer_failed", snap.info["upload_rejection"])
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

            # Add connections during the warm-up (see StreamScaler). Each
            # decision uses the most recent half second, which is past the
            # slow start of connections added a second earlier.
            if fixed is None and window_start is None and t >= 1.0:
                last_scale, settle_until = self._maybe_scale_pool(
                    scaler, samples, pool, t, last_scale, settle_until
                )

            if window_start is None and snap.bytes > 0 and (t >= settle_until or t >= WARMUP_MAX):
                window_start = end
                probe.begin_window()
                cpu_start = cpu.sample()

            if window_start is not None and t - window_start[0] >= 0.3:
                average = (snap.bytes - window_start[1]) * 8 / (t - window_start[0]) / 1e6
                fraction = 0.15 + 0.85 * (t - window_start[0]) / window
            else:
                average = live
                fraction = 0.15 * min(1.0, t / max(settle_until, 0.1))

            grown = t - last_series >= SERIES_INTERVAL
            if grown:
                last_series = t
                series.append([round(t, 2), round(live, 2)])
            self._update_live_transfer(
                direction, window=window, window_start=window_start, live=live, average=average,
                snap=snap, peak=peak, series=series, grown=grown, fraction=fraction, client=client,
            )

            if window_start is not None and t - window_start[0] >= window:
                break
            if snap.finished:
                break
            if snap.bytes == 0 and t >= NO_DATA_TIMEOUT:
                raise TestError("no_data", snap.last_error)
            if t >= WARMUP_MAX + window + 3.0:
                break
        return end, snap, mark, window_start, peak, series, cpu_start, cpu.sample()

    def _finalize_transfer(self, direction, session, pool, loaded, end, snap, mark,
                           window_start, peak, series, cpu_start, cpu_end):
        rejection = pool.snapshot().info.get("upload_rejection")
        if rejection:
            raise TestError("transfer_failed", rejection)
        if snap is None or end[1] <= 0:
            raise TestError("transfer_failed", snap.last_error if snap else "")

        measured = window_start is not None and end[0] - window_start[0] >= 0.5
        if snap.finished and (not session.self_terminating or snap.errors):
            # HTTP streams run until we stop them. A pool which quit on
            # its own cannot substantiate a full measurement window.
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
        if transferred <= 0:
            raise TestError("no_data", snap.last_error)
        mbps = transferred * 8 / seconds / 1e6

        result = {
            "mbps": round(mbps, 2),
            # bytes remains total traffic, including warm-up, for the UI and
            # existing history consumers. measured_bytes / seconds is the rate.
            "bytes": int(end[1]),
            "measured_bytes": int(transferred),
            "seconds": seconds,
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
            # Processor use during the window: tells whether the NAS, rather
            # than the line, may have set the speed.
            "cpu": cpu.summarize(cpu_start, cpu_end) if measured else None,
        }
        if snap.info.get("min_rtt_ms") is not None:
            result["server_min_rtt_ms"] = snap.info["min_rtt_ms"]
        with self._lock:
            prev = self._state[direction]
            self._state[direction] = {
                **prev,
                "live_mbps": 0.0,
                "mbps": result["mbps"],
                "bytes": result["bytes"],
                "streams": 0,
                "streams_used": result["streams"],
                "measuring": False,
                "done": True,
                "latency": result["latency"],
                "peak_mbps": result["peak_mbps"],
                "elapsed": result["elapsed"],
                "window_start": result["warmup"],
                "cpu": result["cpu"],
            }
            self._state["progress"] = self._progress(direction, 1.0)
        return result

    def _transfer(self, direction, session, opts, client):
        window, fixed, cap, initial = self._transfer_plan(session, opts)

        # "window" is the length of the measurement; "window_start" is when the
        # warm-up ended (seconds since the transfer began, None until then).
        live_state = {"live_mbps": 0.0, "mbps": 0.0, "bytes": 0, "streams": 0,
                      "measuring": False, "peak_mbps": 0.0, "series": [],
                      "window": window, "window_start": None}
        self._set(phase=direction, progress=self._progress(direction, 0.0), **{direction: live_state})

        pool = TransferPool(self.cfg, session.spec(direction))
        self._pool = pool
        probe = latency.LoadedProbe(session.endpoint)
        try:
            pool.add_streams(initial)
            probe.start()
            outcome = self._run_transfer_loop(direction, pool, probe, window, fixed, cap, client)
        finally:
            pool.stop()
            self._pool = None
            loaded = probe.finish()

        return self._finalize_transfer(direction, session, pool, loaded, *outcome)
