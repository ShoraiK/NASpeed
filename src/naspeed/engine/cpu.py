# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Processor load during a measurement.

A test can only report what the NAS manages to move. When its processor is
saturated, or the platform caps the CPU time of the service, the result says
more about the NAS than about the line. A reading at the start and another at
the end of the measurement window tell which case it was.

Linux only: elsewhere ``sample()`` returns None and so does ``summarize()``.
"""

from __future__ import annotations

import os
import time

PROC_STAT = "/proc/stat"
PROC_CGROUP = "/proc/self/cgroup"
CGROUP_ROOT = "/sys/fs/cgroup"

BUSY_LIMIT = 90.0      # % of the whole processor
CORE_LIMIT = 95.0      # % of the busiest logical CPU
THROTTLE_LIMIT = 5.0   # % of scheduler periods in which the service was held back


class Sample:
    __slots__ = ("time", "cpus", "cgroup")

    def __init__(self, cpus, cgroup):
        self.time = time.monotonic()
        self.cpus = cpus
        self.cgroup = cgroup


def _read_cpus(path):
    """``{"cpu": (busy, total), "cpu0": ..., ...}`` in clock ticks."""
    cpus = {}
    with open(path, encoding="ascii") as handle:
        for line in handle:
            if not line.startswith("cpu"):
                break
            fields = line.split()
            values = [int(value) for value in fields[1:9]]
            if len(values) < 4:
                continue
            values += [0] * (8 - len(values))
            # user nice system idle iowait irq softirq steal; guest time is
            # already part of user and nice.
            idle = values[3] + values[4]
            total = sum(values)
            cpus[fields[0]] = (total - idle, total)
    return cpus


_UNBOUNDED_MEMORY = 1 << 60


def _cgroup_targets(proc_cgroup, controller):
    """Yield ``(mounts, relpath, is_v2)`` tuples from ``proc_cgroup`` for ``controller``."""
    try:
        with open(proc_cgroup, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[2] in ("", "/"):
            continue  # the root cgroup would count the whole system
        controllers = [name for name in parts[1].split(",") if name]
        if parts[0] == "0" and not controllers:
            yield ("", "unified"), parts[2], True
        elif controller in controllers:
            yield [parts[1]] + controllers, parts[2], False


def _cgroup_stat_files(proc_cgroup=PROC_CGROUP, root=CGROUP_ROOT):
    """The cpu.stat files of this process's cgroup: cgroup v2 first, then v1."""
    found = []
    for mounts, relpath, _is_v2 in _cgroup_targets(proc_cgroup, "cpu"):
        for mount in mounts:
            path = os.path.join(root, mount, relpath.lstrip("/"), "cpu.stat")
            if os.path.isfile(path):
                found.append(path)
                break
    return found


def _read_memory_limit(path):
    try:
        with open(path, encoding="ascii") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 < value < _UNBOUNDED_MEMORY else None


def memory_limit_bytes(proc_cgroup=PROC_CGROUP, root=CGROUP_ROOT):
    """Smallest finite cgroup memory limit for this service, or None."""
    limits = []
    for mounts, relpath, is_v2 in _cgroup_targets(proc_cgroup, "memory"):
        filename = "memory.max" if is_v2 else "memory.limit_in_bytes"
        parts = [part for part in relpath.split("/") if part]
        for mount in mounts:
            mount_root = os.path.join(root, mount) if mount else root
            found_mount = False
            for depth in range(len(parts), 0, -1):
                candidate = os.path.join(mount_root, *parts[:depth], filename)
                if os.path.isfile(candidate):
                    found_mount = True
                    value = _read_memory_limit(candidate)
                    if value is not None:
                        limits.append(value)
            if found_mount:
                break
    return min(limits) if limits else None


def _read_cgroup(files):
    values = {}
    for path in files:
        try:
            with open(path, encoding="ascii") as handle:
                for line in handle:
                    key, _sep, value = line.strip().partition(" ")
                    if value.isdigit():
                        values.setdefault(key, int(value))
        except OSError:
            continue
    return values or None


def sample(proc_stat=PROC_STAT, cgroup_files=None):
    """Read the counters now, or None where they cannot be read."""
    try:
        cpus = _read_cpus(proc_stat)
    except (OSError, ValueError):
        return None
    if "cpu" not in cpus:
        return None
    if cgroup_files is None:
        cgroup_files = _cgroup_stat_files()
    return Sample(cpus, _read_cgroup(cgroup_files))


def _share(start, end):
    busy = end[0] - start[0]
    total = end[1] - start[1]
    return 100.0 * busy / total if total > 0 else None


def summarize(start, end):
    """Processor use between two samples.

    ``busy`` is the share of the whole processor in use, ``peak_core`` that of
    its busiest logical CPU and ``app`` the CPUs' worth of time NASpeed itself
    used. ``throttled`` is the share of scheduler periods in which the platform
    held NASpeed back, when the cgroup reports it. ``limit`` names the first
    reason the NAS may have kept the result below the line's speed: "quota",
    "cpu" or "core", or None.
    """
    if start is None or end is None or end.time <= start.time:
        return None
    busy = _share(start.cpus["cpu"], end.cpus["cpu"])
    if busy is None:
        return None
    cores = [
        share for share in (
            _share(start.cpus[name], end.cpus[name])
            for name in end.cpus if name != "cpu" and name in start.cpus
        ) if share is not None
    ]
    summary = {
        "busy": round(busy, 1),
        "peak_core": round(max(cores), 1) if cores else None,
        "cores": len(cores) or None,
        "app": None,
        "throttled": None,
        "limit": None,
    }
    if start.cgroup and end.cgroup:
        if "usage_usec" in start.cgroup and "usage_usec" in end.cgroup:
            used = (end.cgroup["usage_usec"] - start.cgroup["usage_usec"]) / 1e6
            summary["app"] = round(max(used, 0.0) / (end.time - start.time), 2)
        periods = end.cgroup.get("nr_periods", 0) - start.cgroup.get("nr_periods", 0)
        if periods > 0:
            throttled = end.cgroup.get("nr_throttled", 0) - start.cgroup.get("nr_throttled", 0)
            summary["throttled"] = round(100.0 * max(throttled, 0) / periods, 1)
    if summary["throttled"] is not None and summary["throttled"] >= THROTTLE_LIMIT:
        summary["limit"] = "quota"
    elif busy >= BUSY_LIMIT:
        summary["limit"] = "cpu"
    elif summary["peak_core"] is not None and summary["peak_core"] >= CORE_LIMIT:
        summary["limit"] = "core"
    return summary
