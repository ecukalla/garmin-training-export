"""raw — every endpoint for every activity, one gzipped JSON per activity, resumable from disk."""
import contextlib
import io
import os
import shutil
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import garmin_export as ge
from tests.stubs import StubGarmin, act

TODAY = date.today().isoformat()


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def reset_raw():
    shutil.rmtree(ge.RAW_DIR, ignore_errors=True)
    for p in (ge.RAW_STATE, ge.RAW_ACCOUNT):
        if os.path.exists(p):
            os.remove(p)


def files():
    return sorted(os.listdir(ge.RAW_DIR)) if os.path.isdir(ge.RAW_DIR) else []


def state():
    return ge.load_state(ge.RAW_STATE, {})


def cached(aid, day="2020-01-01", errors=None, partial=False):
    """A capture already on disk, the way raw_capture would have written it."""
    rec = {"schema": ge.RAW_SCHEMA, "activity_id": aid, "date": day, "type_key": "running",
           "list_object": act(aid, day, "running"), "endpoints": {"activity": {"old": True}},
           "skipped": [], "errors": errors or {}}
    os.makedirs(ge.RAW_DIR, exist_ok=True)
    ge.raw_write(ge.raw_path(aid, partial), rec)
    return rec


def fetched(g, name="get_activity"):
    """Activity ids `name` was called for, in call order."""
    return [c[1] for c in g.calls if c[0] == name]


def fails_for(exc, *ids):
    """An endpoint that raises `exc` for the given activity ids and answers the rest."""
    def endpoint(aid, *a, **k):
        if aid in ids:
            raise exc
        return {"ok": aid}
    return endpoint


