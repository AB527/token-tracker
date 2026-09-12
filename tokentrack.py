#!/usr/bin/env python3
"""Track token usage across Claude Code and oh-my-pie (omp).

Both tools write per-message usage into session JSONL files. This reads them
directly, dedupes, prices them from pricing.json, and reports.

Cost is always recomputed from raw token counts. omp stores its own cost
figures but they are inconsistent (some rows are inflated 100x), so they are
ignored.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath so a symlinked entrypoint still finds pricing.json
CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
OMP_DIR = os.path.expanduser("~/.omp/agent/sessions")

# ---------------------------------------------------------------- pricing


def load_pricing(path=None):
    with open(path or os.path.join(HERE, "pricing.json")) as fh:
        cfg = json.load(fh)
    cur = cfg.get("currency") or {"code": "USD", "symbol": "$", "per_usd": 1.0}
    return cfg["models"], cur


def normalize_model(model: str) -> str:
    """Strip date suffixes so claude-haiku-4-5-20251001 prices as claude-haiku-4-5."""
    if not model:
        return "unknown"
    parts = model.split("-")
    if parts[-1].isdigit() and len(parts[-1]) == 8:  # claude-haiku-4-5-20251001
        return "-".join(parts[:-1])
    if model.startswith("gemini-") and parts[-1] in ("low", "medium", "high", "xhigh"):
        return "-".join(parts[:-1])  # effort variants bill at the base model rate
    return model


def price(rec: dict, rates: dict) -> float:
    r = rates.get(rec["model"])
    if not r:
        return 0.0
    return (
        rec["in"] * r["in"]
        + rec["out"] * r["out"]
        + rec["cw5m"] * r["cw5m"]
        + rec["cw1h"] * r["cw1h"]
        + rec["cr"] * r["cr"]
    ) / 1_000_000


# ---------------------------------------------------------------- parsing


def _iso(ts) -> str | None:
    """Normalize a timestamp (ISO string or epoch ms) to ISO-8601 UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
    return str(ts)


def _read_json_lines(fp: str):
    try:
        with open(fp, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _iter_json_lines(pattern: str):
    for fp in glob.iglob(pattern, recursive=True):
        for d in _read_json_lines(fp):
            yield fp, d


def _session_cwd(fp: str):
    """omp records the session cwd in its own entry; find it before pricing rows."""
    for d in _read_json_lines(fp):
        cwd = d.get("cwd")
        if cwd:
            return cwd
    return None


def collect_claude_code():
    """Claude Code writes several rows per API request; dedupe on (requestId, message id)."""
    seen, out = set(), []
    for fp, d in _iter_json_lines(os.path.join(CLAUDE_DIR, "**", "*.jsonl")):
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        key = (d.get("requestId"), msg.get("id"))
        if key in seen:
            continue
        seen.add(key)

        model = normalize_model(msg.get("model") or "unknown")
        if model in ("<synthetic>", "unknown"):
            continue
        cc = usage.get("cache_creation") or {}
        cw5m = cc.get("ephemeral_5m_input_tokens")
        cw1h = cc.get("ephemeral_1h_input_tokens")
        if cw5m is None and cw1h is None:
            # Older rows only have the flat total; assume the 5m rate.
            cw5m, cw1h = usage.get("cache_creation_input_tokens", 0) or 0, 0

        cwd = d.get("cwd") or ""
        out.append(
            {
                "ts": _iso(d.get("timestamp")),
                "source": "claude-code",
                "provider": "anthropic",
                "project": os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(fp)),
                "model": model,
                "in": usage.get("input_tokens", 0) or 0,
                "out": usage.get("output_tokens", 0) or 0,
                "cr": usage.get("cache_read_input_tokens", 0) or 0,
                "cw5m": cw5m or 0,
                "cw1h": cw1h or 0,
            }
        )
    return out


