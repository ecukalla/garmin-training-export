"""Files on disk: whole or untouched, never half-written."""
import os
import tempfile
import unittest
from pathlib import Path

import garmin_export as ge


class AtomicWrite(unittest.TestCase):
    def test_an_interrupted_csv_write_leaves_the_previous_file_whole(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "whole.csv")
        cols = ["activity_id", "n"]
        ge.write_csv(p, cols, [{"activity_id": str(i), "n": i} for i in range(5000)])
        before = Path(p).read_bytes()

        def rows_then_ctrl_c():
            yield from ({"activity_id": "x", "n": i} for i in range(2000))
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            ge.write_csv(p, cols, rows_then_ctrl_c())
        self.assertEqual(Path(p).read_bytes(), before)
        self.assertEqual(os.listdir(d), ["whole.csv"])

    def test_state_and_csv_round_trip(self):
        d = tempfile.mkdtemp()
        ge.save_state(os.path.join(d, "s.json"), {"k": [1, 2]})
        self.assertEqual(ge.load_state(os.path.join(d, "s.json"), {}), {"k": [1, 2]})
        self.assertEqual(ge.load_state(os.path.join(d, "missing.json"), {"a": 1}), {"a": 1})
        ge.write_csv(os.path.join(d, "c.csv"), ["a", "b"], [{"a": 1}])
        self.assertEqual(ge.read_csv_list(os.path.join(d, "c.csv")), [{"a": "1", "b": ""}])
        self.assertEqual(ge.read_csv_by_key(os.path.join(d, "c.csv"), "a"),
                         {"1": {"a": "1", "b": ""}})
        self.assertEqual(ge.read_csv_list(os.path.join(d, "none.csv")), [])
        self.assertEqual(ge.read_csv_by_key(os.path.join(d, "none.csv"), "a"), {})


if __name__ == "__main__":
    unittest.main()