def run_raw(g, mode):
    """raw_run with stdout and stderr captured; returns (stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        ge.raw_run(g, mode)
    return out.getvalue(), err.getvalue()


def stub(acts=(), **answers):
    answers.setdefault("get_activity", lambda aid: {"activityId": aid, "fresh": True})
    return StubGarmin(acts, **answers)


class RawCase(unittest.TestCase):
    def setUp(self):
        reset_raw()
        self.addCleanup(reset_raw)


# ---------------------------------------------------------------------------- raw_run
class RawInit(RawCase):
    def test_init_caches_every_activity_of_every_type_and_records_the_first_date(self):
        acts = [act(2, ago(3), "running"), act(1, "2009-05-03", "strength_training"),
                act(3, "2015-01-01", "yoga")]
        g = stub(acts)
        run_raw(g, "init")

        self.assertEqual(files(), ["1.json.gz", "2.json.gz", "3.json.gz"])
        self.assertEqual(fetched(g), [1, 3, 2])                     # oldest first
        rec = ge.raw_read(ge.raw_path(1))
        self.assertEqual(rec["list_object"], acts[1])
        self.assertEqual(rec["type_key"], "strength_training")
        self.assertEqual(rec["endpoints"]["activity"], {"activityId": 1, "fresh": True})
        s = state()
        self.assertEqual(s["start_date"], "2009-05-03")
        self.assertEqual(s["last_updated"], TODAY)
        self.assertEqual(s["schema"], ge.RAW_SCHEMA)
        self.assertEqual(s["num_activities"], 3)
        self.assertEqual(s["num_with_errors"], 0)
        self.assertEqual(s["bytes_on_disk"], sum(
            os.path.getsize(os.path.join(ge.RAW_DIR, f)) for f in files()))
        self.assertEqual(ge.raw_read(ge.RAW_ACCOUNT)["pulled_date"], TODAY)

    def test_init_lists_every_year_from_the_earliest_date_to_today(self):
        g = stub()
        run_raw(g, "init")
        listings = [c[1:] for c in g.calls if c[0] == "get_activities_by_date"]
        self.assertEqual(len(listings), date.today().year - 2000 + 1)
        self.assertEqual(listings[0], ("2000-01-01", "2000-12-31"))
        self.assertEqual(listings[-1], (f"{date.today().year}-01-01", TODAY))

    def test_init_with_no_activities_keeps_the_earliest_start_and_creates_the_dir(self):
        _, err = run_raw(stub(), "init")
        self.assertTrue(os.path.isdir(ge.RAW_DIR))
        self.assertEqual(files(), [])
        self.assertEqual(state()["start_date"], ge.EARLIEST)
        self.assertEqual(state()["num_activities"], 0)
        self.assertIn("✓ raw: +0 new → 0 activities cached, 0 MB in raw/", err)

    def test_init_skips_healthy_cached_files_and_refetches_transient_holes(self):
        cached(1, "2020-01-01")
        cached(2, "2020-01-02", {"weather": "GarminConnectConnectionError: reset"})
        cached(3, "2020-01-03", {"weather": "GarminConnectNotFoundError: 404"})
        os.makedirs(ge.RAW_DIR, exist_ok=True)
        with open(ge.raw_path(4), "wb") as f:
            f.write(b"not gzip at all")
        cached(99, "2020-02-02")                    # on disk but no longer listed by Garmin
        healthy = Path(ge.raw_path(1)).read_bytes()
        g = stub([act(i, f"2020-01-0{i}", "running") for i in (1, 2, 3, 4)])

        _, err = run_raw(g, "init")

        self.assertEqual(fetched(g), [2, 4])
        self.assertIn("raw: 4.json.gz unreadable (BadGzipFile) — will re-fetch", err)
        self.assertIn("raw: 2 cached files hold transient failures — re-fetching", err)
        self.assertEqual(Path(ge.raw_path(1)).read_bytes(), healthy)
        self.assertEqual(ge.raw_read(ge.raw_path(2))["errors"], {})
        self.assertEqual(ge.raw_read(ge.raw_path(4))["endpoints"]["activity"],
                         {"activityId": 4, "fresh": True})
        self.assertEqual(ge.raw_read(ge.raw_path(3))["errors"],
                         {"weather": "GarminConnectNotFoundError: 404"})
        self.assertEqual(state()["num_activities"], 5)    # the unlisted file still counts

    def test_init_refetches_a_partial_and_removes_it_once_complete(self):
        cached(5, "2020-05-05", {"weather": "rate limited: 429"}, partial=True)
        g = stub([act(5, "2020-05-05", "running")])
        run_raw(g, "init")
        self.assertEqual(fetched(g), [5])
        self.assertEqual(files(), ["5.json.gz"])

    def test_auto_on_a_fresh_cache_is_init(self):
        g = stub([act(1, "2003-03-03")])
        run_raw(g, "auto")
        self.assertEqual(fetched(g), [1])
        self.assertEqual(state()["start_date"], "2003-03-03")


class RawIncomplete(RawCase):
    def test_a_transient_endpoint_failure_parks_the_capture_as_partial(self):
        g = stub([act(1, "2020-01-01"), act(2, "2020-01-02")],
                 get_activity_weather=fails_for(ge.GarminConnectConnectionError("reset"), 2))
        _, err = run_raw(g, "init")

        self.assertEqual(files(), ["1.json.gz", "2.partial.json.gz"])
        rec = ge.raw_read(ge.raw_path(2, partial=True))
        self.assertEqual(rec["errors"], {"weather": "GarminConnectConnectionError: reset"})
        self.assertEqual(state()["num_activities"], 1)
        self.assertEqual(state()["num_with_errors"], 1)
        self.assertIn("raw: 1 activities hit network failures and were left incomplete — "
                      "re-run `raw update` to retry them", err)
        self.assertEqual(state()["bytes_on_disk"], sum(
            os.path.getsize(os.path.join(ge.RAW_DIR, f)) for f in files()))

    def test_a_permanent_failure_is_a_finished_capture(self):
        g = stub([act(1, "2020-01-01"), act(2, "2020-01-02")],
                 get_activity_weather=fails_for(ge.GarminConnectNotFoundError("404"), 2))
        run_raw(g, "init")

        self.assertEqual(files(), ["1.json.gz", "2.json.gz"])
        self.assertEqual(ge.raw_read(ge.raw_path(2))["errors"],
                         {"weather": "GarminConnectNotFoundError: 404"})
        self.assertEqual(state()["num_activities"], 2)
        self.assertEqual(state()["num_with_errors"], 1)

    def test_an_auth_error_is_saved_as_a_finished_capture(self):
        """is_transient() retries auth hiccups; raw's name list does not include them."""
        g = stub([act(1, "2020-01-01")],
                 get_activity_weather=ge.GarminConnectAuthenticationError("401"))
        run_raw(g, "init")
        self.assertEqual(files(), ["1.json.gz"])
        self.assertEqual(ge.raw_read(ge.raw_path(1))["errors"],
                         {"weather": "GarminConnectAuthenticationError: 401"})

    def test_update_retries_an_old_partial_and_removes_it_once_complete(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA, "num_activities": 0,
                                     "num_with_errors": 1})
        cached(7, "2012-06-01", {"weather": "GarminConnectConnectionError: reset"}, partial=True)
        g = stub([act(7, "2012-06-01", "running"), act(8, ago(2))])

        run_raw(g, "update")

        self.assertEqual(fetched(g), [8, 7])        # recent listing first, then the retries
        self.assertEqual(files(), ["7.json.gz", "8.json.gz"])
        self.assertEqual(ge.raw_read(ge.raw_path(7))["list_object"]["activityId"], 7)
        self.assertEqual(state()["num_activities"], 2)
        self.assertEqual(state()["num_with_errors"], 0)

    def test_update_keeps_a_capture_that_fails_again_partial(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA})
        cached(7, "2012-06-01", {"weather": "GarminConnectConnectionError: reset"}, partial=True)
        g = stub(get_activity_weather=ge.GarminConnectTooManyRequestsError("429"))

        _, err = run_raw(g, "update")

        self.assertEqual(files(), ["7.partial.json.gz"])
        self.assertEqual(ge.raw_read(ge.raw_path(7, True))["errors"],
                         {"weather": "rate limited: 429"})
        self.assertEqual(state()["num_activities"], 0)
        self.assertIn("rate limited — sleeping 30s before retry", err)

    def test_a_partial_that_is_also_listed_is_fetched_once(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA})
        cached(8, ago(2), {"weather": "gave up"}, partial=True)
        g = stub([act(8, ago(2))])
        run_raw(g, "update")
        self.assertEqual(fetched(g), [8])
        self.assertEqual(files(), ["8.json.gz"])

    def test_a_cached_hole_that_fails_again_on_init_leaves_both_files_and_update_skips_it(self):
        """The finished-name file still counts as cached, so `update` filters the .partial
        retry out; only the next `init` (which re-reads the old file) fetches it again."""
        cached(1, "2020-01-01", {"weather": "GarminConnectConnectionError: reset"})
        run_raw(stub([act(1, "2020-01-01")],
                     get_activity_weather=ge.GarminConnectConnectionError("again")), "init")
        self.assertEqual(files(), ["1.json.gz", "1.partial.json.gz"])
        self.assertEqual(state()["num_activities"], 0)          # while status counts 1
        self.assertIn(" 1 activities cached", run_raw(None, "status")[0])

        g = stub([act(1, "2020-01-01")])
        run_raw(g, "update")
        self.assertEqual(fetched(g), [])
        self.assertEqual(files(), ["1.json.gz", "1.partial.json.gz"])
        self.assertEqual(state()["num_activities"], 1)

        g = stub([act(1, "2020-01-01")])
        run_raw(g, "init")
        self.assertEqual(fetched(g), [1])
        self.assertEqual(files(), ["1.json.gz"])

    def test_a_partial_from_a_first_run_with_nothing_cached_still_checkpoints(self):
        """len(have) stays 0, and 0 % RAW_SAVE_EVERY == 0, so a partial triggers a save."""
        def get_activity(aid):
            if aid == 2:
                raise KeyboardInterrupt
            return {}
        g = stub([act(1, "2020-01-01"), act(2, "2020-01-02")], get_activity=get_activity,
                 get_activity_weather=ge.GarminConnectConnectionError("reset"))
        with self.assertRaises(KeyboardInterrupt):
            run_raw(g, "init")
        self.assertEqual(files(), ["1.partial.json.gz"])
        self.assertEqual(state()["num_activities"], 0)
        self.assertIsNone(state()["last_updated"])


