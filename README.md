# Training data — Garmin export

This directory holds one script, `garmin_export.py`, that exports my Garmin Connect data to
CSV and remembers what it has already pulled, so it can be updated incrementally. It covers
these **domains** behind a single login:

| Domain | Exports | Cadence |
|--------|---------|---------|
| `swim`  | Swim activities + per-length breakdown | per activity |
| `cycle` | Rides (outdoor, indoor, virtual) + per-lap breakdown | per activity |
| `run`   | Runs (road, trail, treadmill) + per-lap breakdown | per activity |
| `daily` | Daily recovery + fitness-trend metrics | per day |
| `body`  | Weigh-ins, personal records, race predictions | weigh-ins over time; PRs/races are snapshots |
| `raw`   | Every API endpoint for every activity, as gzipped JSON (opt-in, not part of `all`) | per activity |

The data has three different shapes (per-activity, per-day, snapshot), so it stays in
separate CSVs — but all of them join on `date`.

> **For any AI/agent reading this:** everything you need is below. The data is read-only
> fitness data from Garmin Connect. The script is safe to re-run; it's idempotent and only
> fetches what it hasn't seen before. All output goes into `./data`.

---

## Layout

```
garmin-training-export/
├── garmin_export.py          the exporter
├── cycle_probe.py            one-off probe of Garmin's structured-workout data (not part of the export)
├── pyproject.toml            dependencies (garminconnect) and Python version
├── uv.lock                   exact pinned versions of every library — kept current by Dependabot
├── .github/dependabot.yml    weekly dependency-update PRs
├── README.md                 this file
└── data/                     all generated output (created automatically; git-ignored, never committed)
    ├── swim_summary.csv        one row per swim activity
    ├── swim_lengths.csv        one row per pool length (set/stroke/pace/SWOLF breakdown)
    ├── cycle_summary.csv       one row per ride
    ├── cycle_laps.csv          one row per ride lap
    ├── run_summary.csv         one row per run
    ├── run_laps.csv            one row per run lap
    ├── daily_metrics.csv       one row per calendar day (sleep/HRV/Body Battery/readiness/VO2max…)
    ├── body_weight.csv         one row per weigh-in (weight, BMI, body fat…)
    ├── personal_records.csv    current personal records (snapshot)
    ├── race_predictions.csv    current predicted race times (snapshot)
    ├── raw/<activity_id>.json.gz   raw API responses per activity (`raw` domain only)
    ├── raw_account.json.gz     account-level context: zones, FTP, devices, gear (`raw` only)
    └── *_state.json            per-domain manifests (what's pulled, last pull date, and
                                anything that failed and will be retried)
```

The script writes to `./data` **next to the script itself**, regardless of the directory you
run it from. The folder is created on first run. Override with the `SWIM_OUTPUT_DIR` env var.

---

## How to run

