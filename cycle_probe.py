"""
One-shot probe to discover how Garmin stores structured-workout targets (% FTP,
step names) for your rides. Reads only — fetches a few endpoints for ONE recent
ride and dumps the raw JSON so we can see the real field shapes.

USAGE:
  uv run cycle_probe.py                 # newest ride
  uv run cycle_probe.py 12345678901     # a specific activity id

Outputs raw JSON into ./data/_probe/ and prints a focused summary of any
power / target / FTP / step keys it finds. Paste the summary back (or send the
files) and I'll write the parser against the actual structure.
"""
import os
import sys
import json
from datetime import date, timedelta

import garmin_export as gx      # same login, data dir and ride selection as the exporter

OUT_DIR = gx.path("_probe")


def dump(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    with open(p, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    print(f"  wrote {p}")


def find_keys(obj, needles, path=""):
    """Walk a nested dict/list and report paths whose key matches any needle."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else k
            if any(n in k.lower() for n in needles) and not isinstance(v, (dict, list)):
                hits.append((kp, v))
            hits.extend(find_keys(v, needles, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):           # sample first few list items
            hits.extend(find_keys(v, needles, f"{path}[{i}]"))
    return hits


def pick_ride(g, explicit):
    if explicit:
        return explicit, g.get_activity(explicit)
    # newest cycling activity in the last ~120 days
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=120)).isoformat()
    rides = gx.cycle_list(g, start, end)          # oldest first
    if not rides:
        sys.exit("No cycling activities found in the last 120 days. "
                 "Pass an activity id explicitly: uv run cycle_probe.py <id>")
    return rides[-1]["activityId"], rides[-1]


def main(argv):
    explicit = argv[0] if argv else None
    g = gx.login()
    print("authenticated\n")

    aid, summary_obj = pick_ride(g, explicit)
    name = (summary_obj.get("activityName") or "")
    when = (summary_obj.get("startTimeLocal") or "")[:10]
    print(f"Ride: {aid}  {when}  {name!r}\n")

    # the base activity object (where a workoutId, if any, lives); pick_ride has
    # already fetched it when given an id
    base = summary_obj if explicit else g.get_activity(aid)
    dump("activity.json", base)

    # the splits (per-lap execution data we already export)
    try:
        dump("splits.json", g.get_activity_splits(aid))
    except Exception as e:
        print(f"  splits failed: {e}")

    # any workout linkage on the base object?
    workout_id = (base.get("workoutId")
                  or (base.get("metadataDTO") or {}).get("workoutId")
                  or (base.get("summaryDTO") or {}).get("workoutId"))
    if workout_id:
        print(f"\n  workoutId = {workout_id} → fetching planned workout definition")
        try:
            wk = g.get_workout_by_id(workout_id)
            dump("workout.json", wk)
        except Exception as e:
            print(f"  workout fetch failed: {e}")
            wk = None
    else:
        print("\n  no workoutId found on the activity object "
              "(ride may not have been done from a structured workout)")
        wk = None

    # focused summary of interesting keys
    needles = ("power", "ftp", "target", "step", "intensity", "zone", "workout")
    print("\n=== interesting keys in activity.json ===")
    for kp, v in find_keys(base, needles):
        print(f"  {kp} = {v}")
    if wk:
        print("\n=== interesting keys in workout.json ===")
        for kp, v in find_keys(wk, needles):
            print(f"  {kp} = {v}")

    print(f"\nRaw files in: {OUT_DIR}")
    print("Send me the summary above (or the JSON files) and I'll wire up the parser.")


if __name__ == "__main__":
    main(sys.argv[1:])