class RawUpdate(RawCase):
    def test_update_lists_only_the_overlap_window_and_skips_what_is_cached(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(10),
                                     "schema": ge.RAW_SCHEMA, "num_activities": 1,
                                     "num_with_errors": 0})
        cached(2, ago(12))
        g = stub([act(1, ago(30)), act(2, ago(12)), act(3, ago(2))])

        run_raw(g, "update")

        listings = [c[1:] for c in g.calls if c[0] == "get_activities_by_date"]
        self.assertEqual(listings[0][0], ago(17))
        self.assertEqual(listings[-1][1], TODAY)
        self.assertEqual(fetched(g), [3])
        self.assertEqual(files(), ["2.json.gz", "3.json.gz"])
        s = state()
        self.assertEqual(s["start_date"], "2009-01-01")
        self.assertEqual(s["num_activities"], 2)
        self.assertEqual(s["last_updated"], TODAY)

    def test_auto_after_a_finished_run_is_update(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA})
        g = stub([act(1, "2010-01-01"), act(2, ago(1))])
        run_raw(g, "auto")
        self.assertEqual(fetched(g), [2])

    def test_num_with_errors_counts_only_this_run(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA, "num_activities": 1,
                                     "num_with_errors": 5})
        cached(1, "2020-01-01", {"weather": "GarminConnectNotFoundError: 404"})
        run_raw(stub(), "update")
        self.assertEqual(state()["num_with_errors"], 0)
        self.assertEqual(state()["num_activities"], 1)


