"""daily and body runners, plus the shared retry helpers (is_transient, safe, ...) they use."""
import contextlib
import io
import os
import types
import unittest
from datetime import date, timedelta

import garmin_export as ge
from tests.stubs import StubGarmin, rows

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())
SAT, SUN, MON = ((MONDAY + timedelta(days=k)).isoformat() for k in (-2, -1, 0))
THIS_WEEK = [(MONDAY + timedelta(days=k)).isoformat() for k in range(TODAY.weekday() + 1)]
LAST_YEAR, THIS_YEAR = TODAY.year - 1, TODAY.year


def day(offset):
    """The ISO date `offset` days from today (negative = past)."""
    return (TODAY + timedelta(days=offset)).isoformat()


def before(dstr, days):
    return (date.fromisoformat(dstr) - timedelta(days=days)).isoformat()


def n(dstr):
    """A per-day number, so each day's synthetic answer differs."""
    return int(dstr[8:])


def called(g, name):
    """The argument tuples a stub endpoint was called with, in order."""
    return [c[1:] for c in g.calls if c[0] == name]


def rate_limited():
    return ge.GarminConnectTooManyRequestsError("429")


def not_found():
    return ge.GarminConnectNotFoundError("404")


def failing_on(days, exc, ok):
    """A single-date endpoint that raises `exc` for `days` and answers `ok` otherwise."""
    def answer(dstr):
        if dstr in days:
            raise exc
        return ok(dstr) if callable(ok) else ok
    return answer


class Quiet(unittest.TestCase):
    """Runners log warnings to stderr; keep them out of the test output."""

    def setUp(self):
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))


def status_line(runner):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runner(None, "status")
    return out.getvalue()


# ===========================================================================
# DAILY
# ===========================================================================
def user_summary(dstr):
    return {"restingHeartRate": 40 + n(dstr), "bodyBatteryHighestValue": 90,
            "bodyBatteryLowestValue": 20, "bodyBatteryChargedValue": 60,
            "bodyBatteryDrainedValue": 55, "averageStressLevel": 30, "maxStressLevel": 88,
            "averageSpo2": 96, "avgWakingRespirationValue": 14.0, "totalSteps": 1000 * n(dstr),
            "floorsAscended": 10.26, "moderateIntensityMinutes": 20,
            "vigorousIntensityMinutes": 5, "totalKilocalories": 2200.0, "activeKilocalories": 400.0}


SLEEP = {"dailySleepDTO": {"sleepTimeSeconds": 27900, "deepSleepSeconds": 5400,
                           "lightSleepSeconds": 14400, "remSleepSeconds": 8100,
                           "awakeSleepSeconds": 1200, "sleepScores": {"overall": {"value": 80}}}}
HRV = {"hrvSummary": {"lastNightAvg": 50, "weeklyAvg": 52, "status": "BALANCED"}}
READINESS = [{"score": 61, "level": "MODERATE"}, {"score": 5, "level": "LOW"}]
FITNESS_AGE = {"fitnessAge": 33.04}


def training_status(dstr):
    return {"mostRecentVO2Max": {"generic": {"vo2MaxValue": n(dstr) + 0.5}}}


def endurance(start, end):
    return {"enduranceScoreDTO": {"overallScore": 5000 + n(end)}}


def vo2_from(dstr):
    """The vo2max cell a row carries when its week was sampled on `dstr`."""
    return f"{n(dstr)}.5"


def endurance_from(dstr):
    return str(5000 + n(dstr))


def daily_garmin(**over):
    return StubGarmin(**{"get_user_summary": user_summary, "get_sleep_data": SLEEP,
                         "get_hrv_data": HRV, "get_training_readiness": READINESS,
                         "get_training_status": training_status,
                         "get_fitnessage_data": FITNESS_AGE, "get_endurance_score": endurance,
                         **over})


class DailyCase(Quiet):
    def setUp(self):
        super().setUp()
        for p in (ge.DAILY_CSV, ge.DAILY_STATE):
            if os.path.exists(p):
                os.remove(p)
        self.addCleanup(setattr, ge, "DAILY_START", ge.DAILY_START)

    def state(self):
        return ge.load_state(ge.DAILY_STATE, {})

    def by_date(self):
        return {r["date"]: r for r in rows(ge.DAILY_CSV)}

    def seed(self, csv_rows, **state):
        ge.write_csv(ge.DAILY_CSV, ge.DAILY_COLS, csv_rows)
        ge.save_state(ge.DAILY_STATE, {"num_days": len(csv_rows), "failed_days": [], **state})


