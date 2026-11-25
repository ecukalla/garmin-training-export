"""activity_run — the incremental loop swim, cycle and run share."""
import contextlib
import io
import unittest

import garmin_export as ge
from tests.stubs import StubGarmin, act, rows, spec


def swim_spec(tag):
    """SWIM with a trivial builder: one length row + one summary row per activity."""
    d = spec(ge.SWIM, tag)
    d["build"] = lambda a, splits: (
        [{"activity_id": str(a["activityId"]), "date": a["startTimeLocal"][:10],
          d["part_order"]: 1}],
        {"activity_id": str(a["activityId"]), "date": a["startTimeLocal"][:10]})
    return d


def status_line(d):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ge.activity_run(None, "status", d)
    return out.getvalue()


class Init(unittest.TestCase):
    def test_init_finds_activities_older_than_any_configured_year(self):
        d = spec(ge.SWIM, "old")
        ge.save_state(d["state"], {"start_date": "2016-01-01", "last_updated": None,
                                   "processed_ids": [], "num_swims": 0, "num_length_rows": 0})
        ge.activity_run(StubGarmin([act(1, "2009-05-03"), act(2, "2019-07-07")]), "init", d)

        self.assertEqual([r["activity_id"] for r in rows(d["summary_csv"])], ["1", "2"])
        self.assertEqual(ge.load_state(d["state"], {})["start_date"], "2009-05-03")

    def test_init_on_a_domain_with_data_rebuilds_from_scratch(self):
        """init forgets what an earlier run saved: an activity no longer on Garmin is gone
        and everything listed is fetched again."""
        d = swim_spec("rebuild")
        ge.activity_run(StubGarmin([act(1, "2020-01-01"), act(2, "2021-01-01")]), "init", d)
        ge.activity_run(StubGarmin([act(2, "2021-01-01")]), "init", d)
        self.assertEqual([r["activity_id"] for r in rows(d["summary_csv"])], ["2"])
        self.assertEqual(ge.load_state(d["state"], {})["processed_ids"], ["2"])

    def test_auto_on_a_fresh_domain_is_init(self):
        d = swim_spec("auto")
        ge.activity_run(StubGarmin([act(5, "2012-01-01")]), "auto", d)
        self.assertEqual([r["activity_id"] for r in rows(d["summary_csv"])], ["5"])


class Update(unittest.TestCase):
    def test_update_corrects_a_guessed_start_date_from_the_data(self):
        d = spec(ge.SWIM, "upd")
        ge.write_csv(d["summary_csv"], d["summary_cols"],
                     [{"activity_id": "7", "date": "2019-07-07"}])
        ge.write_csv(d["parts_csv"], d["parts_cols"], [])
        ge.save_state(d["state"], {"start_date": "2016-01-01", "last_updated": "2026-11-20",
                                   "processed_ids": [7], "num_swims": 1, "num_length_rows": 0})

        ge.activity_run(StubGarmin([]), "update", d)

        self.assertEqual(ge.load_state(d["state"], {})["start_date"], "2019-07-07")
        self.assertIn("first 2019-07-07", status_line(d))

    def test_rows_for_an_activity_the_state_never_recorded_are_not_duplicated(self):
        """A Ctrl+C between writing the CSVs and the state leaves rows for an activity the
        state doesn't know; the next update fetches it again and must not double it."""
        d = swim_spec("dup")
        for key, cols in (("summary_csv", d["summary_cols"]), ("parts_csv", d["parts_cols"])):
            ge.write_csv(d[key], cols, [{"activity_id": "1", "date": "2026-11-01"},
                                        {"activity_id": "2", "date": "2026-11-02"}])
        ge.save_state(d["state"], {"start_date": "2026-01-01", "last_updated": "2026-11-01",
                                   "processed_ids": [1], "num_swims": 1, "num_length_rows": 1})

        ge.activity_run(StubGarmin([act(1, "2026-11-01"), act(2, "2026-11-02")]), "update", d)

        for key in ("summary_csv", "parts_csv"):
            self.assertEqual([r["activity_id"] for r in rows(d[key])], ["1", "2"], key)

    def test_a_transient_failure_is_retried_next_run_not_saved_as_empty(self):
        d = swim_spec("retry")
        flaky = StubGarmin([act(3, "2026-11-03")],
                           get_activity_splits=ge.GarminConnectTooManyRequestsError("429"))
        ge.activity_run(flaky, "init", d)

        state = ge.load_state(d["state"], {})
        self.assertEqual(state["failed"], {"3": "2026-11-03"})
        self.assertEqual(rows(d["summary_csv"]), [])
        self.assertIn("1 awaiting retry", status_line(d))

        ge.activity_run(StubGarmin([act(3, "2026-11-03")]), "update", d)
        self.assertEqual([r["activity_id"] for r in rows(d["summary_csv"])], ["3"])
        self.assertEqual(ge.load_state(d["state"], {})["failed"], {})

    def test_a_permanent_failure_is_saved_with_no_splits(self):
        d = swim_spec("404")
        gone = StubGarmin([act(4, "2026-11-04")],
                          get_activity_splits=ge.GarminConnectNotFoundError("404"))
        ge.activity_run(gone, "init", d)
        self.assertEqual(ge.load_state(d["state"], {})["processed_ids"], ["4"])


class SplitsReachTheBuilder(unittest.TestCase):
    def test_init_builds_each_swim_from_its_splits(self):
        d = spec(ge.SWIM, "splits")
        lengths = [{"distance": 25.0, "duration": 30.0}, {"distance": 25.0, "duration": 31.0}]
        g = StubGarmin([act(8, "2026-05-06")],
                       get_activity_splits={"lapDTOs": [{"lengthDTOs": lengths}]})
        ge.activity_run(g, "init", d)
        self.assertEqual([r["duration_s"] for r in rows(d["parts_csv"])], ["30.0", "31.0"])
        self.assertEqual(rows(d["summary_csv"])[0]["active_lengths"], "2")


class Status(unittest.TestCase):
    def test_status_before_any_run(self):
        self.assertIn("0 swims, 0 length rows  (last never)", status_line(spec(ge.SWIM, "never")))


    def test_status_of_a_state_written_before_part_rows_were_counted(self):
        d = spec(ge.SWIM, "legacy")
        ge.save_state(d["state"], {"start_date": "2020-01-01", "last_updated": "2026-01-01",
                                   "processed_ids": [1], "num_swims": 1})
        self.assertIn("1 swims, 0 length rows", status_line(d))

if __name__ == "__main__":
    unittest.main()