def collect_omp():
    """omp session JSONL: assistant messages carry message.usage inline."""
    seen, out = set(), []
    for fp in glob.iglob(os.path.join(OMP_DIR, "**", "*.jsonl"), recursive=True):
        cwd = _session_cwd(fp)
        if cwd:
            project = os.path.basename(cwd.rstrip("/")) or cwd
        else:
            # Fall back to the dir name: "-Code-learning-backend" -> "learning-backend"
            rel = os.path.relpath(fp, OMP_DIR).split(os.sep)[0]
            project = rel.lstrip("-").split("-", 1)[-1] or "unknown"

        for d in _read_json_lines(fp):
            if d.get("type") != "message":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage")
            if not usage or msg.get("role") != "assistant":
                continue
            key = msg.get("responseId") or d.get("id")
            if key in seen:
                continue
            seen.add(key)

            cttl = usage.get("cttl") or {}
            cw5m = cttl.get("ephemeral5m")
            cw1h = cttl.get("ephemeral1h")
            if cw5m is None and cw1h is None:
                cw5m, cw1h = usage.get("cacheWrite", 0) or 0, 0

            out.append(
                {
                    "ts": _iso(d.get("timestamp") or msg.get("timestamp")),
                    "source": "omp",
                    "provider": msg.get("provider") or "unknown",
                    "project": project,
                    "model": normalize_model(msg.get("model") or "unknown"),
                    "in": usage.get("input", 0) or 0,
                    "out": usage.get("output", 0) or 0,
                    "cr": usage.get("cacheRead", 0) or 0,
                    "cw5m": cw5m or 0,
                    "cw1h": cw1h or 0,
                }
            )
    return out


def collect(rates, since_days=None, sources=("claude-code", "omp")):
    recs = []
    if "claude-code" in sources:
        recs += collect_claude_code()
    if "omp" in sources:
        recs += collect_omp()

    cutoff = None
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    out = []
    for r in recs:
        if not r["ts"]:
            continue
        if cutoff and r["ts"] < cutoff:
            continue
        r["total"] = r["in"] + r["out"] + r["cr"] + r["cw5m"] + r["cw1h"]
        r["cost"] = price(r, rates)
        r["day"] = r["ts"][:10]
        r["priced"] = r["model"] in rates
        out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------- reporting

def _agg(recs, keyfn):
    buckets = defaultdict(lambda: {"n": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "total": 0, "cost": 0.0})
    for r in recs:
        b = buckets[keyfn(r)]
        b["n"] += 1
        for k in ("in", "out", "total"):
            b[k] += r[k]
        b["cr"] += r["cr"]
        b["cw"] += r["cw5m"] + r["cw1h"]
        b["cost"] += r["cost"]
    return buckets


CUR = {"code": "USD", "symbol": "$", "per_usd": 1.0}


def money(usd: float) -> str:
    """Format a USD amount in the configured display currency."""
    v = usd * CUR["per_usd"]
    return f"{CUR['symbol']}{v:,.2f}"


def human(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))


def table(title, buckets, limit=None, sort_by="cost"):
    rows = sorted(buckets.items(), key=lambda kv: -kv[1][sort_by])
    if limit:
        rows = rows[:limit]
    w = max([len(str(k)) for k, _ in rows] + [len(title)]) if rows else len(title)
    print(f"\n\033[1m{title}\033[0m")
    print(f"  {'':<{w}}  {'msgs':>6} {'input':>8} {'output':>8} {'cache r':>9} {'cache w':>8} {'cost ('+CUR['code']+')':>13}")
    for k, b in rows:
        print(
            f"  {str(k):<{w}}  {b['n']:>6} {human(b['in']):>8} {human(b['out']):>8} "
            f"{human(b['cr']):>9} {human(b['cw']):>8} {money(b['cost']):>13}"
        )


