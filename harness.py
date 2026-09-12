"""Instrumentation for a tokentrack run: --harness.

Measures what this program actually does. Metrics with no data source here are
deliberately absent rather than reported as zero — there is no database, no
network call and no second thread in this tool, so "queries", "API calls" and
"lock contention" would be decoration, not measurement.
"""
from __future__ import annotations

import gc
import os
import resource
import threading
import time

import tokentrack as tt


def _pct(values, q):
    """Nearest-rank percentile. Values need not be sorted."""
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q / 100 * len(s) + 0.5)) - 1))
    return s[k]


def _median(values):
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _mean(values):
    return sum(values) / len(values) if values else 0


def _proc_io():
    """Linux only: bytes this process actually pulled through the block layer."""
    try:
        with open("/proc/self/io") as fh:
            return {k: int(v) for k, v in (l.split(": ") for l in fh.read().splitlines())}
    except (OSError, ValueError):
        return {}


def run(rates, days=None, sources=("claude-code", "omp"), use_cache=True, cap=None):
    """Collect once under instrumentation and return the metrics."""
    tt.CACHE = tt.ParseCache(enabled=use_cache, cap=cap or tt.CACHE_CAP_BYTES)
    tt.PROFILE = {"files": []}
    tt.STATS.update({"bad_lines": 0, "unreadable_files": 0, "lines": 0, "bytes": 0})

    gc.collect()
    gc0 = [dict(s) for s in gc.get_stats()]
    io0 = _proc_io()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    threads0 = threading.active_count()

    t0 = time.perf_counter()
    recs = tt.collect(rates, days, sources)
    t_collect = time.perf_counter() - t0

    t1 = time.perf_counter()
    tt.CACHE.evict()
    t_evict = time.perf_counter() - t1

    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    io1 = _proc_io()
    gc1 = gc.get_stats()

    wall = t_collect + t_evict
    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    files = tt.PROFILE["files"]
    durations_ms = [f["ns"] / 1e6 for f in files]
    cache = tt.CACHE
    accesses = cache.hits + cache.misses
    used, entries = cache.usage()

    unpriced = sorted({r["model"] for r in recs if not r["priced"]})
    unpriced_msgs = sum(1 for r in recs if not r["priced"])

    return {
        "cache": {
            "enabled": use_cache,
            "accesses": accesses,
            "hits": cache.hits,
            "misses": cache.misses,
            "hit_rate": cache.hits / accesses if accesses else 0.0,
            "miss_rate": cache.misses / accesses if accesses else 0.0,
            "evictions": cache.evictions,
            "hit_latency_us": _mean(cache.hit_ns) / 1e3,
            "miss_latency_us": _mean(cache.miss_ns) / 1e3,
            "p95_hit_latency_us": _pct(cache.hit_ns, 95) / 1e3,
            "bytes_used": used,
            "bytes_cap": cache.cap,
            "utilization": used / cache.cap if cache.cap else 0.0,
            "entries": entries,
            "bytes_read": cache.bytes_in,
            "bytes_written": cache.bytes_out,
        },
        "execution": {
            "wall_s": wall,
            "collect_s": t_collect,
            "evict_s": t_evict,
            "cpu_s": cpu,
            "cpu_utilization": cpu / wall if wall else 0.0,
            "files": len(files),
            "mean_file_ms": _mean(durations_ms),
            "median_file_ms": _median(durations_ms),
            "p95_file_ms": _pct(durations_ms, 95),
            "p99_file_ms": _pct(durations_ms, 99),
            "slowest_file_ms": max(durations_ms) if durations_ms else 0,
            "messages": len(recs),
            "msgs_per_s": len(recs) / wall if wall else 0,
            "files_per_s": len(files) / wall if wall else 0,
            "mib_per_s": (tt.STATS["bytes"] / 2**20) / wall if wall else 0,
            # ru_maxrss is KiB on Linux, bytes on macOS.
            "peak_rss_mib": ru1.ru_maxrss / (1024 if os.uname().sysname == "Linux" else 1024**2),
        },
        "system": {
            "lines_parsed": tt.STATS["lines"],
            "bytes_parsed": tt.STATS["bytes"],
            "read_bytes": io1.get("read_bytes", 0) - io0.get("read_bytes", 0),
            "read_syscall_bytes": io1.get("rchar", 0) - io0.get("rchar", 0),
            "write_bytes": io1.get("write_bytes", 0) - io0.get("write_bytes", 0),
            "io_available": bool(io0),
            "voluntary_ctx_switches": ru1.ru_nvcsw - ru0.ru_nvcsw,
            "involuntary_ctx_switches": ru1.ru_nivcsw - ru0.ru_nivcsw,
            "threads": threading.active_count(),
            "threads_created": threading.active_count() - threads0,
            "gc_collections": [s["collections"] - g["collections"] for s, g in zip(gc1, gc0)],
            "gc_collected": [s["collected"] - g["collected"] for s, g in zip(gc1, gc0)],
            "gc_uncollectable": [s["uncollectable"] - g["uncollectable"] for s, g in zip(gc1, gc0)],
            "tracked_objects": len(gc.get_objects()),
        },
        "integrity": {
            "bad_lines": tt.STATS["bad_lines"],
            "unreadable_files": tt.STATS["unreadable_files"],
            "failure_rate": tt.STATS["bad_lines"] / tt.STATS["lines"] if tt.STATS["lines"] else 0.0,
            "unpriced_models": unpriced,
            "unpriced_messages": unpriced_msgs,
            "files_reparsed": sum(1 for f in files if not f["cached"]),
            "files_from_cache": sum(1 for f in files if f["cached"]),
        },
    }, recs