class DailyInit(DailyCase):
    def test_init_writes_one_row_per_day_from_the_start_date_through_today(self):
        ge.DAILY_START = SAT
        ge.daily_run(daily_garmin(), "init")

        got = rows(ge.DAILY_CSV)
        self.assertEqual([r["date"] for r in got], [SAT, SUN] + THIS_WEEK)
        expected = {
            "date": SAT,
            "sleep_score": "80", "sleep_hours": "7.75", "deep_min": "90", "light_min": "240",
            "rem_min": "135", "awake_min": "20",
            "resting_hr": str(40 + n(SAT)), "hrv_last_night": "50", "hrv_weekly_avg": "52",
            "hrv_status": "BALANCED",
            "body_battery_high": "90", "body_battery_low": "20", "body_battery_charged": "60",
            "body_battery_drained": "55",
            "stress_avg": "30", "stress_max": "88", "spo2_avg": "96", "respiration_avg": "14.0",
            "steps": str(1000 * n(SAT)), "floors_ascended": "10.3",
            "intensity_moderate_min": "20", "intensity_vigorous_min": "5",
            "calories_total": "2200.0", "calories_active": "400.0",
            "training_readiness_score": "61", "training_readiness_level": "MODERATE",
            "vo2max": vo2_from(SAT), "endurance_score": endurance_from(SAT), "fitness_age": "33.0",
        }
        self.assertEqual(list(got[0]), list(expected))      # column order
        self.assertEqual(got[0], expected)
        state = self.state()
        self.assertEqual({k: state[k] for k in ("start_date", "last_updated", "num_days",
                                                "failed_days")},
                         {"start_date": SAT, "last_updated": TODAY.isoformat(),
                          "num_days": 2 + len(THIS_WEEK), "failed_days": []})
        self.assertIn("generated_at", state)

    def test_weekly_metrics_are_sampled_once_per_iso_week_and_carried_forward(self):
        ge.DAILY_START = SAT
        g = daily_garmin()
        ge.daily_run(g, "init")

        self.assertEqual(called(g, "get_training_status"), [(SAT,), (MON,)])
        self.assertEqual(called(g, "get_fitnessage_data"), [(SAT,), (MON,)])
        self.assertEqual(called(g, "get_endurance_score"),
                         [(before(SAT, 6), SAT), (before(MON, 6), MON)])
        self.assertEqual(called(g, "get_user_summary"), [(d,) for d in [SAT, SUN] + THIS_WEEK])
        got = self.by_date()
        for d in (SAT, SUN):
            self.assertEqual((got[d]["vo2max"], got[d]["endurance_score"]),
                             (vo2_from(SAT), endurance_from(SAT)), d)
        for d in THIS_WEEK:
            self.assertEqual((got[d]["vo2max"], got[d]["endurance_score"]),
                             (vo2_from(MON), endurance_from(MON)), d)

    def test_a_failed_weekly_sample_marks_every_day_of_that_week_failed(self):
        ge.DAILY_START = SAT
        g = daily_garmin(get_training_status=failing_on({SAT}, rate_limited(), training_status))
        ge.daily_run(g, "init")

        self.assertEqual(self.state()["failed_days"], [SAT, SUN])
        got = self.by_date()
        self.assertEqual(got[SUN]["vo2max"], "")
        self.assertEqual(got[SUN]["endurance_score"], endurance_from(SAT))  # other samples landed
        self.assertEqual(got[SUN]["resting_hr"], str(40 + n(SUN)))          # the day itself did too
        self.assertEqual(got[MON]["vo2max"], vo2_from(MON))

    def test_a_transient_failure_on_one_day_marks_only_that_day(self):
        ge.DAILY_START = SAT
        ge.daily_run(daily_garmin(get_sleep_data=failing_on({SUN}, rate_limited(), SLEEP)), "init")

        self.assertEqual(self.state()["failed_days"], [SUN])
        got = self.by_date()
        self.assertEqual((got[SUN]["sleep_score"], got[SUN]["sleep_hours"]), ("", ""))
        self.assertEqual(got[SUN]["resting_hr"], str(40 + n(SUN)))
        self.assertEqual(got[SAT]["sleep_score"], "80")

    def test_a_permanent_failure_is_saved_blank_and_not_retried(self):
        ge.DAILY_START = SAT
        g = daily_garmin(get_sleep_data=failing_on({SUN}, not_found(), SLEEP),
                         get_training_status=not_found())
        ge.daily_run(g, "init")

        self.assertEqual(self.state()["failed_days"], [])
        got = self.by_date()
        self.assertEqual((got[SUN]["sleep_score"], got[SUN]["vo2max"]), ("", ""))

    def test_init_restarts_from_the_saved_start_date_and_drops_older_rows(self):
        ge.DAILY_START = day(-40)
        self.seed([{"date": day(-20), "resting_hr": "70"}, {"date": day(-10), "resting_hr": "71"}],
                  start_date=day(-2), last_updated=day(-1), failed_days=[day(-10)])
        g = daily_garmin()
        ge.daily_run(g, "init")

        self.assertEqual(called(g, "get_user_summary"), [(day(k),) for k in (-2, -1, 0)])
        self.assertEqual(list(self.by_date()), [day(-2), day(-1), day(0)])
        self.assertEqual(self.state()["num_days"], 3)

    def test_the_weekly_key_is_the_iso_week_across_a_year_boundary(self):
        # 2025-12-28 is a Sunday in ISO 2025-W52; 2025-12-29 .. 2026-01-04 is ISO 2026-W01
        self.seed([], start_date=day(-30), last_updated=day(-1),
                  failed_days=["2025-12-28", "2025-12-29", "2026-01-02"])
        g = daily_garmin()
        ge.daily_run(g, "update")

        sampled = called(g, "get_training_status")
        self.assertEqual(sampled[:2], [("2025-12-28",), ("2025-12-29",)])
        self.assertNotIn(("2026-01-02",), sampled)
        got = self.by_date()
        self.assertEqual(got["2025-12-28"]["vo2max"], "28.5")
        self.assertEqual(got["2026-01-02"]["vo2max"], "29.5")


