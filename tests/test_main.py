"""main() and the shared plumbing it leans on: log, fmt_dur, Progress, login."""
import contextlib
import io
import unittest
from unittest import mock

import garmin_export as ge
import tests.stubs  # noqa: F401  (quiet, non-TTY, no sleeping)

ALL = (*ge.DOMAINS, ge.RAW_DOMAIN)


class Clock:
    """A settable stand-in for time.time."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def keep_globals(test, *names):
    """Restore module globals a test flips (QUIET, IS_TTY) when it ends."""
    for n in names:
        test.addCleanup(setattr, ge, n, getattr(ge, n))


def captured(fn, *args, **kw):
    """Run fn with stdout/stderr captured; returns (stdout, stderr, exit code or None)."""
    out, err, code = io.StringIO(), io.StringIO(), None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*args, **kw)
        except SystemExit as e:
            code = e.code
    return out.getvalue(), err.getvalue(), code


# ---------------------------------------------------------------------------- main
class MainCase(unittest.TestCase):
    """main() with every runner replaced by a recorder and login() by a stub."""

    def setUp(self):
        keep_globals(self, "QUIET")
        self.calls, self.g, self.fail = [], object(), {}

        def runner(name):
            def run(g, mode):
                self.calls.append((name, g, mode))
                if name in self.fail:
                    raise self.fail[name]
            return run

        runners = mock.patch.object(ge, "RUNNERS", {n: runner(n) for n in ALL})
        runners.start()
        self.addCleanup(runners.stop)
        login = mock.patch.object(ge, "login", return_value=self.g)
        self.login = login.start()
        self.addCleanup(login.stop)

    def main(self, *argv):
        return captured(ge.main, list(argv))


class MainArgs(MainCase):
    def test_help_prints_the_usage_and_runs_nothing(self):
        for arg in ("-h", "--help", "help", "HELP"):
            out, err, code = self.main(arg)
            self.assertEqual(out, ge.USAGE + "\n", arg)
            self.assertEqual((err, code), ("", None), arg)
        self.assertEqual(self.calls, [])
        self.login.assert_not_called()

    def test_help_anywhere_after_valid_args_still_prints_the_usage(self):
        out, _, code = self.main("swim", "update", "--help")
        self.assertEqual((out, code), (ge.USAGE + "\n", None))
        self.assertEqual(self.calls, [])

    def test_an_unknown_arg_exits_with_the_usage(self):
        out, _, code = self.main("swim", "Bogus", "-h")
        self.assertEqual(code, f"Unknown arg: Bogus\n\n{ge.USAGE}")
        self.assertEqual(out, "")
        self.assertEqual(self.calls, [])
        self.login.assert_not_called()

    def test_no_args_runs_every_domain_but_raw_in_auto_mode(self):
        _, err, code = self.main()
        self.assertIsNone(code)
        self.assertEqual(self.calls, [(d, self.g, "auto") for d in ge.DOMAINS])
        self.login.assert_called_once_with()
        self.assertIn("● Garmin export — domains: swim, cycle, run, daily, body · mode: auto "
                      f"· dir: {ge.OUTPUT_DIR}", err)
        self.assertIn("✓ authenticated", err)

    def test_domain_and_mode_are_order_and_case_independent(self):
        for argv in (("swim", "update"), ("update", "swim"), ("SWIM", "Update")):
            self.calls.clear()
            self.main(*argv)
            self.assertEqual(self.calls, [("swim", self.g, "update")], argv)

    def test_the_last_domain_and_mode_given_win(self):
        self.main("swim", "init", "run", "update")
        self.assertEqual(self.calls, [("run", self.g, "update")])

    def test_all_excludes_raw_and_raw_runs_only_when_named(self):
        self.main("all", "update")
        self.assertEqual(self.calls, [(d, self.g, "update") for d in ge.DOMAINS])
        self.calls.clear()
        self.main("raw", "init")
        self.assertEqual(self.calls, [("raw", self.g, "init")])

    def test_quiet_flag_hides_routine_lines(self):
        for flag in ("-q", "--quiet", "-Q"):
            ge.QUIET = False
            _, err, _ = self.main(flag, "swim")
            self.assertTrue(ge.QUIET, flag)
            self.assertNotIn("authenticating", err, flag)
            self.assertIn("✓ authenticated", err, flag)
        ge.QUIET = False
        _, err, _ = self.main("swim")
        self.assertFalse(ge.QUIET)
        self.assertIn("· authenticating with saved Garmin tokens …", err)


class MainStatus(MainCase):
    def test_status_reports_every_domain_and_raw_without_logging_in(self):
        for argv in (("status",), ("all", "status")):
            self.calls.clear()
            out, err, code = self.main(*argv)
            self.assertEqual(out, f"Garmin export — data dir: {ge.OUTPUT_DIR}\n", argv)
            self.assertEqual((err, code), ("", None), argv)
            self.assertEqual(self.calls, [(d, None, "status") for d in ALL], argv)
        self.login.assert_not_called()

    def test_status_for_one_domain_reports_only_it(self):
        for d in ("swim", "raw"):
            self.calls.clear()
            self.main("status", d)
            self.assertEqual(self.calls, [(d, None, "status")])
        self.login.assert_not_called()


class StatusWithRealRunners(unittest.TestCase):
    def test_status_reports_every_domain_including_raw(self):
        out, _, code = captured(ge.main, ["status"])
        self.assertIsNone(code)
        prefixes = [ln[:8] for ln in out.splitlines()[1:]]
        self.assertEqual(prefixes, ["  swim  ", "  cycle ", "  run   ", "  daily ", "  body  ",
                                    "  raw   "])


class MainOutcome(MainCase):
    def test_success_logs_all_done(self):
        _, err, code = self.main("cycle")
        self.assertIsNone(code)
        self.assertRegex(err, r"● all done · 1 domain\(s\) · total +\d+s\n$")

    def test_a_failing_domain_is_logged_the_rest_still_run_and_the_exit_is_1(self):
        self.fail = {"swim": RuntimeError("swim broke"), "daily": ValueError("daily broke")}
        _, err, code = self.main("update")

        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [(d, self.g, "update") for d in ge.DOMAINS])
        self.assertIn("✗ swim FAILED: swim broke", err)
        self.assertIn("✗ daily FAILED: daily broke", err)
        self.assertRegex(err, r"✗ finished with errors in: swim, daily · total +\d+s\n$")
        self.assertNotIn("all done", err)

    def test_ctrl_c_in_a_domain_stops_the_whole_run(self):
        self.fail = {"cycle": KeyboardInterrupt()}
        with self.assertRaises(KeyboardInterrupt):
            self.main()
        self.assertEqual([c[0] for c in self.calls], ["swim", "cycle"])


# ---------------------------------------------------------------------------- login
class FakeGarmin:
    error = None

    def __init__(self):
        self.logins = []

    def login(self, store):
        self.logins.append(store)
        if self.error:
            raise self.error


class Login(unittest.TestCase):
    def test_login_uses_the_saved_tokens(self):
        with mock.patch.object(ge, "Garmin", FakeGarmin):
            g = ge.login()
        self.assertIsInstance(g, FakeGarmin)
        self.assertEqual(g.logins, [ge.TOKEN_STORE])

    def test_a_failed_login_exits_pointing_at_garmin_mcp_auth(self):
        class Expired(FakeGarmin):
            error = RuntimeError("token expired")
        with mock.patch.object(ge, "Garmin", Expired), self.assertRaises(SystemExit) as cm:
            ge.login()
        self.assertEqual(cm.exception.code, "Login failed (token expired).\n"
                                             "Run this once in a terminal:  garmin-mcp-auth")


# ---------------------------------------------------------------------------- log / fmt_dur
class FmtDur(unittest.TestCase):
    def test_durations(self):
        cases = {0: "0s", 0.5: "0s", 1.5: "2s", 59.4: "59s", 59.5: "1m00s", 60: "1m00s",
                 185: "3m05s", 3599: "59m59s", 3600: "1h00m", 3725: "1h02m", 7140: "1h59m",
                 90061: "25h01m", -5: "-5s"}
        for seconds, want in cases.items():
            self.assertEqual(ge.fmt_dur(seconds), want, seconds)


class Log(unittest.TestCase):
    def setUp(self):
        keep_globals(self, "QUIET")
        clock = Clock(1000.0)
        for p in (mock.patch.object(ge.time, "time", clock),
                  mock.patch.object(ge, "_T0", 935.0)):
            p.start()
            self.addCleanup(p.stop)

    def log(self, *args):
        return captured(ge.log, *args)[1]

    def test_line_format_is_elapsed_icon_message(self):
        ge.QUIET = False
        self.assertEqual(self.log("hello"), "[+1m05s] · hello\n")
        ge._T0 = 993.0
        self.assertEqual(self.log("done", "✓"), "[+   7s] ✓ done\n")

    def test_quiet_hides_only_routine_lines(self):
        ge.QUIET = True
        self.assertEqual(self.log("routine"), "")
        self.assertEqual(self.log("routine", "·"), "")
        for icon in ("✓", "!", "✗", "●", "▶"):
            self.assertEqual(self.log("shown", icon), f"[+1m05s] {icon} shown\n")


# ---------------------------------------------------------------------------- Progress
class ProgressCase(unittest.TestCase):
    def setUp(self):
        keep_globals(self, "QUIET", "IS_TTY")
        self.clock = Clock(100.0)
        for p in (mock.patch.object(ge.time, "time", self.clock),
                  mock.patch.object(ge, "_T0", 100.0)):
            p.start()
            self.addCleanup(p.stop)

    def mode(self, tty, quiet):
        ge.IS_TTY, ge.QUIET = tty, quiet

    def steps(self, p, times, notes=None):
        """Step p once per clock time; returns what landed on stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for i, t in enumerate(times):
                self.clock.t = t
                p.step(notes[i] if notes else "")
        return err.getvalue()


