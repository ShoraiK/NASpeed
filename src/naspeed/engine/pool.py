# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Transfer pool: owns the workers of one download or upload phase and
aggregates their byte counters.

The main process never touches test data itself. It only reads the counters
the workers report, which keeps it responsive (API, latency probes) while
many gigabits per second are flowing.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from .. import log
from . import transfer

logger = log.get("pool")

# Streams per worker process. TLS gets a process of its own per connection;
# plain-HTTP streams do not contend and can share one.
PLAIN_STREAMS_PER_WORKER = 4
TLS_STREAMS_PER_WORKER = 1


class _ProcessWorker:
    def __init__(self, cfg, spec, streams):
        self.streams = streams
        self.bytes = 0
        self.active = 0
        self.errors = 0
        self.last_error = ""
        self.info = {}
        self.done = False
        env = dict(os.environ)
        env.update(cfg.launcher_env)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            cfg.launcher, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, close_fds=True, **kwargs
        )
        payload = dict(spec)
        payload["streams"] = streams
        self.proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        self._reader = threading.Thread(target=self._read, name="worker-reader", daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                tag = line[:2]
                if tag == b"P ":
                    parts = line.split()
                    if len(parts) >= 4:
                        self.bytes = int(parts[1])
                        self.active = int(parts[2])
                        self.errors = int(parts[3])
                elif tag == b"I ":
                    try:
                        self.info = json.loads(line[2:].decode("utf-8", "replace"))
                    except ValueError:
                        pass
                elif tag == b"E ":
                    self.last_error = line[2:].decode("utf-8", "replace").strip()
                elif line[:1] == b"D":
                    self.done = True
                else:
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        self.last_error = text[:240]
                        logger.debug("worker output: %s", text[:240])
        except (OSError, ValueError):
            pass
        finally:
            self.active = 0
            self.done = True

    def request_stop(self):
        try:
            self.proc.stdin.write(b"stop\n")
            self.proc.stdin.flush()
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass

    def wait(self, deadline):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self.proc.wait(remaining)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self):
        try:
            self.proc.kill()
            self.proc.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self.proc.poll() is not None:
            # Drain the final counters and any late HTTP response before the
            # parent reads the outcome of this phase.
            self._reader.join()
        for stream in (self.proc.stdout, self.proc.stdin):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass


class _ThreadWorker:
    """Same interface as _ProcessWorker, running in this process."""

    def __init__(self, spec, streams):
        self.streams = streams
        payload = dict(spec)
        payload["streams"] = streams
        self._stop = threading.Event()
        self._ctr, self._threads = transfer.start_streams(payload, self._stop)

    @property
    def bytes(self):
        return self._ctr.total()

    @property
    def active(self):
        return self._ctr.active_count()

    @property
    def errors(self):
        return self._ctr.errors

    @property
    def last_error(self):
        return self._ctr.last_error

    @property
    def info(self):
        return dict(self._ctr.info)

    @property
    def done(self):
        return not any(thread.is_alive() for thread in self._threads)

    def request_stop(self):
        self._stop.set()
        self._ctr.close_sockets()

    def wait(self, deadline):
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return self.done

    def kill(self):
        self.request_stop()


class Snapshot:
    __slots__ = ("bytes", "active", "errors", "last_error", "info", "finished")

    def __init__(self, bytes_, active, errors, last_error, info, finished):
        self.bytes = bytes_
        self.active = active
        self.errors = errors
        self.last_error = last_error
        self.info = info
        self.finished = finished


class TransferPool:
    def __init__(self, cfg, spec):
        self.cfg = cfg
        self.spec = spec
        self.workers = []
        self.streams = 0
        self._tls = spec["endpoint"]["scheme"] in ("https", "wss")
        self._mode = cfg.worker_mode
        self._stopped = False

    def add_streams(self, count):
        """Start ``count`` more parallel connections."""
        if self._mode == "process":
            per_worker = TLS_STREAMS_PER_WORKER if self._tls else PLAIN_STREAMS_PER_WORKER
        else:
            per_worker = count
        remaining = int(count)
        while remaining > 0 and not self._stopped:
            batch = min(per_worker, remaining)
            self.workers.append(self._spawn(batch))
            self.streams += batch
            remaining -= batch

    def _spawn(self, streams):
        if self._mode == "process":
            try:
                return _ProcessWorker(self.cfg, self.spec, streams)
            except OSError as exc:
                logger.warning("cannot start a worker process (%s); using threads instead", exc)
                self._mode = "thread"
        return _ThreadWorker(self.spec, streams)

    def snapshot(self):
        total = 0
        active = 0
        errors = 0
        last_error = ""
        info = {}
        finished = bool(self.workers)
        for worker in self.workers:
            total += worker.bytes
            active += worker.active
            errors += worker.errors
            if worker.last_error:
                last_error = worker.last_error
            if worker.info:
                info.update(worker.info)
            if not worker.done:
                finished = False
        return Snapshot(total, active, errors, last_error, info, finished)

    def stop(self, grace=1.5):
        """Stop every worker; kill the ones that do not exit in time."""
        self._stopped = True
        for worker in self.workers:
            worker.request_stop()
        deadline = time.monotonic() + grace
        for worker in self.workers:
            if not worker.wait(deadline):
                worker.kill()
            elif isinstance(worker, _ProcessWorker):
                worker.kill()  # already exited: this just reaps it and closes pipes