class DailyUpdate(DailyCase):
    def test_update_refetches_the_overlap_window_and_keeps_older_rows(self):
        old, stale = day(-15), day(-3)
        self.seed([{"date": old, "resting_hr": "77"}, {"date": stale, "resting_hr": "99"}],
                  start_date=day(-30), last_updated=day(-1))
        g = daily_garmin()
        ge.daily_run(g, "update")

        window = [day(k) for k in range(-4, 1)]          # last_updated - DAILY_OVERLAP .. today
        self.assertEqual(called(g, "get_user_summary"), [(d,) for d in window])
        got = self.by_date()
        self.assertEqual(list(got), [old] + window)
        self.assertEqual(got[old]["resting_hr"], "77")
        self.assertEqual(got[old]["sleep_score"], "")
        self.assertEqual(got[stale]["resting_hr"], str(40 + n(stale)))
        state = self.state()
        self.assertEqual((state["start_date"], state["last_updated"], state["num_days"]),
                         (day(-30), TODAY.isoformat(), 6))

    def test_update_never_reaches_before_the_start_date(self):
        self.seed([], start_date=day(-2), last_updated=day(-1))
        g = daily_garmin()
        ge.daily_run(g, "update")
        self.assertEqual(called(g, "get_user_summary"), [(day(k),) for k in (-2, -1, 0)])

    def test_update_without_state_starts_at_daily_start_and_keeps_the_csv(self):
        ge.DAILY_START = day(-1)
        ge.write_csv(ge.DAILY_CSV, ge.DAILY_COLS, [{"date": day(-9), "steps": "123"}])
        g = daily_garmin()
        ge.daily_run(g, "update")

        self.assertEqual(called(g, "get_user_summary"), [(day(-1),), (day(0),)])
        got = self.by_date()
        self.assertEqual(list(got), [day(-9), day(-1), day(0)])
        self.assertEqual(got[day(-9)]["steps"], "123")

    def test_update_retries_failed_days_however_old_and_clears_them(self):
        lost = day(-20)
        self.seed([{"date": lost}], start_date=day(-30), last_updated=day(-1),
                  failed_days=[lost, day(-2)])
        g = daily_garmin()
        ge.daily_run(g, "update")

        # day(-2) is inside the window too, and is fetched once
        self.assertEqual(called(g, "get_user_summary"),
                         [(lost,)] + [(day(k),) for k in range(-4, 1)])
        self.assertEqual(self.by_date()[lost]["resting_hr"], str(40 + n(lost)))
        self.assertEqual(self.state()["failed_days"], [])

    def test_a_day_that_fails_again_stays_queued(self):
        lost = day(-20)
        self.seed([{"date": lost}], start_date=day(-30), last_updated=day(-1), failed_days=[lost])
        ge.daily_run(daily_garmin(get_user_summary=failing_on({lost}, rate_limited(),
                                                              user_summary)), "update")

        self.assertEqual(self.state()["failed_days"], [lost])
        self.assertEqual(self.by_date()[lost]["resting_hr"], "")

    def test_days_failed_by_init_are_filled_by_the_next_update(self):
        ge.DAILY_START = SAT
        ge.daily_run(daily_garmin(get_training_status=failing_on({SAT}, rate_limited(),
                                                                 training_status)), "init")
        g = daily_garmin()
        ge.daily_run(g, "update")

        fetched = {c[0] for c in called(g, "get_user_summary")}
        self.assertLessEqual({SAT, SUN}, fetched)
        got = self.by_date()
        self.assertEqual((got[SAT]["vo2max"], got[SUN]["vo2max"]), (vo2_from(SAT), vo2_from(SAT)))
        self.assertEqual(self.state()["failed_days"], [])


