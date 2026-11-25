"""
Garmin exporter — one entry point for all training data.

Exports read-only Garmin Connect data to CSV in ./data (next to this script) and remembers
what it has pulled, so each domain can be updated incrementally. Data has three shapes, so
it stays in separate CSVs; this script just unifies the three entry points behind one login.

Domains and their outputs:
  swim   -> data/swim_summary.csv, data/swim_lengths.csv          (one row per swim / per length)
  cycle  -> data/cycle_summary.csv, data/cycle_laps.csv           (one row per ride / per lap)
  run    -> data/run_summary.csv, data/run_laps.csv               (one row per run / per lap)
  daily  -> data/daily_metrics.csv                                (one row per calendar day)
  body   -> data/body_weight.csv, data/personal_records.csv,      (weigh-ins over time;
            data/race_predictions.csv                              PRs & races are snapshots)
  raw    -> data/raw/<activity_id>.json.gz                        (every endpoint, every
            data/raw_account.json.gz                               activity — opt-in, slow)

The CSV domains are views: each keeps a handful of fields, so widening one means going back
to Garmin. The `raw` domain instead caches every endpoint for every activity in the account
(all types, not just swim/cycle/run), letting any later view be built offline. It is excluded
from `all` because a full pull is ~2.5 h; it is resumable and safe to re-run.

USAGE (from this directory; uv installs the versions pinned in uv.lock):
  uv run garmin_export.py                  # all domains, auto (init first run, else update)
  uv run garmin_export.py all update       # update everything, single login
  uv run garmin_export.py swim update      # one domain only
  uv run garmin_export.py cycle update     # rides only
  uv run garmin_export.py run update       # runs only
  uv run garmin_export.py daily init       # full daily rebuild (slow, ~20-30 min)
  uv run garmin_export.py body             # refresh weigh-ins + PRs + race predictions
  uv run garmin_export.py raw init         # cache every endpoint for every activity (~2.5 h)
  uv run garmin_export.py status           # report all domains, fetch nothing

Args are order-independent: a domain (swim|cycle|run|daily|body|all, or raw) and a mode
(init|update|status). Defaults: domain=all, mode=auto.
Auth: saved tokens; if login fails run `garmin-mcp-auth`.
"""
import csv
import gzip
import json
import operator
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import partial, reduce

# Loaded on first use (PEP 810), so `status` — which runs with g=None — never imports them.
lazy import requests
lazy from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

# ---- shared config --------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.environ.get("SWIM_OUTPUT_DIR", os.path.join(SCRIPT_DIR, "data"))
os.makedirs(OUTPUT_DIR, exist_ok=True)
TOKEN_STORE = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))

DOMAINS = ("swim", "cycle", "run", "daily", "body")   # what `all` expands to
RAW_DOMAIN = "raw"                                    # opt-in only — never part of `all`
MODES   = ("init", "update", "status", "auto")
# Activity syncs (swim, cycle, run, raw) list from here on `init` and then record your first
# activity as their start. Empty years are cheap: Garmin pages by result, and raw lists
# year by year, so an empty year costs it one request.
EARLIEST = "2000-01-01"

def path(name):
    return os.path.join(OUTPUT_DIR, name)


# ---- logging & progress ---------------------------------------------------
_T0 = time.time()
QUIET = False                       # set from --quiet
IS_TTY = sys.stderr.isatty()


