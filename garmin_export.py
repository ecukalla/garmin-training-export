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
import os
import sys
import csv
import gzip
import json
import re
import time
from datetime import date, datetime, timedelta

# ---- shared config --------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.environ.get("SWIM_OUTPUT_DIR", os.path.join(SCRIPT_DIR, "data"))
os.makedirs(OUTPUT_DIR, exist_ok=True)
TOKEN_STORE = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))

DOMAINS = ("swim", "cycle", "run", "daily", "body")   # what `all` expands to
RAW_DOMAIN = "raw"                                    # opt-in only — never part of `all`
MODES   = ("init", "update", "status", "auto")

def path(name):
    return os.path.join(OUTPUT_DIR, name)


# ---- logging & progress ---------------------------------------------------
_T0 = time.time()
QUIET = False                       # set from --quiet
IS_TTY = sys.stderr.isatty()


def fmt_dur(seconds):
    """Human-readable duration: 1h02m, 3m05s, 12s."""
    s = int(round(seconds))
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
            print("", file=sys.stderr, flush=True)      # finish the in-place line
        log(f"{self.label}: {summary} in {fmt_dur(time.time() - self.start)}", icon="✓")


# ---- shared helpers -------------------------------------------------------
def login():
    from garminconnect import Garmin
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


def save_state(p, obj):
    with open(p, "w") as f:
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
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def fmt_time(seconds):
    if not isinstance(seconds, (int, float)):
        return ""
    s = int(round(seconds))
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


def is_transient(e):
    """Whether a failed Garmin call is worth asking again on a later run: rate limits,
    auth hiccups, 5xx and dropped connections. A 4xx (404 = nothing there) or a response
    that won't parse comes back the same every time, so that answer is final."""
    import requests
    from garminconnect import (GarminConnectAuthenticationError, GarminConnectConnectionError,
                               GarminConnectNotFoundError, GarminConnectTooManyRequestsError)
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


def retry_note(state):
    """Status-line suffix for whatever the last run couldn't fetch."""
    n = len(state.get("failed") or state.get("failed_days") or [])
    return f", {n} awaiting retry" if n else ""


def fetch_splits(g, aid):
    """Splits for one activity. A permanent failure means there are none; a transient one
    is raised so the caller leaves the activity unprocessed for the next run instead of
    saving it with no laps forever."""
    try:
        return g.get_activity_splits(aid) or {}
    except Exception as e:
        if is_transient(e):
            raise
        print(f"   no splits for {aid}: {e}", file=sys.stderr)
        return {}


# ===========================================================================
# SWIM  — one row per swim + one row per pool length
# ===========================================================================
SWIM_START   = "2016-01-01"
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
    try:
        acts = g.get_activities_by_date(start, end, "swimming")
    except Exception:
        acts = g.get_activities_by_date(start, end)
    swims = [a for a in acts
             if "swim" in (a.get("activityType", {}).get("typeKey", "") or "").lower()]
    swims.sort(key=lambda a: a.get("startTimeLocal", ""))
    return swims