class DailyAutoAndStatus(DailyCase):
    def test_auto_on_a_fresh_domain_is_init(self):
        ge.DAILY_START = day(-2)
        g = daily_garmin()
        ge.daily_run(g, "auto")
        self.assertEqual(list(self.by_date()), [day(-2), day(-1), day(0)])
        self.assertEqual(self.state()["last_updated"], TODAY.isoformat())

    def test_auto_after_a_previous_run_is_update(self):
        self.seed([{"date": day(-15), "steps": "5"}], start_date=day(-30), last_updated=day(-1))
        g = daily_garmin()
        ge.daily_run(g, "auto")

        self.assertEqual(called(g, "get_user_summary"), [(day(k),) for k in range(-4, 1)])
        self.assertEqual(self.by_date()[day(-15)]["steps"], "5")

    def test_status_on_a_fresh_domain(self):
        self.assertEqual(status_line(ge.daily_run), "  daily : 0 days  (last never)\n")
        self.assertFalse(os.path.exists(ge.DAILY_CSV))
        self.assertFalse(os.path.exists(ge.DAILY_STATE))

    def test_status_reports_days_last_run_and_retries(self):
        ge.save_state(ge.DAILY_STATE, {"start_date": "2026-01-01", "last_updated": "2026-11-20",
                                       "num_days": 12, "failed_days": ["2026-11-01", "2026-11-02"]})
        self.assertEqual(status_line(ge.daily_run),
                         "  daily : 12 days  (last 2026-11-20), 2 awaiting retry\n")


WEEKLY = {"vo2max": 44.5, "endurance": 5100, "fitness_age": 31.0}


class DailyDayRow(unittest.TestCase):
    def test_readiness_may_come_as_a_single_object(self):
        g = daily_garmin(get_training_readiness={"score": 72, "level": "HIGH"})
        row = ge.daily_day_row(g, "2026-03-04", WEEKLY, [])
        self.assertEqual((row["training_readiness_score"], row["training_readiness_level"]),
                         (72, "HIGH"))

    def test_readiness_takes_the_first_entry_of_a_list(self):
        row = ge.daily_day_row(daily_garmin(), "2026-03-04", WEEKLY, [])
        self.assertEqual((row["training_readiness_score"], row["training_readiness_level"]),
                         (61, "MODERATE"))

    def test_empty_answers_give_blank_cells(self):
        g = StubGarmin(get_user_summary=None, get_sleep_data={"dailySleepDTO": None},
                       get_hrv_data={"hrvSummary": None}, get_training_readiness=[])
        failures = []
        row = ge.daily_day_row(g, "2026-03-04", {}, failures)

        self.assertEqual(row["date"], "2026-03-04")
        self.assertEqual({k: v for k, v in row.items() if k != "date"},
                         dict.fromkeys(ge.DAILY_COLS[1:], ""))
        self.assertEqual(failures, [])

    def test_sleep_without_a_score_or_duration(self):
        g = daily_garmin(get_sleep_data={"dailySleepDTO": {"deepSleepSeconds": 5460,
                                                           "sleepScores": {"overall": None}}})
        row = ge.daily_day_row(g, "2026-03-04", WEEKLY, [])
        self.assertEqual((row["sleep_score"], row["sleep_hours"], row["deep_min"], row["rem_min"]),
                         ("", "", 91, ""))

    def test_weekly_values_are_copied_in(self):
        row = ge.daily_day_row(daily_garmin(), "2026-03-04", WEEKLY, [])
        self.assertEqual((row["vo2max"], row["endurance_score"], row["fitness_age"]),
                         (44.5, 5100, 31.0))

    def test_every_transient_failure_is_collected(self):
        boom = rate_limited()
        g = StubGarmin(get_user_summary=boom, get_sleep_data=boom, get_hrv_data=boom,
                       get_training_readiness=boom)
        failures = []
        row = ge.daily_day_row(g, "2026-03-04", {}, failures)
        self.assertEqual(len(failures), 4)
        self.assertEqual((row["resting_hr"], row["sleep_score"], row["hrv_status"],
                          row["training_readiness_score"]), ("", "", "", ""))


class DailyWeeklySample(unittest.TestCase):
    def test_reads_vo2max_fitness_age_and_a_seven_day_endurance_window(self):
        g = daily_garmin()
        failures = []
        self.assertEqual(ge.daily_weekly_sample(g, "2026-03-09", failures),
                         {"vo2max": 9.5, "fitness_age": 33.0, "endurance": 5009})
        self.assertEqual(called(g, "get_endurance_score"), [("2026-03-03", "2026-03-09")])
        self.assertEqual(failures, [])

    def test_missing_values_are_blank(self):
        g = StubGarmin(get_training_status={"mostRecentVO2Max": {"generic": None}},
                       get_fitnessage_data=None, get_endurance_score={"enduranceScoreDTO": None})
        self.assertEqual(ge.daily_weekly_sample(g, "2026-03-09", []),
                         {"vo2max": "", "fitness_age": "", "endurance": ""})

    def test_transient_failures_are_collected_permanent_ones_are_not(self):
        g = StubGarmin(get_training_status=rate_limited(), get_fitnessage_data=not_found(),
                       get_endurance_score=ge.GarminConnectConnectionError("API Error 503"))
        failures = []
        self.assertEqual(ge.daily_weekly_sample(g, "2026-03-09", failures),
                         {"vo2max": "", "fitness_age": "", "endurance": ""})
        self.assertEqual([type(e).__name__ for e in failures],
                         ["GarminConnectTooManyRequestsError", "GarminConnectConnectionError"])