class RawSchema(RawCase):
    def test_a_schema_change_refetches_everything_even_on_update(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1),
                                     "schema": ge.RAW_SCHEMA - 1})
        cached(1, "2011-01-01")
        g = stub([act(1, "2011-01-01")])

        _, err = run_raw(g, "update")

        self.assertIn(f"raw: cache schema {ge.RAW_SCHEMA - 1} != {ge.RAW_SCHEMA}, "
                      "re-fetching everything", err)
        self.assertEqual(fetched(g), [1])
        self.assertEqual(ge.raw_read(ge.raw_path(1))["endpoints"]["activity"],
                         {"activityId": 1, "fresh": True})
        self.assertEqual(state()["schema"], ge.RAW_SCHEMA)
        self.assertEqual(state()["start_date"], "2011-01-01")   # ran as init

    def test_a_state_without_a_schema_is_treated_as_stale(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": ago(1)})
        cached(1, "2011-01-01")
        g = stub([act(1, "2011-01-01")])
        _, err = run_raw(g, "update")
        self.assertIn("raw: cache schema None != 1, re-fetching everything", err)
        self.assertEqual(fetched(g), [1])


class RawCheckpoint(RawCase):
    def interrupted_on_third(self):
        def get_activity(aid):
            if aid == 3:
                raise KeyboardInterrupt
            return {"activityId": aid}
        return stub([act(i, f"2020-01-0{i}") for i in (1, 2, 3)], get_activity=get_activity)

    def test_the_manifest_is_checkpointed_every_raw_save_every_captures(self):
        with mock.patch.object(ge, "RAW_SAVE_EVERY", 2), self.assertRaises(KeyboardInterrupt):
            run_raw(self.interrupted_on_third(), "init")
        self.assertEqual(files(), ["1.json.gz", "2.json.gz"])
        s = state()
        self.assertEqual(s["num_activities"], 2)
        self.assertEqual(s["start_date"], "2020-01-01")
        self.assertIsNone(s["last_updated"])

    def test_an_interrupted_run_resumes_from_the_files_not_the_manifest(self):
        with self.assertRaises(KeyboardInterrupt):
            run_raw(self.interrupted_on_third(), "init")
        self.assertFalse(os.path.exists(ge.RAW_STATE))      # no checkpoint before 25
        self.assertEqual(files(), ["1.json.gz", "2.json.gz"])

        g = stub([act(i, f"2020-01-0{i}") for i in (1, 2, 3)])
        run_raw(g, "auto")
        self.assertEqual(fetched(g), [3])
        self.assertEqual(state()["num_activities"], 3)


class RawStatus(RawCase):
    def test_status_counts_finished_captures_and_all_bytes_on_disk(self):
        ge.save_state(ge.RAW_STATE, {"start_date": "2009-01-01", "last_updated": "2026-11-20",
                                     "schema": ge.RAW_SCHEMA})
        cached(1)
        cached(2)
        cached(3, partial=True)
        with open(os.path.join(ge.RAW_DIR, "4.json.gz"), "wb") as f:
            f.write(b"\0" * 2_400_000)
        out, _ = run_raw(None, "status")
        self.assertEqual(out, "  raw   : 3 activities cached, 2 MB on disk  (last 2026-11-20)\n")

    def test_status_before_any_run(self):
        out, _ = run_raw(None, "status")
        self.assertEqual(out, "  raw   : 0 activities cached, 0 MB on disk  (last never)\n")
        self.assertFalse(os.path.exists(ge.RAW_DIR))
        self.assertFalse(os.path.exists(ge.RAW_STATE))


# ---------------------------------------------------------------------------- pieces
class RawAccount(RawCase):
    def account(self, g):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = ge.raw_account(g)
        return out, err.getvalue()

    def test_snapshot_holds_every_account_endpoint_and_gear_for_the_profile_id(self):
        seen = {}
        g = StubGarmin(get_user_profile={"id": 42},
                       get_lactate_threshold=lambda **k: seen.setdefault("lt", k) and "lt",
                       get_gear=lambda pk: [{"gear": pk}])
        out, err = self.account(g)

        self.assertEqual(ge.raw_read(ge.RAW_ACCOUNT), out)
        self.assertEqual(out["schema"], ge.RAW_SCHEMA)
        self.assertEqual(out["pulled_date"], TODAY)
        self.assertEqual(list(out["endpoints"]), [
            "user_profile", "userprofile_settings", "heart_rate_zones", "power_zones",
            "lactate_threshold", "cycling_ftp", "devices", "primary_training_device",
            "activity_types", "max_metrics", "gear"])
        self.assertEqual(out["endpoints"]["user_profile"], {"id": 42})
        self.assertEqual(out["endpoints"]["gear"], [{"gear": 42}])
        self.assertEqual(out["errors"], {})
        self.assertEqual(seen["lt"], {"latest": True})
        self.assertIn(("get_max_metrics", TODAY), g.calls)
        self.assertEqual([c for c in g.calls if c[0] == "get_user_profile"],
                         [("get_user_profile",)])      # fetched once, reused
        self.assertEqual(err, "")

    def test_gear_falls_back_to_the_user_data_profile_pk(self):
        g = StubGarmin(get_user_profile={"userData": {"userProfilePk": 7}},
                       get_gear=lambda pk: pk)
        out, _ = self.account(g)
        self.assertEqual(out["endpoints"]["gear"], 7)

    def test_no_profile_means_no_gear_and_failures_are_recorded(self):
        g = StubGarmin(get_user_profile=ge.GarminConnectTooManyRequestsError("429"),
                       get_devices=ge.GarminConnectNotFoundError("404"),
                       get_cycling_ftp=ge.GarminConnectConnectionError("reset"))
        out, err = self.account(g)

        self.assertEqual(out["endpoints"]["user_profile"], {})
        self.assertNotIn("gear", out["endpoints"])
        self.assertNotIn("get_gear", [c[0] for c in g.calls])
        self.assertEqual(out["errors"], {"cycling_ftp": "GarminConnectConnectionError: reset",
                                         "devices": "GarminConnectNotFoundError: 404"})
        self.assertIn("! raw: account snapshot had 2 failures: cycling_ftp, devices", err)
        self.assertEqual(ge.raw_read(ge.RAW_ACCOUNT)["errors"], out["errors"])


class Listing:
    """get_activities_by_date answering per year: a list, or an exception to raise."""

    def __init__(self, by_year):
        self.by_year, self.calls = by_year, []

    def get_activities_by_date(self, lo, hi):
        self.calls.append((lo, hi))
        r = self.by_year.get(lo[:4], [])
        if isinstance(r, BaseException):
            raise r
        return r


class RawListAll(unittest.TestCase):
    def list_all(self, g, start, end):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = ge.raw_list_all(g, start, end)
        return out, err.getvalue()

    def test_lists_a_year_at_a_time(self):
        g = Listing({})
        out, _ = self.list_all(g, "2019-03-01", "2021-02-01")
        self.assertEqual(out, [])
        self.assertEqual(g.calls, [("2019-03-01", "2019-12-31"), ("2020-01-01", "2020-12-31"),
                                   ("2021-01-01", "2021-02-01")])

    def test_dedups_by_id_drops_idless_entries_and_sorts_oldest_first(self):
        a1, a2, a3 = (act(1, "2019-05-01"), act(2, "2020-01-01"), act(3, "2019-01-01"))
        no_date = {"activityId": 4}
        g = Listing({"2019": [a1, a3, {"activityId": None, "startTimeLocal": "2019-02-02"}],
                     "2020": [a2, dict(a1, dup=True), no_date],
                     "2021": None})
        out, _ = self.list_all(g, "2019-01-01", "2021-12-31")
        self.assertEqual(out, [no_date, a3, a1, a2])

    def test_a_failed_year_is_logged_and_skipped(self):
        g = Listing({"2019": [act(1, "2019-01-01")],
                     "2020": ge.GarminConnectNotFoundError("404"),
                     "2021": [act(3, "2021-01-01")]})
        out, err = self.list_all(g, "2019-01-01", "2021-12-31")
        self.assertEqual([a["activityId"] for a in out], [1, 3])
        self.assertIn("✗ raw: listing 2020 failed: GarminConnectNotFoundError: 404", err)

    def test_a_rate_limited_year_is_tried_three_times_then_skipped(self):
        g = Listing({"2020": ge.GarminConnectTooManyRequestsError("429")})
        out, err = self.list_all(g, "2020-01-01", "2020-12-31")
        self.assertEqual(out, [])
        self.assertEqual(len(g.calls), 3)
        self.assertIn("✗ raw: listing 2020 failed: rate limited: 429", err)


class RawCapture(unittest.TestCase):
    def test_record_holds_each_endpoint_and_the_list_object(self):
        a = act(9, "2026-03-04", "Strength_Training", hrTimeInZone_1=0)
        g = StubGarmin(get_activity_details=lambda *args: list(args))
        before = datetime.now().replace(microsecond=0)
        rec = ge.raw_capture(g, a)

        self.assertEqual(rec["schema"], ge.RAW_SCHEMA)
        self.assertEqual(rec["activity_id"], 9)
        self.assertEqual(rec["date"], "2026-03-04")
        self.assertEqual(rec["type_key"], "strength_training")
        self.assertIs(rec["list_object"], a)
        self.assertGreaterEqual(datetime.fromisoformat(rec["fetched_at"]), before)
        self.assertEqual(len(rec["fetched_at"]), 19)
        self.assertEqual(rec["skipped"], ["power_zones"])
        self.assertEqual(list(rec["endpoints"]), [
            "activity", "splits", "typed_splits", "split_summaries", "hr_zones", "weather",
            "gear", "exercise_sets", "details"])
        self.assertEqual(rec["endpoints"]["details"], [9, ge.RAW_MAXCHART, ge.RAW_MAXPOLY])
        self.assertEqual(rec["errors"], {})
        self.assertNotIn("get_activity_power_in_timezones", [c[0] for c in g.calls])

    def test_skipped_endpoints_are_not_called(self):
        g = StubGarmin()
        rec = ge.raw_capture(g, act(1, "2026-01-01", "running"))
        self.assertEqual(rec["skipped"], ["hr_zones", "power_zones", "exercise_sets"])
        called = [c[0] for c in g.calls]
        for name in ("get_activity_hr_in_timezones", "get_activity_power_in_timezones",
                     "get_activity_exercise_sets"):
            self.assertNotIn(name, called)
        self.assertEqual(called, ["get_activity", "get_activity_splits",
                                  "get_activity_typed_splits", "get_activity_split_summaries",
                                  "get_activity_weather", "get_activity_gear",
                                  "get_activity_details"])

    def test_zone_and_set_endpoints_apply_when_the_list_object_says_so(self):
        for type_key in ("strength_training", "fitness_equipment", "indoor_cardio"):
            a = act(1, "2026-01-01", type_key, hrTimeInZone_1=10, powerTimeInZone_1=5)
            rec = ge.raw_capture(StubGarmin(), a)
            self.assertEqual(rec["skipped"], [], type_key)

    def test_failures_are_recorded_per_endpoint(self):
        g = StubGarmin(get_activity_weather=ge.GarminConnectNotFoundError("404"),
                       get_activity_gear=ge.GarminConnectConnectionError("reset"))
        rec = ge.raw_capture(g, {"activityId": 5})
        self.assertEqual(rec["errors"], {"weather": "GarminConnectNotFoundError: 404",
                                         "gear": "GarminConnectConnectionError: reset"})
        self.assertNotIn("weather", rec["endpoints"])
        self.assertEqual(rec["date"], "")
        self.assertEqual(rec["type_key"], "")


class RawFetch(unittest.TestCase):
    def fetch(self, fn, **kw):
        err = io.StringIO()
        with mock.patch.object(ge.time, "sleep") as sleep, contextlib.redirect_stderr(err):
            out = ge.raw_fetch(fn, **kw)
        return out, [c.args[0] for c in sleep.call_args_list], err.getvalue()

    def test_success_paces_and_returns_the_value(self):
        out, sleeps, _ = self.fetch(lambda: {"v": 1})
        self.assertEqual(out, ({"v": 1}, None))
        self.assertEqual(sleeps, [ge.RAW_PAUSE])

    def test_rate_limits_back_off_doubling_then_succeed(self):
        answers = iter([ge.GarminConnectTooManyRequestsError("429"),
                        ge.GarminConnectTooManyRequestsError("429"), "ok"])

        def fn():
            v = next(answers)
            if isinstance(v, BaseException):
                raise v
            return v
        out, sleeps, err = self.fetch(fn)
        self.assertEqual(out, ("ok", None))
        self.assertEqual(sleeps, [30, 60, ge.RAW_PAUSE])
        self.assertIn("! rate limited — sleeping 30s before retry", err)
        self.assertIn("! rate limited — sleeping 60s before retry", err)

    def test_rate_limited_every_time_gives_up_with_the_last_message(self):
        calls = []

        def fn():
            calls.append(1)
            raise ge.GarminConnectTooManyRequestsError(f"429 #{len(calls)}")
        out, sleeps, _ = self.fetch(fn)
        self.assertEqual(out, (None, "rate limited: 429 #3"))
        self.assertEqual(sleeps, [30, 60, 120])
        self.assertEqual(len(calls), 3)

    def test_any_other_error_returns_its_message_without_retrying(self):
        calls = []

        def fn():
            calls.append(1)
            raise ge.GarminConnectConnectionError("reset")
        out, sleeps, _ = self.fetch(fn)
        self.assertEqual(out, (None, "GarminConnectConnectionError: reset"))
        self.assertEqual(sleeps, [ge.RAW_PAUSE])
        self.assertEqual(calls, [1])

    def test_zero_tries_gives_up_without_calling(self):
        out, sleeps, _ = self.fetch(lambda: self.fail("called"), tries=0)
        self.assertEqual(out, (None, "gave up"))
        self.assertEqual(sleeps, [])


class RawRetryable(unittest.TestCase):
    def test_transient_error_names_are_retryable_permanent_ones_are_not(self):
        cases = {
            "GarminConnectConnectionError: reset": True,
            "GarminConnectConnectionError: API Error 400": True,   # is_transient says final
            "GarminConnectTooManyRequestsError: 429": True,
            "ConnectionResetError: [Errno 54]": True,
            "ReadTimeout: read timed out": True,
            "ChunkedEncodingError: broken": True,
            "rate limited: 429": True,
            "gave up": True,
            "Timeout": True,
            "GarminConnectNotFoundError: 404": False,
            "GarminConnectAuthenticationError: 401": False,
            "KeyError: 'x'": False,
            "rate limited": True,
            "rate limited twice: 429": False,
        }
        for msg, want in cases.items():
            self.assertIs(ge.raw_retryable({"errors": {"x": msg}}), want, msg)

    def test_any_retryable_error_makes_the_capture_retryable(self):
        rec = {"errors": {"a": "GarminConnectNotFoundError: 404", "b": "gave up"}}
        self.assertTrue(ge.raw_retryable(rec))

    def test_no_errors_is_not_retryable(self):
        self.assertFalse(ge.raw_retryable({}))
        self.assertFalse(ge.raw_retryable({"errors": None}))
        self.assertFalse(ge.raw_retryable({"errors": {}}))


class RawFiles(RawCase):
    def put(self, name, data=b"x"):
        os.makedirs(ge.RAW_DIR, exist_ok=True)
        with open(os.path.join(ge.RAW_DIR, name), "wb") as f:
            f.write(data)

    def test_paths(self):
        self.assertEqual(ge.raw_path(5), os.path.join(ge.RAW_DIR, "5.json.gz"))
        self.assertEqual(ge.raw_path(5, partial=True),
                         os.path.join(ge.RAW_DIR, "5.partial.json.gz"))

    def test_no_dir_means_no_files(self):
        self.assertEqual(ge.raw_files(), [])
        self.assertEqual(ge.raw_cached_ids(), set())
        self.assertEqual(ge.raw_incomplete_ids(), set())
        self.assertEqual(ge.raw_partial_list_objects(), [])
        self.assertEqual(ge.raw_disk_bytes(), 0)

    def test_cached_ids_are_finished_captures_only(self):
        for name in ("1.json.gz", "22.json.gz", "3.partial.json.gz", "abc.json.gz",
                     "4.json.gz.tmp", "notes.txt"):
            self.put(name)
        self.assertEqual(sorted(ge.raw_files()), ["1.json.gz", "22.json.gz",
                                                  "3.partial.json.gz", "4.json.gz.tmp",
                                                  "abc.json.gz", "notes.txt"])
        self.assertEqual(ge.raw_cached_ids(), {1, 22})

    def test_disk_bytes_counts_every_json_gz_including_partials(self):
        self.put("1.json.gz", b"a" * 10)
        self.put("2.partial.json.gz", b"b" * 20)
        self.put("abc.json.gz", b"c" * 40)
        self.put("3.json.gz.tmp", b"d" * 100)
        self.put("notes.txt", b"e" * 1000)
        self.assertEqual(ge.raw_disk_bytes(), 70)

    def test_incomplete_ids_are_cached_files_holding_transient_failures_or_unreadable(self):
        cached(1)
        cached(2, errors={"weather": "GarminConnectConnectionError: reset"})
        cached(3, errors={"weather": "GarminConnectNotFoundError: 404"})
        cached(4, errors={"weather": "gave up"}, partial=True)       # not a cached id
        self.put("5.json.gz", b"garbage")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ge.raw_incomplete_ids(), {2, 5})
        self.assertEqual(err.getvalue().count("unreadable"), 1)
        self.assertIn("! raw: 5.json.gz unreadable (BadGzipFile) — will re-fetch", err.getvalue())

    def test_partial_list_objects_reads_only_partials_and_skips_unreadable_ones(self):
        cached(1)
        r2 = cached(2, "2014-02-02", {"weather": "gave up"}, partial=True)
        self.put("3.partial.json.gz", b"garbage")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ge.raw_partial_list_objects(), [r2["list_object"]])
        self.assertIn("! raw: 3.partial.json.gz unreadable (BadGzipFile) — "
                      "`raw init` will re-fetch it", err.getvalue())

    def test_write_round_trips_and_stringifies_what_json_cannot_hold(self):
        os.makedirs(ge.RAW_DIR)
        p = ge.raw_path(1)
        ge.raw_write(p, {"when": datetime(2026, 1, 2, 3, 4, 5), "n": [1, 2]})
        self.assertEqual(ge.raw_read(p), {"when": "2026-01-02 03:04:05", "n": [1, 2]})
        self.assertEqual(files(), ["1.json.gz"])

    def test_a_failed_write_leaves_the_previous_file_whole(self):
        os.makedirs(ge.RAW_DIR)
        p = ge.raw_path(1)
        ge.raw_write(p, {"v": "old"})
        loop = {}
        loop["self"] = loop
        with self.assertRaises(ValueError):
            ge.raw_write(p, {"v": "new", "loop": loop})
        self.assertEqual(ge.raw_read(p), {"v": "old"})
        self.assertEqual(files(), ["1.json.gz"])


if __name__ == "__main__":
    unittest.main()
