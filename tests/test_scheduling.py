"""Schedule cadence is config-driven: friendly knobs (.env) translate to systemd
OnCalendar, so an operator changes the cadence without hand-writing calendar syntax.

These translators are the single source of truth the installer calls (as a CLI) to
render the timer units, so they are pure and exhaustively covered here. Offline, no
network, no systemd needed (R17)."""

import subprocess
import sys

import pytest

from jobagent.scheduling import oncalendar_daily_at, oncalendar_every_hours


class TestOnCalendarEveryHours:
    def test_common_interval(self):
        assert oncalendar_every_hours(4) == "*-*-* 00/4:00:00"

    def test_hourly(self):
        assert oncalendar_every_hours(1) == "*-*-* 00/1:00:00"

    def test_daily_boundary(self):
        # 24 collapses to a single midnight fire — still valid systemd.
        assert oncalendar_every_hours(24) == "*-*-* 00/24:00:00"

    @pytest.mark.parametrize("bad", [0, -1, 25, 100])
    def test_out_of_range_rejected(self, bad):
        with pytest.raises(ValueError):
            oncalendar_every_hours(bad)

    def test_non_int_rejected(self):
        # The Python API takes an int; the CLI is what parses strings.
        with pytest.raises((ValueError, TypeError)):
            oncalendar_every_hours("4")


class TestOnCalendarDailyAt:
    def test_padded(self):
        assert oncalendar_daily_at("07:00") == "*-*-* 07:00:00"

    def test_with_minutes(self):
        assert oncalendar_daily_at("08:30") == "*-*-* 08:30:00"

    def test_single_digit_hour_is_normalized(self):
        assert oncalendar_daily_at("7:05") == "*-*-* 07:05:00"

    def test_midnight_and_end_of_day(self):
        assert oncalendar_daily_at("00:00") == "*-*-* 00:00:00"
        assert oncalendar_daily_at("23:59") == "*-*-* 23:59:00"

    @pytest.mark.parametrize(
        "bad", ["24:00", "07:60", "-1:00", "aa:bb", "0700", "7", "", "07:00:00"]
    )
    def test_malformed_rejected(self, bad):
        with pytest.raises(ValueError):
            oncalendar_daily_at(bad)


class TestCli:
    """install_services.sh (bash) calls these as a subprocess to render the units."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "jobagent.scheduling", *args],
            capture_output=True,
            text=True,
        )

    def test_every_hours_stdout(self):
        r = self._run("oncalendar-every-hours", "6")
        assert r.returncode == 0
        assert r.stdout.strip() == "*-*-* 00/6:00:00"

    def test_daily_at_stdout(self):
        r = self._run("oncalendar-daily-at", "08:30")
        assert r.returncode == 0
        assert r.stdout.strip() == "*-*-* 08:30:00"

    def test_bad_input_exits_nonzero_with_stderr(self):
        r = self._run("oncalendar-every-hours", "0")
        assert r.returncode != 0
        assert r.stderr.strip()

    def test_unknown_command_exits_nonzero(self):
        r = self._run("frobnicate", "9")
        assert r.returncode != 0