# ===========================================================================
# BODY
# ===========================================================================
LAST_WINDOW = (f"{LAST_YEAR}-03-01", f"{LAST_YEAR}-12-31")
THIS_WINDOW = (f"{THIS_YEAR}-01-01", TODAY.isoformat())


def metric(pk, when, **kw):
    """One entry of a weigh-in day's allWeightMetrics (weights in grams)."""
    return {"samplePk": pk, "calendarDate": when, "weight": 71250, "bmi": 22.46,
            "bodyFat": 18.5, "bodyWater": 55.1, "muscleMass": 33100, "boneMass": 3200,
            "visceralFat": 7, "metabolicAge": 30, "physiqueRating": 5,
            "sourceType": "INDEX_SCALE", **kw}


def weigh_ins(metrics):
    """get_weigh_ins answer: the metrics dated inside [lo, hi], one day summary each."""
    def answer(lo, hi):
        ms = [m for m in metrics if lo <= m["calendarDate"][:10] <= hi]
        return {"dailyWeightSummaries": [{"allWeightMetrics": [m]} for m in ms]}
    return answer


PRS = [
    {"typeId": 7, "value": 21500.0, "activityType": "running",
     "prStartTimeLocalFormatted": "2024-05-04 07:30:00"},
    {"typeId": 3, "value": 1500.4, "activityType": "running",
     "activityStartDateTimeLocalFormatted": "2023-04-01 09:00:00"},
    {"typeId": None, "value": 1},
    {"typeId": 12, "value": 25000.0, "activityType": None},
    {"typeId": 99, "value": 3.5},
    {"typeId": 8, "value": "n/a", "activityType": "cycling"},
    {"typeId": 1, "value": None},
]
PR_ROWS = [
    {"type_id": "1", "label": "Fastest 1 km", "activity_type": "", "value_raw": "",
     "value_pretty": "", "units": "h:mm:ss", "record_date": ""},
    {"type_id": "3", "label": "Fastest 5 km", "activity_type": "running", "value_raw": "1500.4",
     "value_pretty": "25:00", "units": "h:mm:ss", "record_date": "2023-04-01"},
    {"type_id": "7", "label": "Longest run", "activity_type": "running", "value_raw": "21500.0",
     "value_pretty": "21.5", "units": "km", "record_date": "2024-05-04"},
    {"type_id": "8", "label": "Longest ride", "activity_type": "cycling", "value_raw": "n/a",
     "value_pretty": "", "units": "km", "record_date": ""},
    {"type_id": "12", "label": "Most steps in a day", "activity_type": "", "value_raw": "25000.0",
     "value_pretty": "25000", "units": "count", "record_date": ""},
    {"type_id": "99", "label": "type_99", "activity_type": "", "value_raw": "3.5",
     "value_pretty": "", "units": "", "record_date": ""},
    {"type_id": "", "label": "type_None", "activity_type": "", "value_raw": "1",
     "value_pretty": "", "units": "", "record_date": ""},
]
RACE = {"calendarDate": "2026-01-15", "time5K": 1500, "time10K": 3125,
        "timeHalfMarathon": 7000, "timeMarathon": None}


class BodyCase(Quiet):
    def setUp(self):
        super().setUp()
        for p in (ge.WEIGHT_CSV, ge.PR_CSV, ge.RACE_CSV, ge.BODY_STATE):
            if os.path.exists(p):
                os.remove(p)
        self.addCleanup(setattr, ge, "BODY_START", ge.BODY_START)
        ge.BODY_START = LAST_WINDOW[0]

    def state(self):
        return ge.load_state(ge.BODY_STATE, {})


