"""The pure activity builders and their helpers.

swim_build / cycle_build / run_build turn one activity-list entry plus its splits response
into (child rows, summary row), which the exporter hands straight to write_csv. These tests
pin what lands in the CSVs today so the builders can be refactored safely: rows are written
with ge.write_csv and read back, so every assertion compares the text a CSV reader sees
("25.0" vs "25", a missing value as ""). All values are invented."""
import os
import unittest

import garmin_export as ge
from tests.stubs import StubGarmin, act, rows

VIEW_CSV = os.path.join(ge.OUTPUT_DIR, "test_builders_view.csv")


def as_csv(built, cols):
    """`built` rows as a CSV reader sees them once the exporter has written them."""
    ge.write_csv(VIEW_CSV, cols, built)
    return rows(VIEW_CSV)


def column(csv_rows, name):
    return [r[name] for r in csv_rows]


def present(**fields):
    """A Garmin DTO: keys Garmin didn't send are absent, not None."""
    return {k: v for k, v in fields.items() if v is not None}


# ---------------------------------------------------------------------------
# SWIM
# ---------------------------------------------------------------------------
def swum(dist=25.0, dur=30.0, stroke="FREESTYLE", swolf=45, strokes=15, hr=None,
         max_hr=None):
    """A swum pool length."""
    return present(distance=dist, duration=dur, swimStroke=stroke, averageSWOLF=swolf,
                   totalNumberOfStrokes=strokes, averageHR=hr, maxHR=max_hr)


def rest(dur, hr=None, max_hr=None):
    """A pause at the wall: Garmin logs it as a length with no distance key at all."""
    return present(duration=dur, averageHR=hr, maxHR=max_hr)


def laps(*lengths_per_lap):
    """A splits response: one lapDTO per argument, each holding those lengths."""
    return {"lapDTOs": [{"lengthDTOs": list(ls)} for ls in lengths_per_lap]}


def swim(**extra):
    return {"activityId": 9001, "startTimeLocal": "2031-04-05 06:45:00",
            "activityName": "Morning Pool",
            "activityType": {"typeKey": "lap_swimming", "typeId": 27, "parentTypeId": 26},
            **extra}


def swim_csv(splits, a=None):
    """(length rows, summary row) of one swim, as read back from the CSVs."""
    lengths, summary = ge.swim_build(a or swim(), splits)
    return as_csv(lengths, ge.SWIM_LENGTH_COLS), as_csv([summary], ge.SWIM_SUMMARY_COLS)[0]


class SwimLengthRow(unittest.TestCase):
    def test_a_length_after_a_rest_fills_every_column(self):
        lengths, _ = swim_csv(laps([
            rest(12.0, hr=118, max_hr=124),
            swum(25.0, 31.04, "FREESTYLE", swolf=46, strokes=15, hr=131, max_hr=137)]))
        self.assertEqual(lengths, [{
            "activity_id": "9001", "date": "2031-04-05", "activity_name": "Morning Pool",
            "type": "lap_swimming", "pool_length_m": "25.0", "set_index": "1",
            "length_in_set": "1", "global_length_n": "1", "distance_m": "25.0",
            "duration_s": "31.0", "pace_per_100m_s": "124.2", "stroke": "FREESTYLE",
            "swolf": "46", "strokes": "15", "avg_hr": "131", "max_hr": "137",
            "rest_before_set_s": "12.0", "rest_avg_hr": "118", "rest_max_hr": "124",
            "likely_glitch": "0"}])

    def test_values_garmin_did_not_send_are_blank(self):
        lengths, _ = swim_csv(laps([{"distance": 25.0, "duration": 28.0}]))
        row = lengths[0]
        self.assertEqual([row[k] for k in ("stroke", "swolf", "strokes", "avg_hr", "max_hr",
                                           "rest_avg_hr", "rest_max_hr")], [""] * 7)
        self.assertEqual((row["pace_per_100m_s"], row["rest_before_set_s"]), ("112.0", "0"))

    def test_rows_and_summary_have_exactly_the_csv_columns(self):
        lengths, summary = ge.swim_build(swim(), laps([swum()]))
        self.assertEqual(set(lengths[0]), set(ge.SWIM_LENGTH_COLS))
        self.assertEqual(set(summary), set(ge.SWIM_SUMMARY_COLS))


