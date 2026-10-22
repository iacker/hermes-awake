"""Files shared by every Hermes process: settings, deadlines (pause/hold) and the journal.

Everything lives in ~/.hermes/hermes-awake/ (not $TMPDIR: Hermes prunes it and it differs
per process). Sleep is a machine-wide resource, so the directory is shared by all profiles.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def root() -> Path:
    path = Path(os.environ.get("HERMES_AWAKE_DIR") or Path.home() / ".hermes" / "hermes-awake")
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- settings -----------------------------------------------------------------------------

@dataclass
class Settings:
    battery_floor: int = 20         # % on battery under which nothing is held
    max_turn_hours: float = 3.0     # a turn older than this is considered hung (use hold for longer)
    turn_silence_minutes: float = 15.0  # no LLM/tool activity for this long: the turn is gone
    grace_seconds: int = 90         # stay awake between chained turns (kanban, cron, retries)
    lid: bool = True                # also hold with the lid closed (needs the optional lid helper)
    lid_battery_hours: float = 3.0  # budget for lid closed on battery, then let it sleep
    thermal_stop: bool = True
    notify: bool = True
    max_hold_hours: float = 12.0    # cap on a single hold: a forgotten release still ends
    tick_seconds: int = 30          # while awake; idle processes check half as often

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads((root() / "config.json").read_text())
        except (OSError, ValueError):
            raw = {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        (root() / "config.json").write_text(json.dumps(asdict(self), indent=2) + "\n")

    def with_value(self, key: str, text: str) -> "Settings":
        kind = {f.name: f.type for f in fields(self)}.get(key)
        if kind is None:
            raise KeyError(f"unknown setting {key!r}; known: {', '.join(asdict(self))}")
        if kind == "bool":
            value = text.lower() in ("1", "true", "on", "yes")
        else:
            value = {"int": int, "float": float}[kind](text)
        return Settings(**{**asdict(self), key: value})


# -- deadlines: "pause until" and "hold until" ----------------------------------------------

def _deadline_file(name: str) -> tuple[float, str]:
    """A deadline file holds `<epoch> <why>`; a missing or corrupt file means no deadline."""
    try:
        until, _, why = (root() / name).read_text().partition(" ")
        return float(until), why.strip()
    except (OSError, ValueError):
        return 0.0, ""


def deadline(name: str) -> float:
    return _deadline_file(name)[0]


def deadline_reason(name: str) -> str:
    return _deadline_file(name)[1]


def set_deadline(name: str, seconds: float | None, why: str = "") -> None:
    path = root() / name
    if seconds:
        path.write_text(f"{time.time() + seconds} {' '.join(why.split())}".rstrip())
    else:
        path.unlink(missing_ok=True)


PAUSE, HOLD = "pause_until", "hold_until"


# -- journal ------------------------------------------------------------------------------

_JOURNAL_MAX = 512 * 1024


def journal(event: str, **fields_) -> None:
    path = root() / "events.jsonl"
    line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(),
                       "event": event, **fields_})
    try:
        if path.exists() and path.stat().st_size > _JOURNAL_MAX:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def journal_tail(n: int = 20) -> list[str]:
    try:
        return (root() / "events.jsonl").read_text().splitlines()[-n:]
    except OSError:
        return []
