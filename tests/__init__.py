"""Offline test suite: `uv run python -m unittest` from the repo root.

Importing garmin_export creates its output directory and fixes every path, so this package
points SWIM_OUTPUT_DIR at a throwaway directory before any test module imports it. No test
logs in or touches the real data/ — Garmin is replaced by stub clients."""
import os
import tempfile

os.environ["SWIM_OUTPUT_DIR"] = tempfile.mkdtemp(prefix="garmin-export-tests-")