# ---------------------------------------------------------------- rendering

B = "\033[1m"
D = "\033[0m"
DIM = "\033[2m"


def _bytes(n):
    for unit, div in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if abs(n) >= div:
            return f"{n/div:.1f} {unit}"
    return f"{int(n)} B"


def render(h):
    c, e, s, i = h["cache"], h["execution"], h["system"], h["integrity"]

    print(f"\n{B}Harness{D}  {e['messages']:,} messages from {e['files']:,} files in {e['wall_s']:.2f}s")

    print(f"\n{B}Cache performance{D} {DIM}(parse cache, ~/.cache/token-tracker){D}")
    if not c["enabled"]:
        print("  disabled for this run (--no-cache)")
    else:
        print(f"  hit rate         {c['hit_rate']*100:>8.1f}%   {c['hits']:,} of {c['accesses']:,} accesses")
        print(f"  miss rate        {c['miss_rate']*100:>8.1f}%   {c['misses']:,} reparsed from source")
        print(f"  latency          {c['hit_latency_us']:>8.0f}us   mean hit, p95 {c['p95_hit_latency_us']:.0f}us, miss {c['miss_latency_us']:.0f}us")
        print(f"  evictions        {c['evictions']:>9,}   LRU, over a {_bytes(c['bytes_cap'])} cap")
        print(f"  utilization      {c['utilization']*100:>8.1f}%   {_bytes(c['bytes_used'])} across {c['entries']:,} entries")

    print(f"\n{B}Execution performance{D}")
    print(f"  total            {e['wall_s']:>8.2f}s   collect {e['collect_s']:.2f}s, evict {e['evict_s']:.3f}s")
    print(f"  per file          mean {e['mean_file_ms']:.2f}ms   median {e['median_file_ms']:.2f}ms, p95 {e['p95_file_ms']:.2f}ms, p99 {e['p99_file_ms']:.2f}ms")
    print(f"  slowest file     {e['slowest_file_ms']:>8.1f}ms")
    print(f"  throughput       {e['msgs_per_s']:>9,.0f}   messages/s  ({e['files_per_s']:,.0f} files/s, {e['mib_per_s']:.1f} MiB/s)")
    print(f"  cpu              {e['cpu_utilization']*100:>8.0f}%   {e['cpu_s']:.2f}s of {e['wall_s']:.2f}s wall")
    print(f"  peak memory      {e['peak_rss_mib']:>8.0f} MiB")

    print(f"\n{B}Resource and system behavior{D}")
    print(f"  parsed           {s['lines_parsed']:>9,}   JSONL lines, {_bytes(s['bytes_parsed'])}")
    if s["io_available"]:
        print(f"  disk read        {_bytes(s['read_bytes']):>9}   from device, {_bytes(s['read_syscall_bytes'])} through syscalls")
        print(f"  disk write       {_bytes(s['write_bytes']):>9}   cache writes")
    print(f"  ctx switches     {s['voluntary_ctx_switches']:>9,}   voluntary, {s['involuntary_ctx_switches']:,} involuntary")
    print(f"  threads          {s['threads']:>9}   {s['threads_created']:+d} created (single-threaded by design)")
    gcs = s["gc_collections"]
    print(f"  gc runs          {sum(gcs):>9,}   gen0 {gcs[0]:,} / gen1 {gcs[1]:,} / gen2 {gcs[2]:,}")
    print(f"  gc collected     {sum(s['gc_collected']):>9,}   objects, {s['tracked_objects']:,} still tracked")

    print(f"\n{B}Failures this run{D}")
    print(f"  malformed lines  {i['bad_lines']:>9,}   {i['failure_rate']*100:.3f}% of lines parsed")
    print(f"  unreadable files {i['unreadable_files']:>9,}")
    print(f"  unpriced         {i['unpriced_messages']:>9,}   messages counted as zero cost")
    if i["unpriced_models"]:
        print(f"    {DIM}no rate for: {', '.join(i['unpriced_models'])}{D}")
    print(f"  {DIM}files: {i['files_from_cache']:,} from cache, {i['files_reparsed']:,} reparsed{D}")
    print()