def swim_build(g, a, splits=None):
    """Pass `splits` to build from an already-fetched response (e.g. the raw cache)
    instead of calling Garmin — same logic either way, so views can be rebuilt offline."""
    aid  = a["activityId"]
    d    = (a.get("startTimeLocal") or "")[:10]
    name = a.get("activityName") or ""
    typ  = a["activityType"]["typeKey"]

    if splits is None:
        splits = fetch_splits(g, aid)
    ordered = [L for lap in splits.get("lapDTOs") or [] for L in lap.get("lengthDTOs") or []]

    pool_len = None
    set_idx = 0
    prev_active = False
    pending_rest = 0.0
    pending_rest_hr = []            # (avgHR, maxHR, duration) per resting length
    length_in_set = 0
    gn = 0
    tally = {}
    swolfs = []
    active_count = 0
    rest_total = 0.0
    length_rows = []

    for L in ordered:
        dist = L.get("distance")
        dur  = L.get("duration") or 0.0
        active = dist is not None and dist > 0
        if active:
            if pool_len is None:
                pool_len = dist
            if not prev_active:
                set_idx += 1
                length_in_set = 0
                rest_before = pending_rest
                rest_hr = _rest_hr(pending_rest_hr)
                pending_rest = 0.0
                pending_rest_hr = []
            else:
                rest_before = 0.0
                rest_hr = (None, None)
            length_in_set += 1
            gn += 1
            active_count += 1
            stroke  = L.get("swimStroke") or ""
            swolf   = L.get("averageSWOLF")
            strokes = L.get("totalNumberOfStrokes")
            pace100 = round(dur / dist * 100, 1) if dur and dist else None
            glitch  = 1 if (pace100 is not None and pace100 < SWIM_GLITCH_PACE) else 0
            if stroke:
                tally[stroke] = tally.get(stroke, 0) + 1
            if swolf is not None and not glitch:
                swolfs.append(swolf)
            length_rows.append({
                "activity_id": aid, "date": d, "activity_name": name, "type": typ,
                "pool_length_m": pool_len, "set_index": set_idx,
                "length_in_set": length_in_set, "global_length_n": gn,
                "distance_m": dist, "duration_s": round(dur, 1),
                "pace_per_100m_s": pace100, "stroke": stroke,
                "swolf": swolf, "strokes": strokes,
                "avg_hr": L.get("averageHR"), "max_hr": L.get("maxHR"),
                "rest_before_set_s": round(rest_before, 1) if rest_before else 0,
                "rest_avg_hr": rest_hr[0], "rest_max_hr": rest_hr[1],
                "likely_glitch": glitch,
            })
            prev_active = True
        else:
            pending_rest += dur
            rest_total += dur
            pending_rest_hr.append((L.get("averageHR"), L.get("maxHR"), dur))
            prev_active = False

    total_dist = a.get("distance")
    total_dur  = a.get("duration")
    moving_dur = a.get("movingDuration")
    avg_swolf  = round(sum(swolfs) / len(swolfs), 1) if swolfs else ""
    pace_mov   = round(moving_dur / total_dist * 100, 1) if (moving_dur and total_dist) else ""
    pace_ela   = round(total_dur / total_dist * 100, 1) if (total_dur and total_dist) else ""
    stroke_mix = "; ".join(f"{k}:{v}" for k, v in sorted(tally.items(), key=lambda x: -x[1]))

    summary_row = {
        "activity_id": aid, "date": d, "activity_name": name, "type": typ,
        "pool_length_m": pool_len or "",
        "total_distance_m": _num(a, "distance"), "active_lengths": active_count,
        "total_duration_s": round(total_dur, 1) if total_dur else "",
        "moving_duration_s": round(moving_dur, 1) if moving_dur else "",
        "rest_time_s": round(rest_total, 1),
        "avg_pace_moving_per_100m_s": pace_mov,
        "avg_pace_elapsed_per_100m_s": pace_ela,
        "avg_hr": a.get("averageHR"), "max_hr": a.get("maxHR"),
        "calories": a.get("calories"), "avg_swolf_clean": avg_swolf,
        "num_sets": set_idx, "stroke_mix": stroke_mix,
    }
    return length_rows, summary_row


