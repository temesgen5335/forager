"""Translate friendly schedule knobs into systemd OnCalendar expressions.

The deploy timers (`deploy/jobagent-*.timer`) carry OnCalendar placeholders that
`scripts/install_services.sh` fills in from `.env` — so an operator sets
`INGEST_EVERY_HOURS` and `DIGEST_AT` in one place and re-runs the installer, rather
than hand-writing systemd calendar syntax. These functions are that single source of
truth; the installer invokes them via `python -m jobagent.scheduling ...`.

GitHub Actions is deliberately out of scope: its `on.schedule.cron` must be a literal
(GitHub forbids expressions/variables there), so the free-tier digest cron stays edited
directly in `.github/workflows/digest.yml`.
"""

import sys


def oncalendar_every_hours(hours: int) -> str:
    """systemd OnCalendar firing every ``hours`` hours, on the hour (1..24).

    Note the systemd ``00/N`` step: for an N that does not divide 24 evenly (5, 7, ...)
    the last fire of the day is followed by a shorter gap across midnight — pick a
    divisor of 24 for an even cadence.
    """
    if isinstance(hours, bool) or not isinstance(hours, int):
        raise TypeError(f"hours must be an int, got {type(hours).__name__}")
    if not 1 <= hours <= 24:
        raise ValueError(f"hours must be between 1 and 24, got {hours}")
    return f"*-*-* 00/{hours}:00:00"


def oncalendar_daily_at(hhmm: str) -> str:
    """systemd OnCalendar firing once a day at ``hhmm`` ("H:MM" or "HH:MM"), server-local."""
    if not isinstance(hhmm, str):
        raise TypeError(f"time must be a string, got {type(hhmm).__name__}")
    parts = hhmm.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"time must be HH:MM, got {hhmm!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range (00:00-23:59), got {hhmm!r}")
    return f"*-*-* {hour:02d}:{minute:02d}:00"


_COMMANDS = {
    "oncalendar-every-hours": lambda a: oncalendar_every_hours(int(a)),
    "oncalendar-daily-at": oncalendar_daily_at,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in _COMMANDS:
        cmds = " | ".join(_COMMANDS)
        print(f"usage: python -m jobagent.scheduling <{cmds}> <value>", file=sys.stderr)
        return 2
    try:
        print(_COMMANDS[argv[0]](argv[1]))
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
