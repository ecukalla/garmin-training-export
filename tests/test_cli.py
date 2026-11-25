"""The command line as the owner runs it."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run_script(*args, prelude="", out=None):
    """Run garmin_export.py as `uv run garmin_export.py ...` would, against a temp data dir."""
    out = out or tempfile.mkdtemp()
    code = (f"import runpy, sys\n{prelude}\n"
            f"sys.argv = ['garmin_export.py', *{list(args)!r}]\n"
            "runpy.run_path('garmin_export.py', run_name='__main__')\n")
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, check=False,
                          text=True, env={**os.environ, "SWIM_OUTPUT_DIR": out})


class CtrlC(unittest.TestCase):
    def test_ctrl_c_exits_130_with_a_message_and_no_traceback(self):
        out = tempfile.mkdtemp()
        Path(out, "swim_export_state.json").write_text("{}")
        # status reads the swim state first; make that read behave like a Ctrl+C
        r = run_script("status", out=out, prelude=(
            "import json\n"
            "def ctrl_c(*a, **k): raise KeyboardInterrupt\n"
            "json.load = ctrl_c"))
        self.assertEqual(r.returncode, 130, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("interrupted", r.stderr)


if __name__ == "__main__":
    unittest.main()