def swim_run(g, mode):
    default = {"start_date": SWIM_START, "last_updated": None,
               "processed_ids": [], "num_swims": 0, "num_length_rows": 0}
    state = load_state(SWIM_STATE, default)
    today = date.today().isoformat()

    if mode == "status":
        print(f"  swim  : {state['num_swims']} swims, {state['num_length_rows']} length rows"
              f"  (last {state['last_updated'] or 'never'}){retry_note(state)}")
        return
    if mode == "auto":
        mode = "update" if state.get("last_updated") else "init"

    if mode == "init":
        from_date = state["start_date"]
        existing_summary, existing_lengths, processed = [], [], set()
        log(f"swim {mode.upper()}: {from_date} → {today}", icon="▶")
    else:
        last = state.get("last_updated") or state["start_date"]
        from_date = max((datetime.fromisoformat(last) - timedelta(days=SWIM_OVERLAP)).date().isoformat(),
                        state["start_date"])
        from_date = min([from_date] + list(state.get("failed", {}).values()))   # reach back to retry
        existing_summary = read_csv_list(SWIM_SUMMARY_CSV)
        existing_lengths = read_csv_list(SWIM_LENGTHS_CSV)
        processed = set(str(x) for x in state.get("processed_ids", []))
        log(f"swim {mode.upper()}: {from_date} → {today}  ({len(processed)} already pulled)", icon="▶")

    swims = swim_list(g, from_date, today)
    todo = [a for a in swims if str(a["activityId"]) not in processed]
    log(f"swim: {len(swims)} in range, {len(todo)} new to fetch")

    new_lengths, new_summary, added, failed, err = [], [], [], {}, None
    prog = Progress("swim", len(todo))
    for a in todo:
        sr_date = (a.get("startTimeLocal") or "")[:10]
        try:
            lr, sr = swim_build(g, a)
        except Exception as e:
            failed[str(a["activityId"])] = sr_date or state["start_date"]
            err = e
            prog.step(f"{sr_date} failed")
            time.sleep(0.3)
            continue
        new_lengths.extend(lr)
        new_summary.append(sr)
        added.append(str(a["activityId"]))
        processed.add(str(a["activityId"]))
        prog.step(f"{sr_date} ({len(lr)} lengths)")
        time.sleep(0.3)
    if todo:
        prog.done(f"{len(added)} swims, {len(new_lengths)} lengths")
    if failed:
        log(f"swim: {len(failed)} swims failed to fetch and will be retried next run "
            f"(last error: {type(err).__name__}: {err})", icon="!")

    all_summary = existing_summary + new_summary
    all_lengths = existing_lengths + new_lengths
    all_summary.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", ""))))
    all_lengths.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", "")),
                                     int(r.get("global_length_n") or 0)))
    write_csv(SWIM_SUMMARY_CSV, SWIM_SUMMARY_COLS, all_summary)
    write_csv(SWIM_LENGTHS_CSV, SWIM_LENGTH_COLS, all_lengths)

    state.update({"last_updated": today,
                  "processed_ids": sorted(processed, key=lambda x: int(x)),
                  "num_swims": len(all_summary), "num_length_rows": len(all_lengths),
                  "failed": failed,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(SWIM_STATE, state)
    log(f"swim: +{len(added)} new → {len(all_summary)} swims / {len(all_lengths)} lengths "
        f"written to {os.path.basename(SWIM_SUMMARY_CSV)}, {os.path.basename(SWIM_LENGTHS_CSV)}", icon="✓")


# ===========================================================================
# CYCLE  — one row per ride
# ===========================================================================
CYCLE_START   = "2016-01-01"
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


def cycle_list(g, start, end):
    try:
        acts = g.get_activities_by_date(start, end, "cycling")
    except Exception:
        acts = g.get_activities_by_date(start, end)
    # by parent type, not name: "enduro_mtb" has no "bik" in it, "motorcycling" does
    rides = [a for a in acts
             if CYCLE_TYPE_ID in ((a.get("activityType") or {}).get("typeId"),
                                  (a.get("activityType") or {}).get("parentTypeId"))]
    rides.sort(key=lambda a: a.get("startTimeLocal", ""))
    return rides


def cycle_build(g, a):
    """Summary row off the activity object + one row per lap from the splits call."""
    aid  = a["activityId"]
    d    = (a.get("startTimeLocal") or "")[:10]
    name = a.get("activityName") or ""
    typ  = a["activityType"]["typeKey"]

    splits = fetch_splits(g, aid)
    laps = splits.get("lapDTOs", []) or []

    lap_rows = []
    for i, L in enumerate(laps, start=1):
        lap_rows.append({
            "activity_id": aid, "date": d, "activity_name": name, "type": typ,
            "lap_index": i,
            "distance_m": _num(L, "distance"),
            "duration_s": _num(L, "duration"),
            "moving_duration_s": _num(L, "movingDuration"),
            "avg_speed_kmh": kmh(L.get("averageSpeed")),
            "max_speed_kmh": kmh(L.get("maxSpeed")),
            "avg_hr": L.get("averageHR", ""), "max_hr": L.get("maxHR", ""),
            "avg_power_w": _num(L, "averagePower", 0), "max_power_w": _num(L, "maxPower", 0),
            "normalized_power_w": _num(L, "normalizedPower", 0),
            "avg_cadence_rpm": _num(L, "averageBikeCadence", 0),
            "max_cadence_rpm": _num(L, "maxBikeCadence", 0),
            "elevation_gain_m": _num(L, "elevationGain"),
            "calories": L.get("calories", ""),
            "intensity_type": L.get("intensityType") or "",
        })

    # The activity list never carries a ride's average temperature, but its laps do; their
    # duration-weighted mean matches Garmin's own ride average.
    temps = [(L["averageTemperature"], L["duration"]) for L in laps
             if isinstance(L.get("averageTemperature"), (int, float)) and L.get("duration")]
    avg_temp = round(sum(t * s for t, s in temps) / sum(s for _, s in temps), 1) if temps else ""

    summary = {
        "activity_id": aid, "date": d, "activity_name": name, "type": typ,
        "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else "",
        "duration_s": _num(a, "duration"),
        "moving_duration_s": _num(a, "movingDuration"),
        "elevation_gain_m": _num(a, "elevationGain"),
        "elevation_loss_m": _num(a, "elevationLoss"),
        "avg_speed_kmh": kmh(a.get("averageSpeed")),
        "max_speed_kmh": kmh(a.get("maxSpeed")),
        "avg_hr": _num(a, "averageHR", 0), "max_hr": a.get("maxHR", ""),
        "avg_cadence_rpm": _num(a, "averageBikingCadenceInRevPerMinute", 0),
        "max_cadence_rpm": _num(a, "maxBikingCadenceInRevPerMinute", 0),
        "avg_power_w": _num(a, "avgPower", 0), "max_power_w": _num(a, "maxPower", 0),
        "normalized_power_w": _num(a, "normPower", 0),
        "calories": _num(a, "calories", 0),
        "avg_temp_c": avg_temp,
        "aerobic_te": _num(a, "aerobicTrainingEffect"),
        "anaerobic_te": _num(a, "anaerobicTrainingEffect"),
        "num_laps": len(lap_rows),
    }
    return lap_rows, summary


def cycle_run(g, mode):
    default = {"start_date": CYCLE_START, "last_updated": None,
               "processed_ids": [], "num_rides": 0, "num_lap_rows": 0}
    state = load_state(CYCLE_STATE, default)
    today = date.today().isoformat()

    if mode == "status":
        print(f"  cycle : {state['num_rides']} rides, {state.get('num_lap_rows', 0)} lap rows"
              f"  (last {state['last_updated'] or 'never'}){retry_note(state)}")
        return
    if mode == "auto":
        mode = "update" if state.get("last_updated") else "init"

    if mode == "init":
        from_date = state["start_date"]
        existing_summary, existing_laps, processed = [], [], set()
        log(f"cycle {mode.upper()}: {from_date} → {today}", icon="▶")
    else:
        last = state.get("last_updated") or state["start_date"]
        from_date = max((datetime.fromisoformat(last) - timedelta(days=CYCLE_OVERLAP)).date().isoformat(),
                        state["start_date"])
        from_date = min([from_date] + list(state.get("failed", {}).values()))   # reach back to retry
        existing_summary = read_csv_list(CYCLE_SUMMARY_CSV)
        existing_laps = read_csv_list(CYCLE_LAPS_CSV)
        processed = set(str(x) for x in state.get("processed_ids", []))
        log(f"cycle {mode.upper()}: {from_date} → {today}  ({len(processed)} already pulled)", icon="▶")

    rides = cycle_list(g, from_date, today)
    todo = [a for a in rides if str(a["activityId"]) not in processed]
    log(f"cycle: {len(rides)} in range, {len(todo)} new to fetch")

    new_laps, new_summary, added, failed, err = [], [], [], {}, None
    prog = Progress("cycle", len(todo))
    for a in todo:
        r_date = (a.get("startTimeLocal") or "")[:10]
        try:
            lr, sr = cycle_build(g, a)
        except Exception as e:
            failed[str(a["activityId"])] = r_date or state["start_date"]
            err = e
            prog.step(f"{r_date} failed")
            time.sleep(0.3)
            continue
        new_laps.extend(lr)
        new_summary.append(sr)
        added.append(str(a["activityId"]))
        processed.add(str(a["activityId"]))
        prog.step(f"{r_date} ({len(lr)} laps)")
        time.sleep(0.3)
    if todo:
        prog.done(f"{len(added)} rides, {len(new_laps)} laps")
    if failed:
        log(f"cycle: {len(failed)} rides failed to fetch and will be retried next run "
            f"(last error: {type(err).__name__}: {err})", icon="!")

    all_summary = existing_summary + new_summary
    all_laps = existing_laps + new_laps
    all_summary.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", ""))))
    all_laps.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", "")),
                                 int(r.get("lap_index") or 0)))
    write_csv(CYCLE_SUMMARY_CSV, CYCLE_SUMMARY_COLS, all_summary)
    write_csv(CYCLE_LAPS_CSV, CYCLE_LAP_COLS, all_laps)

    state.update({"last_updated": today,
                  "processed_ids": sorted(processed, key=lambda x: int(x)),
                  "num_rides": len(all_summary), "num_lap_rows": len(all_laps),
                  "failed": failed,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(CYCLE_STATE, state)
    log(f"cycle: +{len(added)} new → {len(all_summary)} rides / {len(all_laps)} laps "
        f"written to {os.path.basename(CYCLE_SUMMARY_CSV)}, {os.path.basename(CYCLE_LAPS_CSV)}", icon="✓")


# ===========================================================================
# RUN  — one row per run + one row per lap
# ===========================================================================
RUN_START   = "2016-01-01"
RUN_OVERLAP = 7
RUN_SUMMARY_CSV = path("run_summary.csv")
RUN_LAPS_CSV    = path("run_laps.csv")
RUN_STATE       = path("run_export_state.json")
RUN_TYPE_KEYS   = ("run",)   # running, trail_running, treadmill_running, track_running, …

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
    try:
        acts = g.get_activities_by_date(start, end, "running")
    except Exception:
        acts = g.get_activities_by_date(start, end)
    runs = [a for a in acts
            if any(k in (a.get("activityType", {}).get("typeKey", "") or "").lower()
                   for k in RUN_TYPE_KEYS)]
    runs.sort(key=lambda a: a.get("startTimeLocal", ""))
    return runs


def run_build(g, a):
    """Summary row off the activity object + one row per lap from the splits call."""
    aid  = a["activityId"]
    d    = (a.get("startTimeLocal") or "")[:10]
    name = a.get("activityName") or ""
    typ  = a["activityType"]["typeKey"]

    splits = fetch_splits(g, aid)
    laps = splits.get("lapDTOs", []) or []

    lap_rows = []
    for i, L in enumerate(laps, start=1):
        lap_rows.append({
            "activity_id": aid, "date": d, "activity_name": name, "type": typ,
            "lap_index": i,
            "distance_m": _num(L, "distance"),
            "duration_s": _num(L, "duration"),
            "moving_duration_s": _num(L, "movingDuration"),
            "pace_per_km": pace_per_km(L.get("averageSpeed")),
            "avg_speed_kmh": kmh(L.get("averageSpeed")),
            "avg_hr": L.get("averageHR", ""), "max_hr": L.get("maxHR", ""),
            "avg_power_w": _num(L, "averagePower", 0), "max_power_w": _num(L, "maxPower", 0),
            "normalized_power_w": _num(L, "normalizedPower", 0),
            "avg_cadence_spm": _num(L, "averageRunCadence", 0),
            "max_cadence_spm": _num(L, "maxRunCadence", 0),
            "stride_length_cm": _num(L, "strideLength"),
            "ground_contact_ms": _num(L, "groundContactTime"),
            "vertical_oscillation_cm": _num(L, "verticalOscillation"),
            "elevation_gain_m": _num(L, "elevationGain"),
            "calories": L.get("calories", ""),
            "intensity_type": L.get("intensityType") or "",
        })

    summary = {
        "activity_id": aid, "date": d, "activity_name": name, "type": typ,
        "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else "",
        "duration_s": _num(a, "duration"),
        "moving_duration_s": _num(a, "movingDuration"),
        "avg_pace_per_km": pace_per_km(a.get("averageSpeed")),
        "avg_speed_kmh": kmh(a.get("averageSpeed")),
        "elevation_gain_m": _num(a, "elevationGain"),
        "elevation_loss_m": _num(a, "elevationLoss"),
        "avg_hr": _num(a, "averageHR", 0), "max_hr": a.get("maxHR", ""),
        "avg_cadence_spm": _num(a, "averageRunningCadenceInStepsPerMinute", 0),
        "max_cadence_spm": _num(a, "maxRunningCadenceInStepsPerMinute", 0),
        "avg_stride_length_cm": _num(a, "avgStrideLength"),
        "avg_power_w": _num(a, "avgPower", 0), "max_power_w": _num(a, "maxPower", 0),
        "normalized_power_w": _num(a, "normPower", 0),
        "avg_ground_contact_ms": _num(a, "avgGroundContactTime"),
        "avg_vertical_oscillation_cm": _num(a, "avgVerticalOscillation"),
        "avg_vertical_ratio": _num(a, "avgVerticalRatio"),
        "calories": _num(a, "calories", 0),
        "aerobic_te": _num(a, "aerobicTrainingEffect"),
        "anaerobic_te": _num(a, "anaerobicTrainingEffect"),
        "training_effect_label": a.get("trainingEffectLabel", ""),
        "num_laps": len(lap_rows),
    }
    return lap_rows, summary


def run_run(g, mode):
    default = {"start_date": RUN_START, "last_updated": None,
               "processed_ids": [], "num_runs": 0, "num_lap_rows": 0}
    state = load_state(RUN_STATE, default)
    today = date.today().isoformat()

    if mode == "status":
        print(f"  run   : {state['num_runs']} runs, {state.get('num_lap_rows', 0)} lap rows"
              f"  (last {state['last_updated'] or 'never'}){retry_note(state)}")
        return
    if mode == "auto":
        mode = "update" if state.get("last_updated") else "init"

    if mode == "init":
        from_date = state["start_date"]
        existing_summary, existing_laps, processed = [], [], set()
        log(f"run {mode.upper()}: {from_date} → {today}", icon="▶")
    else:
        last = state.get("last_updated") or state["start_date"]
        from_date = max((datetime.fromisoformat(last) - timedelta(days=RUN_OVERLAP)).date().isoformat(),
                        state["start_date"])
        from_date = min([from_date] + list(state.get("failed", {}).values()))   # reach back to retry
        existing_summary = read_csv_list(RUN_SUMMARY_CSV)
        existing_laps = read_csv_list(RUN_LAPS_CSV)
        processed = set(str(x) for x in state.get("processed_ids", []))
        log(f"run {mode.upper()}: {from_date} → {today}  ({len(processed)} already pulled)", icon="▶")

    runs = run_list(g, from_date, today)
    todo = [a for a in runs if str(a["activityId"]) not in processed]
    log(f"run: {len(runs)} in range, {len(todo)} new to fetch")

    new_laps, new_summary, added, failed, err = [], [], [], {}, None
    prog = Progress("run", len(todo))
    for a in todo:
        r_date = (a.get("startTimeLocal") or "")[:10]
        try:
            lr, sr = run_build(g, a)
        except Exception as e:
            failed[str(a["activityId"])] = r_date or state["start_date"]
            err = e
            prog.step(f"{r_date} failed")
            time.sleep(0.3)
            continue
        new_laps.extend(lr)
        new_summary.append(sr)
        added.append(str(a["activityId"]))
        processed.add(str(a["activityId"]))
        prog.step(f"{r_date} ({len(lr)} laps)")
        time.sleep(0.3)
    if todo:
        prog.done(f"{len(added)} runs, {len(new_laps)} laps")
    if failed:
        log(f"run: {len(failed)} runs failed to fetch and will be retried next run "
            f"(last error: {type(err).__name__}: {err})", icon="!")

    all_summary = existing_summary + new_summary
    all_laps = existing_laps + new_laps
    all_summary.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", ""))))
    all_laps.sort(key=lambda r: (str(r.get("date", "")), str(r.get("activity_id", "")),
                                 int(r.get("lap_index") or 0)))
    write_csv(RUN_SUMMARY_CSV, RUN_SUMMARY_COLS, all_summary)
    write_csv(RUN_LAPS_CSV, RUN_LAP_COLS, all_laps)

    state.update({"last_updated": today,
                  "processed_ids": sorted(processed, key=lambda x: int(x)),
                  "num_runs": len(all_summary), "num_lap_rows": len(all_laps),
                  "failed": failed,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(RUN_STATE, state)
    log(f"run: +{len(added)} new → {len(all_summary)} runs / {len(all_laps)} laps "
        f"written to {os.path.basename(RUN_SUMMARY_CSV)}, {os.path.basename(RUN_LAPS_CSV)}", icon="✓")


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
    us = safe(lambda: g.get_user_summary(dstr), {}, DAILY_PAUSE, failures) or {}
    sl = (safe(lambda: g.get_sleep_data(dstr), {}, DAILY_PAUSE, failures) or {}).get("dailySleepDTO", {}) or {}
    hv = (safe(lambda: g.get_hrv_data(dstr), {}, DAILY_PAUSE, failures) or {}).get("hrvSummary", {}) or {}
    tr = safe(lambda: g.get_training_readiness(dstr), None, DAILY_PAUSE, failures)
    tr0 = (tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else {})) or {}
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
    fitness_age = round(fa["fitnessAge"], 1) if isinstance(fa.get("fitnessAge"), (int, float)) else ""
    d = datetime.fromisoformat(dstr).date()
    es = safe(lambda: g.get_endurance_score((d - timedelta(days=6)).isoformat(), dstr), {},
              DAILY_PAUSE, failures) or {}
    endurance = (es.get("enduranceScoreDTO") or {}).get("overallScore", "")
    return {"vo2max": vo2, "fitness_age": fitness_age, "endurance": endurance}


def daily_run(g, mode):
    default = {"start_date": DAILY_START, "last_updated": None, "num_days": 0}
    state = load_state(DAILY_STATE, default)
    today = date.today()

    if mode == "status":
        print(f"  daily : {state['num_days']} days  (last {state['last_updated'] or 'never'})"
              f"{retry_note(state)}")
        return
    if mode == "auto":
        mode = "update" if state.get("last_updated") else "init"

    if mode == "init":
        from_date = datetime.fromisoformat(state["start_date"]).date()
        rows, retry = {}, set()
        ndays = (today - from_date).days + 1
        log(f"daily INIT: {from_date} → {today}  ({ndays} days, ~{ndays * 4} API calls — this is slow)",
            icon="▶")
    else:
        last = state.get("last_updated") or state["start_date"]
        from_date = max(datetime.fromisoformat(last).date() - timedelta(days=DAILY_OVERLAP),
                        datetime.fromisoformat(state["start_date"]).date())
        rows = read_csv_by_key(DAILY_CSV, "date")
        # days an earlier run couldn't fetch, however old — otherwise they'd stay blank for good
        retry = {datetime.fromisoformat(s).date() for s in state.get("failed_days", [])}
        ndays = (today - from_date).days + 1
        log(f"daily UPDATE: {from_date} → {today}  ({ndays} days"
            + (f" + {len(retry)} to retry" if retry else "") + ")", icon="▶")

    days = sorted({from_date + timedelta(days=i) for i in range(ndays)} | retry)
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
            weekly_failed = bool(wf)        # carried forward, so the whole week is suspect
        failures = []
        rows[dstr] = daily_day_row(g, dstr, weekly, failures)
        if failures or weekly_failed:
            failed_days.append(dstr)
        prog.step(dstr)
    prog.done(f"{len(days)} days fetched")
    if failed_days:
        log(f"daily: {len(failed_days)} days hit rate limits or network errors — "
            f"they'll be re-fetched on the next update", icon="!")

    ordered = [rows[k] for k in sorted(rows)]
    write_csv(DAILY_CSV, DAILY_COLS, ordered)
    state.update({"last_updated": today.isoformat(), "num_days": len(rows),
                  "failed_days": failed_days,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(DAILY_STATE, state)
    log(f"daily: +{len(days)} processed → {len(rows)} days written to {os.path.basename(DAILY_CSV)}",
        icon="✓")


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

PR_LABELS = {
    1: ("Fastest 1 km", "time"), 2: ("Fastest 1 mile", "time"),
    3: ("Fastest 5 km", "time"), 4: ("Fastest 10 km", "time"),
    5: ("Fastest half marathon", "time"), 6: ("Fastest marathon", "time"),
    7: ("Longest run", "distance"), 8: ("Longest ride", "distance"),
    12: ("Most steps in a day", "count"), 13: ("Most steps in a week", "count"),
    14: ("Most steps in a month", "count"), 16: ("Most floors in a day", "count"),
}


def body_pull_weight(g):
    """Every weigh-in, a year per call. A year that fails to fetch keeps its rows from the
    last run rather than vanishing from the CSV."""
    rows, failed = {}, []
    start = datetime.fromisoformat(BODY_START).date()
    today = date.today()
    nchunks = today.year - start.year + 1
    prog = Progress("weigh-ins", nchunks)
    d = start
    while d <= today:
        chunk_end = min(d.replace(year=d.year + 1) - timedelta(days=1), today)
        try:
            wi = g.get_weigh_ins(d.isoformat(), chunk_end.isoformat()) or {}
        except Exception:
            failed.append((d.isoformat(), chunk_end.isoformat()))
            wi = {}
        time.sleep(0.2)
        for day in wi.get("dailyWeightSummaries", []) or []:
            for m in day.get("allWeightMetrics", []) or []:
                rows[m.get("samplePk")] = {
                    "date": (m.get("calendarDate") or "")[:10],
                    "weight_kg": round(m["weight"] / 1000, 2) if m.get("weight") else "",
                    "bmi": round(m["bmi"], 1) if isinstance(m.get("bmi"), (int, float)) else "",
                    "body_fat_pct": m.get("bodyFat", ""),
                    "body_water_pct": m.get("bodyWater", ""),
                    "muscle_mass_kg": round(m["muscleMass"] / 1000, 2) if m.get("muscleMass") else "",
                    "bone_mass_kg": round(m["boneMass"] / 1000, 2) if m.get("boneMass") else "",
                    "visceral_fat": m.get("visceralFat", ""),
                    "metabolic_age": m.get("metabolicAge", ""),
                    "physique_rating": m.get("physiqueRating", ""),
                    "source": m.get("sourceType", ""),
                }
        prog.step(f"{d.year}: {len(rows)} so far")
        d = chunk_end + timedelta(days=1)
    prog.done(f"{len(rows)} weigh-ins")
    kept = [r for r in read_csv_list(WEIGHT_CSV)
            if any(lo <= r["date"] <= hi for lo, hi in failed)]
    if failed:
        log(f"body: weigh-ins for {', '.join(lo[:4] for lo, _ in failed)} failed to fetch — "
            f"kept their {len(kept)} rows from the last run", icon="!")
    ordered = sorted(list(rows.values()) + kept, key=lambda r: r["date"])
    write_csv(WEIGHT_CSV, WEIGHT_COLS, ordered)
    return len(ordered)


def body_pull_prs(g):
    try:
        prs = g.get_personal_record() or []
    except Exception as e:
        log(f"body: personal records failed to fetch ({e}) — kept the last snapshot", icon="!")
        return len(read_csv_list(PR_CSV))
    out = []
    for p in prs:
        tid = p.get("typeId")
        label, kind = PR_LABELS.get(tid, (f"type_{tid}", "raw"))
        val = p.get("value")
        if kind == "time":
            pretty, units = fmt_time(val), "h:mm:ss"
        elif kind == "distance":
            pretty, units = (round(val / 1000, 2) if isinstance(val, (int, float)) else ""), "km"
        elif kind == "count":
            pretty, units = (int(val) if isinstance(val, (int, float)) else ""), "count"
        else:
            pretty, units = "", ""
        rec_date = (p.get("prStartTimeLocalFormatted")
                    or p.get("activityStartDateTimeLocalFormatted") or "")
        out.append({"type_id": tid, "label": label, "activity_type": p.get("activityType", ""),
                    "value_raw": val, "value_pretty": pretty, "units": units,
                    "record_date": rec_date[:10] if rec_date else ""})
    out.sort(key=lambda r: (r["type_id"] if isinstance(r["type_id"], int) else 999))
    write_csv(PR_CSV, PR_COLS, out)
    return len(out)


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
        def cnt(p):
            return (sum(1 for _ in open(p)) - 1) if os.path.exists(p) else 0
        print(f"  body  : {cnt(WEIGHT_CSV)} weigh-ins, {cnt(PR_CSV)} PRs, "
              f"{cnt(RACE_CSV)} race predictions")
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
RAW_START    = "2015-01-01"
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


def _typekey(a):
    return ((a.get("activityType") or {}).get("typeKey") or "").lower()


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
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, p)


def raw_fetch(fn, tries=3):
    """Call fn with polite pacing. Backs off and retries on rate limits only;
    other failures return their message so the caller can record them."""
    from garminconnect import GarminConnectTooManyRequestsError
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
        v, err = raw_fetch(lambda: fetch(g, aid, a))
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
    d0 = datetime.fromisoformat(start).date()
    d1 = datetime.fromisoformat(end).date()
    acts, seen, out = [], set(), []
    for y in range(d0.year, d1.year + 1):
        lo = max(d0, date(y, 1, 1)).isoformat()
        hi = min(d1, date(y, 12, 31)).isoformat()
        chunk, err = raw_fetch(lambda: g.get_activities_by_date(lo, hi))
        if err:
            log(f"raw: listing {y} failed: {err}", icon="✗")
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
    if not os.path.isdir(RAW_DIR):
        return set()
    ids = set()
    for f in os.listdir(RAW_DIR):
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
    if not os.path.isdir(RAW_DIR):
        return bad
    for f in os.listdir(RAW_DIR):
        if not f.endswith(".json.gz") or f.endswith(".partial.json.gz"):
            continue
        p = os.path.join(RAW_DIR, f)
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception as e:
            log(f"raw: {f} unreadable ({type(e).__name__}) — will re-fetch", icon="!")
            try:
                bad.add(int(f[:-len(".json.gz")]))
            except ValueError:
                pass
            continue
        if raw_retryable(rec) and rec.get("activity_id"):
            bad.add(rec["activity_id"])
    return bad


def raw_partial_list_objects():
    """List objects of the captures parked as .partial. `update` only lists recent activity,
    so without these an older incomplete capture would wait for the next `init`."""
    out = []
    if not os.path.isdir(RAW_DIR):
        return out
    for f in os.listdir(RAW_DIR):
        if not f.endswith(".partial.json.gz"):
            continue
        try:
            with gzip.open(os.path.join(RAW_DIR, f), "rt", encoding="utf-8") as fh:
                out.append(json.load(fh)["list_object"])
        except Exception as e:
            log(f"raw: {f} unreadable ({type(e).__name__}) — `raw init` will re-fetch it", icon="!")
    return out


def raw_disk_bytes():
    if not os.path.isdir(RAW_DIR):
        return 0
    return sum(os.path.getsize(os.path.join(RAW_DIR, f)) for f in os.listdir(RAW_DIR)
               if f.endswith(".json.gz"))


def raw_run(g, mode):
    default = {"start_date": RAW_START, "last_updated": None, "schema": RAW_SCHEMA,
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
    from_date = state["start_date"] if mode == "init" else (
        max((datetime.fromisoformat(state.get("last_updated") or state["start_date"])
             - timedelta(days=RAW_OVERLAP)).date().isoformat(), state["start_date"]))
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

    state.update({"last_updated": today, "schema": RAW_SCHEMA,
                  "num_activities": len(have),
                  "num_with_errors": n_err,
                  "bytes_on_disk": raw_disk_bytes(),
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
    save_state(RAW_STATE, state)
    log(f"raw: +{len(todo)} new → {len(have)} activities cached, "
        f"{raw_disk_bytes() / 1e6:.0f} MB in {os.path.basename(RAW_DIR)}/", icon="✓")


# ===========================================================================
RUNNERS = {"swim": swim_run, "cycle": cycle_run, "run": run_run,
           "daily": daily_run, "body": body_run, "raw": raw_run}


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
    main(sys.argv[1:])
