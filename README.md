# token-tracker

Know what your coding agents actually cost you.

`token-tracker` reads the session logs that **Claude Code** and **oh-my-pie
(`omp`)** already write, and turns them into a spend report — by tool, by model,
by project, by day. One CLI, one static dashboard, no daemon and no indexing
step: the logs *are* the database, so every run is current.

It was built because `omp`'s own cost numbers are wrong (see
[Costs](#costs)), and because neither tool will tell you what the *other* one
spent.

## Install

Needs Python 3.9+ and nothing else — no dependencies.

```sh
git clone https://github.com/TanKaizokuO/token-tracker
cd token-tracker
./tokentrack.py
```

Put it on your `PATH` if you want it everywhere:

```sh
ln -s "$PWD/tokentrack.py" ~/.local/bin/tokens
```

## Usage

```sh
tokens                      # summary: by tool, model, project
tokens --period week        # last 7 days
tokens --period month       # last 30 days
tokens --period all         # everything (the default)
tokens --days 45            # an arbitrary window
tokens --daily              # add a per-day table
tokens --source omp         # one tool only (repeatable)
tokens --top 20             # show more projects
tokens --usd                # costs in USD instead of the configured currency
tokens --rate 83.2          # override the conversion rate for one run
tokens --html               # write dashboard.html
tokens --json               # aggregates, for other tooling
tokens --raw                # every priced message
```

Output (numbers illustrative):

```
Token usage  2026-08-13 -> 2026-09-12  (31 active days, 25117 messages)
  billable in           91.8M
  output                16.5M
  cache read             2.2B
  cache write           43.8M
  total                  2.3B
  cost              $1,128.06   (~$36.39/active day)

By tool
                 msgs    input   output   cache r  cache w    cost (USD)
  omp           23199    91.8M    15.4M      2.0B    38.6M     $1,022.04
  claude-code    1918     8.3K     1.1M    163.0M     5.2M       $106.02
```

## Dashboard

`tokens --html` renders `dashboard.html` from `dashboard_template.html` with
fresh data baked in. It is a single self-contained file — open it directly, or
host it anywhere.

The page has a **Last 7 days / Last 30 days / All time** toggle. Every section —
stat tiles, stacked daily bars, model and project breakdowns, the detail table —
re-aggregates client-side from a compact `(day, tool, model, project)` fact table
embedded in the page, so switching range is instant and needs no regeneration.
Ranges anchor on the last day with data rather than on today, so a saved copy
reads the same whenever it is opened.

`dashboard.html` is gitignored: it contains your real spend and project names.

## How it works

Both tools append one JSON object per line to a session file, and assistant
messages carry a `usage` block. token-tracker globs those files, keeps the
rows that carry usage, dedupes, prices each one, and aggregates.

| Tool | Path | Dedupe key |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `(requestId, message.id)` |
| omp | `~/.omp/agent/sessions/**/*.jsonl` | `message.responseId` |

Deduping is not optional. Claude Code writes up to **six rows per API request**
as a response streams, each carrying the same cumulative `usage`. Counting them
naively roughly doubles the apparent bill.

Four token classes are tracked separately, because they bill at different
rates: input, output, cache **write** (5-minute and 1-hour TTL, at 1.25x and 2x
the input rate), and cache **read** (0.1x input). Cache reads typically dominate
token counts while contributing little to cost, which is why a headline "total
tokens" number is close to meaningless on its own — the dashboard says so
explicitly rather than letting the big number mislead.

Model ids are normalized before pricing: date suffixes are stripped
(`claude-haiku-4-5-20251001` → `claude-haiku-4-5`) and Gemini effort variants
fold into their base model (`gemini-3.8-flash-high` → `gemini-3.8-flash`), since
effort changes how many tokens you spend, not the rate.

Project names come from each session's recorded `cwd`, falling back to the
encoded directory name.

## Costs

Cost is **always recomputed from raw token counts** using `pricing.json`.
Nothing in either tool's logs is trusted as a cost figure.

That is deliberate. `omp` records its own costs, and they are inconsistent —
some rows are inflated 100x (174 output tokens billed at $0.435, when Opus 5
output is $25/MTok, so it should be $0.00435) while adjacent rows in the same
table are correct. `~/.omp/stats.db` carries the same bad values *and* is a
stale index that silently stopped updating. Both are ignored.

Rates in `pricing.json` are USD per 1M tokens:

```json
"claude-opus-5":  {"in": 5.00, "out": 25.00, "cw5m": 6.25, "cw1h": 10.00, "cr": 0.50}
```

Display currency is converted from USD via the `currency` block:

```json
"currency": {"code": "INR", "symbol": "₹", "per_usd": 95.56}
```

Set `per_usd` to `1` and `code` to `USD` for dollars, or use `--usd` / `--rate`
per run.

**Accuracy caveats.** Anthropic rates are list pricing. Gemini rates are public
Sept 2026 pricing — the Flash tiers are *introductory* through 2026-12-31 and
double on 2027-01-01, and the Gemini cache rates are approximate. Any model with
no entry in `pricing.json` is counted as zero cost and named in the output, so
under-counting is visible rather than silent. This is an estimate from your own
logs, not a copy of your invoice; check it against your provider's billing page
before treating it as authoritative.

## License

MIT