class BodyRefresh(BodyCase):
    def test_refresh_writes_weigh_ins_prs_and_race_predictions(self):
        a, b = f"{LAST_YEAR}-06-01", f"{THIS_YEAR}-01-01"
        g = StubGarmin(get_weigh_ins=weigh_ins([
                           metric(2, b, weight=None, muscleMass=0, boneMass=None, bmi="n/a"),
                           metric(1, f"{a}T07:15:00")]),
                       get_personal_record=PRS, get_race_predictions=RACE)
        ge.body_run(g, "refresh")

        self.assertEqual(called(g, "get_weigh_ins"), [LAST_WINDOW, THIS_WINDOW])
        weights = rows(ge.WEIGHT_CSV)
        self.assertEqual(weights, [
            {"date": a, "weight_kg": "71.25", "bmi": "22.5", "body_fat_pct": "18.5",
             "body_water_pct": "55.1", "muscle_mass_kg": "33.1", "bone_mass_kg": "3.2",
             "visceral_fat": "7", "metabolic_age": "30", "physique_rating": "5",
             "source": "INDEX_SCALE"},
            {"date": b, "weight_kg": "", "bmi": "", "body_fat_pct": "18.5",
             "body_water_pct": "55.1", "muscle_mass_kg": "", "bone_mass_kg": "",
             "visceral_fat": "7", "metabolic_age": "30", "physique_rating": "5",
             "source": "INDEX_SCALE"},
        ])
        self.assertEqual(list(weights[0]), ge.WEIGHT_COLS)
        self.assertEqual(rows(ge.PR_CSV), PR_ROWS)
        self.assertEqual(rows(ge.RACE_CSV), [
            {"pulled_date": TODAY.isoformat(), "calendar_date": "2026-01-15", "distance": "5K",
             "time_seconds": "1500", "time_pretty": "25:00"},
            {"pulled_date": TODAY.isoformat(), "calendar_date": "2026-01-15", "distance": "10K",
             "time_seconds": "3125", "time_pretty": "52:05"},
            {"pulled_date": TODAY.isoformat(), "calendar_date": "2026-01-15",
             "distance": "Half Marathon", "time_seconds": "7000", "time_pretty": "1:56:40"},
        ])
        state = self.state()
        self.assertEqual({k: state[k] for k in ("last_updated", "weigh_ins", "personal_records",
                                                "race_predictions")},
                         {"last_updated": TODAY.isoformat(), "weigh_ins": 2,
                          "personal_records": 7, "race_predictions": 3})
        self.assertIn("generated_at", state)
        self.assertEqual(status_line(ge.body_run),
                         "  body  : 2 weigh-ins, 7 PRs, 3 race predictions\n")

    def test_a_refresh_replaces_rows_the_new_pull_no_longer_has(self):
        ge.write_csv(ge.WEIGHT_CSV, ge.WEIGHT_COLS, [{"date": f"{THIS_YEAR}-01-01",
                                                      "weight_kg": "69.0"}])
        ge.write_csv(ge.PR_CSV, ge.PR_COLS, [{"type_id": "3", "label": "Fastest 5 km"}])
        ge.write_csv(ge.RACE_CSV, ge.RACE_COLS, [{"distance": "5K", "time_seconds": "1600"}])
        ge.body_run(StubGarmin(get_weigh_ins=None, get_personal_record=None,
                               get_race_predictions=None), "refresh")

        self.assertEqual((rows(ge.WEIGHT_CSV), rows(ge.PR_CSV), rows(ge.RACE_CSV)), ([], [], []))
        self.assertEqual({k: self.state()[k] for k in ("weigh_ins", "personal_records",
                                                       "race_predictions")},
                         {"weigh_ins": 0, "personal_records": 0, "race_predictions": 0})

    def test_status_on_a_fresh_domain(self):
        self.assertEqual(status_line(ge.body_run),
                         "  body  : 0 weigh-ins, 0 PRs, 0 race predictions\n")
        self.assertFalse(os.path.exists(ge.BODY_STATE))