def fmt_dur(seconds):
    """Human-readable duration: 1h02m, 3m05s, 12s."""
    s = round(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def log(msg, icon="·"):
    """Timestamped line to stderr: [+elapsed] icon message."""
    if QUIET and icon == "·":
        return
    print(f"[+{fmt_dur(time.time() - _T0):>5}] {icon} {msg}", file=sys.stderr, flush=True)


class Progress:
    """Live progress for long loops. Updating bar on a TTY; milestone lines when piped."""
    WIDTH = 22

    def __init__(self, label, total):
        self.label = label
        self.total = max(0, total or 0)
        self.n = 0
        self.start = time.time()
        self.last = 0.0

    def step(self, note=""):
        self.n += 1
        now = time.time()
        done = self.n >= self.total
        if IS_TTY and not QUIET:
            if now - self.last >= 0.15 or done:
                self._render(now, note, inplace=True)
                self.last = now
        elif not QUIET:
            every = max(1, self.total // 20) if self.total else 50   # ~5% milestones
            if self.n % every == 0 or done:
                self._render(now, note, inplace=False)

    def _render(self, now, note, inplace):
        el = now - self.start
        rate = self.n / el if el > 0 else 0
        pct = (self.n / self.total * 100) if self.total else 0
        eta = (self.total - self.n) / rate if (rate > 0 and self.total) else 0
        filled = int(self.WIDTH * pct / 100)
        bar = "#" * filled + "-" * (self.WIDTH - filled)
        line = (f"  {self.label} [{bar}] {self.n}/{self.total} {pct:4.0f}% "
                f"· {rate:4.1f}/s · ETA {fmt_dur(eta):>5}" + (f" · {note}" if note else ""))
        if inplace:
            print("\r" + line + "\033[K", end="", file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)

    def done(self, summary):
        if IS_TTY and not QUIET:
            print(file=sys.stderr, flush=True)      # finish the in-place line
        log(f"{self.label}: {summary} in {fmt_dur(time.time() - self.start)}", icon="✓")


# ---- shared helpers -------------------------------------------------------
def login():
    g = Garmin()
    try:
        g.login(TOKEN_STORE)
    except Exception as e:
        sys.exit(f"Login failed ({e}).\nRun this once in a terminal:  garmin-mcp-auth")
    return g


def load_state(p, default):
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return dict(default)


@contextmanager
def atomic_write(p, opener=open, mode="w", **kw):
    """Write through a temp file that replaces `p` only once complete, so an interrupted run
    (Ctrl+C, crash) leaves the previous file whole — never a truncated one that the next
    `update` would read back as the full history."""
    tmp = p + ".tmp"
    try:
        with opener(tmp, mode, **kw) as f:
            yield f
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_state(p, obj):
    with atomic_write(p) as f:
        json.dump(obj, f, indent=2)


def read_csv_list(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def read_csv_by_key(p, key):
    if not os.path.exists(p):
        return {}
    with open(p, newline="") as f:
        return {r[key]: r for r in csv.DictReader(f)}


def write_csv(p, cols, rows):
    with atomic_write(p, newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def fmt_time(seconds):
    if not isinstance(seconds, (int, float)):
        return ""
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def mins(seconds):
    return round(seconds / 60) if isinstance(seconds, (int, float)) else ""


def kmh(speed_ms):
    return round(speed_ms * 3.6, 1) if isinstance(speed_ms, (int, float)) else ""


def pace_per_km(speed_ms):
    """m/s → min:sec per km (running pace)."""
    return fmt_time(1000 / speed_ms) if isinstance(speed_ms, (int, float)) and speed_ms > 0 else ""


def _num(d, key, nd=1):
    v = d.get(key)
    return round(v, nd) if isinstance(v, (int, float)) else ""


def _typekey(a):
    return ((a.get("activityType") or {}).get("typeKey") or "").lower()


def is_transient(e):
    """Whether a failed Garmin call is worth asking again on a later run: rate limits,
    auth hiccups, 5xx and dropped connections. A 4xx (404 = nothing there) or a response
    that won't parse comes back the same every time, so that answer is final."""
    if isinstance(e, (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError)):
        return True
    if isinstance(e, GarminConnectNotFoundError) or not isinstance(e, GarminConnectConnectionError):
        return False
    if isinstance(e.__cause__, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(getattr(e, "response", None), "status_code", None)
    if not isinstance(status, int):
        m = re.search(r"(?:API Error|HTTP|error \()\s*(\d{3})", str(e))
        status = int(m.group(1)) if m else None
    return status is not None and status >= 500


def safe(fn, default=None, pause=0.0, failures=None):
    """Call fn, returning `default` if it raises. Pass a list as `failures` to collect the
    transient errors, so the caller can tell "Garmin had nothing" from "ask again later"."""
    try:
        v = fn()
        if pause:
            time.sleep(pause)
        return v
    except Exception as e:
        if failures is not None and is_transient(e):
            failures.append(e)
        if pause:
            time.sleep(pause)
        return default


def retry_note(n):
    """Status-line suffix for however many items the last run couldn't fetch."""
    return f", {n} awaiting retry" if n else ""


def overlap_start(state, days):
    """Where `update` starts: `days` before the last pull, but never before the domain's
    start date."""
    last = state.get("last_updated") or state["start_date"]
    return max((datetime.fromisoformat(last) - timedelta(days=days)).date().isoformat(),
               state["start_date"])


def year_windows(start, end):
    """The ISO dates [start, end] cut at year boundaries, as (lo, hi) ISO pairs."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(max(d0, date(y, 1, 1)).isoformat(), min(d1, date(y, 12, 31)).isoformat())
            for y in range(d0.year, d1.year + 1)]


def fetch_splits(g, aid):
    """Splits for one activity. A permanent failure means there are none; a transient one
    is raised so the caller leaves the activity unprocessed for the next run instead of
    saving it with no laps forever."""
    try:
        return g.get_activity_splits(aid) or {}
    except Exception as e:
        if is_transient(e):
            raise
        log(f"no splits for {aid}: {e}", icon="!")
        return {}


# ===========================================================================
# ACTIVITIES  — the incremental loop swim, cycle and run share
#
# Each of those domains is a spec dict (its names, files, and list/build functions) that
# activity_run() drives. Builders are pure — build(a, splits) -> (child rows, summary row) —
# so they work the same on a fresh splits response or on one from the raw cache, and any
# view can be rebuilt offline.
# ===========================================================================
def list_activities(g, start, end, garmin_type, keep):
    """Activities in [start, end] that `keep` accepts, oldest first. Asks Garmin for
    `garmin_type` only, falling back to every activity if that filter fails."""
    try:
        acts = g.get_activities_by_date(start, end, garmin_type)
    except Exception:
        acts = g.get_activities_by_date(start, end)
    return sorted(filter(keep, acts), key=lambda a: a.get("startTimeLocal", ""))


def _head(a):
    """The four columns every activity row starts with."""
    return {"activity_id": a["activityId"], "date": (a.get("startTimeLocal") or "")[:10],
            "activity_name": a.get("activityName") or "", "type": a["activityType"]["typeKey"]}


def lap_base(head, i, L):
    """The lap columns cycle and run share; each adds its sport's own."""
    return {**head, "lap_index": i,
            "distance_m": _num(L, "distance"),
            "duration_s": _num(L, "duration"),
            "moving_duration_s": _num(L, "movingDuration"),
            "avg_speed_kmh": kmh(L.get("averageSpeed")),
            "avg_hr": L.get("averageHR", ""), "max_hr": L.get("maxHR", ""),
            "avg_power_w": _num(L, "averagePower", 0), "max_power_w": _num(L, "maxPower", 0),
            "normalized_power_w": _num(L, "normalizedPower", 0),
            "elevation_gain_m": _num(L, "elevationGain"),
            "calories": L.get("calories", ""),
            "intensity_type": L.get("intensityType") or ""}


def summary_base(head, a, num_laps):
    """The summary columns cycle and run share; each adds its sport's own."""
    return {**head,
            "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else "",
            "duration_s": _num(a, "duration"),
            "moving_duration_s": _num(a, "movingDuration"),
            "elevation_gain_m": _num(a, "elevationGain"),
            "elevation_loss_m": _num(a, "elevationLoss"),
            "avg_speed_kmh": kmh(a.get("averageSpeed")),
            "avg_hr": _num(a, "averageHR", 0), "max_hr": a.get("maxHR", ""),
            "avg_power_w": _num(a, "avgPower", 0), "max_power_w": _num(a, "maxPower", 0),
            "normalized_power_w": _num(a, "normPower", 0),
            "calories": _num(a, "calories", 0),
            "aerobic_te": _num(a, "aerobicTrainingEffect"),
            "anaerobic_te": _num(a, "anaerobicTrainingEffect"),
            "num_laps": num_laps}


def resolve_mode(mode, state):
    """`auto` is `init` on a domain's first run and `update` after it."""
    if mode == "auto":
        return "update" if state.get("last_updated") else "init"
    return mode


def activity_run(g, mode, d):
    """One activity domain: a summary row per activity plus its child rows ("parts": pool
    lengths or laps). Each activity is fetched once and remembered in the state file."""
    state = load_state(d["state"], {"start_date": EARLIEST, "last_updated": None,
                                    "processed_ids": [], f"num_{d['noun']}s": 0,
                                    f"num_{d['part']}_rows": 0})
    if mode == "status":
        activity_status(d, state)
        return
    today = date.today().isoformat()
    from_date, summaries, parts, processed = activity_plan(d, state, resolve_mode(mode, state),
                                                           today)
    acts = d["list"](g, from_date, today)
    todo = [a for a in acts if str(a["activityId"]) not in processed]
    log(f"{d['name']}: {len(acts)} in range, {len(todo)} new to fetch")
    new_summaries, new_parts, fetched, failed = activity_fetch(g, d, todo, state["start_date"])
    summaries, parts = summaries + new_summaries, parts + new_parts
    activity_save(d, state, summaries, parts, processed | fetched, failed, today)
    log(f"{d['name']}: +{len(new_summaries)} new → {len(summaries)} {d['noun']}s / {len(parts)} "
        f"{d['part']}s written to {os.path.basename(d['summary_csv'])}, "
        f"{os.path.basename(d['parts_csv'])}", icon="✓")


def activity_status(d, state):
    n, rows = state[f"num_{d['noun']}s"], state.get(f"num_{d['part']}_rows", 0)
    first = f"first {state['start_date']}, " if n else ""
    print(f"  {d['name']:<6}: {n} {d['noun']}s, {rows} {d['part']} rows"
          f"  ({first}last {state['last_updated'] or 'never'})"
          f"{retry_note(len(state.get('failed') or {}))}")


def activity_plan(d, state, mode, today):
    """Where to list from, the rows to carry forward, and the IDs not to fetch again."""
    if mode == "init":
        log(f"{d['name']} INIT: every {d['noun']} on Garmin → {today}", icon="▶")
        return EARLIEST, [], [], set()      # the first activity is learned from the data
    from_date = min([overlap_start(state, d["overlap"])]
                    + list(state.get("failed", {}).values()))   # reach back to retry
    processed = {str(x) for x in state.get("processed_ids", [])}
    # The state file is written last, so it is what commits a run: rows of an activity it
    # never recorded come from an interrupted run, and that activity is fetched again.
    summaries, parts = ([r for r in read_csv_list(d[k]) if r["activity_id"] in processed]
                        for k in ("summary_csv", "parts_csv"))
    log(f"{d['name']} UPDATE: {from_date} → {today}  ({len(processed)} already pulled)", icon="▶")
    return from_date, summaries, parts, processed


def activity_fetch(g, d, todo, fallback_day):
    """Fetch and build each activity. One that fails lands in `failed` (id → day) for the
    next run to retry, instead of being saved with no laps."""
    summaries, parts, fetched, failed, err = [], [], set(), {}, None
    prog = Progress(d["name"], len(todo))
    for a in todo:
        aid, day = str(a["activityId"]), (a.get("startTimeLocal") or "")[:10]
        try:
            rows, summary = d["build"](a, fetch_splits(g, a["activityId"]))
        except Exception as e:
            failed[aid] = day or fallback_day
            err = f"{type(e).__name__}: {e}"    # the text, not `e`: its traceback pins this frame
            prog.step(f"{day} failed")
        else:
            parts.extend(rows)
            summaries.append(summary)
            fetched.add(aid)
            prog.step(f"{day} ({len(rows)} {d['part']}s)")
        time.sleep(0.3)
    if todo:
        prog.done(f"{len(summaries)} {d['noun']}s, {len(parts)} {d['part']}s")
    if failed:
        log(f"{d['name']}: {len(failed)} {d['noun']}s failed to fetch and will be retried next "
            f"run (last error: {err})", icon="!")
    return summaries, parts, fetched, failed


def activity_save(d, state, summaries, parts, processed, failed, today):
    """Rewrite both CSVs sorted by date, then the state file — last, because it commits."""
    summaries.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", ""))))
    parts.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", "")),
                              int(r.get(d["part_order"]) or 0)))
    write_csv(d["summary_csv"], d["summary_cols"], summaries)
    write_csv(d["parts_csv"], d["parts_cols"], parts)
    state.update({"last_updated": today,
                  # rows are sorted by date, so the first is your first activity
                  "start_date": summaries[0]["date"] if summaries else state["start_date"],
                  "processed_ids": sorted(processed, key=lambda x: int(x)),
                  f"num_{d['noun']}s": len(summaries), f"num_{d['part']}_rows": len(parts),
                  "failed": failed,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(d["state"], state)


# ===========================================================================
# SWIM  — one row per swim + one row per pool length
# ===========================================================================
SWIM_OVERLAP = 7
SWIM_SUMMARY_CSV = path("swim_summary.csv")
SWIM_LENGTHS_CSV = path("swim_lengths.csv")
SWIM_STATE       = path("swim_export_state.json")
SWIM_GLITCH_PACE = 60

SWIM_SUMMARY_COLS = [
    "activity_id", "date", "activity_name", "type", "pool_length_m",
    "total_distance_m", "active_lengths", "total_duration_s", "moving_duration_s",
    "rest_time_s", "avg_pace_moving_per_100m_s", "avg_pace_elapsed_per_100m_s",
    "avg_hr", "max_hr", "calories", "avg_swolf_clean", "num_sets", "stroke_mix",
]
SWIM_LENGTH_COLS = [
    "activity_id", "date", "activity_name", "type", "pool_length_m", "set_index",
    "length_in_set", "global_length_n", "distance_m", "duration_s",
    "pace_per_100m_s", "stroke", "swolf", "strokes", "avg_hr", "max_hr",
    "rest_before_set_s", "rest_avg_hr", "rest_max_hr", "likely_glitch",
]


def _rest_hr(samples):
    """Collapse the resting lengths before a set into one (avg, max) pair. A rest is
    usually one zero-distance length, but can be several — so the average is weighted
    by how long each lasted rather than treating a 3 s pause like a 60 s one."""
    vals = [(a, m, d) for a, m, d in samples if a is not None]
    if not vals:
        return (None, None)
    total = sum(d for _, _, d in vals)
    avg = (sum(a * d for a, _, d in vals) / total) if total else (
        sum(a for a, _, _ in vals) / len(vals))
    maxes = [m for _, m, _ in vals if m is not None]
    return (round(avg), max(maxes) if maxes else None)


def swim_list(g, start, end):
    return list_activities(g, start, end, "swimming", lambda a: "swim" in _typekey(a))


def _swimming(length):
    return (length.get("distance") or 0) > 0


def _seconds(length):
    return length.get("duration") or 0.0


def _seconds_in(lengths):
    """Left-to-right total, as the exporter has always added durations — sum() compensates
    rounding since Python 3.12, which can tip a .x5 total the other way."""
    return reduce(operator.add, map(_seconds, lengths), 0.0)


def _swim_sets(lengths):
    """Group a swim's lengths into sets — (resting lengths before it, its active lengths).
    Rest after the last set belongs to no set."""
    sets, rest, active = [], [], []
    for L in lengths:
        if _swimming(L):
            active.append(L)
            continue
        if active:
            sets.append((rest, active))
            rest, active = [], []
        rest.append(L)
    if active:
        sets.append((rest, active))
    return sets


def _swim_length_rows(head, sets):
    """One row per active length; the first length of each set carries the rest before it."""
    pool_len = sets[0][1][0]["distance"] if sets else None
    rows = []
    for set_idx, (rest, active) in enumerate(sets, 1):
        for i, L in enumerate(active, 1):
            rows.append({**head, "pool_length_m": pool_len, "set_index": set_idx,
                         "length_in_set": i, "global_length_n": len(rows) + 1,
                         **_swim_length(L), **(_rest_before(rest) if i == 1 else _NO_REST)})
    return rows


_NO_REST = {"rest_before_set_s": 0, "rest_avg_hr": None, "rest_max_hr": None}


def _rest_before(rest):
    """What the first length of a set records about the rest that preceded it."""
    seconds = _seconds_in(rest)
    avg_hr, max_hr = _rest_hr([(L.get("averageHR"), L.get("maxHR"), _seconds(L)) for L in rest])
    return {"rest_before_set_s": round(seconds, 1) if seconds else 0,
            "rest_avg_hr": avg_hr, "rest_max_hr": max_hr}


def _swim_length(L):
    """The columns that come from the length itself."""
    dist, dur = L["distance"], _seconds(L)
    pace100 = round(dur / dist * 100, 1) if dur else None
    return {"distance_m": dist, "duration_s": round(dur, 1), "pace_per_100m_s": pace100,
            "stroke": L.get("swimStroke") or "", "swolf": L.get("averageSWOLF"),
            "strokes": L.get("totalNumberOfStrokes"),
            "avg_hr": L.get("averageHR"), "max_hr": L.get("maxHR"),
            "likely_glitch": int(pace100 is not None and pace100 < SWIM_GLITCH_PACE)}


def _round1(x):
    return round(x, 1) if x else ""


def _per_100m(seconds, metres):
    return round(seconds / metres * 100, 1) if (seconds and metres) else ""


def _swim_summary(a, head, rows, rest_s, num_sets):
    swolfs = [r["swolf"] for r in rows if r["swolf"] is not None and not r["likely_glitch"]]
    strokes = Counter(r["stroke"] for r in rows if r["stroke"])
    dist, dur, moving = a.get("distance"), a.get("duration"), a.get("movingDuration")
    return {**head, "pool_length_m": rows[0]["pool_length_m"] if rows else "",
            "total_distance_m": _num(a, "distance"), "active_lengths": len(rows),
            "total_duration_s": _round1(dur), "moving_duration_s": _round1(moving),
            "rest_time_s": round(rest_s, 1),
            "avg_pace_moving_per_100m_s": _per_100m(moving, dist),
            "avg_pace_elapsed_per_100m_s": _per_100m(dur, dist),
            "avg_hr": a.get("averageHR"), "max_hr": a.get("maxHR"),
            "calories": a.get("calories"),
            "avg_swolf_clean": round(sum(swolfs) / len(swolfs), 1) if swolfs else "",
            "num_sets": num_sets,
            "stroke_mix": "; ".join(f"{k}:{v}" for k, v in strokes.most_common())}


def swim_build(a, splits):
    """One row per pool length, grouped into sets by the rests between them, plus the
    swim's summary row."""
    head = _head(a)
    lengths = [*(lap.get("lengthDTOs") or []) for lap in splits.get("lapDTOs") or []]
    sets = _swim_sets(lengths)
    rows = _swim_length_rows(head, sets)
    rest_s = _seconds_in(L for L in lengths if not _swimming(L))
    return rows, _swim_summary(a, head, rows, rest_s, len(sets))


SWIM = frozendict(name="swim", noun="swim", part="length", part_order="global_length_n",
                  overlap=SWIM_OVERLAP, state=SWIM_STATE,
                  summary_csv=SWIM_SUMMARY_CSV, summary_cols=SWIM_SUMMARY_COLS,
                  parts_csv=SWIM_LENGTHS_CSV, parts_cols=SWIM_LENGTH_COLS,
                  list=swim_list, build=swim_build)


def swim_run(g, mode):
    activity_run(g, mode, SWIM)


# ===========================================================================
# CYCLE  — one row per ride
# ===========================================================================
CYCLE_OVERLAP = 7
CYCLE_SUMMARY_CSV = path("cycle_summary.csv")
CYCLE_LAPS_CSV    = path("cycle_laps.csv")
CYCLE_STATE       = path("cycle_export_state.json")
CYCLE_TYPE_ID     = 2     # Garmin's "cycling"; every ride type (road, indoor, virtual, enduro MTB, …) is its child

CYCLE_SUMMARY_COLS = [
    "activity_id", "date", "activity_name", "type",
    "distance_km", "duration_s", "moving_duration_s",
    "elevation_gain_m", "elevation_loss_m",
    "avg_speed_kmh", "max_speed_kmh", "avg_hr", "max_hr",
    "avg_cadence_rpm", "max_cadence_rpm",
    "avg_power_w", "max_power_w", "normalized_power_w",
    "calories", "avg_temp_c", "aerobic_te", "anaerobic_te", "num_laps",
]
CYCLE_LAP_COLS = [
    "activity_id", "date", "activity_name", "type", "lap_index",
    "distance_m", "duration_s", "moving_duration_s",
    "avg_speed_kmh", "max_speed_kmh", "avg_hr", "max_hr",
    "avg_power_w", "max_power_w", "normalized_power_w",
    "avg_cadence_rpm", "max_cadence_rpm",
    "elevation_gain_m", "calories", "intensity_type",
]


def _is_ride(a):
    # by parent type, not name: "enduro_mtb" has no "bik" in it, "motorcycling" does
    t = a.get("activityType") or {}
    return CYCLE_TYPE_ID in (t.get("typeId"), t.get("parentTypeId"))


def cycle_list(g, start, end):
    return list_activities(g, start, end, "cycling", _is_ride)


def cycle_build(a, splits):
    """Summary row off the activity object + one row per lap from its splits."""
    head = _head(a)
    laps = splits.get("lapDTOs", []) or []
    lap_rows = [{**lap_base(head, i, L),
                 "max_speed_kmh": kmh(L.get("maxSpeed")),
                 "avg_cadence_rpm": _num(L, "averageBikeCadence", 0),
                 "max_cadence_rpm": _num(L, "maxBikeCadence", 0)}
                for i, L in enumerate(laps, start=1)]

    # The activity list never carries a ride's average temperature, but its laps do; their
    # duration-weighted mean matches Garmin's own ride average.
    temps = [(L["averageTemperature"], L["duration"]) for L in laps
             if isinstance(L.get("averageTemperature"), (int, float)) and L.get("duration")]
    avg_temp = round(sum(t * s for t, s in temps) / sum(s for _, s in temps), 1) if temps else ""

    summary = {**summary_base(head, a, len(lap_rows)),
               "max_speed_kmh": kmh(a.get("maxSpeed")),
               "avg_cadence_rpm": _num(a, "averageBikingCadenceInRevPerMinute", 0),
               "max_cadence_rpm": _num(a, "maxBikingCadenceInRevPerMinute", 0),
               "avg_temp_c": avg_temp}
    return lap_rows, summary


CYCLE = frozendict(name="cycle", noun="ride", part="lap", part_order="lap_index",
                   overlap=CYCLE_OVERLAP, state=CYCLE_STATE,
                   summary_csv=CYCLE_SUMMARY_CSV, summary_cols=CYCLE_SUMMARY_COLS,
                   parts_csv=CYCLE_LAPS_CSV, parts_cols=CYCLE_LAP_COLS,
                   list=cycle_list, build=cycle_build)


def cycle_run(g, mode):
    activity_run(g, mode, CYCLE)


# ===========================================================================
# RUN  — one row per run + one row per lap
# ===========================================================================
RUN_OVERLAP = 7
RUN_SUMMARY_CSV = path("run_summary.csv")
RUN_LAPS_CSV    = path("run_laps.csv")
RUN_STATE       = path("run_export_state.json")

RUN_SUMMARY_COLS = [
    "activity_id", "date", "activity_name", "type",
    "distance_km", "duration_s", "moving_duration_s",
    "avg_pace_per_km", "avg_speed_kmh",
    "elevation_gain_m", "elevation_loss_m", "avg_hr", "max_hr",
    "avg_cadence_spm", "max_cadence_spm", "avg_stride_length_cm",
    "avg_power_w", "max_power_w", "normalized_power_w",
    "avg_ground_contact_ms", "avg_vertical_oscillation_cm", "avg_vertical_ratio",
    "calories", "aerobic_te", "anaerobic_te", "training_effect_label", "num_laps",
]
RUN_LAP_COLS = [
    "activity_id", "date", "activity_name", "type", "lap_index",
    "distance_m", "duration_s", "moving_duration_s",
    "pace_per_km", "avg_speed_kmh", "avg_hr", "max_hr",
    "avg_power_w", "max_power_w", "normalized_power_w",
    "avg_cadence_spm", "max_cadence_spm", "stride_length_cm",
    "ground_contact_ms", "vertical_oscillation_cm",
    "elevation_gain_m", "calories", "intensity_type",
]


def run_list(g, start, end):
    # running, trail_running, treadmill_running, track_running, …
    return list_activities(g, start, end, "running", lambda a: "run" in _typekey(a))


def run_build(a, splits):
    """Summary row off the activity object + one row per lap from its splits."""
    head = _head(a)
    laps = splits.get("lapDTOs", []) or []
    lap_rows = [{**lap_base(head, i, L),
                 "pace_per_km": pace_per_km(L.get("averageSpeed")),
                 "avg_cadence_spm": _num(L, "averageRunCadence", 0),
                 "max_cadence_spm": _num(L, "maxRunCadence", 0),
                 "stride_length_cm": _num(L, "strideLength"),
                 "ground_contact_ms": _num(L, "groundContactTime"),
                 "vertical_oscillation_cm": _num(L, "verticalOscillation")}
                for i, L in enumerate(laps, start=1)]
    summary = {**summary_base(head, a, len(lap_rows)),
               "avg_pace_per_km": pace_per_km(a.get("averageSpeed")),
               "avg_cadence_spm": _num(a, "averageRunningCadenceInStepsPerMinute", 0),
               "max_cadence_spm": _num(a, "maxRunningCadenceInStepsPerMinute", 0),
               "avg_stride_length_cm": _num(a, "avgStrideLength"),
               "avg_ground_contact_ms": _num(a, "avgGroundContactTime"),
               "avg_vertical_oscillation_cm": _num(a, "avgVerticalOscillation"),
               "avg_vertical_ratio": _num(a, "avgVerticalRatio"),
               "training_effect_label": a.get("trainingEffectLabel", "")}
    return lap_rows, summary


RUN = frozendict(name="run", noun="run", part="lap", part_order="lap_index",
                 overlap=RUN_OVERLAP, state=RUN_STATE,
                 summary_csv=RUN_SUMMARY_CSV, summary_cols=RUN_SUMMARY_COLS,
                 parts_csv=RUN_LAPS_CSV, parts_cols=RUN_LAP_COLS,
                 list=run_list, build=run_build)


def run_run(g, mode):
    activity_run(g, mode, RUN)


# ===========================================================================
# DAILY  — one row per calendar day
# ===========================================================================
DAILY_START   = "2022-12-01"
DAILY_OVERLAP = 3
DAILY_CSV     = path("daily_metrics.csv")
DAILY_STATE   = path("daily_export_state.json")
DAILY_PAUSE   = 0.15

DAILY_COLS = [
    "date",
    "sleep_score", "sleep_hours", "deep_min", "light_min", "rem_min", "awake_min",
    "resting_hr", "hrv_last_night", "hrv_weekly_avg", "hrv_status",
    "body_battery_high", "body_battery_low", "body_battery_charged", "body_battery_drained",
    "stress_avg", "stress_max", "spo2_avg", "respiration_avg",
    "steps", "floors_ascended", "intensity_moderate_min", "intensity_vigorous_min",
    "calories_total", "calories_active",
    "training_readiness_score", "training_readiness_level",
    "vo2max", "endurance_score", "fitness_age",
]


def daily_day_row(g, dstr, weekly, failures):
    """One day's row. A call that fails counts as no data, and a transient failure is
    recorded in `failures` so the day is fetched again next run."""
    def get(endpoint):
        return safe(lambda: endpoint(dstr), None, DAILY_PAUSE, failures) or {}
    return daily_row(dstr, get(g.get_user_summary), get(g.get_sleep_data),
                     get(g.get_hrv_data), get(g.get_training_readiness), weekly)


def _readiness(tr):
    """Training readiness arrives as a list of readings (latest first) or as one dict."""
    if isinstance(tr, list):
        return (tr[0] if tr else {}) or {}
    return tr if isinstance(tr, dict) else {}


def daily_row(dstr, us, sleep, hrv, readiness, weekly):
    """A day's CSV row from Garmin's per-day answers ({} where there was none)."""
    sl = sleep.get("dailySleepDTO") or {}
    hv = hrv.get("hrvSummary") or {}
    tr0 = _readiness(readiness)
    sleep_overall = (sl.get("sleepScores") or {}).get("overall") or {}
    return {
        "date": dstr,
        "sleep_score": sleep_overall.get("value", ""),
        "sleep_hours": round(sl["sleepTimeSeconds"] / 3600, 2) if sl.get("sleepTimeSeconds") else "",
        "deep_min": mins(sl.get("deepSleepSeconds")),
        "light_min": mins(sl.get("lightSleepSeconds")),
        "rem_min": mins(sl.get("remSleepSeconds")),
        "awake_min": mins(sl.get("awakeSleepSeconds")),
        "resting_hr": us.get("restingHeartRate", ""),
        "hrv_last_night": hv.get("lastNightAvg", ""),
        "hrv_weekly_avg": hv.get("weeklyAvg", ""),
        "hrv_status": hv.get("status", ""),
        "body_battery_high": us.get("bodyBatteryHighestValue", ""),
        "body_battery_low": us.get("bodyBatteryLowestValue", ""),
        "body_battery_charged": us.get("bodyBatteryChargedValue", ""),
        "body_battery_drained": us.get("bodyBatteryDrainedValue", ""),
        "stress_avg": us.get("averageStressLevel", ""),
        "stress_max": us.get("maxStressLevel", ""),
        "spo2_avg": us.get("averageSpo2", ""),
        "respiration_avg": us.get("avgWakingRespirationValue", ""),
        "steps": us.get("totalSteps", ""),
        "floors_ascended": _num(us, "floorsAscended"),
        "intensity_moderate_min": us.get("moderateIntensityMinutes", ""),
        "intensity_vigorous_min": us.get("vigorousIntensityMinutes", ""),
        "calories_total": us.get("totalKilocalories", ""),
        "calories_active": us.get("activeKilocalories", ""),
        "training_readiness_score": tr0.get("score", ""),
        "training_readiness_level": tr0.get("level", ""),
        "vo2max": weekly.get("vo2max", ""),
        "endurance_score": weekly.get("endurance", ""),
        "fitness_age": weekly.get("fitness_age", ""),
    }


def daily_weekly_sample(g, dstr, failures):
    ts = safe(lambda: g.get_training_status(dstr), {}, DAILY_PAUSE, failures) or {}
    vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {}).get("vo2MaxValue", "")
    fa = safe(lambda: g.get_fitnessage_data(dstr), {}, DAILY_PAUSE, failures) or {}
    fitness_age = _num(fa, "fitnessAge")
    d = datetime.fromisoformat(dstr).date()
    es = safe(lambda: g.get_endurance_score((d - timedelta(days=6)).isoformat(), dstr), {},
              DAILY_PAUSE, failures) or {}
    endurance = (es.get("enduranceScoreDTO") or {}).get("overallScore", "")
    return {"vo2max": vo2, "fitness_age": fitness_age, "endurance": endurance}


def daily_run(g, mode):
    state = load_state(DAILY_STATE, {"start_date": DAILY_START, "last_updated": None,
                                     "num_days": 0})
    if mode == "status":
        print(f"  daily : {state['num_days']} days  (last {state['last_updated'] or 'never'})"
              f"{retry_note(len(state.get('failed_days') or []))}")
        return
    today = date.today()
    days, rows = daily_plan(state, resolve_mode(mode, state), today)
    failed_days = daily_fetch(g, days, rows)
    write_csv(DAILY_CSV, DAILY_COLS, [rows[k] for k in sorted(rows)])
    state.update({"last_updated": today.isoformat(), "num_days": len(rows),
                  "failed_days": failed_days,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(DAILY_STATE, state)
    log(f"daily: +{len(days)} processed → {len(rows)} days written to {os.path.basename(DAILY_CSV)}",
        icon="✓")


def daily_plan(state, mode, today):
    """The days to fetch, oldest first, and the rows (by date) to merge them into."""
    if mode == "init":
        from_date = datetime.fromisoformat(state["start_date"]).date()
        rows, retry = {}, set()
        ndays = (today - from_date).days + 1
        log(f"daily INIT: {from_date} → {today}  ({ndays} days, ~{ndays * 4} API calls — this is slow)",
            icon="▶")
    else:
        from_date = date.fromisoformat(overlap_start(state, DAILY_OVERLAP))
        rows = read_csv_by_key(DAILY_CSV, "date")
        # days an earlier run couldn't fetch, however old — otherwise they'd stay blank for good
        retry = {datetime.fromisoformat(s).date() for s in state.get("failed_days", [])}
        ndays = (today - from_date).days + 1
        log(f"daily UPDATE: {from_date} → {today}  ({ndays} days"
            + (f" + {len(retry)} to retry" if retry else "") + ")", icon="▶")
    return sorted({from_date + timedelta(days=i) for i in range(ndays)} | retry), rows


def daily_fetch(g, days, rows):
    """Fill `rows` with each day; returns the days to retry next run. Slow-moving metrics
    are sampled once per ISO week and carried forward, so a failed sample taints the week."""
    cur_week = None
    weekly, weekly_failed = {"vo2max": "", "endurance": "", "fitness_age": ""}, False
    failed_days = []
    prog = Progress("daily", len(days))
    for d in days:
        dstr = d.isoformat()
        wk = (d.isocalendar().year, d.isocalendar().week)
        if wk != cur_week:
            cur_week = wk
            wf = []
            weekly = daily_weekly_sample(g, dstr, wf)
            weekly_failed = bool(wf)
        failures = []
        rows[dstr] = daily_day_row(g, dstr, weekly, failures)
        if failures or weekly_failed:
            failed_days.append(dstr)
        prog.step(dstr)
    prog.done(f"{len(days)} days fetched")
    if failed_days:
        log(f"daily: {len(failed_days)} days hit rate limits or network errors — "
            f"they'll be re-fetched on the next update", icon="!")
    return failed_days


# ===========================================================================
# BODY  — weigh-ins (time series) + PRs + race predictions (snapshots)
# ===========================================================================
BODY_START = "2018-01-01"
WEIGHT_CSV = path("body_weight.csv")
PR_CSV     = path("personal_records.csv")
RACE_CSV   = path("race_predictions.csv")
BODY_STATE = path("body_export_state.json")

WEIGHT_COLS = ["date", "weight_kg", "bmi", "body_fat_pct", "body_water_pct",
               "muscle_mass_kg", "bone_mass_kg", "visceral_fat", "metabolic_age",
               "physique_rating", "source"]
PR_COLS  = ["type_id", "label", "activity_type", "value_raw", "value_pretty",
            "units", "record_date"]
RACE_COLS = ["pulled_date", "calendar_date", "distance", "time_seconds", "time_pretty"]

PR_LABELS = frozendict({
    1: ("Fastest 1 km", "time"), 2: ("Fastest 1 mile", "time"),
    3: ("Fastest 5 km", "time"), 4: ("Fastest 10 km", "time"),
    5: ("Fastest half marathon", "time"), 6: ("Fastest marathon", "time"),
    7: ("Longest run", "distance"), 8: ("Longest ride", "distance"),
    12: ("Most steps in a day", "count"), 13: ("Most steps in a week", "count"),
    14: ("Most steps in a month", "count"), 16: ("Most floors in a day", "count"),
})


def body_pull_weight(g):
    """Every weigh-in, a year per call. A year that fails to fetch keeps its rows from the
    last run rather than vanishing from the CSV."""
    rows, failed = {}, []
    years = year_windows(BODY_START, date.today().isoformat())
    prog = Progress("weigh-ins", len(years))
    for lo, hi in years:
        try:
            answer = g.get_weigh_ins(lo, hi) or {}
        except Exception:
            failed.append((lo, hi))
            answer = {}
        time.sleep(0.2)
        rows.update((m.get("samplePk"), weigh_in_row(m)) for m in _weigh_ins(answer))
        prog.step(f"{lo[:4]}: {len(rows)} so far")
    prog.done(f"{len(rows)} weigh-ins")
    kept = [r for r in read_csv_list(WEIGHT_CSV)
            if any(lo <= r["date"] <= hi for lo, hi in failed)]
    if failed:
        log(f"body: weigh-ins for {', '.join(lo[:4] for lo, _ in failed)} failed to fetch — "
            f"kept their {len(kept)} rows from the last run", icon="!")
    ordered = sorted(list(rows.values()) + kept, key=lambda r: r["date"])
    write_csv(WEIGHT_CSV, WEIGHT_COLS, ordered)
    return len(ordered)


def _weigh_ins(answer):
    """Every weigh-in in one get_weigh_ins answer, in the order Garmin sent them."""
    return [m for day in answer.get("dailyWeightSummaries", []) or []
            for m in day.get("allWeightMetrics", []) or []]


def _kg(grams):
    return round(grams / 1000, 2) if grams else ""


def weigh_in_row(m):
    """One weigh-in's CSV row. Garmin reports masses in grams."""
    return {"date": (m.get("calendarDate") or "")[:10],
            "weight_kg": _kg(m.get("weight")), "bmi": _num(m, "bmi"),
            "body_fat_pct": m.get("bodyFat", ""), "body_water_pct": m.get("bodyWater", ""),
            "muscle_mass_kg": _kg(m.get("muscleMass")), "bone_mass_kg": _kg(m.get("boneMass")),
            "visceral_fat": m.get("visceralFat", ""), "metabolic_age": m.get("metabolicAge", ""),
            "physique_rating": m.get("physiqueRating", ""), "source": m.get("sourceType", "")}


def body_pull_prs(g):
    try:
        prs = g.get_personal_record() or []
    except Exception as e:
        log(f"body: personal records failed to fetch ({e}) — kept the last snapshot", icon="!")
        return len(read_csv_list(PR_CSV))
    out = sorted(map(pr_row, prs),
                 key=lambda r: r["type_id"] if isinstance(r["type_id"], int) else 999)
    write_csv(PR_CSV, PR_COLS, out)
    return len(out)


def _km(metres):
    return round(metres / 1000, 2) if isinstance(metres, (int, float)) else ""


def _count(n):
    return int(n) if isinstance(n, (int, float)) else ""


# How each kind of record in PR_LABELS is shown: (formatter, units). Unknown kinds: blank.
PR_FORMATS = frozendict(time=(fmt_time, "h:mm:ss"), distance=(_km, "km"), count=(_count, "count"))
PR_UNFORMATTED = (lambda value: "", "")


def pr_row(p):
    """One personal record's CSV row."""
    tid, value = p.get("typeId"), p.get("value")
    label, kind = PR_LABELS.get(tid, (f"type_{tid}", "raw"))
    pretty, units = PR_FORMATS.get(kind, PR_UNFORMATTED)
    rec_date = (p.get("prStartTimeLocalFormatted")
                or p.get("activityStartDateTimeLocalFormatted") or "")
    return {"type_id": tid, "label": label, "activity_type": p.get("activityType", ""),
            "value_raw": value, "value_pretty": pretty(value), "units": units,
            "record_date": rec_date[:10]}


def body_pull_race(g):
    try:
        rp = g.get_race_predictions() or {}
    except Exception as e:
        log(f"body: race predictions failed to fetch ({e}) — kept the last snapshot", icon="!")
        return len(read_csv_list(RACE_CSV))
    pulled = date.today().isoformat()
    cal = rp.get("calendarDate", "")
    rows = []
    for dist, key in [("5K", "time5K"), ("10K", "time10K"),
                      ("Half Marathon", "timeHalfMarathon"), ("Marathon", "timeMarathon")]:
        sec = rp.get(key)
        if sec:
            rows.append({"pulled_date": pulled, "calendar_date": cal, "distance": dist,
                         "time_seconds": sec, "time_pretty": fmt_time(sec)})
    write_csv(RACE_CSV, RACE_COLS, rows)
    return len(rows)


def body_run(g, mode):
    if mode == "status":
        print(f"  body  : {len(read_csv_list(WEIGHT_CSV))} weigh-ins, "
              f"{len(read_csv_list(PR_CSV))} PRs, {len(read_csv_list(RACE_CSV))} race predictions")
        return
    log("body ▶ refreshing weigh-ins + PRs + race predictions", icon="▶")
    nw = body_pull_weight(g)
    npr = body_pull_prs(g)
    log(f"body: {npr} personal records")
    nr = body_pull_race(g)
    log(f"body: {nr} race predictions")
    save_state(BODY_STATE, {"last_updated": date.today().isoformat(), "weigh_ins": nw,
                            "personal_records": npr, "race_predictions": nr,
                            "generated_at": datetime.now().isoformat(timespec="seconds")})
    log(f"body: {nw} weigh-ins, {npr} PRs, {nr} race predictions written", icon="✓")


# ===========================================================================
# RAW  — complete per-activity API capture, one gzipped JSON per activity
#
# The CSV domains above are opinionated views: each picks a handful of fields and
# throws the rest away, so widening a view means re-fetching everything. This domain
# fetches every endpoint Garmin exposes for every activity in the account (not just
# swim/cycle/run) and keeps the raw responses on disk, so any later view can be built
# offline. It is deliberately excluded from `all` — a full pull is ~2.5 hours.
# ===========================================================================
RAW_DIR      = path("raw")
RAW_ACCOUNT  = path("raw_account.json.gz")
RAW_STATE    = path("raw_export_state.json")
RAW_OVERLAP  = 7                 # days `update` reaches back to catch late-synced activities
RAW_SCHEMA   = 1                 # bump to invalidate every cached file
RAW_PAUSE    = 0.25              # between individual API calls
RAW_BACKOFF  = 30                # first sleep after a rate-limit rejection
RAW_MAXCHART = 20000             # time series at native resolution (~1 sample/s)
RAW_MAXPOLY  = 4000              # GPS polyline points
RAW_SAVE_EVERY = 25              # checkpoint the manifest this often
RAW_SET_KEYS = ("strength", "fitness_equipment", "indoor_cardio")


# name -> (fetch, applies?). `applies` reads the already-fetched list object so we
# skip calls that are certain to come back empty — worth ~25 min over a full pull.
RAW_ENDPOINTS = [
    ("activity",        lambda g, i, a: g.get_activity(i),                 lambda a: True),
    ("splits",          lambda g, i, a: g.get_activity_splits(i),          lambda a: True),
    ("typed_splits",    lambda g, i, a: g.get_activity_typed_splits(i),    lambda a: True),
    ("split_summaries", lambda g, i, a: g.get_activity_split_summaries(i), lambda a: True),
    ("hr_zones",        lambda g, i, a: g.get_activity_hr_in_timezones(i),
                        lambda a: a.get("hrTimeInZone_1") is not None),
    ("power_zones",     lambda g, i, a: g.get_activity_power_in_timezones(i),
                        lambda a: a.get("powerTimeInZone_1") is not None),
    ("weather",         lambda g, i, a: g.get_activity_weather(i),         lambda a: True),
    ("gear",            lambda g, i, a: g.get_activity_gear(i),            lambda a: True),
    ("exercise_sets",   lambda g, i, a: g.get_activity_exercise_sets(i),
                        lambda a: any(k in _typekey(a) for k in RAW_SET_KEYS)),
    ("details",         lambda g, i, a: g.get_activity_details(i, RAW_MAXCHART, RAW_MAXPOLY),
                        lambda a: True),
]


# Endpoint failures split two ways. A 404 is permanent — that endpoint genuinely has
# nothing for that activity, and asking again tomorrow won't change it. A dropped
# connection or a rate-limit rejection is transient, and a capture holding one is a hole
# in the cache that must not be mistaken for a finished one.
RAW_RETRYABLE = ("GarminConnectConnectionError", "GarminConnectTooManyRequestsError",
                 "ConnectionError", "ConnectionResetError", "ReadTimeout", "Timeout",
                 "ChunkedEncodingError", "rate limited", "gave up")


def raw_retryable(rec):
    return any(str(e).split(":", 1)[0].strip() in RAW_RETRYABLE
               for e in (rec.get("errors") or {}).values())


def raw_path(aid, partial=False):
    """Incomplete captures are parked under a different name so the resume scan — which
    reads filenames, not contents — can't mistake them for finished work."""
    return os.path.join(RAW_DIR, f"{aid}{'.partial' if partial else ''}.json.gz")


def raw_write(p, obj):
    """Write gzipped JSON atomically — a killed run never leaves a half-written file."""
    with atomic_write(p, gzip.open, "wt", encoding="utf-8") as f:
        json.dump(obj, f, default=str)


def raw_read(p):
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def raw_files():
    return os.listdir(RAW_DIR) if os.path.isdir(RAW_DIR) else []


def raw_fetch(fn, tries=3):
    """Call fn with polite pacing. Backs off and retries on rate limits only;
    other failures return their message so the caller can record them."""
    delay, err = RAW_BACKOFF, "gave up"
    for _ in range(tries):
        try:
            v = fn()
            time.sleep(RAW_PAUSE)
            return v, None
        except GarminConnectTooManyRequestsError as e:
            err = f"rate limited: {e}"
            log(f"rate limited — sleeping {delay}s before retry", icon="!")
            time.sleep(delay)
            delay *= 2
        except Exception as e:
            time.sleep(RAW_PAUSE)
            return None, f"{type(e).__name__}: {e}"
    return None, err


def raw_capture(g, a):
    """Every applicable endpoint for one activity, plus whatever failed."""
    aid = a["activityId"]
    rec = {"schema": RAW_SCHEMA, "activity_id": aid,
           "date": (a.get("startTimeLocal") or "")[:10],
           "type_key": _typekey(a),
           "fetched_at": datetime.now().isoformat(timespec="seconds"),
           "list_object": a, "endpoints": {}, "skipped": [], "errors": {}}
    for name, fetch, applies in RAW_ENDPOINTS:
        if not applies(a):
            rec["skipped"].append(name)
            continue
        v, err = raw_fetch(partial(fetch, g, aid, a))
        if err:
            rec["errors"][name] = err
        else:
            rec["endpoints"][name] = v
    return rec


def raw_account(g):
    """Account-level context the per-activity files can't carry: zone configs, FTP,
    lactate threshold, devices, gear. Cheap, so it refreshes on every run."""
    prof = safe(lambda: g.get_user_profile(), {}, RAW_PAUSE) or {}
    pk = prof.get("id") or (prof.get("userData") or {}).get("userProfilePk")
    today = date.today().isoformat()
    calls = [
        ("user_profile",            lambda: prof),
        ("userprofile_settings",    lambda: g.get_userprofile_settings()),
        ("heart_rate_zones",        lambda: g.get_heart_rate_zones()),
        ("power_zones",             lambda: g.get_power_zones()),
        ("lactate_threshold",       lambda: g.get_lactate_threshold(latest=True)),
        ("cycling_ftp",             lambda: g.get_cycling_ftp()),
        ("devices",                 lambda: g.get_devices()),
        ("primary_training_device", lambda: g.get_primary_training_device()),
        ("activity_types",          lambda: g.get_activity_types()),
        ("max_metrics",             lambda: g.get_max_metrics(today)),
    ]
    if pk:
        calls.append(("gear", lambda: g.get_gear(pk)))
    out = {"schema": RAW_SCHEMA, "pulled_date": today,
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "endpoints": {}, "errors": {}}
    for name, fn in calls:
        v, err = raw_fetch(fn)
        if err:
            out["errors"][name] = err
        else:
            out["endpoints"][name] = v
    raw_write(RAW_ACCOUNT, out)
    if out["errors"]:
        log(f"raw: account snapshot had {len(out['errors'])} failures: "
            f"{', '.join(out['errors'])}", icon="!")
    return out


def raw_list_all(g, start, end):
    """Every activity in the window, every type — a year at a time so no one
    response gets huge and a mid-run failure only costs that year."""
    acts, seen, out = [], set(), []
    for lo, hi in year_windows(start, end):
        chunk, err = raw_fetch(partial(g.get_activities_by_date, lo, hi))
        if err:
            log(f"raw: listing {lo[:4]} failed: {err}", icon="✗")
        else:
            acts.extend(chunk or [])
    for a in acts:
        if a.get("activityId") and a["activityId"] not in seen:
            seen.add(a["activityId"])
            out.append(a)
    out.sort(key=lambda a: a.get("startTimeLocal", ""))
    return out


def raw_cached_ids():
    """What's already on disk — the source of truth for resuming, not the manifest.
    `<id>.partial.json.gz` fails the int() and is correctly excluded."""
    ids = set()
    for f in raw_files():
        if f.endswith(".json.gz"):
            try:
                ids.add(int(f[:-len(".json.gz")]))
            except ValueError:
                pass
    return ids


def raw_incomplete_ids():
    """Cached files that hit a transient failure and are worth fetching again. Reads
    every file, so it's `init`-only — `update` relies on the .partial naming instead."""
    bad = set()
    for aid in raw_cached_ids():
        try:
            rec = raw_read(raw_path(aid))
        except Exception as e:
            log(f"raw: {aid}.json.gz unreadable ({type(e).__name__}) — will re-fetch", icon="!")
            bad.add(aid)
            continue
        if raw_retryable(rec):
            bad.add(aid)
    return bad


def raw_partial_list_objects():
    """List objects of the captures parked as .partial. `update` only lists recent activity,
    so without these an older incomplete capture would wait for the next `init`."""
    out = []
    for f in raw_files():
        if not f.endswith(".partial.json.gz"):
            continue
        try:
            out.append(raw_read(os.path.join(RAW_DIR, f))["list_object"])
        except Exception as e:
            log(f"raw: {f} unreadable ({type(e).__name__}) — `raw init` will re-fetch it", icon="!")
    return out


def raw_disk_bytes():
    return sum(os.path.getsize(os.path.join(RAW_DIR, f)) for f in raw_files()
               if f.endswith(".json.gz"))


def raw_run(g, mode):
    default = {"start_date": EARLIEST, "last_updated": None, "schema": RAW_SCHEMA,
               "num_activities": 0, "num_with_errors": 0}
    state = load_state(RAW_STATE, default)
    today = date.today().isoformat()

    if mode == "status":
        n = len(raw_cached_ids())
        print(f"  raw   : {n} activities cached, {raw_disk_bytes() / 1e6:.0f} MB on disk"
              f"  (last {state.get('last_updated') or 'never'})")
        return
    if mode == "auto":
        mode = "update" if state.get("last_updated") else "init"

    os.makedirs(RAW_DIR, exist_ok=True)
    have = raw_cached_ids()
    if state.get("schema") != RAW_SCHEMA:
        log(f"raw: cache schema {state.get('schema')} != {RAW_SCHEMA}, re-fetching everything",
            icon="!")
        have = set()
        mode = "init"                   # every file is stale, not just recent ones

    # `init` rescans all of history for gaps; `update` only looks at recent activity
    # (plus anything left .partial). Both skip whatever is already on disk, so either is
    # safe to re-run.
    from_date = EARLIEST if mode == "init" else overlap_start(state, RAW_OVERLAP)
    if mode == "init" and have:
        log(f"raw: verifying {len(have)} cached files …")
        broken = raw_incomplete_ids()
        if broken:
            log(f"raw: {len(broken)} cached files hold transient failures — re-fetching",
                icon="!")
            have -= broken
    log(f"raw {mode.upper()}: {from_date} → {today}  ({len(have)} already cached)", icon="▶")

    log("raw: refreshing account snapshot (zones, FTP, devices, gear)")
    raw_account(g)

    acts = raw_list_all(g, from_date, today)
    if mode == "init" and acts:            # oldest first, so this is your first activity
        state["start_date"] = acts[0]["startTimeLocal"][:10]
    if mode == "update":
        listed = {a["activityId"] for a in acts}
        retry = [a for a in raw_partial_list_objects() if a.get("activityId") not in listed]
        if retry:
            log(f"raw: retrying {len(retry)} incomplete captures from earlier runs")
        acts += retry
    todo = [a for a in acts if a["activityId"] not in have]
    log(f"raw: {len(acts)} activities in range, {len(todo)} to fetch "
        f"(~{len(todo) * len(RAW_ENDPOINTS) * (RAW_PAUSE + 0.2) / 3600:.1f} h)")

    n_err = n_partial = 0
    prog = Progress("raw", len(todo))
    for a in todo:
        aid = a["activityId"]
        rec = raw_capture(g, a)
        partial = raw_retryable(rec)
        raw_write(raw_path(aid, partial), rec)
        if partial:
            n_partial += 1
        else:
            stale = raw_path(aid, True)         # an earlier attempt may have left one
            if os.path.exists(stale):
                os.remove(stale)
            have.add(aid)
        if rec["errors"]:
            n_err += 1
        prog.step(f"{rec['date']} {rec['type_key']}"
                  + (f" ({len(rec['errors'])} err)" if rec["errors"] else ""))
        if len(have) % RAW_SAVE_EVERY == 0:
            # not the schema: until the run finishes, older files may still be stale
            state.update({"num_activities": len(have)})
            save_state(RAW_STATE, state)
    if todo:
        prog.done(f"{len(todo)} activities, {n_err} with errors, {n_partial} incomplete")
    if n_partial:
        log(f"raw: {n_partial} activities hit network failures and were left incomplete — "
            f"re-run `raw update` to retry them", icon="!")

    nbytes = raw_disk_bytes()
    state.update({"last_updated": today, "schema": RAW_SCHEMA,
                  "num_activities": len(have),
                  "num_with_errors": n_err,
                  "bytes_on_disk": nbytes,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(RAW_STATE, state)
    log(f"raw: +{len(todo)} new → {len(have)} activities cached, "
        f"{nbytes / 1e6:.0f} MB in {os.path.basename(RAW_DIR)}/", icon="✓")


# ===========================================================================
RUNNERS = frozendict(swim=swim_run, cycle=cycle_run, run=run_run,
                     daily=daily_run, body=body_run, raw=raw_run)


USAGE = f"""Garmin exporter — export Garmin Connect data to CSV in ./data

Usage: garmin_export.py [domain] [mode]
  domain : {'|'.join(DOMAINS)}|all   (default: all)
           {RAW_DOMAIN}                        (opt-in; not included in `all`)
  mode   : {'|'.join(MODES)}         (default: auto)

Args are order-independent. Examples:
  garmin_export.py                 all domains, auto (init first run, else update)
  garmin_export.py all update      update everything in one login
  garmin_export.py swim update     one domain only
  garmin_export.py daily init      full daily rebuild (slow)
  garmin_export.py raw init        cache every endpoint for every activity (~2.5 h)
  garmin_export.py raw update      cache anything new since the last raw run
  garmin_export.py status          report all domains, fetch nothing"""


def main(argv):
    global QUIET
    domain, mode = "all", "auto"
    for a in argv:
        al = a.lower()
        if al in ("-h", "--help", "help"):
            print(USAGE)
            return
        if al in ("-q", "--quiet"):
            QUIET = True
        elif al in DOMAINS or al in (RAW_DOMAIN, "all"):
            domain = al
        elif al in MODES:
            mode = al
        else:
            sys.exit(f"Unknown arg: {a}\n\n{USAGE}")

    targets = list(DOMAINS) if domain == "all" else [domain]

    if mode == "status":
        print(f"Garmin export — data dir: {OUTPUT_DIR}")
        # raw is excluded from `all` for fetching, but reporting it costs nothing
        for t in (targets + [RAW_DOMAIN] if domain == "all" else targets):
            RUNNERS[t](None, "status")
        return

    log(f"Garmin export — domains: {', '.join(targets)} · mode: {mode} · dir: {OUTPUT_DIR}", icon="●")
    log("authenticating with saved Garmin tokens …")
    g = login()                         # single login shared across all domains
    log("authenticated", icon="✓")

    failed = []
    for t in targets:
        try:
            RUNNERS[t](g, mode)
        except Exception as e:
            failed.append(t)
            log(f"{t} FAILED: {e}", icon="✗")

    if failed:
        log(f"finished with errors in: {', '.join(failed)} · total {fmt_dur(time.time() - _T0)}", icon="✗")
        sys.exit(1)
    log(f"all done · {len(targets)} domain(s) · total {fmt_dur(time.time() - _T0)}", icon="●")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        if IS_TTY:
            print(file=sys.stderr)          # step off the "^C" or progress bar on this line
        log("interrupted — no file was left half-written; rerun to continue", icon="✗")
        sys.exit(130)
