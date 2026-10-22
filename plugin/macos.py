"""Thin wrappers around the macOS power tools. Every side effect is a named function."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from . import state

PMSET = "/usr/bin/pmset"
CAFFEINATE = "/usr/bin/caffeinate"
IOREG = "/usr/sbin/ioreg"
OSASCRIPT = "/usr/bin/osascript"

LID_MODE_HINT = "optional, see lid-mode/ in https://github.com/iacker/hermes-awake"


def run(*argv: str, timeout: float = 5.0) -> tuple[int, str]:
    """Run a command, never raise: (returncode, stdout)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


@dataclass(frozen=True)
class Power:
    on_battery: bool = False
    percent: int | None = None
    lid_closed: bool = False
    hot: bool = False

    def __str__(self) -> str:
        source = f"battery {self.percent}%" if self.on_battery else "AC"
        lid = "lid closed" if self.lid_closed else "lid open"
        return f"{source}, {lid}, {'HOT' if self.hot else 'thermal ok'}"


def power() -> Power:
    _, batt = run(PMSET, "-g", "batt")
    _, lid = run(IOREG, "-r", "-k", "AppleClamshellState", "-d", "1")
    _, therm = run(PMSET, "-g", "therm")
    percent = re.search(r"(\d+)%", batt)
    return Power(
        on_battery="Battery Power" in batt,
        percent=int(percent[1]) if percent else None,
        lid_closed='"AppleClamshellState" = Yes' in lid,
        hot=_is_hot(therm),
    )


def _is_hot(therm: str) -> bool:
    """Best effort: macOS reports throttling as a CPU speed limit or a warning level."""
    speed = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
    level = re.search(r"warning level (?:set to|is) (\d+)", therm, re.IGNORECASE)
    return bool((speed and int(speed[1]) < 70) or (level and int(level[1]) > 0))


def sleep_disabled() -> bool:
    _, out = run(PMSET, "-g")
    return bool(re.search(r"SleepDisabled\s+1", out))


def lid_helper() -> str | None:
    """Lid-closed mode is an opt-in helper the user installs (lid-mode/). The plugin never
    escalates privileges itself: it only runs this helper with `on`, `off` or `check`."""
    path = state.root() / "lid-helper"
    return str(path) if os.access(path, os.X_OK) else None


def set_sleep_disabled(disabled: bool) -> bool:
    helper = lid_helper()
    return bool(helper) and run(helper, "on" if disabled else "off")[0] == 0


def lid_mode_installed() -> bool:
    helper = lid_helper()
    return bool(helper) and run(helper, "check")[0] == 0


def keep_awake(pid: int) -> subprocess.Popen:
    """Idle-sleep assertion that dies with `pid` (-w). `-s` only matters on AC."""
    return subprocess.Popen([CAFFEINATE, "-i", "-s", "-w", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(title: str, body: str) -> None:
    """Notification Center banner. Text goes through argv, never into the script."""
    subprocess.Popen(
        [OSASCRIPT, "-e", "on run argv", "-e",
         "display notification (item 2 of argv) with title (item 1 of argv)",
         "-e", "end run", title, body],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
