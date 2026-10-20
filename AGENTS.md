# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Cursor, etc.) when working with code in this repository.

## What this is

A personal, single-file Python exporter (`garmin_export.py`) that pulls the owner's Garmin Connect data into CSVs under `data/` and updates them incrementally. `README.md` is the user-facing manual and the data dictionary for every CSV column — read it for column meanings and analysis caveats (glitch lengths, drill-block averages, moving vs elapsed pace, HR lag).

## Commands

Python 3.14, managed with `uv` (`package = false` — scripts, not an installable package). Run from the repo root:

```bash
uv run garmin_export.py status            # reads CSVs/state only — no login, no network
uv run garmin_export.py <domain> <mode>   # domain: swim|cycle|run|daily|body|all|raw; mode: init|update|auto|status (order-independent)
uv run garmin_export.py all update -q     # the normal incremental run; -q hides routine log lines
uv sync                                   # install the versions pinned in uv.lock
uv lock --upgrade-package garminconnect   # manual dependency bump
```

There is no test suite, linter or formatter configured. `status` is the only command that is safe to run freely: every other mode logs into the owner's real Garmin account (tokens in `~/.garminconnect`, override with `GARMINTOKENS`), is subject to Garmin's rate limits, and rewrites files in `data/`. `daily init` takes ~20–30 min and `raw init` ~2.5 h — don't start those unasked. If login fails, the owner re-authenticates interactively with `garmin-mcp-auth`.

`cycle_probe.py` is a one-off, read-only exploration script (dumps raw JSON to `data/_probe/`); it is not part of the export.

## Architecture

`garmin_export.py` is one file split into banner-delimited sections: shared helpers, then one section per domain (SWIM, CYCLE, RUN, DAILY, BODY, RAW), then `RUNNERS` + `main()`. `main()` parses args, logs in once, and calls each target's `<domain>_run(g, mode)`; a domain that raises is logged and the others still run (exit 1 at the end). `all` expands to `DOMAINS`, which excludes `raw`. For `status`, runners receive `g=None` — that is why `garminconnect` is imported lazily inside functions; keep it that way.

Each domain section has the same shape: `<DOMAIN>_START` / `_OVERLAP` constants, CSV and state paths built with `path()`, `_COLS` lists that fix CSV column order, and `<domain>_list` → `<domain>_build` → `<domain>_run`. swim/cycle/run are near-copies of each other, so a change to one usually belongs in all three. Activity selection differs per domain: swim and run match on `typeKey` substrings, while cycle matches `typeId`/`parentTypeId == CYCLE_TYPE_ID` (name matching misses `enduro_mtb` and catches `motorcycling`).

### Incremental state and retries

- Each domain keeps `data/<domain>_export_state.json`. Activity domains track `processed_ids`; `update` re-lists from `last_updated − OVERLAP` days, skips processed IDs, appends new rows to the rows read back from the existing CSV, then rewrites the whole file sorted by date.
- `is_transient()` decides whether a failure is retried: rate limits, auth hiccups, 5xx and dropped connections are transient; 404s, other 4xx and unparseable responses are final. A transient failure must never be saved as "no data" — `fetch_splits()` re-raises it so the activity stays out of `processed_ids` and lands in the state's `failed` map (`id → date`), and the next `update` reaches back to the earliest failed date. Use `safe(fn, default, pause, failures)` for calls where a missing value is fine but a transient error must still be recorded.
- `daily` is keyed by date: it re-fetches `failed_days`. Slow-moving metrics (VO2max, endurance score, fitness age) are fetched once per ISO week and carried forward, so a failed weekly sample marks that whole week as failed.
- `body` always does a full refresh; when a call fails it keeps the previous run's rows/snapshot instead of overwriting them.
- `raw` works differently: the files on disk, not the manifest, are the source of truth for resuming. Incomplete captures are written as `<id>.partial.json.gz` (deliberately excluded by `raw_cached_ids()`), every write goes through the atomic `raw_write()`, and retryability is judged by string-matching recorded errors against `RAW_RETRYABLE`. `RAW_ENDPOINTS` holds the endpoint list with per-activity skip predicates; bumping `RAW_SCHEMA` forces a full re-fetch.

### Changing CSV columns

Because `update` carries old rows forward from the CSV, a new column is only filled for activities fetched from then on — existing rows stay blank until `<domain> init` rebuilds the domain. When adding, renaming or removing a column, update the matching column table in `README.md`. The command examples are duplicated in the module docstring, `USAGE` and `README.md`; keep them in sync.

`swim_build(g, a, splits=...)` accepts an already-fetched splits response, so it can be run offline against a cached `data/raw/<id>.json.gz` (`list_object` + `endpoints.splits`). The cycle/run builders don't support this yet.

## Data and privacy

`data/` (personal health data), `chat/`, `*.zip` and `*.bak` are git-ignored and must never be committed. Output goes to `data/` next to the script regardless of the working directory; `SWIM_OUTPUT_DIR` overrides it for **all** domains despite its name, and importing `garmin_export` creates that directory as a side effect.

## Dependencies

`garminconnect` is the only direct dependency; `uv.lock` pins everything. Dependabot (`.github/dependabot.yml`) opens weekly PRs — one for `garminconnect`, one grouping all transitive libraries. To vet one: `gh pr checkout <n>`, `uv sync`, `uv run garmin_export.py status`, then a real `all update`.