class BodyFailures(BodyCase):
    def test_a_year_that_fails_keeps_its_rows_from_the_last_run(self):
        kept, dropped, fresh = f"{LAST_YEAR}-06-01", f"{THIS_YEAR}-01-01", f"{THIS_YEAR}-01-02"
        ge.write_csv(ge.WEIGHT_CSV, ge.WEIGHT_COLS, [
            {"date": kept, "weight_kg": "70.0", "source": "MANUAL"},
            {"date": dropped, "weight_kg": "69.0"}])

        def flaky(lo, hi):
            if lo == LAST_WINDOW[0]:
                raise rate_limited()
            return weigh_ins([metric(5, fresh, weight=68000)])(lo, hi)
        ge.body_run(StubGarmin(get_weigh_ins=flaky), "refresh")

        got = rows(ge.WEIGHT_CSV)
        self.assertEqual([(r["date"], r["weight_kg"]) for r in got],
                         [(kept, "70.0"), (fresh, "68.0")])
        self.assertEqual(got[0]["source"], "MANUAL")
        self.assertEqual(self.state()["weigh_ins"], 2)

    def test_kept_rows_include_both_edges_of_the_failed_window_and_the_year_is_named(self):
        first, last = LAST_WINDOW
        ge.write_csv(ge.WEIGHT_CSV, ge.WEIGHT_COLS, [{"date": first, "weight_kg": "71.0"},
                                                     {"date": last, "weight_kg": "72.0"}])

        def flaky(lo, hi):
            if lo == first:
                raise rate_limited()
            return {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ge.body_pull_weight(StubGarmin(get_weigh_ins=flaky))
        self.assertEqual([r["date"] for r in rows(ge.WEIGHT_CSV)], [first, last])
        self.assertIn(f"weigh-ins for {LAST_YEAR} failed to fetch — kept their 2 rows",
                      err.getvalue())

    def test_any_error_counts_as_a_failed_year_not_only_transient_ones(self):
        kept = f"{THIS_YEAR}-01-01"
        ge.write_csv(ge.WEIGHT_CSV, ge.WEIGHT_COLS, [{"date": kept, "weight_kg": "70.0"}])
        self.assertEqual(ge.body_pull_weight(StubGarmin(get_weigh_ins=not_found())), 1)
        self.assertEqual([r["date"] for r in rows(ge.WEIGHT_CSV)], [kept])

    def test_failed_pr_and_race_calls_keep_the_last_snapshot(self):
        ge.write_csv(ge.PR_CSV, ge.PR_COLS, [{"type_id": "3", "label": "Fastest 5 km"},
                                             {"type_id": "7", "label": "Longest run"}])
        ge.write_csv(ge.RACE_CSV, ge.RACE_COLS, [{"distance": "5K", "time_seconds": "1600"}])
        before_prs, before_race = rows(ge.PR_CSV), rows(ge.RACE_CSV)
        g = StubGarmin(get_weigh_ins=weigh_ins([metric(1, f"{THIS_YEAR}-01-01")]),
                       get_personal_record=rate_limited(), get_race_predictions=not_found())
        ge.body_run(g, "refresh")

        self.assertEqual(rows(ge.PR_CSV), before_prs)
        self.assertEqual(rows(ge.RACE_CSV), before_race)
        self.assertEqual(len(rows(ge.WEIGHT_CSV)), 1)            # the rest still refreshed
        self.assertEqual({k: self.state()[k] for k in ("weigh_ins", "personal_records",
                                                       "race_predictions")},
                         {"weigh_ins": 1, "personal_records": 2, "race_predictions": 1})

    def test_a_failed_first_pull_writes_no_snapshot(self):
        g = StubGarmin(get_personal_record=rate_limited(), get_race_predictions=rate_limited())
        self.assertEqual((ge.body_pull_prs(g), ge.body_pull_race(g)), (0, 0))
        self.assertFalse(os.path.exists(ge.PR_CSV))
        self.assertFalse(os.path.exists(ge.RACE_CSV))


class BodyPullWeight(BodyCase):
    def test_a_sample_listed_twice_is_written_once_last_answer_wins(self):
        when = f"{THIS_YEAR}-01-01"
        g = StubGarmin(get_weigh_ins={"dailyWeightSummaries": [
            {"allWeightMetrics": [metric(9, when, weight=70000)]},
            {"allWeightMetrics": None},
            {"allWeightMetrics": [metric(9, when, weight=71000)]}]})
        # every year window gets the same answer here, so the sample is seen in both
        self.assertEqual(ge.body_pull_weight(g), 1)
        self.assertEqual([(r["date"], r["weight_kg"]) for r in rows(ge.WEIGHT_CSV)],
                         [(when, "71.0")])

    def test_weigh_ins_without_a_sample_id_collapse_into_one_row(self):
        # pinned as-is: rows are keyed by samplePk, so every metric lacking one shares key None
        g = StubGarmin(get_weigh_ins=weigh_ins([
            metric(None, f"{LAST_YEAR}-06-01", weight=70000),
            metric(None, f"{THIS_YEAR}-01-01", weight=71000)]))
        self.assertEqual(ge.body_pull_weight(g), 1)
        self.assertEqual([(r["date"], r["weight_kg"]) for r in rows(ge.WEIGHT_CSV)],
                         [(f"{THIS_YEAR}-01-01", "71.0")])

    def test_an_empty_answer_writes_a_header_only_file(self):
        g = StubGarmin(get_weigh_ins={"dailyWeightSummaries": None})
        self.assertEqual(ge.body_pull_weight(g), 0)
        with open(ge.WEIGHT_CSV) as f:
            self.assertEqual(f.read().strip(), ",".join(ge.WEIGHT_COLS))


# ===========================================================================
# shared retry helpers
# ===========================================================================
def conn_error(msg, status=None, cause=None):
    e = ge.GarminConnectConnectionError(msg)
    if status is not None:
        e.response = types.SimpleNamespace(status_code=status)
    e.__cause__ = cause
    return e


class IsTransient(unittest.TestCase):
    def check(self, cases):
        for e, expected in cases:
            with self.subTest(e=repr(e), cause=repr(e.__cause__)):
                self.assertIs(ge.is_transient(e), expected)

    def test_rate_limits_and_auth_hiccups_are_transient(self):
        self.check([(ge.GarminConnectTooManyRequestsError("429"), True),
                    (ge.GarminConnectAuthenticationError("401"), True)])

    def test_not_found_and_non_garmin_errors_are_final(self):
        self.check([(ge.GarminConnectNotFoundError("API Error 500"), False),
                    (ValueError("HTTP 503"), False),
                    (KeyError("x"), False)])

    def test_dropped_connections_and_timeouts_are_transient(self):
        self.check([(conn_error("x", cause=ge.requests.ConnectionError()), True),
                    (conn_error("x", cause=ge.requests.Timeout()), True),
                    (conn_error("x", cause=ge.requests.ReadTimeout()), True),
                    (conn_error("x", cause=ValueError()), False),
                    (conn_error("x", cause=ConnectionResetError()), False)])

    def test_the_response_status_decides_first(self):
        self.check([(conn_error("x", status=500), True),
                    (conn_error("x", status=503), True),
                    (conn_error("x", status=499), False),
                    (conn_error("API Error 500", status=404), False)])

    def test_otherwise_the_status_is_read_from_the_message(self):
        self.check([(conn_error("API Error 502: Bad Gateway"), True),
                    (conn_error("HTTP 503"), True),
                    (conn_error("request failed with error (504)"), True),
                    (conn_error("HTTP 400"), False),
                    (conn_error("error (429)"), False),
                    (conn_error("connection reset by peer"), False),
                    (conn_error("HTTP 503", status="503"), True)])    # non-int status is ignored


def raiser(exc):
    def fn():
        raise exc
    return fn


class Safe(unittest.TestCase):
    def test_returns_the_answer_even_when_falsy(self):
        self.assertEqual(ge.safe(lambda: 0, default=5), 0)
        self.assertEqual(ge.safe(lambda: {"a": 1}, default={}), {"a": 1})

    def test_an_error_returns_the_default(self):
        self.assertEqual(ge.safe(lambda: 1 / 0, default="d"), "d")
        self.assertIsNone(ge.safe(lambda: 1 / 0))

    def test_only_transient_errors_are_collected(self):
        failures = []
        for exc in (rate_limited(), not_found(), ValueError("bad json"),
                    conn_error("HTTP 502")):
            ge.safe(raiser(exc), {}, failures=failures)
        self.assertEqual([type(e).__name__ for e in failures],
                         ["GarminConnectTooManyRequestsError", "GarminConnectConnectionError"])

    def test_ctrl_c_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            ge.safe(raiser(KeyboardInterrupt()), default={}, failures=[])

    def test_the_pause_follows_every_call_whether_it_succeeds_or_not(self):
        slept = []
        self.addCleanup(setattr, ge.time, "sleep", ge.time.sleep)
        ge.time.sleep = slept.append
        ge.safe(lambda: 1, pause=0.5)
        ge.safe(lambda: 1 / 0, pause=0.25)
        ge.safe(lambda: 1)
        ge.safe(lambda: 1 / 0)
        self.assertEqual(slept, [0.5, 0.25])


class SmallHelpers(unittest.TestCase):
    def test_retry_note(self):
        self.assertEqual(ge.retry_note(0), "")
        self.assertEqual(ge.retry_note(None), "")
        self.assertEqual(ge.retry_note(1), ", 1 awaiting retry")
        self.assertEqual(ge.retry_note(12), ", 12 awaiting retry")

    def test_overlap_start(self):
        self.assertEqual(ge.overlap_start({"start_date": "2026-01-01",
                                           "last_updated": "2026-11-24"}, 3), "2026-11-21")
        self.assertEqual(ge.overlap_start({"start_date": "2026-01-01",
                                           "last_updated": "2026-11-24T22:15:00"}, 7),
                         "2026-11-17")
        self.assertEqual(ge.overlap_start({"start_date": "2026-11-22",
                                           "last_updated": "2026-11-24"}, 3), "2026-11-22")
        self.assertEqual(ge.overlap_start({"start_date": "2026-03-05",
                                           "last_updated": None}, 3), "2026-03-05")
        self.assertEqual(ge.overlap_start({"start_date": "2026-03-05"}, 3), "2026-03-05")
        self.assertEqual(ge.overlap_start({"start_date": "2026-03-05",
                                           "last_updated": "2026-03-01"}, 0), "2026-03-05")

    def test_year_windows(self):
        self.assertEqual(ge.year_windows("2024-03-05", "2026-02-10"),
                         [("2024-03-05", "2024-12-31"), ("2025-01-01", "2025-12-31"),
                          ("2026-01-01", "2026-02-10")])
        self.assertEqual(ge.year_windows("2026-02-01", "2026-02-01"),
                         [("2026-02-01", "2026-02-01")])
        self.assertEqual(ge.year_windows("2025-12-31", "2026-01-01"),
                         [("2025-12-31", "2025-12-31"), ("2026-01-01", "2026-01-01")])
        self.assertEqual(ge.year_windows("2027-01-01", "2026-06-01"), [])
        # pinned as-is: an inverted range inside one year comes back as one inverted window
        self.assertEqual(ge.year_windows("2026-05-01", "2026-04-01"),
                         [("2026-05-01", "2026-04-01")])



class RowRounding(unittest.TestCase):
    """Units are converted and rounded to two decimals."""

    def test_weigh_in_masses_are_kilograms_to_two_decimals(self):
        row = ge.weigh_in_row(metric(1, "2026-02-03", weight=80123, muscleMass=33149,
                                     boneMass=3201))
        self.assertEqual((row["weight_kg"], row["muscle_mass_kg"], row["bone_mass_kg"]),
                         (80.12, 33.15, 3.2))

    def test_distance_records_are_kilometres_to_two_decimals(self):
        self.assertEqual(ge.pr_row({"typeId": 7, "value": 21512.4})["value_pretty"], 21.51)

    def test_sleep_hours_to_two_decimals(self):
        row = ge.daily_row("2026-02-03", {}, {"dailySleepDTO": {"sleepTimeSeconds": 27022}},
                           {}, {}, {})
        self.assertEqual(row["sleep_hours"], 7.51)

if __name__ == "__main__":
    unittest.main()
