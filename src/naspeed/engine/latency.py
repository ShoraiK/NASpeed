# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Latency measurement.

Latency is measured the same way for every provider: the time of the TCP
three-way handshake (SYN -> SYN/ACK) against the selected test server. That
is one network round trip with no application processing in it, so results
are directly comparable between providers.
"""

from __future__ import annotations

import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import netutil


def tcp_ping(endpoint, timeout=2.0):
    """Return one handshake time in milliseconds, or None on failure."""
    sock = socket.socket(endpoint.family or socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        start = time.perf_counter()
        sock.connect((endpoint.ip or endpoint.host, endpoint.port))
        return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def summarize(values, attempts=None):
    """Ookla-style summary: median latency, low/high and jitter.

    Jitter is the mean absolute difference between consecutive samples.
    """
    attempts = len(values) if attempts is None else attempts
    if not values:
        return {"latency": None, "low": None, "high": None, "jitter": None,
                "count": 0, "loss": 1.0 if attempts else 0.0}
    diffs = [abs(b - a) for a, b in zip(values, values[1:])]
    return {
        "latency": round(statistics.median(values), 2),
        "low": round(min(values), 2),
        "high": round(max(values), 2),
        "jitter": round(sum(diffs) / len(diffs), 2) if diffs else 0.0,
        "count": len(values),
        "loss": round(1.0 - len(values) / attempts, 3) if attempts else 0.0,
    }


def measure(endpoint, count=12, interval=0.06, timeout=2.0, stop=None, on_sample=None):
    """Take ``count`` handshake samples. Returns ``(values, attempts)``."""
    values = []
    attempts = 0
    for _ in range(count):
        if stop is not None and stop.is_set():
            break
        attempts += 1
        value = tcp_ping(endpoint, timeout)
        if value is not None:
            values.append(value)
        if on_sample is not None:
            on_sample(values, attempts)
        if stop is not None:
            if stop.wait(interval):
                break
        else:
            time.sleep(interval)
    return values, attempts


class LoadedProbe(threading.Thread):
    """Samples latency in the background while a transfer is running, to show
    how much the connection degrades under load (bufferbloat)."""

    def __init__(self, endpoint, interval=0.25, timeout=2.0):
        super().__init__(name="loaded-probe", daemon=True)
        self.endpoint = endpoint
        self.interval = interval
        self.timeout = timeout
        self.values = []
        self.attempts = 0
        self._stop_event = threading.Event()
        self._collect = threading.Event()

    def begin_window(self):
        """Only samples taken inside the measurement window are kept."""
        self._collect.set()

    def run(self):
        while not self._stop_event.is_set():
            value = tcp_ping(self.endpoint, self.timeout)
            if self._collect.is_set() and not self._stop_event.is_set():
                self.attempts += 1
                if value is not None:
                    self.values.append(value)
            self._stop_event.wait(self.interval)

    def finish(self):
        self._stop_event.set()
        if self.ident is not None:  # the test can fail before the probe starts
            self.join(timeout=self.timeout + 1.0)
        return summarize(list(self.values), self.attempts)


def probe_host(scheme, host, port, ip_version="auto", samples=3, timeout=1.5):
    """Resolve + pin a host and return ``(endpoint, best_ms)``.

    ``best_ms`` is None when the host could not be reached.
    """
    try:
        candidates = netutil.resolve(host, port, ip_version)
    except netutil.NetError:
        return None, None
    for family, ip in candidates:
        endpoint = netutil.Endpoint(scheme, host, port, ip, family)
        best = None
        for _ in range(samples):
            value = tcp_ping(endpoint, timeout)
            if value is None:
                break
            best = value if best is None else min(best, value)
        if best is not None:
            return endpoint, best
    return None, None


def probe_many(targets, ip_version="auto", samples=3, timeout=1.5, max_workers=16):
    """Probe several hosts in parallel.

    ``targets`` maps a key to ``(scheme, host, port)``. Returns a dict mapping
    the same keys to ``(endpoint, best_ms)``.
    """
    results = {}
    if not targets:
        return results
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        futures = {
            key: pool.submit(probe_host, scheme, host, port, ip_version, samples, timeout)
            for key, (scheme, host, port) in targets.items()
        }
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception:
                results[key] = (None, None)
    return results