class ProgressRender(ProgressCase):
    def test_render_line(self):
        p = ge.Progress("raw", 10)
        p.n = 5
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p._render(110.0, "2026-01-01 running", inplace=False)
            p._render(110.0, "", inplace=True)
        line = "  raw [###########-----------] 5/10   50% ·  0.5/s · ETA   10s"
        self.assertEqual(err.getvalue(),
                         f"{line} · 2026-01-01 running\n\r{line}\033[K")

    def test_render_with_no_time_elapsed_and_no_total(self):
        p = ge.Progress("x", None)
        self.assertEqual(p.total, 0)
        self.assertEqual(ge.Progress("x", -3).total, 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p._render(100.0, "", inplace=False)
        self.assertEqual(err.getvalue(),
                         "  x [----------------------] 0/0    0% ·  0.0/s · ETA    0s\n")


class ProgressEdges(ProgressCase):
    def test_render_before_any_time_has_passed_shows_no_rate_or_eta(self):
        p = ge.Progress("x", 10)
        p.n = 3
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p._render(100.0, "", inplace=False)
        self.assertEqual(err.getvalue(),
                         "  x [######----------------] 3/10   30% ·  0.0/s · ETA    0s\n")

    def test_piped_milestones_are_every_twentieth_of_the_total(self):
        self.mode(tty=False, quiet=False)
        out = self.steps(ge.Progress("lbl", 100), [101.0] * 100)
        counts = [ln.split("] ")[1].split("/")[0] for ln in out.splitlines()]
        self.assertEqual(counts, [str(n) for n in range(5, 101, 5)])

    def test_tty_redraws_once_exactly_0_15s_have_passed(self):
        self.mode(tty=True, quiet=False)
        self.clock.t = 0.0
        p = ge.Progress("lbl", 3)
        self.assertEqual(self.steps(p, [0.15]).count("\r"), 1)


class ProgressStep(ProgressCase):
    def test_tty_redraws_in_place_at_most_every_0_15s_and_always_at_the_end(self):
        self.mode(tty=True, quiet=False)
        p = ge.Progress("lbl", 4)
        out = self.steps(p, [100.05, 100.10, 100.30, 100.31], ["a", "b", "c", "d"])
        frames = out.split("\r")[1:]
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(f.endswith("\033[K") for f in frames))
        self.assertEqual([f.removesuffix("\033[K").rsplit(" · ", 1)[1] for f in frames],
                         ["a", "c", "d"])
        self.assertIn(" 4/4  100% ", frames[-1])
        self.assertNotIn("\n", out)

    def test_piped_prints_about_every_5_percent_and_the_last_step(self):
        self.mode(tty=False, quiet=False)
        out = self.steps(ge.Progress("lbl", 45), [101.0] * 45)
        counts = [ln.split("] ")[1].split(" ")[0] for ln in out.splitlines()]
        self.assertEqual(counts, [f"{n}/45" for n in range(2, 45, 2)] + ["45/45"])

    def test_piped_small_totals_print_every_step(self):
        self.mode(tty=False, quiet=False)
        self.assertEqual(len(self.steps(ge.Progress("lbl", 3), [101.0] * 3).splitlines()), 3)

    def test_piped_with_no_total_prints_every_step(self):
        """With no total, done (n >= 0) is always true, so every step prints."""
        self.mode(tty=False, quiet=False)
        out = self.steps(ge.Progress("lbl", 0), [101.0] * 3)
        self.assertEqual([ln.split("] ")[1].split(" ")[0] for ln in out.splitlines()],
                         ["1/0", "2/0", "3/0"])

    def test_quiet_prints_nothing_tty_or_not(self):
        for tty in (True, False):
            self.mode(tty=tty, quiet=True)
            p = ge.Progress("lbl", 2)
            self.assertEqual(self.steps(p, [101.0, 102.0]), "", tty)
            self.assertEqual(p.n, 2)


class ProgressDone(ProgressCase):
    def done(self):
        self.clock.t = 100.0
        p = ge.Progress("lbl", 4)
        self.clock.t = 105.0
        return captured(p.done, "4 things")[1]

    def test_tty_finishes_the_bar_line_then_logs(self):
        self.mode(tty=True, quiet=False)
        self.assertEqual(self.done(), "\n[+   5s] ✓ lbl: 4 things in 5s\n")

    def test_piped_or_quiet_just_logs(self):
        for tty, quiet in ((False, False), (True, True), (False, True)):
            self.mode(tty=tty, quiet=quiet)
            self.assertEqual(self.done(), "[+   5s] ✓ lbl: 4 things in 5s\n", (tty, quiet))


if __name__ == "__main__":
    unittest.main()
