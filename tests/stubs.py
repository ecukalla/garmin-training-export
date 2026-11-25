"""Shared fakes: a Garmin client stub and helpers for throwaway activity specs."""
import csv
import os

import garmin_export as ge

ge.time.sleep = lambda s: None          # the exporter paces its requests; tests needn't
ge.QUIET, ge.IS_TTY = True, False       # no progress bars or routine log lines in test output


def act(aid, day, type_key="lap_swimming", **extra):
    """A Garmin activity-list entry."""
    return {"activityId": aid, "startTimeLocal": f"{day} 08:00:00",
            "activityType": {"typeKey": type_key}, **extra}


class StubGarmin:
    """Answers a date-range listing the way Garmin does — only what falls inside it — and
    any other endpoint with whatever `answers` maps its name to (default: empty)."""

    def __init__(self, acts=(), **answers):
        self.acts, self.answers, self.calls = list(acts), answers, []

    def get_activities_by_date(self, start, end=None, *a, **k):
        self.calls.append(("get_activities_by_date", start, end))
        return [x for x in self.acts if start <= x["startTimeLocal"][:10] <= (end or "9999")]

    def __getattr__(self, name):
        def endpoint(*a, **k):
            self.calls.append((name, *a))
            answer = self.answers.get(name, {})
            if isinstance(answer, BaseException):
                raise answer
            return answer(*a, **k) if callable(answer) else answer
        return endpoint


def spec(base, tag):
    """A copy of an activity spec (ge.SWIM/CYCLE/RUN) writing to its own files."""
    d = dict(base)
    for k in ("summary_csv", "parts_csv", "state"):
        d[k] = os.path.join(ge.OUTPUT_DIR, f"{tag}_{k}")
    return d


def rows(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))
