# token-tracker

Tracks token usage and spend across **Claude Code** and **oh-my-pie (`omp`)**.

Both tools write per-message usage into session JSONL files. This reads those
directly, so it is always current — no daemon, no indexing step.

## Usage

```sh
./tokentrack.py                  # summary: by tool, model, project
./tokentrack.py --period week    # last 7 days
./tokentrack.py --period month   # last 30 days
./tokentrack.py --period all     # everything (the default)
./tokentrack.py --days 45        # an arbitrary window
./tokentrack.py --daily          # add a per-day table
./tokentrack.py --source omp     # one tool only
./tokentrack.py --usd            # costs in USD instead of INR
./tokentrack.py --html           # regenerate dashboard.html
./tokentrack.py --json           # aggregates, for other tooling
./tokentrack.py --raw            # every priced message
```

## Dashboard

`./tokentrack.py --html` rebuilds `dashboard.html` from `dashboard_template.html`
with fresh data baked in. Republish it to the same artifact URL to update the
hosted copy.

The page has a **Last 7 days / Last 30 days / All time** toggle. Every section —
tiles, daily bars, model and project breakdowns, the table — re-aggregates
client-side from a compact `(day, tool, model, project)` fact table embedded in
the page, so switching range needs no reload. Ranges are anchored on the last
day with data rather than on today, so the page reads the same whenever it is
opened.

## Costs

Cost is **always recomputed from raw token counts** using `pricing.json`.
`omp` stores its own cost figures but they are inconsistent — some rows are
inflated 100x (e.g. 174 output tokens billed at $0.435 when Opus 5 output is
$25/MTok) — so they are ignored. `~/.omp/stats.db` is likewise ignored: it is a
stale index that stopped updating, and carries the same bad cost values.

Rates are USD per 1M tokens, converted for display via `currency.per_usd`
(currently INR at 95.56). Edit `pricing.json` to change rates or currency;
`--rate` overrides the conversion for one run.

Anthropic rates are list pricing. Gemini rates are public Sept 2026 pricing —
Flash tiers are *introductory* through 2026-12-31 and double on 2027-01-01, and
the Gemini cache rates are approximate. A model with no rate entry is counted
as zero cost and called out in the output.

## Data sources

| Tool | Path | Dedupe key |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `(requestId, message.id)` — it writes up to 6 rows per request |
| omp | `~/.omp/agent/sessions/**/*.jsonl` | `message.responseId` |

Project names come from each session's recorded `cwd`, falling back to the
encoded directory name.
