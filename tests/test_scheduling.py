"""Schedule cadence is config-driven: friendly knobs (.env) translate to systemd
OnCalendar, so an operator changes the cadence without hand-writing calendar syntax.

These translators are the single source of truth the installer calls (as a CLI) to
render the timer units, so they are pure and exhaustively covered here. Offline, no
network, no systemd needed (R17)."""

import re
import subprocess
import sys
from pathlib import Path

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

    def test_bool_rejected(self):
        # bool subclasses int, so without the isinstance-bool guard True would render as
        # "00/True:00:00"; the guard must reject it.
        with pytest.raises(TypeError):
            oncalendar_every_hours(True)


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

    def test_non_str_rejected(self):
        with pytest.raises(TypeError):
            oncalendar_daily_at(700)


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

    @pytest.mark.parametrize(
        "args", [(), ("oncalendar-daily-at",), ("oncalendar-daily-at", "07:00", "extra")]
    )
    def test_wrong_argument_count_exits_nonzero(self, args):
        r = self._run(*args)
        assert r.returncode != 0


class TestInstallerPlaceholderContract:
    """Lock the token contract between the CLI output, install_services.sh's sed, and the
    timer templates. A rename on one side must break a test here, not a production install."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_every_timer_placeholder_is_substituted_by_the_installer(self):
        installer_tokens = set(
            re.findall(r"__[A-Z_]+__", (self.ROOT / "scripts" / "install_services.sh").read_text())
        )
        timer_tokens = set()
        for name in ("jobagent-ingest.timer", "jobagent-pipeline.timer"):
            timer_tokens |= set(re.findall(r"__[A-Z_]+__", (self.ROOT / "deploy" / name).read_text()))
        missing = timer_tokens - installer_tokens
        assert not missing, f"timer placeholders the installer never substitutes: {missing}"
        # Guards against all three schedule tokens being renamed away on both sides at once.
        assert {"__INGEST_ONCALENDAR__", "__DIGEST_ONCALENDAR__", "__INGEST_JITTER__"} <= timer_tokens

    def test_rendered_oncalendar_is_sed_safe(self):
        # The installer substitutes these via `s|__TOKEN__|value|`, so a value must never
        # carry sed's delimiter, an ampersand, a backslash, or a newline.
        for val in (
            oncalendar_every_hours(4),
            oncalendar_every_hours(24),
            oncalendar_daily_at("07:00"),
            oncalendar_daily_at("23:59"),
        ):
            assert not (set(val) & set("|&\\\n")), f"sed-unsafe OnCalendar: {val!r}"