Requires [`uv`](https://docs.astral.sh/uv/). Run commands **from this directory**: on first
use uv creates `.venv/` with the exact library versions in `uv.lock` — no manual venv/pip.
From elsewhere, point uv at the project: `uv run --project ~/garmin-training-export ~/garmin-training-export/garmin_export.py …`.

The script takes a **domain** (`swim` | `cycle` | `run` | `daily` | `body` | `all`, or `raw`)
and a **mode** (`init` | `update` | `status`). Arguments are order-independent; defaults are
`all` + `auto`. `all` means every domain except `raw`. Add `-q` to hide routine log lines.

```bash
uv run garmin_export.py                  # all domains, auto (init first run, else update)
uv run garmin_export.py all update       # update everything in one login
uv run garmin_export.py swim update      # one domain only (likewise cycle, run)
uv run garmin_export.py daily init       # full daily rebuild (slow — see below)
uv run garmin_export.py body             # refresh weigh-ins + PRs + race predictions
uv run garmin_export.py raw init         # cache every endpoint for every activity (~2.5 h)
uv run garmin_export.py raw update       # cache anything new since the last raw run
uv run garmin_export.py status           # report all domains; fetches nothing
```

- `init` = full rebuild from that domain's START_DATE
- `update` = incremental (uses a small date overlap to catch late-synced data)
- `auto` = init on first run, else update
- `status` skips login entirely (just reads the CSVs/state)

**Failures are retried, not lost.** If Garmin rate-limits a request or the connection drops,
that activity (or day) is left out and listed in the domain's `*_state.json`; the next
`update` fetches it again, however old it is. `status` shows how many are waiting. A permanent
answer (e.g. a 404 because there's no data) is accepted as final. In `body`, a failed call
keeps the previous run's rows instead of overwriting them.

**Normal workflow:** after new swims/days, run `uv run garmin_export.py all update`. The
`body` domain always does a full refresh (the data is small).

> ⚠️ **`daily init` is slow** — it makes ~4 calls/day back to 2022-12-01 (~5000+ requests,
> ~20–30 min). Slow-changing metrics (VO2max, endurance score, fitness age) are sampled once
> per ISO week and carried forward to limit the call count. Incremental `update` runs are quick.

---

## Authentication

Uses saved Garmin OAuth tokens (no password in the script). Token store defaults to
`~/.garminconnect` (override with the `GARMINTOKENS` env var). Tokens last ~6 months.
If login fails, the script exits with a message; re-authenticate once with:

```bash
garmin-mcp-auth          # interactive: prompts for Garmin email + password
```

---

## `swim_summary.csv` columns (one row per swim)

| Column | Meaning |
|--------|---------|
| `activity_id` | Garmin activity ID (unique key). |
| `date` | Local start date `YYYY-MM-DD`. |
| `activity_name` | Garmin's name for the activity. |
| `type` | `lap_swimming` or `open_water_swimming`. |
| `pool_length_m` | Pool length in metres (blank for open water). |
| `total_distance_m` | Total distance swum. |
| `active_lengths` | Number of pool lengths actually swum. |
| `total_duration_s` | Elapsed time **including rest** (seconds). |
| `moving_duration_s` | Time actually moving (seconds). |
| `rest_time_s` | Time spent resting at the wall (seconds). |
| `avg_pace_moving_per_100m_s` | Pace per 100 m using moving time. **Use this for pace.** |
| `avg_pace_elapsed_per_100m_s` | Pace per 100 m using elapsed time (includes rest — slower; for reference only). |
| `avg_hr`, `max_hr` | Heart rate (bpm). |
| `calories` | kcal. |
| `avg_swolf_clean` | Mean SWOLF over real swum lengths, excluding glitches (lower = more efficient). |
| `num_sets` | Count of sets (groups of lengths separated by rest — see below). |
| `stroke_mix` | Tally of strokes, e.g. `FREESTYLE:24; BACKSTROKE:6; DRILL:4`. |

## `swim_lengths.csv` columns (one row per length)

| Column | Meaning |
|--------|---------|
| `activity_id`, `date`, `activity_name`, `type` | Link back to the swim. |
| `pool_length_m` | Pool length in metres. |
| `set_index` | Which set this length belongs to (1-based). |
| `length_in_set` | Position of this length within its set. |
| `global_length_n` | Sequential length number across the whole swim. |
| `distance_m` | Length distance (= pool length). |
| `duration_s` | Time for this length. |
| `pace_per_100m_s` | Pace per 100 m for this length. |
| `stroke` | `FREESTYLE`, `BREASTSTROKE`, `BACKSTROKE`, `BUTTERFLY`, `DRILL`, `MIXED`. |
| `swolf` | SWOLF for this length (strokes + seconds; lower = more efficient). |
| `strokes` | Stroke count for this length. |
| `avg_hr`, `max_hr` | Heart rate for this length (bpm). Blank where the watch logged no HR. |
| `rest_before_set_s` | Rest taken immediately before this set started (0 except on a set's first length). |
| `rest_avg_hr`, `rest_max_hr` | Heart rate **during** that rest — recovery HR at the wall. Only on a set's first length, alongside `rest_before_set_s`. Where a rest spans several logged pauses the average is weighted by their durations. |
| `likely_glitch` | `1` = sensor miscount (impossible sub-60 s/100 m split); filter these out for clean analysis. |

## `cycle_summary.csv` columns (one row per ride)

Includes every activity Garmin files under cycling: `cycling`, `road_biking`,
`indoor_cycling`, `virtual_ride`, `gravel_cycling`, `mountain_biking`, `enduro_mtb`,
`e_bike_*`, ….

| Column | Meaning |
|--------|---------|
| `activity_id`, `date`, `activity_name`, `type` | Garmin activity ID (unique key), local start date, name, activity type. |
| `distance_km` | Total distance (km). |
| `duration_s`, `moving_duration_s` | Elapsed time and time actually moving (seconds). |
| `elevation_gain_m`, `elevation_loss_m` | Total climb / descent (m). |
| `avg_speed_kmh`, `max_speed_kmh` | Speed (km/h). |
| `avg_hr`, `max_hr` | Heart rate (bpm). |
| `avg_cadence_rpm`, `max_cadence_rpm` | Pedalling cadence (rpm); blank without a cadence sensor. |
| `avg_power_w`, `max_power_w`, `normalized_power_w` | Power (W); blank without a power meter / smart trainer. |
| `calories` | kcal. |
| `avg_temp_c` | Average temperature from the device sensor (°C), worked out from the laps — reads high when worn on the wrist. Blank when the device recorded no temperature. |
| `aerobic_te`, `anaerobic_te` | Garmin Training Effect, 0–5. |
| `num_laps` | Number of rows this ride has in `cycle_laps.csv`. |

## `cycle_laps.csv` columns (one row per lap)

| Column | Meaning |
|--------|---------|
| `activity_id`, `date`, `activity_name`, `type` | Link back to the ride. |
| `lap_index` | Lap number within the ride (1-based). |
| `distance_m`, `duration_s`, `moving_duration_s` | Lap distance (m), elapsed and moving time (s). |
| `avg_speed_kmh`, `max_speed_kmh`, `avg_hr`, `max_hr` | Speed (km/h) and heart rate (bpm) for the lap. |
| `avg_power_w`, `max_power_w`, `normalized_power_w` | Lap power (W). |
| `avg_cadence_rpm`, `max_cadence_rpm` | Lap cadence (rpm). |
| `elevation_gain_m`, `calories` | Climb (m) and kcal for the lap. |
| `intensity_type` | Lap role from a structured workout: `WARMUP`, `ACTIVE`, `REST`, `COOLDOWN`, …; blank for plain laps. |

## `run_summary.csv` columns (one row per run)

Includes `running`, `trail_running`, `treadmill_running`, `street_running`, ….

| Column | Meaning |
|--------|---------|
| `activity_id`, `date`, `activity_name`, `type` | Garmin activity ID (unique key), local start date, name, activity type. |
| `distance_km` | Total distance (km). |
| `duration_s`, `moving_duration_s` | Elapsed time and time actually moving (seconds). |
| `avg_pace_per_km` | Average pace as `m:ss` per km (from Garmin's average speed). |
| `avg_speed_kmh` | Average speed (km/h). |
| `elevation_gain_m`, `elevation_loss_m` | Total climb / descent (m). |
| `avg_hr`, `max_hr` | Heart rate (bpm). |
| `avg_cadence_spm`, `max_cadence_spm` | Cadence (steps per minute). |
| `avg_stride_length_cm` | Average stride length (cm). |
| `avg_power_w`, `max_power_w`, `normalized_power_w` | Running power (W); blank on devices that don't measure it. |
| `avg_ground_contact_ms` | Ground contact time (ms). Running-dynamics fields are blank without a supported sensor. |
| `avg_vertical_oscillation_cm`, `avg_vertical_ratio` | Vertical bounce (cm) and bounce as % of stride length. |
| `calories` | kcal. |
| `aerobic_te`, `anaerobic_te` | Garmin Training Effect, 0–5. |
| `training_effect_label` | Garmin's primary benefit: `RECOVERY`, `AEROBIC_BASE`, `TEMPO`, `LACTATE_THRESHOLD`, `VO2MAX`, …. |
| `num_laps` | Number of rows this run has in `run_laps.csv`. |

## `run_laps.csv` columns (one row per lap)

| Column | Meaning |
|--------|---------|
| `activity_id`, `date`, `activity_name`, `type` | Link back to the run. |
| `lap_index` | Lap number within the run (1-based). |
| `distance_m`, `duration_s`, `moving_duration_s` | Lap distance (m), elapsed and moving time (s). |
| `pace_per_km`, `avg_speed_kmh` | Lap pace (`m:ss` per km) and speed (km/h). |
| `avg_hr`, `max_hr` | Heart rate for the lap (bpm). |
| `avg_power_w`, `max_power_w`, `normalized_power_w` | Lap power (W). |
| `avg_cadence_spm`, `max_cadence_spm`, `stride_length_cm` | Cadence (spm) and stride length (cm). |
| `ground_contact_ms`, `vertical_oscillation_cm` | Running dynamics for the lap. |
| `elevation_gain_m`, `calories` | Climb (m) and kcal for the lap. |
| `intensity_type` | Lap role from a structured workout: `WARMUP`, `ACTIVE`, `INTERVAL`, `RECOVERY`, `REST`, `COOLDOWN`; blank for plain laps. |

## `daily_metrics.csv` columns (one row per calendar day)

Recovery + fitness-trend metrics, keyed by `date` (joins to the swim CSVs).

| Column | Meaning |
|--------|---------|
| `date` | Calendar day `YYYY-MM-DD`. |
| `sleep_score` | Garmin sleep score 0–100. |
| `sleep_hours` | Total sleep (hours). |
| `deep_min`, `light_min`, `rem_min`, `awake_min` | Sleep stage minutes. |
| `resting_hr` | Resting heart rate (bpm). |
| `hrv_last_night` | Overnight HRV average (ms). |
| `hrv_weekly_avg` | 7-day HRV average (ms). |
| `hrv_status` | `BALANCED`, `UNBALANCED`, `LOW`, etc. |
| `body_battery_high`, `body_battery_low` | Day's Body Battery range (0–100). |
| `body_battery_charged`, `body_battery_drained` | Body Battery gained / spent that day. |
| `stress_avg`, `stress_max` | Daily stress (0–100). |
| `spo2_avg` | Average blood-oxygen %. |
| `respiration_avg` | Average waking respiration (breaths/min). |
| `steps`, `floors_ascended` | Daily totals. |
| `intensity_moderate_min`, `intensity_vigorous_min` | Intensity minutes. |
| `calories_total`, `calories_active` | kcal. |
| `training_readiness_score` | Garmin Training Readiness 0–100. |
| `training_readiness_level` | `LOW` / `MODERATE` / `HIGH` / etc. |
| `vo2max` | VO2 max (sampled weekly, carried forward). |
| `endurance_score` | Garmin Endurance Score (~0–10000; weekly, carried forward). |
| `fitness_age` | Estimated fitness age (weekly, carried forward). |

> The current day's row is partial until the day ends; the 3-day overlap on `update`
> finalises recent days. Blank cells mean Garmin had no reading that day.

## `body_weight.csv` columns (one row per weigh-in)

| Column | Meaning |
|--------|---------|
| `date` | Weigh-in date. |
| `weight_kg`, `bmi` | Weight (kg) and BMI. |
| `body_fat_pct`, `body_water_pct` | Body composition %. |
| `muscle_mass_kg`, `bone_mass_kg` | Mass (kg). |
| `visceral_fat`, `metabolic_age`, `physique_rating` | Smart-scale extras (blank if not measured). |
| `source` | Measurement source. |

## `personal_records.csv` columns (snapshot)

| Column | Meaning |
|--------|---------|
| `type_id` | Garmin PR type ID. |
| `label` | Friendly name for known IDs; `type_<id>` for unmapped ones. |
| `activity_type` | Sport the record belongs to. |
| `value_raw` | Raw value as Garmin stores it (seconds for time PRs, metres for distance, count for steps). |
| `value_pretty`, `units` | Formatted value for mapped types (e.g. `20:00` / `h:mm:ss`); blank for unmapped. |
| `record_date` | When the record was set. |

> **Swim PR caveat:** type IDs 17–25 (`lap_swimming`) are pool-swim records, but Garmin's
> exact mapping isn't reliably documented, so they're left as `type_<id>` with the raw value
> rather than guessed. Use `value_raw` for those.

## `race_predictions.csv` columns (snapshot)

| Column | Meaning |
|--------|---------|
| `pulled_date` | When this snapshot was taken. |
| `calendar_date` | Date Garmin computed the prediction for. |
| `distance` | `5K`, `10K`, `Half Marathon`, `Marathon`. |
| `time_seconds`, `time_pretty` | Predicted time. |

## `raw` domain (gzipped JSON, opt-in)

The CSVs keep a handful of fields each. `raw` instead stores Garmin's full responses for
**every** activity in the account (all types, not just swim/cycle/run), so new views can be
built offline. It's excluded from `all` because a full `raw init` takes ~2.5 h; it's
resumable and safe to re-run.

- `raw/<activity_id>.json.gz` — one file per activity: `list_object` (the activity as listed),
  `endpoints` (`activity`, `splits`, `typed_splits`, `split_summaries`, `hr_zones`,
  `power_zones`, `weather`, `gear`, `exercise_sets`, `details` — the per-second time series and
  GPS track), plus `skipped` (endpoints that don't apply) and `errors`.
- `raw/<activity_id>.partial.json.gz` — a capture that hit a network or rate-limit failure.
  The next `raw update` or `raw init` fetches it again.
- `raw_account.json.gz` — user profile and settings, HR and power zones, lactate threshold,
  cycling FTP, devices, gear, activity types, max metrics. Refreshed on every `raw` run.

---

## How sets are derived

The Garmin watch logs idle/rest time at the wall as zero-distance "lengths". The script
treats every run of consecutive swum lengths between rests as one **set** (`set_index`),
and records the rest before each set in `rest_before_set_s`. This reconstructs the
warm-up / drills / main-set / cool-down structure of a session. To see one swim's
structure: filter `swim_lengths.csv` by `activity_id`, then read in `global_length_n`
order, grouped by `set_index`.

## Data caveats (important for analysis)

- **Glitches:** rows with `likely_glitch = 1` are sensor miscounts (e.g. a 50 m logged in
  15 s). Exclude them when computing pace/SWOLF. They're ~1% of rows.
- **Drill / kick lengths:** when the watch can't detect strokes (drills, kick sets) it
  divides the block's total time evenly across its lengths, so those per-length times are
  block **averages**, not exact splits, and have no SWOLF. `stroke` shows `DRILL`.
- **Open-water swims:** no pool lengths, so they appear in `swim_summary.csv` but contribute
  few/no rows to `swim_lengths.csv`.
- **Two pace definitions:** prefer `avg_pace_moving_per_100m_s`. The elapsed version is
  inflated by rest time.
- **HR lags effort:** `rest_max_hr` is often *higher* than the `max_hr` of the length it
  follows — heart rate peaks after a hard effort ends, not during it. When judging how hard
  a rep was, read the rep's HR together with the rest that follows it.

## Dependency updates

`pyproject.toml` lists the one direct dependency (`garminconnect`); `uv.lock` pins it and
everything it pulls in. Dependabot checks weekly and opens pull requests: one for
`garminconnect`, one bundling the supporting libraries (`requests`, `urllib3`, `certifi`, …).
Security fixes come as separate PRs as soon as an advisory is published.

To try a Dependabot PR before merging:

```bash
gh pr checkout <number>
uv sync                                   # install the PR's versions
uv run garmin_export.py status            # no login
uv run garmin_export.py all update        # the real check against Garmin
```

To update by hand instead: `uv lock --upgrade` (or `--upgrade-package garminconnect`), then
`uv sync`.

## Config (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `SWIM_OUTPUT_DIR` | `./data` (next to the script) | Where the CSVs + state files are written. |
| `GARMINTOKENS` | `~/.garminconnect` | Garmin OAuth token store. |

Each domain has a `*_START` constant in its section of `garmin_export.py`
(`SWIM_START`, `CYCLE_START`, `RUN_START`, `DAILY_START`, `BODY_START`, `RAW_START`)
controlling how far back a fresh `init` goes — swim/cycle/run default to `2016-01-01`, daily
to `2022-12-01`, body (weigh-ins) to `2018-01-01`, raw to `2015-01-01`. On `update`, the
stored last-pull date in `data/*_state.json` governs instead.