def report(recs, args):
    if not recs:
        print("No usage found. Checked:\n  " + CLAUDE_DIR + "\n  " + OMP_DIR)
        return

    tot = _agg(recs, lambda r: "all")["all"]
    days = len({r["day"] for r in recs})
    span = f"{recs[0]['day']} -> {recs[-1]['day']}"

    print(f"\n\033[1mToken usage\033[0m  {span}  ({days} active days, {len(recs)} messages)")
    print(f"  billable in   {human(tot['in']):>13}")
    print(f"  output        {human(tot['out']):>13}")
    print(f"  cache read    {human(tot['cr']):>13}")
    print(f"  cache write   {human(tot['cw']):>13}")
    print(f"  \033[1mtotal        {human(tot['total']):>13}\033[0m")
    print(f"  \033[1mcost        {money(tot['cost']):>13}\033[0m   (~{money(tot['cost']/max(days,1))}/active day)")

    unpriced = {r["model"] for r in recs if not r["priced"]}
    if unpriced:
        print(f"  \033[33mno rate for: {', '.join(sorted(unpriced))} - counted as $0\033[0m")

    table("By tool", _agg(recs, lambda r: r["source"]))
    table("By model", _agg(recs, lambda r: r["model"]))
    table(f"By project (top {args.top})", _agg(recs, lambda r: r["project"]), limit=args.top)
    if args.daily:
        table("By day", _agg(recs, lambda r: r["day"]), limit=args.daily, sort_by="cost")
    print()


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description="Track token usage across Claude Code and omp.")
    p.add_argument("--days", type=int, help="only include the last N days")
    p.add_argument("--period", choices=["week", "month", "all"],
                   help="shorthand for --days: week=7, month=30, all=everything")
    p.add_argument("--source", choices=["claude-code", "omp"], action="append",
                   help="limit to one tool (repeatable)")
    p.add_argument("--top", type=int, default=10, help="projects to show (default 10)")
    p.add_argument("--daily", type=int, nargs="?", const=14, help="also show a per-day table")
    p.add_argument("--json", action="store_true", help="emit aggregated JSON for the dashboard")
    p.add_argument("--raw", action="store_true", help="emit every priced message as JSON")
    p.add_argument("--html", nargs="?", const="dashboard.html", metavar="PATH",
                   help="write the dashboard HTML (default ./dashboard.html)")
    p.add_argument("--pricing", help="path to an alternate pricing.json")
    p.add_argument("--usd", action="store_true", help="show costs in USD instead of the configured currency")
    p.add_argument("--rate", type=float, help="override the per-USD conversion rate")
    args = p.parse_args()

    if args.period:
        args.days = {"week": 7, "month": 30, "all": None}[args.period]

    rates, cur = load_pricing(args.pricing)
    if args.usd:
        cur = {"code": "USD", "symbol": "$", "per_usd": 1.0}
    if args.rate:
        cur = dict(cur, per_usd=args.rate)
    CUR.update(cur)
    recs = collect(rates, args.days, tuple(args.source or ["claude-code", "omp"]))

    if args.raw:
        json.dump(recs, sys.stdout, indent=1)
        return
    if args.json or args.html:
        def dump(buckets):
            return [dict(key=k, **v) for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"])]
        payload = {
                "generated": datetime.now(timezone.utc).isoformat(),
                "currency": CUR,
                "messages": len(recs),
                "span": [recs[0]["day"], recs[-1]["day"]] if recs else None,
                "totals": _agg(recs, lambda r: "all")["all"] if recs else {},
                "by_day": dump(_agg(recs, lambda r: r["day"])),
                "by_model": dump(_agg(recs, lambda r: r["model"])),
                "by_source": dump(_agg(recs, lambda r: r["source"])),
                "by_project": dump(_agg(recs, lambda r: r["project"])),
                "by_day_source": [
                    dict(key=list(k), **v) for k, v in _agg(recs, lambda r: (r["day"], r["source"])).items()
                ],
                # Fact table: one row per (day, tool, model, project). The dashboard
                # re-aggregates from this so its week/month/all toggle needs no reload.
                "cells": [
                    [k[0], k[1], k[2], k[3], v["n"], v["in"], v["out"], v["cr"], v["cw"],
                     round(v["cost"], 8)]
                    for k, v in _agg(recs, lambda r: (r["day"], r["source"], r["model"], r["project"])).items()
                ],
        }
        if args.html:
            tpl = os.path.join(HERE, "dashboard_template.html")
            with open(tpl) as fh:
                html = fh.read().replace("/*__DATA__*/", json.dumps(payload))
            out_path = args.html if os.path.isabs(args.html) else os.path.join(os.getcwd(), args.html)
            with open(out_path, "w") as fh:
                fh.write(html)
            print(f"wrote {out_path}  ({len(recs)} messages, {payload['span'][0]} -> {payload['span'][1]})")
            return
        json.dump(payload, sys.stdout, indent=1)
        return

    report(recs, args)


if __name__ == "__main__":
    main()