class SwimSets(unittest.TestCase):
    def test_a_rest_ends_a_set_and_the_next_length_starts_a_new_one(self):
        lengths, summary = swim_csv(laps([swum(), swum(), rest(20.0), swum(), swum(), swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "1", "2", "2", "2"])
        self.assertEqual(column(lengths, "length_in_set"), ["1", "2", "1", "2", "3"])
        self.assertEqual(column(lengths, "global_length_n"), ["1", "2", "3", "4", "5"])
        self.assertEqual((summary["num_sets"], summary["active_lengths"]), ("2", "5"))

    def test_lap_boundaries_do_not_split_a_set(self):
        lengths, summary = swim_csv(laps([swum(), swum()], [swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "1", "1"])
        self.assertEqual(column(lengths, "length_in_set"), ["1", "2", "3"])
        self.assertEqual(column(lengths, "global_length_n"), ["1", "2", "3"])
        self.assertEqual(summary["num_sets"], "1")

    def test_a_rest_at_the_end_of_a_lap_splits_the_set_across_laps(self):
        lengths, _ = swim_csv(laps([swum(), rest(15.0)], [swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "2"])
        self.assertEqual(column(lengths, "global_length_n"), ["1", "2"])

    def test_rest_before_set_is_on_the_set_first_length_only(self):
        lengths, _ = swim_csv(laps([swum(), swum(), rest(20.0), swum(), swum()]))
        self.assertEqual(column(lengths, "rest_before_set_s"), ["0", "0", "20.0", "0"])

    def test_a_rest_before_the_first_length_belongs_to_set_one(self):
        lengths, summary = swim_csv(laps([rest(8.5), swum(), swum()]))
        self.assertEqual(column(lengths, "rest_before_set_s"), ["8.5", "0"])
        self.assertEqual(column(lengths, "set_index"), ["1", "1"])
        self.assertEqual(summary["rest_time_s"], "8.5")

    def test_back_to_back_pauses_are_one_rest(self):
        lengths, summary = swim_csv(laps([swum(), rest(4.0), rest(11.0), swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "2"])
        self.assertEqual(column(lengths, "rest_before_set_s"), ["0", "15.0"])
        self.assertEqual(summary["num_sets"], "2")

    def test_rest_is_rounded_to_a_tenth(self):
        lengths, summary = swim_csv(laps([swum(), rest(12.34), swum()]))
        self.assertEqual(column(lengths, "rest_before_set_s"), ["0", "12.3"])
        self.assertEqual(summary["rest_time_s"], "12.3")

    def test_a_zero_distance_length_is_a_rest_like_a_missing_one(self):
        lengths, summary = swim_csv(laps([swum(), {"distance": 0.0, "duration": 9.0}, swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "2"])
        self.assertEqual(column(lengths, "rest_before_set_s"), ["0", "9.0"])
        self.assertEqual((summary["active_lengths"], summary["rest_time_s"]), ("2", "9.0"))

    def test_a_pause_without_duration_still_splits_the_set(self):
        lengths, summary = swim_csv(laps([swum(), {"averageHR": 120}, swum()]))
        self.assertEqual(column(lengths, "set_index"), ["1", "2"])
        self.assertEqual(column(lengths, "rest_before_set_s"), ["0", "0"])
        self.assertEqual(column(lengths, "rest_avg_hr"), ["", "120"])
        self.assertEqual(summary["rest_time_s"], "0.0")

    def test_a_rest_after_the_last_set_counts_only_in_rest_time(self):
        lengths, summary = swim_csv(laps([rest(5.0), swum(), rest(20.0), swum(), rest(40.0)]))
        self.assertEqual(column(lengths, "rest_before_set_s"), ["5.0", "20.0"])
        self.assertEqual((summary["rest_time_s"], summary["num_sets"]), ("65.0", "2"))


class SwimRestHeartRate(unittest.TestCase):
    def test_rest_hr_is_weighted_by_how_long_each_pause_lasted(self):
        lengths, _ = swim_csv(laps([swum(), swum(), rest(5.0, 150, 155), rest(15.0, 130, 140),
                                    swum(), swum()]))
        self.assertEqual(column(lengths, "rest_avg_hr"), ["", "", "135", ""])
        self.assertEqual(column(lengths, "rest_max_hr"), ["", "", "155", ""])

    def test_a_rest_without_hr_leaves_rest_hr_blank(self):
        lengths, _ = swim_csv(laps([swum(), rest(20.0), swum()]))
        self.assertEqual(column(lengths, "rest_avg_hr"), ["", ""])
        self.assertEqual(column(lengths, "rest_max_hr"), ["", ""])

    def test_rest_hr_with_an_average_but_no_max(self):
        lengths, _ = swim_csv(laps([swum(), rest(10.0, hr=112), swum()]))
        self.assertEqual(column(lengths, "rest_avg_hr"), ["", "112"])
        self.assertEqual(column(lengths, "rest_max_hr"), ["", ""])

    def test_rest_hr_does_not_carry_past_the_set_it_precedes(self):
        lengths, _ = swim_csv(laps([rest(10.0, 100, 105), swum(), rest(10.0), swum()]))
        self.assertEqual(column(lengths, "rest_avg_hr"), ["100", ""])


class RestHr(unittest.TestCase):
    """_rest_hr(samples): (avgHR, maxHR, duration) per pause → one (avg, max) pair."""

    def test_no_samples(self):
        self.assertEqual(ge._rest_hr([]), (None, None))

    def test_duration_weighted_average_and_overall_max(self):
        avg, mx = ge._rest_hr([(150, 155, 5.0), (130, 140, 15.0)])
        self.assertEqual((avg, mx), (135, 155))
        self.assertIsInstance(avg, int)

    def test_average_rounds_to_whole_beats(self):
        self.assertEqual(ge._rest_hr([(120, 121, 2.0), (131, 133, 1.0)]), (124, 133))

    def test_pauses_without_an_average_are_ignored_even_their_max(self):
        self.assertEqual(ge._rest_hr([(None, 170, 30.0), (120, 125, 10.0)]), (120, 125))
        self.assertEqual(ge._rest_hr([(None, 170, 30.0)]), (None, None))

    def test_zero_duration_pause_has_no_weight_but_its_max_counts(self):
        self.assertEqual(ge._rest_hr([(100, 160, 0.0), (140, 145, 10.0)]), (140, 160))

    def test_all_zero_durations_fall_back_to_a_plain_mean(self):
        self.assertEqual(ge._rest_hr([(120, None, 0.0), (130, None, 0.0)]), (125, None))

    def test_max_is_none_when_no_pause_logged_one(self):
        self.assertEqual(ge._rest_hr([(118, None, 12.0)]), (118, None))


class SwimPoolAndPace(unittest.TestCase):
    def test_pool_length_is_the_first_swum_length(self):
        lengths, summary = swim_csv(laps([rest(5.0), swum(25.0), swum(25.0), rest(10.0),
                                          swum(50.0, 60.0)]))
        self.assertEqual(column(lengths, "pool_length_m"), ["25.0", "25.0", "25.0"])
        self.assertEqual(column(lengths, "distance_m"), ["25.0", "25.0", "50.0"])
        self.assertEqual(summary["pool_length_m"], "25.0")

    def test_distances_are_written_as_garmin_sent_them(self):
        lengths, summary = swim_csv(laps([swum(22.86, 20.0)]))        # a 25-yard pool
        self.assertEqual((lengths[0]["pool_length_m"], lengths[0]["distance_m"]),
                         ("22.86", "22.86"))
        self.assertEqual(lengths[0]["pace_per_100m_s"], "87.5")
        self.assertEqual(summary["pool_length_m"], "22.86")
        whole, _ = swim_csv(laps([swum(25, 30.0)]))
        self.assertEqual((whole[0]["pool_length_m"], whole[0]["distance_m"]), ("25", "25"))

    def test_pace_per_100m_and_duration_round_to_a_tenth(self):
        lengths, _ = swim_csv(laps([swum(50.0, 47.34)]))
        self.assertEqual((lengths[0]["duration_s"], lengths[0]["pace_per_100m_s"]),
                         ("47.3", "94.7"))

    def test_sub_60s_per_100m_is_a_likely_glitch_60_is_not(self):
        lengths, _ = swim_csv(laps([swum(25.0, 14.9), swum(25.0, 15.0)]))
        self.assertEqual(column(lengths, "pace_per_100m_s"), ["59.6", "60.0"])
        self.assertEqual(column(lengths, "likely_glitch"), ["1", "0"])
        self.assertEqual(ge.SWIM_GLITCH_PACE, 60)

    def test_a_length_without_duration_has_no_pace_and_is_no_glitch(self):
        lengths, _ = swim_csv(laps([{"distance": 25.0, "swimStroke": "FREESTYLE"},
                                    {"distance": 25.0, "duration": 0}]))
        self.assertEqual(column(lengths, "duration_s"), ["0.0", "0.0"])
        self.assertEqual(column(lengths, "pace_per_100m_s"), ["", ""])
        self.assertEqual(column(lengths, "likely_glitch"), ["0", "0"])


class SwimSwolfAndStrokes(unittest.TestCase):
    def test_clean_swolf_skips_glitches_and_lengths_without_swolf(self):
        lengths, summary = swim_csv(laps([
            swum(swolf=40), swum(swolf=41), swum(dur=10.0, swolf=20),     # 3rd is a glitch
            swum(stroke="DRILL", swolf=None), swum(swolf=43)]))
        self.assertEqual(column(lengths, "swolf"), ["40", "41", "20", "", "43"])
        self.assertEqual(column(lengths, "likely_glitch"), ["0", "0", "1", "0", "0"])
        self.assertEqual(summary["avg_swolf_clean"], "41.3")

    def test_clean_swolf_blank_without_any_swolf(self):
        _, summary = swim_csv(laps([swum(swolf=None), swum(dur=10.0, swolf=30)]))
        self.assertEqual(summary["avg_swolf_clean"], "")

    def test_stroke_mix_lists_the_most_used_stroke_first(self):
        _, summary = swim_csv(laps([swum(stroke="BACKSTROKE"), swum(), swum(stroke="DRILL"),
                                    swum(), swum(stroke="DRILL"), swum()]))
        self.assertEqual(summary["stroke_mix"], "FREESTYLE:3; DRILL:2; BACKSTROKE:1")

    def test_stroke_mix_ties_keep_the_order_strokes_first_appeared(self):
        _, summary = swim_csv(laps([swum(stroke="BREASTSTROKE"), swum(stroke="FREESTYLE"),
                                    swum(stroke="FREESTYLE"), swum(stroke="BREASTSTROKE")]))
        self.assertEqual(summary["stroke_mix"], "BREASTSTROKE:2; FREESTYLE:2")

    def test_a_length_without_stroke_is_blank_and_not_tallied(self):
        lengths, summary = swim_csv(laps([swum(stroke=None), swum(stroke="BACKSTROKE")]))
        self.assertEqual(column(lengths, "stroke"), ["", "BACKSTROKE"])
        self.assertEqual(summary["stroke_mix"], "BACKSTROKE:1")

    def test_glitch_lengths_still_count_as_lengths_and_strokes(self):
        _, summary = swim_csv(laps([swum(dur=12.0, stroke="BUTTERFLY"), swum()]))
        self.assertEqual(summary["active_lengths"], "2")
        self.assertEqual(summary["stroke_mix"], "BUTTERFLY:1; FREESTYLE:1")

    def test_a_drill_block_is_its_own_set_with_averaged_times_and_no_swolf(self):
        drill = swum(25.0, 40.0, "DRILL", swolf=None, strokes=0)
        lengths, summary = swim_csv(laps([swum(), swum(), rest(30.0)],
                                         [drill, drill, drill, drill]))
        self.assertEqual(column(lengths, "set_index"), ["1", "1", "2", "2", "2", "2"])
        self.assertEqual(column(lengths, "stroke")[2:], ["DRILL"] * 4)
        self.assertEqual(column(lengths, "pace_per_100m_s")[2:], ["160.0"] * 4)
        self.assertEqual(column(lengths, "swolf")[2:], [""] * 4)
        self.assertEqual(column(lengths, "strokes")[2:], ["0"] * 4)
        self.assertEqual(summary["stroke_mix"], "DRILL:4; FREESTYLE:2")
        self.assertEqual(summary["avg_swolf_clean"], "45.0")


class SwimSummary(unittest.TestCase):
    def test_a_full_summary_row(self):
        a = swim(distance=1500.0, duration=2400.0, movingDuration=1980.0, averageHR=128.6,
                 maxHR=151, calories=310.0)
        _, summary = swim_csv(laps([swum(swolf=44), swum(swolf=46), rest(25.0)],
                                   [swum(stroke="BACKSTROKE", swolf=50), rest(10.0)]), a)
        self.assertEqual(summary, {
            "activity_id": "9001", "date": "2031-04-05", "activity_name": "Morning Pool",
            "type": "lap_swimming", "pool_length_m": "25.0", "total_distance_m": "1500.0",
            "active_lengths": "3", "total_duration_s": "2400.0",
            "moving_duration_s": "1980.0", "rest_time_s": "35.0",
            "avg_pace_moving_per_100m_s": "132.0", "avg_pace_elapsed_per_100m_s": "160.0",
            "avg_hr": "128.6", "max_hr": "151", "calories": "310.0",
            "avg_swolf_clean": "46.7", "num_sets": "2",
            "stroke_mix": "FREESTYLE:2; BACKSTROKE:1"})

    def test_totals_and_paces_round_to_a_tenth(self):
        a = swim(distance=1234.56, duration=2000.04, movingDuration=1500.06)
        _, summary = swim_csv(laps([swum()]), a)
        self.assertEqual([summary[k] for k in (
            "total_distance_m", "total_duration_s", "moving_duration_s",
            "avg_pace_moving_per_100m_s", "avg_pace_elapsed_per_100m_s")],
            ["1234.6", "2000.0", "1500.1", "121.5", "162.0"])

    def test_activity_totals_garmin_did_not_send_are_blank(self):
        _, summary = swim_csv(laps([swum()]))
        self.assertEqual([summary[k] for k in (
            "total_distance_m", "total_duration_s", "moving_duration_s",
            "avg_pace_moving_per_100m_s", "avg_pace_elapsed_per_100m_s",
            "avg_hr", "max_hr", "calories")], [""] * 8)

    def test_zero_distance_shows_as_zero_but_leaves_the_paces_blank(self):
        _, summary = swim_csv({}, swim(distance=0.0, duration=600.0, movingDuration=0))
        self.assertEqual([summary[k] for k in (
            "total_distance_m", "total_duration_s", "moving_duration_s",
            "avg_pace_moving_per_100m_s", "avg_pace_elapsed_per_100m_s")],
            ["0.0", "600.0", "", "", ""])

    def test_a_swim_with_no_splits(self):
        lengths, summary = ge.swim_build(swim(distance=800.0), {})
        self.assertEqual(lengths, [])
        self.assertEqual(as_csv([summary], ge.SWIM_SUMMARY_COLS)[0], {
            "activity_id": "9001", "date": "2031-04-05", "activity_name": "Morning Pool",
            "type": "lap_swimming", "pool_length_m": "", "total_distance_m": "800.0",
            "active_lengths": "0", "total_duration_s": "", "moving_duration_s": "",
            "rest_time_s": "0.0", "avg_pace_moving_per_100m_s": "",
            "avg_pace_elapsed_per_100m_s": "", "avg_hr": "", "max_hr": "", "calories": "",
            "avg_swolf_clean": "", "num_sets": "0", "stroke_mix": ""})

    def test_laps_without_lengths_give_no_length_rows(self):
        """Open water: laps but no pool lengths."""
        for splits in ({"lapDTOs": None}, {"lapDTOs": []},
                       {"lapDTOs": [{"distance": 500.0, "duration": 600.0},
                                    {"lengthDTOs": None}, {"lengthDTOs": []}]}):
            with self.subTest(splits=splits):
                lengths, summary = swim_csv(splits)
                self.assertEqual(lengths, [])
                self.assertEqual((summary["active_lengths"], summary["num_sets"],
                                  summary["pool_length_m"]), ("0", "0", ""))

    def test_a_swim_of_only_rests(self):
        lengths, summary = swim_csv(laps([rest(10.0, 90, 95), rest(5.5)]))
        self.assertEqual(lengths, [])
        self.assertEqual((summary["rest_time_s"], summary["num_sets"], summary["pool_length_m"]),
                         ("15.5", "0", ""))


# ---------------------------------------------------------------------------
# Shared columns
# ---------------------------------------------------------------------------
class Head(unittest.TestCase):
    def test_the_four_leading_columns(self):
        a = {"activityId": 77, "startTimeLocal": "2031-01-02 18:30:00",
             "activityName": "Evening Ride", "activityType": {"typeKey": "road_biking"}}
        self.assertEqual(ge._head(a), {"activity_id": 77, "date": "2031-01-02",
                                       "activity_name": "Evening Ride", "type": "road_biking"})

    def test_missing_name_and_start_time_are_blank(self):
        for extra in ({}, {"activityName": None, "startTimeLocal": None}):
            with self.subTest(extra=extra):
                a = {"activityId": 78, "activityType": {"typeKey": "running"}, **extra}
                self.assertEqual(ge._head(a), {"activity_id": 78, "date": "",
                                               "activity_name": "", "type": "running"})

    def test_type_keeps_garmin_spelling(self):
        a = {"activityId": 79, "activityType": {"typeKey": "Lap_Swimming"}}
        self.assertEqual(ge._head(a)["type"], "Lap_Swimming")

    def test_an_activity_type_without_type_key_raises(self):
        with self.assertRaises(KeyError):
            ge._head({"activityId": 80, "activityType": {"typeId": 2}})


HEAD = {"activity_id": 4242, "date": "2031-06-07", "activity_name": "Hill Loop",
        "type": "road_biking"}


class LapBase(unittest.TestCase):
    def test_every_shared_lap_column(self):
        L = {"distance": 5012.34, "duration": 900.06, "movingDuration": 890.0,
             "averageSpeed": 5.569, "averageHR": 141.6, "maxHR": 158, "averagePower": 181.6,
             "maxPower": 402, "normalizedPower": 190.4, "elevationGain": 42.06,
             "calories": 205.0, "intensityType": "ACTIVE"}
        self.assertEqual(ge.lap_base(HEAD, 3, L), {
            **HEAD, "lap_index": 3, "distance_m": 5012.3, "duration_s": 900.1,
            "moving_duration_s": 890.0, "avg_speed_kmh": 20.0, "avg_hr": 141.6,
            "max_hr": 158, "avg_power_w": 182.0, "max_power_w": 402,
            "normalized_power_w": 190.0, "elevation_gain_m": 42.1, "calories": 205.0,
            "intensity_type": "ACTIVE"})

    def test_an_empty_lap(self):
        self.assertEqual(ge.lap_base(HEAD, 1, {}), {
            **HEAD, "lap_index": 1, "distance_m": "", "duration_s": "",
            "moving_duration_s": "", "avg_speed_kmh": "", "avg_hr": "", "max_hr": "",
            "avg_power_w": "", "max_power_w": "", "normalized_power_w": "",
            "elevation_gain_m": "", "calories": "", "intensity_type": ""})

    def test_null_values_from_garmin(self):
        L = dict.fromkeys(("distance", "averageSpeed", "averageHR", "maxHR", "averagePower",
                           "calories", "intensityType"))
        base = ge.lap_base(HEAD, 1, L)
        self.assertEqual([base[k] for k in ("distance_m", "avg_speed_kmh", "avg_power_w",
                                            "intensity_type")], ["", "", "", ""])
        # passed through as None, which the CSV writes as blank
        self.assertEqual([base[k] for k in ("avg_hr", "max_hr", "calories")], [None] * 3)


class SummaryBase(unittest.TestCase):
    def test_every_shared_summary_column(self):
        a = {"distance": 40123.0, "duration": 5400.26, "movingDuration": 5300.0,
             "elevationGain": 512.0, "elevationLoss": 498.66, "averageSpeed": 7.43,
             "averageHR": 138.4, "maxHR": 171.0, "avgPower": 176.3, "maxPower": 655,
             "normPower": 189.9, "calories": 1123.6, "aerobicTrainingEffect": 3.24,
             "anaerobicTrainingEffect": 0.96}
        self.assertEqual(ge.summary_base(HEAD, a, 4), {
            **HEAD, "distance_km": 40.12, "duration_s": 5400.3, "moving_duration_s": 5300.0,
            "elevation_gain_m": 512.0, "elevation_loss_m": 498.7, "avg_speed_kmh": 26.7,
            "avg_hr": 138.0, "max_hr": 171.0, "avg_power_w": 176.0, "max_power_w": 655,
            "normalized_power_w": 190.0, "calories": 1124.0, "aerobic_te": 3.2,
            "anaerobic_te": 1.0, "num_laps": 4})

    def test_an_activity_with_no_metrics(self):
        self.assertEqual(ge.summary_base(HEAD, {}, 0), {
            **HEAD, "distance_km": "", "duration_s": "", "moving_duration_s": "",
            "elevation_gain_m": "", "elevation_loss_m": "", "avg_speed_kmh": "",
            "avg_hr": "", "max_hr": "", "avg_power_w": "", "max_power_w": "",
            "normalized_power_w": "", "calories": "", "aerobic_te": "", "anaerobic_te": "",
            "num_laps": 0})

    def test_zero_distance_is_blank(self):
        self.assertEqual(ge.summary_base(HEAD, {"distance": 0.0}, 0)["distance_km"], "")

    def test_lap_keys_are_not_summary_keys(self):
        """Laps say averagePower/normalizedPower, the activity avgPower/normPower."""
        a = {"averagePower": 200, "normalizedPower": 210}
        s = ge.summary_base(HEAD, a, 0)
        self.assertEqual((s["avg_power_w"], s["normalized_power_w"]), ("", ""))


# ---------------------------------------------------------------------------
# CYCLE
# ---------------------------------------------------------------------------
def ride(**extra):
    return {"activityId": 4242, "startTimeLocal": "2031-06-07 09:10:00",
            "activityName": "Hill Loop",
            "activityType": {"typeKey": "road_biking", "typeId": 10, "parentTypeId": 2},
            **extra}


RIDE_LAP = {"distance": 5012.34, "duration": 900.06, "movingDuration": 890.0,
            "averageSpeed": 5.569, "maxSpeed": 11.2, "averageHR": 141, "maxHR": 158,
            "averagePower": 181.6, "maxPower": 402, "normalizedPower": 190.4,
            "averageBikeCadence": 87.4, "maxBikeCadence": 110.0, "elevationGain": 42.06,
            "calories": 205.0, "intensityType": "ACTIVE", "averageTemperature": 20.0}


class CycleBuild(unittest.TestCase):
    def test_one_row_per_lap(self):
        laps_, _ = ge.cycle_build(ride(), {"lapDTOs": [RIDE_LAP, {"distance": 1000.0}]})
        got = as_csv(laps_, ge.CYCLE_LAP_COLS)
        self.assertEqual(got[0], {
            "activity_id": "4242", "date": "2031-06-07", "activity_name": "Hill Loop",
            "type": "road_biking", "lap_index": "1", "distance_m": "5012.3",
            "duration_s": "900.1", "moving_duration_s": "890.0", "avg_speed_kmh": "20.0",
            "max_speed_kmh": "40.3", "avg_hr": "141", "max_hr": "158",
            "avg_power_w": "182.0", "max_power_w": "402", "normalized_power_w": "190.0",
            "avg_cadence_rpm": "87.0", "max_cadence_rpm": "110.0",
            "elevation_gain_m": "42.1", "calories": "205.0", "intensity_type": "ACTIVE"})
        self.assertEqual(got[1]["lap_index"], "2")
        self.assertEqual(got[1]["distance_m"], "1000.0")
        self.assertEqual([v for k, v in got[1].items() if k not in (
            "activity_id", "date", "activity_name", "type", "lap_index", "distance_m")],
            [""] * 14)

    def test_summary_row(self):
        a = ride(distance=40123.0, duration=5400.26, movingDuration=5300.0,
                 elevationGain=512.0, elevationLoss=498.66, averageSpeed=7.43,
                 maxSpeed=15.01, averageHR=138.4, maxHR=171.0, avgPower=176.3,
                 maxPower=655.0, normPower=189.9, calories=1123.6,
                 aerobicTrainingEffect=3.24, anaerobicTrainingEffect=0.96,
                 averageBikingCadenceInRevPerMinute=84.2,
                 maxBikingCadenceInRevPerMinute=121.0)
        _, summary = ge.cycle_build(a, {"lapDTOs": [
            {**RIDE_LAP, "averageTemperature": 20.0, "duration": 600.0},
            {**RIDE_LAP, "averageTemperature": 26.0, "duration": 1800.0}]})
        self.assertEqual(as_csv([summary], ge.CYCLE_SUMMARY_COLS)[0], {
            "activity_id": "4242", "date": "2031-06-07", "activity_name": "Hill Loop",
            "type": "road_biking", "distance_km": "40.12", "duration_s": "5400.3",
            "moving_duration_s": "5300.0", "elevation_gain_m": "512.0",
            "elevation_loss_m": "498.7", "avg_speed_kmh": "26.7", "max_speed_kmh": "54.0",
            "avg_hr": "138.0", "max_hr": "171.0", "avg_cadence_rpm": "84.0",
            "max_cadence_rpm": "121.0", "avg_power_w": "176.0", "max_power_w": "655.0",
            "normalized_power_w": "190.0", "calories": "1124.0", "avg_temp_c": "24.5",
            "aerobic_te": "3.2", "anaerobic_te": "1.0", "num_laps": "2"})

    def test_temperature_ignores_laps_without_a_reading_or_a_duration(self):
        _, summary = ge.cycle_build(ride(), {"lapDTOs": [
            {"averageTemperature": 18.04, "duration": 300.0},
            {"averageTemperature": 30.0, "duration": 0},
            {"averageTemperature": 30.0},
            {"averageTemperature": None, "duration": 900.0},
            {"averageTemperature": "30", "duration": 900.0},
            {"duration": 900.0}]})
        self.assertEqual((summary["avg_temp_c"], summary["num_laps"]), (18.0, 6))

    def test_a_ride_with_no_laps(self):
        for splits in ({}, {"lapDTOs": None}, {"lapDTOs": []}):
            with self.subTest(splits=splits):
                laps_, summary = ge.cycle_build(ride(distance=1500.0), splits)
                self.assertEqual(laps_, [])
                got = as_csv([summary], ge.CYCLE_SUMMARY_COLS)[0]
                self.assertEqual((got["num_laps"], got["avg_temp_c"], got["distance_km"]),
                                 ("0", "", "1.5"))

    def test_whole_number_power_stays_whole(self):
        laps_, _ = ge.cycle_build(ride(), {"lapDTOs": [{"averagePower": 182,
                                                         "averageBikeCadence": 88}]})
        got = as_csv(laps_, ge.CYCLE_LAP_COLS)[0]
        self.assertEqual((got["avg_power_w"], got["avg_cadence_rpm"]), ("182", "88"))

    def test_rows_and_summary_have_exactly_the_csv_columns(self):
        laps_, summary = ge.cycle_build(ride(), {"lapDTOs": [RIDE_LAP]})
        self.assertEqual(set(laps_[0]), set(ge.CYCLE_LAP_COLS))
        self.assertEqual(set(summary), set(ge.CYCLE_SUMMARY_COLS))


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
def run(**extra):
    return {"activityId": 5150, "startTimeLocal": "2031-08-09 06:00:00",
            "activityName": "Tempo",
            "activityType": {"typeKey": "running", "typeId": 1, "parentTypeId": 17}, **extra}


RUN_LAP = {"distance": 1000.0, "duration": 268.4, "movingDuration": 268.0,
           "averageSpeed": 3.7258, "averageHR": 150, "maxHR": 162, "averagePower": 262.7,
           "maxPower": 330, "normalizedPower": 265.2, "averageRunCadence": 172.3,
           "maxRunCadence": 181.0, "strideLength": 130.04, "groundContactTime": 239.96,
           "verticalOscillation": 8.77, "elevationGain": 4.0, "calories": 70.0,
           "intensityType": "INTERVAL"}


class RunBuild(unittest.TestCase):
    def test_one_row_per_lap(self):
        laps_, _ = ge.run_build(run(), {"lapDTOs": [RUN_LAP, {"distance": 400.0}]})
        got = as_csv(laps_, ge.RUN_LAP_COLS)
        self.assertEqual(got[0], {
            "activity_id": "5150", "date": "2031-08-09", "activity_name": "Tempo",
            "type": "running", "lap_index": "1", "distance_m": "1000.0",
            "duration_s": "268.4", "moving_duration_s": "268.0", "pace_per_km": "4:28",
            "avg_speed_kmh": "13.4", "avg_hr": "150", "max_hr": "162",
            "avg_power_w": "263.0", "max_power_w": "330", "normalized_power_w": "265.0",
            "avg_cadence_spm": "172.0", "max_cadence_spm": "181.0",
            "stride_length_cm": "130.0", "ground_contact_ms": "240.0",
            "vertical_oscillation_cm": "8.8", "elevation_gain_m": "4.0",
            "calories": "70.0", "intensity_type": "INTERVAL"})
        self.assertEqual((got[1]["lap_index"], got[1]["distance_m"], got[1]["pace_per_km"]),
                         ("2", "400.0", ""))

    def test_summary_row(self):
        a = run(distance=10000.0, duration=2700.0, movingDuration=2690.0,
                elevationGain=35.0, elevationLoss=33.0, averageSpeed=3.7037, maxSpeed=5.2,
                averageHR=151.2, maxHR=176, avgPower=260.4, maxPower=410, normPower=268.0,
                calories=702.4, aerobicTrainingEffect=3.8, anaerobicTrainingEffect=1.2,
                averageRunningCadenceInStepsPerMinute=171.6,
                maxRunningCadenceInStepsPerMinute=190.0, avgStrideLength=129.73,
                avgGroundContactTime=241.36, avgVerticalOscillation=8.84,
                avgVerticalRatio=6.81, trainingEffectLabel="TEMPO")
        _, summary = ge.run_build(a, {"lapDTOs": [RUN_LAP] * 10})
        self.assertEqual(as_csv([summary], ge.RUN_SUMMARY_COLS)[0], {
            "activity_id": "5150", "date": "2031-08-09", "activity_name": "Tempo",
            "type": "running", "distance_km": "10.0", "duration_s": "2700.0",
            "moving_duration_s": "2690.0", "avg_pace_per_km": "4:30",
            "avg_speed_kmh": "13.3", "elevation_gain_m": "35.0", "elevation_loss_m": "33.0",
            "avg_hr": "151.0", "max_hr": "176", "avg_cadence_spm": "172.0",
            "max_cadence_spm": "190.0", "avg_stride_length_cm": "129.7",
            "avg_power_w": "260.0", "max_power_w": "410", "normalized_power_w": "268.0",
            "avg_ground_contact_ms": "241.4", "avg_vertical_oscillation_cm": "8.8",
            "avg_vertical_ratio": "6.8", "calories": "702.0", "aerobic_te": "3.8",
            "anaerobic_te": "1.2", "training_effect_label": "TEMPO", "num_laps": "10"})

    def test_a_run_with_no_laps_or_metrics(self):
        laps_, summary = ge.run_build(run(), {"lapDTOs": None})
        self.assertEqual(laps_, [])
        got = as_csv([summary], ge.RUN_SUMMARY_COLS)[0]
        self.assertEqual(got["num_laps"], "0")
        self.assertEqual([v for k, v in got.items() if k not in (
            "activity_id", "date", "activity_name", "type", "num_laps")], [""] * 22)

    def test_a_standing_lap_has_no_pace(self):
        laps_, _ = ge.run_build(run(), {"lapDTOs": [{"averageSpeed": 0.0}]})
        got = as_csv(laps_, ge.RUN_LAP_COLS)[0]
        self.assertEqual((got["pace_per_km"], got["avg_speed_kmh"]), ("", "0.0"))

    def test_rows_and_summary_have_exactly_the_csv_columns(self):
        laps_, summary = ge.run_build(run(), {"lapDTOs": [RUN_LAP]})
        self.assertEqual(set(laps_[0]), set(ge.RUN_LAP_COLS))
        self.assertEqual(set(summary), set(ge.RUN_SUMMARY_COLS))


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
class Formatters(unittest.TestCase):
    def check(self, fn, cases):
        for arg, want in cases:
            with self.subTest(fn=fn.__name__, arg=arg):
                self.assertEqual(fn(arg), want)
                self.assertIs(type(fn(arg)), type(want))

    def test_fmt_time(self):
        self.check(ge.fmt_time, [
            (0, "0:00"), (7, "0:07"), (59.4, "0:59"), (59.6, "1:00"), (61, "1:01"),
            (754.0, "12:34"), (3599.6, "1:00:00"), (3725, "1:02:05"),
            (36000, "10:00:00"), (90061, "25:01:01"),
            (2.5, "0:02"), (3.5, "0:04"),                   # round half to even
            (-5, "-1:59:55"),                               # negative wraps oddly
            (None, ""), ("75", ""), ([], "")])

    def test_mins(self):
        self.check(ge.mins, [(0, 0), (29, 0), (45, 1), (90, 2), (150, 2), (3600.0, 60),
                             (None, ""), ("60", "")])

    def test_kmh(self):
        self.check(ge.kmh, [(10, 36.0), (2.777, 10.0), (5.569, 20.0), (0, 0.0), (0.0, 0.0),
                            (None, ""), ("3", "")])

    def test_pace_per_km(self):
        self.check(ge.pace_per_km, [(4.0, "4:10"), (3.7037, "4:30"), (1, "16:40"),
                                    (0.5, "33:20"), (0.25, "1:06:40"), (0, ""), (0.0, ""),
                                    (-3.0, ""), (None, ""), ("4", "")])

    def test_num(self):
        d = {"f": 12.345, "i": 7, "big": 181.6, "z": 0, "none": None, "s": "3.2",
             "neg": -1.26}
        for key, nd, want in [("f", 1, 12.3), ("f", 2, 12.35), ("i", 1, 7), ("big", 0, 182.0),
                              ("i", 0, 7), ("z", 1, 0), ("neg", 1, -1.3), ("none", 1, ""),
                              ("s", 1, ""), ("missing", 1, "")]:
            with self.subTest(key=key, nd=nd):
                self.assertEqual(ge._num(d, key, nd), want)
                self.assertIs(type(ge._num(d, key, nd)), type(want))
        self.assertEqual(ge._num({"v": 2.25}, "v"), 2.2)               # default: a tenth

    def test_typekey(self):
        for a, want in [({"activityType": {"typeKey": "Lap_Swimming"}}, "lap_swimming"),
                        ({"activityType": {"typeKey": "running"}}, "running"),
                        ({"activityType": {"typeKey": None}}, ""),
                        ({"activityType": {}}, ""), ({"activityType": None}, ""), ({}, "")]:
            with self.subTest(a=a):
                self.assertEqual(ge._typekey(a), want)


# ---------------------------------------------------------------------------
# Which activities each domain keeps
# ---------------------------------------------------------------------------
def typed(aid, day, **activity_type):
    return act(aid, day, activityType=activity_type or None)


class Selection(unittest.TestCase):
    def ids(self, lister, acts, start="2031-01-01", end="2031-12-31"):
        return [a["activityId"] for a in lister(StubGarmin(acts), start, end)]

    def test_swim_keeps_every_swim_type_key_oldest_first(self):
        acts = [act(1, "2031-03-04", "open_water_swimming"),
                act(2, "2031-03-01", "lap_swimming"),
                act(3, "2031-03-02", "Lap_Swimming"),
                act(4, "2031-03-03", "running"),
                act(5, "2031-03-05", "swimrun"),
                typed(6, "2031-03-06"), typed(7, "2031-03-07", typeId=27),
                act(8, "2030-12-31", "lap_swimming")]                 # outside the range
        self.assertEqual(self.ids(ge.swim_list, acts), [2, 3, 1, 5])

    def test_run_keeps_every_run_type_key_oldest_first(self):
        acts = [act(1, "2031-05-03", "trail_running"), act(2, "2031-05-01", "running"),
                act(3, "2031-05-02", "Treadmill_Running"), act(4, "2031-05-04", "track_running"),
                act(5, "2031-05-05", "walking"), act(6, "2031-05-06", "hiking"),
                act(7, "2031-05-07", "lap_swimming"), act(8, "2031-05-08", "swimrun"),
                typed(9, "2031-05-09"), typed(10, "2031-05-10", typeId=1)]
        self.assertEqual(self.ids(ge.run_list, acts), [2, 3, 1, 4, 8])

    def test_cycle_keeps_cycling_and_its_children_by_type_id_not_name(self):
        acts = [typed(1, "2031-06-01", typeKey="cycling", typeId=2, parentTypeId=17),
                typed(2, "2031-06-02", typeKey="road_biking", typeId=10, parentTypeId=2),
                typed(3, "2031-06-03", typeKey="enduro_mtb", typeId=301, parentTypeId=2),
                typed(4, "2031-06-04", typeKey="virtual_ride", typeId=152, parentTypeId=2),
                typed(5, "2031-06-05", typeKey="motorcycling", typeId=302, parentTypeId=4),
                typed(6, "2031-06-06", typeKey="cycling"),               # no ids: name is ignored
                typed(7, "2031-06-07", typeKey="running", typeId=1, parentTypeId=17),
                typed(8, "2031-06-08", typeKey="cycling", typeId="2", parentTypeId=17),
                typed(9, "2031-06-09")]
        self.assertEqual(self.ids(ge.cycle_list, acts), [1, 2, 3, 4])

    def test_same_day_activities_sort_by_start_time(self):
        acts = [act(1, "2031-07-01", "running", startTimeLocal="2031-07-01 18:00:00"),
                act(2, "2031-07-01", "running", startTimeLocal="2031-07-01 06:30:00")]
        self.assertEqual(self.ids(ge.run_list, acts), [2, 1])


if __name__ == "__main__":
    unittest.main()
